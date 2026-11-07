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


def grants(waiting: Sequence[Ticket], held: Mapping[str, int], caps: Mapping[str, int], free: int,
           default_project: str = '') -> list[int]:
    """The ``seq`` of the tickets that take the ``free`` slots now, in order.

    A ticket without a project counts as ``default_project`` (none: it gets no slot). A
    ticket whose project already holds its cap (``caps``; a project not listed has
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
    return out


def queue_full(waiting: int, max_waiting: int) -> bool:
    """Whether a project's new part ends ``unavailable`` ("project queue full"): it
    already has ``max_waiting`` parts waiting on this pool (rule 2). Pure."""
    return waiting >= max_waiting


# ------------------------------------------------------------------ the queue on disk
#
# The files are brainspike's ``ci_slot.sh``'s (D5): slot 1 is ``<lock>``, slot i >= 2
# is ``<stem>.<i>.lock``; the queue directory holds ``seq``, ``seq.lock`` and its
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

_TICKET = re.compile(r'([0-9]{12})\.(ticket|wait)\Z')
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

    def __init__(self, lock: str, n: int, queue: str | None = None, poll: float = 3.0,
                 default_project: str = '') -> None:
        if n < 1:
            raise ValueError('the slot count must be positive')
        self.slots = slot_paths(lock, n)
        stem = lock[:-len('.lock')] if lock.endswith('.lock') else lock
        self.queue = queue or stem + '.queue'
        self.poll = poll
        self.default = default_project  # the project of ci_slot.sh's tickets and holders (rule 4)
        self.seq = 0
        self.ticket = ''

    # -------------------------------------------------------------- tickets

    def take_ticket(self, project: str, cmd: str) -> str:
        """Write our ticket after every queued one, under ``seq.lock`` as ``ci_slot.sh`` does."""
        self._make_queue()
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
            out.append(Ticket(int(m.group(1)), tag.group(1) if tag and m.group(2) == 'wait' else ''))
        return out

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
        non-blocking flock. Returns the fd of the slot taken (inheritable), or None.

        The round holds ``take.lock``, so no other round reads the holders between our
        flock of a slot and our holder record: a slot held without a record counts for
        no project, and a project could then pass its cap."""
        if not os.path.exists(self.ticket):  # deleted while we were stopped: re-queue
            self.take_ticket(project, 'requeued')
        lk = os.open(os.path.join(self.queue, 'take.lock'), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o660)
        try:
            fcntl.flock(lk, fcntl.LOCK_EX)
            return self._take(project, caps)
        finally:
            os.close(lk)

    def _take(self, project: str, caps: dict[str, int]) -> int | None:
        per, nfree, _ = self.held()
        if self.default:
            per[self.default] = per.get(self.default, 0) + per.pop('', 0)
            caps = dict(caps, **{self.default: len(self.slots)})  # its ci_slot.sh waiters apply none (rule 4)
        if self.seq not in grants(self.waiting(), per, caps, nfree, self.default):
            return None
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
