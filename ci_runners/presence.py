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
  stop;
- *tier* (decision 19.2, the tick only): on a host whose registry entry has a ``tier``, a
  runner of class ``part``, ``gpu`` or ``multi`` with capability labels (``ci-`` ones and
  ``mem-big``) and no other cause, while a host of an earlier tier offers them: its row,
  read in this tick through its forced probe (``pool.probe_host``, fast), is up, read
  within ``YIELD_FRESH_S``, has this host's repository entries, and one of its pools
  carries every one of those labels with a ``listening`` runner (online, idle, its
  listener connected). A runner presence started is not yielded for ``DWELL_S``. A *hold*
  on the host (``ci-run presence hold``, 19.8) suspends the cause: no runner yields while
  one is active.

*Idle* is read without a token: no ``Runner.Worker`` below the runner's directory and no
part scope of the runner. A runner is stopped (``systemctl --user stop``) only after
``TICKS`` consecutive idle ticks with a cause, after one more look for a worker or a scope
just before the stop; one that presence stopped (or that the unit's ``ExecCondition=``
held back, ``Result=exec-condition``) is started after ``TICKS`` ticks without one, and one
stopped for the tier cause alone at the first tick without one (up at once). A runner
that is not idle is never stopped; nor is the host's light runner without a ``ci-`` label
(the ``home`` pool of §5.1, for the host's salvage legs; until a host's light pool carries
``ci-light``, 16.1, every light runner there is such a runner). One tick runs at a time
(``state/presence.json.lock``). The runner unit's
``ExecCondition=`` runs ``ci-run presence check <runner>``, the same causes now but the
tier (no network in a unit's start), so that a restart (15.5) does not bring back a runner
whose resource is still taken.

*Black holes.* ``presence.infra_failures`` (3) infrastructure ends within 15 min drain the
host as 15.2's miss does (``drained: infra`` in the host's log): a part's ``unavailable``
other than busy, drained or a full queue (and other than a cluster part's, which is the
cluster's); a ``lost`` part on a host that stayed up; a hook refusal ``stale``, ``runner``
(a runner directory the inventory does not know), ``policy``, ``inventory``, ``timeout`` or
``error``. Ends before the last self-drain or the last undrain do not count.
Nothing undrains by itself.

*Rollback*: ``presence = { enabled = false }`` (or no table): the tick starts every runner
it stopped, at once, and does nothing else but stop the public runners; ``check`` always
lets a runner start.

*The public runners* (decision 18.4, 18.5; from C2). A public runner is a confined
single-job runner the launcher starts as the user unit ``ci-pub-runner@<role>-pub-gpu<N>-<seq>``
(C3), holding card N's lease for its life. The *public audience* holds only while presence is
enabled, the inventory names the host's public pool and its ``isolation``, and the conformance
check's record (``state/confine-check.json``) passed every item within ``CHECK_MAX_AGE``, in
this boot and kernel, for that backend, with the GPU group alone (mode (a)). At each tick a
withdrawn audience stops every public runner at once, busy or not; what stops the private
runners for the whole host (a drain of the host or of its gpu pool, a self-drain, the health
gate) stops the idle ones (a ``Runner.Worker`` in the unit's cgroup is a job, which finishes)
and offers no card. While one is up, its card
is a cause of the card's private runners (``public: ...``), so they stop when idle and the
unit's ``ExecCondition=`` keeps them down. Each enabled tick writes ``state/public.json``: the
audience, the check's result and age, and the cards a public runner could take now (no
cause, no private job, no public runner), which the capacity row carries for the issuer.

Every stop, start, infrastructure end and drain is one JSON line in the host's
``logs/presence-<UTC date>.log`` (the reaper drops it after 14 days), and the stops and
starts are counted per runner in ``state/presence.json``.

CLI (as the CI account)::

    ci-run presence              one tick (the timer)
    ci-run presence --dry-run    what a tick would stop and start, and why, as if enabled;
                                 changes nothing
    ci-run presence check NAME   the runner unit's ExecCondition: exit 77 while a cause holds
    ci-run presence hold [--minutes N | --release]
                                 decision 19.8: hold this host (no runner yields to an earlier
                                 tier) for N minutes, or end the caller's hold; no option:
                                 print the holds
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
INFRA_FAILURES = 3  # the default count of infrastructure ends that drain the host
INFRA_WINDOW = 15 * 60  # s: the window of those ends
TICKS = 2  # consecutive ticks with (to stop) or without (to start) a cause
LOAD_SECONDS = 1.0  # s: the load sample of a slot's CPUs
LOAD_ON, LOAD_OFF = 0.5, 0.25  # a slot's CPUs busy with others: holds at half of them, clears below a quarter
HOLD = 77  # check's exit while a cause holds: ExecCondition= skips the start (1-254), and no other error maps to it
LOST_HORIZON = 12 * 3600  # s: the part records read for a lost part (the longest part, 600 min, and its queue)
SLOT_CLASSES = ('part', 'compile')  # the classes whose parts take one host CI slot (part.SLOT_CLASSES)
EVERY_SLOT = 'nightly'  # the class whose part takes every slot (part.EVERY_SLOT)
CARD_CLASSES = {'gpu', 'multi'}  # the classes whose runners lease cards: its own (gpu), every card (multi)
TIMING = 'compile'  # the exclusive timing class (16.1 ci-timing)
RUNNING = ('active', 'activating', 'reloading')
DOWN = ('inactive', 'failed')
#: a part's ``unavailable`` that is no infrastructure end (16.4): busy, drained, a full queue;
#: also a wait that ran out (the host was busy: no slot, no card, no host lock) and a request
#: the class's row refuses (the caller's ``gpus``, ``lock`` or class, not the host)
NOT_INFRA = ('busy', 'drained', 'project queue full', drain_mod.UNREADABLE, 'queue timeout', 'gpus ', 'lock ',
             'this host has no compile_cpus')
HOOK_INFRA = ('stale', 'runner', 'policy', 'inventory', 'timeout', 'error')  # the hook's refusals that are infrastructure ends
CLUSTER = 'slurm'  # a cluster part's unavailable is the cluster's, not the host's
YIELD_CLASSES = ('part', 'gpu', 'multi')  # the classes whose runners yield to an earlier tier (19.2)
YIELD_FRESH_S = 45  # s: an earlier tier's row older than this offers nothing (two of its ticks and a margin)
DWELL_S = 300  # s: a runner presence started is not yielded before this (19.2's hysteresis)
TIER = 'tier: '  # the cause's prefix
HOLD_MAX_MIN = 720  # min: the longest hold (19.8: one night)
HOLD_ENTRIES = 32  # the most holders at once
HOLD_CLASS = 'nightly'  # the policy class a job's repository needs on the host to hold it (19.8)
PUBLIC_UNIT = 'ci-pub-runner@'  # the public runners' units (18.4; the launcher's, C3)
CHECK_FILE, PUBLIC_FILE = 'confine-check.json', 'public.json'  # the check's record (18.5); the tick's public row
CHECK_MAX_AGE = 26 * 3600  # s: an older check withdraws the public audience (18.5)
PUBLIC_CAUSE = 'public: '


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
    public: set[int] | None = dataclasses.field(default_factory=set)  # cards a public runner holds; None: all (unread)


class Probe:
    """Reads the host for a tick. Every reading is injectable (tests stub them all)."""

    def __init__(self, inv: inventory.Inventory, *, run: Callable = subprocess.run,
                 read: Callable[[str], str] = hostload._read, listdir: Callable[[str], list[str]] = os.listdir,
                 uid: int | None = None, sleep: Callable[[float], None] = time.sleep,
                 load: Callable[[], dict | None] | None = None, disk_free: Callable[[str], float] | None = None,
                 own_access: Callable[[dict, int], list] | None = None, locks: Callable[[], str] | None = None,
                 stat_path: Callable[[str], os.stat_result] = os.stat, etc: str = '',
                 tiers: Callable[[], tuple | None] | None = None) -> None:
        self.inv, self.h = inv, inv.host
        self.etc = etc  # the account's etc (the environment's by default): where pool.toml is
        self.run, self.read, self.listdir, self.sleep = run, read, listdir, sleep
        self.uid = os.getuid() if uid is None else uid
        self._load = load
        self._disk_free = disk_free or (lambda p: shutil.disk_usage(p).free / (1 << 30))
        self._own_access = own_access
        self._locks = locks or (lambda: self.read('/proc/locks'))
        self.stat_path = stat_path
        self._tiers = tiers

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

    def tiers(self) -> tuple[int, list[dict], str] | None:
        """Decision 19.2: ``(this host's tier, the rows of the hosts of an earlier tier read
        now, this host's repository entries' digest)``; None where the account has no
        registry, its local machine no tier, or no host is of an earlier tier. Each row is
        read through its forced probe, fast (``pool.probe_host``), all at once; one that
        does not answer is a down row."""
        if self._tiers is not None:
            return self._tiers()
        import concurrent.futures
        from ci_runners import pool
        path = os.path.join(pool._etc(self.etc), 'pool.toml')
        if not os.path.isfile(path):
            return None
        reg = pool.load_file(path)
        me = pool._local(reg)
        if me.tier is None:
            return None
        up = [m for m in reg.machines.values() if m.access == 'ssh' and m.tier is not None and m.tier < me.tier]
        if not up:
            return None
        with concurrent.futures.ThreadPoolExecutor(len(up)) as ex:
            rows = list(ex.map(lambda m: pool.probe_host(m, etc=self.etc, fast=True), up))
        pol = pool.policy_digests(self.etc) or {}
        return me.tier, rows, str(pol.get('repositories') or '')

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

    def public_units(self) -> list[tuple[str, str]] | None:
        """This account's public runner units ``(unit, active state)`` that are not down, or
        None where they cannot be read (decision 18.4)."""
        r = self.systemctl('list-units', '--all', '--plain', '--no-legend', f'{PUBLIC_UNIT}*')
        if r is None or r.returncode != 0:
            return None
        out = []
        for line in (r.stdout or '').splitlines():
            f = line.split()
            if len(f) >= 3 and f[0].startswith(PUBLIC_UNIT) and f[2] not in DOWN:
                out.append((f[0], f[2]))
        return out

    def public_busy(self, units: Sequence[tuple[str, str]]) -> set[str] | None:
        """The public runner units of ``units`` with a job; None if /proc cannot be read."""
        return public_workers([u for u, _ in units], self.read, self.listdir)

    def check_record(self) -> dict | None:
        """The conformance check's last record (18.5), or None."""
        return _json(os.path.join(str(self.h['data']), 'ci-runners', 'state', CHECK_FILE))

    def kernel(self) -> str:
        return os.uname().release

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
                  slots=self.slot_holds() if any(r.cls in SLOT_CLASSES for r in rs) else None, busy=self.busy(rs),
                  public=public_cards(self.public_units(), self.inv.this) if any(r.cards for r in mine) else set())
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
        held = [c for c in r.cards if f.public is None or c in f.public]
        if held:  # one job per card: a public runner holds its lease (18.4)
            why.append(f'{PUBLIC_CAUSE}card {held[0]} is held by a public runner' if f.public is not None
                       else f'{PUBLIC_CAUSE}the public runners cannot be read')
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


