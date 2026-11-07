# Copyright 2026 BrainX Ecosystem Limited. Apache 2.0.
"""Python port of ``scripts/host_lock.sh``: the reference host's lock, shared or exclusive.

The same files and the same protocol, so that a Python holder and a ``host_lock.sh``
holder exclude each other (both use ``flock(2)``):

- the host lock ``$LOCAL_CI_HOST_LOCK`` (default ``/tmp/brainspike-host.lock``);
- the turnstile ``${LOCAL_CI_HOST_LOCK%.lock}.turnstile.lock``. Every request takes it
  exclusively first. A shared request releases it as soon as it holds the host lock; an
  exclusive one keeps it until it lets the host lock go. So a waiting exclusive request
  keeps new shared requests out (writer preference);
- the shared marker ``${LOCAL_CI_HOST_LOCK%.lock}.shared.lock``, held shared by every
  shared holder for as long as it holds: a light request (``host_lock.sh -l``) enters
  past a waiting exclusive request only while a shared holder is in.

Nesting. ``host_lock.sh`` exports ``BRAINSPIKE_HOST_LOCK=s|x`` to the command it runs,
and a :class:`HostLock` exports it to its children while it holds the lock. Under ``x``
every request here is a no-op: the caller's exclusive hold covers it. Under ``s`` a
shared request is a no-op, and an exclusive one raises :class:`NestedLockError`, because
it would wait for the caller's own shared hold forever. A cover is checked before it is
trusted: this process or an ancestor must have the host lock open, and a non-blocking
probe on a fresh fd of the host lock must be refused (under ``x`` a shared probe, under
``s`` an exclusive one). If either fails, the holder that exported the cover is gone (a
child left running after its ``host_lock.sh`` exited, or a stopped parent), and a hold
the probe sees is another job's: the cover is stale, which is said on stderr, and the
request takes the lock normally. Any other value of ``BRAINSPIKE_HOST_LOCK`` is an
error.

Timeout. ``timeout`` (default ``$BRAINSPIKE_HOST_LOCK_TIMEOUT`` seconds, else none)
bounds the total time one :class:`HostLock` waits, over all its requests; past it a
request raises :class:`LockTimeout` and holds nothing.

Order. Linux wakes the requests blocked in ``flock(2)`` on a file in the order they
blocked, so blocked requests are served first come, first served. Every request here
waits blocked in the kernel, with or without a timeout. A timed wait runs ``flock -w``
(util-linux) on this process's open file description, so the lock it takes is ours,
just as ``host_lock.sh``'s ``flock`` takes it. Polling with ``LOCK_NB`` would keep a
request out of the kernel's queue. Every release would then go to a blocked request,
and the poller could wait without bound. A compile-gate sample polled like that once
waited 38 min for its exclusive request and timed out (lessons L57).

The compile gates use it (``benchmarks/compile_probe.py``, architecture §8 rule 4): a
gate sample builds its network under the shared lock and holds the exclusive lock only
around its timed calls. Stdlib only, plus flock(1) (util-linux) for a timed wait, as
``host_lock.sh`` uses it.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator

ENV = 'BRAINSPIKE_HOST_LOCK'
TIMEOUT_ENV = 'BRAINSPIKE_HOST_LOCK_TIMEOUT'
DEFAULT_PATH = '/tmp/brainspike-host.lock'

__all__ = ['ENV', 'TIMEOUT_ENV', 'HostLock', 'LockTimeout', 'NestedLockError', 'held_here', 'lock_paths',
           'shared_marker']


class LockTimeout(TimeoutError):
    """The host lock was not acquired within the timeout."""


class NestedLockError(RuntimeError):
    """An exclusive request under the caller's shared hold: it would wait for itself."""


_UNSET = object()


def bad_cover_message(value: str) -> str:
    """The error for an unknown ``BRAINSPIKE_HOST_LOCK`` value (``host_lock.sh`` prints the same)."""
    return f'host_lock: {ENV}={value}: expected s or x (or unset)'


