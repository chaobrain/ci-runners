#!/usr/bin/env python3
"""Run one command confined to a per-job sandbox, without root (prototype).

Experimental: wired into nothing. Standard library only, plus ``newuidmap`` /
``newgidmap`` (package ``uidmap``) and ``setpriv`` (util-linux). Linux, x86-64.

    sandbox.py run --tree DIR [--card N] [--relay HOST:PORT] [--policy DIR] [--env DIR]
                   [--map subuid|single] [--groups gpu|kept|none] [--setenv K=V]... -- CMD...
    sandbox.py probe [--card N]   # can this account build the sandbox here (exit 0 yes, 125 no),
                                  # and in which group modes?

Processes:

* L, the launcher, stays outside. It forks S, waits until S has a new user namespace,
  writes S's id maps (``newuidmap`` / ``newgidmap`` for ``--map subuid``, the account's own
  ids for ``--map single``; ``--groups gpu`` adds the GPU group's gid, which the account's
  ``/etc/subgid`` must list), and relays TCP connections from the sandbox's loopback
  (``127.0.0.1:3128`` inside) to ``--relay`` outside. The listening socket lives in the
  sandbox's network namespace, but only L holds it (passed over a socketpair).
* S, the sandbox's root, unshares mount, pid, ipc, network and uts namespaces, brings up
  loopback only, builds the new root on a tmpfs and forks
* I, pid 1 of the new pid namespace: mounts a fresh ``/proc``, pivots into the new root,
  sets every ``user.max_*_namespaces`` of the sandbox's user namespace to 0 (no nested
  namespace), hands the tree to the job's uid, forks the job and reaps; when the job ends
  it gives the tree back to the account and exits, which kills whatever the job left.
* J, the job: ``setpriv`` drops to uid/gid 1000 (``--map subuid``; under ``--map single``
  it stays the account's uid), sets its supplementary groups (``gpu``: the GPU group alone;
  ``kept``: those inherited; ``none``: none), empties the capability bounding, inheritable and ambient
  sets, sets no-new-privileges and execs the command.

Layout inside: ``/job`` the tree (read-write), ``/usr`` and the ``/lib*`` and ``/bin``
links read-only, a generated read-only ``/etc``, ``/opt/env`` the Python environment
read-only, ``/ci/policy`` the hook-and-policy stand-in read-only, fresh ``/proc``,
``/tmp``, ``/dev/shm`` and ``/dev`` (null, zero, full, random, urandom, tty-less; the leased
card's node, the control node and the UVM nodes). Nothing else of the host's file tree.
"""
from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import grp
import os
import pwd
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading

CLONE_NEWNS = 0x00020000
CLONE_NEWUTS = 0x04000000
CLONE_NEWIPC = 0x08000000
CLONE_NEWUSER = 0x10000000
CLONE_NEWPID = 0x20000000
CLONE_NEWNET = 0x40000000

MS_RDONLY = 1
MS_NOSUID = 2
MS_NODEV = 4
MS_NOEXEC = 8
MS_REMOUNT = 32
MS_NOATIME = 1024
MS_NODIRATIME = 2048
MS_BIND = 4096
MS_REC = 16384
MS_PRIVATE = 1 << 18
MS_RELATIME = 1 << 21
MNT_DETACH = 2
SYS_PIVOT_ROOT = 155  # x86-64
PR_SET_PDEATHSIG = 1

JOB_UID = 1000
JOB_GID = 1000
RELAY_PORT = 3128  # inside the sandbox's own loopback
NS_LIMITS = ('user', 'mnt', 'pid', 'net', 'ipc', 'uts', 'cgroup', 'time')
DEV_NODES = ('null', 'zero', 'full', 'random', 'urandom')
EXIT_CANNOT = 125

_libc = ctypes.CDLL(None, use_errno=True)
_libc.mount.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_ulong, ctypes.c_char_p]
_libc.umount2.argtypes = [ctypes.c_char_p, ctypes.c_int]
_libc.unshare.argtypes = [ctypes.c_int]
_libc.syscall.restype = ctypes.c_long


class Cannot(RuntimeError):
    """The sandbox cannot be built here; the message says why."""


def _check(ret: int, what: str) -> None:
    if ret != 0:
        e = ctypes.get_errno()
        raise OSError(e, f'{what}: {os.strerror(e)}')


def _b(s):
    return None if s is None else os.fsencode(s)