def capability(r: Runner) -> frozenset[str]:
    """The runner's capability labels: its ``ci-`` labels and ``mem-big`` (16.1). Pure."""
    return frozenset(x for x in r.labels if x.startswith('ci-') or x == 'mem-big')


def offers(tiers: tuple | None, t: float) -> list[tuple[int, str, frozenset[str], int]]:
    """What the earlier tiers offer now (decision 19.2): ``(tier, key, labels, listening)``
    per pool of a host row that is of an earlier tier than this host's, up, read within
    ``YIELD_FRESH_S``, with this host's repository entries, and with a ``listening``
    runner; by tier, then key. Nothing where ``tiers`` is None. Pure."""
    if not tiers:
        return []
    me, rows, repos = tiers
    out = []
    for row in rows:
        tier, read = row.get('tier'), row.get('read_at')
        if type(tier) is not int or type(me) is not int or tier >= me or row.get('problems') or row.get('target'):
            continue
        # ``read_at`` is in this host's clock (``pool.probe_host``: now less the file's age), and
        # ``t`` is read after the probes: a row past ``t`` is not proven fresh
        if type(read) not in (int, float) or not 0 <= t - read <= YIELD_FRESH_S:
            continue
        pol = row.get('policy') if isinstance(row.get('policy'), dict) else {}
        if not repos or pol.get('repositories') != repos:
            continue
        for p in row.get('pools') if isinstance(row.get('pools'), list) else []:
            n = p.get('listening') if isinstance(p, dict) else None
            if type(n) is int and n > 0 and isinstance(p.get('labels'), list):
                out.append((tier, str(row.get('key')), frozenset(str(x) for x in p['labels']), n))
    return sorted(out, key=lambda o: (o[0], o[1]))


