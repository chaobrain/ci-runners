"""``ci-run pool``: every machine is a pool (decision 15.1). This module holds the machine
registry, the live capacity query (``pool status``), the claim, the env check and ``pool
run``, which puts a heavy command on a free machine. Adding a host (``pool add``, its
checks and ``pool ready``) is ``ci_runners/pooladd.py``.

**The registry** is ``$CI_RUNNERS_ETC/pool.toml`` (``cli.etc_dir``). It is private site
config, never in the repository: it names hosts, ssh aliases and paths, which is inventory
(design §3.3); ``python -m ci_runners.pool deny FILE`` prints their hashes for
``tests/deny.sha256``. Each ``[machine.<key>]`` entry describes one machine:

.. code-block:: toml

    schema = 1
    project = "myproj"                   # the env's project and the slot queue's
    repository = "owner/myproj"          # a cluster job's key and comment
    check = ["jax", "numpy"]             # the env check's distributions
    gpu_need_mib = 6000                  # free MiB a card needs
    warm = "<command>"                   # the compile-cache warm-up pool add's checks run (optional)
    numerics = "<command>"               # the numerics check pool add's checks run (optional)
    capacity = { cluster_seconds = 300 } # s between two readings of a cluster by pool status (optional; 30-450)

    [machine.hostb]                      # a host: ssh (or local) access
    access = "ssh"                       # "ssh" or "local"
    ssh = "<alias>"                      # ssh: the alias
    hostname = "<hostname>"              # what `hostname` prints there: the caller's own host
    cpus = 28
    memory_gib = 125
    gpus = 2                             # cards; 0 for none
    gpu_model = "3090"
    gpu_mib = 24576                      # MiB per card
    cuda = "cuda13"
    p2p = true                           # its P2P self-test passed
    python = "/abs/env/bin/python"       # the env a command runs in
    work = "/abs/dir"                    # where a run stages its tree (removed at its end)
    slots = { lock = "/abs/x.lock", count = 2 }   # the host's CI slots (and queue = DIR)
    max_slots = 1                        # the project's cap on that queue (optional; none: the slot count)
    drain = "/abs/<data>/ci-runners/drain.json"   # the CI account's drain copy (optional)
    leases = { dir = "/abs/leases", parts = 1 }   # the cards' leases, shared with the parts (optional)
    runners = "/abs/<data>/actions-runner"        # the CI account's runners: none alive is a problem (optional)
    labels = { cpu = "hostb-cpu" }       # its runner labels per group (HostState.labels)
    probe = "forced"                     # ssh: its key is forced to `ci-run pool probe` (status only; optional)
    numerics_ok = true                   # its numerics check passed
    cache = "/abs/compile/cache"         # its compile cache (optional)
    ready = true                         # pool add writes false, pool ready true; no key: ready
    tier = 1                             # its tier, 1 to 99: a later tier's runners yield to it (optional; 19.1)
    dev_slots = { lock = "/abs/dev.lock", cpus = ["0-15", "16-31"], memory_gib = 64 }   # pool run's own (16.8; optional)

    [machine.cluster]                    # a Slurm cluster: access through the account's gate
    access = "slurm"
    ssh = "<alias>"                      # the account owner's login
    gate = "/abs/<root>/ci-runners/releases/hook/bin/ci-slurm-gate"
    billed = true
    [machine.cluster.targets.cpu]        # one row per target, keyed <machine>:<target>
    partition = "<partition>"
    cpus = 32                            # cores a job takes (a GPU target: per card, the site's cpus_per_card)
    memory_gib = 1900
    max_minutes = 90
    variant = "cpu"                      # the env's variant (envs/<project>-<variant>)
    price = 0.05                         # yuan per core-hour (per card-hour on a GPU target)
    numerics_ok = false
    labels = { cpu = "cluster-cpu" }
    # a GPU target also has: gpu_model, cards (per node), max_cards, gpu_mib, cuda
    # a CI account names the site config's site and account instead of ssh and gate
    # (site = "<site>", account = "<account>"): reached through the account's CI key, for
    # pool status only

**A CI account's registry** (decision 15.1, item 8) is read by its runners' plan jobs. A CI
account has no unrestricted login: a cluster entry names ``site`` and ``account``, and
``pool status`` reaches it as the parts do (``slurmpart.Gate``: the account's key
``etc/slurm/keys/<account>``, batch mode) and calls the gate's read-only verb
``capacity``; another host's entry sets ``probe = "forced"``, and ``pool status`` sends no
code there but logs in with ``etc/pool/keys/<key>``, whose ``authorized_keys`` line on that
host's CI account is ``restrict`` and forces ``ci-run pool probe``: the probe of the one
``local`` machine of that account's own registry, as JSON. Such entries serve ``pool
status`` only: ``pool run`` refuses them.

**pool status**: one row per host and per cluster target, with the fields of brainspike's
``HostState`` (``labels``, ``problems``, ``queue``, ``gpu_free``, ``multi_gpu``,
``gpu_model``, ``cuda``, ``cpus_free``, ``billed``, ``price``, ``target``,
``numerics_ok``, ``start_now``) and a few of its own (``slots_free``, ``slots``,
``mem_free_gib``, ``here``, ``access``, ``runners_online``, ``runners``, ``jobs_free``). A host's slots are read from ``/proc/locks``
without being taken (``slots._flocked``). A cluster target's free cores and cards come from
``sinfo``, less what the account's own running jobs hold of its association's ``GrpTRES``.
Decision 15.7 adds two, for brainspike's placement: the ``local`` machine's row has
``runners``, the runner instances its inventory (``<etc>/inventory.toml``, beside the
registry) registers per label (``setup.runners``: each instance once per label it carries,
shared labels included); a forced probe returns its own host's the same way. A row whose
inventory is missing or does not load has none. A cluster target's row has ``jobs_free``,
how many jobs of its size (``start_now``'s: its cores, on a GPU target cards x cores per
card, and the memory) the partition's ``idle`` and ``mix`` nodes hold now, summed, capped
by the association's room; positive exactly where ``start_now`` is true.

**Decision 19** (R5b): a host entry's ``tier`` (1 to 99, 1 first) orders the hosts: a later
tier's idle runners yield to an earlier tier's listening ones (presence, 19.2), and its row
carries it. The CI account's own probe (``_with_inventory``) adds
the host's ``role``, its ``pools`` (``pool_table``), its hook policy's digests
(``policy_digests``: a policy that does not load is the problem ``policy: does not load``)
and its health gate's failure (the problem ``health: ...``), and its hold (19.8:
``hold_until``, ``holds``; ``pool status`` prints ``hold until``); a cluster target's row has
its ``memory_gib``, ``max_minutes`` and an opaque ``partition`` (``partition_key``). ``pool
status`` flags a host whose repository entries differ from most hosts' (``capacity.flag_policy``).

**The capacity file** (decision 16.5; ``ci_runners/capacity.py``): ``pool status`` writes
its rows, each with ``read_at``, into ``state/capacity.json`` beside the account's ``etc``,
one machine's as soon as it answers, with the ``local`` machine's probe; a host row also
has ``label_runners``, each runner label's instances registered, online and free (no
``Runner.Worker``). The account's ``ci-capacity.timer`` runs it every 30 s; a cluster is
asked again only once its rows in the file are ``capacity.cluster_seconds`` old (300 s by
default: its login node is shared), and its rows count as down after 10 min, a host's after 2. The forced
probe answers with that file's probe while it is fresh (``capacity.STALE_S``), without
running anything. ``pool status --fast`` reads the files: the forced hosts' through their
probes, the hosts this account probes itself without ``nvidia-smi`` (their cards from the
file), the cluster's targets from the file; each ssh call connects within 2 s and ends
within ``FAST_TIMEOUT``; a host row older than 2 min, a cluster row older than 10 min,
counts as down. The route reads it through
``capacity.load``.

A machine that is not ``ready`` is skipped by ``pool run``'s plan unless ``--on`` names it
(``pool add``'s checks run there that way). A cluster's entry is hand-written.

A run that asks for cards takes them on a host by the parts' leases (``part.Leases``, in
their ticket order, in the directory the inventory's ``files.lease_dir`` shares with the
CI account), where the registry names it (``leases``); it then takes no CI slot, as a GPU
part takes none. Without ``leases`` a host's cards are its parts' alone, and a GPU run goes
to a cluster target. A host whose ``drain`` file drains it (the CI account's world-readable
copy of its drain state; unreadable or malformed drains it, missing does not) shows the
problem ``drained`` and starts no pool run. A host whose entry names ``runners`` (the CI
account's ``<data>/actions-runner``) gets ``runners_online``, how many of them have a live process,
and with none the problem ``no runner online`` (decision 15.5): brainspike's plan places no
part there, while a pool run, which takes a slot and no runner, ignores it.

**Dev slots** (decision 16.8, R7): a host entry's ``dev_slots`` gives ``pool run`` slots of its
own, outside every CI slot: ``cpus``, one cpuset per dev slot (disjoint, below the entry's
``cpus``), ``memory_gib``, the most a run there may ask (at most the entry's), and their lock
files (``lock``, ``<stem>.<i>.lock`` from the second, created when taken; ``queue``, default
``<stem>.queue``), which may not be the CI slots' files. On such a host a CPU run takes a dev
slot (in line, as a CI slot) and never a CI slot, runs pinned to its cpuset (``taskset``), and
its row shows ``dev_slots``, ``dev_free`` and ``dev_queue``; a GPU run takes the cards' leases
as before. Before it takes one, the host side reads the host's inventory (``inventory``, default
``$CI_RUNNERS_ETC/inventory.toml`` or ``~/data/ci-runners/etc/inventory.toml`` there) where it is
readable, and refuses the run (NOT_STARTED) if a dev slot's cpuset meets a CI slot's (a pool's
``slot.cpus``), the parts' ``cpus`` or ``compile_cpus``. A host without ``dev_slots`` keeps the
claim below. An older release refuses the key (``unknown keys``): it goes into a registry only
once every host that reads that registry has this release and ``v1`` points at it.

**The claim**: a host's capacity is a slot of its CI slot queue, the same ``SlotQueue``
(and files) that the parts' ``ci-slot`` and brainspike's ``ci_slot.sh`` take, first come
first served, within the project's ``max_slots`` there. A run waits in line for it on the
one host it chose to wait on, or with ``--no-wait`` says so (exit 75); a host that looked
free but fills before the take is given up after ``FREE_NOW_ROUNDS`` tries, for the next
machine. It never overbooks. On a cluster the job itself is the claim. It is submitted through the
account's ``ci-slurm-gate``, the one the parts use (the same batch script, supervisor and
env), and withdrawn if it has not started within ``--queue-minutes``.

**The env check**: before a run, the target env's versions of the registry's ``check``
distributions (plus the commit of an editable install) are compared with the caller's.
A difference refuses the run (exit 3) unless ``--env-mismatch warn``. Commits are compared
only where both sides are editable installs.

**pool run** stages the caller's tree (the tracked files and the untracked ones that are
not ignored, as they are on disk, with a small git repository of its HEAD and merge base,
``git_dir``) and the release's ``ci_runners`` on the machine, takes
the claim, and runs the command there under the parts' supervisor (``part.supervise``: its
own session, the timeout, the memory cap). The supervisor is a process of its own that
holds the slot (and the leases) for the command's life, in a systemd scope of its own
(``MemoryMax`` the run's memory) where the user manager can be reached, else in a session
of its own: a host side that is killed leaves the command running with its slot held,
never a running command without one. It streams the output and exits with the command's
code. If the caller ends (a signal, a crash, a closed connection), the remote side sees its
stdin close and stops the supervisor, which ends the command and releases the slot. A
cluster job is cancelled through the gate, or the gate's reaper ends it at its deadline.
A run's directory is removed at its end; one left behind is removed after ``STALE_DAYS``
unless its run still holds its ``alive.lock`` (``--timeout`` and the wait in line are
bounded below that).

CLI::

    ci-run pool status [--json] [--fast]   # --fast: from the capacity files, within 2 s
    ci-run pool probe                    # the local machine's probe, as JSON (a forced command)
    ci-run pool run [--cpu N] [--mem G] [--gpu MODEL[:COUNT]] [--on KEY] [--here]
                    [--timeout MIN] [--queue-minutes MIN] [--no-wait]
                    [--env-mismatch refuse|warn] [--python PATH] [--dir DIR] -- COMMAND...
    ci-run pool add|check|ready KEY ...  # adding a host (ci_runners/pooladd.py)
    python -m ci_runners.pool deny FILE  # hashes of the registry's tokens

Stdlib only.
"""
from __future__ import annotations

import dataclasses
import datetime
import hashlib
import io
import json
import math
import os
import re
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import tomllib
from collections.abc import Callable, Iterable, Mapping, Sequence

SCHEMA = 1
#: s between two readings of a cluster's figures by ``pool status`` (16.5: the login node is shared)
CLUSTER_SECONDS = 300
#: its largest: read again, at the next 30 s tick and within the probe's 90 s, before 600 s (capacity.CLUSTER_STALE_S)
CLUSTER_SECONDS_MAX = 450
#: exit codes of pool run itself (any other is the command's)
EXIT_USAGE, EXIT_ENV, EXIT_NO_ROOM, EXIT_FAILED = 2, 3, 75, 70
#: what a host side or a cluster try returns when it could not start the command: never a
#: command's code (``pool_run`` then tries the next machine and ends EXIT_NO_ROOM)
NOT_STARTED = 211
#: the command's ends that are not an exit code (``exit.json``)
EXIT_TIMEOUT, EXIT_OOM, EXIT_SIGNALLED = 124, 137, 143
#: the caller's variables a run keeps: those that change what a test computes or how it runs
FORWARD = re.compile(r'(JAX_|XLA_|BRAINSPIKE_|PYTEST_|OMP_|MKL_|OPENBLAS_|TF_CPP_).*|CI_TEST_TIMEOUT')
#: what a run's stage directory on a host is called (a stale one is removed after STALE_DAYS)
RUN_PREFIX = 'pool-'
STALE_DAYS = 2
#: a lock file in a run's directory, flocked by the host side and the supervisor for the run's life
ALIVE = 'alive.lock'
#: s between a cluster job's status reads, and the bounds of a run
POLL = 5.0
DEFAULT_TIMEOUT_MIN = 60
#: a run's longest --timeout and wait in line on a host: together below STALE_DAYS
MAX_TIMEOUT_MIN = 20 * 60
LONG_WAIT_MIN = 24 * 60
#: the tries of a host that was free in pool status before a run gives it up for the next machine
FREE_NOW_ROUNDS = 3
#: s the supervisor has to end the command after a stop, before its process group is killed
STOP_GRACE = 30
DEFAULT_QUEUE_MIN = 2  # a cluster job that has not started by then is withdrawn (D-0111: 1 min for the plan)
#: the distributions the env check reads when the registry names none
DEFAULT_CHECK = ('jax', 'jaxlib', 'numpy', 'scipy')

_KEY = re.compile(r'[a-z0-9][a-z0-9_-]{0,39}\Z')
_GPU = re.compile(r'([a-z0-9]+)(?::([1-9][0-9]?))?\Z')


class RegistryError(Exception):
    """The registry is malformed or of an unknown schema."""


@dataclasses.dataclass(frozen=True)
class Target:
    name: str
    partition: str
    cpus: int
    memory_gib: int
    max_minutes: int
    variant: str
    price: float = 0.0
    numerics_ok: bool = False
    labels: Mapping[str, str] = dataclasses.field(default_factory=dict)
    gpu_model: str = ''
    cards: int = 0  # per node
    max_cards: int = 0
    gpu_mib: int = 0
    cuda: str = ''


@dataclasses.dataclass(frozen=True)
class Slots:
    lock: str
    count: int
    queue: str = ''


#: the leases per card a GPU part takes (``part.Leases``' ``max_parts`` there); a registry's must match
LEASE_PARTS = 1


