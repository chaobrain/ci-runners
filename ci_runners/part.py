"""The executor (design §4.3) and ``salvage`` (§4.5): run one part on this host.

``ci-run part`` checks the inputs against the project registry, takes a CI slot and
GPU leases, and runs the command in its own systemd scope
(``ci-part-<runner>-<id>-<n>`` under ``ci-<project>.slice``, with ``MemoryMax``,
``MemorySwapMax=0``, nice 10, ionice best-effort 7 and OOM score +500). In the scope a
small supervisor runs the command, enforces the inner timeout and records the exit in
the part's record, so the record finishes even when the runner itself is stopped
mid-part (the scope is not in the runner's cgroup): the verdict's salvage then reads it.

Where the user manager has no memory controller (cgroup v1, design decision 11.11),
``MemoryMax`` is not enforced, so the supervisor enforces the part's memory itself: it
sums the scope's processes' ``RssAnon`` every ``CAP_POLL`` s and kills them over the
cap. ``OOMPolicy`` is passed on systemd 253 and later only (``OOM_POLICY_SYSTEMD``).

A part's record is ``state/parts/<owner>/<repo>/<run>-<run attempt>/<id>-<attempt>/``:
``record.json`` (inputs, start), ``exit.json`` (the supervisor's) and ``outcome.json``
(the outputs). The step exits 1 only for ``red``; 2 for an input the registry refuses.
"""
from __future__ import annotations

import argparse
import dataclasses
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence

from ci_runners import gpus as gpus_mod
from ci_runners import drain as drain_mod
from ci_runners import hostload
from ci_runners import envuse, inventory, locks, outcome, registry, slots
from ci_runners import p2p as p2p_mod
from ci_runners import sampler as sampler_mod
from ci_runners import slurmpart, slurmsite

ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}')
#: a part record of any host, ``<role>:<owner>/<repo>/<run>-<run attempt>/<id>-<attempt>``
#: (design §4.3 ``record``): a retry on another host takes its attempt from the name (13.3)
RECORD_REF = re.compile(r'[a-z0-9][a-z0-9-]{0,30}:[^/:]+/[^/:]+/[0-9]+-[0-9]+/'
                        r'(?P<id>[A-Za-z0-9][A-Za-z0-9_.-]{0,63})-(?P<n>[1-9][0-9]{0,2})')
SLOT_CLASSES = ('part', 'compile')  # the classes whose parts take one host CI slot (design §4.3, §7)
EVERY_SLOT = 'nightly'  # the class whose parts take every slot and every card lease (decision 12.8)
LOCKS = ('none', 's', 'x', 'l')  # the ``lock`` input: hold the host lock for the command, or not
#: what each class's ``part`` accepts (design §4.8): ``(least cards, most cards, locks,
#: longest timeout-minutes)``; most None: every card of the host
ROWS: dict[str, tuple[int, int | None, tuple[str, ...], int]] = {
    'light': (0, 0, ('none',), 30),
    'part': (0, 0, LOCKS, 240),
    'gpu': (1, 1, LOCKS, 240),
    'multi': (2, 2, LOCKS, 240),
    'compile': (0, 0, ('x',), 240),
    'nightly': (0, None, LOCKS, 600),
    # a target's max_cards and max_minutes bound it further (slurmpart.row, decision 14.2)
    'slurm': (0, None, ('none',), 24 * 60),
}
P2P_CLASSES = ('multi',)  # the classes whose parts run the P2P self-test first (decision 12.6)
P2P_KILL_WAIT = 5.0  # s the supervisor waits for the self-test after killing it at its bound
KILL_GRACE = 30  # s between TERM and KILL at the inner timeout
RELAY_DRAIN = 5  # s the relay may still copy once the command has ended
CAP_POLL = 2.0  # s between the supervisor's memory readings, where it enforces the cap
OOM_POLICY_SYSTEMD = 253  # the first systemd that takes OOMPolicy on a scope
SCOPE = re.compile(r'ci-part-[^/]*\.scope')  # a part's scope, the only cgroup the cap kills in
_LEASE_TICKET = re.compile(r'([0-9]{12})\.json\Z')  # a lease ticket in ``state/leases/queue``
#: the step's own files: the command must not write the part's outputs or the job's env
STEP_FILES = frozenset({'GITHUB_OUTPUT', 'GITHUB_STEP_SUMMARY', 'GITHUB_ENV', 'GITHUB_PATH', 'GITHUB_STATE'})
OUTPUTS = ('outcome', 'reason', 'next', 'retry-on', 'host', 'gpus', 'queue-seconds', 'run-seconds',
           'record', 'summary', 'attempt', 'resume')


class Refused(Exception):
    """An input the registry or the host refuses: exit 2, no outcome."""


# ------------------------------------------------------------------ small facts


def boot_id() -> str:
    try:
        with open('/proc/sys/kernel/random/boot_id') as f:
            return f.read().strip()
    except OSError:
        return ''


def _write_json(path: str, obj: object) -> None:
    tmp = f'{path}.{os.getpid()}.new'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(obj, f, indent=1, sort_keys=True)
        f.write('\n')
    os.replace(tmp, path)


def _read_json(path: str) -> dict | None:
    try:
        with open(path, encoding='utf-8') as f:
            v = json.load(f)
        return v if isinstance(v, dict) else None
    except (OSError, ValueError):
        return None


def unit_name(runner: str, part_id: str, run: str) -> str:
    """The scope's name, unique per run and attempt; ``job-completed`` stops
    ``ci-part-<runner>-*``. Pure."""
    safe = re.sub(r'[^A-Za-z0-9_.-]', '_', runner) or 'runner'
    return f'ci-part-{safe}-{part_id}-{run}.scope'


def scope_argv(unit: str, project: str, memory: str, inner: Sequence[str], oom_policy: bool = True) -> list[str]:
    """``systemd-run`` for the part's scope (design §4.3); ``oom_policy`` false on a
    systemd that refuses ``OOMPolicy`` on a scope. Pure."""
    policy = ['-p', 'OOMPolicy=continue'] if oom_policy else []
    return ['systemd-run', '--user', '--scope', '--quiet', f'--unit={unit}',
            f'--slice=ci-{project}.slice', '-p', f'MemoryMax={memory}', '-p', 'MemorySwapMax=0',
            *policy, *inner]


def systemd_major(version_text: str) -> int:
    """The major version in ``systemctl --version``'s first line; 0 if unread. Pure."""
    m = re.match(r'systemd (\d+)\b', version_text)
    return int(m.group(1)) if m else 0


def memory_delegated(proc_cgroup: str, read: Callable[[str], str]) -> bool:
    """Whether systemd enforces a user scope's ``MemoryMax`` here: the unified hierarchy
    only (``/proc/self/cgroup`` a single ``0::`` line), with ``memory`` in the user
    manager's ``cgroup.subtree_control``. ``read`` reads a file below ``/sys/fs/cgroup``;
    an unreadable one counts as no. Pure but for ``read``."""
    lines = proc_cgroup.strip().splitlines()
    if len(lines) != 1 or not lines[0].startswith('0::/'):
        return False
    parts = lines[0][3:].strip('/').split('/')
    top = next((i for i, x in enumerate(parts) if re.fullmatch(r'user@\d+\.service', x)), None)
    if top is None:
        return False
    try:
        return 'memory' in read('/' + '/'.join(parts[:top + 1]) + '/cgroup.subtree_control').split()
    except OSError:
        return False


def scope_procs_file(proc_cgroup: str) -> str:
    """The ``cgroup.procs`` of this process's part scope, or ``''`` outside a part scope.
    v1 and hybrid hosts list ``name=systemd``, which wins (a hybrid host's ``0::`` is
    mounted elsewhere); a unified host lists only ``0::``. Pure."""
    found = {}
    for line in proc_cgroup.splitlines():
        hid, _, rest = line.partition(':')
        ctl, _, path = rest.partition(':')
        if SCOPE.fullmatch(path.rsplit('/', 1)[-1]):
            if ctl == 'name=systemd':
                found['v1'] = f'/sys/fs/cgroup/systemd{path}/cgroup.procs'
            elif hid == '0' and ctl == '':
                found['v2'] = f'/sys/fs/cgroup{path}/cgroup.procs'
    return found.get('v1') or found.get('v2', '')


def rss_anon(status_text: str) -> int:
    """``RssAnon`` in bytes from a ``/proc/<pid>/status``; 0 without it. Pure."""
    m = re.search(r'^RssAnon:\s+(\d+) kB$', status_text, re.M)
    return int(m.group(1)) * 1024 if m else 0