def tier_yield(rs: Sequence[Runner], why: Mapping[str, list[str]], tiers: tuple | None, st: Mapping,
               t: float) -> dict[str, str]:
    """The runners that yield now (decision 19.2), each with its cause: one of
    ``YIELD_CLASSES`` with capability labels and no other cause in ``why``, not started by
    presence within ``DWELL_S`` (``st``), whose labels a host of an earlier tier
    ``offers``. Pure."""
    got = offers(tiers, t)
    out: dict[str, str] = {}
    for r in rs:
        caps = capability(r)
        if r.cls not in YIELD_CLASSES or not caps or why.get(r.name) or never_stopped(r):
            continue
        started = (st.get('runners', {}).get(r.name) or {}).get('started')
        if type(started) in (int, float) and 0 <= t - started < DWELL_S:
            continue
        for tier, key, labels, n in got:
            if caps <= labels:
                out[r.name] = f'{TIER}{key} (tier {tier}) has {n} idle runner(s) of {" + ".join(sorted(caps))}'
                break
    return out


# ------------------------------------------------------------------ state and log


def state_path(data: str) -> str:
    return os.path.join(data, 'ci-runners', 'state', 'presence.json')


def fresh_state() -> dict:
    return {'runners': {}, 'counts': {}, 'infra': {'since': 0.0, 'drained': False, 'seen': {}}}


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
    if not (isinstance(st.get('infra'), dict) and isinstance(st['infra'].get('seen'), dict)):
        st['infra'] = fresh_state()['infra']  # a state from before the self-drain, or a bad count: count afresh
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


# ------------------------------------------------------------------ black holes


def _json(path: str) -> dict | None:
    try:
        with open(path, encoding='utf-8') as f:
            v = json.load(f)
        return v if isinstance(v, dict) else None
    except (OSError, ValueError):
        return None


def boot_id(read: Callable[[str], str] = hostload._read) -> str:
    return read('/proc/sys/kernel/random/boot_id').strip()


def part_end(doc: Mapping | None, rec: Mapping, boot: str) -> str:
    """Why a part's end is an infrastructure end (16.4), or empty: ``unavailable`` other
    than busy, drained or a full queue, and other than a cluster part's (the cluster's, not
    this host's); ``lost`` on a host that stayed up (its boot id unchanged). ``doc`` is its
    ``outcome.json``, None for a record that never finished (lost). Pure."""
    kind = 'lost' if doc is None else str(doc.get('outcome', ''))
    reason = '' if doc is None else str(doc.get('reason', ''))
    if kind == 'unavailable':
        if reason.startswith(NOT_INFRA) or rec.get('class') == CLUSTER:
            return ''
        return f'unavailable: {reason}'
    if kind == 'lost' and boot and rec.get('boot_start') == boot:
        return 'lost'
    return ''