@dataclasses.dataclass(frozen=True)
class DevSlots:
    """A host's dev slots (decision 16.8): one cpuset each, a memory cap, their lock files."""
    lock: str
    cpus: tuple[str, ...]
    memory_gib: int
    queue: str = ''
    inventory: str = ''  # the host's inventory, for the overlap check ('' : the host's default)


#: the most dev slots a host entry may name
DEV_SLOTS_MAX = 64


@dataclasses.dataclass(frozen=True)
class LeaseDir:
    dir: str
    parts: int = 1  # leases per card (the parts' max_parts)


#: the interpreter the host side (the probe, ``remote``) runs on a host whose entry names no
#: ``host_python``: the system's, 3.11 or later on the 3090 hosts (h100's is 3.10, decision 15.8)
HOST_PYTHON = '/usr/bin/python3'
#: the largest ``tier`` of a host entry (decision 19.1)
TIER_MAX = 99


@dataclasses.dataclass(frozen=True)
class Machine:
    key: str
    access: str  # ssh, local or slurm
    ssh: str = ''
    hostname: str = ''
    cpus: int = 0
    memory_gib: int = 0
    gpus: int = 0
    gpu_model: str = ''
    gpu_mib: int = 0
    cuda: str = ''
    p2p: bool = False
    billed: bool = False
    price: float = 0.0
    python: str = ''
    host_python: str = HOST_PYTHON  # the host side's interpreter, 3.11 or later (decision 15.8)
    work: str = ''
    slots: Slots | None = None
    max_slots: int | None = None  # the project's cap on the host's slot queue (None: the slot count)
    drain: str = ''  # the CI account's world-readable drain state
    leases: LeaseDir | None = None  # the cards' leases, shared with the parts
    runners: str = ''  # the CI account's runner directories (decision 15.5)
    labels: Mapping[str, str] = dataclasses.field(default_factory=dict)
    numerics_ok: bool = False
    cache: str = ''
    gate: str = ''
    ready: bool = True  # ``pool add`` writes false, ``pool ready`` true; no key: ready
    targets: Mapping[str, Target] = dataclasses.field(default_factory=dict)
    site: str = ''  # a cluster through its CI key: the site config's site and account (item 8)
    account: str = ''
    probe: str = ''  # 'forced': a host whose key is forced to ``ci-run pool probe`` (item 8)
    tier: int | None = None  # a host's tier, 1 first; None: it neither yields nor is yielded to (19.1)
    dev_slots: DevSlots | None = None  # pool run's own slots, outside every CI slot (16.8); None: CI slots

    @property
    def status_only(self) -> bool:
        """Whether the entry serves ``pool status`` only (a CI account's, item 8)."""
        return bool(self.site) or self.probe == 'forced'

    @property
    def base(self) -> str:
        """A cluster account's ``<root>/ci-runners`` (the gate is ``<base>/releases/<tag>/bin/ci-slurm-gate``)."""
        return os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(self.gate))))


@dataclasses.dataclass(frozen=True)
class Registry:
    project: str
    repository: str
    check: tuple[str, ...]
    machines: Mapping[str, Machine]
    gpu_need_mib: int = 6000
    warm: str = ''  # the project's compile-cache warm-up command (``pool add``'s checks)
    numerics: str = ''  # the project's numerics check command (``pool add``'s checks)
    cluster_seconds: int = CLUSTER_SECONDS  # ``capacity.cluster_seconds``: a cluster's figures' interval (16.5)


def _get(t: Mapping, k: str, kind: type | tuple, where: str, default: object = None, required: bool = False):
    if k not in t:
        if required:
            raise RegistryError(f'{where}: {k} is missing')
        return default
    v = t[k]
    if kind is int and (type(v) is not int or v < 0):
        raise RegistryError(f'{where}: {k} must be a non-negative integer')
    if kind is float and (type(v) not in (int, float) or v < 0):
        raise RegistryError(f'{where}: {k} must be a non-negative number')
    if kind is bool and type(v) is not bool:
        raise RegistryError(f'{where}: {k} must be true or false')
    if kind is str and not isinstance(v, str):
        raise RegistryError(f'{where}: {k} must be a string')
    if kind is dict and not isinstance(v, dict):
        raise RegistryError(f'{where}: {k} must be a table')
    return v


def _abs(t: Mapping, k: str, where: str, required: bool = False) -> str:
    v = _get(t, k, str, where, '', required)
    if v and not os.path.isabs(v):
        raise RegistryError(f'{where}: {k} must be an absolute path')
    return v


def _labels(t: Mapping, where: str) -> dict[str, str]:
    v = _get(t, 'labels', dict, where, {})
    if not all(isinstance(x, str) for x in v.values()):
        raise RegistryError(f'{where}: labels must map a group to a label')
    return dict(v)


_MACHINE = {'access', 'ssh', 'hostname', 'cpus', 'memory_gib', 'gpus', 'gpu_model', 'gpu_mib', 'cuda', 'p2p', 'billed',
            'price', 'python', 'host_python', 'work', 'slots', 'labels', 'numerics_ok', 'cache', 'gate', 'targets', 'ready',
            'max_slots', 'drain', 'leases', 'site', 'account', 'probe', 'runners', 'tier', 'dev_slots'}
_TARGET = {'partition', 'cpus', 'memory_gib', 'max_minutes', 'variant', 'price', 'numerics_ok', 'labels', 'gpu_model',
           'cards', 'max_cards', 'gpu_mib', 'cuda'}
_TOP = {'schema', 'project', 'repository', 'check', 'gpu_need_mib', 'machine', 'warm', 'numerics', 'capacity'}


def _unknown(t: Mapping, known: set[str], where: str) -> None:
    extra = sorted(set(t) - known)
    if extra:
        raise RegistryError(f'{where}: unknown keys {extra}')


def cpus_of(spec: str) -> frozenset[int] | None:
    """The CPUs of a cpuset list (``0-3,8``), or None where it is not one. Pure."""
    from ci_runners import inventory
    ranges = inventory.cpu_ranges(spec)
    return None if ranges is None else frozenset(c for lo, hi in ranges for c in range(lo, hi + 1))


def _dev_slots(t: Mapping, where: str, cpus: int, memory_gib: int, slots: Slots) -> DevSlots:
    """An entry's ``dev_slots`` (decision 16.8), checked against the entry itself: disjoint
    cpusets below its ``cpus``, a cap within its memory, lock files that are not its CI slots'."""
    from ci_runners import slots as slots_mod
    w = f'{where}.dev_slots'
    d = _get(t, 'dev_slots', dict, where)
    _unknown(d, {'lock', 'cpus', 'memory_gib', 'queue', 'inventory'}, w)
    sets = _get(d, 'cpus', list, w, required=True)
    if not sets or len(sets) > DEV_SLOTS_MAX or not all(isinstance(x, str) for x in sets):
        raise RegistryError(f'{w}: cpus lists 1 to {DEV_SLOTS_MAX} cpusets, one per dev slot')
    seen: set[int] = set()
    for x in sets:
        got = cpus_of(x)
        if not got:
            raise RegistryError(f'{w}: {x!r} is not a cpuset')
        if seen & got:
            raise RegistryError(f'{w}: the cpusets overlap (CPUs {sorted(seen & got)})')
        if cpus and max(got) >= cpus:
            raise RegistryError(f'{w}: {x!r} names a CPU beyond the entry\'s {cpus}')
        seen |= got
    mem = _get(d, 'memory_gib', int, w, required=True)
    if not 1 <= mem <= memory_gib:
        raise RegistryError(f'{w}: memory_gib from 1 to the entry\'s {memory_gib}')
    dev = DevSlots(_abs(d, 'lock', w, required=True), tuple(sets), mem, _abs(d, 'queue', w), _abs(d, 'inventory', w))
    ci = slots_mod.slot_paths(slots.lock, slots.count)
    ci_queue = slots.queue or (slots.lock[:-len('.lock')] if slots.lock.endswith('.lock') else slots.lock) + '.queue'
    dev_queue = dev.queue or (dev.lock[:-len('.lock')] if dev.lock.endswith('.lock') else dev.lock) + '.queue'
    if set(ci) & set(slots_mod.slot_paths(dev.lock, len(sets))) or os.path.normpath(ci_queue) == os.path.normpath(dev_queue):
        raise RegistryError(f'{w}: its lock files or queue are the CI slots\' (a dev slot is never a CI slot)')
    return dev


def ci_cpusets(inv) -> list[tuple[str, frozenset[int]]]:
    """The cpusets CI pins its parts to on the inventory's own host (``inv.host``): each
    pool's ``slot.cpus`` (16.2), the parts' ``cpus`` and ``compile_cpus``. Pure."""
    h = inv.host
    out = []
    for k in ('cpus', 'compile_cpus'):
        got = cpus_of(str(h.get(k) or ''))
        if got:
            out.append((k, got))
    for name, v in (h.get('pools') or {}).items():
        slot = v.get('slot') if isinstance(v, dict) else None
        for i, x in enumerate((slot or {}).get('cpus') or []):
            got = cpus_of(str(x))
            if got:
                out.append((f'pools.{name}.slot.cpus[{i}]', got))
    return out


def dev_overlaps(dev_cpus: Sequence[str], inv) -> list[str]:
    """Each dev slot that meets a cpuset CI pins its parts to (``ci_cpusets``): why, per
    pair; empty where none does. Pure."""
    out = []
    ci = ci_cpusets(inv)
    for i, x in enumerate(dev_cpus):
        mine = cpus_of(x) or frozenset()
        for name, theirs in ci:
            if mine & theirs:
                out.append(f'dev slot {i + 1} (CPUs {x}) meets the inventory\'s {name} (CPUs {sorted(mine & theirs)})')
    return out


def _target(name: str, t: Mapping, where: str) -> Target:
    _unknown(t, _TARGET, where)
    gpu = _get(t, 'gpu_model', str, where, '')
    tg = Target(name, _get(t, 'partition', str, where, required=True), _get(t, 'cpus', int, where, required=True),
                _get(t, 'memory_gib', int, where, required=True), _get(t, 'max_minutes', int, where, required=True),
                _get(t, 'variant', str, where, required=True), float(_get(t, 'price', float, where, 0.0)),
                _get(t, 'numerics_ok', bool, where, False), _labels(t, where), gpu,
                _get(t, 'cards', int, where, 0, required=bool(gpu)), _get(t, 'max_cards', int, where, 0, required=bool(gpu)),
                _get(t, 'gpu_mib', int, where, 0), _get(t, 'cuda', str, where, ''))
    if not tg.cpus or not tg.max_minutes:
        raise RegistryError(f'{where}: cpus and max_minutes must be positive')
    return tg


def _machine(key: str, t: Mapping) -> Machine:
    where = f'machine.{key}'
    if not _KEY.fullmatch(key):
        raise RegistryError(f'{where}: a key is [a-z0-9][a-z0-9_-]*')
    if not isinstance(t, dict):
        raise RegistryError(f'{where}: must be a table')
    _unknown(t, _MACHINE, where)
    access = _get(t, 'access', str, where, required=True)
    if access not in ('ssh', 'local', 'slurm'):
        raise RegistryError(f'{where}: access is ssh, local or slurm')
    site, account = _get(t, 'site', str, where, ''), _get(t, 'account', str, where, '')
    if (site or account) and (access != 'slurm' or not (_KEY.fullmatch(site) and _KEY.fullmatch(account))
                              or 'ssh' in t or 'gate' in t):
        raise RegistryError(f'{where}: site and account name a cluster\'s site config and account, both, '
                            'instead of ssh and gate')
    probe_kind = _get(t, 'probe', str, where, '')
    if probe_kind and (probe_kind != 'forced' or access != 'ssh'):
        raise RegistryError(f'{where}: probe is "forced", on an ssh machine')
    if access in ('ssh', 'slurm') and not site and not _get(t, 'ssh', str, where, ''):
        raise RegistryError(f'{where}: an {access} machine needs its ssh alias')
    tier = _get(t, 'tier', int, where)
    if tier is not None and (access == 'slurm' or not 1 <= tier <= TIER_MAX):
        raise RegistryError(f'{where}: tier is an integer from 1 to {TIER_MAX}, on a host (decision 19.1)')
    if 'dev_slots' in t and access == 'slurm':
        raise RegistryError(f'{where}: dev_slots on a host only (a cluster job is its own claim)')
    m = dict(key=key, access=access, ssh=_get(t, 'ssh', str, where, ''), hostname=_get(t, 'hostname', str, where, ''),
             billed=_get(t, 'billed', bool, where, access == 'slurm'), price=float(_get(t, 'price', float, where, 0.0)),
             labels=_labels(t, where), numerics_ok=_get(t, 'numerics_ok', bool, where, False),
             cache=_abs(t, 'cache', where), ready=_get(t, 'ready', bool, where, True), site=site, account=account,
             probe=probe_kind, tier=tier)
    if access == 'slurm':
        targets = _get(t, 'targets', dict, where, required=True)
        if not targets:
            raise RegistryError(f'{where}: a cluster needs a target')
        bad = [n for n, v in targets.items() if not _KEY.fullmatch(n) or not isinstance(v, dict)]
        if bad:
            raise RegistryError(f'{where}: malformed targets {bad}')
        m.update(gate=_abs(t, 'gate', where, required=not site),
                 targets={n: _target(n, v, f'{where}.targets.{n}') for n, v in targets.items()})
        return Machine(**m)
    s = _get(t, 'slots', dict, where, required=True)
    _unknown(s, {'lock', 'count', 'queue'}, f'{where}.slots')
    slots = Slots(_abs(s, 'lock', f'{where}.slots', required=True), _get(s, 'count', int, f'{where}.slots', required=True),
                  _abs(s, 'queue', f'{where}.slots'))
    if slots.count < 1:
        raise RegistryError(f'{where}.slots: count must be positive')
    gpus = _get(t, 'gpus', int, where, 0)
    leases = None
    if 'leases' in t:
        lt = _get(t, 'leases', dict, where)
        _unknown(lt, {'dir', 'parts'}, f'{where}.leases')
        leases = LeaseDir(_abs(lt, 'dir', f'{where}.leases', required=True), _get(lt, 'parts', int, f'{where}.leases', 1))
        if leases.parts != LEASE_PARTS or not gpus:  # another count would let a run share a card a part holds
            raise RegistryError(f'{where}.leases: parts must be {LEASE_PARTS} (the parts\' leases per card), '
                                'on a machine with gpus')
    max_slots = _get(t, 'max_slots', int, where)
    if max_slots is not None and not 1 <= max_slots <= slots.count:
        raise RegistryError(f'{where}.max_slots: from 1 to the slot count ({slots.count})')
    m.update(max_slots=max_slots, drain=_abs(t, 'drain', where), leases=leases, runners=_abs(t, 'runners', where))
    if 'dev_slots' in t and m['probe'] == 'forced':
        raise RegistryError(f'{where}: dev_slots on an entry for pool status only (probe = "forced")')
    m.update(dev_slots=_dev_slots(t, where, _get(t, 'cpus', int, where, required=True),
                                  _get(t, 'memory_gib', int, where, required=True), slots) if 'dev_slots' in t else None)
    m.update(cpus=_get(t, 'cpus', int, where, required=True), memory_gib=_get(t, 'memory_gib', int, where, required=True),
             gpus=gpus, gpu_model=_get(t, 'gpu_model', str, where, '', required=bool(gpus)),
             gpu_mib=_get(t, 'gpu_mib', int, where, 0), cuda=_get(t, 'cuda', str, where, ''),
             p2p=_get(t, 'p2p', bool, where, False), python=_abs(t, 'python', where, required=True),
             host_python=_abs(t, 'host_python', where) or HOST_PYTHON, work=_abs(t, 'work', where, required=True),
             slots=slots)
    return Machine(**m)