def mount(src, dst, fstype, flags, data=None) -> None:
    _check(_libc.mount(_b(src), _b(dst), _b(fstype), flags, _b(data)), f'mount {src} on {dst}')


def unshare(flags: int) -> None:
    _check(_libc.unshare(flags), 'unshare')


def pdeathsig(sig=signal.SIGKILL) -> None:
    _libc.prctl(PR_SET_PDEATHSIG, int(sig), 0, 0, 0)


# ----------------------------------------------------------------------------- mounts

def _mountpoints_under(path: str) -> list[str]:
    """Mount points at or under ``path`` in this mount namespace, outermost first."""
    out = []
    with open('/proc/self/mountinfo') as f:
        for line in f:
            mp = line.split()[4].encode().decode('unicode_escape')
            if mp == path or mp.startswith(path.rstrip('/') + '/'):
                out.append(mp)
    return out


def _locked_flags(path: str) -> int:
    """The flags a remount must keep (the kernel locks them in a user namespace)."""
    f = os.statvfs(path).f_flag
    out = 0
    for st, ms in ((os.ST_NOSUID, MS_NOSUID), (os.ST_NODEV, MS_NODEV), (os.ST_NOEXEC, MS_NOEXEC),
                   (os.ST_NOATIME, MS_NOATIME), (os.ST_NODIRATIME, MS_NODIRATIME),
                   (getattr(os, 'ST_RELATIME', 4096), MS_RELATIME)):
        if f & st:
            out |= ms
    return out


def bind(src: str, dst: str, *, ro: bool) -> None:
    """Bind ``src`` (recursively) on ``dst``; read-only binds remount every submount too."""
    if os.path.isdir(src):
        os.makedirs(dst, exist_ok=True)
    else:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if not os.path.exists(dst):
            open(dst, 'a').close()
    mount(src, dst, None, MS_BIND | MS_REC)
    flags = MS_BIND | MS_REMOUNT | MS_NOSUID | (MS_RDONLY if ro else 0)
    for mp in _mountpoints_under(dst) if ro else [dst]:
        try:
            mount(None, mp, None, flags | _locked_flags(mp))
        except OSError as e:
            if mp == dst:
                raise
            print(f'sandbox: could not remount submount {mp}: {e}', file=sys.stderr)


def _ifup_lo() -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        ifr = fcntl.ioctl(s, 0x8913, struct.pack('16sH22x', b'lo', 0))  # SIOCGIFFLAGS
        flags = struct.unpack('16sH22x', ifr)[1]
        fcntl.ioctl(s, 0x8914, struct.pack('16sH22x', b'lo', flags | 0x1 | 0x40))  # SIOCSIFFLAGS, UP|RUNNING
    finally:
        s.close()


def _etc(root: str, groups_line: str) -> None:
    etc = os.path.join(root, 'etc')
    os.makedirs(etc)
    mount('tmpfs', etc, 'tmpfs', MS_NOSUID | MS_NODEV | MS_NOEXEC, 'mode=0755,size=16m')
    files = {
        'passwd': f'root:x:0:0:root:/job/home:/bin/sh\njob:x:{JOB_UID}:{JOB_GID}:job:/job/home:/bin/sh\n'
                  'nobody:x:65534:65534:nobody:/nonexistent:/usr/sbin/nologin\n',
        'group': f'root:x:0:\njob:x:{JOB_GID}:\n{groups_line}nogroup:x:65534:\n',
        'hosts': '127.0.0.1 localhost sandbox\n::1 localhost\n',
        'hostname': 'sandbox\n',
        'nsswitch.conf': 'passwd: files\ngroup: files\nhosts: files\n',
        'resolv.conf': '',
    }
    for name, text in files.items():
        with open(os.path.join(etc, name), 'w') as f:
            f.write(text)
    for name in ('ld.so.cache', 'localtime'):
        src = os.path.join('/etc', name)
        if os.path.exists(src):
            shutil.copyfile(src, os.path.join(etc, name))
    for name in ('ssl', 'ca-certificates', 'alternatives'):
        src = os.path.join('/etc', name)
        if os.path.isdir(src):
            bind(src, os.path.join(etc, name), ro=True)
    mount(None, etc, None, MS_REMOUNT | MS_RDONLY | MS_NOSUID | MS_NODEV | MS_NOEXEC)