def hook_end(line: Mapping) -> str:
    """Why a hook log line is an infrastructure end: a refusal ``stale``, ``policy``,
    ``timeout`` or ``error`` (the hook could not decide). Pure."""
    if line.get('accepted') is False and line.get('reason') in HOOK_INFRA and 'hook' not in line:
        return f'hook: {line["reason"]}'
    return ''


def _iso(s: object) -> float | None:
    try:
        return datetime.datetime.fromisoformat(str(s)).timestamp()
    except ValueError:
        return None


def _record_end(record: str, since: float, *, alive: Callable[[int, float], bool], active: Callable[[str], bool],
                boot: str) -> dict | None:
    """One part record's infrastructure end in the window after ``since``, or None (see
    ``infra_ends``)."""
    try:
        t = os.stat(os.path.join(record, 'outcome.json')).st_mtime
    except OSError:
        t = None
    if t is not None and t <= since:  # ended before the window: not read
        return None
    rec = _json(os.path.join(record, 'record.json'))
    if rec is None:
        return None
    if t is not None:
        doc = _json(os.path.join(record, 'outcome.json'))
        if doc is None:
            return None
    else:  # never finished: lost once nothing of it runs
        if os.path.exists(os.path.join(record, 'exit.json')):
            return None
        pid, start = int(rec.get('executor', 0)), float(rec.get('start_epoch', 0))
        if pid > 0 and alive(pid, start) or rec.get('unit') and active(str(rec['unit'])):
            return None
        with os.scandir(record) as it:
            t = max([os.stat(record).st_mtime, *(e.stat(follow_symlinks=False).st_mtime for e in it)])
        if t <= since:
            return None
        doc = None
    why = part_end(doc, rec, boot)
    ref = '/'.join(record.split(os.sep)[-4:])
    return {'key': f'part:{ref}', 'time': t, 'why': why, 'record': ref} if why else None


def infra_ends(data: str, now: float, *, alive: Callable[[int, float], bool] = slots.alive,
               active: Callable[[str], bool] = lambda unit: False, boot: str = '') -> list[dict]:
    """The infrastructure ends of this host in the window before ``now``: ``{'key',
    'time', 'why'}`` each, from the part records (an ``outcome.json``'s time; a record
    that never finished, whose executor is gone, whose scope is not ``active`` and with no
    ``exit.json``, at its newest file's time) and the hook's log lines. A record or line
    that cannot be read is skipped, whatever the error."""
    out = []
    since = now - INFRA_WINDOW
    root = os.path.join(data, 'ci-runners', 'state', 'parts')

    def dirs(d: str, horizon: float) -> list[str]:
        try:
            with os.scandir(d) as it:
                return [e.path for e in it if e.is_dir(follow_symlinks=False) and e.stat().st_mtime >= horizon]
        except OSError:
            return []
    horizon = now - LOST_HORIZON
    for owner in dirs(root, 0):
        for repo in dirs(owner, 0):
            for run in dirs(repo, horizon):
                for record in dirs(run, horizon):
                    try:
                        end = _record_end(record, since, alive=alive, active=active, boot=boot)
                    except Exception:  # noqa: BLE001 - one bad record never stops a tick
                        end = None
                    if end:
                        out.append(end)
    logs = os.path.join(data, 'ci-runners', 'logs')
    days = sorted({datetime.datetime.fromtimestamp(x, datetime.timezone.utc).strftime('%Y-%m-%d') for x in (since, now)})
    for day in days:
        try:
            with open(os.path.join(logs, f'hook-{day}.log'), encoding='utf-8', errors='replace') as f:
                text = f.read(16 << 20)
        except OSError:
            continue
        for n, raw in enumerate(text.splitlines()):
            try:
                line = json.loads(raw)
                t = _iso(line.get('time')) if isinstance(line, dict) else None
                why = hook_end(line) if t is not None else ''
            except Exception:  # noqa: BLE001 - one bad line never stops a tick (a deep nesting, say)
                continue
            if why and since < t <= now:
                out.append({'key': f'hook:{day}:{n}', 'time': t, 'why': why, 'runner': str(line.get('runner', ''))})
    return sorted(out, key=lambda e: e['time'])


def black_hole(inv: inventory.Inventory, st: dict, f: Facts, now: float, limit: int, *,
               ends: Sequence[dict], dry_run: bool = False) -> list[dict]:
    """Count the infrastructure ends after the last self-drain or undrain, log each new one,
    and drain the host (``drained: infra``) once ``limit`` fall in the window. Returns the
    ends counted."""
    data = str(inv.host['data'])
    infra = st['infra']
    drained = f.drain is None or f.drain['host'] is not None
    if infra.get('drained') and not drained:  # undrained since the last tick: count afresh
        infra['since'] = now
    counted = [e for e in ends if e['time'] > float(infra.get('since') or 0)]
    if dry_run:
        return counted
    for e in counted:
        if e['key'] not in infra['seen']:
            infra['seen'][e['key']] = e['time']
            log(data, {'infra': e['why'], **{k: e[k] for k in ('record', 'runner') if k in e}}, now)
    infra['seen'] = {k: t for k, t in infra['seen'].items() if t > now - INFRA_WINDOW - 3600}
    if len(counted) >= limit and not drained:
        try:
            drain_mod.change(data, True)
            log(data, {'drain': 'drained: infra', 'ends': [e['why'] for e in counted]}, now)
            drained = True
        except (OSError, ValueError) as e:  # ValueError: an unreadable state, drained already
            log(data, {'drain': 'drained: infra', 'error': str(e)}, now)
        infra['since'] = now
    infra['drained'] = drained
    return counted


