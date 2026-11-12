"""The plan-time route and the retry table (decision 16.5; RUN-10 R5).

**The route** (``route``) runs once, in the reusable workflow's plan job, over the rows of
the capacity reader (``capacity.load``, R4). Each plan part names its *need* (``cpu``,
``gpu``, ``gpu2`` or ``timing``), its ``minutes`` on the reference host and, optionally,
its ``memory_gib``. Its labels are ``[self-hosted, ci-<need>]``, plus ``mem-big`` where its
memory exceeds the largest slot of the label without it, and its job's
``timeout_minutes`` is its minutes times the largest ``speed`` of the runners those labels
reach, plus 15 (16.2). It never names a host label. It overflows only: a part goes to
``ci-slurm`` with the cheapest cluster target that fits when no host has a free runner of
its labels left (after the plan's earlier parts) and the target can start it now
(``jobs_free``, each part booked against it); ``timing`` never overflows. A label that no
host serves is an error at plan time (a job queued on it would wait 24 h); a runner that
presence holds back for a passing cause is still registered on a host that is up, so its
label is served.

*Facts and proof* (an unproven fact counts as false, so the route takes no exceptional
step on a guess):

- a host *serves* a label set when its row is up (no problem: not stale, reachable, not
  drained, a runner online) and its inventory registers runners carrying each label
  (``Row.runners``); a host that is up but whose runner table is not read (an older
  release, ``{}``) *might* serve any label;
- *a label no host serves* is an error only when no host serves it and none might;
- *free* runners are the ``free`` counts of the serving hosts, the smallest of the set's
  labels per host; free is *unknown* where a host might serve the set or hides its
  ``/proc`` (``free`` None). A part overflows only where free is known and every free
  runner is booked by the plan's earlier parts. A part placed on a host label set books
  one free runner of its capability label;
- the *cluster can start it now* where a host serves ``ci-slurm`` (with a free
  ``ci-slurm`` runner left, where that count is known) and a target row is up, its
  ``numerics_ok`` true, of the need's class (``cpu``: no card model; ``gpu``: a card model;
  ``gpu2``: a card model and ``multi_gpu``), with ``jobs_free`` left (two for ``gpu2``;
  targets that name the same ``partition`` share it), and, where the row reports them, a
  ``memory_gib`` that holds the part's memory and a ``max_minutes`` that holds its bound.
  A part that needs ``mem-big`` overflows only to a target that reports enough memory.
  Targets are tried by ``price``, then key;
- *slots*: a host row may carry ``slot_pools``, ``[{"labels": [...], "memory_gib": n,
  "speed": x}]``, one per pool with a ``slot`` table (16.2). ``mem-big`` and the memory
  error need every serving host to report its pools of the label; a label whose speeds
  are not reported counts 1.0, as a pool without ``slot`` runs its minutes unscaled.

**Unreadable capacity** (the reader's ``CapacityError``, or no rows at all): every part
goes to its shared label, without ``mem-big``, overflow or the plan-time error, and its
``why`` says so: a missing capacity file must not fail every plan.

**The retry table** (``retry``): a first attempt's row of ``outcome.hint`` (its ``where``,
or derived from its outcome and ``next``) and its routed part give the retry's labels:

====================  ==========================================================
``cool-card``         the cool card's host label, ``<host>-gpu<M>``
``same-host``         the first attempt's host label of its class; a cluster part
                      ``ci-slurm`` and its target
``more-memory``       the part's labels plus ``mem-big`` (a cluster part: its
                      need's host label plus ``mem-big``), with ``resume``
``any``               the part's labels again
``fallback``          the part's needs routed again
====================  ==========================================================

A part is retried at most once. The host labels come from the first attempt's record
(``host``, ``pool_labels``, ``cool_card``), never from ``retry-on``.

``ci-run route [--rows FILE]`` reads the plan (schema 1, design §4.10) on stdin and
writes the routed plan on stdout: exit 0; 1 for a plan-time route error (each named on
stderr, the label first); 2 for a plan that does not load. Stdlib only.
"""
from __future__ import annotations

import json
import math
import re
import sys
from collections.abc import Mapping, Sequence

