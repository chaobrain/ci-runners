#!/usr/bin/env python3
"""Conformance check for the sandbox (prototype): does the confinement hold?

Experimental: wired into nothing. Standard library only (the G items load the host's
``libcuda.so.1`` through ctypes and import ``jax`` from the Python environment).

    check.py suite --variant control|subuid-gpu|subuid-kept|subuid-none|single --scratch DIR
                   --env DIR [--card N] [--proxy-port P] [--out FILE]
    check.py cost  --scratch DIR --env DIR --card N [--proxy-port P] [--repeat 3] [--out FILE]

``suite`` creates its own markers under ``--scratch`` -- a marker file outside the job's
tree, a second one readable only by its owner and one of the account's groups other
than the GPU group, a marker process (which allows any same-uid tracer, so a trace attach can succeed
when nothing confines it) and a marker TCP listener on the host's loopback -- then runs
the item checks inside the variant's sandbox (``control``: with no sandbox at all) and
removes the markers. Every item states what a confined job must observe; the control
must report "not confined" on the confinement items, which shows the check can tell.
``cost`` times a small GPU test inside the sandbox against outside.
"""
from __future__ import annotations

import argparse
import ctypes
import errno
import glob
import grp
import json
import os
import pwd
import secrets
import shutil
import socket
import stat
import statistics
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SANDBOX = os.path.join(HERE, 'sandbox.py')
VARIANTS = {'control': None, 'subuid-gpu': ('subuid', 'gpu'), 'subuid-kept': ('subuid', 'kept'),
            'subuid-none': ('subuid', 'none'), 'single': ('single', 'kept')}
INSIDE_RELAY = 'http://127.0.0.1:3128'
ALLOWED_URL = 'https://api.github.com/zen'
DENIED_URL = 'https://example.com/'
PATTERN = 0x5A17C0DE
FILL_MIB, SCAN_MIB = 384, 512

EXPECTED = {
    'I1': ("the job's kernel uid and gid, read outside, are a subordinate pair and its kernel groups are "
           "the GPU group alone (no group under --groups none or without a card); recorded inside and outside"),
    'F1': 'a marker file outside the tree is not visible',
    'F2': 'creating a file in the read-only hook/policy stand-in fails',
    'F3': "the account's home path does not exist",
    'F4': 'writing outside the per-job tree (and the per-job /tmp) fails',
    'F5': ("a file outside the tree readable by one of the account's other groups (mode 0640) is not visible, "
           "and the job's kernel credentials, read outside, could not open it"),
    'P1': "/proc lists only the sandbox's processes; the marker is not among them",
    'P2': "signal 0 to the marker's pid fails",
    'P3': 'a trace attach to the marker fails',
    'N1': "connecting to the marker listener on the host's loopback fails",
    'N2': 'the forwarded proxy port is not reachable directly',
    'N3': 'through the relay the allowlisted URL answers and a non-allowlisted one is refused by the proxy',
    'K1': 'creating a nested user namespace fails',
    'K2': 'mount fails (also after trying a new user namespace)',
    'K3': 'mknod of a device node fails',
    'G1': 'the leased card works (jax.devices(); one small matmul)',
    'G2': "no other card's node exists; opening one fails",
    'G3': "record what nvidia-smi shows of other cards and of processes outside (a marker of ours holds 64 MiB on the card)",
    'G4': "record whether a fresh allocation reads the previous process's pattern",
    'R1': 'record whether a process of the account outside can trace the job (the reverse direction)',
}
INFO_ITEMS = {'G3', 'G4', 'R1'}          # recorded, not pass/fail
FUNCTIONAL_ITEMS = {'G1', 'N3'}    # must work in every variant, control included

MARKER_CODE = (
    'import ctypes, sys, time\n'
    'ctypes.CDLL(None).prctl(0x59616d61, ctypes.c_ulong(-1), 0, 0, 0)  # PR_SET_PTRACER_ANY\n'
    'time.sleep(3600)\n'
)

GPU_MARKER_CODE = r'''
import ctypes, sys, time
cu = ctypes.CDLL('libcuda.so.1')
dev, ctx, ptr = ctypes.c_int(), ctypes.c_void_p(), ctypes.c_uint64()
assert cu.cuInit(0) == 0 and cu.cuDeviceGet(ctypes.byref(dev), 0) == 0
assert cu.cuDevicePrimaryCtxRetain(ctypes.byref(ctx), dev) == 0 and cu.cuCtxSetCurrent(ctx) == 0
assert cu.cuMemAlloc_v2(ctypes.byref(ptr), ctypes.c_size_t(64 << 20)) == 0
print('ready', flush=True)
time.sleep(600)
'''