# ------------------------------------------------------------------ the hold (decision 19.8)


def hold_path(data: str) -> str:
    return os.path.join(data, 'ci-runners', 'state', 'hold.json')


def read_holds(data: str, now: float) -> dict[str, dict]:
    """The unexpired holds of the host, by holder key: ``{"until", "by", "set_at"}``. A
    missing, unreadable or malformed file holds nothing (the yield's facts decide). An
    entry is kept only with a finite ``until`` after ``now`` and at most ``HOLD_MAX_MIN``
    (and a minute) beyond it: a hand-edited or junk marker, or a clock set back, holds no
    longer than the command could set."""
    try:
        with open(hold_path(data), encoding='utf-8') as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return {}
    holders = doc.get('holders') if isinstance(doc, dict) else None
    if not isinstance(holders, dict):
        return {}
    return {str(k): dict(v) for k, v in holders.items() if isinstance(v, dict)
            and type(v.get('until')) in (int, float)
            and now < v['until'] <= now + 60 * (HOLD_MAX_MIN + 1)}  # also not inf or NaN


def hold_until(data: str, now: float) -> float | None:
    """The latest ``until`` of the host's unexpired holds, or None."""
    return max((v['until'] for v in read_holds(data, now).values()), default=None)


def _write_holds(data: str, holders: Mapping[str, dict]) -> None:
    path = hold_path(data)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = f'{path}.{os.getpid()}.new'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump({'holders': dict(holders)}, f, sort_keys=True)
        f.write('\n')
    os.replace(tmp, path)


def holder(inv: inventory.Inventory, env: Mapping[str, str], etc: str = '') -> tuple[str, str, str]:
    """``(key, by, why not)`` of the caller of ``ci-run presence hold`` (decision 19.8): the
    maintainer outside a job (``GITHUB_ACTIONS`` not ``true``), key ``maintainer``; a job
    only on this host's ``home`` runner (``RUNNER_NAME``, a runner presence never stops),
    for a repository whose entry in this host's policy is not paused, has the job's
    repository id (a job without one is refused), and lists ``HOLD_CLASS``; its key is the
    repository, run id and attempt (one hold per run: its jobs share it).
    ``why not`` is empty when the caller may hold."""
    if env.get('GITHUB_ACTIONS') != 'true':
        return 'maintainer', 'maintainer', ''
    repo = str(env.get('GITHUB_REPOSITORY') or '').lower()
    key = f'{repo}#{env.get("GITHUB_RUN_ID", "")}-{env.get("GITHUB_RUN_ATTEMPT", "")}'
    name = str(env.get('RUNNER_NAME') or '')
    r = next((x for x in runners(inv) if x.name == name), None)
    if r is None or not never_stopped(r):
        return key, repo, f'a job holds its host only from the host\'s home runner, not {name or "an unnamed runner"}'
    from ci_runners import policy, pool
    try:
        with open(os.path.join(pool._etc(etc), 'policy.toml'), 'rb') as f:
            raw = f.read(pool.POLICY_MAX + 1)
        if len(raw) > pool.POLICY_MAX:
            raise policy.PolicyError('too large')
        pol = policy.load(raw)
    except (OSError, policy.PolicyError) as e:
        return key, repo, f'this host\'s policy does not load ({type(e).__name__})'
    entry = pol.repos.get(repo)
    if entry is None or entry.paused:
        return key, repo, f'{repo or "no repository"} has no entry in force in this host\'s policy'
    rid = str(env.get('GITHUB_REPOSITORY_ID') or '')
    if rid != str(entry.id):
        return key, repo, f'{repo}: the job\'s repository id is not the policy\'s'
    if HOLD_CLASS not in entry.classes:
        return key, repo, f'{repo}: its entry in this host\'s policy lacks the class {HOLD_CLASS}'
    return key, repo, ''