def _descendants(root: int) -> set[int]:
    """``root`` and its descendants, from ``/proc`` (the cap's members outside a scope)."""
    parent: dict[int, int] = {}
    for d in os.listdir('/proc'):
        if d.isdigit():
            try:
                with open(f'/proc/{d}/stat') as f:
                    parent[int(d)] = int(f.read().rsplit(')', 1)[1].split()[1])
            except (OSError, IndexError, ValueError):
                continue
    out, todo = {root}, [root]
    while todo:
        p = todo.pop()
        for c, pp in parent.items():
            if pp == p and c not in out:
                out.add(c)
                todo.append(c)
    return out


class MemoryCap:
    """The supervisor's own memory cap (design decision 11.11): the members are the part
    scope's processes (its ``cgroup.procs``), or outside a scope the command and its
    descendants; all but the supervisor count and are killed."""

    def __init__(self, cap: int, child: int, procs_file: str) -> None:
        self.cap, self.child, self.procs_file, self.me = cap, child, procs_file, os.getpid()
        self.killed = False

    def members(self) -> set[int]:
        if self.procs_file:
            try:
                with open(self.procs_file) as f:
                    pids = {int(x) for x in f.read().split()}
            except (OSError, ValueError):
                pids = set()
        else:
            pids = _descendants(self.child)
        pids.discard(self.me)
        return pids

    def used(self, pids: set[int]) -> int:
        total = 0
        for p in pids:
            try:
                with open(f'/proc/{p}/status') as f:
                    total += rss_anon(f.read())
            except OSError:
                continue
        return total

    def check(self) -> bool:
        """Kill the members if they hold more than the cap; True once they have been."""
        pids = self.members()
        if not self.killed and self.used(pids) > self.cap:
            self.killed = True
            for p in pids:
                try:
                    os.kill(p, signal.SIGKILL)
                except OSError:
                    pass
        return self.killed


def command_argv(command: str, *, cpus: str = '', nice: int = 10) -> list[str]:
    """What the supervisor runs: ``taskset`` to the inventory's ``cpus`` where it gives
    them (``compile_cpus`` on ``compile``, design §5.5), nice (0 on
    ``compile``) and ionice, then ``bash -c``. Pure."""
    argv = ['taskset', '-c', cpus] if cpus else []
    argv += ['nice', '-n', str(nice), 'ionice', '-c', '2', '-n', '7']
    return argv + ['bash', '-c', command]


def part_env(base: Mapping[str, str], *, role: str, project: registry.Project, part_dir: str, results: str,
             cards: Sequence[int], cpus: str, files: Mapping[str, str], slot: str, resume: bool,
             held: str = '') -> dict[str, str]:
    """The command's environment (design §4.3): the job's without our ``CI_*`` names,
    then the project's variables, then ours, which nothing overrides. On a host with the
    host lock its path (``CI_HOST_LOCK``, and ``LOCAL_CI_HOST_LOCK`` for brainspike's
    ``host_lock.sh``), and ``held``, the mode the part holds it in (``s`` or ``x``), as
    the protocol's cover ``BRAINSPIKE_HOST_LOCK`` (decision 12.9). Pure."""
    env = {k: v for k, v in base.items() if not k.startswith('CI_') and k not in STEP_FILES
           and k not in (locks.ENV, 'LOCAL_CI_HOST_LOCK')}
    env.update(project.variables)
    if project.env:
        env['PATH'] = f'{project.env}/bin:' + env.get('PATH', '/usr/bin:/bin')
        env['CONDA_PREFIX'] = project.env
    env.update({
        'CI_RUNNERS_HOST': role, 'CI_PROJECT': project.name, 'CI_PART_DIR': part_dir, 'CI_RESULTS_DIR': results,
        'CUDA_VISIBLE_DEVICES': ','.join(str(c) for c in cards), 'CUDA_DEVICE_ORDER': 'PCI_BUS_ID',
        'CI_CPUS': cpus, 'CI_SLOT_LOCK': files['slot_lock'],
        'CI_SLOT_QUEUE': files['slot_queue'], 'CI_SLOT_HELD': slot, 'CI_RESUME': '1' if resume else '0',
    })
    if files.get('host_lock'):
        env['CI_HOST_LOCK'] = env['LOCAL_CI_HOST_LOCK'] = files['host_lock']
        if held:
            env[locks.ENV] = held
    return env


def class_refusal(cls: str, *, gpus: int, lock: str, cards: int, host_lock: bool) -> str:
    """Why a part's ``gpus`` or ``lock`` is outside its class's row of design §4.8, or
    ``''``. ``cards`` is the host's card count; ``host_lock`` whether the host has the
    host lock (inventory ``files.host_lock``). Pure."""
    lo, hi, locks, _ = ROWS[cls]
    hi = cards if hi is None else hi
    if not lo <= gpus <= hi:
        want = 'no card' if hi == 0 else (f'{lo} card{"s" if lo > 1 else ""}' if lo == hi else f'0 to {hi} cards')
        return f'gpus {gpus}: the {cls} runners take {want} (design §4.8)'
    if lock not in locks:
        return f'lock {lock}: the {cls} runners take lock {" or ".join(locks)} (design §4.8)'
    if lock != 'none' and not host_lock:
        return f'lock {lock}: this host has no host lock'
    return ''


# ------------------------------------------------------------------ the supervisor (in the scope)


def _relay(src: int, dst: int) -> None:
    """Copy ``src`` to ``dst`` until EOF; once ``dst`` fails, keep draining ``src``."""
    alive = True
    while True:
        try:
            chunk = os.read(src, 65536)
        except OSError:
            return
        if not chunk:
            return
        while alive and chunk:
            try:
                chunk = chunk[os.write(dst, chunk):]
            except OSError:  # the job's end closed: EPIPE (the supervisor ignores SIGPIPE)
                alive = False


def p2p_argv(release: str) -> list[str]:
    """The P2P self-test as the supervisor runs it: ``ci_runners.p2p.main`` of the
    release, its result the exit status. Pure."""
    return [sys.executable, '-I', '-c', 'import sys; sys.path.insert(0, sys.argv.pop(1)); '
            'from ci_runners.p2p import main; sys.exit(main())', release]