def _dev(root: str, card) -> list[str]:
    dev = os.path.join(root, 'dev')
    os.makedirs(dev)
    mount('tmpfs', dev, 'tmpfs', MS_NOSUID | MS_NOEXEC, 'mode=0755,size=64k')
    nodes = list(DEV_NODES)
    if card is not None:
        nodes += [f'nvidia{card}', 'nvidiactl', 'nvidia-uvm', 'nvidia-uvm-tools']
    present = []
    for n in nodes:
        src = os.path.join('/dev', n)
        if os.path.exists(src):
            bind(src, os.path.join(dev, n), ro=False)
            present.append(n)
        elif n.startswith('nvidia'):
            raise Cannot(f'{src} does not exist')
    for name, target in (('fd', '/proc/self/fd'), ('stdin', '/proc/self/fd/0'),
                         ('stdout', '/proc/self/fd/1'), ('stderr', '/proc/self/fd/2')):
        os.symlink(target, os.path.join(dev, name))
    os.makedirs(os.path.join(dev, 'shm'))
    mount('tmpfs', os.path.join(dev, 'shm'), 'tmpfs', MS_NOSUID | MS_NODEV, 'mode=1777,size=1g')
    mount(None, dev, None, MS_REMOUNT | MS_RDONLY | MS_NOSUID | MS_NOEXEC)
    return present


def build_root(root: str, a, groups_line: str) -> None:
    """The new root, on a tmpfs at ``root`` (in this, private, mount namespace)."""
    mount('tmpfs', root, 'tmpfs', MS_NOSUID | MS_NODEV, 'mode=0755,size=16m')
    bind('/usr', os.path.join(root, 'usr'), ro=True)
    for name in ('bin', 'sbin', 'lib', 'lib32', 'lib64', 'libx32'):
        src = os.path.join('/', name)
        if os.path.islink(src):
            os.symlink(os.readlink(src), os.path.join(root, name))
        elif os.path.isdir(src):
            bind(src, os.path.join(root, name), ro=True)
    _etc(root, groups_line)
    _dev(root, a.card)
    if a.env:
        bind(os.path.realpath(a.env), os.path.join(root, 'opt', 'env'), ro=True)
    if a.policy:
        bind(os.path.realpath(a.policy), os.path.join(root, 'ci', 'policy'), ro=True)
    bind(os.path.realpath(a.tree), os.path.join(root, 'job'), ro=False)
    os.makedirs(os.path.join(root, 'tmp'))
    mount('tmpfs', os.path.join(root, 'tmp'), 'tmpfs', MS_NOSUID | MS_NODEV, f'mode=1777,size={a.tmp_size}')
    os.makedirs(os.path.join(root, 'proc'))
    os.makedirs(os.path.join(root, 'sys'))
    # a fresh sysfs (this network namespace's view), read-only: the NVIDIA user-space driver
    # reads /sys/module/nvidia/initstate and runs nvidia-modprobe when it is missing
    mount('sysfs', os.path.join(root, 'sys'), 'sysfs', MS_RDONLY | MS_NOSUID | MS_NODEV | MS_NOEXEC)
    os.makedirs(os.path.join(root, 'run'))
    mount('tmpfs', os.path.join(root, 'run'), 'tmpfs', MS_NOSUID | MS_NODEV | MS_NOEXEC, 'mode=0755,size=1m')


def _chown_tree(path: str, uid: int, gid: int) -> None:
    os.lchown(path, uid, gid)
    for d, dirs, files in os.walk(path):
        for n in dirs + files:
            try:
                os.lchown(os.path.join(d, n), uid, gid)
            except OSError as e:
                print(f'sandbox: chown {n}: {e}', file=sys.stderr)


# ----------------------------------------------------------------------------- id maps

def subid_lines(text: str, name: str, uid: int) -> list[tuple[int, int]]:
    """The account's ``(first id, count)`` lines of a subuid or subgid file, in file order
    (by name or by uid; an account may have several)."""
    out = []
    for line in text.splitlines():
        parts = line.strip().split(':')
        if len(parts) == 3 and parts[0] in (name, str(uid)):
            try:
                first, count = int(parts[1]), int(parts[2])
            except ValueError:
                continue
            if count > 0:
                out.append((first, count))
    return out


def subordinate_range(lines: list[tuple[int, int]], what: str) -> tuple[int, int]:
    """The first line large enough for the job's ids (the subordinate pair's range)."""
    for first, count in lines:
        if count > JOB_UID:
            return first, count
    raise Cannot(f'no subordinate range of more than {JOB_UID} ids for the account in {what}')


