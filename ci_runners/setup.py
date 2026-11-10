"""Host setup as the runner account (design §5.2, §5.3): ``plan`` prints every change,
``apply`` makes exactly an approved plan. Neither needs sudo.

``plan`` reads the inventory (``etc/inventory.toml``), the project registry
(``etc/projects/*.toml``: ``name`` and ``memory_max``) and the installed release
(``releases/hook``: hook scripts and unit templates), and lists:

- directories (§3.2), with ``etc/`` mode 700;
- the hook scripts, the rendered units and slices, each with its sha256 and a diff
  against the file in place;
- with a ``tunnel`` table (h100, decision 11.10), ``ci-tunnel``, an ssh forward to the
  egress host's proxy, and each runner's ``.env`` points at the forward; the tunnel's key,
  ``ssh_config`` and ``known_hosts`` in ``etc/tunnel/`` are the lead's, checked but never
  written;
- the runner app, checked by sha256, extracted once per runner directory;
- each runner's ``.env``;
- the ``systemctl --user`` commands;
- with ``push_key`` (a 3090 host, decision 12.4), the push key's line in the account's
  ``~/.ssh/authorized_keys``: ``restrict``, forced to ``org-sync --receive``; other lines
  are kept;
- with ``local_accounts`` (a 3090 host, decision 12.9), the root steps that share the host
  lock and slot files with the local accounts: a ``tmpfiles.d`` file that makes them
  root-owned with ACL entries for every account, and its state. Setup does not run them
  (it has no root): the lead does, before ``apply``, which refuses while one is not done.

Units are rendered with the inventory's ``data`` (the root of §3.2: our subtree on h100)
and ``python``. A ``ci-*`` unit file that exists but that no
earlier ``apply`` wrote (``state/setup.json``) stops the plan: another user of a shared
account may own it. ``plan --adopt`` instead lists each such file as ``adopt`` (a regular
file of this account only) and plans it as usual, for a host whose units an older release
wrote without a record; the lead approves the adoption with the plan, and ``apply`` must
be given ``--adopt`` too. A unit an earlier apply recorded that the plan no longer holds
(``ci-bridge.service``, decision 11.10) is listed as ``remove``: ``apply`` disables and
stops it, deletes the file (a regular file of this account only, never a link) and
drops it from the record.

Registration is not in the plan: the lead runs ``setup/register.sh`` with a fresh
token. ``apply --plan FILE`` refuses unless a fresh plan is identical (by sha256) and
records what it made in ``state/setup.json``; a second run is a no-op.
"""
from __future__ import annotations

import difflib
import glob
import hashlib
import json
import os
import pwd
import re
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import time
import tomllib
import urllib.request

from ci_runners import hookcopy, inventory, locks, slots

RUNNER_VERSION = '2.338.2'
RUNNER_SHA256 = 'baebf204307bac6193480852732f0aba2ae97b6c5597c244f4d5558390147c40'
PORT_WAIT = 60  # s: the egress forward's start, before the runner app is fetched through it
RUNNER_URL = (f'https://github.com/actions/runner/releases/download/v{RUNNER_VERSION}/'
              f'actions-runner-linux-x64-{RUNNER_VERSION}.tar.gz')
UNITS = ('ci-runner@.service', 'ci-tunnel.service', 'ci-reaper.service', 'ci-reaper.timer', 'ci-orgsync.service',
         'ci-orgsync.timer', 'ci-envrefresh.service', 'ci-envrefresh.timer', 'ci-hostload.service',
         'ci-hostload.timer', 'ci-hooks.service', 'ci.slice')
RUNNER_FILES = ('config.sh', 'run.sh', os.path.join('bin', 'Runner.Listener'))
# what goes into a unit file: one word each, so nothing can add a line
PROJECT_NAME = re.compile(r'[a-z0-9][a-z0-9-]{0,39}')
PATH = re.compile(r'/[A-Za-z0-9._/-]+')
MEMORY = re.compile(r'[1-9][0-9]*[KMGT]?|[1-9][0-9]?%|infinity')
#: env refresh: decision 12.11; the load record: decision 13.2
TIMERS = ('ci-reaper.timer', 'ci-orgsync.timer', 'ci-envrefresh.timer', 'ci-hostload.timer')
SYSTEM_PYTHON = '/usr/bin/python3'
#: the runner unit's order after the ssh forward to the egress (h100, decision 11.10); the
#: template's key is still PROXY_ORDER, so that the rendered unit stays as it was
TUNNEL_ORDER = 'After=ci-tunnel.service\nWants=ci-tunnel.service'
#: the lead's files in etc/tunnel/ (design §5.3): the key first, which no one else may read
TUNNEL_FILES = ('id_ed25519', 'ssh_config', 'known_hosts')
#: the root step's file (decision 12.9, §5.3): the shared lock and slot files, recreated at boot
TMPFILES = '/etc/tmpfiles.d/ci-runners-shared.conf'
#: the runner unit's order after the hooks' copies (decision 13.5)
HOOKS_ORDER = 'Requires=ci-hooks.service\nAfter=ci-hooks.service'
#: the root step's drop-in (decision 12.16, §5.3): the CI account's user manager in its own mount namespace
DROPIN = '/etc/systemd/system/user@{uid}.service.d/ci-runners-private.conf'
#: the comment that marks the push key's line in authorized_keys (decision 12.4)
PUSH_MARK = 'ci-runners-orgsync-push'
#: the egress account's line for a tunnel key (decision 11.10; operations §6)
EGRESS_LINE = 'restrict,port-forwarding,permitopen="127.0.0.1:{port}",permitlisten="127.0.0.1:9",command="/usr/sbin/nologin" {key}'
#: etc/tunnel/ssh_config: ``Host egress`` and only where it is (the unit fixes the rest)
SSH_CONFIG_LINE = re.compile(r'\s*(?:#.*)?|\s*(?:HostName|User|Port)[ \t=]+[A-Za-z0-9._-]+\s*|Host egress\s*')


