"""A host's CI slots and their fair queue (design §7, rules 1, 2 and 4; decision 12.9).

``grants`` and ``queue_full`` are pure; ``SlotQueue`` keeps the queue directory, the
tickets and the slot locks that the inventory's ``files`` name; ``ci-slot`` (``main``)
runs a command holding a slot. On the 3090 hosts the files are brainspike's, which its
``scripts/ci_slot.sh`` takes too.
"""
from __future__ import annotations

import dataclasses
import errno
import fcntl
import os
import re
import signal
import stat
import sys
import time
from collections.abc import Mapping, Sequence

from .locks import MissingSharedFile, open_lock


@dataclasses.dataclass(frozen=True)
class Ticket:
    seq: int  # the queue's number: lower is older
    project: str = ''  # empty: a ticket of ci_slot.sh, which names none: the default project's (rule 4)
    every: bool = False  # it waits for every slot of the host (a ``nightly`` part, decision 12.8)


def grants(waiting: Sequence[Ticket], held: Mapping[str, int], caps: Mapping[str, int], free: int,
           default_project: str = '') -> list[int]:
    """The ``seq`` of the tickets that take the ``free`` slots now, in order.

    A ticket without a project counts as ``default_project`` (none: it gets no slot). A
    ticket whose project already holds its cap (``caps``; a project not listed has
    cap 0) is skipped. Each
    free slot goes to the waiting project that holds the fewest slots, and among
    those to the oldest ticket.

    A ticket that waits for ``every`` slot holds back every later ticket: the tickets
    older than it are served first, as above, and it takes each slot left, whatever its
    project's cap, so its seq may appear more than once. Pure.
    """
    held = {p: n for p, n in held.items()}
    queue = sorted(waiting, key=lambda t: t.seq)
    first = next((t for t in queue if t.every), None)
    if first is not None:
        queue = [t for t in queue if t.seq < first.seq]
    out: list[int] = []
    for _ in range(max(0, free)):
        best = None
        for t in queue:
            p = t.project or default_project
            if t.seq in out or held.get(p, 0) >= caps.get(p, 0):
                continue
            key = (held.get(p, 0), t.seq)
            if best is None or key < best[0]:
                best = (key, t)
        if best is None:
            break
        t = best[1]
        out.append(t.seq)
        p = t.project or default_project
        held[p] = held.get(p, 0) + 1
    if first is not None:
        out += [first.seq] * (max(0, free) - len(out))
    return out


def queue_full(waiting: int, max_waiting: int) -> bool:
    """Whether a project's new part ends ``unavailable`` ("project queue full"): it
    already has ``max_waiting`` parts waiting on this pool (rule 2). Pure."""
    return waiting >= max_waiting


# ------------------------------------------------------------------ the queue on disk
#
# The files are brainspike's ``ci_slot.sh``'s (D5): slot 1 is ``<lock>``, slot i >= 2
# is ``<stem>.<i>.lock``; the queue directory holds ``seq``, ``seq.lock``, ``take.lock`` and its
# tickets ``<seq, 12 digits>.ticket``: ``<seq> <pid> <start epoch> <cwd> <command>``.
#
# ``ci_slot.sh`` serves its tickets first come, first served: only the lowest live
# ``.ticket`` tries the slots. A ``ci-slot`` ticket is ``<seq>.wait`` instead, with
# ``ci-slot project=<p>`` before its command, so ``ci_slot.sh`` never waits behind it;
# it shares the counter, so numbers stay in order. A ``ci-slot`` waiter tries the slots
# only when ``grants`` names its ticket, counting ``ci_slot.sh``'s tickets and holders
# as the default project's (rule 4). Both take a slot with a non-blocking ``flock`` on
# the same files, so together they never hold more than the slot count. A holder of
# ours writes ``holder.<i>`` (``<pid> <start> <project>``) beside the tickets.
#
# Two accounts (decision 12.9): the slot files are root-owned in /tmp with ACL entries
# for both, and the queue directory carries default ACL entries, so what either account
# creates in it the other can read, replace and delete. A slot file is opened as the
# host lock's are (``locks.open_lock``); a missing queue directory is created only
# outside a sticky directory.