def hold(inv: inventory.Inventory, argv: Sequence[str], *, env: Mapping[str, str] | None = None, etc: str = '',
         now: Callable[[], float] = time.time, out: Callable[[str], None] = print) -> int:
    """``ci-run presence hold [--minutes N | --release]`` (decision 19.8): set or extend the
    caller's hold to now + N minutes (1 to ``HOLD_MAX_MIN``; a hold already later is kept),
    end it, or print the holds. Exit 0; 1 refused; 2 usage; 3 the marker not written.
    Every write drops the expired entries and is logged."""
    env = os.environ if env is None else env
    data = str(inv.host['data'])
    t = now()
    if not argv:
        held = read_holds(data, t)
        if not held:
            out('presence: no hold on this host')
        for k, v in sorted(held.items()):
            out(f'presence: held by {k} until {_utc(v["until"])} (set by {v.get("by", "?")})')
        return 0
    minutes = None
    if len(argv) == 2 and argv[0] == '--minutes':
        try:
            minutes = int(argv[1])
        except ValueError:
            minutes = 0
        if not 1 <= minutes <= HOLD_MAX_MIN:
            out(f'presence: hold --minutes takes 1 to {HOLD_MAX_MIN}')
            return 2
    elif list(argv) != ['--release']:
        out('usage: ci-run presence hold [--minutes N | --release]')
        return 2
    key, by, why = holder(inv, env, etc)
    if why:
        out(f'presence: hold refused: {why}')
        return 1
    try:
        os.makedirs(os.path.dirname(hold_path(data)), mode=0o700, exist_ok=True)
        fd = os.open(hold_path(data) + '.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            held = read_holds(data, t)
            if minutes is None:
                held.pop(key, None)
                action = {'hold': 'released', 'by': key}
            else:
                if key not in held and len(held) >= HOLD_ENTRIES:
                    out(f'presence: hold refused: {HOLD_ENTRIES} holds already')
                    return 1
                until = max(round(t + 60 * minutes, 3), held.get(key, {}).get('until', 0))
                held[key] = {'until': until, 'by': by, 'set_at': round(t, 3)}
                action = {'hold': 'set', 'by': key, 'until': _utc(until)}
            _write_holds(data, held)
        finally:
            os.close(fd)
    except OSError as e:
        out(f'presence: hold not written: {type(e).__name__}')
        return 3
    log(data, action, t)
    return 0


def _utc(t: float) -> str:
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')


# ------------------------------------------------------------------ the public runners (18.4, 18.5)

_PUBLIC_NAME = re.compile(r'([a-z0-9][a-z0-9-]{0,30})-pub-gpu([0-9]{1,4})-([0-9]{1,12})\Z')


def public_card(unit: str, role: str) -> int | None:
    """The card of a public runner unit of this host (``ci-pub-runner@<role>-pub-gpu<N>-<seq>``),
    or None where the name is not one. Pure."""
    name = unit[len(PUBLIC_UNIT):] if unit.startswith(PUBLIC_UNIT) else ''
    name = name[:-len('.service')] if name.endswith('.service') else name
    m = _PUBLIC_NAME.fullmatch(name)
    return int(m.group(2)) if m and m.group(1) == role else None


def public_workers(units: Sequence[str], read: Callable[[str], str], listdir: Callable[[str], list[str]],
                   proc: str = '/proc') -> set[str] | None:
    """The public runner units of ``units`` with a job: a process whose ``argv[0]`` ends in
    ``/bin/Runner.Worker`` (as the confinement's mounts name it) in the unit's cgroup or one
    beneath it; None when ``proc`` cannot be listed. A process the job forges keeps its own
    runner, which runs a job anyway, up; it never stops one."""
    want = set(units)
    try:
        pids = [p for p in listdir(proc) if p.isdigit()]
    except OSError:
        return None
    busy = set()
    for pid in pids:
        if not read(os.path.join(proc, pid, 'cmdline')).split('\0', 1)[0].endswith('/bin/Runner.Worker'):
            continue
        for line in read(os.path.join(proc, pid, 'cgroup')).splitlines():
            busy.update(want & set(line.split(':', 2)[-1].split('/')))
    return busy


def public_cards(units: Sequence[tuple[str, str]] | None, role: str) -> set[int] | None:
    """The cards the public runners hold; None (every card) where they cannot be read or a
    unit's name names no card of this host. Pure."""
    if units is None:
        return None
    cards = [public_card(u, role) for u, _ in units]
    return None if None in cards else set(cards)


def check_result(doc: Mapping | None, *, isolation: object, boot: str, kernel: str, now: float) -> tuple[str, str]:
    """The conformance check's result (18.5): ``('passed', '')``, or ``(state, why)`` with
    ``state`` one of ``none``, ``failed`` and ``stale``. Pure."""
    if not isinstance(doc, Mapping):
        return 'none', 'no conformance check has run'
    failed = doc.get('failed')
    if doc.get('passed') is not True or failed != []:
        items = ', '.join(str(x) for x in failed) if isinstance(failed, list) and failed else 'its record'
        return 'failed', f'the conformance check failed ({items})'
    t = doc.get('time')
    if type(t) not in (int, float) or not now - CHECK_MAX_AGE <= t <= now + 300:
        return 'stale', f'the conformance check is older than {CHECK_MAX_AGE // 3600} h'
    if not boot or doc.get('boot_id') != boot or doc.get('kernel') != kernel:
        return 'stale', 'the conformance check ran in another boot or kernel'
    if doc.get('isolation') != isolation:
        return 'failed', f'the conformance check proved {doc.get("isolation")!r}, not the inventory\'s {isolation!r}'
    if doc.get('groups') != 'gpu':
        return 'failed', 'the job keeps more than the GPU group (mode (a) is required, 18.4)'
    return 'passed', ''


def public_audience(inv: inventory.Inventory, enabled: bool, check: tuple[str, str]) -> str:
    """Why this host offers no public audience now, or ''. Pure."""
    if inventory.public_pool(inv) is None:
        return 'the inventory names no public pool'
    if not inv.host.get('isolation'):
        return 'the inventory names no isolation'
    if not enabled:
        return 'presence is off'
    return check[1] if check[0] != 'passed' else ''


def public_hold(inv: inventory.Inventory, probe: Probe) -> str:
    """What stops the host's runners for the whole host now (a drain of the host or of its
    gpu pool, a self-drain, an unreadable drain state, a failed health gate), or '': it stops
    the idle public runners and offers no card; a busy one finishes its job."""
    return drain_mod.refusal(drain_mod.read(str(inv.host['data'])), inv.host, 'gpu') or probe.health()


def stop_idle_public(probe: Probe, data: str, units: Sequence[tuple[str, str]], why: str, t: float) -> None:
    """Stop the public runners without a job, each looked at again just before its stop;
    none where /proc cannot be read (an unproven idle is busy)."""
    busy = probe.public_busy(units)
    for unit, state in units:
        if busy is None or unit in busy:
            continue
        now = probe.public_busy([(unit, state)])
        if now is None or unit in now:  # a job came between the look and the stop
            log(data, {'runner': unit, 'action': 'kept', 'cause': why, 'why': 'a job started'}, t)
            continue
        p = probe.systemctl('--no-block', 'stop', unit)
        ok = p is not None and p.returncode == 0
        log(data, {'runner': unit, 'action': 'stop' if ok else 'stop failed', 'cause': why}, t)


def public_row(inv: inventory.Inventory, rs: Sequence[Runner], f: Facts, why: Mapping[str, list[str]],
               check: tuple[str, str], doc: Mapping | None, withdrawn: str, t: float) -> dict:
    """``state/public.json``: the capacity row's public part (18.7). The cards a public
    runner could take now: every card of the host's gpu pool whose runner is idle, without
    a cause, and held by no public runner; none while the audience is withdrawn. Pure."""
    free = []
    if not withdrawn and f.busy is not None and f.public is not None \
            and not any(x.cards and x.cls != 'gpu' and x.name in f.busy for x in rs):  # a multi-card job holds them all
        for r in rs:
            if r.cls == 'gpu' and len(r.cards) == 1 and r.name not in f.busy and not why.get(r.name):  # a public runner's card has its cause
                free.append(r.cards[0])
    age = round(t - doc['time']) if isinstance(doc, Mapping) and type(doc.get('time')) in (int, float) else None
    return {'time': t, 'isolation': inv.host.get('isolation', ''), 'check': check[0], 'why': withdrawn,
            'groups': doc.get('groups', '') if isinstance(doc, Mapping) else '',
            'proven': doc.get('isolation', '') if check[0] == 'passed' and isinstance(doc, Mapping) else '',
            'age_s': age, 'offered': not withdrawn, 'cards': sorted(free),
            'running': sorted(f.public) if f.public is not None else None}


def stop_public(probe: Probe, data: str, units: Sequence[tuple[str, str]] | None, why: str, t: float) -> None:
    """Stop every public runner at once (a withdrawn audience, 18.5); where they cannot be
    read, every unit of the public runners' pattern."""
    for unit, _ in [(f'{PUBLIC_UNIT}*', '')] if units is None else units:
        p = probe.systemctl('--no-block', 'stop', unit)
        ok = p is not None and p.returncode == 0
        log(data, {'runner': unit, 'action': 'stop' if ok else 'stop failed', 'cause': f'public audience withdrawn: {why}'}, t)


def public_tick(inv: inventory.Inventory, probe: Probe, data: str, enabled: bool, t: float) -> tuple[str, str, dict | None]:
    """The public runners' part of a tick: their audience now, and its stop where it is
    withdrawn, and the idle ones' stop where the host is held for the whole host (a drain, a
    self-drain, the health gate). ``(why no card is offered, check state and why, the check's
    record)``."""
    units = probe.public_units()
    doc = probe.check_record()
    check = check_result(doc, isolation=inv.host.get('isolation'), boot=boot_id(probe.read), kernel=probe.kernel(), now=t)
    withdrawn = public_audience(inv, enabled, check)
    if withdrawn and units != []:
        stop_public(probe, data, units, withdrawn, t)
        return withdrawn, check, doc
    held = '' if withdrawn else public_hold(inv, probe)
    if held and units:  # units unread: no idle is proven, nothing is stopped; the row offers none
        stop_idle_public(probe, data, units, held, t)
    return withdrawn or held, check, doc


def write_public(data: str, row: Mapping) -> None:
    path = os.path.join(data, 'ci-runners', 'state', PUBLIC_FILE)
    tmp = f'{path}.{os.getpid()}.tmp'
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600), 'w', encoding='utf-8') as f:
        json.dump(row, f, sort_keys=True)
    os.replace(tmp, path)


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