def stale_cover_message(held: str, path: str) -> str:
    """The stderr line for a stale cover (``host_lock.sh`` prints the same)."""
    kind = 'exclusive holder' if held == 'x' else 'holder'
    return f'host_lock: stale {ENV}={held} cover (no {kind} of {path}): taking the lock'


def _nested() -> NestedLockError:
    return NestedLockError(
        f"an exclusive request under the caller's shared hold ({ENV}=s) would wait for "
        f'itself: run the timing job outside the shared hold, or under host_lock.sh -x')


def lock_paths(path: str | None = None) -> tuple[str, str]:
    """``(host lock, turnstile)`` paths, as ``host_lock.sh`` derives them."""
    path = path or os.environ.get('LOCAL_CI_HOST_LOCK') or DEFAULT_PATH
    stem = path[:-len('.lock')] if path.endswith('.lock') else path
    return path, stem + '.turnstile.lock'


def shared_marker(path: str) -> str:
    """The shared marker beside the host lock ``path`` (``host_lock.sh``'s HOST_LOCK_SHARED)."""
    return (path[:-len('.lock')] if path.endswith('.lock') else path) + '.shared.lock'


def held_here(path: str, pid: int | None = None) -> bool:
    """Whether process ``pid`` (default this one) or one of its ancestors has ``path``
    open: the holder whose cover a ``BRAINSPIKE_HOST_LOCK`` is keeps it open."""
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
            stat = open(f'/proc/{pid}/stat').read()
        except OSError:
            return False
        pid = int(stat[stat.rindex(')') + 2:].split()[1])
    return False


