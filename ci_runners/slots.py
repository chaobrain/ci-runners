"""A host's CI slots and their fair queue (design §7, rules 1 and 2).

``grants`` and ``queue_full`` are pure; ``SlotQueue`` keeps the queue directory, the
tickets and the slot locks that the inventory's ``files`` name.
"""
from __future__ import annotations

import dataclasses
import errno
import fcntl
import os
import re
import time
from collections.abc import Mapping, Sequence


@dataclasses.dataclass(frozen=True)
class Ticket:
    seq: int  # the queue's number: lower is older
    project: str = ''  # empty: a ticket that names no project, which no cap admits


def grants(waiting: Sequence[Ticket], held: Mapping[str, int], caps: Mapping[str, int], free: int) -> list[int]:
    """The ``seq`` of the tickets that take the ``free`` slots now, in order.

    A ticket whose project already holds its cap (``caps``; a project not listed has
    cap 0) is skipped. Each
    free slot goes to the waiting project that holds the fewest slots, and among
    those to the oldest ticket. Pure.
    """
    held = {p: n for p, n in held.items()}
    queue = sorted(waiting, key=lambda t: t.seq)
    out: list[int] = []
    for _ in range(max(0, free)):
        best = None
        for t in queue:
            p = t.project
            if t.seq in out or held.get(p, 0) >= caps.get(p, 0):
                continue
            key = (held.get(p, 0), t.seq)
            if best is None or key < best[0]:
                best = (key, t)
        if best is None:
            break
        t = best[1]
        out.append(t.seq)
        p = t.project
        held[p] = held.get(p, 0) + 1
    return out


def queue_full(waiting: int, max_waiting: int) -> bool:
    """Whether a project's new part ends ``unavailable`` ("project queue full"): it
    already has ``max_waiting`` parts waiting on this pool (rule 2). Pure."""
    return waiting >= max_waiting


# ------------------------------------------------------------------ the queue on disk
#
# Slot 1 is ``<lock>``, slot i >= 2 is ``<stem>.<i>.lock``; the queue directory holds
# ``seq``, ``seq.lock`` and the tickets ``<seq, 12 digits>.wait``: ``<seq> <pid> <start
# epoch> <cwd> ci-slot project=<p> <command>``. A waiter tries the slots only when
# ``grants`` names its ticket, and takes one with a non-blocking ``flock``. A holder
# writes ``holder.<i>`` (``<pid> <start> <project>``) beside the tickets.

_TICKET = re.compile(r'([0-9]{12})\.wait\Z')
_TAG = re.compile(r'ci-slot project=([A-Za-z0-9_.-]+)(?:\s|\Z)')


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

    def __init__(self, lock: str, n: int, queue: str | None = None, poll: float = 3.0) -> None:
        if n < 1:
            raise ValueError('the slot count must be positive')
        self.slots = slot_paths(lock, n)
        stem = lock[:-len('.lock')] if lock.endswith('.lock') else lock
        self.queue = queue or stem + '.queue'
        self.poll = poll
        self.seq = 0
        self.ticket = ''

    # -------------------------------------------------------------- tickets

    def take_ticket(self, project: str, cmd: str) -> str:
        """Write our ticket after every queued one, under ``seq.lock``."""
        os.makedirs(self.queue, exist_ok=True)
        fd = os.open(os.path.join(self.queue, 'seq.lock'), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o660)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                with open(os.path.join(self.queue, 'seq')) as f:
                    n = int(f.read().strip() or 0)
            except (OSError, ValueError):
                n = 0
            n = max(n, self.seq)
            for name in os.listdir(self.queue):
                m = _TICKET.match(name)
                if m:
                    n = max(n, int(m.group(1)))
            self.seq = n + 1
            tmp = os.path.join(self.queue, f'seq.{os.getpid()}')
            with open(tmp, 'w') as f:
                f.write(f'{self.seq}\n')
            os.replace(tmp, os.path.join(self.queue, 'seq'))
            self.ticket = os.path.join(self.queue, f'{self.seq:012d}.wait')
            line = ' '.join([str(self.seq), str(os.getpid()), str(int(time.time())), os.getcwd(),
                             f'ci-slot project={project}', cmd.replace('\n', ' ')])
            with open(self.ticket + f'.{os.getpid()}', 'w') as f:
                f.write(line + '\n')
            os.replace(self.ticket + f'.{os.getpid()}', self.ticket)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        return self.ticket

    def waiting(self) -> list[Ticket]:
        """The live tickets; a dead or malformed one is deleted (ours is never deleted
        here)."""
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
                with open(path) as f:
                    text = f.read()
            except OSError:
                continue
            parts = text.split(maxsplit=3)
            ok = (len(parts) >= 3 and all(p.isdigit() for p in parts[:3]) and int(parts[0]) == int(m.group(1))
                  and int(parts[1]) > 0)
            if path != self.ticket and not (ok and alive(int(parts[1]), float(parts[2]))):
                try:
                    os.unlink(path)
                except OSError:
                    pass
                continue
            tag = _TAG.search(parts[3]) if len(parts) > 3 else None
            out.append(Ticket(int(m.group(1)), tag.group(1) if tag else ''))
        return out

    def held(self) -> tuple[dict[str, int], int, set[int]]:
        """``(slots held per project, free slots, indexes of free slots)``. A held slot
        without a live holder record counts under the key ''."""
        per: dict[str, int] = {}
        free = set()
        for i, path in enumerate(self.slots, 1):
            if not _flocked(path):
                free.add(i)
                continue
            project = ''
            try:
                with open(os.path.join(self.queue, f'holder.{i}')) as f:
                    pid, start, project = f.read().split()[:3]
                if not alive(int(pid), float(start)):
                    project = ''
            except (OSError, ValueError):
                project = ''
            per[project] = per.get(project, 0) + 1
        return per, len(free), free

    # -------------------------------------------------------------- taking a slot

    def try_take(self, project: str, caps: dict[str, int]) -> int | None:
        """One round: if ``grants`` names our ticket, try the free slots in order with a
        non-blocking flock. Returns the fd of the slot taken (inheritable), or None."""
        if not os.path.exists(self.ticket):  # deleted while we were stopped: re-queue
            self.take_ticket(project, 'requeued')
        per, nfree, _ = self.held()
        if self.seq not in grants(self.waiting(), per, caps, nfree):
            return None
        for i, path in enumerate(self.slots, 1):
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o660) if not os.path.exists(path) \
                else os.open(path, os.O_WRONLY | os.O_APPEND)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as e:
                os.close(fd)
                if e.errno in (errno.EAGAIN, errno.EACCES):
                    continue
                raise
            rec = os.path.join(self.queue, f'holder.{i}')
            with open(rec + f'.{os.getpid()}', 'w') as f:
                f.write(f'{os.getpid()} {_start_of(os.getpid()):.0f} {project}\n')
            os.replace(rec + f'.{os.getpid()}', rec)
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

