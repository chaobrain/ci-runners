"""What ``ci-run slurm`` runs on a cluster's login host through the account owner's own
login (design decision 14.9): the steps that set an account's ``ci-runners`` tree up and
take it down, which the CI key's gate cannot do (it is forced to ``ci-slurm-gate``).

The driver (``ci_runners.slurmsetup``) sends this module's source, and the gate's for
``facts``, on stdin to the site's ``login_python`` (``slurmsetup.BOOT``) and reads one
JSON value back; nothing is installed for it and nothing stays running. Each function
takes and returns JSON values; a refusal raises ``LoginError``, which the boot prints on
stderr (exit 2).

Python 3.11 or later; stdlib only.
"""
from __future__ import annotations

import base64
import fcntl
import getpass
import hmac
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time

#: the subdirectories of ``<root>/ci-runners``, each mode 700
DIRS = ('releases', 'etc', 'envs', 'work', 'state', 'logs', 'run')
CANCEL_WAIT = 120  # s ``uninstall --cancel`` waits for the account's ci- jobs to end
RELAY_HEAD = 16 << 10  # the longest request head the relay reads
RELAY_CONNS = 32  # connections the relay serves at once; more are closed
RELAY_IDLE = 30.0  # s a connection may take to send its whole request head
KEEP = 3  # builds of a project kept: the linked ones and the newest
UNLINKED = '.ci-unlinked'  # in a build no link holds: its mtime is when that was first seen
CONDA_DONE = '.ci-installed'  # in <base>/conda once its installer finished
STAMP = r'[0-9]{8}T[0-9]{6}Z'
VARIANT = r'[a-z0-9][a-z0-9_.-]{0,30}'  # an env variant (``slurmsite.VARIANT``)


class LoginError(Exception):
    """A step refused: its reason."""


def _gate():
    m = sys.modules.get('ci_runners.slurmgate')
    if m is None:
        from ci_runners import slurmgate as m
    return m


def _own_dir(path: str) -> bool:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise LoginError(f'{path} is not a directory of this account')
    return True


def _write(path: str, text: str) -> None:
    tmp = f'{path}.{os.getpid()}.new'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(text)
    os.replace(tmp, path)


def facts(root: str, entry: dict) -> dict:
    """``check`` before an install: the gate's ``check`` in a temporary tree under
    ``root``, with ``entry`` as its account, removed afterwards."""
    if not _own_dir(root):
        return {'root': f'{root} does not exist'}
    tmp = tempfile.mkdtemp(prefix='.ci-runners-check-', dir=root)
    try:
        os.mkdir(os.path.join(tmp, 'etc'), 0o700)
        _write(os.path.join(tmp, 'etc', 'account.json'), json.dumps(entry))
        return _gate().check(tmp, os.path.join(tmp, 'releases', 'hook'))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def prepare(base: str) -> dict:
    """``<root>/ci-runners`` and its directories, mode 700; ``root`` must exist."""
    if not _own_dir(os.path.dirname(base)):
        raise LoginError(f'{os.path.dirname(base)} does not exist')
    made = []
    for d in (base, *(os.path.join(base, x) for x in DIRS)):
        if not _own_dir(d):
            os.mkdir(d, 0o700)
            made.append(d)
        os.chmod(d, 0o700)
    return {'made': made}


def install(base: str, tag: str, files: dict) -> dict:
    """``etc/`` files (mode 600) and ``releases/hook`` -> ``tag``, a release ``rsync``
    copied there before."""
    rel = os.path.join(base, 'releases')
    if not _own_dir(os.path.join(rel, tag)) or not os.path.isfile(os.path.join(rel, tag, 'bin', 'ci-slurm-gate')):
        raise LoginError(f'release {tag} is not in place')
    for name, text in sorted(files.items()):
        if '/' in name or name.startswith('.'):
            raise LoginError(f'etc/{name}: not a plain name')
        _write(os.path.join(base, 'etc', name), text)
    hook = os.path.join(rel, 'hook')
    if os.path.lexists(hook) and not os.path.islink(hook):
        raise LoginError(f'{hook} is not a link')
    tmp = f'{hook}.{os.getpid()}.new'
    os.symlink(tag, tmp)
    os.replace(tmp, hook)
    return {'hook': tag, 'etc': sorted(files)}


