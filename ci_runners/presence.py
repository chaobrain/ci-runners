"""Presence (design decision 16.4): stop an idle runner whose resource cannot start a job
now, and start it again once it can, so that a queued job is picked up by a runner that can
run it.

``ci-presence.timer`` runs ``ci-run presence`` (one tick) every ``presence.tick_seconds``
(20 s by default) as the CI account. A tick reads local state only (no token, no
network) and, per runner of the host, its *causes*:

- its card is held by someone else (a compute process that is not ours, or memory that is
  not ours of at least ``busy.gpu_mib``), or fails the card health gate (§5.5, per-card
  hosts: at or above ``max_temp_c``, or unread);
- no free CI slot for it: the host's CI slots minus those held by processes that are not
  ours (a local run's, a pool run's) minus those our busy part runners hold or wait for,
  shared among the idle part runners, one of each pool first, the online ones first (16.2:
  no more part runners online than free slots); or its slot's CPUs kept busy by processes that are not ours (one
  load sample of ``LOAD_SECONDS``; the cause holds at half its CPUs and clears below a quarter);
- the host or its pool is drained (13.1, 15.2), or the host's health gate fails (its free
  disk below ``disk_min_gib``, or the CI account's access to the shared files, 15.2);
- an exclusive timing part runs or waits (its runner is not idle): the runners whose CPUs
  meet ``compile_cpus`` (a slot's cpuset, or the host's ``cpus`` for a ``part`` runner)
  stop.

*Idle* is read without a token: no ``Runner.Worker`` below the runner's directory and no
part scope of the runner. A runner is stopped (``systemctl --user stop``) only after
``TICKS`` consecutive idle ticks with a cause, after one more look for a worker or a scope
just before the stop; one that presence stopped (or that the unit's ``ExecCondition=``
held back, ``Result=exec-condition``) is started after ``TICKS`` ticks without one. A runner
that is not idle is never stopped; nor is the host's light runner without a ``ci-`` label
(the ``home`` pool of §5.1, for the host's salvage legs; until a host's light pool carries
``ci-light``, 16.1, every light runner there is such a runner). One tick runs at a time
(``state/presence.json.lock``). The runner unit's
``ExecCondition=`` runs ``ci-run presence check <runner>``, the same causes now, so that a
restart (15.5) does not bring back a runner whose resource is still taken.

*Rollback*: ``presence = { enabled = false }`` (or no table): the tick starts every runner
it stopped, at once, and does nothing else; ``check`` always lets a runner start.

Every stop and start is one JSON line in the host's
``logs/presence-<UTC date>.log`` (the reaper drops it after 14 days), and the stops and
starts are counted per runner in ``state/presence.json``.

CLI (as the CI account)::

    ci-run presence              one tick (the timer)
    ci-run presence --dry-run    what a tick would stop and start, and why, as if enabled;
                                 changes nothing
    ci-run presence check NAME   the runner unit's ExecCondition: exit 77 while a cause holds
"""
from __future__ import annotations

import dataclasses
import datetime
import fcntl
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence

from ci_runners import drain as drain_mod
from ci_runners import hostload, inventory, outcome, slots

TICK_SECONDS = 20  # the default tick (presence.tick_seconds)
TICKS = 2  # consecutive ticks with (to stop) or without (to start) a cause
LOAD_SECONDS = 1.0  # s: the load sample of a slot's CPUs
LOAD_ON, LOAD_OFF = 0.5, 0.25  # a slot's CPUs busy with others: holds at half of them, clears below a quarter
HOLD = 77  # check's exit while a cause holds: ExecCondition= skips the start (1-254), and no other error maps to it
SLOT_CLASSES = ('part', 'compile')  # the classes whose parts take one host CI slot (part.SLOT_CLASSES)
EVERY_SLOT = 'nightly'  # the class whose part takes every slot (part.EVERY_SLOT)
CARD_CLASSES = {'gpu', 'multi'}  # the classes whose runners lease cards: its own (gpu), every card (multi)
TIMING = 'compile'  # the exclusive timing class (16.1 ci-timing)
RUNNING = ('active', 'activating', 'reloading')
DOWN = ('inactive', 'failed')