def load(text: str) -> Registry:
    """Parse and validate ``pool.toml``. Raises RegistryError. Pure."""
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise RegistryError(f'not TOML: {e}') from None
    if doc.get('schema') != SCHEMA:
        raise RegistryError(f'unknown schema {doc.get("schema")!r} (this release reads {SCHEMA})')
    _unknown(doc, _TOP, 'pool.toml')
    check = _get(doc, 'check', list, 'pool.toml', list(DEFAULT_CHECK))
    if not all(isinstance(x, str) and x for x in check):
        raise RegistryError('pool.toml: check lists distribution names')
    machines = _get(doc, 'machine', dict, 'pool.toml', {})
    cap = _get(doc, 'capacity', dict, 'pool.toml', {})
    _unknown(cap, {'cluster_seconds'}, 'pool.toml: capacity')
    cluster_seconds = _get(cap, 'cluster_seconds', int, 'pool.toml: capacity', CLUSTER_SECONDS)
    if not 30 <= cluster_seconds <= CLUSTER_SECONDS_MAX:
        raise RegistryError(f'pool.toml: capacity.cluster_seconds must lie from 30 to {CLUSTER_SECONDS_MAX}')
    return Registry(_get(doc, 'project', str, 'pool.toml', required=True),
                    _get(doc, 'repository', str, 'pool.toml', required=True), tuple(check),
                    {k: _machine(k, v) for k, v in machines.items()}, _get(doc, 'gpu_need_mib', int, 'pool.toml', 6000),
                    _get(doc, 'warm', str, 'pool.toml', ''), _get(doc, 'numerics', str, 'pool.toml', ''),
                    cluster_seconds)


def load_file(path: str) -> Registry:
    """Load the registry at ``path``; one that others than its owner may write is refused."""
    st = os.stat(path)
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise RegistryError(f'{path}: writable by others (chmod 600)')
    with open(path, encoding='utf-8') as f:
        return load(f.read())


#: the keys whose values are inventory (``deny``); the rest are capabilities and role names. The
#: slot files are not: they are the shared files brainspike's scripts name (a home-directory
#: path among them is still the lint's). ``drain`` and the leases' ``dir`` are the CI account's
PRIVATE_KEYS = ('ssh', 'hostname', 'python', 'host_python', 'work', 'gate', 'cache', 'partition', 'drain', 'dir', 'runners')


def deny_tokens(doc: Mapping) -> set[str]:
    """The registry's tokens the repository must not hold: the values of PRIVATE_KEYS, split
    as the inventory lint splits them (``inventory.deny_tokens``). Pure."""
    from ci_runners import inventory
    vals: list[str] = []

    def walk(t):
        for k, v in t.items():
            if isinstance(v, dict):
                walk(v)
            elif k in PRIVATE_KEYS and isinstance(v, str):
                vals.append(v)
    walk(doc)
    return inventory.deny_tokens({'values': vals})


# ------------------------------------------------------------------ pool status


def drained(path: str) -> bool:
    """Whether the drain state at ``path`` (the CI account's world-readable copy,
    ``drain.public_path``) drains the host, as ``drain.read_path`` and ``drain.parse`` read
    it: a missing file does not, an unreadable or malformed one does (fail closed). A
    drained pool of runners does not drain the pool runs. Self-contained (its source runs on
    the host, in ``probe``). Stdlib."""
    import json
    import os
    import stat
    if not path:
        return False
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    try:
        with os.fdopen(fd, 'rb') as f:
            st = os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_size > 1 << 16:
                return True
            doc = json.loads(f.read(1 << 16).decode('utf-8', 'replace'))
    except (OSError, ValueError):
        return True
    num = (int, float)
    if not isinstance(doc, dict) or set(doc) != {'host', 'pools'} or not isinstance(doc['pools'], dict):
        return True
    if not all(isinstance(k, str) and type(v) in num for k, v in doc['pools'].items()):
        return True
    return doc['host'] is not None  # a number: drained since then; anything else is malformed


#: the problem of a host none of whose runners has a live process: its parts would wait in
#: GitHub's queue, so brainspike's plan places none there; a pool run needs no runner (15.5)
NO_RUNNER = 'no runner online'


def runner_procs(root: str, proc: str = '/proc') -> 'tuple[set[str], set[str]] | None':
    """The names of the runners under ``root`` (``<data>/actions-runner``) with a live
    process, and of those running a job, read from each process's ``cmdline`` without
    taking anything. Alive: a listener
    (``argv[0]`` ``<root>/<name>/bin/Runner.Listener``) or its helper (``argv[1]``
    ``<root>/<name>/run-helper.sh``, after its shell), which outlives the listener's
    self-update (decision 15.5). Busy: a worker (``argv[0]``
    ``<root>/<name>/bin/Runner.Worker``), which runs the runner's job (16.5). ``root`` counts as written and as its real path (the
    runner's scripts start from their physical directory), and where its owner is known
    only that account's processes count. None when it cannot be known: ``proc`` hides
    other accounts' processes (``hidepid``) and ``root`` is not the caller's.
    Self-contained (its source runs on the host). Stdlib."""
    import os
    prefixes = {r.rstrip('/') + '/' for r in (root, os.path.realpath(root))}
    try:
        owner = os.stat(root).st_uid
    except OSError:
        owner = None
    try:
        with open(os.path.join(proc, 'self', 'mountinfo'), errors='replace') as f:
            hidden = any(' /proc ' in line and 'hidepid=' in line and not any(
                f'hidepid={v}' in line for v in ('0', 'off')) for line in f)
    except OSError:
        hidden = False
    if hidden and owner != os.getuid():
        return None
    names, busy = set(), set()
    try:
        pids = [p for p in os.listdir(proc) if p.isdigit()]
    except OSError:
        return None
    for pid in pids:
        try:
            if owner is not None and os.stat(os.path.join(proc, pid)).st_uid != owner:
                continue  # another account's process naming the path
            with open(os.path.join(proc, pid, 'cmdline'), 'rb') as f:
                argv = f.read(8192).split(b'\0')
        except OSError:
            continue  # gone, or hidden
        for i, tail, into in ((0, ['bin', 'Runner.Listener'], names), (1, ['run-helper.sh'], names),
                              (0, ['bin', 'Runner.Worker'], busy)):
            path = argv[i].decode('utf-8', 'replace') if len(argv) > i else ''
            for prefix in prefixes:
                rest = path[len(prefix):].split('/') if path.startswith(prefix) else []
                if rest and rest[0] and rest[1:] == tail:
                    into.add(rest[0])
    return names, busy & names


def runner_listening(root: str, proc: str = '/proc') -> 'set[str] | None':
    """The names of the runners under ``root`` whose listener (``argv[0]``
    ``<root>/<name>/bin/Runner.Listener``) holds an established TCP connection, decision
    19.2: one of its sockets (``<proc>/<pid>/fd``) among the established ones (state
    ``01``) of its network namespace's ``net/tcp`` and ``net/tcp6``. A listener keeps its
    connection between two long polls; one whose session failed, or that waits to retry,
    holds none. Only ``root``'s owner's processes count (as ``runner_procs``); a process
    whose descriptors cannot be read is not listening. None where the processes cannot be
    listed. Self-contained (its source runs on the host). Stdlib."""
    import os
    prefixes = {x.rstrip('/') + '/' for x in (root, os.path.realpath(root))}
    try:
        owner = os.stat(root).st_uid
    except OSError:
        owner = None
    try:
        pids = [p for p in os.listdir(proc) if p.isdigit()]
    except OSError:
        return None
    found, tables = set(), {}
    for pid in pids:
        try:
            uid = os.stat(os.path.join(proc, pid)).st_uid
            if owner is not None and uid != owner:  # another account's process
                continue
            with open(os.path.join(proc, pid, 'cmdline'), 'rb') as f:
                arg0 = f.read(8192).split(b'\0')[0].decode('utf-8', 'replace')
        except OSError:
            continue
        name = ''
        for prefix in prefixes:
            rest = arg0[len(prefix):].split('/') if arg0.startswith(prefix) else []
            if len(rest) == 3 and rest[0] and rest[1:] == ['bin', 'Runner.Listener']:
                name = rest[0]
        if not name or name in found:
            continue
        try:
            ns = os.readlink(os.path.join(proc, pid, 'ns', 'net'))
        except OSError:
            ns = 'pid ' + pid
        if ns not in tables:
            est = set()
            for t in ('tcp', 'tcp6'):
                try:
                    with open(os.path.join(proc, pid, 'net', t)) as f:
                        for line in f:
                            c = line.split()
                            if len(c) > 9 and c[3] == '01':
                                est.add(c[9])
                except OSError:
                    pass
            tables[ns] = est
        try:
            fds = os.listdir(os.path.join(proc, pid, 'fd'))
        except OSError:
            continue
        for fd in fds:
            try:
                link = os.readlink(os.path.join(proc, pid, 'fd', fd))
            except OSError:
                continue
            if link.startswith('socket:[') and link.endswith(']') and link[8:-1] in tables[ns]:
                found.add(name)
                break
    return found


def runners_alive(root: str, proc: str = '/proc') -> 'set[str] | None':
    """The names of the runners under ``root`` with a live process (``runner_procs``), or
    None where it cannot be known (decision 15.5)."""
    got = runner_procs(root, proc)
    return None if got is None else got[0]


def probe(lock: str, count: int, queue: str, drain: str = '', leases: str = '', parts: int = 1, gpus: int = 0,
          runners: str = '', smi: bool = True, dev_lock: str = '', dev_count: int = 0, dev_queue: str = '') -> dict:
    """What a host's row needs, read on the host without taking anything: its free CI slots
    (``/proc/locks``, as ``slots._flocked``), the tickets waiting, the load, the available
    memory, the cards' free MiB, whether ``drain`` drains it, with ``leases`` (the
    leases' directory, ``parts`` a card) the leases held per card, and with ``runners``
    how many of the CI account's runners have a live process (decision 15.5), their names
    and those running a job (16.5) and those whose listener holds a connection
    (19.2), and with ``dev_count`` the dev slots free (a missing lock file is free: a take
    creates it) and waiting (16.8). Without ``smi`` the cards are not read (no
    ``nvidia-smi``: ``pool status --fast``). Self-contained (its source, ``drained``'s and
    ``runner_procs``'s run on the host). Stdlib."""
    import os
    import subprocess

    def flocked(path):
        try:
            st = os.stat(path)
            with open('/proc/locks') as f:
                for line in f:
                    p = line.split()
                    if len(p) >= 6 and p[1] == 'FLOCK':
                        maj, mnr, ino = p[5].split(':')
                        if int(ino) == st.st_ino and os.makedev(int(maj, 16), int(mnr, 16)) == st.st_dev:
                            return True
        except (OSError, ValueError, IndexError):
            pass
        return False
    stem = lock[:-len('.lock')] if lock.endswith('.lock') else lock
    paths = [lock] + [f'{stem}.{i}.lock' for i in range(2, count + 1)]
    out = {'slots_free': sum(1 for p in paths if os.path.exists(p) and not flocked(p)),
           'slots_missing': sum(1 for p in paths if not os.path.exists(p))}
    try:
        out['queue'] = sum(1 for n in os.listdir(queue or stem + '.queue') if n.endswith('.ticket'))
    except OSError:
        out['queue'] = 0
    if dev_count:
        dstem = dev_lock[:-len('.lock')] if dev_lock.endswith('.lock') else dev_lock
        dpaths = [dev_lock] + [f'{dstem}.{i}.lock' for i in range(2, dev_count + 1)]
        out['dev_free'] = sum(1 for p in dpaths if not flocked(p))
        try:
            out['dev_queue'] = sum(1 for n in os.listdir(dev_queue or dstem + '.queue') if n.endswith('.ticket'))
        except OSError:
            out['dev_queue'] = 0
    with open('/proc/loadavg') as f:
        out['load'] = float(f.read().split()[0])
    out['cpus'] = os.cpu_count() or 0
    with open('/proc/meminfo') as f:
        for line in f:
            if line.startswith('MemAvailable:'):
                out['mem_free_gib'] = int(line.split()[1]) // (1 << 20)
    try:
        if smi:
            r = subprocess.run(['nvidia-smi', '--query-gpu=index,memory.free', '--format=csv,noheader,nounits'],
                               capture_output=True, text=True, timeout=30)
            out['gpu_free'] = {int(a): int(b) for a, b in (x.split(',') for x in r.stdout.split('\n') if x.strip())} \
                if r.returncode == 0 else {}
    except (OSError, subprocess.SubprocessError, ValueError):
        out['gpu_free'] = {}
    rt = os.environ.get('XDG_RUNTIME_DIR') or f'/run/user/{os.getuid()}'
    out['scope'] = os.path.exists(os.path.join(rt, 'bus'))  # systemd-run --user works
    out['drained'] = drained(drain)
    if leases:
        out['cards_leased'] = {c: sum(1 for k in range(parts) if flocked(os.path.join(leases, f'card{c}.{k}.lock')))
                               for c in range(gpus)}
    if runners:
        got = runner_procs(runners)
        out['runners'] = None if got is None else len(got[0])
        if got is not None:
            out['runner_names'], out['runner_busy'] = sorted(got[0]), sorted(got[1])
            heard = runner_listening(runners)
            if heard is not None:  # 19.2: an idle runner that GitHub can reach
                out['runner_listening'] = sorted(heard & got[0])
    return out


def _probe_src() -> str:
    import inspect
    return '\n\n'.join(inspect.getsource(f) for f in (drained, runner_procs, runner_listening, probe))


def runner_labels(etc: str = '') -> dict[str, int] | None:
    """The runner instances per label that this account's inventory (``<etc>/inventory.toml``)
    registers, by setup's own rule (``setup.runners``, design §5.1): each instance once for
    each label it carries, shared labels included (decision 15.7). None where the inventory
    is missing or does not load (a maintainer's registry has none beside it)."""
    rs = _inventory_runners(etc)
    if rs is None:
        return None
    out: dict[str, int] = {}
    for _, labels in rs:
        for label in set(labels):
            out[label] = out.get(label, 0) + 1
    return out


def _inventory(etc: str):
    """This account's inventory (``inventory.Inventory``), or None where it is missing or
    does not load."""
    from ci_runners import inventory
    try:
        with open(os.path.join(_etc(etc), 'inventory.toml'), encoding='utf-8') as f:
            return inventory.load(f.read())
    except (OSError, ValueError):  # InventoryError is a ValueError
        return None


def _runners_of(inv) -> list[tuple[str, list[str], str]] | None:
    from ci_runners import setup
    try:
        return setup.runners(inv)
    except ValueError:  # two runners of one name
        return None


def _inventory_runners(etc: str) -> list[tuple[str, list[str]]] | None:
    inv = _inventory(etc)
    rs = None if inv is None else _runners_of(inv)
    return None if rs is None else [(n, labels) for n, labels, _ in rs]


def _known(p: Mapping) -> tuple[bool, set[str], set[str]]:
    """Whether the probe ``p`` knows its runners, and their names alive and busy."""
    alive, busy = p.get('runner_names'), p.get('runner_busy')
    known = isinstance(alive, list) and isinstance(busy, list)
    return known, (set(alive) if known else set()), (set(busy) if known else set())


def _label_counts(rs: Sequence[tuple[str, list[str]]], p: Mapping) -> dict[str, dict]:
    known, alive, busy = _known(p)
    out: dict[str, dict] = {}
    for name, labels in rs:
        for label in set(labels):
            c = out.setdefault(label, {'registered': 0, 'online': 0 if known else None, 'free': 0 if known else None})
            c['registered'] += 1
            if known and name in alive:
                c['online'] += 1
                c['free'] += name not in busy
    return out