def _runners(inv: inventory.Inventory) -> list[tuple[str, list[str], str, int | None]]:
    out = []
    for pool, v in inv.host['pools'].items():
        spec = v if isinstance(v, dict) else {'count': v}
        cls = inventory.pool_class(inv.host, pool)  # the pool's key unless it names its class (12.5)
        labels = inventory.pool_labels(inv, cls) or list(dict.fromkeys([pool, inv.this]))
        if spec.get('per_card'):
            for card in inv.host['gpus']:
                out.append((f'{inv.this}-gpu{card}', [*labels, f'{inv.this}-gpu{card}'], cls, card))
        else:
            for i in range(1, spec['count'] + 1):
                out.append((f'{inv.this}-{pool}-{i}', labels, cls, None))
    names = [n for n, _, _, _ in out]
    if len(set(names)) != len(names):
        raise ValueError('two runners of this host have the same name')
    return out


def timers(h: dict) -> list[str]:
    """The timers this host enables: the org snapshot only where the inventory sets
    ``orgsync`` (design §6.5)."""
    return [t for t in TIMERS if h.get('orgsync') or 'orgsync' not in t]


def runners(inv: inventory.Inventory) -> list[tuple[str, list[str], str]]:
    """``(name, labels, class)`` of this host's runners (design §5.1): ``<role>-<pool>-<i>``,
    labelled with the pool and the host role, or with the pool's own ``labels``; a
    ``per_card`` pool has one runner per card, ``<role>-gpu<N>``, labelled also
    ``<role>-gpu<N>``. The class is the pool's ``class``, else its key (decision 12.5)."""
    return [(n, labels, cls) for n, labels, cls, _ in _runners(inv)]


def python_of(inv: inventory.Inventory) -> str:
    """The host layer's interpreter: the inventory's ``python``, else the system's."""
    return str(inv.host.get('python') or SYSTEM_PYTHON)


HOOK_PYTHON = 'py=/usr/bin/python3  # @CI_PYTHON@\n'
#: the hook's release line (decision 13.5): its own path's grandparent until setup names the base
HOOK_BASE = 'base=$(/usr/bin/dirname "$(/usr/bin/dirname "$(/usr/bin/readlink -f "$0")")")  # @CI_BASE@\n'


def hooks_check() -> str:
    """The runner unit's check that both hook copies are executable files (decision
    13.5): a runner does not start without them. Pure."""
    return '\n'.join(f'ExecStartPre=/usr/bin/test -f {hookcopy.DIR}/{n} -a -x {hookcopy.DIR}/{n}' for n in hookcopy.NAMES)


def hook_text(src: str, python: str, base: str) -> str:
    """A hook as installed: its interpreter line names ``python`` and its release line
    ``base`` (``<data>/ci-runners``), both validated by the caller as one absolute path, so
    that a copy of it elsewhere runs the same release (decision 13.5). Raises ValueError if
    the hook lacks either line. Pure."""
    if src.count(HOOK_PYTHON) != 1:
        raise ValueError('a hook has no interpreter line')
    if src.count(HOOK_BASE) != 1:
        raise ValueError('a hook has no release line')
    return src.replace(HOOK_PYTHON, f'py={python}\n').replace(HOOK_BASE, f'base={base}\n')