def _jobs(slurm_account: str) -> list[str]:
    """The account's ``ci-`` jobs that are still there: ``id`` each."""
    r = subprocess.run(['squeue', '-h', '-A', slurm_account, '-u', getpass.getuser(), '-o', '%i|%j|%T'],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise LoginError(f'squeue failed: {(r.stderr or "").strip()[-200:]}')
    return [line.split('|')[0] for line in r.stdout.splitlines() if line.count('|') == 2 and
            line.split('|')[1].startswith('ci-')]


def ci_jobs(slurm_account: str) -> dict:
    return {'jobs': _jobs(slurm_account)}


def uninstall(base: str, slurm_account: str, gate: str) -> dict:
    """Take the account's install down, in an order that lets nothing new start: the
    ``authorized_keys`` lines that force ``gate`` go first (no new submit), then every
    ``ci-`` job of the account is cancelled and its end awaited, then the tree ``base`` is
    removed. A link in place of ``authorized_keys`` or of ``base`` refuses."""
    if os.path.basename(base) != 'ci-runners':
        raise LoginError(f'{base} is not a ci-runners tree')
    present = _own_dir(base)
    keys = os.path.join(os.path.expanduser('~'), '.ssh', 'authorized_keys')
    removed = 0
    try:
        fd = os.open(keys, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        fd = None
    except OSError:
        raise LoginError(f'{keys} is a link: edit it by hand') from None
    if fd is not None:
        with os.fdopen(fd, encoding='utf-8') as f:
            mode = stat.S_IMODE(os.fstat(f.fileno()).st_mode)
            lines = f.read().splitlines(keepends=True)
        keep = [x for x in lines if f'command="{gate}"' not in x]
        removed = len(lines) - len(keep)
        if removed:
            _write(keys, ''.join(keep))
            os.chmod(keys, mode)
    jobs = _jobs(slurm_account)
    if jobs:
        subprocess.run(['scancel', *jobs], capture_output=True, timeout=60)
        end = time.monotonic() + CANCEL_WAIT
        while (left := _jobs(slurm_account)) and time.monotonic() < end:
            time.sleep(2)
        if left:
            raise LoginError(f'ci- jobs still there after scancel: {" ".join(left)}')
    if present:
        shutil.rmtree(base)
    return {'cancelled': jobs, 'key_lines': removed, 'removed': present}


# ------------------------------------------------------------------ env builds (14.9)


class Relay:
    """The build's egress on the login host (``egress = "tunnel"``, D4 a): an HTTP proxy
    relay on 127.0.0.1 that takes only requests carrying the build's ``Proxy-Authorization``
    (``ci`` and ``secret``), drops that header and passes each connection to ``target``, the
    Unix socket the driver's reverse forward listens on (its egress proxy's allowlist
    applies there, 11.10). It passes only ``CONNECT``: the connection is then a tunnel, and
    no later request on it carries the header past the check. At most ``RELAY_CONNS``
    connections at once, each sending its whole head within ``RELAY_IDLE``, and a connection
    from another user of the login host is closed before it takes one: they cannot tie it
    up. It lives in the build's process, and ``close`` ends every
    connection: nothing listens after it."""

    def __init__(self, secret: str, target: str) -> None:
        self.expected = 'Basic ' + base64.b64encode(f'ci:{secret}'.encode()).decode()
        self.target = target
        self.slots = threading.BoundedSemaphore(RELAY_CONNS)
        self.live: set[socket.socket] = set()
        self.lock = threading.Lock()
        self.closed = False
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.bind(('127.0.0.1', 0))
        self.srv.listen(16)
        self.port = self.srv.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def close(self) -> None:
        try:
            self.srv.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.srv.close()
        with self.lock:
            self.closed = True
            for s in self.live:
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def _track(self, s: socket.socket) -> bool:
        with self.lock:
            if self.closed:
                return False
            self.live.add(s)
            return True

    def _serve(self) -> None:
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            if _peer_uid(c) not in (None, os.getuid()) or not self.slots.acquire(blocking=False):
                c.close()
                continue
            threading.Thread(target=self._slot, args=(c,), daemon=True).start()

    def _slot(self, c: socket.socket) -> None:
        try:
            if self._track(c):
                self._handle(c)
        except OSError:
            pass
        finally:
            c.close()
            with self.lock:
                self.live.discard(c)
            self.slots.release()

    def _handle(self, c: socket.socket) -> None:
        with c:
            deadline = time.monotonic() + RELAY_IDLE
            head = b''
            while b'\r\n\r\n' not in head:
                c.settimeout(max(0.01, deadline - time.monotonic()))  # one deadline for the whole head
                chunk = c.recv(4096)
                if not chunk or len(head) + len(chunk) > RELAY_HEAD:
                    return
                head += chunk
            top, _, rest = head.partition(b'\r\n\r\n')
            lines = top.split(b'\r\n')
            auth = [x.split(b':', 1)[1].strip().decode('latin-1') for x in lines[1:]
                    if x.lower().startswith(b'proxy-authorization:')]
            if len(auth) != 1 or not hmac.compare_digest(auth[0].encode(), self.expected.encode()):
                c.sendall(b'HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm="ci"\r\n'
                          b'Content-Length: 0\r\nConnection: close\r\n\r\n')
                return
            if not lines[0].startswith(b'CONNECT '):
                c.sendall(b'HTTP/1.1 405 Method Not Allowed\r\nAllow: CONNECT\r\nContent-Length: 0\r\n'
                          b'Connection: close\r\n\r\n')
                return
            c.settimeout(None)
            kept = [x for x in lines if not x.lower().startswith(b'proxy-authorization:')]
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as u:
                try:
                    u.connect(self.target)
                except OSError:
                    c.sendall(b'HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
                    return
                if not self._track(u):
                    return
                try:
                    self._tunnel(c, u, b'\r\n'.join(kept) + b'\r\n\r\n' + rest)
                finally:
                    with self.lock:
                        self.live.discard(u)

    @staticmethod
    def _tunnel(c: socket.socket, u: socket.socket, first: bytes) -> None:
        u.sendall(first)
        back = threading.Thread(target=_pipe, args=(u, c), daemon=True)
        back.start()
        _pipe(c, u)
        back.join(60)


def _peer_uid(c: socket.socket) -> int | None:
    """The uid that owns the other end of a loopback TCP connection, from
    ``/proc/net/tcp``; None where that cannot be read (the header check still holds)."""
    try:
        port, mine = c.getpeername()[1], c.getsockname()[1]
        with open('/proc/net/tcp', encoding='ascii') as f:
            rows = [r.split() for r in f.read().splitlines()[1:]]
    except (OSError, ValueError):
        return None
    for r in rows:  # the client's socket: local is its port, remote is ours
        if len(r) > 7 and int(r[1].rsplit(':', 1)[1], 16) == port and int(r[2].rsplit(':', 1)[1], 16) == mine:
            return int(r[7])
    return None


def _pipe(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    try:
        dst.shutdown(socket.SHUT_WR)
    except OSError:
        pass


def _conda_ok(conda: str) -> bool:
    return os.access(os.path.join(conda, 'bin', 'conda'), os.X_OK) and os.path.isfile(os.path.join(conda, CONDA_DONE))


def conda_state(base: str) -> dict:
    return {'conda': _conda_ok(os.path.join(base, 'conda'))}


def _links(envs: str) -> set[str]:
    return {os.readlink(os.path.join(envs, n)) for n in os.listdir(envs) if os.path.islink(os.path.join(envs, n))}


def build(base: str, project: str, stamp: str, builds: list, files: dict, record: dict, egress: str,
          proxy: str = '', secret: str = '', socket_path: str = '', installer: str = '', grace: float = 0) -> dict:
    """Build ``project``'s envs, one per entry of ``builds`` (``variant``: ``''`` for the
    base build ``<base>/envs/<project>-<stamp>``, else that variant's own
    ``<project>-<stamp>-<variant>``; ``steps``, the driver's ``envbuild.steps``, run at nice
    10; ``install``; ``links``, the variants it serves), its requirement ``files`` in
    ``<base>/run/req-<stamp>``. Only once every build has passed, point each
    ``<project>-<variant>`` at its build (one rename each): a failed build removes this
    run's builds and leaves every link as it was. Then remove the project's builds no link
    holds but the newest ``KEEP`` of each kind, once no link has held them for ``grace``
    seconds (a job resolves its env's link when it starts, ``slurmgate.job``, and may run
    that long). The egress is the relay (``tunnel``), ``proxy`` or none (``direct``); a
    missing conda is installed from ``installer`` first, offline (a partial install is
    removed and redone)."""
    envs, run = os.path.join(base, 'envs'), os.path.join(base, 'run')
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,39}', project) or not re.fullmatch(STAMP, stamp):
        raise LoginError('project or stamp is malformed')
    names = [b['variant'] for b in builds]
    served = [v for b in builds for v in b['links']]
    if (not builds or len(set(names)) != len(names) or len(set(served)) != len(served)
            or not all(re.fullmatch(VARIANT, v) for v in served + [n for n in names if n])):
        raise LoginError('the builds are malformed')
    dirs = [os.path.join(envs, f'{project}-{stamp}' + (f'-{n}' if n else '')) for n in names]
    reqdir = os.path.join(run, f'req-{stamp}')
    env = {'PATH': '/usr/bin:/bin', 'HOME': os.path.expanduser('~'), 'LANG': 'C.UTF-8', 'GIT_TERMINAL_PROMPT': '0',
           'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull, 'PIP_NO_INPUT': '1', 'TMPDIR': run}
    lock = open(os.path.join(envs, f'.{project}.lock'), 'a')
    relay = None
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise LoginError(f'another build of {project} is running') from None
        for d in dirs:
            if os.path.lexists(d):
                raise LoginError(f'{d} exists')
        url = ''
        if egress == 'tunnel':
            relay = Relay(secret, socket_path)
            url = f'http://ci:{secret}@127.0.0.1:{relay.port}'
        elif egress == 'proxy':
            url = proxy
        if url:
            env.update({k: url for k in ('https_proxy', 'http_proxy', 'HTTPS_PROXY', 'HTTP_PROXY')},
                       no_proxy='localhost,127.0.0.1', NO_PROXY='localhost,127.0.0.1')
        conda = os.path.join(base, 'conda')
        with open(os.path.join(base, '.conda.lock'), 'a') as clock:
            fcntl.flock(clock, fcntl.LOCK_EX)  # another project's build may be installing it
            if not _conda_ok(conda):
                if not installer:
                    raise LoginError('no conda and no installer')
                shutil.rmtree(conda, ignore_errors=True)  # a partial install
                _step(['bash', installer, '-b', '-p', conda], env)
                open(os.path.join(conda, CONDA_DONE), 'w').close()
        os.makedirs(reqdir, mode=0o700)
        for rel, text in files.items():
            path = os.path.normpath(os.path.join(reqdir, rel))
            if not path.startswith(reqdir + os.sep):
                raise LoginError(f'{rel}: outside the requirements')
            os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                f.write(text)
        commits: dict[str, str] = {}
        try:
            for b, d in zip(builds, dirs):
                sys.stderr.write(f'# {os.path.basename(d)}: for {", ".join(b["links"])}\n')
                for argv in b['steps']:
                    _step(['nice', '-n', '10', *argv], env)
                got = {}
                for name in record.get('upstream_names', []):
                    r = subprocess.run(['git', '-C', os.path.join(d, 'src', name), 'rev-parse', 'HEAD'], env=env,
                                       capture_output=True, text=True, timeout=60)
                    got[name] = r.stdout.strip()
                    if r.returncode != 0 or not re.fullmatch(r'[0-9a-f]{40}', got[name]):
                        raise LoginError(f'{name}: no commit checked out')
                commits = commits or got
                with open(os.path.join(d, 'ci-env.json'), 'w', encoding='utf-8') as f:
                    json.dump({**record, 'stamp': stamp, 'variant': b['variant'], 'install': b.get('install', []),
                               'commits': got}, f, indent=1)
        except BaseException:
            for d in dirs:
                shutil.rmtree(d, ignore_errors=True)
            raise
        links = []
        for b, d in zip(builds, dirs):
            for v in b['links']:
                link = os.path.join(envs, f'{project}-{v}')
                if os.path.lexists(link) and not os.path.islink(link):
                    raise LoginError(f'{link} is not a link')
                tmp = f'{link}.new-{stamp}'
                os.symlink(os.path.basename(d), tmp)
                os.replace(tmp, link)
                links.append(os.path.basename(link))
        held, now = _links(envs), time.time()
        kinds: dict[str, list[str]] = {}
        for n in os.listdir(envs):
            m = re.fullmatch(re.escape(project) + '-' + STAMP + f'(-{VARIANT})?', n)
            if m:
                kinds.setdefault(m.group(1) or '', []).append(n)
        removed = []
        for kind in kinds.values():
            kind.sort()
            for i, n in enumerate(kind):
                if n in held:
                    continue
                mark = os.path.join(envs, n, UNLINKED)
                try:
                    since = os.stat(mark).st_mtime
                except FileNotFoundError:
                    open(mark, 'w').close()
                    since = now
                if i < len(kind) - KEEP and now - since >= grace:
                    shutil.rmtree(os.path.join(envs, n), ignore_errors=True)
                    removed.append(n)
        return {'builds': {os.path.basename(d): b['links'] for b, d in zip(builds, dirs)}, 'links': links,
                'removed': sorted(removed), 'commits': commits, 'relay_port': relay.port if relay else 0}
    finally:
        if relay is not None:
            relay.close()
        lock.close()
        shutil.rmtree(reqdir, ignore_errors=True)
        for path in (socket_path, installer):
            if path and os.path.lexists(path) and os.path.dirname(path) == run:
                os.unlink(path)


def _step(argv: list, env: dict) -> None:
    sys.stderr.write('+ ' + ' '.join(argv) + '\n')
    sys.stderr.flush()
    r = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, stdout=sys.stderr, stderr=sys.stderr)
    if r.returncode != 0:
        raise LoginError(f'step failed ({r.returncode}): {" ".join(argv[:12])}')