# The queue's lock files, opened in place (not written new and renamed), so they keep the
# ACL entries they were made with; the part's gate checks them (decision 15.2, issue #124).
SEQ_LOCK, TAKE_LOCK = 'seq.lock', 'take.lock'
QUEUE_LOCKS = (SEQ_LOCK, TAKE_LOCK)

_TICKET = re.compile(r'([0-9]{12})\.(ticket|wait)\Z')
_TAG = re.compile(r'ci-slot project=([A-Za-z0-9_.-]+)(?:\s|\Z)')
_EVERY = re.compile(r'ci-slot project=[A-Za-z0-9_.-]+ every(?:\s|\Z)')


# The queue directory is not sticky and both accounts write in it (decision 12.9, T17):
# nothing here writes or creates through a link there. A file is written new, created
# exclusively under a random name (O_EXCL, O_NOFOLLOW), then renamed over its name (a
# rename replaces a link, never its target); a lock file there is opened O_NOFOLLOW and
# O_NONBLOCK, and used only if it is a regular file; a
# file is read O_NOFOLLOW and O_NONBLOCK, and only if it is a regular one (a FIFO would
# block the reader).
_NEW = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
_READ = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_LOCK = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def _replace(path: str, text: str) -> None:
    """Write ``text`` to ``path`` by an exclusive new file and a rename (mode 660, so the
    queue's default ACL entries give the other account its access)."""
    tmp = f'{path}.{os.getpid()}.{os.urandom(6).hex()}'
    fd = os.open(tmp, _NEW, 0o660)
    try:
        with os.fdopen(fd, 'w', closefd=False) as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    finally:
        os.close(fd)


def _queue_lock(path: str) -> int:
    """Open a lock file of the queue for flock: never through a link, never blocking (a
    FIFO there would block the open), and only a regular file."""
    fd = os.open(path, _LOCK, 0o660)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError(errno.EINVAL, 'not a regular file', path)
    return fd


def _read(path: str) -> str:
    """A small regular file's text; OSError for a link, a FIFO or anything else."""
    fd = os.open(path, _READ)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, 'not a regular file', path)
        return os.read(fd, 65536).decode('utf-8', 'replace')
    finally:
        os.close(fd)


def slot_paths(lock: str, n: int) -> list[str]:
    stem = lock[:-len('.lock')] if lock.endswith('.lock') else lock
    return [lock] + [f'{stem}.{i}.lock' for i in range(2, n + 1)]


def _boot_time() -> float | None:
    try:
        with open('/proc/stat') as f:
            for line in f:
                if line.startswith('btime '):
                    return float(line.split()[1])
    except OSError:
        pass
    return None


def alive(pid: int, start: float) -> bool:
    """``pid`` runs (not a zombie, not stopped) and started no
    later than ``start`` (+2 s): a pid reused by a later process does not count."""
    try:
        with open(f'/proc/{pid}/stat') as f:
            stat = f.read()
    except OSError:
        return False
    fields = stat[stat.rindex(')') + 2:].split()
    if fields[0] in ('Z', 'X', 'x', 'T', 't'):
        return False
    boot = _boot_time()
    if boot is None:
        return True
    return boot + int(fields[19]) / os.sysconf('SC_CLK_TCK') <= start + 2


UNSEEN_MAX = 24 * 3600  # seconds: how long a ticket whose process /proc hides is believed (see ``unseen``)


def unseen(pid: int) -> bool:
    """``pid`` is a process of another account that this account's ``/proc`` hides
    (``ProtectProc=invisible``, design 12.16): it has no ``/proc/<pid>/stat`` for us, yet
    signal 0 is refused (EPERM), where a pid that does not exist gives ESRCH. Whether it
    is a zombie, is stopped or is a later process on a reused pid cannot be told from
    here. A pid whose ``/proc`` entry we can see is judged by ``alive``, never here."""
    if os.path.exists(f'/proc/{pid}'):
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return False


