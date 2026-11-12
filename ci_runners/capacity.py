"""The capacity file and its reader (decision 16.5): what the plan-time route reads.

**The file** is ``<data>/ci-runners/state/capacity.json`` (``path``: ``state/`` beside the
account's ``etc``), mode 600, replaced by an atomic rename under ``capacity.lock`` beside
it. ``pool status`` writes it, one machine's rows as soon as that machine answers
(``merge``), and the account's ``ci-capacity.timer`` runs ``pool status`` every 30 s, so
the other hosts' rows stay cached there; a cluster is asked again only once its rows are
``capacity.cluster_seconds`` old (the registry's, 300 s by default: its login node is
shared); presence (16.4, from
R3) rewrites the host's own part at every tick (``pool.record_local``). Its keys:

.. code-block:: json

    {"schema": 1,
     "written_at": 1762905600.0,
     "rows": [{"key": "hostb", "read_at": 1762905599.2, "...": "the --json row's fields"}],
     "local": {"key": "hostb", "read_at": 1762905599.2, "probe": {"...": "pool.probe's answer"}}}

``rows`` are ``pool status --json``'s rows, in the registry's order, each with
``read_at``, the epoch second its machine answered. A host row has ``label_runners``,
``{label: {"registered": n, "online": n, "free": n}}``, where the host's inventory is
read: each runner instance counts once for each label it carries; ``online`` the ones
with a live process, ``free`` those without a ``Runner.Worker`` (no job), ``None`` where
``/proc`` hides them. From decision 19.4 a host row read by the CI account's own probe also
has ``role``, ``pools`` (``pool.pool_table``: each runner pool's labels, counts, its
``listening`` and ``yielded`` runners and its slot), ``policy`` (``pool.policy_digests``)
and its health gate's problem, and ``tier`` from the registry that read it; a cluster target row has ``memory_gib``, ``max_minutes`` and an
opaque ``partition``. Every one is optional: schema 1 stays. A cluster target row has
``jobs_free`` (decision 15.7). ``local``
is the raw probe of the registry's ``local`` machine, which ``ci-run pool probe`` (the
forced probe, 15.1 item 8) returns while it is fresh instead of probing again, so no
``nvidia-smi`` runs for another host's ``pool status --fast``.

**The staleness rule**: a host row read more than ``STALE_S`` (120 s) ago, a cluster
target row more than ``CLUSTER_STALE_S`` (600 s) ago, counts as down (``stale``): it gets
the problem ``stale``, no free slot or core, ``start_now`` false and no free job. So a
stale cluster row says "the cluster cannot start it now", and the route keeps the part on
its shared label. ``pool status --fast`` applies it to its rows, and ``load`` to what it
returns.

**Policy drift** (decision 19.6): ``flag_policy`` compares the ``repositories`` digests of
the host rows that report one; where they differ, each host off the norm (the value most of
them hold; none on a tie) gets ``policy_drift`` and a warning in ``warnings``. A warning is
not a problem: the row stays up. ``rows_from`` applies it, and ``pool status`` prints it.

**The reader** (for the route, R5): ``load(etc)`` returns one ``Row`` per host and per
cluster target, from ``pool status --fast`` (``fast=True``, the default: the other hosts'
files through their forced probes, this host's slots read now, the cluster from the cached
file) or from this account's file alone (``fast=False``, no ssh). A row is ``down`` when it
has any problem (stale, unreachable, drained, no runner online, a cluster unreachable, ...):
15.1's "a host with a problem gets no part". A stale cluster target row is ``down`` with
``jobs_free`` 0: the cluster cannot start a part now, and the route leaves the part on its
shared label (it never overflows on a figure that old). ``CapacityError`` says why nothing could be
read (no registry, no file, a file of another schema). ``rows_from`` turns ``pool status
--fast --json`` output read elsewhere into the same rows.

Stdlib only.
"""
from __future__ import annotations

import dataclasses
import fcntl
import json
import os
import time
from collections.abc import Callable, Mapping, Sequence

SCHEMA = 1
#: s after which a host's row counts as down (decision 16.5)
STALE_S = 120
#: s after which a cluster target's row does: its figures are read every ``capacity.cluster_seconds``
#: of the registry (300 by default), to spare the shared login node
CLUSTER_STALE_S = 600
STALE = 'stale'
NAME = 'capacity.json'


class CapacityError(Exception):
    """Nothing could be read: no registry, no capacity file, or one of another schema."""


def path(etc: str) -> str:
    """``state/capacity.json`` beside the account's ``etc`` (``<data>/ci-runners/state``)."""
    return os.path.join(os.path.dirname(os.path.abspath(etc)), 'state', NAME)