# ------------------------------------------------------------------ the runners


@dataclasses.dataclass(frozen=True)
class Runner:
    name: str
    pool: str  # its pool's inventory key
    cls: str
    labels: tuple[str, ...]
    cards: tuple[int, ...]  # the cards it leases: its own (gpu), every card (multi); none for the rest
    slot: int | None  # its index in its pool's slot.cpus (16.2), or None
    cpus: frozenset[int]  # its slot's CPUs, else the host's for a part runner, else none (it fences nothing)

    @property
    def unit(self) -> str:
        return f'ci-runner@{self.name}.service'


def runners(inv: inventory.Inventory, ncpu: int | None = None) -> list[Runner]:
    """This host's runners as setup makes them (``setup._runners``, its order), with their
    pool, cards and CPUs."""
    from ci_runners import setup
    h = inv.host
    keys = []
    for pool, v in h['pools'].items():
        spec = v if isinstance(v, dict) else {'count': v}
        keys += [pool] * (len(h['gpus']) if spec.get('per_card') else spec['count'])
    host_cpus = hostload.cpu_set(str(h.get('cpus') or '')) or frozenset(range(ncpu or os.cpu_count() or 1))
    out = []
    for (name, labels, cls, card, slot), pool in zip(setup._runners(inv), keys, strict=True):
        spec = h['pools'][pool]
        if slot is not None:
            cpus = hostload.cpu_set(spec['slot']['cpus'][slot])
        else:
            cpus = host_cpus if cls == 'part' else frozenset()
        cards = (card,) if card is not None else tuple(h['gpus']) if cls == 'multi' else ()
        out.append(Runner(name, pool, cls, tuple(labels), cards if cls in CARD_CLASSES else (), slot, cpus))
    return out


def never_stopped(r: Runner) -> bool:
    """The host's light runner without a capability label (``ci-light``, ``ci-pub-light``):
    the ``home`` pool of §5.1, for the host's salvage legs, which presence never stops. Pure."""
    return r.cls == 'light' and not any(lb.startswith('ci-') for lb in r.labels)


def per_card(h: Mapping[str, object]) -> bool:
    """A host with a runner per card: its parts' health gate refuses (part.per_card). Pure."""
    pools = h.get('pools')
    return isinstance(pools, Mapping) and inventory.pool_of(h, 'gpu') is not None


# ------------------------------------------------------------------ readings


def unit_states(text: str) -> dict[str, tuple[str, str]]:
    """``unit -> (ActiveState, Result)`` from ``systemctl show --property=Id
    --property=ActiveState --property=Result`` of several units. Pure."""
    out = {}
    for block in text.split('\n\n'):
        kv = dict(line.split('=', 1) for line in block.splitlines() if '=' in line)
        if kv.get('Id'):
            out[kv['Id']] = (kv.get('ActiveState', ''), kv.get('Result', ''))
    return out


def card_status(csv: str) -> dict[int, tuple[str, float, int]]:
    """``index -> (bus, temperature C, memory used MiB)`` from ``nvidia-smi
    --query-gpu=index,pci.bus_id,temperature.gpu,memory.used --format=csv,noheader,nounits``;
    a card whose temperature does not parse is missing (unread). Pure."""
    out = {}
    for line in csv.splitlines():
        p = [x.strip() for x in line.split(',')]
        if len(p) != 4 or not p[0].isdigit() or not p[1]:
            continue
        try:
            temp = float(p[2])
        except ValueError:
            continue
        out[int(p[0])] = (p[1], temp, int(p[3]) if p[3].isdigit() else 0)
    return out