def gpu_group(card: int, node=None, ctl=None) -> int:
    """The GPU group: the group the leased card's node opens to (``os.stat`` results of the
    card's node and the control node; read from ``/dev`` when not given)."""
    node = node if node is not None else os.stat(f'/dev/nvidia{card}')
    ctl = ctl if ctl is not None else os.stat('/dev/nvidiactl')
    if node.st_mode & 0o006 == 0o006:
        raise Cannot(f'/dev/nvidia{card} is open to everyone: the job needs no group (use --groups none)')
    if node.st_mode & 0o060 != 0o060:
        raise Cannot(f'/dev/nvidia{card} is not open to its group: no group gives the job the card')
    if ctl.st_gid != node.st_gid and ctl.st_mode & 0o006 != 0o006:
        raise Cannot(f'/dev/nvidiactl opens to another group than /dev/nvidia{card}: one group cannot give the card')
    return node.st_gid


def id_plan(mapping: str, groups: str, name: str, uid: int, gid: int, subuid: str, subgid: str,
            gpu_gid=None) -> dict:
    """The id maps and the job's groups, from the account and its ``/etc/subuid`` and
    ``/etc/subgid`` texts.

    ``subuid``: the account's uid and gid at 0, the subordinate range at 1..n (n at most
    65536); the job runs as 1000. Its groups: ``gpu`` maps the GPU group's gid (a
    single-gid line of the account in ``/etc/subgid``) at n + 1, outside every other
    inside id, and sets the job's groups to it alone; ``kept`` leaves the groups the
    launcher inherited; ``none`` clears them. ``single``: the account's own ids, groups
    kept (``setgroups`` is denied without a privileged map).
    """
    if mapping == 'single':
        if groups != 'kept':
            raise Cannot('--map single keeps every group of the account (setgroups is denied without '
                         'newgidmap): only --groups kept')
        return {'uid_map': ['0', str(uid), '1'], 'gid_map': ['0', str(gid), '1'], 'setgroups': 'deny',
                'job_groups': None, 'gpu_inside': None}
    su, sn = subordinate_range(subid_lines(subuid, name, uid), '/etc/subuid')
    glines = subid_lines(subgid, name, uid)
    sg, gn = subordinate_range(glines, '/etc/subgid')
    n = min(sn, gn, 65536)
    um = ['0', str(uid), '1', '1', str(su), str(n)]
    gm = ['0', str(gid), '1', '1', str(sg), str(n)]
    plan = {'uid_map': um, 'gid_map': gm, 'setgroups': 'allow', 'job_groups': [] if groups == 'none' else None,
            'gpu_inside': None, 'job_kernel_uid': su + JOB_UID - 1, 'job_kernel_gid': sg + JOB_GID - 1}
    if groups != 'gpu':
        return plan
    if gpu_gid is None:
        raise Cannot('--groups gpu needs --card (the GPU group is the group of the card node)')
    if gpu_gid == gid:
        raise Cannot(f'the GPU group (gid {gpu_gid}) is the account\'s primary group: mode gpu cannot '
                     'tell it from the account')
    if sg <= gpu_gid < sg + n:
        raise Cannot(f'the GPU group (gid {gpu_gid}) lies in the subordinate range {sg}:{n}')
    if not any(first <= gpu_gid < first + count for first, count in glines):
        raise Cannot(f'mode gpu needs the GPU group (gid {gpu_gid}, the group of the card node) among the '
                     f'account\'s lines in /etc/subgid; its administrator adds the line "{name}:{gpu_gid}:1"')
    inside = n + 1
    gm += [str(inside), str(gpu_gid), '1']
    plan.update(job_groups=[inside], gpu_inside=inside, gpu_gid=gpu_gid)
    return plan


def make_plan(a) -> dict:
    """``id_plan`` for this account, its files and, in mode gpu, the leased card's group."""
    pw = pwd.getpwuid(os.getuid())
    texts = []
    for path in ('/etc/subuid', '/etc/subgid'):
        try:
            with open(path) as f:
                texts.append(f.read())
        except OSError:
            texts.append('')
    if a.map == 'subuid' and os.getgid() != pw.pw_gid:
        raise Cannot('newgidmap maps only the primary group: run the launcher with its primary gid')
    gg = gpu_group(a.card) if a.map == 'subuid' and a.groups == 'gpu' and a.card is not None else None
    return id_plan(a.map, a.groups, pw.pw_name, os.getuid(), os.getgid(), *texts, gpu_gid=gg)