def label_runners(etc: str, p: Mapping) -> dict[str, dict] | None:
    """Each label's runner instances that this account's inventory registers (as
    ``runner_labels``), with how many of them are online and free by the probe ``p``'s
    names (``runner_names``: a live process; ``runner_busy``: running a job), decision
    16.5: ``{label: {"registered": n, "online": n, "free": n}}``, ``online`` and ``free``
    None where the probe does not know them. None where the inventory does not load."""
    rs = _inventory_runners(etc)
    return None if rs is None else _label_counts(rs, p)


def pool_table(inv, p: Mapping, yielded: set[str] | None = None) -> list[dict]:
    """One entry per runner pool of the inventory ``inv``'s host (decision 19.4): ``pool``,
    ``class``, ``labels`` (those every runner of the pool carries, in the pool's order: a
    per-card runner's ``<role>-gpu<N>`` is not among them where the pool has two cards or
    more), ``registered``, and ``online`` and ``free`` by the probe ``p``'s names (as
    ``label_runners``; None where it does not know them); ``listening``, its free runners
    whose listener holds a connection (``runner_listening``), where the probe read them;
    ``yielded``, its runners that are not online and that presence holds offline for the
    ``tier`` cause alone (19.2, 19.3), where ``yielded`` (their names) is known; a card
    runner (``gpu``, ``multi``) one of whose cards holds a lease (``cards_leased``, our own
    parts' too) is not listening: it cannot start a job now; a pool with
    a ``slot`` table adds its ``memory_gib``, ``speed`` and ``workers`` (16.2). Pure."""
    from ci_runners import inventory, registry
    known, alive, busy = _known(p)
    heard = p.get('runner_listening')
    heard = set(heard) if known and isinstance(heard, list) else None
    rs = _runners_of(inv) or []
    leased = {str(c) for c, n in (p.get('cards_leased') or {}).items() if n} if isinstance(p.get('cards_leased'), dict) \
        else set()
    held = set()
    if leased:
        from ci_runners import presence
        held = {r.name for r in presence.runners(inv) if {str(c) for c in r.cards} & leased}
    out = []
    for name, v in inv.host['pools'].items():
        cls = inventory.pool_class(inv.host, name)
        mine = [r for r in rs if r[2] == cls]
        common = [x for x in dict.fromkeys(mine[0][1]) if all(x in r[1] for r in mine[1:])] if mine else []
        e = {'pool': name, 'class': cls, 'labels': common, 'registered': len(mine),
             'online': sum(r[0] in alive for r in mine) if known else None,
             'free': sum(r[0] in alive and r[0] not in busy for r in mine) if known else None}
        if heard is not None:
            e['listening'] = sum(r[0] in alive and r[0] not in busy and r[0] in heard and r[0] not in held for r in mine)
        if yielded is not None and known:
            e['yielded'] = sum(r[0] in yielded and r[0] not in alive for r in mine)
        slot = v.get('slot') if isinstance(v, dict) else None
        if isinstance(slot, dict):
            e.update(memory_gib=round(registry.memory_bytes(slot['memory']) / (1 << 30), 3), speed=float(slot['speed']),
                     workers=int(slot['workers']))
        out.append(e)
    return out


#: the most of a policy file the probe reads (the hook's own bound, ``hook.MAX_POLICY``)
POLICY_MAX = 1 << 20


def policy_digests(etc: str) -> dict | None:
    """This account's hook policy (``<etc>/policy.toml``) as the capacity row shows it
    (decision 19.6): ``file``, the first 12 hex digits of its SHA-256 (the hook's
    ``policy=``), and ``repositories``, those of its repository entries
    (``policy.repositories_digest``); ``{"error": ...}`` where it does not load (the hook
    then refuses every job); None where there is none (a maintainer's account)."""
    from ci_runners import policy
    try:
        with open(os.path.join(_etc(etc), 'policy.toml'), 'rb') as f:
            raw = f.read(POLICY_MAX + 1)
    except FileNotFoundError:
        return None
    except OSError as e:
        return {'error': f'unreadable ({type(e).__name__})'}
    try:
        if len(raw) > POLICY_MAX:
            raise policy.PolicyError('too large')
        pol = policy.load(raw)
    except policy.PolicyError:
        return {'error': 'does not load'}
    return {'file': pol.digest[:12], 'repositories': policy.repositories_digest(pol)[:12]}


def host_health(inv, etc: str) -> str:
    """Why the host's health gate fails (the data directory's free space below
    ``disk_min_gib``, the CI account's access to the shared files; presence's reading,
    16.4), or ''. A reading that fails proves nothing: ''."""
    from ci_runners import presence
    try:
        return presence.Probe(inv, etc=etc).health()
    except Exception:  # noqa: BLE001 - no proven miss: the part's own gate is the backstop
        return ''


def _with_inventory(p: dict, etc: str) -> dict:
    """``p`` with this account's ``runner_labels`` and ``label_runners``, where its inventory
    loads, and from decision 19.4 its ``role``, ``pools``, ``policy`` and ``health``."""
    inv = _inventory(etc)
    rs = None if inv is None else _runners_of(inv)
    if rs is None:
        return p
    pairs = [(n, labels) for n, labels, _ in rs]
    counts: dict[str, int] = {}
    for _, labels in pairs:
        for label in set(labels):
            counts[label] = counts.get(label, 0) + 1
    p['runner_labels'] = counts
    p['label_runners'] = _label_counts(pairs, p)
    p['role'] = inv.this
    p['pools'] = pool_table(inv, p, yielded_runners(inv))
    hold = holds_of(inv)
    if hold is not None:
        p['hold_until'], p['holds'] = hold
    pol = policy_digests(etc)
    if pol is not None:
        p['policy'] = pol
    health = host_health(inv, etc)
    if health:
        p['health'] = health
    return p


def holds_of(inv, now: float | None = None) -> tuple[float, int] | None:
    """The host's hold (decision 19.8): ``(the latest until, how many holders)``, or None
    where none is active or the marker cannot be read."""
    from ci_runners import presence
    try:
        held = presence.read_holds(str(inv.host['data']), time.time() if now is None else now)
    except Exception:  # noqa: BLE001 - not known: no hold shown
        return None
    return (max(v['until'] for v in held.values()), len(held)) if held else None


def yielded_runners(inv) -> set[str] | None:
    """The runners presence holds offline for the ``tier`` cause alone (decision 19.2), from
    its state; None where the state cannot be read."""
    from ci_runners import presence
    try:
        st, ok = presence.read_state(str(inv.host['data']))
    except Exception:  # noqa: BLE001 - not known: no yielded count
        return None
    if not ok:
        return None
    return {n for n, v in st['runners'].items() if isinstance(v, dict) and v.get('stopped') and v.get('yielded')}


def _local(reg: Registry) -> Machine:
    local = [m for m in reg.machines.values() if m.access == 'local']
    if len(local) != 1:
        raise RegistryError(f'pool probe: {len(local)} local machines in the registry, not one')
    return local[0]


def live_probe(m: Machine, etc: str = '') -> dict:
    """The local machine ``m``'s probe, read now, with this account's runners per label."""
    lz = m.leases or LeaseDir('', 1)
    return _with_inventory(probe(m.slots.lock, m.slots.count, m.slots.queue, m.drain, lz.dir, lz.parts, m.gpus,
                                 m.runners), etc)


def local_probe(reg: Registry, etc: str = '', now: Callable[[], float] | None = None) -> dict:
    """``ci-run pool probe``: the probe of the registry's one ``local`` machine, what a
    forced key's caller reads (item 8), with this account's ``runner_labels`` and
    ``label_runners`` where its inventory loads (15.7, 16.5). While the account's capacity
    file holds a fresh probe of it (``capacity.fresh_local``), that one, with its ``age``,
    and nothing is run (no ``nvidia-smi``, 16.5). Takes no input."""
    from ci_runners import capacity
    m = _local(reg)
    cached = capacity.fresh_local(capacity.path(_etc(etc)), m.key, (now or time.time)())
    return cached if cached is not None else live_probe(m, etc)


def record_local(reg: Registry, etc: str = '', p: dict | None = None, here: str | None = None,
                 now: Callable[[], float] = time.time) -> dict:
    """Write the local machine's row and probe ``p`` (read now by default) into the
    account's capacity file, the others' rows kept: presence's hook at each tick (16.4,
    16.5). Returns the row."""
    from ci_runners import capacity
    m = _local(reg)
    p = live_probe(m, etc) if p is None else p
    t = round(now(), 3)
    row = dict(host_row(m, p, here=socket.gethostname() if here is None else here), read_at=t)
    capacity.merge(capacity.path(_etc(etc)), [row], list(reg.machines), {'key': m.key, 'read_at': t, 'probe': p}, now)
    return row


def host_row(m: Machine, p: Mapping | None, why: str = '', here: str = '') -> dict:
    """A host's row (``HostState`` fields and ours) from its probe ``p``, or with ``why`` it
    could not be read. ``cpus_free``: the cores the load leaves, 0 while no slot is free (a
    run would wait in line); ``runners_online``: the probe's count of live runners;
    ``runners``: its ``runner_labels``, where it has a well-formed one. Pure."""
    row = _base_row(m.key, m, here)
    row.update(gpu_model=m.gpu_model, cuda=m.cuda, multi_gpu=m.p2p, price=m.price, slots=m.slots.count if m.slots else 0)
    if m.dev_slots:
        row.update(dev_slots=len(m.dev_slots.cpus), dev_free=0, dev_queue=0)
    if m.tier is not None:
        row['tier'] = m.tier  # the reading registry's tier (19.1)
    if p is None:
        row['problems'] = [why or 'not read']
        return row
    if p.get('slots_missing'):
        row['problems'].append(f'{p["slots_missing"]} slot lock file(s) missing')
    if p.get('drained'):
        row['problems'].append('drained')
    if p.get('runners') is not None:  # a forced probe counts what its host's own registry names (decision 15.5)
        row['runners_online'] = int(p['runners'])
        if not row['runners_online']:
            row['problems'].append(NO_RUNNER)
    labels = p.get('runner_labels')  # the host's own inventory's instances per label (decision 15.7)
    if isinstance(labels, dict) and all(isinstance(k, str) and type(v) is int and v >= 0 for k, v in labels.items()):
        row['runners'] = dict(labels)
    table = p.get('label_runners')  # and how many are online and free (16.5)
    if isinstance(table, dict) and all(isinstance(k, str) and _counts(v) for k, v in table.items()):
        row['label_runners'] = {k: dict(v) for k, v in table.items()}
    if isinstance(p.get('role'), str) and p['role']:  # 19.4: the host's role, its pools, policy and health
        row['role'] = p['role']
    pools = p.get('pools')
    if isinstance(pools, list) and all(_pool_entry(x) for x in pools):
        row['pools'] = [dict(x) for x in pools]
    pol = p.get('policy')
    if isinstance(pol, dict):
        row['policy'] = {k: pol[k] for k in ('file', 'repositories', 'error') if isinstance(pol.get(k), str)}
        if 'error' in row['policy']:
            row['problems'].append('policy: does not load')
    if type(p.get('hold_until')) in (int, float) and type(p.get('holds')) is int and p['holds'] > 0:
        row['hold_until'], row['holds'] = p['hold_until'], p['holds']  # 19.8
    if isinstance(p.get('health'), str) and p['health']:
        row['problems'].append(p['health'] if p['health'].startswith('health') else f'health: {p["health"]}')
    free = int(p.get('slots_free', 0))
    cores = max(0, (m.cpus or int(p.get('cpus', 0))) - math.ceil(float(p.get('load', 0.0))))
    gpu_free = {int(k): int(v) for k, v in p.get('gpu_free', {}).items()}
    if m.leases or 'cards_leased' in p:  # a card whose every lease is held is not free, whatever its memory
        # (a forced probe reads the leases its host's own registry names, whatever the caller's entry says)
        parts = m.leases.parts if m.leases else LEASE_PARTS
        leased = {int(k): int(v) for k, v in p.get('cards_leased', {}).items()}
        gpu_free = {c: v for c, v in gpu_free.items() if leased.get(c, 0) < parts}
    row.update(slots_free=free, queue=int(p.get('queue', 0)), cpus_free=cores if free else 0,
               mem_free_gib=int(p.get('mem_free_gib', 0)), gpu_free=gpu_free, scope=bool(p.get('scope')))
    if m.dev_slots:  # 16.8: pool run's own slots; the CI figures above stay the CI slots'
        row.update(dev_slots=len(m.dev_slots.cpus), dev_free=int(p.get('dev_free', 0)), dev_queue=int(p.get('dev_queue', 0)))
    return row


def _pool_entry(v: object) -> bool:
    """A well-formed ``pools`` entry (``pool_table``): names, its labels, its counts as
    ``_counts`` and, where present, a count ``listening`` and ``yielded`` (an int >= 0) and
    a positive ``memory_gib``, ``speed`` and ``workers``."""
    if not isinstance(v, dict) or not isinstance(v.get('pool'), str) or not isinstance(v.get('class'), str):
        return False
    if not isinstance(v.get('labels'), list) or not all(isinstance(x, str) for x in v['labels']):
        return False
    if not _counts({k: v.get(k) for k in ('registered', 'online', 'free')}):
        return False
    if not all(type(v[k]) is int and v[k] >= 0 for k in ('listening', 'yielded') if k in v):
        return False
    return all(type(v[k]) in (int, float) and math.isfinite(v[k]) and v[k] > 0
               for k in ('memory_gib', 'speed', 'workers') if k in v)


def _counts(v: object) -> bool:
    """A well-formed ``label_runners`` entry: registered >= online >= free >= 0, the last two
    both None where not known."""
    if not isinstance(v, dict) or set(v) != {'registered', 'online', 'free'} or type(v['registered']) is not int:
        return False
    if v['online'] is None and v['free'] is None:
        return v['registered'] >= 0
    return all(type(v[k]) is int for k in ('online', 'free')) and v['registered'] >= v['online'] >= v['free'] >= 0


def _base_row(key: str, m: Machine, here: str) -> dict:
    return {'key': key, 'name': key, 'access': m.access, 'labels': dict(m.labels), 'problems': [], 'queue': 0,
            'gpu_free': {}, 'multi_gpu': False, 'gpu_model': '', 'cuda': '', 'cpus_free': 0, 'billed': m.billed,
            'price': m.price, 'target': '', 'numerics_ok': m.numerics_ok, 'start_now': True, 'slots_free': 0,
            'slots': 0, 'mem_free_gib': 0, 'here': bool(here) and m.hostname == here}


@dataclasses.dataclass(frozen=True)
class Node:
    name: str
    partition: str
    state: str
    cpus_idle: int
    mem_free_mib: int
    cards_free: int


def _gres_count(gres: str) -> int:
    n = 0
    for part in gres.split(','):
        part = part.split('(')[0]
        if part.startswith('gpu'):
            tail = part.rsplit(':', 1)[-1]
            n += int(tail) if tail.isdigit() else 0
    return n


def parse_sinfo(text: str) -> list[Node]:
    """``sinfo -h -N -O NodeHost:|,Partition:|,StateCompact:|,CPUsState:|,Memory:|,AllocMem:|,Gres:|,GresUsed:|``.
    A line that does not parse is dropped. Pure."""
    out = []
    for line in text.splitlines():
        f = line.split('|')
        if len(f) < 8:
            continue
        try:
            _, idle, _, _ = (int(x) for x in f[3].split('/'))
            mem_free = int(f[4]) - int(f[5])
        except ValueError:
            continue
        out.append(Node(f[0].strip(), f[1].strip().rstrip('*'), f[2].strip(), idle, mem_free,
                        max(0, _gres_count(f[6]) - _gres_count(f[7]))))
    return out