BASE = 'self-hosted'
#: a need to its capability label (16.1)
NEEDS = {'cpu': 'ci-cpu', 'gpu': 'ci-gpu', 'gpu2': 'ci-gpu2', 'timing': 'ci-timing'}
SLURM = 'ci-slurm'
MEM_BIG = 'mem-big'
#: the labels a route may name: never a host's (16.1)
SHARED = frozenset({BASE, SLURM, MEM_BIG, *NEEDS.values()})
#: a job's bound past its parts' scaled minutes (16.2)
GRACE_MIN = 15
MAX_MINUTES = 240
MAX_PARTS = 256
PART_KEYS = {'id': True, 'command': True, 'needs': True, 'minutes': True, 'memory_gib': False, 'results': False}
ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*\Z')
#: the five rows of ``outcome.hint`` (16.5)
ROWS = ('cool-card', 'same-host', 'more-memory', 'any', 'fallback')
EXIT_ROUTE, EXIT_PLAN = 1, 2


class PlanError(ValueError):
    """The plan does not load (design §4.10): the consumer's to fix."""


class RouteError(ValueError):
    """A plan-time route error (16.5): ``errors``, one line each, the label first."""

    def __init__(self, errors: Sequence[str]):
        self.errors = list(errors)
        super().__init__('; '.join(self.errors))


# ------------------------------------------------------------------ the plan

def _num(v: object) -> bool:
    return type(v) in (int, float) and math.isfinite(v)


def load_plan(doc: object) -> list[dict]:
    """The parts of a schema 1 plan (design §4.10), checked; ``PlanError`` names each fault."""
    if not isinstance(doc, dict) or set(doc) != {'schema', 'parts'}:
        raise PlanError('a plan is an object with exactly "schema" and "parts"')
    if doc['schema'] != 1:
        raise PlanError(f'plan schema {doc["schema"]!r}: this release reads schema 1')
    parts = doc['parts']
    if not isinstance(parts, list) or not 1 <= len(parts) <= MAX_PARTS:
        raise PlanError(f'"parts" is a list of 1 to {MAX_PARTS} parts')
    errors, seen = [], set()
    for i, p in enumerate(parts):
        if not isinstance(p, dict):
            errors.append(f'part {i}: not an object')
            continue
        name = p.get('id') if isinstance(p.get('id'), str) else f'part {i}'
        bad = sorted(set(p) - set(PART_KEYS))
        missing = [k for k, req in PART_KEYS.items() if req and k not in p]
        if bad or missing:
            errors.append(f'{name}: ' + '; '.join(([f'unknown key(s) {bad}'] if bad else [])
                                                  + ([f'missing {missing}'] if missing else [])))
            continue
        if not ID.match(p['id']) or p['id'] in seen:
            errors.append(f'{name}: an id is [A-Za-z0-9][A-Za-z0-9_.-]*, unique in the plan')
        seen.add(p['id'])
        if not isinstance(p['command'], str) or not p['command'].strip():
            errors.append(f'{name}: command is a non-empty string')
        if p['needs'] not in NEEDS:
            errors.append(f'{name}: needs {p["needs"]!r} is not one of {", ".join(NEEDS)}')
        if not _num(p['minutes']) or not 0 < p['minutes'] <= MAX_MINUTES:
            errors.append(f'{name}: minutes is a number in (0, {MAX_MINUTES}]')
        if 'memory_gib' in p and (not _num(p['memory_gib']) or p['memory_gib'] <= 0):
            errors.append(f'{name}: memory_gib is a positive number')
    if errors:
        raise PlanError('; '.join(errors))
    return [dict(p) for p in parts]


# ------------------------------------------------------------------ the facts

def _hosts(rows: Sequence) -> list:
    return [r for r in rows if not r.target]


def _has(r, labels: Sequence[str]) -> bool:
    return all(r.runners.get(lb) is not None and r.runners[lb].registered > 0 for lb in labels)


def _pools(r) -> list[dict] | None:
    """A host row's ``slot_pools`` where well-formed, else None (not reported)."""
    v = r.row.get('slot_pools')
    if not isinstance(v, list):
        return None
    out = []
    for p in v:
        if not isinstance(p, dict) or not isinstance(p.get('labels'), list):
            return None
        mem, speed = p.get('memory_gib'), p.get('speed', 1.0)
        out.append({'labels': {str(x) for x in p['labels']},
                    'memory_gib': mem if _num(mem) and mem > 0 else None,
                    'speed': speed if _num(speed) and speed > 0 else 1.0})
    return out