G1_CODE = r'''
import json, time
t = time.time()
import jax, jax.numpy as jnp
d = jax.devices()
x = jnp.ones((1024, 1024), jnp.float32)
r = float((x @ x).sum())
print(json.dumps({'devices': [str(i) for i in d], 'platform': d[0].platform, 'result': r,
                  'secs': round(time.time() - t, 2)}))
'''

GPU_MEM_CODE = r'''
import ctypes, json, os, sys
mode, mib, pattern = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
cu = ctypes.CDLL('libcuda.so.1')
def ok(r, what):
    if r:
        print(json.dumps({'error': f'{what} returned {r}'})); sys.exit(1)
ok(cu.cuInit(0), 'cuInit')
dev = ctypes.c_int(); ok(cu.cuDeviceGet(ctypes.byref(dev), 0), 'cuDeviceGet')
ctx = ctypes.c_void_p(); ok(cu.cuDevicePrimaryCtxRetain(ctypes.byref(ctx), dev), 'cuDevicePrimaryCtxRetain')
ok(cu.cuCtxSetCurrent(ctx), 'cuCtxSetCurrent')
n = mib << 20
ptr = ctypes.c_uint64(); ok(cu.cuMemAlloc_v2(ctypes.byref(ptr), ctypes.c_size_t(n)), 'cuMemAlloc')
if mode == 'fill':
    ok(cu.cuMemsetD32_v2(ptr, ctypes.c_uint(pattern), ctypes.c_size_t(n // 4)), 'cuMemsetD32')
    ok(cu.cuCtxSynchronize(), 'cuCtxSynchronize')
    print(json.dumps({'filled_mib': mib})); sys.stdout.flush()
    os._exit(0)  # exit without freeing: the driver reclaims the memory
buf = ctypes.create_string_buffer(n)
ok(cu.cuMemcpyDtoH_v2(buf, ptr, ctypes.c_size_t(n)), 'cuMemcpyDtoH')
raw = buf.raw
words = n // 4
print(json.dumps({'scanned_mib': mib, 'pattern_words': raw.count(pattern.to_bytes(4, 'little')),
                  'zero_words_approx': raw.count(b'\0\0\0\0'), 'words': words}))
'''


# ============================================================================ inside

def _item(i, observed, held):
    return {'id': i, 'expected': EXPECTED[i], 'observed': observed, 'held': held}


def _errname(e: OSError) -> str:
    return errno.errorcode.get(e.errno, str(e.errno)) if e.errno else type(e).__name__


def _in_child(fn) -> dict:
    """Run ``fn()`` in a forked child (so it cannot change this process); return its dict."""
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        try:
            out = fn()
        except BaseException as e:  # noqa: BLE001
            out = {'error': repr(e)}
        os.write(w, json.dumps(out).encode())
        os._exit(0)
    os.close(w)
    data = b''
    while True:
        chunk = os.read(r, 65536)
        if not chunk:
            break
        data += chunk
    os.close(r)
    os.waitpid(pid, 0)
    return json.loads(data or b'{}')


_libc = ctypes.CDLL(None, use_errno=True)
_libc.ptrace.restype = ctypes.c_long
_libc.ptrace.argtypes = [ctypes.c_long, ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p]
_libc.mount.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_ulong, ctypes.c_char_p]


def _call(ret) -> str:
    if ret == 0:
        return 'ok'
    return errno.errorcode.get(ctypes.get_errno(), str(ctypes.get_errno()))


def i1(spec) -> dict:
    status = {}
    with open('/proc/self/status') as f:
        for line in f:
            k, _, v = line.partition(':')
            if k in ('CapEff', 'CapPrm', 'CapBnd', 'CapAmb', 'NoNewPrivs', 'Seccomp', 'NSpid'):
                status[k] = v.strip()
    names = []
    for g in os.getgroups():
        try:
            names.append(grp.getgrgid(g).gr_name)
        except KeyError:
            names.append(str(g))
    obs = {'resuid': os.getresuid(), 'resgid': os.getresgid(), 'groups': os.getgroups(), 'group_names': names,
           **status}
    # let the suite read the kernel ids from outside while this process lives
    with open(spec['ids_ready'], 'w') as f:
        f.write(str(os.getpid()))
    deadline = time.time() + 20
    while time.time() < deadline and not os.path.exists(spec['ids_ack']):
        time.sleep(0.1)
    return _item('I1', obs, None)


def f1(spec):
    seen = os.path.exists(spec['marker_file'])
    return _item('F1', 'visible' if seen else 'not visible', not seen)