def write_maps(pid: int, plan: dict) -> dict:
    """Write the child's uid and gid maps as ``plan`` says; return what was mapped."""
    um, gm = plan['uid_map'], plan['gid_map']
    if plan['setgroups'] == 'deny':
        try:
            with open(f'/proc/{pid}/uid_map', 'w') as f:
                f.write(' '.join(um) + '\n')
            with open(f'/proc/{pid}/setgroups', 'w') as f:
                f.write('deny\n')
            with open(f'/proc/{pid}/gid_map', 'w') as f:
                f.write(' '.join(gm) + '\n')
        except OSError as e:
            raise Cannot(f'writing the id map failed ({e}){_why_userns()}')
        return {'uid_map': ' '.join(um), 'gid_map': ' '.join(gm), 'setgroups': 'deny'}
    for tool in ('newuidmap', 'newgidmap'):
        if not shutil.which(tool):
            raise Cannot(f'{tool} is not installed (package uidmap){_why_userns()}')
    for tool, args in (('newuidmap', um), ('newgidmap', gm)):
        r = subprocess.run([tool, str(pid)] + args, capture_output=True, text=True)
        if r.returncode:
            raise Cannot(f'{tool} failed: {r.stderr.strip()}{_why_userns()}')
    return {'uid_map': ' '.join(um), 'gid_map': ' '.join(gm), 'setgroups': 'allow',
            'job_kernel_uid': plan['job_kernel_uid'], 'job_kernel_gid': plan['job_kernel_gid'],
            'job_groups': plan['job_groups']}


def _why_userns() -> str:
    try:
        with open('/proc/sys/kernel/apparmor_restrict_unprivileged_userns') as f:
            if f.read().strip() == '1':
                return ('; AppArmor restricts unprivileged user namespaces on this host '
                        '(kernel.apparmor_restrict_unprivileged_userns=1): a namespace gets no '
                        'capabilities unless root installs a profile for the launcher')
    except OSError:
        pass
    return ''


# ----------------------------------------------------------------------------- relay

def _pipe(a: socket.socket, b: socket.socket) -> None:
    try:
        while True:
            data = a.recv(65536)
            if not data:
                break
            b.sendall(data)
    except OSError:
        pass
    finally:
        for s, how in ((b, socket.SHUT_WR), (a, socket.SHUT_RD)):
            try:
                s.shutdown(how)
            except OSError:
                pass


def _relay_conn(c: socket.socket, target: tuple[str, int]) -> None:
    try:
        u = socket.create_connection(target, timeout=15)
        u.settimeout(None)
    except OSError:
        c.close()
        return
    t = threading.Thread(target=_pipe, args=(u, c), daemon=True)
    t.start()
    _pipe(c, u)
    t.join()
    c.close()
    u.close()


def relay(listener: socket.socket, target: tuple[str, int]) -> None:
    """Forward each connection on ``listener`` (inside) to ``target`` (outside)."""
    while True:
        try:
            c, _ = listener.accept()
        except OSError:
            return
        threading.Thread(target=_relay_conn, args=(c, target), daemon=True).start()


# ----------------------------------------------------------------------------- run

def _job_env(a) -> dict:
    path = '/usr/local/bin:/usr/bin:/bin'
    if a.env:
        path = '/opt/env/bin:' + path
    env = {'PATH': path, 'HOME': '/job/home', 'TMPDIR': '/tmp', 'LANG': 'C.UTF-8',
           'SSL_CERT_FILE': '/etc/ssl/certs/ca-certificates.crt', 'PYTHONNOUSERSITE': '1'}
    if a.relay:
        proxy = f'http://127.0.0.1:{RELAY_PORT}'
        env.update(https_proxy=proxy, http_proxy=proxy, HTTPS_PROXY=proxy, HTTP_PROXY=proxy,
                   no_proxy='localhost,127.0.0.1', NO_PROXY='localhost,127.0.0.1')
    for kv in a.setenv:
        k, _, v = kv.partition('=')
        env[k] = v
    return env


def _setpriv(a) -> list[str]:
    cmd = ['/usr/bin/setpriv', '--no-new-privs', '--bounding-set=-all', '--inh-caps=-all']
    if _ambient_supported():
        cmd.append('--ambient-caps=-all')
    if a.map == 'subuid':
        cmd += [f'--reuid={JOB_UID}', f'--regid={JOB_GID}']
    return cmd + _groups_arg(a.plan['job_groups']) + ['--']