def config(h: Mapping[str, object]) -> tuple[bool, int]:
    """``(enabled, infra_failures)`` from the host's ``presence`` table. Pure."""
    p = h.get('presence') if isinstance(h.get('presence'), dict) else {}
    return bool(p.get('enabled', False)), int(p.get('infra_failures', INFRA_FAILURES))


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
    enabled, limit = config(h)
    rs = runners(inv)
    st, read_ok = read_state(data)
    # the public runners first: nothing below (an unread unit, the rollback) keeps a withdrawn audience up
    pub = None if dry_run else public_tick(inv, probe, data, enabled, now())
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
    withdrawn, check, check_doc = pub or ('', ('none', ''), None)
    f = probe.facts(rs)
    held = {n for n, v in st['runners'].items() if v.get('load')}
    why = causes(inv, rs, f, units, held)
    tiers = None
    hold_end = hold_until(data, t)  # 19.8: a hold suspends the cause, and no row is read
    if hold_end is None and any(r.cls in YIELD_CLASSES for r in rs):  # 19.2: read the earlier tiers' rows now
        try:
            tiers = probe.tiers()
        except Exception as e:  # noqa: BLE001 - not read: nothing yields
            if not dry_run:
                log(data, {'tier': 'not read', 'error': f'{type(e).__name__}: {e}'[:200]}, t)
    yields = tier_yield(rs, why, tiers, st, now())  # the clock after the probes: their rows are stamped by it
    for n, c in yields.items():
        why[n] = [c]
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
            if why[r.name]:  # held for the tier cause alone: up at the first tick without it (19.2)
                rst['yielded'] = r.name in yields
            # a yielded runner needs no reading of /proc to come back (an unproven fact counts as false)
            rst['start_ticks'] = 0 if why[r.name] or f.busy is None and not rst.get('yielded') \
                else rst['start_ticks'] + 1
            if rst['start_ticks'] >= (1 if rst.get('yielded') else TICKS) \
                    or dry_run and not why[r.name] and f.busy is not None:
                plan.append(('start', r))
        else:  # down by someone else's hand, or between states: not presence's
            rst['stop_ticks'] = rst['start_ticks'] = 0
    if dry_run:
        if hold_end is not None:
            out(f'presence: held until {_utc(hold_end)}: no runner yields to an earlier tier (19.8)')
        try:
            ends: list[dict] | None = black_hole(inv, st, f, t, limit, ends=_ends(probe, data, t), dry_run=True)
        except Exception:  # noqa: BLE001 - the report without the count
            ends = None
        out(report(inv, rs, units, f, why, plan, enabled, ends=ends, limit=limit))
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
            rst['yielded'] = r.name in yields
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
                rst['stopped'], rst['start_ticks'], rst['yielded'], rst['started'] = None, 0, False, t
                log(data, {'runner': r.name, 'action': 'start', 'cause': 'no cause'}, t)
                _count(st, r.name, 'start')
    try:  # after the stops and starts, and never in their way
        black_hole(inv, st, f, t, limit, ends=_ends(probe, data, t))
    except Exception as e:  # noqa: BLE001 - the controller goes on; the error is logged
        log(data, {'infra': 'not counted', 'error': f'{type(e).__name__}: {e}'}, t)
    names = {r.name for r in rs}
    st['runners'] = {n: v for n, v in st['runners'].items() if n in names}
    write_state(data, st)
    if inventory.public_pool(inv) is not None:  # 18.7: the capacity row's public part, for the issuer
        try:
            write_public(data, public_row(inv, rs, f, why, check, check_doc, withdrawn, t))
        except OSError as e:
            log(data, {'public': 'not written', 'error': f'{type(e).__name__}: {e}'[:200]}, t)
    # 16.5: the host's own part of the capacity file, at every tick, after the state is saved: a
    # hung nvidia-smi in its probe that gets the tick killed then loses nothing of the tick
    publish_capacity(data, probe, t)
    return 0