def f5(spec):
    p = spec['group_file']
    if not os.path.exists(p):
        return _item('F5', 'not visible', True)  # the credentials half is added outside
    try:
        with open(p) as f:
            f.read(1)
        return _item('F5', 'visible, opened', False)
    except OSError as e:
        return _item('F5', f'visible, open failed ({_errname(e)})', False)


def f2(spec):
    p = os.path.join(spec['policy'], f'f2-{spec["token"]}')
    try:
        with open(p, 'x') as f:
            f.write('x')
        os.unlink(p)
        return _item('F2', 'created', False)
    except OSError as e:
        return _item('F2', f'failed ({_errname(e)})', True)


def f3(spec):
    there = os.path.exists(spec['home'])
    return _item('F3', 'exists' if there else 'does not exist', not there)


def f4(spec):
    cands = [spec['scratch'], os.path.dirname(spec['scratch']), spec['home'], '/', '/etc', '/usr', '/usr/lib',
             '/opt', '/opt/env', '/var/tmp', '/dev', spec['env_prefix']]
    writable, tried = [], 0
    for d in dict.fromkeys(cands):
        if not os.path.isdir(d):
            continue
        tried += 1
        p = os.path.join(d, f'f4-{spec["token"]}')
        try:
            with open(p, 'x') as f:
                f.write('x')
            os.unlink(p)
            writable.append(d if d in ('/', '/etc', '/usr', '/usr/lib', '/opt', '/opt/env', '/var/tmp', '/dev')
                            else {spec['scratch']: '<scratch>', spec['home']: '<home>',
                                  spec['env_prefix']: '<env>'}.get(d, '<scratch parent>'))
        except OSError:
            pass
    obs = f'writable: {", ".join(writable)}' if writable else f'none of {tried} existing places writable'
    return _item('F4', obs, not writable)


def p1(spec):
    pids = [p for p in os.listdir('/proc') if p.isdigit()]
    found = False
    for p in pids:
        try:
            with open(f'/proc/{p}/cmdline', 'rb') as f:
                argv = f.read().split(b'\0')
            if spec['token'].encode() in argv and b'inside' not in argv:  # the marker, not this check
                found = True
        except OSError:
            pass
    try:
        with open('/proc/1/comm') as f:
            init = f.read().strip()
    except OSError:
        init = '?'
    obs = f'{len(pids)} processes, pid 1 is {init}, marker {"listed" if found else "not listed"}'
    return _item('P1', obs, (not found) and init not in ('systemd', 'init'))


def p2(spec):
    try:
        os.kill(spec['marker_pid'], 0)
        return _item('P2', 'signal delivered', False)
    except OSError as e:
        return _item('P2', f'failed ({_errname(e)})', True)


def p3(spec):
    def attach():
        scope = '?'
        try:
            with open('/proc/sys/kernel/yama/ptrace_scope') as f:
                scope = f.read().strip()
        except OSError:
            pass
        r = _libc.ptrace(0x4206, spec['marker_pid'], None, None)  # PTRACE_SEIZE; detached at exit
        return {'r': _call(r), 'scope': scope}
    out = _in_child(attach)
    ok = out.get('r') == 'ok'
    return _item('P3', f'{"attached" if ok else "failed (" + str(out.get("r")) + ")"}; yama ptrace_scope={out.get("scope")}',
                 not ok)


def _connect(port, timeout=3) -> str:
    try:
        s = socket.create_connection(('127.0.0.1', port), timeout=timeout)
        s.close()
        return 'connected'
    except OSError as e:
        return f'failed ({_errname(e)})'


def n1(spec):
    r = _connect(spec['marker_port'])
    ifs = sorted(n for _, n in socket.if_nameindex())
    return _item('N1', f'{r}; interfaces: {", ".join(ifs) if len(ifs) < 4 else str(len(ifs)) + " (lo and others)"}',
                 r != 'connected')


def n2(spec):
    if not spec.get('proxy_port'):
        return _item('N2', 'n/a (no forwarded proxy port on this host)', None)
    r = _connect(spec['proxy_port'])
    return _item('N2', r, r != 'connected')


def _fetch(url, proxy) -> str:
    import urllib.request
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({'https': proxy, 'http': proxy}))
    try:
        with opener.open(url, timeout=20) as r:
            return f'{r.status} {r.read(200).decode(errors="replace").strip()!r}'
    except Exception as e:  # noqa: BLE001
        return f'refused: {e}'.replace('\n', ' ')[:160]


def n3(spec):
    if not spec.get('relay_url'):
        return _item('N3', 'n/a (no proxy)', None)
    a = _fetch(ALLOWED_URL, spec['relay_url'])
    d = _fetch(DENIED_URL, spec['relay_url'])
    held = a.startswith('200 ') and d.startswith('refused') and '403' in d
    return _item('N3', f'zen: {a}; example.com: {d}', held)