def _groups_arg(job_groups) -> list[str]:
    """setpriv's groups argument: ``None`` keeps the inherited groups (no ``setgroups``),
    ``[]`` clears them, a list sets exactly those (inside ids)."""
    if job_groups is None:
        return ['--keep-groups']
    if not job_groups:
        return ['--clear-groups']
    return ['--groups=' + ','.join(str(g) for g in job_groups)]


def _ambient_supported() -> bool:
    try:
        r = subprocess.run(['/usr/bin/setpriv', '--help'], capture_output=True, text=True)
        return '--ambient-caps' in r.stdout
    except OSError:
        return False


def _mask_nvidia_proc(root: str, card: int) -> None:
    """What libnvidia-container does for a container: the driver's parameters say
    ``ModifyDeviceFiles: 0`` (else the user-space driver, seeing device nodes it cannot
    check or fix inside a user namespace, refuses: CUDA error 304, NVML "GPU access
    blocked by the operating system"), and ``/proc/driver/nvidia/gpus`` lists only the
    leased card."""
    nv = os.path.join(root, 'proc', 'driver', 'nvidia')
    run = os.path.join(root, 'run')
    with open(os.path.join(nv, 'params')) as f:
        params = f.read()
    patched = os.path.join(run, 'nvidia-params')
    with open(patched, 'w') as f:
        f.write(params.replace('ModifyDeviceFiles: 1', 'ModifyDeviceFiles: 0'))
    bind(patched, os.path.join(nv, 'params'), ro=True)
    gpus = os.path.join(nv, 'gpus')
    keep = None
    for bus in sorted(os.listdir(gpus)):
        try:
            with open(os.path.join(gpus, bus, 'information')) as f:
                if any(ln.split(':', 1)[0].strip() == 'Device Minor' and ln.split(':', 1)[1].strip() == str(card)
                       for ln in f):
                    keep = bus
        except OSError:
            pass
    stash = os.path.join(run, 'gpu')
    if keep:
        os.makedirs(stash)
        mount(os.path.join(gpus, keep), stash, None, MS_BIND)
    mount('tmpfs', gpus, 'tmpfs', MS_NOSUID | MS_NODEV | MS_NOEXEC, 'mode=0555,size=16k')
    if keep:
        os.makedirs(os.path.join(gpus, keep))
        mount(stash, os.path.join(gpus, keep), None, MS_BIND | MS_REC)
        _check(_libc.umount2(os.fsencode(stash), MNT_DETACH), 'umount stash')
        os.rmdir(stash)
    mount(None, gpus, None, MS_REMOUNT | MS_RDONLY | MS_NOSUID | MS_NODEV | MS_NOEXEC)
    mount(None, run, None, MS_REMOUNT | MS_RDONLY | MS_NOSUID | MS_NODEV | MS_NOEXEC)


def _init(a, root: str) -> int:
    """pid 1 of the sandbox: finish the root, run the job, reap, give the tree back."""
    pdeathsig()
    mount('proc', os.path.join(root, 'proc'), 'proc', MS_NOSUID | MS_NODEV | MS_NOEXEC)
    if a.card is not None:
        _mask_nvidia_proc(root, a.card)
    os.chdir(root)
    _check(_libc.syscall(SYS_PIVOT_ROOT, b'.', b'.'), 'pivot_root')
    _check(_libc.umount2(b'.', MNT_DETACH), 'umount old root')
    os.chdir('/')
    for ns in NS_LIMITS:
        try:
            with open(f'/proc/sys/user/max_{ns}_namespaces', 'w') as f:
                f.write('0\n')
        except OSError as e:
            if ns == 'user':
                raise Cannot(f'cannot block nested user namespaces: {e}')
    mount('/proc/sys', '/proc/sys', None, MS_BIND)
    mount(None, '/proc/sys', None, MS_BIND | MS_REMOUNT | MS_RDONLY | MS_NOSUID | MS_NODEV | MS_NOEXEC)
    mount(None, '/', None, MS_REMOUNT | MS_RDONLY | MS_NOSUID | MS_NODEV)
    owner = (JOB_UID, JOB_GID) if a.map == 'subuid' else (0, 0)
    os.makedirs('/job/home', exist_ok=True)
    _chown_tree('/job', *owner)
    cmd = _setpriv(a) + a.cmd
    env = _job_env(a)
    sys.stdout.flush()
    sys.stderr.flush()
    job = os.fork()
    if job == 0:
        try:
            os.chdir('/job')
            os.execve(cmd[0], cmd, env)
        except OSError as e:
            print(f'sandbox: exec failed: {e}', file=sys.stderr)
        os._exit(127)
    signal.signal(signal.SIGTERM, lambda *_: os.kill(job, signal.SIGTERM))
    status = 1
    while True:
        try:
            pid, st = os.wait()
        except ChildProcessError:
            break
        except InterruptedError:
            continue
        if pid == job:
            status = os.waitstatus_to_exitcode(st)
            break
    if a.map == 'subuid':
        _chown_tree('/job', 0, 0)  # back to the account outside
    return status if status >= 0 else 128 - status