class _Facts:
    """What the rows prove about a label set, with the plan's bookings."""

    def __init__(self, rows: Sequence):
        self.rows = list(rows)
        self.up = [r for r in _hosts(self.rows) if not r.down]
        self.unknown = [r for r in self.up if not r.runners]  # up, its runner table not read
        self.booked: dict[str, int] = {}  # capability label (or ci-slurm) -> runners booked by this plan
        self.jobs: dict[tuple[str, str], int] = {}  # (machine, partition) -> cluster jobs booked

    def serving(self, labels: Sequence[str]) -> list:
        return [r for r in self.up if _has(r, labels)]

    def unserved(self, labels: Sequence[str]) -> bool:
        """Proven: no host that is up serves ``labels``, and none might."""
        return not self.serving(labels) and not self.unknown

    def free(self, labels: Sequence[str]) -> int | None:
        """The free runners of ``labels`` left after the bookings, or None where unknown."""
        if self.unknown:
            return None
        total = 0
        for r in self.serving(labels):
            counts = [r.runners[lb].free for lb in labels]
            if any(c is None for c in counts):
                return None
            total += min(counts)
        return max(0, total - self.booked.get(labels[0], 0))

    def book(self, label: str) -> None:
        self.booked[label] = self.booked.get(label, 0) + 1

    def slots(self, cap: str) -> list[dict] | None:
        """The slot pools of ``cap`` on its serving hosts, or None where a serving host
        does not report them (or a host might serve it)."""
        if self.unknown:
            return None
        out = []
        for r in self.serving([cap]):
            mine = [p for p in (_pools(r) or []) if cap in p['labels']]
            if not mine or any(p['memory_gib'] is None for p in mine):
                return None
            out += mine
        return out

    def speed(self, labels: Sequence[str]) -> float:
        """The largest speed of the runners ``labels`` reach, on every host row that
        reports its pools (a down host may come back while the job waits); 1.0 unreported."""
        speeds = [p['speed'] for r in _hosts(self.rows) for p in (_pools(r) or []) if set(labels) <= p['labels']]
        return max(speeds, default=1.0)


def _timeout(minutes: float, speed: float) -> int:
    return math.ceil(round(minutes * speed, 6)) + GRACE_MIN


def _target_kind_fits(need: str, t) -> bool:
    gpu = bool(t.row.get('gpu_model'))
    return {'cpu': not gpu, 'gpu': gpu, 'gpu2': gpu and bool(t.row.get('multi_gpu'))}.get(need, False)


def _overflow(f: _Facts, part: Mapping, labels: Sequence[str]) -> tuple[object, str]:
    """The cluster target a part overflows to, or (None, why not)."""
    need = part['needs']
    if need == 'timing':
        return None, 'timing never overflows'
    if not f.serving([SLURM]):
        return None, f'no host serves {SLURM}'
    left = f.free([SLURM])
    if left is not None and left <= 0:
        return None, f'no free {SLURM} runner'
    jobs = 2 if need == 'gpu2' else 1
    mem = part.get('memory_gib')
    why = 'no cluster target can start it now'
    for t in sorted((r for r in f.rows if r.target), key=lambda r: (float(r.row.get('price') or 0.0), r.key)):
        room = (t.machine, str(t.row.get('partition') or t.target))
        tmem, tmax = t.row.get('memory_gib'), t.row.get('max_minutes')
        if t.down or not t.row.get('numerics_ok') or not _target_kind_fits(need, t):
            continue
        if (t.jobs_free or 0) - f.jobs.get(room, 0) < jobs:
            continue
        if mem is not None and (MEM_BIG in labels and not _num(tmem) or _num(tmem) and tmem < mem):
            why = f'no cluster target holds {mem:g} GiB'
            continue
        if _num(tmax) and _timeout(part['minutes'], float(t.row.get('speed') or 1.0)) - GRACE_MIN > tmax:
            continue
        f.jobs[room] = f.jobs.get(room, 0) + jobs
        return t, ''
    return None, why