def parse_assoc(text: str) -> dict[str, dict[str, int]]:
    """``sacctmgr -nP show assoc user=$USER format=Partition,GrpTRES``: each partition's
    (lower case) group limits, ``cpu`` and ``gpu`` (any ``gres/gpu*``). Pure."""
    out: dict[str, dict[str, int]] = {}
    for line in text.splitlines():
        part, _, tres = line.partition('|')
        lim: dict[str, int] = {}
        typed = 0
        for kv in tres.split(','):
            k, _, v = kv.partition('=')
            if not v.isdigit():
                continue
            if k == 'cpu':
                lim['cpu'] = int(v)
            elif k == 'gres/gpu':  # the untyped total wins over the typed ones
                lim['gpu'] = int(v)
            elif k.startswith('gres/gpu:'):
                typed += int(v)
        if typed and 'gpu' not in lim:
            lim['gpu'] = typed
        if part.strip():
            out[part.strip().lower()] = lim
    return out


def parse_squeue(text: str) -> dict[str, dict[str, int]]:
    """``squeue -h -u $USER -t RUNNING -o %P|%C|%b``: cores and cards the account's
    running jobs hold, per partition (lower case). Pure."""
    out: dict[str, dict[str, int]] = {}
    for line in text.splitlines():
        f = line.split('|')
        if len(f) < 3 or not f[1].isdigit():
            continue
        d = out.setdefault(f[0].strip().lower(), {'cpu': 0, 'gpu': 0})
        d['cpu'] += int(f[1])
        d['gpu'] += _gres_count(f[2].replace('gres/', '').replace('gres:', ''))
    return out


def partition_key(machine: str, partition: str) -> str:
    """A cluster target row's ``partition``: an opaque key, equal for the targets of one
    machine that share a partition, which names no partition (decision 19.4, §3.3). Pure."""
    return hashlib.sha256(f'{machine}\0{partition.lower()}'.encode()).hexdigest()[:12]


