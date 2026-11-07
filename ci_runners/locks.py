"""The host lock: brainspike's ``host_lock.sh`` protocol exactly, on the 3090 hosts
(design decision 12.9, D5, §7).

The same files and the same ``flock(2)`` protocol, so a holder here and a
``host_lock.sh`` (or brainspike ``host_lock.py``) holder exclude each other:

- the host lock ``<lock>``: shared (``-s``, ``-l``) or exclusive (``-x``);
- the turnstile ``<stem>.turnstile.lock``: every request takes it exclusively first. A
  shared request lets it go once it holds the host lock; an exclusive one keeps it until
  it lets the host lock go. A waiting exclusive request so keeps new shared requests
  out (writer preference);
- the shared marker ``<stem>.shared.lock``, held shared by every normal shared holder.
  A light request (``-l``) enters past the turnstile, beside them, while one is in;
  otherwise it queues like ``-s``.

Nesting: the command runs with ``BRAINSPIKE_HOST_LOCK=s|x``, the mode it holds. Under
``x`` every request is a no-op; under ``s`` a shared request is a no-op and an
exclusive one fails (status 3), since it would wait for its caller. A cover is checked
before it is trusted: this process or an ancestor has the host lock open, and a
non-blocking probe on a fresh descriptor is refused. Otherwise it is stale, said on
stderr, and the request takes the lock.

Every wait blocks in the kernel, which wakes blocked requests in order; a timed wait
runs ``flock -w`` on our own open file description. The lock descriptors are not
inherited by the command. CLI::

    ci-lock [--lock PATH] [--timeout S] -s|-x|-l CMD [ARG...]

The path is ``--lock``, else ``$CI_HOST_LOCK``, else ``$LOCAL_CI_HOST_LOCK``.

Two accounts. The files are root-owned in ``/tmp``, with ACL entries giving the CI
account and the local account ``rw`` (decision 12.9). ``fs.protected_regular`` refuses
an ``O_CREAT`` open of another account's file there, so an existing file is opened
without it. A missing file in a sticky directory is an error, not created: a file of
ours there would lock the other account out until the next boot's ``tmpfiles.d``.
"""
from __future__ import annotations

import errno
import fcntl
import os
import shutil
import stat
import subprocess
import sys
import time

ENV = 'BRAINSPIKE_HOST_LOCK'  # the protocol's cover variable, shared with host_lock.sh
MODES = ('s', 'x', 'l')


class LockTimeout(TimeoutError):
    """Not acquired within the timeout; nothing is held."""


class NestedLockError(RuntimeError):
    """An exclusive request under the caller's shared hold: it would wait for itself."""


def paths(lock: str) -> tuple[str, str, str]:
    """``(host lock, turnstile, shared marker)``, as ``host_lock.sh`` derives them."""
    stem = lock[:-len('.lock')] if lock.endswith('.lock') else lock
    return lock, stem + '.turnstile.lock', stem + '.shared.lock'


class MissingSharedFile(OSError):
    """A shared lock file is missing from a sticky directory (see the module doc)."""


def open_lock(path: str) -> int:
    """Open a lock file for flock. An existing file is opened without O_CREAT, which
    ``fs.protected_regular`` refuses for another account's file in /tmp. A missing file
    is created (mode 660) only outside a sticky directory."""
    try:
        return os.open(path, os.O_WRONLY | os.O_APPEND)
    except FileNotFoundError:
        if os.stat(os.path.dirname(os.path.abspath(path))).st_mode & stat.S_ISVTX:
            raise MissingSharedFile(errno.ENOENT, 'shared lock file missing (the host\'s tmpfiles.d entry '
                                    'creates it, root-owned)', path) from None
        return os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o660)


def held_here(path: str, pid: int | None = None) -> bool:
    """Whether process ``pid`` (default: this one) or an ancestor has ``path`` open."""
    try:
        target = os.stat(path)
    except OSError:
        return False
    pid = os.getpid() if pid is None else pid
    for _ in range(100):
        if pid <= 1:
            return False
        try:
            fds = os.listdir(f'/proc/{pid}/fd')
        except OSError:
            fds = []
        for fd in fds:
            try:
                st = os.stat(f'/proc/{pid}/fd/{fd}')
            except OSError:
                continue
            if (st.st_dev, st.st_ino) == (target.st_dev, target.st_ino):
                return True
        try:
            with open(f'/proc/{pid}/stat') as f:
                text = f.read()
        except OSError:
            return False
        pid = int(text[text.rindex(')') + 2:].split()[1])
    return False