def _route_one(f: _Facts | None, part: Mapping, unread: str, errors: list[str]) -> dict:
    cap = NEEDS[part['needs']]
    labels = [BASE, cap]
    out = {**part, 'labels': labels, 'where': 'host', 'target': '', 'timeout_minutes': _timeout(part['minutes'], 1.0)}
    if f is None:
        out['why'] = f'capacity unreadable ({unread}): {cap}, no overflow'
        return out
    if f.unserved([cap]):
        errors.append(f'{cap}: part {part["id"]}: no host serves the label '
                      '(every host that carries it is down, drained, unreachable or stale)')
        out['why'] = 'no host serves it'
        return out
    notes = []
    mem = part.get('memory_gib')
    if mem is not None:
        pools = f.slots(cap)
        if pools is None:
            notes.append('slot memory not reported')
        else:
            plain = max((p['memory_gib'] for p in pools if MEM_BIG not in p['labels']), default=0)
            big = max((p['memory_gib'] for p in pools if MEM_BIG in p['labels']), default=0)
            if mem > plain and big >= mem:
                labels.append(MEM_BIG)
                notes.append(f'{mem:g} GiB > {plain:g} GiB')
            elif mem > plain:
                errors.append(f'{cap}: part {part["id"]}: {mem:g} GiB fits no slot of the label, '
                              f'{MEM_BIG} included (largest {max(plain, big):g} GiB)')
                out['why'] = 'fits no slot'
                return out
    if f.unserved(labels[1:]):
        errors.append(f'{" + ".join(labels[1:])}: part {part["id"]}: no host serves the labels together')
        out['why'] = 'no host serves it'
        return out
    out['timeout_minutes'] = _timeout(part['minutes'], f.speed(labels[1:]))
    free = f.free(labels[1:])
    if free is None or free > 0:
        f.book(cap)
        notes.append('free runner unknown' if free is None else f'{free} free')
        out['why'] = '; '.join(notes)
        return out
    t, why = _overflow(f, part, labels)
    if t is None:
        f.book(cap)
        out['why'] = '; '.join([*notes, f'0 free; {why}'])
        return out
    f.book(SLURM)
    speed = float(t.row.get('speed') or 1.0)
    out.update(labels=[BASE, SLURM], where='slurm', target=t.target, timeout_minutes=_timeout(part['minutes'], speed),
               why='; '.join([*notes, f'0 free; {t.key} can start it now']))
    return out


def route(parts: Sequence[Mapping], rows: Sequence | None, unread: str = '') -> list[dict]:
    """Each part with its ``labels``, ``timeout_minutes``, ``where`` (``host`` or ``slurm``),
    ``target`` (a cluster part's, else '') and ``why``, in plan order. ``rows`` are
    ``capacity.Row``\\ s; None (or empty) where the capacity could not be read, ``unread``
    the reader's reason. Raises ``RouteError`` with every plan-time error. Pure and
    deterministic: the plan's earlier parts book the runners and cluster jobs they take.
    An unknown need raises ``PlanError``."""
    bad = [f'{p.get("id")}: needs {p.get("needs")!r} is not one of {", ".join(NEEDS)}'
           for p in parts if p.get('needs') not in NEEDS]
    if bad:
        raise PlanError('; '.join(bad))
    f = _Facts(rows) if rows else None
    unread = unread or 'no rows'
    errors: list[str] = []
    out = [_route_one(f, p, unread, errors) for p in parts]
    if errors:
        raise RouteError(errors)
    return out


# ------------------------------------------------------------------ the retry table

def row_of(outcome: str, next_: str) -> str:
    """A first attempt's row of the table from its outcome and ``next`` (``outcome.hint``):
    one of ``ROWS``, or '' for no retry."""
    if next_ == 'fallback':
        return 'fallback'
    if next_ != 'retry':
        return ''
    return {'thermal': 'cool-card', 'oom': 'more-memory', 'reboot': 'same-host', 'unavailable': 'same-host',
            'lost': 'any', 'stopped': 'any'}.get(outcome, '')


def _host_labels(first: Mapping) -> list[str]:
    host = str(first.get('host') or '')
    own = [str(x) for x in first.get('pool_labels') or ()]
    return [x for x in own if x not in SHARED and x != host and not re.fullmatch(r'.+-gpu\d+', x)] if host else []