def card_causes(cards: Sequence[int], status: Mapping[int, tuple[str, float, int]],
                apps: Sequence[tuple[int, str, int]], mine: Callable[[int], bool], *, max_temp_c: float,
                gpu_mib: float, health: bool) -> dict[int, list[str]]:
    """Each card's causes: held by someone else (a compute process that is not ours, or
    memory not ours of at least ``gpu_mib``, which also sees a process the host does not
    list), or, where ``health`` (a per-card host), at or above ``max_temp_c`` or unread.
    Pure but for ``mine``."""
    out: dict[int, list[str]] = {}
    for c in cards:
        why = []
        st = status.get(c)
        if health and (st is None or st[1] >= max_temp_c):
            why.append(f'health gate: card {c} at or above {max_temp_c:g} C or unread')
        if st is not None:
            on = [(pid, mib) for pid, bus, mib in apps if outcome.bus_key(bus) == outcome.bus_key(st[0])]
            theirs = [pid for pid, _ in on if not mine(pid)]
            mib = st[2] - sum(m for pid, m in on if pid not in theirs)
            if theirs:
                why.append(f'card {c}: held by a process that is not ours')
            elif mib >= gpu_mib:
                why.append(f'card {c}: {mib} MiB that are not ours')
        out[c] = why
    return out


def workers(root: str, read: Callable[[str], str], listdir: Callable[[str], list[str]],
            proc: str = '/proc') -> set[str] | None:
    """The runners under ``root`` with a ``Runner.Worker`` (a job), read from each
    process's ``cmdline`` (``argv[0]`` ``<root>/<name>/bin/Runner.Worker``, as written or
    as its real path); None when ``proc`` cannot be listed. Any process naming the path
    counts: a false one keeps a runner online, never stops one."""
    prefixes = {r.rstrip('/') + '/' for r in (root, os.path.realpath(root))}
    try:
        pids = [p for p in listdir(proc) if p.isdigit()]
    except OSError:
        return None
    names = set()
    for pid in pids:
        argv0 = read(os.path.join(proc, pid, 'cmdline')).split('\0', 1)[0]
        for prefix in prefixes:
            rest = argv0[len(prefix):].split('/') if argv0.startswith(prefix) else []
            if len(rest) == 3 and rest[0] and rest[1:] == ['bin', 'Runner.Worker']:
                names.add(rest[0])
    return names


def scope_prefix(name: str) -> str:
    """The part scopes of a runner start with this (``part.unit_name``). Pure."""
    return 'ci-part-' + (re.sub(r'[^A-Za-z0-9_.-]', '_', name) or 'runner') + '-'


# ------------------------------------------------------------------ the host's facts


@dataclasses.dataclass
class Facts:
    drain: dict | None = None  # drain.read
    health: str = ''  # the host's health gate: why it fails, or empty
    cards: dict[int, list[str]] = dataclasses.field(default_factory=dict)
    slots: tuple[int, int] | None = None  # (the host's CI slots, those held by processes not ours)
    busy: set[str] | None = None  # the runners that are not idle; None: not known (nothing stops)
    load: dict | None = None  # hostload.load_between, where a runner has a slot