def _wait_in_queue(fd: int, op: int, seconds: float) -> bool:
    """Wait at most ``seconds`` in the kernel's queue for ``op`` on our ``fd``."""
    if shutil.which('flock') is None:
        raise RuntimeError('flock(1) is required for a timed wait')
    r = subprocess.run(['flock', '-w', f'{seconds:.3f}', '-x' if op & fcntl.LOCK_EX else '-s', str(fd)],
                       pass_fds=(fd,), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
    if r.returncode not in (0, 1):
        raise RuntimeError(f'flock -w exited {r.returncode}')
    return r.returncode == 0


class HostLock:
    """One hold on the host lock (one holder per object, not thread-safe)."""

    def __init__(self, lock: str, *, timeout: float | None = None, held: str | None = None) -> None:
        self.path, self.turnstile, self.marker = paths(lock)
        self.held = os.environ.get(ENV, '') if held is None else held
        if self.held not in ('', 's', 'x'):
            raise ValueError(f'host_lock: {ENV}={self.held}: expected s or x (or unset)')
        self.timeout = timeout
        self.mode: str | None = None  # what is held: 's' or 'x' (a light hold is 's')
        self.covered = False
        self.stale = ''
        self.waited = 0.0
        self._fds: list[int] = []  # every descriptor we hold a lock on

    def cover_live(self) -> bool:
        """Whether the inherited hold's holder still holds it (see the module doc)."""
        if not held_here(self.path):
            return False
        fd = open_lock(self.path)
        try:
            fcntl.flock(fd, (fcntl.LOCK_SH if self.held == 'x' else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        finally:
            os.close(fd)
        return False

    def acquire(self, mode: str) -> float:
        """Take the lock in ``mode`` (``s``, ``x`` or ``l``); returns the seconds waited.
        Raises NestedLockError (an ``x`` request under a live ``s`` cover) or
        LockTimeout."""
        if mode not in MODES:
            raise ValueError(f'mode {mode!r}: expected one of {MODES}')
        if self.mode is not None:
            raise RuntimeError('already holding the host lock')
        want = 's' if mode == 'l' else mode
        if self.held:
            if not self.cover_live():
                kind = 'exclusive holder' if self.held == 'x' else 'holder'
                print(f'host_lock: stale {ENV}={self.held} cover (no {kind} of {self.path}): taking the lock',
                      file=sys.stderr, flush=True)
                self.stale, self.held = self.held, ''
            elif self.held == 'x' or want == 's':
                self.mode, self.covered = self.held, True
                return 0.0
            else:
                raise NestedLockError(f"an exclusive request under the caller's shared hold ({ENV}=s) "
                                      'would wait for itself')
        t0 = time.monotonic()
        try:
            if mode == 'l' and self._normal_shared_in():
                fd = open_lock(self.path)  # enter beside the shared holders, past the turnstile
                self._fds.append(fd)
                self._flock(fd, fcntl.LOCK_SH, t0)
            else:
                ts = open_lock(self.turnstile)
                self._fds.append(ts)
                self._flock(ts, fcntl.LOCK_EX, t0)
                fd = open_lock(self.path)
                self._fds.append(fd)
                self._flock(fd, fcntl.LOCK_SH if want == 's' else fcntl.LOCK_EX, t0)
                if want == 's':
                    if mode == 's':  # a normal shared holder: light requests may enter beside it
                        sh = open_lock(self.marker)
                        self._fds.append(sh)
                        fcntl.flock(sh, fcntl.LOCK_SH)
                    fcntl.flock(ts, fcntl.LOCK_UN)
                    os.close(ts)
                    self._fds.remove(ts)
        except BaseException:
            self._close()
            raise
        self.waited = time.monotonic() - t0
        self.mode = want
        return self.waited

    @property
    def fds(self) -> tuple[int, ...]:
        """The descriptors the hold is on, to pass to a process that keeps it (``part``'s
        supervisor): the hold lasts while any process has them open."""
        return tuple(self._fds)

    def close(self) -> None:
        """Close our descriptors without unlocking: a process they were passed to keeps
        the hold until it ends (``release`` would end it for every holder of them)."""
        for fd in self._fds:
            os.close(fd)
        self._fds = []
        self.mode, self.covered = None, False

    def release(self) -> None:
        """Let go of what ``acquire`` took; the marker first, the turnstile last."""
        self._close()
        self.mode, self.covered = None, False

    def _normal_shared_in(self) -> bool:
        fd = open_lock(self.marker)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        finally:
            os.close(fd)
        return False

    def _flock(self, fd: int, op: int, t0: float) -> None:
        if self.timeout is None:
            fcntl.flock(fd, op)
            return
        try:
            fcntl.flock(fd, op | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            pass
        left = self.timeout - (time.monotonic() - t0)
        if left <= 0 or not _wait_in_queue(fd, op, left):
            raise LockTimeout(f'host lock {self.path} not acquired within {self.timeout:g} s')

    def _close(self) -> None:
        for fd in reversed(self._fds):  # the marker and the lock before the turnstile
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        self._fds = []


def main(argv: list[str]) -> int:
    lock = os.environ.get('CI_HOST_LOCK') or os.environ.get('LOCAL_CI_HOST_LOCK') or ''
    timeout = None
    while argv and argv[0] in ('--lock', '--timeout'):
        if len(argv) < 2:
            break
        if argv[0] == '--lock':
            lock = argv[1]
        else:
            timeout = float(argv[1])
        argv = argv[2:]
    if len(argv) < 2 or argv[0] not in ('-s', '-x', '-l') or not lock:
        print('usage: ci-lock [--lock PATH] [--timeout S] -s|-x|-l CMD [ARG...] '
              '(the path: --lock, $CI_HOST_LOCK or $LOCAL_CI_HOST_LOCK)', file=sys.stderr)
        return 2
    mode, cmd = argv[0][1], argv[1:]
    try:
        h = HostLock(lock, timeout=timeout)
        waited = h.acquire(mode)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    except MissingSharedFile as e:
        print(f'ci-lock: {e}', file=sys.stderr)
        return 2
    except NestedLockError as e:
        print(f'host_lock: {e}', file=sys.stderr)
        return 3
    except LockTimeout as e:
        print(f'ci-lock: {e}', file=sys.stderr)
        return 4
    if h.covered:
        print(f"host_lock: {mode} request covered by the caller's {h.held} hold", file=sys.stderr)
    else:
        print(f'host_lock: {mode} lock on {lock} after {int(waited) // 60}m{int(waited) % 60}s', file=sys.stderr)
    env = dict(os.environ, **{ENV: h.mode})
    try:
        return subprocess.call(cmd, env=env)  # our lock descriptors are not inheritable
    except OSError as e:
        print(f'ci-lock: {cmd[0]}: {e.strerror}', file=sys.stderr)
        return 127
    finally:
        h.release()


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