def _start_of(pid: int) -> float:
    with open(f'/proc/{pid}/stat') as f:
        stat = f.read()
    return (_boot_time() or 0) + int(stat[stat.rindex(')') + 2:].split()[19]) / os.sysconf('SC_CLK_TCK')


def _flocked(path: str) -> bool:
    """Whether some process holds a flock on ``path``, read from /proc/locks without
    taking it (a probe would make a waiter's try fail)."""
    try:
        st = os.stat(path)
        with open('/proc/locks') as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 6 and parts[1] == 'FLOCK':  # a holder; '->' lines are waiters
                    maj, mnr, ino = parts[5].split(':')
                    if int(ino) == st.st_ino and os.makedev(int(maj, 16), int(mnr, 16)) == st.st_dev:
                        return True
    except (OSError, ValueError, IndexError):
        pass
    return False


class SlotQueue:
    """A host's CI slots and their queue (see above)."""

    def __init__(self, lock: str, n: int, queue: str | None = None, poll: float = 3.0,
                 default_project: str = '', default_cap: int | None = None) -> None:
        if n < 1:
            raise ValueError('the slot count must be positive')
        self.slots = slot_paths(lock, n)
        stem = lock[:-len('.lock')] if lock.endswith('.lock') else lock
        self.queue = queue or stem + '.queue'
        self.poll = poll
        self.default = default_project  # the project of ci_slot.sh's tickets and holders (rule 4)
        # its cap: the slot count (rule 4), or a pool run's cap where it is the run's own project (15.1)
        self.default_cap = len(self.slots) if default_cap is None else default_cap
        self.seq = 0
        self.ticket = ''
        self.every: list[int] = []  # the slots ``try_take_every`` holds so far

    # -------------------------------------------------------------- tickets

    def take_ticket(self, project: str, cmd: str, every: bool = False) -> str:
        """Write our ticket after every queued one, under ``seq.lock`` as ``ci_slot.sh`` does;
        ``every`` marks a ticket that waits for every slot (``ci-slot project=<p> every
        <command>``)."""
        self._make_queue()
        fd = _queue_lock(os.path.join(self.queue, SEQ_LOCK))
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                n = int(_read(os.path.join(self.queue, 'seq')).strip() or 0)
            except (OSError, ValueError):
                n = 0
            n = max(n, self.seq)
            for name in os.listdir(self.queue):
                m = _TICKET.match(name)
                if m:
                    n = max(n, int(m.group(1)))
            self.seq = n + 1
            _replace(os.path.join(self.queue, 'seq'), f'{self.seq}\n')
            self.ticket = os.path.join(self.queue, f'{self.seq:012d}.wait')
            line = ' '.join([str(self.seq), str(os.getpid()), str(int(time.time())), os.getcwd(),
                             f'ci-slot project={project}', *(['every'] if every else []), cmd.replace('\n', ' ')])
            _replace(self.ticket, line + '\n')
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        return self.ticket

    def waiting(self) -> list[Ticket]:
        """The live tickets of both kinds; a dead or malformed one is deleted, as
        ``ci_slot.sh`` does (ours is never deleted here)."""
        out = []
        try:
            names = sorted(os.listdir(self.queue))
        except OSError:
            return out
        for name in names:
            m = _TICKET.match(name)
            if not m:
                continue
            path = os.path.join(self.queue, name)
            try:
                text = _read(path)
            except OSError:
                continue
            parts = text.split(maxsplit=3)
            ok = (len(parts) >= 3 and all(p.isdigit() for p in parts[:3]) and int(parts[0]) == int(m.group(1))
                  and int(parts[1]) > 0)
            ours = m.group(2) == 'wait'
            if path != self.ticket and not (ok and self._live(int(parts[1]), float(parts[2]), ours)):
                try:
                    os.unlink(path)
                except OSError:
                    pass
                continue
            tag = _TAG.search(parts[3]) if len(parts) > 3 else None
            every = ours and len(parts) > 3 and bool(_EVERY.search(parts[3]))
            out.append(Ticket(int(m.group(1)), tag.group(1) if tag and ours else '', every))
        return out

    @staticmethod
    def _live(pid: int, start: float, ours: bool) -> bool:
        """Whether the owner of a ticket runs. A ``ci_slot.sh`` ticket belongs to a local
        account's process, which our ``/proc`` may hide: if signal 0 shows that it exists
        we keep the ticket (its owner deletes it as it ends, and ``ci_slot.sh``'s own
        readers delete a dead one), for ``UNSEEN_MAX`` after its start; deleting it would
        send a live waiter to the back of the queue on every poll of ours."""
        if alive(pid, start):
            return True
        return not ours and time.time() - start <= UNSEEN_MAX and unseen(pid)

    def held(self) -> tuple[dict[str, int], int, set[int]]:
        """``(slots held per project, free slots, indexes of free slots)``. A held slot
        without a live holder record of ours (``ci_slot.sh``'s) counts under the key
        ''."""
        per: dict[str, int] = {}
        free = set()
        for i, path in enumerate(self.slots, 1):
            if not _flocked(path):
                free.add(i)
                continue
            project = ''
            try:
                pid, start, project = _read(os.path.join(self.queue, f'holder.{i}')).split()[:3]
                if not alive(int(pid), float(start)):
                    project = ''
            except (OSError, ValueError):
                project = ''
            per[project] = per.get(project, 0) + 1
        return per, len(free), free

    # -------------------------------------------------------------- taking a slot

    def try_take(self, project: str, caps: dict[str, int]) -> int | None:
        """One round: if ``grants`` names our ticket, try the free slots in order with a
        non-blocking flock. Returns the fd of the slot taken (inheritable), or None.

        The round holds ``take.lock``, so no other round reads the holders between our
        flock of a slot and our holder record: a slot held without a record counts for
        no project, and a project could then pass its cap."""
        if not os.path.exists(self.ticket):  # deleted while we were stopped: re-queue
            self.take_ticket(project, 'requeued')
        lk = _queue_lock(os.path.join(self.queue, TAKE_LOCK))
        try:
            fcntl.flock(lk, fcntl.LOCK_EX)
            return self._take(project, caps)
        finally:
            os.close(lk)

    def try_take_every(self, project: str, caps: dict[str, int]) -> list[int] | None:
        """One round for a ticket that waits for every slot (``take_ticket(...,
        every=True)``): take the free slots ``grants`` gives it, after the older tickets
        within their ``caps``, and keep them. Returns the
        fds of all the slots (inheritable) once it holds every one, else None; the slots
        it holds so far stay held (``self.every``) and its ticket stays queued."""
        if not os.path.exists(self.ticket):  # deleted while we were stopped: re-queue
            self.take_ticket(project, 'requeued', every=True)
        lk = _queue_lock(os.path.join(self.queue, TAKE_LOCK))
        try:
            fcntl.flock(lk, fcntl.LOCK_EX)
            for _ in range(self._grants(caps).count(self.seq)):
                fd = self._grab(project, unlink=False)
                if fd is None:
                    break
                self.every.append(fd)
        finally:
            os.close(lk)
        if len(self.every) < len(self.slots):
            return None
        try:
            os.unlink(self.ticket)
        except OSError:
            pass
        return list(self.every)

    def _grants(self, caps: dict[str, int]) -> list[int]:
        """``grants`` over the queue and the holders now, ``ci_slot.sh``'s counted as the
        default project's (rule 4)."""
        per, nfree, _ = self.held()
        if self.default:
            per[self.default] = per.get(self.default, 0) + per.pop('', 0)
            caps = dict(caps, **{self.default: self.default_cap})  # its ci_slot.sh waiters apply none (rule 4)
        return grants(self.waiting(), per, caps, nfree, self.default)

    def _take(self, project: str, caps: dict[str, int]) -> int | None:
        if self.seq not in self._grants(caps):
            return None
        return self._grab(project)

    def _grab(self, project: str, unlink: bool = True) -> int | None:
        """Take a free slot with a non-blocking flock and write its holder record; with
        ``unlink``, our ticket is done."""
        for i, path in enumerate(self.slots, 1):
            fd = open_lock(path)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as e:
                os.close(fd)
                if e.errno in (errno.EAGAIN, errno.EACCES):
                    continue
                raise
            rec = os.path.join(self.queue, f'holder.{i}')
            _replace(rec, f'{os.getpid()} {_start_of(os.getpid()):.0f} {project}\n')
            if unlink:
                try:
                    os.unlink(self.ticket)
                except OSError:
                    pass
            os.set_inheritable(fd, True)
            return fd
        return None

    def _index(self, fd: int) -> int:
        st = os.fstat(fd)
        for i, p in enumerate(self.slots):
            try:
                if os.stat(p).st_ino == st.st_ino:
                    return i
            except OSError:
                pass
        return 0

    def _make_queue(self) -> None:
        """Create the queue directory if it is missing, but not in a sticky directory:
        one of ours there would lack the default ACL entries the other account needs."""
        if os.path.isdir(self.queue):
            return
        parent = os.path.dirname(os.path.abspath(self.queue))
        if os.path.isdir(parent) and os.stat(parent).st_mode & stat.S_ISVTX:
            raise MissingSharedFile(errno.ENOENT, "shared slot queue missing (the host's tmpfiles.d entry "
                                    'creates it, root-owned)', self.queue)
        os.makedirs(self.queue, exist_ok=True)

    def hold(self, project: str, caps: dict[str, int], cmd: list[str], deadline: float | None = None) -> int:
        """Queue, take a slot, then exec ``cmd`` holding it (the slot stays held until
        the last process that inherited it ends, as with ``ci_slot.sh``). Returns only on
        a timeout (status 4)."""
        def stop(signum, _frame):
            sys.exit(128 + signum)  # runs the finally below: our ticket goes
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, stop)
        self.take_ticket(project, ' '.join(cmd))
        try:
            while True:
                fd = self.try_take(project, caps)
                if fd is not None:
                    print(f'ci-slot: holding {self.slots[self._index(fd)]}', file=sys.stderr, flush=True)
                    os.execvp(cmd[0], cmd)
                if deadline is not None and time.time() >= deadline:
                    print('ci-slot: queue timeout', file=sys.stderr)
                    return 4
                time.sleep(self.poll)
        finally:
            if self.ticket and os.path.exists(self.ticket):
                os.unlink(self.ticket)