def check_python(path: str, run=subprocess.run) -> None:
    """Refuse an interpreter that is not an executable Python 3.11 or later (``tomllib``)."""
    if not os.path.isfile(path) or not os.access(path, os.X_OK):
        raise ValueError(f'python {path} is not an executable file')
    try:
        r = run([path, '-I', '-c', 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'],
                capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        raise ValueError(f'python {path} did not answer: {type(e).__name__}') from None
    if r.returncode != 0:
        raise ValueError(f'python {path} is not Python 3.11 or later')


def render(template: str, subs: dict[str, str]) -> str:
    for k, v in subs.items():
        if not v:  # an empty value takes its line with it
            template = template.replace(f'@{k}@\n', '')
        template = template.replace(f'@{k}@', v)
    if '@' in ''.join(line for line in template.splitlines() if not line.lstrip().startswith('#')):
        raise ValueError('a unit template has an unknown @KEY@')
    return template


def check_tunnel(etc: str) -> None:
    """Refuse unless ``etc/tunnel/`` holds the lead's three files, as regular files of
    this account, the key and the directory readable by no one else, and ``ssh_config``
    says only ``Host egress`` with its ``HostName`` and, if it likes, ``User`` and
    ``Port``: the unit fixes every other option, and an option that accumulates or adds a
    forward or a command could not be fixed there."""
    d = os.path.join(etc, 'tunnel')
    try:
        st = os.lstat(d)
    except OSError:
        st = None
    if st is None or not stat.S_ISDIR(st.st_mode) or st.st_mode & 0o077 or st.st_uid != os.getuid():
        raise ValueError(f'{d} must be a directory of this account, mode 700, holding {", ".join(TUNNEL_FILES)}')
    for name in TUNNEL_FILES:
        path = os.path.join(d, name)
        try:
            st = os.lstat(path)
        except OSError:
            st = None
        if st is None or not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            raise ValueError(f'{path} must be a regular file of this account (the lead places it)')
        if name == 'id_ed25519' and st.st_mode & 0o077:
            raise ValueError(f'{path} is readable by others: chmod 600 it')
    with open(os.path.join(d, 'ssh_config'), encoding='utf-8', errors='replace') as f:
        lines = f.read().splitlines()
    bad = [i for i, line in enumerate(lines, 1) if not SSH_CONFIG_LINE.fullmatch(line)]
    if bad:
        raise ValueError(f'{d}/ssh_config line {bad[0]}: only Host egress, HostName, User and Port may be set there')
    if [line.strip() for line in lines if line.startswith('Host ')] != ['Host egress'] or not any(
            line.strip().startswith('HostName') for line in lines):
        raise ValueError(f'{d}/ssh_config must have one "Host egress" with its HostName')


def env_file(inv: inventory.Inventory, cls: str, etc: str, card: int | None = None) -> str:
    """A runner's ``.env`` (design §5.3): the runner app reads it into its environment,
    which its hooks and its jobs' steps inherit. A per-card runner names its card in
    ``CI_RUNNER_CARD``."""
    h = inv.host
    hooks = hookcopy.hooks_dir(h)  # the copies in the private /tmp on a 3090 host (decision 13.5)
    lines = []
    if 'tunnel' in h:
        t = h['tunnel']
        lines += [f'https_proxy=http://127.0.0.1:{t["http_port"]}', f'http_proxy=http://127.0.0.1:{t["http_port"]}',
                  f'no_proxy={",".join(t["direct"])}']
    lines += [f'ACTIONS_RUNNER_HOOK_JOB_STARTED={hooks}/job-started.sh',
              f'ACTIONS_RUNNER_HOOK_JOB_COMPLETED={hooks}/job-completed.sh',
              f'CI_RUNNER_CLASS={cls}', f'CI_RUNNERS_ETC={etc}']
    own = inventory.pool_labels(inv, cls)
    if own:  # the launcher's retry-on when this host lacks the release
        lines.append(f'CI_RUNNER_POOL_LABELS={",".join(own)}')
    if card is not None:
        lines.append(f'CI_RUNNER_CARD={card}')
    if h.get('python'):
        lines.append(f'CI_RUNNERS_PYTHON={h["python"]}')
    lines.append('LANG=C.UTF-8')
    return '\n'.join(lines) + '\n'


def shared_files(h: dict) -> tuple[list[str], list[str]]:
    """The files the CI account shares with the local accounts (decision 12.9): the host
    lock with its turnstile, shared marker and probe file (issue #69), when the host has
    one, and every slot file; and the directories: the slot queue, and the GPU leases'
    ``lease_dir`` where the inventory names one (decision 15.1). Pure."""
    lock = h['files'].get('host_lock')
    files = [*locks.paths(lock), locks.probe_path(lock)] if lock else []
    files += slots.slot_paths(h['files']['slot_lock'], h['slots'])
    return files, [h['files']['slot_queue'], *([h['files']['lease_dir']] if h['files'].get('lease_dir') else [])]


def tmpfiles_text(h: dict) -> str:
    """The root step's ``tmpfiles.d`` file (§5.3): each shared file root-owned (so that
    ``fs.protected_regular`` lets every account open it with ``O_CREAT``, as brainspike's
    scripts do), with ``rw`` ACL entries for the CI account and each local account; the
    queue directory (and the lease directory) with the same entries, recursively, and as
    default entries, so what either account creates in it the other can use; all of them
    out of age cleaning. Pure."""
    files, dirs = shared_files(h)
    accounts = [h['account'], *h['local_accounts']]
    for x in [*files, *dirs]:
        if not inventory.shared_path(x):
            raise ValueError(f'shared file {x!r} must be a plain name directly in /tmp')
    for a in accounts:
        if not inventory.ACCOUNT.fullmatch(a):
            raise ValueError(f'account {a!r} is malformed')
    rw = ','.join(f'u:{a}:rw' for a in accounts) + ',m::rw'
    rwx = ','.join(f'u:{a}:rwx' for a in accounts) + ',m::rwx'
    dflt = ','.join(['d:u::rwx', 'd:g::---', 'd:o::---', *(f'd:u:{a}:rwx' for a in accounts), 'd:m::rwx'])
    out = ['# ci-runners (decision 12.9): the host lock and slot files that the CI account shares',
           '# with the local accounts, root-owned in /tmp, recreated at boot, never age-cleaned.']
    for f in files:
        out += [f'f {f} 0660 root root -', f'a+ {f} - - - - {rw}']
    for d in dirs:
        out += [f'd {d} 0770 root root -', f'A+ {d} - - - - {rwx},{dflt}']
    out += [f'x {x}' for x in [*files, *dirs]]
    return '\n'.join(out) + '\n'


def private_text(h: dict) -> str:
    """The root step's drop-in on the CI account's ``user@<uid>.service`` (decision 12.16,
    §5.3): a private ``/tmp`` and ``/var/tmp``, an empty ``/dev/shm``, ``/home`` and the
    data-disk root (the parent of ``data``) emptied, and only the account's ``home``, its
    ``data`` and the shared files of 12.9 bound back in; every other account's processes
    hidden (``ProtectProc=invisible``). Pure."""
    home, data = h['home'], h['data']
    for key, v in (('home', home), ('data', data)):
        if not isinstance(v, str) or not PATH.fullmatch(v) or os.path.normpath(v) != v or v.startswith('//'):
            raise ValueError(f'{key} must be a normal absolute path matching {PATH.pattern}')
    root = os.path.dirname(data)
    if root == '/':
        raise ValueError(f'data must lie below a data-disk root, not directly in /: {data!r}')
    if home == '/home' or root == home or root.startswith(home + '/'):
        raise ValueError('home must not be /home, and the data-disk root (the parent of data) must not be in home')
    files, dirs = shared_files(h)
    for x in [*files, *dirs]:
        if not inventory.shared_path(x):
            raise ValueError(f'shared file {x!r} must be a plain name directly in /tmp')
    empty = ['/dev/shm:mode=1777', '/home', *([root] if root != '/home' else [])]
    return '\n'.join(['# ci-runners (decision 12.16): the CI account sees no other account\'s files',
                      '[Service]', 'PrivateTmp=yes', 'ProtectProc=invisible', f'TemporaryFileSystem={" ".join(empty)}',
                      f'BindPaths={" ".join([home, data, *files, *dirs])}']) + '\n'


#: never a data-disk root, nor below one (decision 12.16): a mount point alone would let /run, /dev or a separate /tmp pass
SYSTEM_DIRS = ('/', '/run', '/dev', '/proc', '/sys', '/tmp', '/var/tmp', '/usr', '/etc', '/boot')


def check_private_paths(h: dict) -> None:
    """Refuse a drop-in whose paths are not what they say on this host (decision 12.16):
    ``home``, ``data`` and the data-disk root each its own real path (no link in it, so a
    bind or an emptied directory is the one named), and the data-disk root a mount point
    (a disk, never a system tree such as ``/usr`` or ``/tmp``). Reads the file system."""
    root = os.path.dirname(h['data'])
    for key, v in (('home', h['home']), ('data', h['data']), ('the data-disk root', root)):
        if os.path.realpath(v) != v:
            raise ValueError(f'{key} {v!r} is not its real path: name it without links')
    if any(root == d or (d != '/' and root.startswith(d + '/')) for d in SYSTEM_DIRS):
        raise ValueError(f'the data-disk root {root!r} is a system directory or lies in one')
    if not os.path.ismount(root):
        raise ValueError(f'the data-disk root {root!r} (the parent of data) must be a mount point')


def account_uid(account: str) -> int | None:
    """The account's uid, or None if there is no such account."""
    try:
        return pwd.getpwnam(account).pw_uid
    except KeyError:
        return None


def manager_mounts(uid: int, run=subprocess.run) -> str | None:
    """The mount table of the account's user manager (``/proc/<pid>/mountinfo``), or None
    if it is not running or the table cannot be read."""
    try:
        r = run(['systemctl', 'show', '-p', 'MainPID', '--value', f'user@{uid}.service'], capture_output=True,
                text=True, timeout=30, stdin=subprocess.DEVNULL)
        pid = int(r.stdout.strip()) if r.returncode == 0 else 0
        if pid <= 0:
            return None
        with open(f'/proc/{pid}/mountinfo', encoding='utf-8') as f:
            return f.read()
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def private_state(h: dict, uid: int | None, *, read=None, mounts=manager_mounts, here=None) -> list[tuple[str, str]]:
    """``(what, what is missing)`` for each part of the drop-in's root step not done: the
    drop-in as planned, and the account's user manager running with it (its mount table,
    ``namespace_faults``, against the plan's own: the plan runs outside the namespace, as
    the lead runs it). Empty when both are done. Reads the files."""
    if uid is None:
        return [(h['account'], 'no such account')]
    path = DROPIN.format(uid=uid)
    todo = []
    try:
        with open(path if read is None else read, encoding='utf-8') as f:
            if f.read() != private_text(h):
                todo.append((path, 'differs from the plan'))
    except OSError:
        todo.append((path, 'missing'))
    table = mounts(uid)
    unit = f'user@{uid}.service'
    if table is None:
        return [*todo, (unit, 'not running, or its mount table cannot be read')]
    try:
        with open('/proc/self/mountinfo' if here is None else here, encoding='utf-8') as f:
            own = f.read()
    except OSError:
        own = ''
    bad = namespace_faults(h, table, own)
    if bad:
        todo.append((unit, f'runs without the drop-in as planned ({"; ".join(bad)}): restart it'))
    return todo


def namespace_faults(h: dict, table: str, here: str) -> list[str]:
    """What in a mount table (``/proc/<pid>/mountinfo``) differs from the namespace the
    drop-in makes, judged by the mounts a path reaches (the host's mounts below the
    emptied trees stay in the table, shadowed): tmpfs at ``/home`` and the data-disk
    root, and at ``/dev/shm`` one other than the host's (``here``: the plan's own
    table, read outside the namespace); ``/tmp`` and ``/var/tmp`` private;
    ``/proc`` with ``hidepid=invisible``; a bind at ``home``, ``data`` and each shared
    file, none of a deleted file (one replaced since the start, which would split the
    lock); and no other mount reached in ``/home``, the data-disk root or ``/tmp``. Empty
    when it matches. Pure."""
    def parse(text: str) -> dict[str, dict]:
        out = {}
        for line in text.splitlines():
            left, _, right = line.partition(' - ')
            f, r = left.split(), right.split()
            if len(f) >= 5 and len(r) >= 3:
                out[f[0]] = {'id': f[0], 'parent': f[1], 'dev': f[2], 'root': f[3], 'point': f[4], 'type': r[0],
                             'opts': r[2]}
        return out

    def reacher(mounts: dict[str, dict]):
        order = list(mounts.values())  # a later line is mounted over an earlier one

        def reach(path: str) -> dict:
            """The mount that ``path`` resolves into: from the namespace's root mount, at
            each prefix the last mount on the one reached so far (stacked ones in turn)."""
            cur = next((m for m in order if m['parent'] not in mounts), {'id': None})
            parts = path.strip('/').split('/') if path != '/' else []
            for p in ['/', *('/' + '/'.join(parts[:k]) for k in range(1, len(parts) + 1))]:
                while True:
                    on = [m for m in order if m['point'] == p and m['parent'] == cur['id']]
                    if not on:
                        break
                    cur = on[-1]
            return cur
        return reach

    mounts = parse(table)
    order = list(mounts.values())
    reach = reacher(mounts)

    root = os.path.dirname(h['data'])
    files, dirs = shared_files(h)
    binds = [h['home'], h['data'], *files, *dirs]
    bad = [f'no tmpfs at {p}' for p in ('/home', root) if reach(p).get('type') != 'tmpfs' or reach(p)['point'] != p]
    shm, host = reach('/dev/shm'), reacher(parse(here))('/dev/shm')
    if shm.get('type') != 'tmpfs' or shm['point'] != '/dev/shm' or 'dev' not in host or shm['dev'] == host['dev']:
        bad.append("/dev/shm is the host's (or this plan runs in the namespace)")
    bad += [f'{p} not private' for p in ('/tmp', '/var/tmp')
            if reach(p).get('point') != p or '/systemd-private-' not in reach(p)['root']]
    if 'hidepid=invisible' not in reach('/proc').get('opts', '').split(','):
        bad.append('other processes visible')
    for p in binds:
        m = reach(p)
        if m.get('point') != p:
            bad.append(f'no bind at {p}')
        elif m['root'].endswith('//deleted'):
            bad.append(f'{p} bound from a deleted file')
    allowed = {'/home', root, '/tmp', *binds}
    for m in order:
        p = m['point']
        if (any(p.startswith(d + '/') for d in ('/home', root, '/tmp')) and p not in allowed
                and not any(p.startswith(b + '/') for b in (h['home'], h['data'])) and reach(p) is m):
            bad.append(f'another mount at {p}')
    return bad

ACL_USER, ACL_MASK = 0x02, 0x10


def acl_users(path: str, default: bool = False) -> dict[int, int] | None:
    """``{uid: perm}`` of the named-user entries of ``path``'s access (or default) ACL,
    with the mask applied; None if it has no such ACL. Read from the xattr, so no
    ``getfacl`` is needed."""
    try:
        raw = os.getxattr(path, 'system.posix_acl_default' if default else 'system.posix_acl_access',
                          follow_symlinks=False)
    except OSError:
        return None
    entries = [(int.from_bytes(raw[i:i + 2], 'little'), int.from_bytes(raw[i + 2:i + 4], 'little'),
                int.from_bytes(raw[i + 4:i + 8], 'little')) for i in range(4, len(raw) - 7, 8)]
    mask = next((perm for tag, perm, _ in entries if tag == ACL_MASK), 7)
    return {uid: perm & mask for tag, perm, uid in entries if tag == ACL_USER}


def shared_state(h: dict, *, uid_of=lambda a: pwd.getpwnam(a).pw_uid, read=None) -> list[tuple[str, str]]:
    """``(path, what is missing)`` for each root step not done: the ``tmpfiles.d`` file as
    planned, and each shared file root-owned with ``rw`` for every account (the queue with
    its default entries too). Empty when all are done. Reads the files."""
    todo = []
    try:
        with open(TMPFILES if read is None else read, encoding='utf-8') as f:
            if f.read() != tmpfiles_text(h):
                todo.append((TMPFILES, 'differs from the plan'))
    except OSError:
        todo.append((TMPFILES, 'missing'))
    uids = {}
    for a in [h['account'], *h['local_accounts']]:
        try:
            uids[a] = uid_of(a)
        except KeyError:
            todo.append((a, 'no such account'))
    files, dirs = shared_files(h)
    for path in [*files, *dirs]:
        is_dir = path in dirs
        try:
            st = os.lstat(path)
        except OSError:
            todo.append((path, 'missing'))
            continue
        if not (stat.S_ISDIR if is_dir else stat.S_ISREG)(st.st_mode):
            todo.append((path, 'not a directory' if is_dir else 'not a regular file'))
            continue
        if st.st_uid != 0:
            todo.append((path, 'not root-owned'))
            continue
        need, kind = (7, 'rwx') if is_dir else (6, 'rw')  # a directory: x to reach its tickets
        for default in ((False, True) if is_dir else (False,)):
            got = acl_users(path, default) or {}
            short = [a for a, u in uids.items() if got.get(u, 0) & need != need]
            if short:
                todo.append((path, f'no {"default " if default else ""}{kind} ACL entry for {", ".join(short)}'))
    return todo


def authorized_keys_text(old: str, key: str, *, etc: str, python: str, base: str) -> str:
    """The account's ``authorized_keys`` with the push key's line (decision 12.4): the key
    may only run ``org-sync --receive`` of the installed release, with nothing else
    (``restrict``); a line of ours from before is replaced, every other line kept. Pure."""
    if not inventory.PUSH_KEY.fullmatch(key):
        raise ValueError('push_key must be an ssh-ed25519 public key')
    for v in (etc, python, base):
        if not PATH.fullmatch(v):
            raise ValueError(f'{v!r} must be an absolute path matching {PATH.pattern}')
    kind, b64 = key.split()[:2]
    cmd = f'CI_RUNNERS_ETC={etc} CI_RUNNERS_PYTHON={python} exec {base}/releases/hook/bin/ci-run org-sync --receive'
    keep = [line for line in old.splitlines() if line.split()[-1:] != [PUSH_MARK]]
    return '\n'.join([*keep, f'restrict,command="{cmd}" {kind} {b64} {PUSH_MARK}']) + '\n'


def tunnel_public_key(etc: str, run=subprocess.run) -> str:
    """The tunnel key's public half, for the egress account's line, or a placeholder."""
    try:
        r = run(['ssh-keygen', '-y', '-P', '', '-f', os.path.join(etc, 'tunnel', 'id_ed25519')], capture_output=True,
                text=True, timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        r = None
    # the comment, which may hold spaces, is dropped: it often names an account and host
    pub = ' '.join((r.stdout if r is not None and r.returncode == 0 else '').split()[:2])
    if not inventory.PUSH_KEY.fullmatch(pub):
        return '<the public key of etc/tunnel/id_ed25519>'
    return pub


class Plan:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.actions: list[dict] = []
        self.blocked = ''  # why apply must refuse this plan (a root step not done), or ''

    def add(self, line: str, **action) -> None:
        self.lines.append(line)
        if action:
            self.actions.append(action)

    def text(self) -> str:
        return '\n'.join(self.lines) + '\n'

    def digest(self) -> str:
        return hashlib.sha256(self.text().encode()).hexdigest()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file(plan: Plan, path: str, content: str, mode: int) -> None:
    data = content.encode()
    try:
        with open(path, 'rb') as f:
            old = f.read()
    except OSError:
        old = None
    try:
        old_mode = os.stat(path).st_mode & 0o777
    except OSError:
        old_mode = None
    if old == data and old_mode == mode:
        plan.add(f'  keep   {path}')
        return
    verb = 'write' if old is None else 'update'
    plan.add(f'  {verb:6} {path} mode {mode:o} sha256 {_sha(data)[:16]}', kind='file', path=path,
             content=content, mode=mode)
    if old is not None:
        for d in difflib.unified_diff(old.decode(errors='replace').splitlines(), content.splitlines(),
                                      'in place', 'planned', lineterm='', n=1):
            plan.add(f'         {d}')
    else:
        for line in content.splitlines():
            plan.add(f'         | {line}')


def build(etc: str, adopt: bool = False) -> Plan:
    """The plan for this host, from ``etc`` and the installed release
    (``$CI_RUNNERS_RELEASE`` instead, to preview a plan before the account exists).
    Pure but for reading files."""
    with open(os.path.join(etc, 'inventory.toml'), encoding='utf-8') as f:
        inv = inventory.load(f.read())
    h = inv.host
    data, home = h['data'], h['home']
    base = os.path.join(data, 'ci-runners')
    release = os.path.realpath(os.environ.get('CI_RUNNERS_RELEASE') or os.path.join(base, 'releases', 'hook'))
    plan = Plan()
    plan.add(f'# ci-runners setup plan for {inv.this}, release {os.path.basename(release)}')
    plan.add('directories:')
    dirs = [(base, 0o755), (os.path.join(base, 'releases'), 0o755), (os.path.join(base, 'hooks'), 0o755),
            (etc, 0o700), (os.path.join(base, 'state'), 0o700), (os.path.join(base, 'logs'), 0o700),
            (os.path.join(data, 'actions-runner'), 0o700), (os.path.join(data, 'ci-work'), 0o700),
            (os.path.join(home, '.config', 'systemd', 'user'), 0o755)]
    unit_dir = os.path.join(home, '.config', 'systemd', 'user')
    for d, mode in dirs:
        if os.path.isdir(d) and (d == unit_dir or os.stat(d).st_mode & 0o777 == mode):
            plan.add(f'  keep   {d}/')  # the unit directory is the account's: its mode is never changed
        else:
            plan.add(f'  mkdir  {d}/ mode {mode:o}', kind='mkdir', path=d, mode=mode)
    if not isinstance(h['memory_max'], str) or not MEMORY.fullmatch(h['memory_max']):
        raise ValueError(f'memory_max must match {MEMORY.pattern}')
    python = python_of(inv)
    for key, v in (('data', data), ('python', python), ('etc', etc)):
        if not isinstance(v, str) or not PATH.fullmatch(v):
            raise ValueError(f'{key} must be an absolute path matching {PATH.pattern}')
    if etc != os.path.join(base, 'etc'):
        raise ValueError(f'the etc directory must be {os.path.join(base, "etc")}')
    if h.get('python'):
        check_python(python)
    if 'tunnel' in h:
        check_tunnel(etc)
    plan.add('hooks:')
    before = len(plan.actions)
    for name in hookcopy.NAMES:
        with open(os.path.join(release, 'hooks', name)) as f:
            _file(plan, os.path.join(base, 'hooks', name), hook_text(f.read(), python, base), 0o755)
    hooks_changed = len(plan.actions) > before
    plan.add('units:')
    subs = {'CI_MEMORY_MAX': h['memory_max'], 'CI_DATA': data, 'CI_ETC': etc,
            'CI_PYTHON': python, 'PROXY_ORDER': TUNNEL_ORDER if 'tunnel' in h else '',
            # the runners run the hooks' copies, and start only with both there (decision 13.5)
            'HOOKS_ORDER': HOOKS_ORDER if hookcopy.used(h) else '',
            'HOOKS_CHECK': hooks_check() if hookcopy.used(h) else '',
            # the forward's ports are in its unit: a changed port changes the unit, which restarts it
            'TUNNEL_HTTP': str(h['tunnel']['http_port']) if 'tunnel' in h else '',
            'TUNNEL_EGRESS': str(h['tunnel']['egress_port']) if 'tunnel' in h else ''}
    ours = _ours(os.path.join(base, 'state', 'setup.json'))
    planned: set[str] = set()

    def unit(name: str, content: str) -> None:
        path = os.path.join(unit_dir, name)
        planned.add(path)
        if os.path.lexists(path) and path not in ours:
            if not adopt:
                raise ValueError(f'{path} exists and no apply of ci-runners wrote it: another user\'s unit? '
                                 'Rename or remove it first, or plan with --adopt if it is ours')
            st = os.lstat(path)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
                raise ValueError(f'{path} is not a regular file of this account: it cannot be adopted')
            # the uid check tells nothing apart on a shared account: there the lead's reading
            # of each listed file is the check (design §5.3)
            plan.add(f'  adopt  {path} (no apply recorded it; the lead names it ours)')
            n = len(plan.actions)
            _file(plan, path, content, 0o644)
            if len(plan.actions) == n:  # kept as it is: record it; a write or update records itself, once made
                plan.actions.append({'kind': 'adopt', 'path': path})
            return
        _file(plan, path, content, 0o644)

    for name in UNITS:
        if (name.startswith('ci-orgsync') and not h.get('orgsync') or name == 'ci-tunnel.service' and 'tunnel' not in h
                or name == 'ci-hooks.service' and not hookcopy.used(h)):
            continue
        with open(os.path.join(release, 'setup', 'units', name)) as f:
            unit(name, render(f.read(), subs))
    with open(os.path.join(release, 'setup', 'units', 'ci-project.slice')) as f:
        tmpl = f.read()
    projects = []
    for path in sorted(glob.glob(os.path.join(etc, 'projects', '*.toml'))):
        with open(path, 'rb') as f:
            p = tomllib.load(f)
        name, mem = p.get('name'), p.get('hosts', {}).get(inv.this, {}).get('memory_max')
        if not isinstance(name, str) or not isinstance(mem, str):
            raise ValueError(f'{path}: name and hosts.{inv.this}.memory_max are required')
        if not PROJECT_NAME.fullmatch(name) or not MEMORY.fullmatch(mem):
            raise ValueError(f'{path}: name must match {PROJECT_NAME.pattern} and memory_max {MEMORY.pattern}')
        projects.append(name)
        unit(f'ci-{name}.slice', render(tmpl, {'PROJECT': name, 'PROJECT_MEMORY_MAX': mem}))
    if 'tunnel' in h:
        t = h['tunnel']
        plan.add('egress tunnel (the lead\'s files, checked):')
        for name in TUNNEL_FILES:
            plan.add(f'  keep   {os.path.join(etc, "tunnel", name)}')
        plan.add(f'  forward 127.0.0.1:{t["http_port"]} -> the egress proxy, 127.0.0.1:{t["egress_port"]} there; '
                 f'direct: {", ".join(t["direct"]) or "nothing"}')
        plan.add("  the egress account's authorized_keys line for this key (the lead adds it there, decision 12.3):")
        plan.add('    ' + EGRESS_LINE.format(port=t['egress_port'], key=tunnel_public_key(etc)))
    if h.get('push_key'):
        plan.add("the org snapshot's push key (decision 12.4):")
        ssh_dir = os.path.join(home, '.ssh')
        if not os.path.isdir(ssh_dir):
            plan.add(f'  mkdir  {ssh_dir}/ mode 700', kind='mkdir', path=ssh_dir, mode=0o700)
        elif os.stat(ssh_dir).st_mode & 0o022 or os.stat(ssh_dir).st_uid != os.getuid():
            raise ValueError(f'{ssh_dir} must be a directory of this account, writable by it alone')
        ak = os.path.join(ssh_dir, 'authorized_keys')
        if os.path.islink(ak):
            raise ValueError(f'{ak} is a link: replace it by a file first')
        try:
            with open(ak, encoding='utf-8') as f:
                old = f.read()
        except FileNotFoundError:
            old = ''
        _file(plan, ak, authorized_keys_text(old, h['push_key'], etc=etc, python=python, base=base), 0o600)
    if h.get('local_accounts'):
        text = tmpfiles_text(h)
        todo = shared_state(h)
        plan.add('root steps (decision 12.9, §5.3): the lead runs them as root, before apply; setup cannot:')
        plan.add(f'  {"done " if not any(p == TMPFILES for p, _ in todo) else "to do"}  write {TMPFILES} '
                 f'mode 644 owner root, sha256 {_sha(text.encode())[:16]}:')
        for line in text.splitlines():
            plan.add(f'         | {line}')
        plan.add(f'  {"done " if not todo else "to do"}  run    systemd-tmpfiles --create {TMPFILES}  '
                 '(while no local job holds them: under host_lock.sh -x, with ci_slot.sh\'s queue empty)')
        for path, why in todo:
            plan.add(f'         {path}: {why}')
        uid = account_uid(h['account'])
        text, path = private_text(h), DROPIN.format(uid='<uid>' if uid is None else uid)
        check_private_paths(h)
        unit = f'user@{"<uid>" if uid is None else uid}.service'
        private = private_state(h, uid)
        mark = 'done ' if not private else 'to do'
        names = [f'ci-runner@{n}.service' for n, _, _, _ in _runners(inv)]
        plan.add(f'  {"done " if uid is not None and all(p != path for p, _ in private) else "to do"}  write {path} '
                 f'mode 644 owner root, sha256 {_sha(text.encode())[:16]} (decision 12.16):')
        for line in text.splitlines():
            plan.add(f'         | {line}')
        plan.add(f'  {mark}  as the account, first: systemctl --user disable --now {" ".join(names)}  (the restart '
                 'starts every enabled runner, and a reboot before the drop-in would start them without it)')
        plan.add(f'  {mark}  run    systemctl daemon-reload; systemctl restart {unit}')
        plan.add(f'  {mark}  as the account, then: systemctl --user start {names[0]}  (only it, for the leak check '
                 'in a live job, T18; run apply only after that check has passed in a job on it: apply enables '
                 'every runner, and the next reboot starts them all)')
        for p, why in private:
            plan.add(f'         {p}: {why}')
        if todo or private:
            plan.blocked = 'the root steps are not done'
            plan.add('  apply refuses until they are done: run them, then plan again (and apply only after the '
                     'leak check has passed)')
    gone = sorted(p for p in ours if os.path.dirname(p) == unit_dir and p not in planned)
    if gone:  # recorded units this plan no longer holds (decision 11.10: ci-bridge.service)
        plan.add('units removed (an earlier apply wrote them; this plan no longer holds them):')
        for path in gone:
            if os.path.lexists(path):
                st = os.lstat(path)
                if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
                    raise ValueError(f'{path} is not a regular file of this account: remove it by hand')
                plan.add(f'  remove {path} (disable --now, then delete)', kind='remove', path=path)
            else:
                plan.add(f'  forget {path} (already gone)', kind='remove', path=path)
    egress = ['ci-tunnel.service'] if 'tunnel' in h else []
    # egress units already running with an older file: enable --now leaves them as they are
    stale = 'tunnel' in h and any(a['kind'] == 'file' and os.path.dirname(a['path']) == unit_dir
                                  and os.path.basename(a['path']) in egress and os.path.exists(a['path'])
                                  for a in plan.actions)
    if plan.actions:
        plan.add('systemd --user (units, egress):' if 'tunnel' in h else 'systemd --user (units):')
        plan.add('  run    systemctl --user daemon-reload', kind='run', argv=['systemctl', '--user', 'daemon-reload'])
        if egress:
            argv = ['systemctl', '--user', 'enable', '--now', *egress]
            plan.add(f'  run    {" ".join(argv)}', kind='run', argv=argv)
        if stale:
            argv = ['systemctl', '--user', 'restart', *egress]
            plan.add(f'  run    {" ".join(argv)}', kind='run', argv=argv)
    port = h['tunnel']['http_port'] if 'tunnel' in h else 0
    via = 'fetched through the egress tunnel' if 'tunnel' in h else 'fetched directly'
    plan.add(f'runners (actions/runner {RUNNER_VERSION}, sha256 {RUNNER_SHA256[:16]}, {via}):')
    cache = os.path.join(base, 'state', 'cache', os.path.basename(RUNNER_URL))
    rs = _runners(inv)
    waited = 'tunnel' not in h
    for name, labels, cls, card in rs:
        rdir = os.path.join(data, 'actions-runner', name)
        if runner_complete(rdir):
            plan.add(f'  keep   {rdir}/ (runner app present)')
        else:
            if not waited:  # the forward's port listens only once ssh is in
                t = h['tunnel']
                plan.add(f'  wait   127.0.0.1:{t["http_port"]} (tunnel)', kind='wait', ports=[t['http_port']])
                waited = True
            plan.add(f'  unpack {RUNNER_URL} -> {rdir}/', kind='runner', path=rdir, cache=cache, port=port)
        _file(plan, os.path.join(rdir, '.env'), env_file(inv, cls, etc, card), 0o600)
        plan.add(f'         labels {",".join(labels)}; work {os.path.join(data, "ci-work", name)} (at registration)')
    # a 3090 host's root step disables the runners (decision 12.16): apply enables them again
    wants = os.path.join(unit_dir, 'default.target.wants')
    off = h.get('local_accounts') and any(not os.path.lexists(os.path.join(wants, f'ci-runner@{n}.service'))
                                          for n, _, _, _ in rs)
    if plan.actions or off:  # nothing changed: a second apply is a no-op
        plan.add('systemd --user (timers, runners):')
        cmds = [['systemctl', '--user', 'daemon-reload'],
                ['systemctl', '--user', 'enable', '--now', *timers(h)]]
        if hookcopy.used(h):  # the copies before the runners; a changed hook is copied again (decision 13.5)
            cmds.append(['systemctl', '--user', 'enable', '--now', 'ci-hooks.service'])
            if hooks_changed:  # a reload, not a restart: a restart would restart every runner (Requires=)
                cmds.append(['systemctl', '--user', 'reload', 'ci-hooks.service'])
        cmds.append(['systemctl', '--user', 'enable', *[f'ci-runner@{n}.service' for n, _, _, _ in rs]])
        for c in cmds:
            plan.add(f'  run    {" ".join(c)}', kind='run', argv=c)
    plan.add('  (a runner starts at its registration: setup/register.sh, run by the lead)')
    return plan


def _ours(state_path: str) -> frozenset[str]:
    """The files an earlier apply wrote, from ``state/setup.json`` (none if it is absent)."""
    try:
        with open(state_path, encoding='utf-8') as f:
            state = json.load(f)
        out: set[str] = set()
        for a in state['applied']:
            for m in a['made']:
                if m.get('kind') == 'file':
                    out.add(m['path'])
                elif m.get('kind') == 'remove':
                    out.discard(m['path'])
        return frozenset(out)
    except FileNotFoundError:
        return frozenset()
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise ValueError(f'{state_path} is unreadable ({type(e).__name__}): fix it before planning') from None


def wait_port(port: int, timeout: float = PORT_WAIT, poll: float = 0.5) -> None:
    """Wait until something listens on ``127.0.0.1:port``: ``systemctl enable --now``
    returns once the forward has started, before it listens."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=poll):
                return
        except OSError:
            if time.monotonic() >= deadline:
                raise RuntimeError(f'nothing listens on the egress port {port} after {timeout:g} s') from None
            time.sleep(poll)


def _unpack(path: str, cache: str, port: int) -> None:
    if not os.path.exists(cache):
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        if port:
            wait_port(port)
        proxies = {'https': f'http://127.0.0.1:{port}', 'http': f'http://127.0.0.1:{port}'} if port else {}
        opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
        with opener.open(RUNNER_URL, timeout=600) as r, open(cache + '.part', 'wb') as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
        os.replace(cache + '.part', cache)
    h = hashlib.sha256()
    with open(cache, 'rb') as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    if h.hexdigest() != RUNNER_SHA256:
        os.unlink(cache)
        raise RuntimeError('the runner app does not match its sha256')
    # extract beside it, then move in: an interrupted unpack never looks complete
    tmp = path + '.ci-new'
    if os.path.lexists(tmp):
        shutil.rmtree(tmp)
    os.makedirs(tmp, mode=0o700)
    with tarfile.open(cache) as t:
        t.extractall(tmp, filter='tar')
    if not runner_complete(tmp):
        raise RuntimeError('the runner app is incomplete after extraction')
    if not os.path.lexists(path):
        os.rename(tmp, path)
        return
    for n in os.listdir(tmp):
        dst = os.path.join(path, n)
        if os.path.isdir(dst) and not os.path.islink(dst):
            shutil.rmtree(dst)
        os.replace(os.path.join(tmp, n), dst)
    os.rmdir(tmp)


def runner_complete(path: str) -> bool:
    """The runner app is all there: its configure script and its listener."""
    return all(os.path.isfile(os.path.join(path, f)) for f in RUNNER_FILES)


def apply(etc: str, plan_file: str, run=subprocess.run, adopt: bool = False) -> int:
    with open(plan_file, encoding='utf-8') as f:
        approved = f.read()
    plan = build(etc, adopt)
    if plan.blocked:
        print(f'apply: {plan.blocked}: see the plan', file=sys.stderr)
        return 1
    if not plan.actions:
        print(f'apply: nothing to change (plan {plan.digest()[:16]})')
        return 0
    if plan.text() != approved:
        print(f'apply: the plan changed (approved {hashlib.sha256(approved.encode()).hexdigest()[:16]}, '
              f'now {plan.digest()[:16]}): run plan.sh again and have it approved', file=sys.stderr)
        return 1
    state_path = os.path.join(os.path.dirname(etc), 'state', 'setup.json')
    made = []
    try:
        _apply_actions(plan, made, run)
    finally:  # a failed apply still records what it made, so the next plan knows its own files
        _record(state_path, plan.digest(), made)
    print(f'apply: {len(made)} change(s), plan {plan.digest()[:16]}')
    return 0


def _apply_actions(plan: Plan, made: list, run) -> None:
    for a in plan.actions:
        k = a['kind']
        if k == 'mkdir':
            os.makedirs(a['path'], exist_ok=True)
            os.chmod(a['path'], a['mode'])
        elif k == 'file':
            tmp = a['path'] + '.ci-new'
            if os.path.lexists(tmp):
                os.unlink(tmp)  # a leftover, or a planted link: removed, never followed
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(a['content'])
                os.fchmod(f.fileno(), a['mode'])
            os.replace(tmp, a['path'])
        elif k == 'adopt':  # recorded as a file of this apply's, so uninstall removes it
            made.append({'kind': 'file', 'path': a['path']})
            print(f'apply: adopt {a["path"]}')
            continue
        elif k == 'remove':
            _remove_unit(a['path'], run)
        elif k == 'wait':
            for port in a['ports']:
                wait_port(port)
        elif k == 'runner':
            _unpack(a['path'], a['cache'], a['port'])
        elif k == 'run':
            run(a['argv'], check=True, timeout=300)
        made.append({x: a[x] for x in ('kind', 'path', 'argv') if x in a})
        print(f'apply: {k} {a.get("path") or " ".join(a.get("argv", []))}')


def _remove_unit(path: str, run) -> None:
    """Disable and stop a unit an earlier apply wrote, then delete its file: a regular
    file of this account only, checked again here; a link or another's file stops the
    apply. An absent file is forgotten, but its unit is still stopped and disabled: it may
    run on from memory, holding its port, with its wants link behind."""
    if not os.path.lexists(path):
        name = os.path.basename(path)
        run(['systemctl', '--user', 'stop', name], check=False, timeout=300)
        run(['systemctl', '--user', 'disable', name], check=False, timeout=300)
        return
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
        raise RuntimeError(f'{path} is not a regular file of this account: not removed')
    run(['systemctl', '--user', 'disable', '--now', os.path.basename(path)], check=True, timeout=300)
    os.unlink(path)


def _record(state_path: str, digest: str, made: list) -> None:
    if not made:
        return
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    try:
        with open(state_path) as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {'applied': []}
    state['applied'].append({'plan': digest, 'made': made})
    with open(state_path + '.new', 'w') as f:
        json.dump(state, f, indent=1)
    os.replace(state_path + '.new', state_path)


def main(argv: list[str]) -> int:
    etc = os.environ.get('CI_RUNNERS_ETC') or os.path.expanduser('~/data/ci-runners/etc')
    if argv == ['runners']:
        with open(os.path.join(etc, 'inventory.toml'), encoding='utf-8') as f:
            inv = inventory.load(f.read())
        for name, labels, cls in runners(inv):
            print(name, ','.join(labels), cls)
        return 0
    if argv in (['plan'], ['plan', '--adopt']):
        p = build(etc, adopt=len(argv) == 2)
        sys.stdout.write(p.text())
        print(f'# plan sha256 {p.digest()}', file=sys.stderr)
        return 0
    if len(argv) in (3, 4) and argv[0] == 'apply' and argv[1] == '--plan' and argv[3:] in ([], ['--adopt']):
        return apply(etc, argv[2], adopt=len(argv) == 4)
    print('usage: ci-run plan [--adopt] | ci-run apply --plan FILE [--adopt]', file=sys.stderr)
    return 2