def read(file: str) -> dict:
    """The file's document, or an empty one of this schema where it is missing, malformed or
    of another schema (a writer then starts afresh). Never raises."""
    try:
        with open(file, encoding='utf-8') as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return {'schema': SCHEMA, 'rows': []}
    if not isinstance(doc, dict) or doc.get('schema') != SCHEMA or not isinstance(doc.get('rows'), list):
        return {'schema': SCHEMA, 'rows': []}
    doc['rows'] = [r for r in doc['rows'] if isinstance(r, dict) and isinstance(r.get('key'), str)
                   and isinstance(r.get('problems', []), list)]
    return doc


#: the warning of a host whose hook policy's repository entries differ from the others' (19.6)
DRIFT = 'policy: repository entries differ from {n} other host(s)'


def policy_drift(rows: Sequence[Mapping]) -> dict[str, int]:
    """The host rows whose repository entries' digest (``policy.repositories``) is off the
    norm, the digest most of the reporting host rows hold (none where two or more tie), each
    with how many other reporting hosts hold another digest than its own. Pure."""
    digs = {str(r['key']): r['policy']['repositories'] for r in rows
            if r.get('access') != 'slurm' and not r.get('target') and isinstance(r.get('policy'), dict)
            and isinstance(r['policy'].get('repositories'), str)}
    counts: dict[str, int] = {}
    for d in digs.values():
        counts[d] = counts.get(d, 0) + 1
    if len(counts) < 2:
        return {}
    top = max(counts.values())
    norm = [d for d, c in counts.items() if c == top]
    norm_d = norm[0] if len(norm) == 1 else None
    return {k: sum(1 for d2 in digs.values() if d2 != d) for k, d in digs.items() if d != norm_d}


def flag_policy(rows: Sequence[Mapping]) -> list[dict]:
    """``rows`` with ``policy_drift`` and its warning on each host off the norm
    (``policy_drift``), and neither on the others (a row read back from a file is judged
    afresh). Pure."""
    drift = policy_drift(rows)
    out = []
    for r in rows:
        r = dict(r)
        warnings = [w for w in r.get('warnings') or () if not str(w).startswith(DRIFT.split('{')[0])]
        r.pop('policy_drift', None)
        if r.get('key') in drift:
            warnings.append(DRIFT.format(n=drift[r['key']]))
            r['policy_drift'] = True
        if warnings:
            r['warnings'] = warnings
        else:
            r.pop('warnings', None)
        out.append(r)
    return out


def machine_of(key: str) -> str:
    """A row's machine: its key, or a cluster target's ``<machine>`` of ``<machine>:<target>``."""
    return key.split(':', 1)[0]


def merge(file: str, rows: Sequence[Mapping], order: Sequence[str], local: Mapping | None = None,
          now: Callable[[], float] = time.time) -> None:
    """Replace the rows of each machine that ``rows`` name, drop those of machines not in
    ``order`` (the registry's machines, in its order), and with ``local`` set the local
    probe; under ``capacity.lock``, by an atomic rename (mode 600)."""
    d = os.path.dirname(file)
    os.makedirs(d, mode=0o700, exist_ok=True)
    fd = os.open(os.path.join(d, 'capacity.lock'), os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        doc = read(file)
        new = {machine_of(r['key']) for r in rows}
        rank = {k: i for i, k in enumerate(order)}
        kept = [r for r in doc['rows'] if machine_of(r['key']) not in new and machine_of(r['key']) in rank]
        merged = sorted(kept + [dict(r) for r in rows], key=lambda r: rank.get(machine_of(r['key']), len(rank)))
        out = {'schema': SCHEMA, 'written_at': round(now(), 3), 'rows': merged}
        old = doc.get('local')
        if local is not None:
            out['local'] = dict(local)
        elif isinstance(old, dict) and old.get('key') in rank:
            out['local'] = old
        tmp = f'{file}.{os.getpid()}.tmp'
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o600), 'w', encoding='utf-8') as f:
            json.dump(out, f, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, file)
    finally:
        os.close(fd)


def fresh_local(file: str, key: str, now: float) -> dict | None:
    """The local probe the file holds for machine ``key``, with its ``age`` (s), while it is
    younger than ``STALE_S``; else None. What the forced probe returns (16.5)."""
    loc = read(file).get('local')
    if not isinstance(loc, dict) or loc.get('key') != key or not isinstance(loc.get('probe'), dict):
        return None
    at = loc.get('read_at')
    if type(at) not in (int, float) or not 0 <= now - at <= STALE_S:
        return None
    return {**loc['probe'], 'age': round(now - at, 1)}


def age(row: Mapping, now: float) -> float | None:
    """Seconds since ``row`` was read, or None where it says not."""
    at = row.get('read_at')
    return max(0.0, now - at) if type(at) in (int, float) else None