def retry(part: Mapping, first: Mapping, rows: Sequence | None = None, unread: str = '') -> dict:
    """The retry of a routed ``part`` after its ``first`` attempt (16.5): ``retry`` (bool),
    ``row``, ``labels``, ``where``, ``target``, ``timeout_minutes``, ``resume`` and ``why``.
    ``first`` holds the attempt's ``outcome``, ``next``, ``attempt`` and, where known,
    ``where`` (the hint's row), ``host``, ``pool_labels`` and ``cool_card`` (its record).
    ``rows``: the capacity now, for the fallback's route and a check that a ``mem-big``
    retry has a host. Pure."""
    row = str(first.get('where') or '') or row_of(str(first.get('outcome', '')), str(first.get('next', '')))
    same = {'retry': True, 'row': row, 'labels': list(part['labels']), 'where': part.get('where', 'host'),
            'target': part.get('target', ''), 'timeout_minutes': part['timeout_minutes'], 'resume': False}
    if int(first.get('attempt') or 1) > 1 or first.get('next') not in ('retry', 'fallback') or row not in ROWS:
        return {**same, 'retry': False, 'row': row, 'labels': [], 'why': 'no retry: final, or retried once already'}
    host = str(first.get('host') or '')
    if row == 'cool-card':
        card = first.get('cool_card')
        if host and type(card) is int and card >= 0:
            return {**same, 'labels': [BASE, f'{host}-gpu{card}'], 'where': 'host', 'target': '',
                    'why': f'thermal: a cool card of {host}'}
        row = 'fallback'  # no cool card recorded: the thermal row's other branch
    if row == 'same-host':
        if part.get('where') == 'slurm':
            return {**same, 'why': f'same target {part.get("target")}'}
        own = _host_labels(first)
        if own:
            return {**same, 'labels': [BASE, *own], 'where': 'host', 'target': '', 'why': f'same host {host}'}
        return {**same, 'row': 'any', 'why': 'same host: its labels not recorded; the part\'s labels again'}
    if row == 'more-memory':
        labels = [BASE, NEEDS[part['needs']]] if part.get('where') == 'slurm' else list(part['labels'])
        labels += [] if MEM_BIG in labels else [MEM_BIG]
        if rows and _Facts(rows).unserved(labels[1:]):
            return {**same, 'retry': False, 'labels': [], 'why': f'oom: no host serves {" + ".join(labels[1:])}'}
        return {**same, 'labels': labels, 'where': 'host', 'target': '', 'resume': True, 'why': 'oom: more memory'}
    if row == 'any':
        return {**same, 'why': 'the part\'s labels again'}
    plan = {k: v for k, v in part.items() if k in PART_KEYS}
    try:
        again = route([plan], rows, unread)[0]
    except RouteError as e:
        return {**same, 'retry': False, 'labels': [], 'why': f'fallback: {e}'}
    return {**same, 'row': 'fallback', 'labels': again['labels'], 'where': again['where'], 'target': again['target'],
            'timeout_minutes': again['timeout_minutes'], 'why': f'routed again: {again["why"]}'}


# ------------------------------------------------------------------ ci-run route

_PATH = re.compile(r"(?<![\w.:/-])/(?:[^\s'\"/]+/)*([^\s'\"/]*)")


def redact(text: str) -> str:
    """``text`` with each absolute path cut to its last name: a job log shows no host's
    paths (design §3.3)."""
    return _PATH.sub(lambda m: m.group(1) or '/', text)


def main(argv: Sequence[str], etc: str, stdin=None, stdout=None, stderr=None) -> int:
    """``ci-run route [--rows FILE]``: the plan on stdin, the routed plan on stdout."""
    import argparse
    stdin, stdout, stderr = stdin or sys.stdin, stdout or sys.stdout, stderr or sys.stderr
    ap = argparse.ArgumentParser(prog='ci-run route')
    ap.add_argument('--rows', default='', help='pool status --fast --json output to read instead of the capacity')
    a = ap.parse_args(list(argv))
    try:
        parts = load_plan(json.load(stdin))
    except (ValueError, PlanError) as e:
        print(f'route: the plan does not load: {e}', file=stderr)
        return EXIT_PLAN
    from ci_runners import capacity
    rows, unread = None, ''
    try:
        if a.rows:
            with open(a.rows, encoding='utf-8') as f:
                got = json.load(f)
            if not isinstance(got, list) or not all(isinstance(r, dict) and isinstance(r.get('key'), str) for r in got):
                raise capacity.CapacityError(f'{a.rows}: not a list of rows')
            rows = capacity.rows_from(got)
        else:
            rows = capacity.load(etc)
    except (capacity.CapacityError, OSError, ValueError) as e:
        unread = redact(str(e) or type(e).__name__)
    try:
        out = route(parts, rows, unread)
    except RouteError as e:
        for line in e.errors:
            print(f'route: {line}', file=stderr)
        return EXIT_ROUTE
    json.dump({'schema': 1, 'capacity': f'unreadable: {unread or "no rows"}' if not rows else 'read', 'parts': out},
              stdout, sort_keys=True)
    stdout.write('\n')
    if not rows:
        print(f'route: capacity unreadable ({unread or "no rows"}): every part on its shared label, no overflow',
              file=stderr)
    return 0