def run_p2p(argv: Sequence[str], bound: float) -> str:
    """Run the P2P self-test ``argv`` (``ci_runners.p2p``) in a session of its own, at
    most ``bound`` s. Returns ``''`` on a pass, else the reason: its last line, its exit,
    or the bound (decision 12.6). The bound is hard: after the kill at ``bound`` the
    supervisor waits ``P2P_KILL_WAIT`` s more, at most, for the test to go (a process
    stuck in the driver does not die of SIGKILL until its call returns, and one that
    holds the pipe keeps it open); then it gives up on it and the scope's stop reaps it."""
    try:
        child = subprocess.Popen(list(argv), start_new_session=True, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    except OSError as e:
        return f'the self-test did not start: {type(e).__name__}'
    try:
        text, _ = child.communicate(timeout=bound)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            child.communicate(timeout=P2P_KILL_WAIT)
        except subprocess.TimeoutExpired:  # give it up: the pipe closed, reaped if it went
            child.stdout.close()
            child.poll()
        return f'the self-test ran over its {bound:g} s bound'
    lines = text.decode('utf-8', 'replace').strip().splitlines()
    last = re.sub(r'[^\x20-\x7e]', '?', lines[-1])[:200] if lines else ''
    sys.stdout.write(f'ci-runners: {last or "p2p: no output"}\n')
    sys.stdout.flush()
    if child.returncode == 0:
        return ''
    return last.removeprefix('p2p: ') or f'the self-test exited {child.returncode}'


def supervise(record: str, timeout: float, argv: Sequence[str], cap: int = 0, p2p: Sequence[str] = ()) -> int:
    """Run ``argv`` in its own session with OOM score +500, end it at ``timeout``, and
    write ``exit.json``. Runs inside the part's scope, in a session of its own, so the
    job's signals (a cancel, a stopped runner) do not reach it: only a stop of the scope
    does (job-completed, the reaper). It keeps the slot and lease fds it inherited for
    the part's life; the command does not get them.

    The command's output goes through a pipe this supervisor relays to its own: when the
    job's end of it closes (a stopped runner), the relay drops the rest and the command
    runs on, instead of dying of SIGPIPE at its next write.

    ``cap`` > 0 (bytes): enforce the part's memory here, every ``CAP_POLL`` s
    (``MemoryCap``), and count a kill over it as an OOM kill.

    ``p2p``: the P2P self-test, run first, in the scope and with the part's environment
    (decision 12.6). If it does not pass, the command does not run and ``exit.json``
    holds its reason under ``p2p``."""
    try:
        os.setsid()
    except OSError:
        pass  # already a session leader
    child: subprocess.Popen | None = None
    signalled = []

    def stop(signum, _frame):
        signalled.append(signum)
        if child is not None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except OSError:
                pass
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, stop)

    def adj():
        with open('/proc/self/oom_score_adj', 'w') as f:
            f.write('500')
    timed_out = False
    if p2p:
        why = run_p2p(p2p, p2p_mod.BOUND)
        if why or signalled:
            _write_json(os.path.join(record, 'exit.json'), {'rc': None, 'p2p': why, 'timed_out': False, 'oom_kills': 0,
                                                             'signalled': bool(signalled), 'end_epoch': time.time(),
                                                             'boot_end': boot_id()})
            return 0
    child = subprocess.Popen(list(argv), start_new_session=True, preexec_fn=adj, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT)
    relay = threading.Thread(target=_relay, args=(child.stdout.fileno(), sys.stdout.fileno()), daemon=True)
    relay.start()
    _write_json(os.path.join(record, 'scope.json'), {'supervisor': os.getpid(), 'child': child.pid})
    mcap = None
    if cap > 0:
        try:
            with open('/proc/self/cgroup') as f:
                procs_file = scope_procs_file(f.read())
        except OSError:
            procs_file = ''
        mcap = MemoryCap(cap, child.pid, procs_file)
    deadline = time.monotonic() + timeout
    while True:
        left = max(0.0, deadline - time.monotonic())
        try:
            rc = child.wait(timeout=min(left, CAP_POLL) if mcap else left)
            break
        except subprocess.TimeoutExpired:
            if mcap is not None and time.monotonic() < deadline:
                mcap.check()
                continue
            if timed_out:  # TERM did not end it within the grace: KILL
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except OSError:
                    pass
                deadline = time.monotonic() + 3600
                continue
            timed_out = True
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except OSError:
                pass
            deadline = time.monotonic() + KILL_GRACE
    relay.join(timeout=RELAY_DRAIN)  # a background process the command left may hold the pipe
    oom_kills = 0
    try:
        with open('/proc/self/cgroup') as f:
            cg = f.read().strip().split('::', 1)[-1]
        with open(f'/sys/fs/cgroup{cg}/memory.events') as f:
            oom_kills = outcome.memory_oom_kills(f.read())
    except (OSError, ValueError):
        pass
    if mcap is not None and mcap.killed:
        oom_kills += 1
    _write_json(os.path.join(record, 'exit.json'), {'rc': rc, 'timed_out': timed_out, 'oom_kills': oom_kills,
                                                     'signalled': bool(signalled), 'end_epoch': time.time(),
                                                     'boot_end': boot_id()})
    return rc if rc >= 0 else 128 - rc


# ------------------------------------------------------------------ leases