def stale_s(row: Mapping) -> int:
    """A row's bound: ``CLUSTER_STALE_S`` for a cluster target's, ``STALE_S`` for a host's."""
    return CLUSTER_STALE_S if row.get('access') == 'slurm' else STALE_S


def is_stale(row: Mapping, now: float) -> bool:
    """The staleness rule: read more than ``stale_s`` ago, or at no known time."""
    a = age(row, now)
    return a is None or a > stale_s(row)


def mark(row: Mapping, now: float) -> dict:
    """``row`` as its age leaves it: a stale one is down (problem ``stale``, nothing free,
    no start). Its ``gpu_free`` keyed by card number again (JSON keeps them as strings).
    Pure."""
    out = dict(row)
    if isinstance(row.get('gpu_free'), dict):
        out['gpu_free'] = {int(k): v for k, v in row['gpu_free'].items() if str(k).isdigit()}
    if is_stale(row, now) and not any(str(p).startswith(STALE) for p in row.get('problems', [])):  # once
        a = age(row, now)
        out['problems'] = [*row.get('problems', []), STALE if a is None else f'{STALE} (read {a:.0f} s ago)']
        out.update(start_now=False, slots_free=0, cpus_free=0, gpu_free={})
        if 'jobs_free' in out:
            out['jobs_free'] = 0
    return out


@dataclasses.dataclass(frozen=True)
class Runners:
    """A label's runner instances on one host: registered (by the inventory), online (a live
    process; None where not known) and free (online without a job; None where not known)."""
    registered: int
    online: int | None
    free: int | None


@dataclasses.dataclass(frozen=True)
class Row:
    """One host or cluster target, as the route reads it."""
    key: str  # '<machine>' or '<machine>:<target>'
    machine: str
    target: str  # '' on a host
    access: str  # 'local', 'ssh' or 'slurm'
    labels: Mapping[str, str]  # the registry's labels per group (HostState.labels)
    runners: Mapping[str, Runners]  # per runner label; {} where the host's inventory is not read
    problems: tuple[str, ...]  # 'stale (read N s ago)' among them past STALE_S
    age: float | None  # s since its machine answered; None: not known (and so stale)
    down: bool  # any problem: no part goes there (15.1)
    jobs_free: int | None  # a cluster target's jobs of its size that start now (15.7); None on a host
    slots_free: int
    row: Mapping  # the whole row, for the rest (cpus_free, gpu_free, mem_free_gib, slots, ...)


def _runners(v: object) -> dict[str, Runners]:
    out: dict[str, Runners] = {}
    if not isinstance(v, dict):
        return out
    for label, c in v.items():
        if isinstance(label, str) and isinstance(c, dict) and type(c.get('registered')) is int:
            def n(k):
                return c.get(k) if type(c.get(k)) is int else None
            out[label] = Runners(c['registered'], n('online'), n('free'))
    return out


def rows_from(rows: Sequence[Mapping], now: float | None = None) -> list[Row]:
    """``Row``\\ s from ``pool status --json`` rows (or the file's), the staleness rule
    applied. Pure but for the clock."""
    now = time.time() if now is None else now
    out = []
    for r in flag_policy(rows):
        m = mark(r, now)
        jf = m.get('jobs_free')
        out.append(Row(key=m['key'], machine=machine_of(m['key']), target=str(m.get('target') or ''),
                       access=str(m.get('access', '')), labels=dict(m.get('labels') or {}),
                       runners=_runners(m.get('label_runners')), problems=tuple(m.get('problems') or ()),
                       age=age(r, now), down=bool(m.get('problems')), jobs_free=jf if type(jf) is int else None,
                       slots_free=int(m.get('slots_free') or 0), row=m))
    return out


def load(etc: str = '', fast: bool = True, now: float | None = None, run: Callable | None = None) -> list[Row]:
    """The rows the route reads: ``pool status --fast``'s (``fast``) or this account's file's
    alone, each with its age and the staleness rule applied. ``etc``: the account's
    (``cli.etc_dir`` by default). Raises ``CapacityError`` where nothing can be read."""
    from ci_runners import cli, pool
    etc = etc or cli.etc_dir()
    if fast:
        try:
            reg = pool.load_file(os.path.join(etc, 'pool.toml'))
        except (OSError, pool.RegistryError) as e:
            raise CapacityError(f'no registry: {e}') from None
        kw = {'run': run} if run is not None else {}
        return rows_from(pool.fast_status(reg, etc=etc, **kw), now)
    file = path(etc)
    if not os.path.exists(file):
        raise CapacityError(f'no capacity file {NAME}: pool status writes it (ci-capacity.timer)')
    doc = read(file)
    if not doc['rows']:
        raise CapacityError(f'{NAME}: no rows, or not of schema {SCHEMA}')
    return rows_from(doc['rows'], now)