def _sandbox_root(a, sync: socket.socket, root: str) -> int:
    """S: the user namespace's first process."""
    pdeathsig()
    unshare(CLONE_NEWUSER)
    sync.send(b'U')
    if sync.recv(1) != b'M':
        return EXIT_CANNOT
    os.setresgid(0, 0, 0)
    os.setresuid(0, 0, 0)
    unshare(CLONE_NEWNS | CLONE_NEWPID | CLONE_NEWIPC | CLONE_NEWNET | CLONE_NEWUTS)
    socket.sethostname('sandbox')
    mount(None, '/', None, MS_REC | MS_PRIVATE)
    _ifup_lo()
    lst = None
    if a.relay:
        lst = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        lst.bind(('127.0.0.1', RELAY_PORT))
        lst.listen(64)
    socket.send_fds(sync, [b'L'], [lst.fileno()] if lst else [])
    if lst:
        lst.close()
    inside = a.plan['gpu_inside']
    groups_line = f'gpu:x:{inside}:job\n' if inside is not None else ''
    build_root(root, a, groups_line)
    sys.stdout.flush()
    sys.stderr.flush()
    init = os.fork()
    if init == 0:
        try:
            os._exit(_init(a, root))
        except Cannot as e:
            print(f'sandbox: {e}', file=sys.stderr)
            os._exit(EXIT_CANNOT)
        except BaseException as e:  # noqa: BLE001 -- pid 1 must not unwind into S's code
            print(f'sandbox: init failed: {e!r}', file=sys.stderr)
            os._exit(EXIT_CANNOT)
    sync.close()
    while True:
        try:
            _, st = os.waitpid(init, 0)
        except InterruptedError:
            continue
        code = os.waitstatus_to_exitcode(st)
        return code if code >= 0 else 128 - code


def run(a) -> int:
    if not a.cmd:
        raise SystemExit('sandbox: no command (put it after --)')
    if os.geteuid() == 0:
        raise SystemExit('sandbox: refuses to run as root')
    os.makedirs(a.tree, exist_ok=True)
    os.chmod(a.tree, 0o755)
    target = None
    if a.relay:
        host, _, port = a.relay.rpartition(':')
        target = (host, int(port))
    try:
        a.plan = make_plan(a)
    except Cannot as e:
        print(f'sandbox: {e}', file=sys.stderr)
        return EXIT_CANNOT
    root = tempfile.mkdtemp(prefix='.sandbox-root-', dir=os.path.dirname(os.path.realpath(a.tree)))
    mine, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()
    if pid == 0:
        mine.close()
        code = EXIT_CANNOT
        try:
            code = _sandbox_root(a, theirs, root)
        except Cannot as e:
            print(f'sandbox: {e}', file=sys.stderr)
        except OSError as e:
            print(f'sandbox: {e}{_why_userns()}', file=sys.stderr)
        os._exit(code)
    theirs.close()
    try:
        if mine.recv(1) != b'U':
            raise Cannot('the sandbox did not start')
        maps = write_maps(pid, a.plan)
        if a.verbose:
            print(f'sandbox: maps {maps}', file=sys.stderr)
        mine.send(b'M')
        msg, fds, _, _ = socket.recv_fds(mine, 1, 1)
        if msg == b'L' and fds:
            lst = socket.socket(fileno=fds[0])
            threading.Thread(target=relay, args=(lst, target), daemon=True).start()
    except Cannot as e:
        print(f'sandbox: {e}', file=sys.stderr)
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        _rmdir(root)
        return EXIT_CANNOT
    except OSError:
        pass  # S failed and said why
    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(s, lambda n, _f: os.kill(pid, signal.SIGKILL))
    while True:
        try:
            _, st = os.waitpid(pid, 0)
            break
        except InterruptedError:
            continue
    _rmdir(root)
    code = os.waitstatus_to_exitcode(st)
    return code if code >= 0 else 128 - code