class Probe:
    """Reads the host for a tick. Every reading is injectable (tests stub them all)."""

    def __init__(self, inv: inventory.Inventory, *, run: Callable = subprocess.run,
                 read: Callable[[str], str] = hostload._read, listdir: Callable[[str], list[str]] = os.listdir,
                 uid: int | None = None, sleep: Callable[[float], None] = time.sleep,
                 load: Callable[[], dict | None] | None = None, disk_free: Callable[[str], float] | None = None,
                 own_access: Callable[[dict, int], list] | None = None, locks: Callable[[], str] | None = None,
                 stat_path: Callable[[str], os.stat_result] = os.stat, etc: str = '') -> None:
        self.inv, self.h = inv, inv.host
        self.etc = etc  # the account's etc (the environment's by default): where pool.toml is
        self.run, self.read, self.listdir, self.sleep = run, read, listdir, sleep
        self.uid = os.getuid() if uid is None else uid
        self._load = load
        self._disk_free = disk_free or (lambda p: shutil.disk_usage(p).free / (1 << 30))
        self._own_access = own_access
        self._locks = locks or (lambda: self.read('/proc/locks'))
        self.stat_path = stat_path

    def mine(self, pid: int) -> bool:
        return hostload.ours(pid, self.uid, self.read)

    def publish(self) -> bool:
        """Rewrite this host's own part of the account's capacity file (``pool.record_local``,
        its probe read now), where the account has a machine registry; False where it has none."""
        from ci_runners import pool
        path = os.path.join(pool._etc(self.etc), 'pool.toml')
        if not os.path.isfile(path):
            return False
        pool.record_local(pool.load_file(path), self.etc)
        return True

    def systemctl(self, *argv: str) -> subprocess.CompletedProcess | None:
        try:
            return self.run(['systemctl', '--user', *argv], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return None

    def units(self, rs: Sequence[Runner]) -> dict[str, tuple[str, str]] | None:
        if not rs:
            return {}
        r = self.systemctl('show', '--property=Id', '--property=ActiveState', '--property=Result',
                           *[x.unit for x in rs])
        if r is None or r.returncode != 0:
            return None
        return unit_states(r.stdout or '')

    def scopes(self) -> list[str] | None:
        r = self.systemctl('list-units', '--type=scope', '--plain', '--no-legend', 'ci-part-*')
        if r is None or r.returncode != 0:
            return None
        return [line.split()[0] for line in (r.stdout or '').splitlines() if line.split()]

    def busy(self, rs: Sequence[Runner]) -> set[str] | None:
        """The runners with a worker or a part scope; None if either cannot be read."""
        w = workers(os.path.join(str(self.h['data']), 'actions-runner'), self.read, self.listdir)
        sc = self.scopes()
        if w is None or sc is None:
            return None
        return {r.name for r in rs if r.name in w or any(s.startswith(scope_prefix(r.name)) for s in sc)}

    def idle_now(self, r: Runner) -> bool:
        """The look just before a stop: no worker and no part scope of ``r`` now."""
        b = self.busy([r])
        return b is not None and r.name not in b

    def health(self) -> str:
        h = self.h
        try:
            free = self._disk_free(str(h['data']))
            if free < h['disk_min_gib']:
                return f'health: {free:.0f} GiB free under the data directory, below {h["disk_min_gib"]}'
        except OSError:
            pass
        if h.get('local_accounts'):
            try:
                if self._own_access is None:
                    from ci_runners import setup
                    self._own_access = setup.own_access
                if self._own_access(h, self.uid):
                    return 'health: the CI account lacks access to the shared files (decision 15.2)'
            except Exception:  # noqa: BLE001 - no proven miss: the part's gate is the backstop
                pass
        return ''

    def smi(self, *argv: str) -> str:
        try:
            r = self.run(['nvidia-smi', *argv], capture_output=True, text=True, timeout=20)
            return r.stdout if r.returncode == 0 else ''
        except (OSError, subprocess.SubprocessError):
            return ''

    def cards(self, rs: Sequence[Runner]) -> dict[int, list[str]]:
        want = sorted({c for r in rs for c in r.cards})
        if not want:
            return {}
        h = self.h
        status = card_status(self.smi('--query-gpu=index,pci.bus_id,temperature.gpu,memory.used',
                                      '--format=csv,noheader,nounits'))
        apps = hostload.compute_mib(self.smi('--query-compute-apps=pid,gpu_bus_id,used_memory',
                                             '--format=csv,noheader,nounits'))
        busy_t = h.get('busy') if isinstance(h.get('busy'), dict) else {}
        return card_causes(want, status, apps, self.mine, max_temp_c=h['max_temp_c'],
                           gpu_mib=float(busy_t.get('gpu_mib', hostload.DEFAULTS['gpu_mib'])), health=per_card(h))

    def slot_holds(self) -> tuple[int, int] | None:
        """``(the host's CI slots, those held by a process that is not ours)``: a slot
        file's exclusive ``flock`` in ``/proc/locks`` (read, never taken), by a PID that is
        not ours (a hidden one is not). None where the host has no slot files."""
        h = self.h
        lock = h['files'].get('slot_lock')
        if not lock:
            return None
        n = int(h['slots'])
        text, theirs = self._locks(), 0
        for path in slots.slot_paths(str(lock), n):
            try:
                st = self.stat_path(path)
            except OSError:
                continue
            pids = hostload.write_flocks(text, (os.major(st.st_dev), os.minor(st.st_dev)), st.st_ino)
            if pids and not any(self.mine(p) for p in pids):
                theirs += 1
        return n, theirs

    def load(self) -> dict | None:
        if self._load is not None:
            return self._load()
        return hostload.load_sample(LOAD_SECONDS, self.uid, read=self.read, listdir=self.listdir, sleep=self.sleep)

    def facts(self, rs: Sequence[Runner], focus: Sequence[Runner] | None = None) -> Facts:
        """The host's facts for ``rs``; the cards and the load sample only for ``focus``
        where given (``check``: the one runner, so a light runner's start reads no card)."""
        mine = rs if focus is None else focus
        f = Facts(drain=drain_mod.read(str(self.h['data'])), health=self.health(), cards=self.cards(mine),
                  slots=self.slot_holds() if any(r.cls in SLOT_CLASSES for r in rs) else None, busy=self.busy(rs))
        if any(r.slot is not None for r in mine):
            f.load = self.load()
        return f


# ------------------------------------------------------------------ the causes


def causes(inv: inventory.Inventory, rs: Sequence[Runner], f: Facts, units: Mapping[str, tuple[str, str]],
           held_load: set[str] = frozenset()) -> dict[str, list[str]]:
    """Each runner's causes now (decision 16.4; see the module's text). ``units`` gives
    which runners are online (the free slots go to those first); ``held_load`` the runners
    whose load cause held at the last tick (it clears below ``LOAD_OFF``). The never
    stopped runner has none. Pure."""
    h = inv.host
    busy = f.busy or set()
    compile_cpus = hostload.cpu_set(str(h.get('compile_cpus') or ''))
    timing = any(r.cls == TIMING and r.name in busy for r in rs)
    out: dict[str, list[str]] = {}
    for r in rs:
        why = []
        d = drain_mod.refusal(f.drain, h, r.cls)
        if d:
            why.append(d)
        if f.health:
            why.append(f.health)
        for c in r.cards:
            why += f.cards.get(c, [])
        if timing and r.cls != TIMING and r.cpus & compile_cpus:
            why.append(f'timing: a timing part fences CPUs {h.get("compile_cpus")}')
        if r.slot is not None and f.load is not None and r.cpus:
            v = hostload.foreign_cores(f.load, r.cpus)
            if v >= (LOAD_OFF if r.name in held_load else LOAD_ON) * len(r.cpus):
                why.append(f'load: {v:.1f} cores that are not ours on its {len(r.cpus)} CPUs')
        out[r.name] = why
    if f.slots is not None:  # 16.2: no more part runners online than free CI slots
        n, theirs = f.slots
        part = [r for r in rs if r.cls in SLOT_CLASSES]
        ours = sum(1 for r in part if r.name in busy)
        free = 0 if any(r.cls == EVERY_SLOT and r.name in busy for r in rs) else max(0, n - theirs - ours)
        idle = [r for r in part if r.name not in busy and not out[r.name]]
        idle.sort(key=lambda r: units.get(r.unit, ('', ''))[0] not in RUNNING)  # stable: online first, then by order
        # one runner of each pool first (a timing runner is not starved by idle part runners), then the rest
        first = list({r.pool: r for r in reversed(idle)}.values())[::-1]
        first.sort(key=idle.index)
        idle = first + [r for r in idle if r not in first]
        for r in idle[free:]:
            out[r.name].append(f'slot: no free CI slot ({theirs} of {n} held by others, {ours} by our parts)')
    for r in rs:
        if never_stopped(r):
            out[r.name] = []
    return out


# ------------------------------------------------------------------ state and log


def state_path(data: str) -> str:
    return os.path.join(data, 'ci-runners', 'state', 'presence.json')


def fresh_state() -> dict:
    return {'runners': {}, 'counts': {}}


def read_state(data: str) -> tuple[dict, bool]:
    """The state and whether it was read; a missing file is a fresh state (read), an
    unreadable or malformed one a fresh state not read: the caller then takes every runner
    that is down for one presence stopped, so a rollback still brings it back."""
    try:
        with open(state_path(data), encoding='utf-8') as f:
            st = json.load(f)
    except FileNotFoundError:
        return fresh_state(), True
    except (OSError, ValueError):
        return fresh_state(), False
    if not (isinstance(st, dict) and isinstance(st.get('runners'), dict) and isinstance(st.get('counts'), dict)):
        return fresh_state(), False
    return st, True


def write_state(data: str, st: Mapping) -> None:
    path = state_path(data)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = f'{path}.{os.getpid()}.new'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(st, f, sort_keys=True)
        f.write('\n')
    os.replace(tmp, path)


def log(data: str, line: Mapping[str, object], now: float | None = None) -> None:
    """Append ``line`` to ``logs/presence-<UTC date>.log`` (never through a link or into a
    FIFO), and print it for the journal. A line that cannot be written is lost."""
    t = datetime.datetime.fromtimestamp(time.time() if now is None else now, datetime.timezone.utc)
    text = json.dumps({'time': t.isoformat(timespec='seconds'), **line}, sort_keys=True)
    print(text, flush=True)
    try:
        logs = os.path.join(data, 'ci-runners', 'logs')
        os.makedirs(logs, mode=0o700, exist_ok=True)
        fd = os.open(os.path.join(logs, f'presence-{t:%Y-%m-%d}.log'),
                     os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            if stat.S_ISREG(os.fstat(fd).st_mode):
                os.write(fd, (text + '\n').encode())
        finally:
            os.close(fd)
    except OSError:
        pass


def _count(st: dict, name: str, action: str) -> None:
    c = st['counts'].setdefault(name, {})
    c[action] = int(c.get(action, 0)) + 1


# ------------------------------------------------------------------ the tick


def publish_capacity(data: str, probe: Probe, t: float) -> None:
    """Decision 16.5: presence rewrites this host's own part of the account's capacity file
    at every enabled tick, after its stops and starts (``Probe.publish``). Best effort: a
    failure is logged and the tick goes on; the file is ``pool status``'s as well, whose
    timer keeps it within 30 s."""
    try:
        probe.publish()
    except Exception as e:  # noqa: BLE001 - the capacity file never stops a tick
        log(data, {'capacity': 'not written', 'error': f'{type(e).__name__}: {e}'[:200]}, t)


def config(h: Mapping[str, object]) -> bool:
    """Whether the host's ``presence`` table enables presence. Pure."""
    p = h.get('presence') if isinstance(h.get('presence'), dict) else {}
    return bool(p.get('enabled', False))


def tick(inv: inventory.Inventory, probe: Probe, *, dry_run: bool = False, now: Callable[[], float] = time.time,
         out: Callable[[str], None] = print) -> int:
    """One tick (see the module's text). ``dry_run`` evaluates as if presence were enabled,
    prints what it would stop and start and why, and changes nothing: no state, no unit, no
    drain."""
    h = inv.host
    data = str(h['data'])
    if dry_run:
        return _tick(inv, probe, data, dry_run=True, now=now, out=out)
    os.makedirs(os.path.dirname(state_path(data)), mode=0o700, exist_ok=True)
    fd = os.open(state_path(data) + '.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:  # another tick (the timer's, or one by hand) runs: this one does nothing
            out('presence: another tick runs; nothing done')
            return 0
        return _tick(inv, probe, data, dry_run=False, now=now, out=out)
    finally:
        os.close(fd)


def _tick(inv: inventory.Inventory, probe: Probe, data: str, *, dry_run: bool, now: Callable[[], float],
          out: Callable[[str], None]) -> int:
    h = inv.host
    enabled = config(h)
    rs = runners(inv)
    st, read_ok = read_state(data)
    units = probe.units(rs)
    if units is None:
        out('presence: the runner units cannot be read; nothing done')
        return 1

    def ours_down(r: Runner) -> bool:  # down, and presence's to start: it stopped it, or the condition held it
        a, res = units.get(r.unit, ('', ''))
        rst = st['runners'].get(r.name) or {}
        return a in DOWN and (bool(rst.get('stopped')) or res == 'exec-condition' or not read_ok)

    if not enabled and not dry_run:  # the rollback: start what presence stopped, and nothing else
        for r in rs:
            if ours_down(r):
                p = probe.systemctl('--no-block', 'start', r.unit)
                if p is not None and p.returncode == 0:
                    log(data, {'runner': r.name, 'action': 'start', 'cause': 'presence is off'}, now())
                    _count(st, r.name, 'start')
                    st['runners'].pop(r.name, None)
        if st['runners'] or not read_ok:
            st['runners'] = {n: v for n, v in st['runners'].items() if v.get('stopped')}
            write_state(data, st)
        return 0

    t = now()
    f = probe.facts(rs)
    held = {n for n, v in st['runners'].items() if v.get('load')}
    why = causes(inv, rs, f, units, held)
    plan: list[tuple[str, Runner]] = []
    for r in rs:
        rst = st['runners'].setdefault(r.name, {'stop_ticks': 0, 'start_ticks': 0, 'stopped': None, 'load': False})
        a, _ = units.get(r.unit, ('', ''))
        idle = f.busy is not None and r.name not in f.busy
        rst['load'] = any(c.startswith('load:') for c in why[r.name])
        if a in RUNNING:
            rst['stopped'], rst['start_ticks'] = None, 0
            rst['stop_ticks'] = rst['stop_ticks'] + 1 if why[r.name] and idle else 0
            if rst['stop_ticks'] >= TICKS or dry_run and why[r.name] and idle:
                plan.append(('stop', r))
        elif ours_down(r):
            rst['stop_ticks'] = 0
            rst['start_ticks'] = 0 if why[r.name] or f.busy is None else rst['start_ticks'] + 1
            if rst['start_ticks'] >= TICKS or dry_run and not why[r.name] and f.busy is not None:
                plan.append(('start', r))
        else:  # down by someone else's hand, or between states: not presence's
            rst['stop_ticks'] = rst['start_ticks'] = 0
    if dry_run:
        out(report(inv, rs, units, f, why, plan, enabled))
        return 0
    for action, r in plan:
        rst = st['runners'][r.name]
        cause = '; '.join(why[r.name]) or 'no cause'
        if action == 'stop':
            if not probe.idle_now(r):  # a job came between the tick's look and now
                rst['stop_ticks'] = 0
                log(data, {'runner': r.name, 'action': 'kept', 'cause': cause, 'why': 'a job started'}, t)
                continue
            rst['stopped'] = t  # on disk before the stop: a tick that dies after it still owns the runner
            write_state(data, st)
            p = probe.systemctl('--no-block', 'stop', r.unit)
            if p is not None and p.returncode == 0:
                rst['stop_ticks'] = 0
                log(data, {'runner': r.name, 'action': 'stop', 'cause': cause}, t)
                _count(st, r.name, 'stop')
            else:  # not stopped: a running unit clears the mark at the next tick anyway
                rst['stopped'] = None
        else:
            p = probe.systemctl('--no-block', 'start', r.unit)
            if p is not None and p.returncode == 0:
                rst['stopped'], rst['start_ticks'] = None, 0
                log(data, {'runner': r.name, 'action': 'start', 'cause': 'no cause'}, t)
                _count(st, r.name, 'start')
    names = {r.name for r in rs}
    st['runners'] = {n: v for n, v in st['runners'].items() if n in names}
    write_state(data, st)
    # 16.5: the host's own part of the capacity file, at every tick, after the state is saved: a
    # hung nvidia-smi in its probe that gets the tick killed then loses nothing of the tick
    publish_capacity(data, probe, t)
    return 0


def report(inv: inventory.Inventory, rs: Sequence[Runner], units: Mapping[str, tuple[str, str]], f: Facts,
           why: Mapping[str, list[str]], plan: Sequence[tuple[str, Runner]], enabled: bool) -> str:
    """The dry run's text: each runner's state, causes and what a tick would do (a stop
    needs ``TICKS`` such ticks in a row). Pure."""
    acts = {r.name: a for a, r in plan}
    lines = [f'presence: {"enabled" if enabled else "not enabled"} on this host; dry run, as if enabled '
             f'(a stop or start needs {TICKS} ticks in a row; nothing is changed)']
    if f.busy is None:
        lines.append('  idle: not known (the processes or the scopes cannot be read): nothing would stop')
    for r in rs:
        a, res = units.get(r.unit, ('?', ''))
        idle = '?' if f.busy is None else 'busy' if r.name in f.busy else 'idle'
        state = f'{a}{" (" + res + ")" if a in DOWN and res not in ("", "success") else ""}'
        what = acts.get(r.name, 'never stopped' if never_stopped(r) else 'keep')
        lines.append(f'  {r.name}: {state}, {idle}; {what}' + (f'; {"; ".join(why[r.name])}' if why[r.name] else ''))
    return '\n'.join(lines)


def check(inv: inventory.Inventory, probe: Probe, name: str, out: Callable[[str], None] = print) -> int:
    """The runner unit's ``ExecCondition=``: ``HOLD`` while a cause of the runner holds
    (presence enabled), else 0. Any error lets the runner start: the executor's refusals
    are the backstop (16.4)."""
    try:
        enabled = config(inv.host)
        if not enabled:
            return 0
        rs = runners(inv)
        r = next((x for x in rs if x.name == name), None)
        if r is None or never_stopped(r):
            return 0
        units = probe.units(rs) or {}
        why = causes(inv, rs, probe.facts(rs, focus=[r]), units)[name]
        if why:
            out(f'presence: {name} held back: {"; ".join(why)}')
            return HOLD
        return 0
    except Exception as e:  # noqa: BLE001 - never keep a runner down on an error of ours
        out(f'presence: check failed, the runner starts: {type(e).__name__}: {e}')
        return 0


def main(argv: list[str], inv_loader: Callable[[], inventory.Inventory]) -> int:
    usage = 'usage: ci-run presence [--dry-run] | ci-run presence check RUNNER'
    if len(argv) == 2 and argv[0] == 'check':
        try:
            inv = inv_loader()
        except Exception as e:  # noqa: BLE001 - the inventory unread: let the runner start (its part refuses)
            print(f'presence: inventory unread, the runner starts: {type(e).__name__}', file=sys.stderr)
            return 0
        return check(inv, Probe(inv), argv[1])
    if argv not in ([], ['--dry-run']):
        print(usage, file=sys.stderr)
        return 2
    inv = inv_loader()
    return tick(inv, Probe(inv), dry_run=argv == ['--dry-run'])