def target_row(m: Machine, t: Target, nodes: Sequence[Node], assoc: Mapping[str, Mapping[str, int]],
               used: Mapping[str, Mapping[str, int]], cards: int = 1, mem_gib: int = 1) -> dict:
    """A cluster target's row: its best node's idle cores and free cards (an ``idle`` or
    ``mix`` node of its partition), less what the account's running jobs hold of its
    association's ``GrpTRES``; ``start_now`` if a job of the target would start now, and
    ``jobs_free``, how many (decision 15.7). A job takes the target's ``cpus`` cores, on a
    GPU target ``cards`` cards (at most ``max_cards``) and ``cpus`` cores per card, and
    ``mem_gib`` GiB (at most the target's). Pure."""
    row = _base_row(f'{m.key}:{t.name}', m, '')
    row.update(target=t.name, gpu_model=t.gpu_model, cuda=t.cuda or t.variant, price=t.price,
               numerics_ok=t.numerics_ok, labels=dict(t.labels), multi_gpu=t.max_cards >= 2,
               memory_gib=t.memory_gib, max_minutes=t.max_minutes, partition=partition_key(m.key, t.partition))
    part = t.partition.lower()
    lim, u = assoc.get(part, {}), used.get(part, {})
    room_cpu = lim['cpu'] - u.get('cpu', 0) if 'cpu' in lim else None
    room_gpu = lim['gpu'] - u.get('gpu', 0) if 'gpu' in lim else None
    mine = [n for n in nodes if n.partition.lower() == part and n.state.rstrip('*~#!%$@^-') in ('idle', 'mix')]
    need_cards = max(1, min(cards, t.max_cards)) if t.gpu_model else 0
    need_cpu = t.cpus * need_cards if t.gpu_model else t.cpus  # a GPU target's cpus are per card (the gate's cpus_per_card)
    need_mib = max(1, min(t.memory_gib, mem_gib)) * 1024  # at least 1 GiB: a target of memory_gib 0 divides nothing
    best = max(mine, key=lambda n: (n.cards_free, n.cpus_idle), default=None)
    cpus = best.cpus_idle if best else 0
    free_cards = best.cards_free if (best and t.gpu_model) else 0
    if room_cpu is not None:
        cpus = min(cpus, max(0, room_cpu))
    if room_gpu is not None:
        free_cards = min(free_cards, max(0, room_gpu))

    def held(n: Node) -> int:  # the jobs node n has room for now
        return min([n.cpus_idle // need_cpu, n.mem_free_mib // need_mib] + ([n.cards_free // need_cards] if need_cards else []))
    jobs = sum(max(0, held(n)) for n in mine)
    if room_cpu is not None:
        jobs = min(jobs, max(0, room_cpu) // need_cpu)
    if room_gpu is not None and need_cards:
        jobs = min(jobs, max(0, room_gpu) // need_cards)
    start = jobs > 0
    row.update(cpus_free=cpus, gpu_free={i: t.gpu_mib for i in range(free_cards)}, start_now=start,
               slots_free=1 if start else 0, jobs_free=jobs)
    if not mine:
        row['problems'] = [f'no usable node in its partition']
    return row


Runner = Callable[..., subprocess.CompletedProcess]


def _ssh(alias: str, key: str = '', config: str = '', connect: int = 15) -> list[str]:
    """ssh to ``alias`` in batch mode, connecting within ``connect`` s; with ``key``, that
    key only; with ``config``, that file for the account's ssh configuration."""
    only = ['-o', 'IdentitiesOnly=yes', '-i', key] if key else []
    only += ['-F', config] if config else []
    return ['ssh', '-o', 'BatchMode=yes', *only, '-o', f'ConnectTimeout={connect}', '-o', 'ServerAliveInterval=15', alias]


#: what ``pool status`` asks of a host whose key is forced to it (the forced line runs it anyway)
FORCED_PROBE = 'ci-run pool probe'
HOST_TIMEOUT, CLUSTER_TIMEOUT = 60, 90  # s a host's probe and a cluster's may take; status runs them in parallel
#: pool status --fast (16.5): each ssh call connects within FAST_CONNECT s and ends within
#: FAST_TIMEOUT s, so that the whole ends within 2 s with a host down
FAST_CONNECT, FAST_TIMEOUT = 2, 1.5


def _etc(etc: str) -> str:
    from ci_runners import cli
    return etc or cli.etc_dir()


def probe_key(etc: str, key: str) -> str:
    """The key ``pool status`` logs in with to machine ``key`` of ``probe = "forced"``."""
    return os.path.join(_etc(etc), 'pool', 'keys', key)


def probe_config(etc: str) -> str:
    """``etc/pool/ssh_config`` where it is a file, else '': the forced probes' aliases and
    pinned host keys on a shared account, whose ``~/.ssh`` is not ours (decision 15.8)."""
    path = os.path.join(_etc(etc), 'pool', 'ssh_config')
    return path if os.path.isfile(path) else ''


def _remote(m: Machine, argv: Sequence[str], connect: int = 15) -> list[str]:
    """``argv`` run on ``m``: through ssh as one quoted command line, or here for a local machine."""
    return list(argv) if m.access == 'local' else [*_ssh(m.ssh, connect=connect), shlex.join(argv)]


def probe_host(m: Machine, run: Runner = subprocess.run, here: str = '', etc: str = '', fast: bool = False,
               cached: Mapping | None = None, now: Callable[[], float] = time.time) -> dict:
    """A host's row, with ``read_at``: its probe's source sent to its python, or with
    ``probe = "forced"`` no code and no stdin, through its key (``probe_key``), whose line
    forces ``ci-run pool probe`` there; that answers with its account's capacity file while
    it is fresh, and the row was read when that was (its ``age``). ``fast`` (``pool status
    --fast``, 16.5): ssh connects within ``FAST_CONNECT`` s and the whole ends within
    ``FAST_TIMEOUT`` s; a probe this account runs reads no cards (no ``nvidia-smi``) and
    takes them from ``cached``, this account's file's row of the host, while that is fresh."""
    return _host(m, run, here, etc, fast, cached, now)[0]


def _host(m: Machine, run: Runner, here: str, etc: str, fast: bool = False, cached: Mapping | None = None,
          now: Callable[[], float] = time.time) -> tuple[dict, dict | None]:
    """``probe_host``'s row and the probe it was made of (None where none answered)."""
    from ci_runners import capacity
    connect, timeout = (FAST_CONNECT, FAST_TIMEOUT) if fast else (15, HOST_TIMEOUT)

    def down(why):
        return dict(host_row(m, None, why, here), read_at=round(now(), 3)), None
    if m.probe == 'forced':
        key = probe_key(etc, m.key)
        if not os.path.isfile(key):
            return down(f'no key etc/pool/keys/{m.key}')
        argv, kw = [*_ssh(m.ssh, key, probe_config(etc), connect), FORCED_PROBE], {'stdin': subprocess.DEVNULL}
    else:
        lz = m.leases or LeaseDir('', 1)
        dv = m.dev_slots
        src = _probe_src() + (f'\nimport json; print(json.dumps(probe({m.slots.lock!r}, {m.slots.count}, '
                              f'{m.slots.queue!r}, {m.drain!r}, {lz.dir!r}, {lz.parts}, {m.gpus}, {m.runners!r}'
                              f'{", smi=False" if fast else ""}'
                              + (f', dev_lock={dv.lock!r}, dev_count={len(dv.cpus)}, dev_queue={dv.queue!r}' if dv else '')
                              + ')))\n')
        argv, kw = _remote(m, [m.host_python, '-I', '-'], connect), {'input': src}
    try:
        r = run(argv, capture_output=True, text=True, timeout=timeout, **kw)
    except subprocess.TimeoutExpired:
        return down(f'unreachable (no answer within {timeout:g} s)')
    except (OSError, subprocess.SubprocessError) as e:
        return down(f'probe failed: {type(e).__name__}')
    if r.returncode != 0:
        return down(f'unreachable (exit {r.returncode})')
    try:
        p = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return down('probe answer not JSON')
    if not isinstance(p, dict):
        return down('probe answer not JSON')
    t = now()
    age = p.pop('age', None)  # a forced probe's answer from its account's capacity file (16.5)
    read_at = t - age if type(age) in (int, float) and age >= 0 else t
    if m.access == 'local':  # the caller's own host: its inventory is this account's (15.7)
        _with_inventory(p, etc)
    if fast and m.probe != 'forced':  # no cards read now: this account's last reading, while fresh
        p['gpu_free'] = dict(cached['gpu_free']) if cached and isinstance(cached.get('gpu_free'), dict) \
            and not capacity.is_stale(cached, t) else {}
    return dict(host_row(m, p, here=here), read_at=round(read_at, 3)), p


CLUSTER_PROBE = ('sinfo -h -N -O "NodeHost:|,Partition:|,StateCompact:|,CPUsState:|,Memory:|,AllocMem:|,Gres:|,GresUsed:|"; '
                 'echo ===; sacctmgr -nP show assoc user="$USER" format=Partition,GrpTRES; '
                 'echo ===; squeue -h -u "$USER" -t RUNNING -o "%P|%C|%b"')


def capacity(m: Machine, run: Runner = subprocess.run, etc: str = '') -> tuple[str, str]:
    """A CI account's cluster entry (``site``, ``account``): the gate's ``capacity``
    answer through the account's CI key, as the parts reach it (``slurmpart.Gate``), or
    ``('', why)``. The reasons name no host, path or Slurm text: the plan prints them."""
    from ci_runners import slurmpart, slurmsite
    etc = _etc(etc)
    data, tree = os.path.dirname(os.path.dirname(etc)), os.path.dirname(etc)
    if os.path.basename(etc) != 'etc' or os.path.basename(tree) != 'ci-runners':  # the gate's state is <data>/ci-runners
        return '', 'the registry is not in <data>/ci-runners/etc'
    path = slurmsite.site_files(etc).get(m.site)
    if path is None:
        return '', f'no site config etc/slurm/{m.site}.toml'
    try:
        site = slurmsite.load_file(path)
        if m.account not in site.accounts:
            return '', f'no account {m.account} in site {m.site}'
        g = slurmpart.Gate(etc, data, site, m.account, run=run)
        return g.call('capacity', timeout=CLUSTER_TIMEOUT).decode(errors='replace'), ''
    except (slurmsite.SiteError, ValueError):  # ValueError: not UTF-8
        return '', 'site config: invalid'
    except slurmpart.Refused:  # a gate without the verb (the account's slurm apply brings it), or a query failed
        return '', 'the gate refused capacity'
    except (slurmpart.Unreachable, OSError, subprocess.SubprocessError):
        return '', 'cluster unreachable'


def probe_cluster(m: Machine, run: Runner = subprocess.run, cards: int = 1, mem_gib: int = 1, etc: str = '') -> list[dict]:
    if m.site:
        text, why = capacity(m, run, etc)
    else:
        try:
            r = run([*_ssh(m.ssh), CLUSTER_PROBE], capture_output=True, text=True, timeout=CLUSTER_TIMEOUT)
            text, why = r.stdout if r.returncode == 0 else '', ''
        except (OSError, subprocess.SubprocessError):
            text, why = '', ''
    parts = text.split('===\n')
    if len(parts) != 3:
        rows = []
        for t in m.targets.values():
            row = target_row(m, t, [], {}, {})
            row.update(problems=[why or 'cluster unreachable'], start_now=False)
            rows.append(row)
        return rows
    nodes, assoc, used = parse_sinfo(parts[0]), parse_assoc(parts[1]), parse_squeue(parts[2])
    return [target_row(m, t, nodes, assoc, used, cards, mem_gib) for t in m.targets.values()]


def status(reg: Registry, run: Runner = subprocess.run, here: str | None = None, cards: int = 1,
           mem_gib: int = 1, etc: str = '', on_rows: Callable[[list[dict], dict | None], None] | None = None,
           now: Callable[[], float] = time.time, cached: Mapping[str, Mapping] | None = None) -> list[dict]:
    """``pool status``: every machine's rows, in the registry's order, probed in parallel,
    so that it ends within the longest probe's bound (``CLUSTER_TIMEOUT``); each row with
    ``read_at``, when its machine answered. ``on_rows(rows, local)`` is called with each
    machine's rows as they come in, ``local`` the ``local`` machine's probe
    (``{key, read_at, probe}``) with its row, else None: the capacity file's writer (16.5).
    A cluster whose rows in ``cached`` (the capacity file's, by key) were all read less than
    ``reg.cluster_seconds`` ago is not asked again: those rows are its rows (16.5)."""
    from ci_runners import capacity
    here = socket.gethostname() if here is None else here
    out: dict[str, list[dict]] = {}

    def one(m: Machine) -> None:
        local = None
        keep = [(cached or {}).get(f'{m.key}:{t}') for t in m.targets]
        if m.access == 'slurm' and keep and all(
                r is not None and (a := capacity.age(r, now())) is not None and a < reg.cluster_seconds for r in keep):
            out[m.key] = [dict(r) for r in keep]  # read recently: the shared login node is not asked again
            return
        if m.access == 'slurm':
            rows = probe_cluster(m, run, cards, mem_gib, etc)
            t = round(now(), 3)  # when it answered: a slow probe's rows are no older than they look
            rows = [dict(r, read_at=t) for r in rows]
        else:
            row, p = _host(m, run, here, etc, now=now)
            rows = [row]
            if m.access == 'local' and p is not None:
                local = {'key': m.key, 'read_at': row['read_at'], 'probe': p}
        out[m.key] = rows
        if on_rows is not None:
            on_rows(rows, local)
    threads = [threading.Thread(target=one, args=(m,)) for m in reg.machines.values()]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return [row for k in reg.machines for row in out.get(k, [])]


def fast_status(reg: Registry, run: Runner = subprocess.run, here: str | None = None, etc: str = '',
                now: Callable[[], float] = time.time) -> list[dict]:
    """``pool status --fast`` (16.5): the rows from the capacity files, within 2 s. A host
    with ``probe = "forced"`` answers with its account's file (``local_probe``); a host this
    account probes itself (``local``, or ssh with code) has its slots, load and runners read
    now without ``nvidia-smi``, its cards from this account's file; each ssh call within
    ``FAST_TIMEOUT`` s. A cluster target's row is this account's file's, which
    ``ci-capacity.timer`` keeps. Every row past ``capacity.STALE_S`` counts as down
    (``capacity.mark``). Writes nothing."""
    from ci_runners import capacity
    here = socket.gethostname() if here is None else here
    cached = {r['key']: r for r in capacity.read(capacity.path(_etc(etc)))['rows']}
    out: dict[str, list[dict]] = {}

    def not_read(m: Machine, t: Target | None = None) -> dict:
        row = target_row(m, t, [], {}, {}) if t else host_row(m, None, 'not read', here)
        row.update(problems=['not read'], start_now=False)
        return row

    def one(m: Machine) -> None:
        out[m.key] = [probe_host(m, run, here, etc, fast=True, cached=cached.get(m.key), now=now)]
    threads = []
    for m in reg.machines.values():
        if m.access == 'slurm':  # the timer's cached figures
            out[m.key] = [cached.get(f'{m.key}:{t.name}') or not_read(m, t) for t in m.targets.values()]
        else:
            threads.append(threading.Thread(target=one, args=(m,), daemon=True))
    for t in threads:
        t.start()
    deadline = time.monotonic() + FAST_TIMEOUT + 0.3
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    t = now()
    return [capacity.mark(row, t) for m in reg.machines.values() for row in out.get(m.key) or [not_read(m)]]


def _hold_time(t: object) -> str:
    try:
        return datetime.datetime.fromtimestamp(float(t), datetime.timezone.utc).strftime('%H:%MZ')
    except (TypeError, ValueError, OverflowError, OSError):
        return '?'


def table(rows: Sequence[Mapping]) -> str:
    """``pool status`` for people. Pure."""
    head = ('machine', 'slots', 'queue', 'cores', 'mem GiB', 'cards free', 'model', 'billed', 'start', 'numerics', 'problems')
    lines = [head]
    for r in rows:
        cards = ','.join(f'{i}:{v // 1024}G' for i, v in sorted(r['gpu_free'].items())) if r['gpu_free'] else '-'
        slots = f'{r["slots_free"]}/{r["slots"]}' if r['access'] != 'slurm' else '-'
        if r.get('dev_slots'):
            slots += f' dev {r.get("dev_free", 0)}/{r["dev_slots"]}'
        lines.append((r['key'] + (' (here)' if r.get('here') else ''), slots, str(r['queue']), str(r['cpus_free']),
                      str(r['mem_free_gib']) if r['access'] != 'slurm' else '-', cards, r['gpu_model'] or '-',
                      f'{r["price"]:g}/h' if r['billed'] else 'free',
                      ('now' + (f' ({r["jobs_free"]})' if 'jobs_free' in r else '')) if r['start_now'] else 'no',
                      'ok' if r['numerics_ok'] else 'not passed',
                      '; '.join([*r['problems'], *(f'warn: {w}' for w in r.get('warnings') or ()),
                                 *([f'hold until {_hold_time(r["hold_until"])}'] if r.get('hold_until') else [])]) or '-'))
    w = [max(len(x[i]) for x in lines) for i in range(len(head))]
    return '\n'.join('  '.join(c.ljust(w[i]) for i, c in enumerate(x)).rstrip() for x in lines)


# ------------------------------------------------------------------ picking a machine


@dataclasses.dataclass(frozen=True)
class Ask:
    """What a pool run asks of a machine: cores, memory, cards (of a model, if named)."""
    cpus: int = 1
    memory_gib: int = 8
    cards: int = 0
    gpu_model: str = ''


def parse_gpu(s: str) -> tuple[str, int]:
    """``MODEL[:COUNT]`` (``any`` for any model) -> (model or '', count). Pure."""
    m = _GPU.fullmatch(s.lower())
    if not m:
        raise ValueError(f'--gpu {s!r}: MODEL[:COUNT]')
    return ('' if m[1] == 'any' else m[1]), int(m[2] or 1)


def fits(row: Mapping, ask: Ask, m: Machine, need_mib: int) -> str:
    """Why the row's machine cannot run ``ask`` at all ('' if it can, now or later). Pure."""
    problems = [p for p in row['problems'] if p != NO_RUNNER]  # a pool run takes a slot, not a runner
    if problems:
        return '; '.join(problems)
    if ask.cards:
        model = row['gpu_model']
        if not model:
            return 'no cards'
        if not row['target'] and not m.leases:  # a host's cards go by the parts' leases, shared or not
            return 'a host\'s cards are leased to its parts (a GPU pool run goes to a cluster target)'
        if ask.gpu_model and model != ask.gpu_model:
            return f'cards {model}, not {ask.gpu_model}'
        total = m.targets[row['target']].max_cards if row['target'] else m.gpus
        if total < ask.cards:
            return f'{total} card(s), {ask.cards} asked'
        if ask.cards >= 2 and not row['multi_gpu']:
            return 'no P2P self-test passed for two cards'
    elif row['target'] and m.targets[row['target']].gpu_model:
        return 'a GPU target, and no card asked'
    if row['target']:
        t = m.targets[row['target']]
        if ask.memory_gib > t.memory_gib:
            return f'{t.memory_gib} GiB a node, {ask.memory_gib} asked'
        if not t.gpu_model and ask.cpus > t.cpus:
            return f'{t.cpus} cores a job, {ask.cpus} asked'
    else:
        if ask.cpus > m.cpus:
            return f'{m.cpus} cores, {ask.cpus} asked'
        if ask.memory_gib > m.memory_gib:
            return f'{m.memory_gib} GiB, {ask.memory_gib} asked'
        if m.dev_slots and not ask.cards:  # 16.8: a CPU run takes a dev slot here, never a CI slot
            most = max(len(cpus_of(x) or ()) for x in m.dev_slots.cpus)
            if ask.cpus > most:
                return f'{most} cores a dev slot, {ask.cpus} asked'
            if ask.memory_gib > m.dev_slots.memory_gib:
                return f'{m.dev_slots.memory_gib} GiB a dev slot, {ask.memory_gib} asked'
    return ''


def free_now(row: Mapping, ask: Ask, need_mib: int) -> bool:
    """Whether the row can start ``ask`` now: a job that starts now, or on a host the memory
    and a free slot and the cores, or (a GPU run) the cards with a free lease. Pure."""
    if row['target']:
        return bool(row['start_now']) and (not ask.cards or len(row['gpu_free']) >= ask.cards)
    if row['mem_free_gib'] < ask.memory_gib:
        return False
    if ask.cards:  # its cards' leases instead of a CI slot, as a GPU part (design §7)
        return sum(1 for v in row['gpu_free'].values() if v >= need_mib) >= ask.cards
    if row.get('dev_slots'):  # a dev slot, its own cpuset (16.8): the CI slots and their cores do not count
        return row.get('dev_free', 0) >= 1
    return row['slots_free'] >= 1 and row['cpus_free'] >= ask.cpus


def pick(rows: Sequence[Mapping], ask: Ask, reg: Registry, *, here_ok: bool = False, only: str = '') -> tuple[list[dict], list[dict], list[str]]:
    """``(free now, wait in line, skipped)``: the rows that can run ``ask`` now in the
    order a run tries them, free hosts first (fewest cores in use), then the billed
    cluster targets that start it now (cheapest first); the hosts it could wait on for a
    slot (shortest queue first); and why each other row was skipped. The caller's own host
    is skipped unless ``here_ok``, and a machine that is not ``ready`` (``pool add``) unless
    ``only`` names it; ``only`` keeps the rows of one machine or target. Pure."""
    now, later, skipped = [], [], []
    for r in rows:
        key = r['key']
        if only and key != only and key.split(':')[0] != only:
            continue
        m = reg.machines[key.split(':')[0]]
        if m.status_only:
            skipped.append(f'{key}: a CI account\'s entry, for pool status only')
            continue
        if r.get('here') and not here_ok:
            skipped.append(f'{key}: the caller\'s own host (--here to allow)')
            continue
        if not m.ready and only not in (key, m.key):
            skipped.append(f'{key}: not ready (pool add\'s checks; --on {m.key} runs there)')
            continue
        why = fits(r, ask, m, reg.gpu_need_mib)
        if why:
            skipped.append(f'{key}: {why}')
        elif free_now(r, ask, reg.gpu_need_mib):
            now.append(dict(r))
        elif not r['target']:
            later.append(dict(r))
        else:
            skipped.append(f'{key}: cannot start it now')
    now.sort(key=lambda r: (r['billed'], r['price'] if r['billed'] else 0, -r['cpus_free']))
    later.sort(key=lambda r: (r['dev_queue'], -r['dev_slots']) if r.get('dev_slots') and not ask.cards
               else (r['queue'], -r['slots']))
    return now, later, skipped


# ------------------------------------------------------------------ the env check

FINGERPRINT = r'''
import importlib.metadata as m, json, subprocess, sys
out = {}
for d in json.loads(sys.argv[1]):
    try:
        dist = m.distribution(d)
    except m.PackageNotFoundError:
        out[d] = None
        continue
    v, du = dist.version, dist.read_text('direct_url.json')
    if du and '"dir_info"' in du:
        url = json.loads(du).get('url', '')
        path = url[7:] if url.startswith('file://') else ''
        r = subprocess.run(['git', '-C', path, 'rev-parse', 'HEAD'], capture_output=True, text=True) if path else None
        v += '@' + (r.stdout.strip()[:12] if r is not None and r.returncode == 0 else 'editable')
    out[d] = v
print(json.dumps(out))
'''


def fingerprint(argv_prefix: Sequence[str], python: str, dists: Sequence[str], run: Runner = subprocess.run) -> dict:
    """The versions of ``dists`` in the env of ``python`` (``argv_prefix``: how to reach its
    machine, ``[]`` for here); an editable install's version carries its commit."""
    cmd = [python, '-I', '-c', FINGERPRINT, json.dumps(list(dists))]
    argv = [*argv_prefix, shlex.join(cmd)] if argv_prefix else cmd
    r = run(argv, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f'the env check could not run {python}: {(r.stderr or "").strip()[-300:]}')
    return json.loads(r.stdout.strip().splitlines()[-1])


def env_diff(mine: Mapping[str, str | None], theirs: Mapping[str, str | None]) -> list[str]:
    """The differences between two fingerprints: a version, a missing distribution, or the
    commit of an install that is editable on both sides. Pure."""
    out = []
    for d in sorted(set(mine) | set(theirs)):
        a, b = mine.get(d), theirs.get(d)
        if a is None or b is None:
            if a != b:
                out.append(f'{d}: {a or "missing"} here, {b or "missing"} there')
            continue
        va, _, ca = a.partition('@')
        vb, _, cb = b.partition('@')
        if va != vb or (ca and cb and ca != cb):
            out.append(f'{d}: {a} here, {b} there')
    return out


# ------------------------------------------------------------------ staging the caller's tree


def tree_files(root: str, run: Runner = subprocess.run) -> list[str]:
    """The files a run stages: tracked, and untracked but not ignored, as git lists them."""
    r = run(['git', '-C', root, 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], capture_output=True,
            timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f'{root}: not a git checkout')
    return sorted({p for p in r.stdout.decode().split('\0') if p and os.path.lexists(os.path.join(root, p))})


def git_dir(root: str, dest: str, base: str = 'origin/main', run: Runner = subprocess.run) -> str:
    """A small git repository for the staged tree, so that commands which read git (``git
    ls-files``, ``git merge-base origin/main HEAD``, a diff against it) see what they see in
    ``root``. It holds HEAD, fetched shallow down to its merge base with ``base``, and
    ``refs/remotes/<base>`` at that merge base: the same merge base, so the same diffs. Its
    index is built here: HEAD, plus the files added to the index but not committed. Returns
    its path (``dest/.git``), or '' where ``root`` has no HEAD. A branch without the merge
    base gets HEAD alone, and ``base`` at HEAD."""
    def git(*a, cwd=root, **kw):
        return run(['git', '-C', cwd, *a], capture_output=True, text=True, timeout=600, **kw)
    head = git('rev-parse', 'HEAD').stdout.strip()
    if not head:
        return ''
    mb = git('merge-base', base, 'HEAD').stdout.strip() or head
    depth = int(git('rev-list', '--count', f'{mb}..{head}').stdout.strip() or 0) + 1
    gd = os.path.join(dest, '.git')
    for argv in (['init', '-q', dest], ['-C', dest, 'fetch', '-q', '--no-tags', f'--depth={depth}', f'file://{root}', head],
                 ['-C', dest, 'update-ref', 'refs/heads/pool', head], ['-C', dest, 'symbolic-ref', 'HEAD', 'refs/heads/pool'],
                 ['-C', dest, 'update-ref', f'refs/remotes/{base}', mb]):
        r = run(['git', *argv], capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            raise RuntimeError(f'git {argv[2] if argv[0] == "-C" else argv[0]}: {r.stderr.strip()[-300:]}')
    env = {**os.environ, 'GIT_DIR': gd, 'GIT_WORK_TREE': root}
    run(['git', 'read-tree', 'HEAD'], env=env, capture_output=True, timeout=600)
    added = git('diff', '--cached', '--name-only', '--diff-filter=A', '-z', 'HEAD').stdout.split('\0')
    added = [p for p in added if p]
    if added:
        run(['git', 'add', '--', *added], env=env, capture_output=True, timeout=600)
    return gd


def bundle(root: str, files: Iterable[str], pkg: str | None = None, git: str = '') -> bytes:
    """A tar of ``files`` under ``ws/``, with ``git`` (:func:`git_dir`) as ``ws/.git`` and,
    with ``pkg``, the ``ci_runners`` package under ``pkg/ci_runners/`` (what the host side
    runs). Links stay links."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w') as tar:
        for p in files:
            tar.add(os.path.join(root, p), arcname=f'ws/{p}', recursive=False)
        if git:
            tar.add(git, arcname='ws/.git')
        if pkg:
            for n in sorted(os.listdir(pkg)):
                if n.endswith('.py'):
                    tar.add(os.path.join(pkg, n), arcname=f'pkg/ci_runners/{n}', recursive=False)
    return buf.getvalue()


def head_of(root: str) -> str:
    r = subprocess.run(['git', '-C', root, 'rev-parse', '--short=12', 'HEAD'], capture_output=True, text=True)
    d = subprocess.run(['git', '-C', root, 'status', '--porcelain', '--untracked-files=no'], capture_output=True, text=True)
    return r.stdout.strip() + (' + local changes' if d.stdout.strip() else '')


# ------------------------------------------------------------------ the host side


def _say(msg: str) -> None:
    print(f'pool: {msg}', file=sys.stderr, flush=True)


def _remove_stale(work: str, keep: str, now: float) -> None:
    """Remove the run directories in ``work`` older than ``STALE_DAYS`` whose run is gone:
    none holds its ``ALIVE`` lock (a host side or a supervisor that outlived it would)."""
    from ci_runners import slots as slots_mod
    try:
        names = os.listdir(work)
    except OSError:
        return
    for n in names:
        p = os.path.join(work, n)
        if n.startswith(RUN_PREFIX) and p != keep:
            try:
                if now - os.lstat(p).st_mtime > STALE_DAYS * 86400 and not slots_mod._flocked(os.path.join(p, ALIVE)):
                    subprocess.run(['rm', '-rf', '--one-file-system', p], timeout=600)
            except (OSError, subprocess.SubprocessError):
                pass


def _hold_alive(d: str) -> int:
    """Flock the run directory's ``ALIVE`` file, for the host side and then its supervisor."""
    import fcntl
    fd = os.open(os.path.join(d, ALIVE), os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def _user_bus(env: Mapping[str, str]) -> str:
    """The user manager's runtime directory if ``systemd-run --user`` can reach it, else ''."""
    rt = env.get('XDG_RUNTIME_DIR') or f'/run/user/{os.getuid()}'
    return rt if os.path.exists(os.path.join(rt, 'bus')) else ''


def _take_slot(spec: Mapping, q, rounds_max: int, deadline: float, what: str = 'CI slot') -> int | None:
    """Wait in line for a slot of ``q``, within the project's ``max_slots`` (``q``'s
    ``default_cap``: the run's project is the queue's default one, whose ``ci_slot.sh``
    tickets and holders count as its own): the slot's fd, or None once the host is drained,
    after ``rounds_max`` tries (0: no bound) or at ``deadline``. The drain is read again
    before each try."""
    project = spec['project']
    q.take_ticket(project, 'pool run: ' + spec['command'][:200])
    shown, rounds = None, 0
    while True:
        if drained(spec.get('drain', '')):
            _say('this host is drained')
            return None
        fd = q.try_take(project, {})
        rounds += 1
        if fd is not None:
            return fd
        if rounds_max and rounds >= rounds_max:
            _say(f'no {what} free on this host now')
            return None
        if time.time() >= deadline:
            _say(f'no {what} within the queue bound')
            return None
        ahead = sum(1 for t in q.waiting() if t.seq < q.seq)
        if ahead != shown:
            _say(f'waiting in line for a {what}: {ahead} ahead')
            shown = ahead
        time.sleep(q.poll)


def _take_cards(spec: Mapping, leases, rounds_max: int, deadline: float) -> list[int] | None:
    """Wait for ``cards`` of the host's cards by the parts' leases, in their ticket order:
    the cards taken (their leases in ``leases.fds``), or None as ``_take_slot``."""
    from ci_runners import part
    n, need, cards = int(spec['cards']), int(spec.get('need_mib', 0)), list(range(int(spec['gpus'])))
    shown, rounds = False, 0
    while True:
        if drained(spec.get('drain', '')):
            _say('this host is drained')
            return None
        got = leases.try_take(n, cards, part.smi_free(), need)
        rounds += 1
        if got is not None:
            return got
        if rounds_max and rounds >= rounds_max:
            _say(f'no {n} card(s) with a free lease and {need} MiB free on this host now')
            return None
        if time.time() >= deadline:
            _say('no card within the queue bound')
            return None
        if not shown:
            _say(f'waiting in line for {n} card(s)')
            shown = True
        time.sleep(float(spec.get('poll', 3)))


#: the supervisor as a process of its own: ``part.supervise`` of the run's ``ci_runners``
SUPERVISOR = ('import sys; sys.path.insert(0, sys.argv.pop(1)); from ci_runners.part import supervise; '
              'sys.exit(supervise(sys.argv[1], float(sys.argv[2]), sys.argv[4:], int(sys.argv[3])))')


def _start_supervisor(spec: Mapping, d: str, record: str, env: Mapping[str, str], fds: Sequence[int],
                      inner: Sequence[str], started: list) -> str:
    """Start the supervisor holding ``fds`` (the slot or the leases, and ``ALIVE``), in a
    scope of its own where the user manager can be reached (``spec['scope']`` false: never),
    else in a session of its own. Its process goes to ``started`` as soon as it runs (the
    last one is the supervisor); returns the scope's unit, or ''. Its stdin is /dev/null;
    its output is ours."""
    from ci_runners import part
    pkg = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sup = [sys.executable, '-I', '-c', SUPERVISOR, pkg, record, str(float(spec['timeout_seconds'])),
           str(int(spec['memory_gib']) << 30), *inner]
    env = dict(env)
    rt = _user_bus(os.environ) if spec.get('scope', True) else ''
    exe = shutil.which('systemd-run') if rt else None
    if exe:
        env['XDG_RUNTIME_DIR'] = rt
        unit = part.unit_name('pool', spec['project'], os.path.basename(d))
        try:
            v = subprocess.run(['systemctl', '--version'], capture_output=True, text=True, timeout=30).stdout or ''
        except (OSError, subprocess.SubprocessError):
            v = ''
        argv = part.scope_argv(unit, spec['project'], f'{int(spec["memory_gib"])}G', sup,
                               part.systemd_major(v) >= part.OOM_POLICY_SYSTEMD)
        argv[0] = exe
        p = subprocess.Popen(argv, env=env, pass_fds=tuple(fds), stdin=subprocess.DEVNULL, start_new_session=True)
        started.append(p)
        if not _failed_to_start(p, record):
            return unit
        _say(f'no systemd scope (systemd-run exit {p.returncode}); the supervisor runs in a session of its own')
    started.append(subprocess.Popen(sup, env=env, pass_fds=tuple(fds), stdin=subprocess.DEVNULL, start_new_session=True))
    return ''


def _failed_to_start(p: subprocess.Popen, record: str) -> bool:
    """Whether ``p`` (``systemd-run`` of the supervisor) ended before the supervisor started
    the command (it writes ``scope.json`` first): it could not make the scope."""
    while not os.path.exists(os.path.join(record, 'scope.json')):
        if p.poll() is not None:
            return not os.path.exists(os.path.join(record, 'scope.json'))
        time.sleep(0.05)
    return False


def _stop_supervisor(p: subprocess.Popen, record: str, unit: str) -> None:
    """The caller is gone: TERM the supervisor, which TERMs the command's process group and
    records its end; after ``STOP_GRACE`` s kill that group (checked as the supervisor's
    child) and stop the scope."""
    try:
        os.kill(p.pid, signal.SIGTERM)
    except OSError:
        pass
    try:
        p.wait(timeout=STOP_GRACE)
        return
    except subprocess.TimeoutExpired:
        pass
    child = _read(os.path.join(record, 'scope.json')).get('child')
    try:
        with open(f'/proc/{child}/stat') as f:
            ours = int(f.read().rsplit(')', 1)[1].split()[1]) == p.pid
    except (OSError, ValueError, IndexError, TypeError):
        ours = False
    if ours:
        try:
            os.killpg(child, signal.SIGKILL)
        except OSError:
            pass
    if unit:
        subprocess.run(['systemctl', '--user', 'stop', unit], capture_output=True, timeout=120)
    try:
        p.wait(timeout=30)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()


def remote(argv: Sequence[str], stdin=None) -> int:
    """The host side of a run (``argv``: the run directory and the spec as JSON), started
    in ``<dir>/ws`` by ``run_host``. It takes a slot of the host's queue, or with ``cards``
    the cards' leases (waiting in line, or giving up after ``rounds`` tries: NOT_STARTED,
    as on a drained host), then starts the supervisor (``_start_supervisor``), which holds
    them for the command's life. A closed stdin (the caller is gone) or a signal stops the
    supervisor, which ends the command. The run directory is removed at the end."""
    from ci_runners import part, slots as slots_mod
    d, spec = argv[0], json.loads(argv[1])
    stdin = sys.stdin if stdin is None else stdin

    # a signal while the supervisor starts waits until its process is known (``started``); after
    # the first one that stops the run, the next ones change nothing (the stop is under way)
    sigs = {'starting': False, 'stopping': False, 'pending': 0}

    def stop(signum, _frame):
        if sigs['starting'] or sigs['stopping']:
            sigs['pending'] = sigs['pending'] or signum
            return
        sigs['stopping'] = True
        raise SystemExit(128 + signum)
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, stop)

    def watch():  # EOF on stdin: the caller (or its ssh) is gone
        try:
            while stdin.read(4096):
                pass
        except (OSError, ValueError):
            pass
        os.kill(os.getpid(), signal.SIGTERM)
    threading.Thread(target=watch, daemon=True).start()
    q, leases, sup, slot = None, None, None, None
    fds: list[int] = []
    started: list[subprocess.Popen] = []
    record = os.path.join(d, 'record')
    ours = False  # the run directory is this run's (its ALIVE lock taken): only then removed at the end
    try:
        fds.append(_hold_alive(d))
        ours = True
        _remove_stale(os.path.dirname(d), d, time.time())
        rounds_max = int(spec.get('rounds') or 0)
        deadline = time.time() + float(spec['queue_seconds'])
        visible, cpus = '', ''
        dev = spec.get('dev') if not int(spec.get('cards', 0)) else None
        if int(spec.get('cards', 0)):
            if not spec.get('leases'):
                _say('this host leases no cards to pool runs')
                return NOT_STARTED
            leases = part.Leases(spec['leases'], int(spec.get('lease_parts', 1)), shared=True)
            got = _take_cards(spec, leases, rounds_max, deadline)
            if got is None:
                return NOT_STARTED
            visible = ','.join(str(c) for c in sorted(got))
            _say(f'holding card(s) {visible}')
        elif dev:  # 16.8: a dev slot of its own, never a CI slot
            why = dev_refusal(dev) or (f"{spec['memory_gib']} GiB asked, a dev slot holds {dev['memory_gib']}"
                                       if int(spec['memory_gib']) > int(dev.get('memory_gib') or 0) else '')
            if why:
                _say(f'refused: {why}')
                return NOT_STARTED
            q = slots_mod.SlotQueue(dev['lock'], len(dev['cpus']), dev.get('queue') or None, float(spec.get('poll', 3)),
                                    default_project=spec['project'])
            slot = _take_slot(spec, q, rounds_max, deadline, 'dev slot')
            if slot is None:
                return NOT_STARTED
            fds.append(slot)
            cpus = dev['cpus'][q._index(slot)]
            _say(f"holding dev slot {q._index(slot) + 1} of {len(dev['cpus'])} (CPUs {cpus})")
        else:
            q = slots_mod.SlotQueue(spec['lock'], int(spec['count']), spec.get('queue') or None,
                                    float(spec.get('poll', 3)), default_project=spec['project'],
                                    default_cap=spec.get('max_slots'))
            slot = _take_slot(spec, q, rounds_max, deadline)
            if slot is None:
                return NOT_STARTED
            fds.append(slot)
            _say(f"holding CI slot {q._index(slot) + 1} of {spec['count']}")
        env = {k: v for k, v in os.environ.items() if k in ('HOME', 'USER', 'LOGNAME', 'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS')}
        envd = os.path.dirname(os.path.dirname(spec['python']))
        tmp = os.path.join(d, 'tmp')
        os.makedirs(tmp, mode=0o700, exist_ok=True)
        env.update({'PATH': f'{envd}/bin:/usr/local/bin:/usr/bin:/bin', 'CONDA_PREFIX': envd, 'LANG': 'C.UTF-8',
                    'TMPDIR': tmp, 'CI_PROJECT': spec['project'], 'CUDA_DEVICE_ORDER': 'PCI_BUS_ID',
                    'CUDA_VISIBLE_DEVICES': visible, 'CI_POOL_RUN': '1', 'PYTHONNOUSERSITE': '1'})
        env.update(spec.get('variables', {}))
        os.makedirs(record, exist_ok=True)
        unit = ''
        try:
            sigs['starting'] = True
            try:
                unit = _start_supervisor(spec, d, record, env, [*fds, *(leases.fds if leases else [])],
                                         part.command_argv(spec['command'], cpus=cpus), started)
            finally:
                sigs['starting'] = False
            sup = started[-1]
            if sigs['pending'] and not sigs['stopping']:  # a signal while it started: stop it now
                sigs['stopping'] = True
                raise SystemExit(128 + sigs['pending'])
            if q is not None:  # the slot's holder is the supervisor now (its project counts while it runs)
                try:
                    i = q._index(slot) + 1
                    slots_mod._replace(os.path.join(q.queue, f'holder.{i}'),
                                       f'{sup.pid} {slots_mod._start_of(sup.pid):.0f} {spec["project"]}\n')
                except OSError:
                    pass
            rc = sup.wait()
        except SystemExit:
            if not started:
                raise
            sup = started[-1]
            for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                signal.signal(sig, signal.SIG_IGN)
            _stop_supervisor(sup, record, unit)
            rc = EXIT_SIGNALLED
        return _exit_code(_read(os.path.join(record, 'exit.json')), rc if rc >= 0 else 128 - rc)
    except SystemExit as e:
        return int(e.code) if isinstance(e.code, int) else EXIT_FAILED
    except Exception as e:  # the host side failed, not the command
        _say(f'failed: {type(e).__name__}: {e}')
        return EXIT_FAILED
    finally:
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):  # the caller going now ends nothing more
            signal.signal(sig, signal.SIG_IGN)
        sup = started[-1] if started else None
        if q is not None and q.ticket and os.path.exists(q.ticket):
            os.unlink(q.ticket)
        if leases is not None:
            leases.release()
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass
        if ours and (sup is None or sup.poll() is not None):  # a supervisor still running keeps its directory
            subprocess.run(['rm', '-rf', '--one-file-system', d], timeout=600)


def dev_refusal(dev: Mapping) -> str:
    """Why the host side refuses its dev slots (16.8): one meets a cpuset CI pins its parts
    to, in the host's inventory (``dev['inventory']``, else this account's
    ``<etc>/inventory.toml``) where that is readable and loads; '' otherwise."""
    from ci_runners import cli, inventory
    path = dev.get('inventory') or os.path.join(cli.etc_dir(), 'inventory.toml')
    try:
        with open(path, encoding='utf-8') as f:
            inv = inventory.load(f.read(1 << 20))
    except (OSError, ValueError):
        return ''
    bad = dev_overlaps(dev['cpus'], inv)
    return '; '.join(bad[:3]) + ' (a dev slot is outside every CI slot)' if bad else ''


def _read(path: str) -> dict:
    try:
        with open(path, encoding='utf-8') as f:
            v = json.load(f)
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def _exit_code(ex: Mapping, fallback: int = 1) -> int:
    """A command's exit code from the supervisor's ``exit.json``. Pure."""
    if ex.get('timed_out'):
        return EXIT_TIMEOUT
    if ex.get('oom_kills'):
        return EXIT_OOM
    rc = ex.get('rc')
    if type(rc) is int:
        return rc if rc >= 0 else 128 - rc
    return EXIT_SIGNALLED if ex.get('signalled') else fallback


# ------------------------------------------------------------------ the caller's side


@dataclasses.dataclass
class RunArgs:
    command: str
    root: str
    ask: Ask
    timeout_min: float = DEFAULT_TIMEOUT_MIN
    queue_min: float = DEFAULT_QUEUE_MIN
    no_wait: bool = False
    env_mismatch: str = 'refuse'
    python: str = ''
    variables: Mapping[str, str] = dataclasses.field(default_factory=dict)


def forwarded(env: Mapping[str, str]) -> dict[str, str]:
    """The caller's variables a run keeps (:data:`FORWARD`). Pure."""
    return {k: v for k, v in env.items() if FORWARD.fullmatch(k)}


def _env_ok(reg: Registry, a: RunArgs, prefix: Sequence[str], python: str, run: Runner) -> bool:
    try:
        mine = fingerprint([], a.python or sys.executable, reg.check, run)
        theirs = fingerprint(prefix, python, reg.check, run)
    except (RuntimeError, ValueError, IndexError, OSError, subprocess.SubprocessError) as e:
        _say(f'env check failed: {e}')
        return a.env_mismatch == 'warn'
    diff = env_diff(mine, theirs)
    if not diff:
        _say(f'env check passed ({len(reg.check)} distributions)')
        return True
    _say('the env there differs from yours:\n  ' + '\n  '.join(diff))
    if a.env_mismatch == 'warn':
        _say('running anyway (--env-mismatch warn): the result may not be what your env gives')
        return True
    _say('refused (--env-mismatch warn runs anyway); bring the two envs to the same versions')
    return False


def host_spec(reg: Registry, m: Machine, a: RunArgs, wait: bool) -> dict:
    """What ``remote`` is told of a run on host ``m`` (``run_host``'s ``wait``). Pure."""
    return {'lock': m.slots.lock, 'count': m.slots.count, 'queue': m.slots.queue, 'project': reg.project,
            'command': a.command, 'python': m.python, 'memory_gib': a.ask.memory_gib, 'cards': a.ask.cards,
            'need_mib': reg.gpu_need_mib, 'timeout_seconds': a.timeout_min * 60,
            'rounds': 0 if wait else FREE_NOW_ROUNDS, 'queue_seconds': LONG_WAIT_MIN * 60 if wait else 120,
            'max_slots': m.max_slots, 'drain': m.drain, 'leases': m.leases.dir if m.leases else '',
            'lease_parts': m.leases.parts if m.leases else 1, 'gpus': m.gpus, 'variables': dict(a.variables),
            'dev': {'lock': m.dev_slots.lock, 'cpus': list(m.dev_slots.cpus), 'queue': m.dev_slots.queue,
                    'memory_gib': m.dev_slots.memory_gib, 'inventory': m.dev_slots.inventory}
            if m.dev_slots and not a.ask.cards else None}


def run_host(reg: Registry, m: Machine, a: RunArgs, files: Sequence[str], run: Runner = subprocess.run,
             popen: Callable = subprocess.Popen, run_id: str = '', wait: bool = True) -> int:
    """Stage the tree on host ``m`` and run there (``remote``): returns the command's code.
    ``wait``: wait in line for the slot (or the cards) up to ``LONG_WAIT_MIN``; else give
    the host up after ``FREE_NOW_ROUNDS`` tries (NOT_STARTED)."""
    prefix = [] if m.access == 'local' else _ssh(m.ssh)
    if not _env_ok(reg, a, prefix, m.python, run):
        return EXIT_ENV
    d = os.path.join(m.work, run_id or f'{RUN_PREFIX}{int(time.time())}-{os.getpid()}')
    pkg = os.path.dirname(os.path.abspath(__file__))
    with tempfile.TemporaryDirectory(prefix='pool-git-') as g:
        data = bundle(a.root, files, pkg, git_dir(a.root, g))
    r = run(_remote(m, ['sh', '-c', 'umask 077 && mkdir -p "$1" && tar -x -C "$1"', 'sh', d]), input=data,
            capture_output=True, timeout=600)
    if r.returncode != 0:
        _say(f'{m.key}: staging failed: {(r.stderr or b"").decode(errors="replace").strip()[-300:]}')
        return NOT_STARTED
    spec = host_spec(reg, m, a, wait)
    launch = ('import sys; sys.path.insert(0, sys.argv.pop(1)); from ci_runners.pool import remote; '
              'sys.exit(remote(sys.argv[1:]))')
    argv = ['sh', '-c', 'cd "$1/ws" && shift && exec "$@"', 'sh', d, m.host_python, '-I', '-c', launch,
            f'{d}/pkg', d, json.dumps(spec)]
    p = popen(_remote(m, argv), stdin=subprocess.PIPE)  # stdin stays open: its close tells the host we are gone
    try:
        rc = p.wait()
        if rc == 255 and m.access == 'ssh':
            _say(f'{m.key}: ssh failed (exit 255)')
            return EXIT_FAILED
        return rc
    except BaseException:
        p.terminate()  # ssh ends; the host side sees its stdin close and ends the command
        try:
            p.wait(timeout=30)
        except subprocess.TimeoutExpired:
            p.kill()
        raise


class OwnerGate:
    """A cluster account's ``ci-slurm-gate`` reached through the account owner's login: the
    verbs the parts' driver uses (``slurmpart.Gate``), with the verb passed the way the
    CI key's forced command passes it (``SSH_ORIGINAL_COMMAND``)."""

    def __init__(self, m: Machine, run: Runner = subprocess.run) -> None:
        self.m, self.run = m, run

    def _cmd(self, words: Sequence[str]) -> str:
        return f'SSH_ORIGINAL_COMMAND={shlex.quote(shlex.join(["ci-slurm-gate", *words]))} exec {shlex.quote(self.m.gate)}'

    def call(self, verb: str, *args: str, data: bytes = b'', timeout: float = 120) -> bytes:
        r = self.run([*_ssh(self.m.ssh), self._cmd([verb, *args])], input=data, capture_output=True, timeout=timeout)
        if r.returncode != 0:
            why = (r.stderr or b'').decode(errors='replace').strip().removeprefix('ci-slurm-gate: refused: ')
            raise RuntimeError(f'gate {verb}: {why[-300:] or f"exit {r.returncode}"}')
        return r.stdout or b''

    def json(self, verb: str, *args: str, data: bytes = b'', timeout: float = 120) -> dict:
        return json.loads(self.call(verb, *args, data=data, timeout=timeout))

    def stage(self, src: str, key: str) -> None:
        shim = f'sh -c \'SSH_ORIGINAL_COMMAND="ci-slurm-gate stage $*" exec {self.m.gate}\' x'
        r = self.run(['rsync', '-a', '--delete', '-e', shlex.join(_ssh(self.m.ssh)[:-1]), f'--rsync-path={shim}',
                      src.rstrip('/') + '/', f'{self.m.ssh}:{key}/ws/'], capture_output=True, timeout=1800)
        if r.returncode != 0:
            raise RuntimeError(f'stage: rsync exit {r.returncode}: {(r.stderr or b"").decode(errors="replace")[-300:]}')


def run_key(repository: str, now: float, pid: int) -> str:
    """A pool run's key below the gate's ``work/`` (``slurmgate.KEY``): the repository, a
    run number from the clock and an id of its own. Pure."""
    return f'{repository.lower()}/{int(now * 1000)}-1/pool-{pid}-1'


def run_cluster(reg: Registry, m: Machine, t: Target, a: RunArgs, files: Sequence[str], run: Runner = subprocess.run,
                sleep: Callable[[float], None] = time.sleep, out=None) -> int:
    """Run on cluster target ``t``: stage through the gate, submit, relay the log, and return
    the command's code; NOT_STARTED if the job did not start within ``queue_min``. The job is
    cancelled on any way out before its end."""
    out = sys.stdout.buffer if out is None else out
    python = os.path.join(m.base, 'envs', f'{reg.project}-{t.variant}', 'bin', 'python')
    if not _env_ok(reg, a, _ssh(m.ssh), python, run):
        return EXIT_ENV
    if a.timeout_min > t.max_minutes:
        _say(f'{m.key}:{t.name}: at most {t.max_minutes} min a job; --timeout {a.timeout_min:g} asked')
        return EXIT_USAGE
    g = OwnerGate(m, run)
    key = run_key(reg.repository, time.time(), os.getpid())
    with tempfile.TemporaryDirectory(prefix='pool-') as tmp:
        with tempfile.TemporaryDirectory(prefix='pool-git-') as gtmp, \
                tarfile.open(fileobj=io.BytesIO(bundle(a.root, files, git=git_dir(a.root, gtmp)))) as tar:
            tar.extractall(tmp, filter='tar')
        g.stage(os.path.join(tmp, 'ws'), key)
    req = {'project': reg.project, 'target': t.name, 'cards': a.ask.cards if t.gpu_model else 0,
           'minutes': a.timeout_min, 'queue_minutes': a.queue_min, 'memory': f'{a.ask.memory_gib}G',
           'command': a.command, 'variables': dict(a.variables),
           'run_url': f'https://github.com/{reg.repository}/actions/runs/0',  # a pool run has no run
           'host': 'pool', 'resume': False}
    ended, offset, started, job = False, 0, None, '?'
    t0 = time.monotonic()
    try:  # from the submit on: a submit that ran sbatch and then failed still leaves a job to cancel
        job = g.json('submit', key, data=json.dumps(req).encode())['job']
        _say(f'{m.key}:{t.name}: job {job} submitted')
        while True:
            st = _status(g, key, sleep)
            state = str(st.get('state', 'UNKNOWN'))
            if state == 'PENDING':
                if time.monotonic() - t0 > a.queue_min * 60:
                    _say(f'{m.key}:{t.name}: not started within {a.queue_min:g} min ({st.get("reason") or "pending"}); withdrawn')
                    return NOT_STARTED
            elif started is None:
                started = time.monotonic()
                _say(f'{m.key}:{t.name}: running on its node')
            if int(st.get('log_size') or 0) > offset:
                chunk = g.call('log', key, str(offset))
                out.write(chunk)
                out.flush()
                offset += len(chunk)
                if offset < int(st.get('log_size') or 0):
                    continue
            if state not in ('PENDING', 'CONFIGURING', 'RUNNING', 'COMPLETING', 'UNKNOWN') or st.get('exit'):
                ended = True
                ex = st.get('exit') or {}
                if not ex:
                    _say(f'{m.key}:{t.name}: job ended {state} without an exit record')
                    return 1
                return _exit_code(ex)
            sleep(POLL)
    finally:
        if not ended:
            try:
                g.json('cancel', key)
                _say(f'{m.key}:{t.name}: job {job} cancelled')
            except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as e:
                _say(f'{m.key}:{t.name}: cancel failed ({e}); the gate\'s reaper ends it at its deadline')


def _status(g: OwnerGate, key: str, sleep: Callable[[float], None], tries: int = 3) -> dict:
    """The job's status, a failed read tried again (``tries`` in all) before it fails the run."""
    for i in range(tries):
        try:
            return g.json('status', key)
        except (RuntimeError, ValueError, OSError, subprocess.SubprocessError):
            if i == tries - 1:
                raise
            sleep(POLL)
    raise AssertionError('unreachable')


def pool_run(reg: Registry, a: RunArgs, *, only: str = '', here_ok: bool = False, run: Runner = subprocess.run,
             popen: Callable = subprocess.Popen, rows: Sequence[Mapping] | None = None) -> int:
    """``pool run``: pick a machine by ``pool status`` and run there."""
    if not 0 < a.timeout_min <= MAX_TIMEOUT_MIN:
        _say(f'--timeout is above 0 and at most {MAX_TIMEOUT_MIN} min (a run\'s directory is removed after '
             f'{STALE_DAYS} days)')
        return EXIT_USAGE
    if only and only.split(':')[0] in reg.machines and reg.machines[only.split(':')[0]].status_only:
        _say(f'{only}: a CI account\'s entry, for pool status only')
        return EXIT_USAGE
    rows = status(reg, run, cards=max(1, a.ask.cards), mem_gib=a.ask.memory_gib) if rows is None else rows
    now, later, skipped = pick(rows, a.ask, reg, here_ok=here_ok, only=only)
    for s in skipped:
        _say(f'skipped {s}')
    files = tree_files(a.root, run)
    _say(f'staging {len(files)} files of {head_of(a.root)}')
    tried = []
    for r in now:
        key = r['key']
        m = reg.machines[key.split(':')[0]]
        _say(f'on {key} (free now)')
        try:
            rc = (run_cluster(reg, m, m.targets[r['target']], a, files, run) if r['target']
                  else run_host(reg, m, a, files, run, popen, wait=False))
        except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as e:
            _say(f'{key}: failed: {e}')
            rc = NOT_STARTED
        if rc != NOT_STARTED:
            return rc
        tried.append(key)
    if later and not a.no_wait:
        r = later[0]
        m = reg.machines[r['key']]
        _say(f'no machine free now; waiting in line on {r["key"]} (queue {r["queue"]})')
        rc = run_host(reg, m, a, files, run, popen, wait=True)
        return EXIT_NO_ROOM if rc == NOT_STARTED else rc
    _say('no machine can start it now' + (f' (tried {", ".join(tried)})' if tried else '')
         + ('; --no-wait' if later else ''))
    return EXIT_NO_ROOM


# ------------------------------------------------------------------ CLI


def _cached(etc: str) -> dict[str, dict]:
    """The capacity file's rows by key (none where it is missing or malformed)."""
    from ci_runners import capacity
    return {r['key']: r for r in capacity.read(capacity.path(etc))['rows']}


def _writer(reg: Registry, etc: str) -> Callable[[list[dict], dict | None], None]:
    """``status``'s ``on_rows``: each machine's rows into the account's capacity file (16.5);
    a file that cannot be written is said once on standard error, and the rows still print."""
    from ci_runners import capacity
    file, order, said = capacity.path(etc), list(reg.machines), []

    def write(rows: list[dict], local: dict | None) -> None:
        try:
            capacity.merge(file, rows, order, local)
        except OSError as e:
            if not said:
                said.append(1)
                print(f'pool: {capacity.NAME} not written: {e.strerror or type(e).__name__}', file=sys.stderr)
    return write


def main(argv: Sequence[str], etc: str) -> int:
    if list(argv[:1]) in (['add'], ['check'], ['ready']):  # adding a machine: ci_runners/pooladd.py
        from ci_runners import pooladd
        return pooladd.main(list(argv), etc)
    import argparse
    ap = argparse.ArgumentParser(prog='ci-run pool')
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('status')
    s.add_argument('--json', action='store_true')
    s.add_argument('--fast', action='store_true', help='from the capacity files, within 2 s (writes nothing)')
    sub.add_parser('probe', help='the local machine\'s probe, as JSON (a forced command: no argument, no stdin)')
    r = sub.add_parser('run')
    r.add_argument('--cpu', type=int, default=8)
    r.add_argument('--mem', type=int, default=32, help='GiB')
    r.add_argument('--gpu', default='', help='MODEL[:COUNT], MODEL "any" for any')
    r.add_argument('--on', default='', help='a machine or <machine>:<target> only')
    r.add_argument('--here', action='store_true', help='the caller\'s own host may run it too')
    r.add_argument('--timeout', type=float, default=DEFAULT_TIMEOUT_MIN, help='min')
    r.add_argument('--queue-minutes', type=float, default=DEFAULT_QUEUE_MIN)
    r.add_argument('--no-wait', action='store_true')
    r.add_argument('--env-mismatch', choices=('refuse', 'warn'), default='refuse')
    r.add_argument('--python', default='', help='your env\'s python (default: the one running this)')
    r.add_argument('--dir', default='.', help='the checkout to stage (default: .)')
    r.add_argument('command', nargs=argparse.REMAINDER)
    a = ap.parse_args(list(argv))
    path = os.path.join(etc, 'pool.toml')
    try:
        reg = load_file(path)
    except (OSError, RegistryError) as e:
        print(f'pool: {e}', file=sys.stderr)
        return EXIT_USAGE
    if a.cmd == 'probe':
        try:
            print(json.dumps(local_probe(reg, etc), sort_keys=True))
        except RegistryError as e:
            print(f'pool: {e}', file=sys.stderr)
            return EXIT_USAGE
        return 0
    if a.cmd == 'status':
        rows = fast_status(reg, etc=etc) if a.fast else status(reg, etc=etc, on_rows=_writer(reg, etc), cached=_cached(etc))
        from ci_runners import capacity
        rows = capacity.flag_policy(rows)  # a host whose repository entries differ from the others' (19.6)
        print(json.dumps(rows, indent=1, sort_keys=True) if a.json else table(rows))
        return 0
    cmd = a.command[1:] if a.command[:1] == ['--'] else a.command
    if not cmd:
        ap.error('a command is required after --')
    try:
        model, cards = parse_gpu(a.gpu) if a.gpu else ('', 0)
    except ValueError as e:
        ap.error(str(e))
    root = subprocess.run(['git', '-C', a.dir, 'rev-parse', '--show-toplevel'], capture_output=True, text=True).stdout.strip()
    if not root:
        ap.error(f'{a.dir}: not a git checkout')
    ra = RunArgs(shlex.join(cmd) if len(cmd) > 1 else cmd[0], root, Ask(a.cpu, a.mem, cards, model), a.timeout,
                 a.queue_minutes, a.no_wait, a.env_mismatch, a.python, forwarded(os.environ))
    return pool_run(reg, ra, only=a.on, here_ok=a.here)


if __name__ == '__main__':
    if sys.argv[1:2] == ['deny'] and len(sys.argv) == 3:
        with open(sys.argv[2], 'rb') as f:
            from ci_runners import inventory
            print('\n'.join(sorted(inventory.hash_token(t) for t in deny_tokens(tomllib.load(f)))))
        sys.exit(0)
    print(__doc__.split('CLI::', 1)[1], file=sys.stderr)
    sys.exit(EXIT_USAGE)