class Leases:
    """GPU leases: per card ``max_parts`` lock files under ``state/leases``, flock-held by
    the executor for the part's life. A part waits with a ticket in ``queue/``
    (``<seq>.json``: its pid and start, and what it asks for), and the cards go out in
    ticket order per card (``gpus.lease_grants``, decision 12.6). Tickets, choosing and
    taking happen under ``.lock``."""

    def __init__(self, directory: str, max_parts: int = 1) -> None:
        self.dir, self.max_parts = directory, max_parts
        self.queue = os.path.join(directory, 'queue')
        os.makedirs(self.queue, mode=0o700, exist_ok=True)
        self.fds: list[int] = []
        self.ticket = ''
        self.seq = 0

    def _path(self, card: int, k: int) -> str:
        return os.path.join(self.dir, f'card{card}.{k}.lock')

    def leased(self, cards: Sequence[int]) -> dict[int, int]:
        return {c: sum(slots._flocked(self._path(c, k)) for k in range(self.max_parts)) for c in cards}

    def _enqueue(self, n: int, cards: Sequence[int], need_mib: int, every: bool) -> None:
        """Our ticket, after every other one (under ``.lock``)."""
        seqs = [int(m.group(1)) for m in map(_LEASE_TICKET.match, os.listdir(self.queue)) if m]
        try:
            with open(os.path.join(self.queue, 'seq')) as f:
                last = int(f.read().strip() or 0)
        except (OSError, ValueError):
            last = 0
        self.seq = max([last, *seqs]) + 1
        with open(os.path.join(self.queue, f'seq.{os.getpid()}'), 'w') as f:
            f.write(f'{self.seq}\n')
        os.replace(os.path.join(self.queue, f'seq.{os.getpid()}'), os.path.join(self.queue, 'seq'))
        self.ticket = os.path.join(self.queue, f'{self.seq:012d}.json')
        _write_json(self.ticket, {'pid': os.getpid(), 'start': round(slots._start_of(os.getpid())), 'n': n,
                                  'cards': list(cards), 'need_mib': need_mib, 'every': every})

    def tickets(self) -> list[gpus_mod.LeaseTicket]:
        """The live tickets; a dead or malformed one is deleted (ours never is)."""
        out = []
        for name in sorted(os.listdir(self.queue)):
            m = _LEASE_TICKET.match(name)
            if not m:
                continue
            path = os.path.join(self.queue, name)
            t = _read_json(path) or {}
            try:
                ok = (type(t['pid']) is int and t['pid'] > 0 and type(t['n']) is int and t['n'] >= 0
                      and all(type(c) is int for c in t['cards']) and type(t['need_mib']) is int
                      and type(t['every']) is bool and slots.alive(t['pid'], float(t['start'])))
            except (KeyError, TypeError, ValueError):
                ok = False
            if not ok and path != self.ticket:
                try:
                    os.unlink(path)
                except OSError:
                    pass
                continue
            if ok:
                out.append(gpus_mod.LeaseTicket(int(m.group(1)), t['n'], tuple(t['cards']), t['need_mib'], t['every']))
        return out

    def try_take(self, n: int, cards: Sequence[int], free: Mapping[int, int], need_mib: int,
                 every: bool = False) -> list[int] | None:
        """One round: queue a ticket for ``n`` of ``cards`` on the first, then take the
        cards ``lease_grants`` gives it, if any; ``every`` takes every lease of each card.
        Returns the cards taken, or None (the ticket stays queued until ``release``)."""
        mutex = os.open(os.path.join(self.dir, '.lock'), os.O_WRONLY | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(mutex, fcntl.LOCK_EX)
            if not self.ticket or not os.path.exists(self.ticket):
                self._enqueue(n, cards, need_mib, every)
            waiting = self.tickets()
            asked = sorted({c for t in waiting for c in t.cards})
            chosen = gpus_mod.lease_grants(waiting, free=free, leased=self.leased(asked),
                                           max_parts=self.max_parts).get(self.seq)
            if chosen is None:
                return None
            for c in chosen:
                for k in range(self.max_parts):
                    fd = os.open(self._path(c, k), os.O_WRONLY | os.O_CREAT | os.O_CLOEXEC, 0o600)
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except OSError:
                        os.close(fd)
                        continue
                    self.fds.append(fd)
                    if not every:
                        break
            self._drop()
            return chosen
        finally:
            os.close(mutex)

    def _drop(self) -> None:
        if self.ticket:
            try:
                os.unlink(self.ticket)
            except OSError:
                pass
            self.ticket = ''

    def release(self) -> None:
        for fd in self.fds:
            os.close(fd)
        self.fds = []
        self._drop()


def smi_free(run: Callable = subprocess.run) -> dict[int, int]:
    try:
        r = run(['nvidia-smi', '--query-gpu=index,memory.total,memory.used', '--format=csv,noheader,nounits'],
                capture_output=True, text=True, timeout=30)
        return gpus_mod.gpu_free_mib(r.stdout) if r.returncode == 0 else {}
    except (OSError, subprocess.SubprocessError):
        return {}


def _smi(argv: Sequence[str], run: Callable) -> str:
    try:
        r = run(['nvidia-smi', *argv], capture_output=True, text=True, timeout=30)
        return r.stdout if r.returncode == 0 else ''
    except (OSError, subprocess.SubprocessError):
        return ''


def smi_status(run: Callable = subprocess.run) -> dict[int, tuple[str, float, float]]:
    return gpus_mod.gpu_status(_smi(['--query-gpu=index,pci.bus_id,temperature.gpu,utilization.gpu',
                                     '--format=csv,noheader,nounits'], run))


def smi_apps(run: Callable = subprocess.run) -> list[tuple[int, str]]:
    return gpus_mod.compute_apps(_smi(['--query-compute-apps=pid,gpu_bus_id', '--format=csv,noheader'], run))


#: why a first attempt goes to the other host while a nightly part has this host (decision 13.6)
NIGHTLY_BUSY = 'busy: nightly holds every slot and card'


def every_path(data: str) -> str:
    """The hold record of the ``nightly`` part that holds, or waits for, every slot and
    card of this host (decision 13.6): ``state/every.json``."""
    return os.path.join(data, 'ci-runners', 'state', 'every.json')


def every_hold(data: str, pids: Sequence[int], record: str) -> None:
    """Write the hold record: the processes that keep the hold (the executor, then the
    supervisor too, which outlives a stopped executor), by PID and start, and the part's
    record. It only places other parts, so a failed write is ignored (fail open)."""
    try:
        _write_json(every_path(data), {'procs': [[p, round(slots._start_of(p))] for p in pids],
                                       'record': record_ref(record), 'since': round(time.time())})
    except OSError:
        pass


def every_holder(data: str) -> dict | None:
    """The hold record while one of its processes runs, else None: a record whose
    processes are all gone holds nothing (it fails open, and the part waits as before)."""
    rec = _read_json(every_path(data))
    for p in (rec or {}).get('procs') or ():
        try:
            pid, start = int(p[0]), float(p[1])
        except (TypeError, ValueError, IndexError):
            continue
        if pid > 0 and slots.alive(pid, start):
            return rec
    return None


def every_drop(data: str) -> None:
    """Delete the hold record if this process wrote it."""
    rec = _read_json(every_path(data)) or {}
    procs = rec.get('procs') or [[0]]
    if isinstance(procs, list) and procs and isinstance(procs[0], list) and procs[0][:1] == [os.getpid()]:
        try:
            os.unlink(every_path(data))
        except OSError:
            pass


def per_card(host: Mapping[str, object]) -> bool:
    """Whether a host has runners per card (a pool of class ``gpu``, design §5.1): the
    health gate refuses there instead of waiting, a per-card runner leases its own card, a
    thermal retry names a cool card's ``<role>-gpu<M>``, and the early stop is on (design
    §5.5). Pure."""
    pools = host.get('pools')
    return isinstance(pools, Mapping) and any(inventory.pool_of(host, c) is not None
                                              for c in sorted(inventory.PER_CARD_CLASSES))


# ------------------------------------------------------------------ outputs


def write_outputs(out: Mapping[str, object], github_output: str) -> None:
    if not github_output:
        return
    with open(github_output, 'a', encoding='utf-8') as f:
        for k in OUTPUTS:
            v = out.get(k, '')
            v = json.dumps(v) if isinstance(v, (list, dict)) else ('true' if v is True else 'false' if v is False
                                                                     else str(v))
            if '\n' in v:
                raise ValueError(f'output {k} spans lines')
            f.write(f'{k}={v}\n')


def slurm_record(record: str, rec: dict[str, object]) -> dict[str, object]:
    """``rec`` with a slurm part's ``slurm`` block (``slurm.json``, decision 14.4): the
    site, account, target, job, node, state, elapsed, cards, CPUs and cost; its run
    starts when its job does."""
    blk = _read_json(os.path.join(record, 'slurm.json'))
    if blk is not None:
        rec['slurm'] = blk
        if isinstance(blk.get('run_epoch'), (int, float)):
            rec['run_epoch'] = blk['run_epoch']
            rec['queue_seconds'] = round(max(0.0, float(blk['run_epoch']) - float(rec.get('start_epoch', 0))))
    return rec


def slurm_line(rec: Mapping[str, object]) -> str:
    """A slurm part's summary line: ``<driver> → <site>:<target>``, its job, node and
    cost (decision 14.4); empty for any other part. Pure."""
    b = rec.get('slurm')
    if not isinstance(b, dict) or not b.get('target'):
        return ''
    line = f'{rec.get("host", "")} → {b.get("site")}:{b.get("target")} ({b.get("account")})'
    if b.get('job'):
        line += f', job {b["job"]}' + (f' on {b["node"]}' if b.get('node') else '')
    if isinstance(b.get('cost'), (int, float)):
        line += f', {b.get("elapsed", 0)} s, cost {b["cost"]:.3f}'
    return line


def summary_md(out: Mapping[str, object], part_id: str) -> str:
    rows = [('outcome', f'{out["outcome"]} ({out["reason"]})'), ('next', out['next']), ('host', out['host']),
            ('cards', out['gpus'] or 'none'), ('queue', f'{out["queue-seconds"]} s'), ('run', f'{out["run-seconds"]} s'),
            ('attempt', out['attempt'])]
    if out.get('peak'):
        rows.append(('peak temperature', out['peak']))
    if out.get('slurm'):
        rows.append(('cluster', out['slurm']))
    return f'### part `{part_id}`\n\n| | |\n|---|---|\n' + ''.join(f'| {k} | {v} |\n' for k, v in rows)


def finish(record: str, rec: Mapping[str, object], ex: Mapping[str, object] | None, *, stopped: bool = False,
           unavailable: str = '', unit_result: str = '', cool_card: int | None = None,
           retry_on: Sequence[str] | None = None, nowhere: bool = False) -> dict[str, object]:
    """The outputs of a part from its record and exit (design §4.4). ``cool_card`` is a
    card of this per-card host that passes the health gate, for a thermal retry;
    ``retry_on`` overrides the labels of a retry or fallback; ``nowhere`` marks a part
    refused here with no other host to go to (decision 13.3): ``next`` ``none``. Pure but
    for the clock when the exit has no end time. A failed P2P self-test in the exit
    (``p2p``) ends the part ``unavailable`` (``p2p: <why>``) with ``next`` ``none``
    (decision 12.6)."""
    end = float((ex or {}).get('end_epoch') or time.time())
    p2p = str((ex or {}).get('p2p') or '')
    unavailable = unavailable or str((ex or {}).get('unavailable') or '') or (f'p2p: {p2p}' if p2p else '')
    ev = outcome.Evidence(
        rc=None if ex is None else ex.get('rc'), timed_out=bool(ex and ex.get('timed_out')),
        stopped=stopped or bool(ex and ex.get('signalled') and not ex.get('timed_out')),
        unavailable=unavailable, boot_start=str(rec.get('boot_start', '')),
        boot_end=str((ex or {}).get('boot_end', '')),
        oom_kills=int((ex or {}).get('oom_kills', 0)) + (1 if unit_result == 'oom-kill' else 0),
        early_stop=bool(rec.get('early_stop')))
    o = outcome.classify(ev)
    if o.kind == 'lost' and ex and ex.get('red'):  # a slurm job's time limit (decision 14.4)
        o = outcome.Outcome('red', str(ex['red']))
    attempt = int(rec.get('attempt', 1))
    h = outcome.hint(o.kind, attempt=attempt, retries=int(rec.get('retries', 1)), cool_card=cool_card is not None,
                     host_fault=bool(p2p) or nowhere, retry_here=bool(ex and ex.get('retry_here')))
    cls, role = str(rec.get('class', '')), str(rec.get('host', ''))
    own = [str(x) for x in rec.get('pool_labels') or ()]  # the pool's labels name its host already (RUN-5)
    if h.next != 'none' and retry_on is not None:
        retry_on = list(retry_on)
    elif h.next == 'retry' and h.where == 'cool-card':
        retry_on = ['self-hosted', f'{role}-gpu{cool_card}']  # setup labels a card's runner <role>-gpu<N>
    elif h.next == 'retry':
        retry_on = ['self-hosted', *own] if own else ['self-hosted', cls, role] if h.where == 'same-host' \
            else ['self-hosted', cls]
    elif h.next == 'fallback':
        retry_on = list(rec.get('fallback') or ['self-hosted', *(own or [cls])])
    else:
        retry_on = []
    peak = rec.get('peak_c') or {}
    run_seconds = int(max(0.0, end - float(rec['run_epoch']))) if rec.get('run_epoch') else 0
    return {'outcome': o.kind, 'reason': o.reason, 'next': h.next, 'retry-on': retry_on, 'host': role,
            'gpus': ','.join(str(c) for c in rec.get('cards', [])), 'queue-seconds': int(rec.get('queue_seconds', 0)),
            'run-seconds': run_seconds,
            'record': f'{role}:{record_ref(record)}', 'summary': f'{record_ref(record)}/summary.md', 'attempt': attempt,
            'run-attempt': record_run_attempt(record),
            'resume': h.resume, 'peak': ', '.join(f'card {c} {t:g} C' for c, t in sorted(peak.items()))
            if isinstance(peak, dict) else '', 'slurm': slurm_line(rec)}


def _publish(record: str, out: dict[str, object], part_id: str, env: Mapping[str, str]) -> None:
    with open(os.path.join(record, 'summary.md'), 'w', encoding='utf-8') as f:
        f.write(summary_md(out, part_id))
    _write_json(os.path.join(record, 'outcome.json'), out)
    write_outputs(out, env.get('GITHUB_OUTPUT', ''))
    if env.get('RUNNER_TEMP'):  # what the part action uploads as ci-part-<id>-<attempt>
        art = os.path.join(env['RUNNER_TEMP'], 'ci-runners', part_id)
        os.makedirs(art, exist_ok=True)
        for n in ('outcome.json', 'summary.md'):
            shutil.copyfile(os.path.join(record, n), os.path.join(art, n))
    if env.get('GITHUB_STEP_SUMMARY'):
        with open(env['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as f:
            f.write(summary_md(out, part_id))


# ------------------------------------------------------------------ the executor


@dataclasses.dataclass
class Inputs:
    project: str
    command: str
    id: str
    timeout_minutes: float
    gpus: int = 0
    gpu_mem_mib: int = 0
    memory: str = ''
    queue_timeout_minutes: float = 60
    results: str = ''
    resume: bool = False
    attempt_of: str = ''
    lock: str = ''  # empty: the class's default, ``x`` on ``compile``, else ``none`` (design §4.3)
    target: str = ''  # a slurm part's targets, in the order to try (decision 14.2)


def parse_args(argv: Sequence[str]) -> Inputs:
    p = argparse.ArgumentParser(prog='ci-run part')
    p.add_argument('--project', required=True)
    p.add_argument('--command', required=True)
    p.add_argument('--id', required=True)
    p.add_argument('--timeout-minutes', type=float, required=True)
    p.add_argument('--gpus', type=int, default=0)
    p.add_argument('--gpu-mem-mib', type=int, default=0)
    p.add_argument('--memory', default='')
    p.add_argument('--queue-timeout-minutes', type=float, default=60)
    p.add_argument('--results', default='')
    p.add_argument('--resume', choices=('true', 'false', ''), default='false')
    p.add_argument('--attempt-of', default='')
    p.add_argument('--lock', choices=('', *LOCKS), default='')
    p.add_argument('--target', default='')
    a = p.parse_args(list(argv))
    return Inputs(a.project, a.command, a.id, a.timeout_minutes, a.gpus, a.gpu_mem_mib, a.memory,
                  a.queue_timeout_minutes, a.results, a.resume == 'true', a.attempt_of, a.lock, a.target)


class Executor:
    """One part. The I/O is injectable: ``run`` (subprocess.run), ``spawn``
    (subprocess.Popen) and ``scope`` (False runs the supervisor without systemd, for tests);
    for the GPU sampler, ``procs`` (the PIDs in the part's scope), ``stop_part`` (the early
    stop), ``clock`` and ``sample_s``; ``p2p``, the P2P self-test's argv."""

    def __init__(self, inv: inventory.Inventory, etc: str, env: Mapping[str, str], *, release: str,
                 run: Callable = subprocess.run, spawn: Callable = subprocess.Popen, scope: bool = True,
                 poll: float = 3.0, procs: Callable[[], set[int]] | None = None,
                 stop_part: Callable[[], None] | None = None, clock: Callable[[], float] = time.time,
                 sample_s: float = sampler_mod.SAMPLE_S, own_cap: bool | None = None,
                 p2p: Sequence[str] | None = None, slurm_tools: Mapping[str, str] | None = None,
                 slurm_env: Mapping[str, str] | None = None) -> None:
        self.inv, self.etc, self.env, self.release = inv, etc, dict(env), release
        # a slurm part's ssh and rsync, and what its driver's environment adds (the tests' fakes)
        self.slurm_tools = dict(slurm_tools or {'ssh': '/usr/bin/ssh', 'rsync': '/usr/bin/rsync'})
        self.slurm_env = dict(slurm_env or {})
        # the P2P self-test of a multi part (decision 12.6); a fixture in the tests
        self.p2p = list(p2p) if p2p is not None else p2p_argv(release)
        self.own_cap = own_cap  # None: the supervisor caps memory where systemd does not (11.11)
        self.run, self.spawn, self.scope, self.poll = run, spawn, scope, poll
        self.procs, self.stop_part, self.clock, self.sample_s = procs, stop_part, clock, sample_s
        self.stopped = False
        self.proc: subprocess.Popen | None = None
        self.unit = ''
        self.record = ''

    def caps_memory(self) -> bool:
        """Whether the supervisor enforces the memory cap: where systemd would not."""
        if self.own_cap is not None:
            return self.own_cap
        def read(path: str) -> str:
            with open('/sys/fs/cgroup' + path) as f:
                return f.read()
        try:
            with open('/proc/self/cgroup') as f:
                return not memory_delegated(f.read(), read)
        except OSError:
            return True

    def oom_policy(self) -> bool:
        """Whether this systemd takes ``OOMPolicy`` on a scope."""
        try:
            r = self.run(['systemctl', '--version'], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return False
        return systemd_major(r.stdout or '') >= OOM_POLICY_SYSTEMD

    # -- checks before anything is taken

    def check(self, a: Inputs) -> tuple[registry.Project, str]:
        if not ID.fullmatch(a.id):
            raise Refused(f'id must match {ID.pattern}')
        try:
            proj = registry.load_file(self.etc, a.project, self.inv.this)
        except registry.RegistryError as e:
            raise Refused(f'project {a.project}: {e}') from None
        if self.env.get('GITHUB_REPOSITORY', '').lower() != proj.repository:
            raise Refused(f'project {a.project} belongs to another repository')
        cls = self.env.get('CI_RUNNER_CLASS', '')
        if not cls:
            raise Refused('CI_RUNNER_CLASS is not set: not one of our runners')
        if cls not in ROWS:
            raise Refused(f'CI_RUNNER_CLASS {cls!r} is no runner class')
        memory = a.memory or proj.memory or proj.memory_max
        try:
            if registry.memory_bytes(memory) > registry.memory_bytes(proj.memory_max):
                raise Refused(f'memory {memory} is above the project\'s {proj.memory_max} here')
        except registry.RegistryError as e:
            raise Refused(str(e)) from None
        if a.gpu_mem_mib < 0 or a.queue_timeout_minutes < 0:
            raise Refused('gpu-mem-mib and queue-timeout-minutes must not be negative')
        if cls != slurmpart.CLASS and not 0 <= a.gpus <= len(self.inv.host['gpus']):
            raise Refused(f'gpus must be in [0, {len(self.inv.host["gpus"])}] here')
        if not 0 < a.timeout_minutes <= ROWS[cls][3]:
            raise Refused(f'timeout-minutes must be above 0 and at most {ROWS[cls][3]} on the {cls} runners')
        if a.lock not in ('', *LOCKS):
            raise Refused(f'lock must be one of {", ".join(LOCKS)}')
        card = self.env.get('CI_RUNNER_CARD', '')
        if card and (not card.isdigit() or int(card) not in self.inv.host['gpus']):
            raise Refused('CI_RUNNER_CARD names no card of this host')
        return proj, memory

    def candidates(self) -> list[int]:
        """The cards this runner may lease: its own card on a per-card host, where the
        runner of card N sets ``CI_RUNNER_CARD=N`` (design §5.1); else every card."""
        card = self.env.get('CI_RUNNER_CARD', '')
        if card and per_card(self.inv.host):
            return [int(card)]
        return list(self.inv.host['gpus'])

    def gate(self, a: Inputs, cls: str, lock: str) -> str:
        """Why a part cannot start on this host before anything is taken, or empty: its
        ``gpus`` or ``lock`` outside its class's row (design §4.8), or a ``compile`` part on
        a host without ``compile_cpus``."""
        h = self.inv.host
        why = class_refusal(cls, gpus=a.gpus, lock=lock, cards=len(h['gpus']), host_lock=bool(h['files'].get('host_lock')))
        if why:
            return why
        if cls == 'compile' and not h.get('compile_cpus'):
            return 'this host has no compile_cpus for compile parts'
        return ''

    def cool_elsewhere(self, exclude: Sequence[int]) -> int | None:
        """The coolest card of this per-card host outside ``exclude`` that passes the
        health gate, or None (and None on any other host)."""
        h = self.inv.host
        if not per_card(h):
            return None
        cool = gpus_mod.cool_cards([c for c in h['gpus'] if c not in exclude], smi_status(self.run), h['max_temp_c'])
        return cool[0] if cool else None

    # -- the sampler's defaults

    def _scope_procs(self) -> set[int]:
        """The PIDs in the part's scope (its cgroup's ``cgroup.procs``)."""
        if not self.unit:
            return set()
        r = self.run(['systemctl', '--user', 'show', '-p', 'ControlGroup', '--value', self.unit],
                     capture_output=True, text=True, timeout=30)
        cg = (r.stdout or '').strip()
        if not cg.startswith('/'):
            return set()
        with open(f'/sys/fs/cgroup{cg}/cgroup.procs') as f:
            return {int(x) for x in f.read().split() if x.isdigit()}

    def _stop_scope(self) -> None:
        """The early stop: stop the scope (its cgroup dies with it); without systemd, kill
        the command's process group, whose PID the supervisor recorded."""
        if self.unit:
            self.run(['systemctl', '--user', 'stop', self.unit], capture_output=True, timeout=90)
            return
        sc = _read_json(os.path.join(self.record, 'scope.json')) or {}
        if isinstance(sc.get('child'), int) and sc['child'] > 1:
            try:
                os.killpg(sc['child'], signal.SIGKILL)
            except OSError:
                pass

    def record_dir(self, a: Inputs, attempt: int) -> str:
        repo = self.env.get('GITHUB_REPOSITORY', 'unknown/unknown').lower()
        run = f'{self.env.get("GITHUB_RUN_ID", "0")}-{self.env.get("GITHUB_RUN_ATTEMPT", "1")}'
        if not re.fullmatch(r'[0-9]+-[0-9]+', run) or not registry.REPO.fullmatch(repo):
            raise Refused('GITHUB_RUN_ID, GITHUB_RUN_ATTEMPT or GITHUB_REPOSITORY is malformed')
        return os.path.join(self.inv.host['data'], 'ci-runners', 'state', 'parts', repo, run, f'{a.id}-{attempt}')

    def attempt(self, a: Inputs) -> int:
        """This part's attempt: 1, or one more than ``attempt-of``'s. A record of another
        host, which this host cannot read, is taken by its name: its ``<id>-<n>`` gives
        ``n``, and its id must be this part's (decision 13.3)."""
        if not a.attempt_of:
            return 1
        if a.attempt_of.partition(':')[0] != self.inv.this:
            m = RECORD_REF.fullmatch(a.attempt_of)
            if m is None or m['id'] != a.id:
                raise Refused(f'attempt-of {a.attempt_of!r} is no record of part {a.id}')
            return int(m['n']) + 1
        prev = read_record(self.inv, a.attempt_of)
        return int((prev or {}).get('attempt', 1)) + 1

    # -- the run

    def _on_signal(self, signum, _frame) -> None:
        """The job is cancelled or its runner stops. Before the command starts: end
        ``stopped``. Once it runs: leave it to its scope (job-completed stops it on a
        cancel; after a stopped runner it finishes and ``salvage`` reads it) and exit
        without outputs."""
        self.stopped = True
        if self.proc is not None:
            raise SystemExit(128 + signum)

    def execute(self, a: Inputs) -> tuple[int, dict[str, object]]:
        proj, memory = self.check(a)
        h = self.inv.host
        cls = self.env['CI_RUNNER_CLASS']
        attempt = self.attempt(a)
        record = self.record_dir(a, attempt)
        self.record = record
        os.makedirs(os.path.dirname(record), mode=0o700, exist_ok=True)
        try:
            os.mkdir(record, 0o700)  # atomic: two runs of one id cannot both pass
        except FileExistsError:
            raise Refused(f'part {a.id} already ran in this run (give each part its own id)') from None
        work = os.path.join(record, 'work')  # the command's CI_PART_DIR: it cannot rewrite the record
        results = os.path.join(work, 'results')
        os.makedirs(results, mode=0o700)
        fallback = list(proj.fallback.get(cls, ()))
        rec: dict[str, object] = {
            'project': proj.name, 'id': a.id, 'class': cls, 'host': self.inv.this, 'runner': self.env.get('RUNNER_NAME', ''),
            'attempt': attempt, 'retries': proj.retries, 'fallback': fallback, 'inputs': dataclasses.asdict(a),
            'start_epoch': time.time(), 'boot_start': boot_id(), 'executor': os.getpid(),
            'release': self.release}
        if inventory.pool_labels(self.inv, cls):
            rec['pool_labels'] = inventory.pool_labels(self.inv, cls)
        _write_json(os.path.join(record, 'record.json'), rec)
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, self._on_signal)
        t0 = time.monotonic()
        deadline = t0 + a.queue_timeout_minutes * 60
        leases = Leases(os.path.join(h['data'], 'ci-runners', 'state', 'leases'))
        slot_fd, slot_path, env_fd = None, '', None
        every: list[int] = []  # a nightly part's slots, every one of them (decision 12.8)
        nightly = cls == EVERY_SLOT
        lock = a.lock or ('x' if cls == 'compile' else 'none')  # the class's row checks it (gate)
        hold: locks.HostLock | None = None  # the host lock, held for the whole command (design §5.5 item 3)
        rec['lock'] = lock
        try:
            free = shutil.disk_usage(h['data']).free / (1 << 30)
            if free < h['disk_min_gib']:
                return self._end(record, rec, a, unavailable=f'low disk: {free:.0f} GiB free under the data directory')
            if cls == slurmpart.CLASS:  # one batch job on a Slurm cluster (decision 14.3)
                return self._slurm(a, proj, memory, record, rec, work, lock)
            why = self.gate(a, cls, lock)
            if why:
                return self._end(record, rec, a, unavailable=why)
            why = drain_mod.refusal(drain_mod.read(str(h['data'])), h, cls)  # decision 13.1
            if why:
                return self._away(record, rec, a, why, cls)
            why = self.busy(a, cls, proj.retries)  # decisions 13.2, 13.6
            if why:
                return self._away(record, rec, a, why, cls)
            if nightly:  # from its first ticket to its end, first attempts go elsewhere (13.6)
                every_hold(str(h['data']), [os.getpid()], record)
            if cls in SLOT_CLASSES or nightly:
                q = slots.SlotQueue(h['files']['slot_lock'], h['slots'], h['files']['slot_queue'], poll=self.poll,
                                    default_project=h.get('default_project', ''))
                mine = sum(1 for t in q.waiting() if t.project == proj.name)
                # a retry queues past the cap, which still counts it (13.6): it has nowhere else to go
                if not a.attempt_of and slots.queue_full(mine, proj.max_waiting):
                    return self._end(record, rec, a, unavailable='project queue full')
                q.take_ticket(proj.name, f'part {a.id}', every=nightly)
                every = q.every  # the slots it holds so far, closed at the end
                try:
                    caps = registry.slot_caps(self.etc, self.inv.this)
                    caps.setdefault(proj.name, proj.max_slots)
                    while slot_fd is None:
                        if nightly:
                            slot_fd = (q.try_take_every(proj.name, caps) or [None])[0]
                        else:
                            slot_fd = q.try_take(proj.name, caps)
                        if slot_fd is None:
                            if self.stopped:
                                return self._end(record, rec, a, stopped=True)
                            # a nightly part came in after the gate, and our ticket is behind its (13.6)
                            if (self.yields(a, cls, proj.retries) and every_holder(str(h['data']))
                                    and not any(t.every and t.seq > q.seq for t in q.waiting())):
                                return self._away(record, rec, a, NIGHTLY_BUSY, cls)
                            if time.monotonic() >= deadline:
                                return self._end(record, rec, a, unavailable='queue timeout: no CI slot')
                            time.sleep(self.poll)
                finally:
                    if q.ticket and os.path.exists(q.ticket):
                        os.unlink(q.ticket)
                slot_path = q.slots[q._index(slot_fd)]
                for fd in {slot_fd, *every}:
                    os.set_inheritable(fd, False)  # passed to the supervisor alone (pass_fds)
            cards: list[int] = []  # the cards the command gets (CUDA_VISIBLE_DEVICES)
            if a.gpus or (nightly and h['gpus']):
                need = (a.gpu_mem_mib or proj.gpu_mem_mib) if a.gpus else 0
                mine = list(h['gpus']) if nightly else self.candidates()  # a nightly part leases every card
                n = len(mine) if nightly else a.gpus
                use = mine  # what the health gate reads: a nightly part's command gets any cool cards of them
                while True:
                    # the health gate (per-card hosts):
                    # a hot or unread card refuses, to a cool card's runner, else the fallback
                    cool = gpus_mod.cool_cards(use, smi_status(self.run), h['max_temp_c']) if per_card(h) else use
                    if len(cool) < a.gpus:
                        other = self.cool_elsewhere(mine)
                        return self._end(record, rec, a, unavailable=f'health gate: card {",".join(map(str, use))} '
                                         f'is at or above {h["max_temp_c"]} C or unread',
                                         retry_on=None if other is None else ['self-hosted', f'{self.inv.this}-gpu{other}'])
                    hot = set(use) - set(cool)
                    free = smi_free(self.run)
                    got = None
                    if nightly:
                        # every lease, whatever the cards hold; gpu-mem-mib only on the cards its command
                        # gets: the lowest cool ones with that much free (lead, 2026-11-07; §4.8, §5.5)
                        fit = sorted(c for c in cool if free.get(c, 0) >= need)[:a.gpus]
                        if len(fit) == a.gpus:
                            got = leases.try_take(n, mine, free, 0, every=True)
                    else:
                        fit = []
                        got = leases.try_take(n, mine, {c: m for c, m in free.items() if c not in hot}, need)
                    if got is not None:
                        cards = fit if nightly else got
                        rec['leased'] = sorted(got)
                        break
                    if self.stopped:
                        return self._end(record, rec, a, stopped=True)
                    if not nightly and self.yields(a, cls, proj.retries):
                        # a nightly part came in after the gate: it runs, or our lease ticket is behind its (13.6)
                        held = every_holder(str(h['data']))
                        if held and (len(held.get('procs') or ()) > 1
                                     or any(t.every and t.seq < leases.seq for t in leases.tickets())):
                            return self._away(record, rec, a, NIGHTLY_BUSY, cls)
                    if time.monotonic() >= deadline:
                        return self._end(record, rec, a, unavailable='queue timeout: no card fits')
                    time.sleep(self.poll)
            if lock != 'none':
                # Last, after the slots and the leases, as every holder takes them (§5.5 item 3): a part
                # holds the host lock only once it waits for nothing else, so a nightly part holding
                # every slot and a compile part holding x never wait for each other.
                hold = locks.HostLock(str(h['files']['host_lock']), held='',
                                      timeout=max(0.0, deadline - time.monotonic()))
                try:
                    hold.acquire(lock)
                except locks.LockTimeout:
                    return self._end(record, rec, a, unavailable=f'queue timeout: no host lock ({lock})')
                except locks.MissingSharedFile as e:
                    return self._end(record, rec, a, unavailable=f'host lock: {e}')
                if self.stopped:
                    return self._end(record, rec, a, stopped=True)
                rec['lock_wait_seconds'] = round(hold.waited)
            rec.update(queue_seconds=round(time.monotonic() - t0), cards=cards, slot=slot_path, run_epoch=time.time())
            pin = str((h.get('compile_cpus') if cls == 'compile' else h.get('cpus')) or '')  # design §5.5 item 4
            cpus = pin or str(len(os.sched_getaffinity(0)))
            build, env_fd = envuse.hold(proj.env) if proj.env else ('', None)  # the link, resolved once
            if build:
                rec['env_build'] = os.path.basename(build)  # the one env this part runs in (design 12.11)
            env = part_env(self.env, role=self.inv.this, project=dataclasses.replace(proj, env=build), part_dir=work,
                           results=results, cards=cards,
                           cpus=cpus, files=h['files'], slot=slot_path, resume=a.resume,
                           held=hold.mode if hold else '')
            inner = command_argv(a.command, cpus=pin, nice=0 if cls == 'compile' else 10)
            cap = registry.memory_bytes(memory) if self.caps_memory() else 0
            p2p = self.p2p if cls in P2P_CLASSES else []
            sup = [sys.executable, '-I', '-c', 'import json, sys; sys.path.insert(0, sys.argv.pop(1)); '
                   'from ci_runners.part import supervise; '
                   'sys.exit(supervise(sys.argv[1], float(sys.argv[2]), sys.argv[5:], int(sys.argv[3]), '
                   'json.loads(sys.argv[4])))',
                   self.release, record, str(a.timeout_minutes * 60), str(cap), json.dumps(p2p), *inner]
            run_id = f'{os.path.basename(os.path.dirname(record))}-{attempt}'
            self.unit = unit_name(self.env.get('RUNNER_NAME', ''), a.id, run_id) if self.scope else ''
            rec['unit'] = self.unit
            _write_json(os.path.join(record, 'record.json'), rec)
            argv = scope_argv(self.unit, proj.name, memory, sup, self.oom_policy()) if self.scope else sup
            if self.stopped:
                return self._end(record, rec, a, stopped=True)
            why = drain_mod.refusal(drain_mod.read(str(h['data'])), h, cls)  # drained while it waited (13.1)
            if why:
                return self._away(record, rec, a, why, cls)
            # the supervisor keeps the host lock with the slot: the command's own requests find it
            # held by an ancestor (the cover), and it is let go when the scope ends
            held = tuple(fd for fd in {slot_fd, env_fd, *every, *leases.fds, *(hold.fds if hold else ())}
                         if fd is not None)
            self.proc = self.spawn(argv, cwd=self.env.get('GITHUB_WORKSPACE') or os.getcwd(), env=env, pass_fds=held)
            if nightly:  # the supervisor keeps the slots and leases, so it keeps the hold too
                every_hold(str(h['data']), [os.getpid(), self.proc.pid], record)
            smp, done, thread = None, threading.Event(), None
            if cards:
                smp = sampler_mod.Sampler(cards, csv_path=os.path.join(record, 'samples.csv'),
                                          query=lambda: (smi_status(self.run), smi_apps(self.run)),
                                          procs=self.procs or self._scope_procs, stop=self.stop_part or self._stop_scope,
                                          early_stop=per_card(h), clock=self.clock)
                thread = threading.Thread(target=smp.loop, args=(done, self.sample_s), daemon=True)
                thread.start()
            self.proc.wait()
            done.set()
            if thread is not None:
                thread.join(timeout=60)
                rec.update(pids=sorted(smp.pids), buses=[smp.buses[c] for c in sorted(smp.buses)],
                           early_stop=smp.stopped_card is not None, peak_c={str(c): t for c, t in smp.peak.items()})
            unit_result = ''
            if self.scope:
                r = self.run(['systemctl', '--user', 'show', '-p', 'Result', '--value', self.unit],
                             capture_output=True, text=True, timeout=30)
                unit_result = (r.stdout or '').strip()
                self.run(['systemctl', '--user', 'reset-failed', self.unit], capture_output=True, timeout=30)
            return self._end(record, rec, a, ex=_read_json(os.path.join(record, 'exit.json')),
                             stopped=self.stopped, unit_result=unit_result,
                             cool_card=self.cool_elsewhere(cards) if cards else None)
        finally:
            if nightly and (self.proc is None or self.proc.poll() is not None):
                every_drop(str(h['data']))  # a supervisor that outlives us keeps the record live
            if hold is not None:
                hold.close()  # not release: a supervisor that outlives us keeps the hold
            leases.release()
            for fd in {slot_fd, env_fd, *every} - {None}:
                os.close(fd)

    def yields(self, a: Inputs, cls: str, retries: int) -> bool:
        """Whether a busy host sends this part elsewhere (decisions 13.2, 13.6): only where
        it can go there, a class with a peer pool other than ``light`` (and ``nightly``), and
        a project that allows a retry; never a retry."""
        return not (a.attempt_of or retries < 1 or cls in ('light', EVERY_SLOT)
                    or not inventory.peer_labels(self.inv, cls))

    def _slurm(self, a: Inputs, proj: registry.Project, memory: str, record: str, rec: dict[str, object], work: str,
               lock: str) -> tuple[int, dict[str, object]]:
        """A ``slurm`` part (decision 14.3): the drain, the class row per target, then
        ``slurmpart.drive`` in the part's scope, as a supervisor; its ``exit.json`` and
        ``slurm.json`` end the part (14.4)."""
        h = self.inv.host
        why = drain_mod.refusal(drain_mod.read(str(h['data'])), h, slurmpart.CLASS)  # decision 13.1
        if why:
            return self._away(record, rec, a, why, slurmpart.CLASS)
        names = [x for x in re.split(r'[\s,]+', a.target) if x]
        if not names:
            raise Refused('a slurm part names its target (design §4.8)')
        found = [slurmpart.site_path(self.etc, n) for n in names]
        bad = [w for p, w in found if not p]
        if bad:
            return self._end(record, rec, a, unavailable=f'target: {bad[0]}')
        if len({p for p, _ in found}) > 1:
            raise Refused('a slurm part\'s targets are of one site')
        try:
            site = slurmsite.load_file(found[0][0])
        except slurmsite.SiteError as e:
            return self._end(record, rec, a, unavailable=f'site config: {e}')
        targets, why = slurmpart.row(str(h['data']), site, [t for _, t in found], project=proj.name, gpus=a.gpus,
                                     minutes=a.timeout_minutes, lock=lock)
        if not targets:
            return self._end(record, rec, a, unavailable=why)
        e = self.env
        ws = e.get('GITHUB_WORKSPACE', '')
        if not ws or not os.path.isdir(ws):
            ws = os.path.join(record, 'empty')
            os.makedirs(ws, mode=0o700, exist_ok=True)
        temp = e.get('RUNNER_TEMP', '')
        plan = {
            'site_file': found[0][0], 'targets': targets, 'key': record_ref(record), 'etc': self.etc, 'data': str(h['data']),
            'ssh': self.slurm_tools['ssh'], 'rsync': self.slurm_tools['rsync'], 'poll': self.poll if not self.scope
            else slurmpart.POLL, 'deadline': time.time() + a.queue_timeout_minutes * 60, 'cards': a.gpus,
            'minutes': a.timeout_minutes, 'workspace': ws, 'temp': temp if temp and os.path.isdir(temp) else '',
            'part_dir': work,
            'request': {'project': proj.name, 'cards': a.gpus, 'minutes': a.timeout_minutes,
                        'queue_minutes': min(a.queue_timeout_minutes, 24 * 60), 'memory': memory, 'command': a.command,
                        'variables': {**{k: e[k] for k in slurmpart.JOB_VARS if k in e}, **proj.variables},
                        'run_url': f'https://github.com/{e.get("GITHUB_REPOSITORY", "")}/actions/runs/'
                                   f'{e.get("GITHUB_RUN_ID", "0")}/attempts/{e.get("GITHUB_RUN_ATTEMPT", "1")}',
                        'host': self.inv.this, 'resume': a.resume}}
        _write_json(os.path.join(record, 'slurm-plan.json'), plan)
        drv = [sys.executable, '-I', '-c', 'import sys; sys.path.insert(0, sys.argv.pop(1)); '
               'from ci_runners.slurmpart import drive; sys.exit(drive(sys.argv[1]))', self.release, record]
        run_id = f'{os.path.basename(os.path.dirname(record))}-{rec["attempt"]}'
        self.unit = unit_name(e.get('RUNNER_NAME', ''), a.id, run_id) if self.scope else ''
        rec.update(unit=self.unit, cards=[], targets=targets)
        _write_json(os.path.join(record, 'record.json'), rec)
        argv = scope_argv(self.unit, proj.name, slurmpart.DRIVER_MEMORY, drv, self.oom_policy()) if self.scope else drv
        if self.stopped:
            return self._end(record, rec, a, stopped=True)
        env = {'HOME': e.get('HOME') or os.path.expanduser('~'), 'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8',
               **self.slurm_env}
        self.proc = self.spawn(argv, cwd=ws, env=env)
        self.proc.wait()
        if self.scope:
            self.run(['systemctl', '--user', 'reset-failed', self.unit], capture_output=True, timeout=30)
        return self._end(record, slurm_record(record, rec), a, ex=_read_json(os.path.join(record, 'exit.json')),
                         stopped=self.stopped)

    def busy(self, a: Inputs, cls: str, retries: int) -> str:
        """Busy-host avoidance (decisions 13.2, 13.6): why this part's first attempt goes
        to the other host, or empty (see ``yields``). A ``nightly`` part that holds or waits
        for every slot and card sends it away every time; the load rule once per head and
        part on this host (a marker, then it waits here)."""
        if not self.yields(a, cls, retries):
            return ''
        data = str(self.inv.host['data'])
        if every_holder(data):
            return NIGHTLY_BUSY
        mark = hostload.marker(data, self.env.get('GITHUB_REPOSITORY', ''), self.env.get('GITHUB_SHA', ''), a.id)
        if os.path.lexists(mark):
            return ''
        why = hostload.check(self.inv, cards=self.candidates() if a.gpus else [])
        if why:
            os.makedirs(os.path.dirname(mark), mode=0o700, exist_ok=True)
            with open(mark, 'w', encoding='utf-8') as f:
                f.write(why + '\n')
        return why

    def _away(self, record: str, rec: dict[str, object], a: Inputs, why: str, cls: str) -> tuple[int, dict[str, object]]:
        """End a part this host refuses as drained or busy: ``unavailable``, to the other
        host's pool of its class, or ``next`` ``none`` where the inventory names none
        (decision 13.3)."""
        peer = inventory.peer_labels(self.inv, cls)
        return self._end(record, rec, a, unavailable=why, retry_on=['self-hosted', *peer] if peer else [],
                         nowhere=not peer)

    def _end(self, record: str, rec: dict[str, object], a: Inputs, *, ex: Mapping[str, object] | None = None,
             stopped: bool = False, unavailable: str = '', unit_result: str = '',
             cool_card: int | None = None, retry_on: Sequence[str] | None = None,
             nowhere: bool = False) -> tuple[int, dict[str, object]]:
        _write_json(os.path.join(record, 'record.json'), rec)
        out = finish(record, rec, ex, stopped=stopped, unavailable=unavailable, unit_result=unit_result,
                     cool_card=cool_card, retry_on=retry_on, nowhere=nowhere)
        _publish(record, out, a.id, self.env)
        return (1 if out['outcome'] == 'red' else 0), out


# ------------------------------------------------------------------ salvage


def record_ref(record: str) -> str:
    """A record's path below ``state/parts`` (``<owner>/<repo>/<run>/<id>-<attempt>``):
    what the ``record`` output names, so no home path reaches the job's outputs. Pure."""
    parts = record.split(os.sep)
    return '/'.join(parts[-4:])


def record_run_attempt(record: str) -> int:
    """The run attempt (``GITHUB_RUN_ATTEMPT``, which ``record_dir`` names) a record was
    made in: ``<run id>-<run attempt>``, its parent's name; 0 if that names none. What
    the verdict orders a part's artifacts by (issue #64). Pure."""
    m = re.fullmatch(r'[0-9]+-([0-9]+)', os.path.basename(os.path.dirname(record.rstrip(os.sep))))
    return int(m.group(1)) if m else 0


def _record_path(inv: inventory.Inventory, ref: str) -> str:
    """The local path of a ``<role>:<owner>/<repo>/<run>/<id>-<attempt>`` record,
    which must lie under this host's part records."""
    role, _, rel = ref.partition(':')
    if role != inv.this:
        raise Refused(f'record {ref!r} is on another host')
    root = os.path.realpath(os.path.join(inv.host['data'], 'ci-runners', 'state', 'parts'))
    real = os.path.realpath(os.path.join(root, rel))
    if not real.startswith(root + os.sep):
        raise Refused('the record is not a part record of this host')
    return real


def read_record(inv: inventory.Inventory, ref: str) -> dict | None:
    return _read_json(os.path.join(_record_path(inv, ref), 'record.json'))


def salvage(inv: inventory.Inventory, ref: str, env: Mapping[str, str], *, wait_seconds: float = 0,
            run: Callable = subprocess.run, poll: float = 10) -> tuple[int, dict[str, object]]:
    """The outputs of a part whose job ended without them (design §4.5): its
    ``outcome.json``, else what its record and exit say, else ``lost``. A part whose
    scope still runs is waited for up to ``wait_seconds``."""
    record = _record_path(inv, ref)
    rec = _read_json(os.path.join(record, 'record.json'))
    if rec is None:
        raise Refused('no part record there')
    if not record_ref(record).lower().startswith(env.get('GITHUB_REPOSITORY', '').lower() + '/'):
        raise Refused('the record is another repository\'s')
    done = _read_json(os.path.join(record, 'outcome.json'))
    if done is None:
        deadline = time.monotonic() + wait_seconds
        unit = str(rec.get('unit', ''))
        while unit and not os.path.exists(os.path.join(record, 'exit.json')) and time.monotonic() < deadline:
            r = run(['systemctl', '--user', 'is-active', unit], capture_output=True, text=True, timeout=30)
            if (r.stdout or '').strip() not in ('active', 'activating', 'deactivating'):
                break
            time.sleep(poll)
        ex = _read_json(os.path.join(record, 'exit.json'))
        done = finish(record, rec, ex)
        if ex is not None:  # final: kept for the next reader
            pid = str(rec.get('id', ''))
            _publish(record, done, pid if ID.fullmatch(pid) else 'part', env)
        else:  # lost for now: the record may still finish, so nothing is kept
            write_outputs(done, env.get('GITHUB_OUTPUT', ''))
    else:
        write_outputs(done, env.get('GITHUB_OUTPUT', ''))
    return (1 if done['outcome'] == 'red' else 0), done


# ------------------------------------------------------------------ CLI


def main(argv: list[str], inv: inventory.Inventory, etc: str) -> int:
    release = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        code, out = Executor(inv, etc, os.environ, release=release).execute(parse_args(argv))
    except Refused as e:
        print(f'ci-run part: refused: {e}', file=sys.stderr)
        return 2
    print(f'ci-run part: {out["outcome"]} ({out["reason"]}), next {out["next"]}', file=sys.stderr)
    return code