def _rmdir(path: str) -> None:
    try:
        os.rmdir(path)
    except OSError:
        pass


def _cards() -> list[int]:
    """The device minors of the host's card nodes, lowest first."""
    out = []
    for n in os.listdir('/dev'):
        if n.startswith('nvidia') and n[6:].isdigit():
            out.append(int(n[6:]))
    return sorted(out)


def probe(card=None) -> int:
    """Exit 0 when this account can build the sandbox here, else 125 and why; then say which
    group modes it can build (``card``: the card node whose group is the GPU group; the
    lowest minor when not given; the card is not opened)."""
    def attempt(**kw) -> int:
        with tempfile.TemporaryDirectory(prefix='sandbox-probe-') as d:
            ns = argparse.Namespace(tree=os.path.join(d, 'tree'), card=None, relay=None, policy=None, env=None,
                                    map='subuid', groups='none', setenv=[], tmp_size='16m', verbose=False,
                                    cmd=['/bin/true'])
            vars(ns).update(kw)
            return run(ns)
    if attempt() != 0:
        print('sandbox: not usable here')
        return EXIT_CANNOT
    print('sandbox: usable')
    print('sandbox: --groups none: usable (the job holds no group)')
    cards = _cards()
    if card is None and cards:
        card = cards[0]
    if card is None:
        print('sandbox: --groups gpu, kept: no card node here')
        return 0
    try:
        gg = gpu_group(card)
    except Cannot as e:
        print(f'sandbox: --groups gpu, kept: not usable ({e})')
        return 0
    others = sorted(set(os.getgroups()) - {gg, os.getgid()})
    if gg in os.getgroups() or gg == os.getgid():
        print(f'sandbox: --groups kept: usable (the job keeps every group of the account: the GPU group '
              f'and {len(others)} other(s))')
    else:
        print('sandbox: --groups kept: no card (the account is not in the GPU group)')
    if attempt(card=card, groups='gpu') == 0:
        print('sandbox: --groups gpu: usable (the job holds the GPU group alone)')
    else:
        print('sandbox: --groups gpu: not usable (the reason is above)')
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog='sandbox.py', description=__doc__.split('\n\n')[0])
    sub = p.add_subparsers(dest='verb', required=True)
    r = sub.add_parser('run', help='run CMD in a sandbox')
    r.add_argument('--tree', required=True, help='the per-job tree, read-write at /job')
    r.add_argument('--card', type=int, help='the leased card\'s device minor: /dev/nvidia<N> is the only card node')
    r.add_argument('--relay', help='HOST:PORT outside that 127.0.0.1:3128 inside forwards to (the proxy)')
    r.add_argument('--policy', help='a directory mounted read-only at /ci/policy (hook and policy)')
    r.add_argument('--env', help='a Python environment mounted read-only at /opt/env')
    r.add_argument('--map', choices=('subuid', 'single'), default='subuid',
                   help='subuid: the job runs as a subordinate uid; single: as the account itself')
    r.add_argument('--groups', choices=('gpu', 'kept', 'none'), default=None,
                   help='gpu: the GPU group alone (needs its line in /etc/subgid; default with --card); '
                        'kept: every group of the account (default with --map single); '
                        'none: no group (default without --card)')
    r.add_argument('--setenv', action='append', default=[], metavar='K=V')
    r.add_argument('--tmp-size', default='4g')
    r.add_argument('-v', '--verbose', action='store_true')
    r.add_argument('cmd', nargs=argparse.REMAINDER)
    pr = sub.add_parser('probe', help='can this account build the sandbox here, and in which group modes?')
    pr.add_argument('--card', type=int, help='the card node whose group is the GPU group (not opened)')
    a = p.parse_args(argv)
    if a.verb == 'probe':
        return probe(a.card)
    if a.cmd and a.cmd[0] == '--':
        a.cmd = a.cmd[1:]
    if a.groups is None:
        a.groups = 'kept' if a.map == 'single' else 'gpu' if a.card is not None else 'none'
    if a.map == 'single' and a.groups != 'kept':
        p.error('--map single keeps every group of the account (setgroups is denied without a subgid map): '
                'only --groups kept')
    if a.groups == 'gpu' and a.card is None:
        p.error('--groups gpu needs --card (the GPU group is the group of the card node)')
    return run(a)


if __name__ == '__main__':
    sys.exit(main())