def k1(spec):
    out = _in_child(lambda: {'r': _call(_libc.unshare(0x10000000))})
    return _item('K1', out.get('r', out), out.get('r') != 'ok')


def k2(spec):
    target = tempfile.mkdtemp(prefix='k2-')

    def attempt():
        plain = _call(_libc.mount(b'tmpfs', target.encode(), b'tmpfs', 0, None))
        uid, gid = os.getuid(), os.getgid()
        u = _call(_libc.unshare(0x10000000 | 0x00020000))
        after = 'not tried'
        if u == 'ok':
            try:
                for name, text in (('uid_map', f'0 {uid} 1'), ('setgroups', 'deny'), ('gid_map', f'0 {gid} 1')):
                    with open(f'/proc/self/{name}', 'w') as f:
                        f.write(text)
                after = _call(_libc.mount(b'tmpfs', target.encode(), b'tmpfs', 0, None))
            except OSError as e:
                after = f'map: {_errname(e)}'
        return {'plain': plain, 'unshare': u, 'after': after}
    out = _in_child(attempt)
    os.rmdir(target)
    held = out.get('plain') != 'ok' and out.get('after') != 'ok'
    return _item('K2', f'mount: {out.get("plain")}; unshare(user|mnt): {out.get("unshare")}; '
                       f'mount after it: {out.get("after")}', held)


def k3(spec):
    p = os.path.join(tempfile.gettempdir(), f'k3-{spec["token"]}')
    try:
        os.mknod(p, stat.S_IFCHR | 0o600, os.makedev(1, 3))
        os.unlink(p)
        return _item('K3', 'created', False)
    except OSError as e:
        return _item('K3', f'failed ({_errname(e)})', True)


def _py(spec, code, *args, timeout=240):
    env = dict(os.environ, XLA_PYTHON_CLIENT_PREALLOCATE='false', TF_CPP_MIN_LOG_LEVEL='3')
    t = time.time()
    r = subprocess.run([spec['env_python'], '-c', code, *args], capture_output=True, text=True,
                       timeout=timeout, env=env)
    last = [ln for ln in r.stdout.splitlines() if ln.startswith('{')]
    out = json.loads(last[-1]) if last else {'error': (r.stderr.strip().splitlines() or ['no output'])[-1][:200]}
    out['wall'] = round(time.time() - t, 2)
    return out


def g1(spec):
    out = _py(spec, G1_CODE)
    held = out.get('platform') == 'gpu' and len(out.get('devices', [])) == 1 and out.get('result') == 1024.0 ** 3
    if 'error' in out:
        return _item('G1', f'error: {out["error"]}', False)
    return _item('G1', f'{len(out["devices"])} device(s) {out["devices"]}; matmul sum {out["result"]:.0f}; '
                       f'{out["wall"]} s', held)


def g2(spec):
    nodes = sorted(glob.glob('/dev/nvidia[0-9]*'))
    mine = f'/dev/nvidia{spec["card"]}'
    others = [n for n in nodes if n != mine]
    opened = []
    for n in others:
        try:
            fd = os.open(n, os.O_RDWR)
            os.close(fd)
            opened.append(n)
        except OSError:
            pass
    probe = '/dev/nvidia{}'.format(0 if spec['card'] != 0 else 1)
    try:
        os.close(os.open(probe, os.O_RDWR))
        probe_r = 'opened'
    except OSError as e:
        probe_r = f'failed ({_errname(e)})'
    obs = f'{len(nodes)} card node(s), {len(others)} other(s), {len(opened)} other(s) openable; open {probe}: {probe_r}'
    return _item('G2', obs, not others and probe_r != 'opened')