class HostLock:
    """One process's hold on the host lock (not thread-safe; one holder per object).

    Parameters
    ----------
    path : str, optional
        The host lock file (default ``$LOCAL_CI_HOST_LOCK``, else
        ``/tmp/brainspike-host.lock``).
    timeout : float, optional
        Total seconds this object may wait, over all its requests (default
        ``$BRAINSPIKE_HOST_LOCK_TIMEOUT``, else no limit).
    held : {'', 's', 'x'}, optional
        The hold inherited from the caller (default ``$BRAINSPIKE_HOST_LOCK``).
    poll : float, default 0.25
        Longest sleep between attempts of a timed request when flock(1) is missing (it
        then polls instead of waiting in the kernel's queue).

    Examples
    --------
    .. code-block:: python

        >>> import sys
        >>> sys.path.insert(0, 'scripts')
        >>> from host_lock import HostLock
        >>> lock = HostLock('/tmp/example-host.lock', held='')
        >>> wait = lock.acquire('s')  # build: load on the host
        >>> with lock.exclusive():    # the timed calls
        ...     pass
        >>> lock.release()
        >>> [e['mode'] for e in lock.record()['events']]
        ['s', 'x', 's']
    """

    def __init__(self, path: str | None = None, *, timeout: float | None = None,
                 held: str | None = None, poll: float = 0.25) -> None:
        self.path, self.turnstile = lock_paths(path)
        self.held = os.environ.get(ENV, '') if held is None else held
        if self.held not in ('', 's', 'x'):
            raise ValueError(bad_cover_message(self.held))
        self.stale: str | None = None  # the inherited cover, when a probe found it stale
        if timeout is None and os.environ.get(TIMEOUT_ENV):
            timeout = float(os.environ[TIMEOUT_ENV])
        self.timeout = timeout
        self.poll = poll
        self.mode: str | None = None   # what this object holds: None, 's' or 'x'
        self.waited = 0.0              # seconds waited so far, over all requests
        self.events: list[dict] = []   # one per request: mode, wait_s, hold_s, covered
        self._fd: int | None = None
        self._ts: int | None = None
        self._sh: int | None = None    # the shared marker, while holding shared
        self._since = 0.0
        self._env_saved: object = _UNSET  # BRAINSPIKE_HOST_LOCK before we exported ours

    # ------------------------------------------------------------------ requests

    def acquire(self, mode: str) -> float:
        """Take the lock in ``mode`` (``'s'`` or ``'x'``) from not holding it.

        Returns
        -------
        float
            Seconds waited (0 when the caller's hold covers the request).

        Raises
        ------
        NestedLockError
            ``mode='x'`` under the caller's live shared hold.
        LockTimeout
            Not acquired within what is left of the timeout; nothing is held then.
        """
        if mode not in ('s', 'x'):
            raise ValueError(f'mode {mode!r}: expected s or x')
        if self.mode is not None:
            raise RuntimeError(f'already holding the host lock ({self.mode}); release it first')
        if self._covers(mode):
            self._start(mode, 0.0, covered=True)
            return 0.0
        t0 = time.monotonic()
        ts = os.open(self.turnstile, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o666)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o666)
        try:
            self._flock(ts, fcntl.LOCK_EX, mode, t0)
            self._flock(fd, fcntl.LOCK_SH if mode == 's' else fcntl.LOCK_EX, mode, t0)
        except BaseException:
            os.close(fd)  # closing drops whatever was taken
            os.close(ts)
            raise
        if mode == 's':  # in: mark the shared hold, let the next request through
            self._mark()
            fcntl.flock(ts, fcntl.LOCK_UN)
            os.close(ts)
        wait = time.monotonic() - t0
        self.waited += wait
        self._fd, self._ts = fd, (ts if mode == 'x' else None)
        self._start(mode, wait, covered=False)
        return wait

    def release(self) -> None:
        """Let go of what :meth:`acquire` took (nothing if nothing is held)."""
        if self.mode is None:
            return
        self._stop()
        self._unmark()
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
        if self._ts is not None:
            fcntl.flock(self._ts, fcntl.LOCK_UN)
            os.close(self._ts)
        self._fd = self._ts = None
        self.mode = None
        self._unexport()

    def owns(self, mode: str) -> bool:
        """Whether this object holds the lock in ``mode`` itself (not through a cover)."""
        return self.mode == mode and self._fd is not None

    def cover_live(self) -> bool:
        """Whether the inherited hold is still held by its holder: this process or an
        ancestor has the host lock open (:func:`held_here`; else a hold the probe sees is
        another job's), and a non-blocking probe on a fresh fd of the host lock (shared
        under ``x``, exclusive under ``s``) is refused."""
        if not held_here(self.path):
            return False
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o666)
        try:
            fcntl.flock(fd, (fcntl.LOCK_SH if self.held == 'x' else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        finally:
            os.close(fd)  # drops the probe's lock, if it got one
        return False

    @contextlib.contextmanager
    def exclusive(self) -> Iterator[None]:
        """Hold the lock exclusively inside the block, then return to the mode before.

        From a shared hold, the shared lock is released *before* the turnstile is
        requested: an exclusive request already queued holds the turnstile and waits for
        the shared holders, so asking for it while still holding the shared lock would
        deadlock. Back from exclusive to shared is immediate: the host lock is
        converted in place while the turnstile keeps every other request out.
        """
        before = self.mode
        if before == 'x':
            yield
            return
        if self.held == 's' and self.cover_live():
            raise _nested()
        if before == 's':
            self.release()
        self.acquire('x')
        try:
            yield
        finally:
            if before == 's':
                self._downgrade()
            else:
                self.release()

    def record(self) -> dict:
        """JSON-ready summary, seconds to 4 decimals.

        ``held`` (the inherited hold), ``events`` (every request: ``mode``, ``wait_s``,
        ``hold_s``, ``covered``, ``timeout``), and ``s_wait_s``, ``x_wait_s``,
        ``x_hold_s``, ``wait_s``: totals of what this object took itself, so a request
        the caller's hold covered adds nothing (its ``hold_s`` is still listed).
        """
        events = [dict(e) for e in self.events]
        if self.mode is not None:  # still held: report the hold so far
            events[-1]['hold_s'] = time.monotonic() - self._since
        tot = lambda m, k: sum(e[k] or 0.0 for e in events if e['mode'] == m and not e['covered'])
        r4 = lambda v: round(v, 4) if isinstance(v, float) else v
        return dict(held=self.held or None, stale_cover=self.stale, timeout_s=self.timeout,
                    events=[{k: r4(v) for k, v in e.items()} for e in events],
                    s_wait_s=r4(tot('s', 'wait_s')), x_wait_s=r4(tot('x', 'wait_s')),
                    x_hold_s=r4(tot('x', 'hold_s')), wait_s=r4(self.waited))

    # ------------------------------------------------------------------ internals

    def _covers(self, mode: str) -> bool:
        """Whether the inherited hold covers a ``mode`` request; a stale one is dropped."""
        if not self.held:
            return False
        if not self.cover_live():
            print(stale_cover_message(self.held, self.path), file=sys.stderr, flush=True)
            self.stale, self.held = self.held, ''
            return False
        if self.held == 'x' or mode == 's':
            return True
        raise _nested()

    def _start(self, mode: str, wait: float, covered: bool) -> None:
        self.mode = mode
        self._since = time.monotonic()
        self.events.append(dict(mode=mode, wait_s=wait, hold_s=None, covered=covered))
        if not covered:  # children see the hold, as host_lock.sh's command does
            if self._env_saved is _UNSET:
                self._env_saved = os.environ.get(ENV)
            os.environ[ENV] = mode

    def _unexport(self) -> None:
        if self._env_saved is _UNSET:
            return
        if self._env_saved is None:
            os.environ.pop(ENV, None)
        else:
            os.environ[ENV] = self._env_saved  # type: ignore[assignment]
        self._env_saved = _UNSET

    def _stop(self) -> None:
        self.events[-1]['hold_s'] = time.monotonic() - self._since

    def _mark(self) -> None:
        self._sh = os.open(shared_marker(self.path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o666)
        fcntl.flock(self._sh, fcntl.LOCK_SH)  # only ever probed exclusively, without blocking

    def _unmark(self) -> None:
        if self._sh is not None:
            fcntl.flock(self._sh, fcntl.LOCK_UN)
            os.close(self._sh)
            self._sh = None

    def _downgrade(self) -> None:
        """Exclusive -> shared in place (we hold the turnstile, so nobody can cut in)."""
        self._stop()
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_SH)
            self._mark()
        if self._ts is not None:
            fcntl.flock(self._ts, fcntl.LOCK_UN)
            os.close(self._ts)
            self._ts = None
        self._start('s', 0.0, covered=self._fd is None)

    def _flock(self, fd: int, op: int, mode: str, t0: float) -> None:
        if self.timeout is None:
            fcntl.flock(fd, op)  # blocks; the kernel wakes us in our turn
            return
        try:
            fcntl.flock(fd, op | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            pass
        left = self.timeout - self.waited - (time.monotonic() - t0)
        if left > 0 and _wait_in_queue(fd, op, left, self.poll):
            return
        spent = time.monotonic() - t0
        self.waited += spent
        self.events.append(dict(mode=mode, wait_s=spent, hold_s=None, covered=False, timeout=True))
        raise LockTimeout(f'{mode} lock on {self.path} not acquired within '
                          f'{self.timeout:g} s (waited {self.waited:.1f} s in all)')


def _wait_in_queue(fd: int, op: int, seconds: float, poll: float) -> bool:
    """Wait at most ``seconds`` for ``op`` on ``fd`` in the kernel's queue, and return
    whether the lock was taken.

    The wait is ``flock -w`` on our open file description, so the lock it takes is ours.
    If flock(1) is missing or fails, the request polls with ``LOCK_NB`` instead, outside
    the queue, sleeping at most ``poll`` seconds between tries.
    """
    end = time.monotonic() + seconds
    if shutil.which('flock') is not None:
        r = subprocess.run(['flock', '-w', f'{seconds:.3f}', '-x' if op & fcntl.LOCK_EX else '-s',
                            str(fd)], pass_fds=(fd,), stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # A flock(1) left waiting by a killed caller takes the lock at its turn and exits
        # at once, which drops it: the description it inherited closes with it.
        if r.returncode in (0, 1):  # 1: the timeout ran out
            return r.returncode == 0
    delay = 0.005
    while True:
        try:
            fcntl.flock(fd, op | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            pass
        left = end - time.monotonic()
        if left <= 0:
            return False
        time.sleep(min(delay, left))
        delay = min(2 * delay, poll)