def main(argv: list[str]) -> int:
    """``ci-slot [--lock PATH] [--slots N] [--queue DIR] --project P --default P
    [--cap P=N...] [--poll S] [--timeout S] CMD...``; the first three default to
    ``$LOCAL_CI_SLOT_LOCK``, ``$LOCAL_CI_SLOTS`` and ``$LOCAL_CI_SLOT_QUEUE``, as
    ``ci_slot.sh``'s do. ``--default`` names the project of ``ci_slot.sh``'s tickets
    (rule 4), whose cap is the slot count."""
    import argparse
    ap = argparse.ArgumentParser(prog='ci-slot')
    ap.add_argument('--lock', default=os.environ.get('LOCAL_CI_SLOT_LOCK'))
    ap.add_argument('--slots', type=int, default=int(os.environ.get('LOCAL_CI_SLOTS', '2')))
    ap.add_argument('--queue', default=os.environ.get('LOCAL_CI_SLOT_QUEUE'))
    ap.add_argument('--project', required=True)
    ap.add_argument('--default', required=True)
    ap.add_argument('--cap', action='append', default=[], help='P=N, a project cap')
    ap.add_argument('--poll', type=float, default=float(os.environ.get('LOCAL_CI_SLOT_POLL', '3')))
    ap.add_argument('--timeout', type=float)
    ap.add_argument('cmd', nargs=argparse.REMAINDER)
    a = ap.parse_args(argv)
    if not a.lock or not a.cmd:
        ap.error('a slot lock and a command are required')
    caps = {}
    for c in a.cap:
        p, _, n = c.partition('=')
        caps[p] = int(n)
    try:
        q = SlotQueue(a.lock, a.slots, a.queue, a.poll, default_project=a.default)
        return q.hold(a.project, caps, a.cmd, time.time() + a.timeout if a.timeout else None)
    except (ValueError, MissingSharedFile) as e:
        print(f'ci-slot: {e}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