def g3(spec):
    smi = shutil.which('nvidia-smi') or '/usr/bin/nvidia-smi'
    if not os.path.exists(smi):
        return _item('G3', 'no nvidia-smi', None)
    try:
        cards = subprocess.run([smi, '-L'], capture_output=True, text=True, timeout=60)
        mem = subprocess.run([smi, '--query-gpu=memory.used', '--format=csv,noheader'],
                             capture_output=True, text=True, timeout=60)
        apps = subprocess.run([smi, '--query-compute-apps=gpu_uuid,pid,used_memory', '--format=csv,noheader'],
                              capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return _item('G3', f'failed: {e}', None)
    lines = [ln for ln in apps.stdout.splitlines() if ln.count(',') >= 2]
    pids_shown = sum(1 for ln in lines if ln.split(',')[1].strip().isdigit()) if lines else 0
    own = {p for p in os.listdir('/proc') if p.isdigit()}
    foreign = sum(1 for ln in lines if ln.split(',')[1].strip() not in own)
    note = '' if apps.returncode == 0 else f'; query failed: {(apps.stdout + apps.stderr).strip()[:80]}'
    obs = (f'-L lists {len([c for c in cards.stdout.splitlines() if c.startswith("GPU")])} card(s)'
           f'{" (rc " + str(cards.returncode) + ")" if cards.returncode else ""}; compute apps listed: {len(lines)} '
           f'({pids_shown} with a pid, {foreign} not in this /proc); memory.used: {", ".join(mem.stdout.split(chr(10))[:6]).strip(", ")}{note}')
    return _item('G3', obs, None)


def g4(spec):
    a = _py(spec, GPU_MEM_CODE, 'fill', str(FILL_MIB), str(PATTERN), timeout=120)
    if 'error' in a:
        return _item('G4', f'fill failed: {a["error"]}', None)
    b = _py(spec, GPU_MEM_CODE, 'scan', str(SCAN_MIB), str(PATTERN), timeout=120)
    if 'error' in b:
        return _item('G4', f'scan failed: {b["error"]}', None)
    found = b['pattern_words']
    obs = (f'A filled {FILL_MIB} MiB with 0x{PATTERN:08X} and exited; B read {SCAN_MIB} MiB uninitialised: '
           f'pattern {"FOUND (" + str(found) + " words)" if found else "not found"}, '
           f'~{100 * b["zero_words_approx"] // b["words"]} % zero words')
    return _item('G4', obs, None)


ITEMS = [i1, f1, f2, f3, f4, f5, p1, p2, p3, n1, n2, n3, k1, k2, k3]
GPU_ITEMS = [g1, g2, g3, g4]


def inside(spec) -> int:
    results = []
    for fn in ITEMS + (GPU_ITEMS if spec.get('card') is not None else []):
        try:
            results.append(fn(spec))
        except Exception as e:  # noqa: BLE001
            results.append(_item(fn.__name__.upper(), f'check error: {e!r}', False))
    print(json.dumps({'results': results}))
    return 0


# ============================================================================ outside

def _marker_listener():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(('127.0.0.1', 0))
    s.listen(8)
    hits = []

    def serve():
        while True:
            try:
                c, _ = s.accept()
            except OSError:
                return
            hits.append(1)
            c.close()
    threading.Thread(target=serve, daemon=True).start()
    return s, hits


def _descendants(pid: int):
    todo, seen = [pid], []
    while todo:
        p = todo.pop()
        seen.append(p)
        try:
            for t in os.listdir(f'/proc/{p}/task'):
                with open(f'/proc/{p}/task/{t}/children') as f:
                    todo += [int(c) for c in f.read().split()]
        except OSError:
            pass
    return seen


def _kernel_ids(root_pid: int, inner_pid: int):
    for p in _descendants(root_pid):
        st = {}
        try:
            with open(f'/proc/{p}/status') as f:
                for line in f:
                    k, _, v = line.partition(':')
                    st[k] = v.split()
        except OSError:
            continue
        if st.get('NSpid') and int(st['NSpid'][-1]) == inner_pid:
            ids = {'pid_outside': p, 'uid': [int(x) for x in st['Uid']], 'gid': [int(x) for x in st['Gid']],
                   'groups': [int(x) for x in st.get('Groups', [])], 'NSpid': st['NSpid']}
            for name in ('uid_map', 'gid_map', 'setgroups'):
                try:
                    with open(f'/proc/{p}/{name}') as f:
                        ids[name] = [' '.join(ln.split()) for ln in f.read().splitlines()]
                except OSError:
                    pass
            return ids
    return None


def _sandbox_module():
    """sandbox.py (beside this file), for its ``/etc/subuid`` parsing."""
    import importlib.util
    spec = importlib.util.spec_from_file_location('sandbox', SANDBOX)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def expected_groups(gpu_gid, groups_mode) -> list:
    """Check 8's groups: the GPU group alone; none under ``--groups none`` or without a card.
    ``kept`` expects the GPU group alone too, so the account's other groups fail it."""
    return [gpu_gid] if gpu_gid is not None and groups_mode != 'none' else []


def identity_held(ids, account_uid: int, urange, grange, expected: list) -> bool:
    """Check 8: the job's kernel uid and gid (every one of real, effective, saved, fs) are a
    subordinate pair -- in the account's subordinate ranges (``(first, count)``, the lines
    the launcher maps; None when the account has none), at the same offset, not the
    account's -- and its kernel groups are exactly ``expected``."""
    if not ids or not urange or not grange:
        return False
    (uf, un), (gf, gn) = urange, grange
    offs = {u - uf for u in ids['uid']} | {g - gf for g in ids['gid']}
    return (len(offs) == 1 and 0 <= min(offs) < min(un, gn) and account_uid not in ids['uid']
            and sorted(set(ids['groups'])) == sorted(set(expected)))


def opened_by(ids, st) -> str:
    """Which of a file's permission classes lets credentials ``ids`` (from /proc/PID/status,
    outside) read it, by its mode bits: ``owner``, ``group``, ``others``, or '' (none; the
    fs uid, then the fs gid and groups; no capability inside the namespace reaches a file of
    an unmapped owner)."""
    fsuid, fsgid = ids['uid'][3], ids['gid'][3]
    if fsuid == st.st_uid:
        return 'owner' if st.st_mode & 0o400 else ''
    if st.st_gid == fsgid or st.st_gid in ids['groups']:
        return 'group' if st.st_mode & 0o040 else ''
    return 'others' if st.st_mode & 0o004 else ''


def f5_verdict(inside: str, hidden, ids, st) -> tuple[str, bool]:
    """F5 from the inside observation and the kernel ids read outside: held only when the
    mount namespace hid the file and the credentials could not open it."""
    if not ids:
        return f'mount namespace: {inside}; credentials: not read', False
    by = opened_by(ids, st)
    cred = f'the job could open it as its {by}' if by else 'the job is not its owner, not in its group, and it is not world-readable: it could not open it'
    return f'mount namespace: {inside}; credentials: {cred}', bool(hidden) and not by


def _card_uuid(card: int) -> str:
    """The UUID of the card whose device minor is ``card`` (CUDA's and nvidia-smi's indices
    follow their own order, not the minor), for CUDA_VISIBLE_DEVICES outside a sandbox."""
    for info in glob.glob('/proc/driver/nvidia/gpus/*/information'):
        fields = {}
        with open(info) as f:
            for ln in f:
                k, _, v = ln.partition(':')
                fields[k.strip()] = v.strip()
        if fields.get('Device Minor') == str(card):
            return fields['GPU UUID']
    raise SystemExit(f'check: no card with device minor {card}')


def _seize_from_outside(pid: int) -> dict:
    """PTRACE_SEIZE ``pid`` from a fresh process of the account (not an ancestor of it)."""
    code = ('import ctypes, errno, sys\n'
            'libc = ctypes.CDLL(None, use_errno=True)\n'
            'libc.ptrace.argtypes = [ctypes.c_long, ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p]\n'
            'r = libc.ptrace(0x4206, int(sys.argv[1]), None, None)\n'
            'print("ok" if r == 0 else errno.errorcode.get(ctypes.get_errno(), "?"))\n')
    r = subprocess.run([sys.executable, '-c', code, str(pid)], capture_output=True, text=True)
    return {'r': r.stdout.strip() or r.stderr.strip()[-80:]}


def _env_python(env: str) -> str:
    return os.path.join(os.path.realpath(env), 'bin', 'python')


def suite(a) -> int:
    variant = VARIANTS[a.variant]
    os.makedirs(a.scratch, exist_ok=True)
    run = tempfile.mkdtemp(prefix=f'run-{a.variant}-', dir=a.scratch)
    tree, policy = os.path.join(run, 'tree'), os.path.join(run, 'policy')
    os.makedirs(tree)
    os.makedirs(policy)
    shutil.copy(os.path.abspath(__file__), os.path.join(policy, 'check.py'))
    token = 'marker-' + secrets.token_hex(6)
    marker_file = os.path.join(run, token)
    with open(marker_file, 'w') as f:
        f.write(token)
    gpu_gid = os.stat(f'/dev/nvidia{a.card}').st_gid if a.card is not None else None
    # F5's file: readable by its owner and one of the account's groups other than the GPU group
    # (the primary group when the account has no other), by no one else
    others = sorted(set(os.getgroups()) - {gpu_gid, os.getgid()})
    group_file = os.path.join(run, token + '-group')
    with open(group_file, 'w') as f:
        f.write(token)
    os.chown(group_file, -1, others[0] if others else os.getgid())
    os.chmod(group_file, 0o640)
    try:  # a default ACL of the scratch directory would add entries the mode bits do not show
        os.removexattr(group_file, 'system.posix_acl_access')
    except OSError:
        pass
    lst, hits = _marker_listener()
    marker = subprocess.Popen([sys.executable, '-c', MARKER_CODE, token])
    gpu_marker = None
    if a.card is not None:  # a process of ours on the leased card, outside: does nvidia-smi inside list it?
        gpu_marker = subprocess.Popen([sys.executable, '-c', GPU_MARKER_CODE, token], stdout=subprocess.PIPE,
                                      text=True, env=dict(os.environ, CUDA_VISIBLE_DEVICES=_card_uuid(a.card)))
        if gpu_marker.stdout.readline().strip() != 'ready':
            raise SystemExit('check: the GPU marker did not start')
    home = os.path.expanduser('~')
    spec = {'token': token, 'marker_file': marker_file, 'group_file': group_file, 'marker_pid': marker.pid,
            'marker_port': lst.getsockname()[1], 'proxy_port': a.proxy_port, 'home': home, 'scratch': run,
            'card': a.card, 'env_prefix': os.path.realpath(a.env)}
    if variant is None:
        spec.update(tree=tree, policy=policy, env_python=_env_python(a.env),
                    relay_url=f'http://127.0.0.1:{a.proxy_port}' if a.proxy_port else None)
        env = dict(os.environ)
        if a.card is not None:
            env['CUDA_VISIBLE_DEVICES'] = _card_uuid(a.card)
        for k in ('https_proxy', 'http_proxy', 'HTTPS_PROXY', 'HTTP_PROXY'):
            env.pop(k, None)
        inner = [spec['env_python'], os.path.join(policy, 'check.py')]
        cmd_prefix = []
    else:
        spec.update(tree='/job', policy='/ci/policy', env_python='/opt/env/bin/python',
                    relay_url=INSIDE_RELAY if a.proxy_port else None)
        env = dict(os.environ)
        inner = ['/opt/env/bin/python', '/ci/policy/check.py']
        cmd_prefix = [sys.executable, SANDBOX, 'run', '--tree', tree, '--policy', policy, '--env', a.env,
                      '--map', variant[0], '--groups', variant[1]]
        if a.card is not None:
            cmd_prefix += ['--card', str(a.card)]
        if a.proxy_port:
            cmd_prefix += ['--relay', f'127.0.0.1:{a.proxy_port}']
        cmd_prefix.append('--')
    spec['ids_ready'] = os.path.join(spec['tree'], 'ids.ready')
    spec['ids_ack'] = os.path.join(spec['policy'], 'ids.ack')
    cmd = cmd_prefix + inner + ['inside', json.dumps(spec)]
    t0 = time.time()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    outer = {}

    def watch():
        ready = os.path.join(tree, 'ids.ready')
        deadline = time.time() + 120
        while time.time() < deadline and proc.poll() is None:
            if os.path.exists(ready):
                time.sleep(0.2)
                with open(ready) as f:
                    inner_pid = int(f.read().strip() or 0)
                outer['ids'] = _kernel_ids(proc.pid, inner_pid)
                if outer['ids']:
                    outer['reverse'] = _seize_from_outside(outer['ids']['pid_outside'])
                break
            time.sleep(0.1)
        open(os.path.join(policy, 'ids.ack'), 'w').close()
    w = threading.Thread(target=watch, daemon=True)
    w.start()
    out, err = proc.communicate(timeout=1800)
    wall = time.time() - t0
    w.join(5)
    for m in (marker, gpu_marker):
        if m:
            m.kill()
            m.wait()
    lst.close()
    lines = [ln for ln in out.splitlines() if ln.startswith('{"results"')]
    if lines:
        results = json.loads(lines[-1])['results']
    else:
        msg = (err.strip().splitlines() or [f'exit {proc.returncode}'])[-1]
        results = [{'id': 'ALL', 'expected': 'the sandbox starts', 'observed': f'did not start: {msg}', 'held': False}]
    ids = outer.get('ids')
    sb = _sandbox_module()
    pw = pwd.getpwuid(os.getuid())
    ranges = []
    for path in ('/etc/subuid', '/etc/subgid'):
        try:
            with open(path) as f:
                ranges.append(sb.subordinate_range(sb.subid_lines(f.read(), pw.pw_name, pw.pw_uid), path))
        except (OSError, sb.Cannot):
            ranges.append(None)
    expected = expected_groups(gpu_gid, variant[1] if variant else None)
    gst = os.stat(group_file)
    for r in results:
        if r['id'] == 'I1':
            inside_ids = r['observed']
            r['observed'] = {'inside': inside_ids, 'kernel': ids, 'gpu_group': gpu_gid}
            r['held'] = identity_held(ids, os.getuid(), *ranges, expected)
        if r['id'] == 'F5':
            r['observed'], r['held'] = f5_verdict(r['observed'], r['held'], ids, gst)
    if 'reverse' in outer:
        results.append({'id': 'R1', 'expected': EXPECTED['R1'], 'held': None,
                        'observed': 'attached' if outer['reverse'].get('r') == 'ok'
                        else f'failed ({outer["reverse"].get("r")})'})
    if hits:
        for r in results:
            if r['id'] == 'N1':
                r['observed'] += f' (listener saw {len(hits)} connection(s))'
    report = {'variant': a.variant, 'host': a.host_label, 'group_file_gid': gst.st_gid,
              'exit': proc.returncode, 'wall_s': round(wall, 1),
              'account_uid': os.getuid(), 'account_groups': os.getgroups(), 'results': results,
              'stderr_tail': err.strip().splitlines()[-5:]}
    shutil.rmtree(run, ignore_errors=True)
    if os.path.exists(run):
        report['cleanup'] = f'could not remove {run}'
    if a.out:
        with open(a.out, 'w') as f:
            json.dump(report, f, indent=1)
    print(table([report]))
    return 0


def verdict(r, variant) -> str:
    h = r['held']
    if r['id'] in INFO_ITEMS or h is None:
        return 'recorded'
    if r['id'] in FUNCTIONAL_ITEMS:
        return 'works' if h else 'FAILS'
    if variant == 'control':
        return 'not confined' if not h else 'confined (control!)'
    return 'held' if h else 'NOT HELD'


def table(reports) -> str:
    rows = ['| item | variant | expected | observed | verdict |', '|---|---|---|---|---|']
    for rep in reports:
        for r in rep['results']:
            obs = r['observed'] if isinstance(r['observed'], str) else json.dumps(r['observed'])
            rows.append(f'| {r["id"]} | {rep["variant"]} | {r["expected"]} | {obs.replace("|", "/")} | '
                        f'{verdict(r, rep["variant"])} |')
    return '\n'.join(rows)


def cost(a) -> int:
    py = _env_python(a.env)
    env = dict(os.environ, XLA_PYTHON_CLIENT_PREALLOCATE='false', TF_CPP_MIN_LOG_LEVEL='3',
               CUDA_VISIBLE_DEVICES=_card_uuid(a.card))
    run = tempfile.mkdtemp(prefix='cost-', dir=a.scratch)
    outside, inside_t = [], []
    for _ in range(a.repeat):
        t = time.time()
        subprocess.run([py, '-c', G1_CODE], env=env, check=True, capture_output=True)
        outside.append(time.time() - t)
        tree = tempfile.mkdtemp(prefix='tree-', dir=run)
        t = time.time()
        subprocess.run([sys.executable, SANDBOX, 'run', '--tree', tree, '--env', a.env, '--card', str(a.card),
                        '--groups', 'gpu',
                        '--setenv', 'XLA_PYTHON_CLIENT_PREALLOCATE=false', '--setenv', 'TF_CPP_MIN_LOG_LEVEL=3',
                        '--', '/opt/env/bin/python', '-c', G1_CODE], check=True, capture_output=True)
        inside_t.append(time.time() - t)
        t = time.time()
        subprocess.run([sys.executable, SANDBOX, 'run', '--tree', tree, '--', '/bin/true'], check=True)
        inside_t[-1] = (inside_t[-1], time.time() - t)
    shutil.rmtree(run, ignore_errors=True)
    rep = {'outside_s': [round(x, 2) for x in outside], 'inside_s': [round(x[0], 2) for x in inside_t],
           'empty_sandbox_s': [round(x[1], 3) for x in inside_t],
           'median_outside_s': round(statistics.median(outside), 2),
           'median_inside_s': round(statistics.median(x[0] for x in inside_t), 2),
           'median_empty_sandbox_s': round(statistics.median(x[1] for x in inside_t), 3)}
    print(json.dumps(rep))
    if a.out:
        with open(a.out, 'w') as f:
            json.dump(rep, f, indent=1)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog='check.py', description=__doc__.split('\n\n')[0])
    sub = p.add_subparsers(dest='verb', required=True)
    s = sub.add_parser('suite')
    s.add_argument('--variant', choices=sorted(VARIANTS), required=True)
    s.add_argument('--scratch', required=True)
    s.add_argument('--env', required=True, help='the Python environment (with jax) the job uses')
    s.add_argument('--card', type=int, help='the leased card: its device minor (the N of /dev/nvidiaN)')
    s.add_argument('--proxy-port', type=int, help="the host's forwarded proxy port on 127.0.0.1")
    s.add_argument('--host-label', default='this host')
    s.add_argument('--out')
    c = sub.add_parser('cost')
    c.add_argument('--scratch', required=True)
    c.add_argument('--env', required=True)
    c.add_argument('--card', type=int, required=True)
    c.add_argument('--repeat', type=int, default=3)
    c.add_argument('--out')
    i = sub.add_parser('inside')
    i.add_argument('spec')
    a = p.parse_args(argv)
    if a.verb == 'inside':
        return inside(json.loads(a.spec))
    return suite(a) if a.verb == 'suite' else cost(a)


if __name__ == '__main__':
    sys.exit(main())