def _ends(probe: Probe, data: str, now: float) -> list[dict]:
    return infra_ends(data, now, active=lambda unit: _active(probe, unit), boot=boot_id(probe.read))


def _active(probe: Probe, unit: str) -> bool:
    r = probe.systemctl('is-active', unit)
    return r is not None and (r.stdout or '').strip() in ('active', 'activating', 'deactivating', 'reloading')


def report(inv: inventory.Inventory, rs: Sequence[Runner], units: Mapping[str, tuple[str, str]], f: Facts,
           why: Mapping[str, list[str]], plan: Sequence[tuple[str, Runner]], enabled: bool, *,
           ends: Sequence[dict] | None, limit: int) -> str:
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
    if ends is None:
        lines.append('infrastructure ends: not counted (an error; see the tick\'s log)')
    else:
        lines.append(f'infrastructure ends in the last {INFRA_WINDOW // 60} min: {len(ends)} (the host drains at {limit})')
        lines += [f'  {e["why"]}' for e in ends]
    return '\n'.join(lines)


def check(inv: inventory.Inventory, probe: Probe, name: str, out: Callable[[str], None] = print) -> int:
    """The runner unit's ``ExecCondition=``: ``HOLD`` while a cause of the runner holds
    (presence enabled), else 0. Any error lets the runner start: the executor's refusals
    are the backstop (16.4)."""
    try:
        enabled, _ = config(inv.host)
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
    usage = 'usage: ci-run presence [--dry-run] | ci-run presence check RUNNER | ci-run presence hold [--minutes N | --release]'
    if len(argv) == 2 and argv[0] == 'check':
        try:
            inv = inv_loader()
        except Exception as e:  # noqa: BLE001 - the inventory unread: let the runner start (its part refuses)
            print(f'presence: inventory unread, the runner starts: {type(e).__name__}', file=sys.stderr)
            return 0
        return check(inv, Probe(inv), argv[1])
    if argv[:1] == ['hold']:
        try:
            inv = inv_loader()
        except Exception as e:  # noqa: BLE001 - one line, not a traceback
            print(f'presence: hold: inventory unread: {type(e).__name__}', file=sys.stderr)
            return 3
        return hold(inv, argv[1:])
    if argv not in ([], ['--dry-run']):
        print(usage, file=sys.stderr)
        return 2
    inv = inv_loader()
    return tick(inv, Probe(inv), dry_run=argv == ['--dry-run'])
