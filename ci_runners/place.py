"""``place``: hosts and cards for a consumer's parts, from probe results (design §4.2,
D3, D7). Pure: the probes (ssh, ``systemctl``, ``nvidia-smi``, the proxy) are the
action's.
"""
from __future__ import annotations

import dataclasses
import math
from collections.abc import Iterable, Mapping, Sequence

CLASSES = ('cpu', 'gpu', 'compile')
#: the pool a part class runs on, on a host that is not driven remotely
POOL = {'cpu': 'part', 'gpu': 'part', 'compile': 'compile'}
#: the pool of a remote (h100) host; each card has its own runner, ``h100-gpu<N>``
REMOTE_POOL = 'h100'
TIMEOUT_CAP = 240


@dataclasses.dataclass(frozen=True)
class Card:
    index: int
    free_mib: int
    temp_c: float = 0.0
    others: bool = False  # in heavy use by other users
    healthy: bool = True  # the per-card part of the h100 health gate


@dataclasses.dataclass(frozen=True)
class Probe:
    """One host's probe results. A field the probe could not establish is left at its
    failing value (the host is then skipped)."""
    reachable: bool = False
    runners: frozenset[str] = frozenset()  # pools with an active runner (remote: h100-gpu<N> labels)
    proxy: bool = False  # the CI proxy answers
    reserved: bool = False  # a reservation is held (the nightly on 3090a)
    quarantined: bool = False  # h100 after two manifest mismatches in 24 h
    slots: int = 0  # CI slots
    busy: int = 0  # slots held plus tickets waiting
    cards: tuple[Card, ...] = ()
    slowdown: float = 1.0  # per-core time against the reference host
    remote: bool = False  # driven over ssh (h100): one card per part, a label per card
    max_temp_c: float = 80.0


@dataclasses.dataclass(frozen=True)
class Part:
    id: str
    cls: str  # cpu, gpu or compile
    gpus: int = 0
    gpu_mem_mib: int = 0
    memory: str = ''
    estimate_minutes: float = 0.0


def parse_parts(obj: object) -> list[Part]:
    """The ``parts`` input: a JSON list of ``{id, class, gpus, gpu-mem-mib, memory,
    estimate-minutes}``. Raises ValueError on a malformed entry or a repeated id."""
    if not isinstance(obj, list):
        raise ValueError('parts: not a list')
    out, seen = [], set()
    for p in obj:
        if not isinstance(p, dict) or not isinstance(p.get('id'), str) or not p['id']:
            raise ValueError('parts: an entry has no id')
        if p.get('class') not in CLASSES:
            raise ValueError(f'parts: {p["id"]}: class must be one of {", ".join(CLASSES)}')
        gpus, mib, est = p.get('gpus', 0), p.get('gpu-mem-mib', 0), p.get('estimate-minutes', 0)
        if type(gpus) is not int or gpus < 0 or type(mib) is not int or mib < 0 \
                or type(est) not in (int, float) or est < 0:
            raise ValueError(f'parts: {p["id"]}: gpus, gpu-mem-mib and estimate-minutes must be numbers >= 0')
        if (p['class'] == 'gpu') != (gpus > 0):
            raise ValueError(f'parts: {p["id"]}: a gpu part leases cards, and only a gpu part')
        if p['id'] in seen:
            raise ValueError(f'parts: {p["id"]} is listed twice')
        seen.add(p['id'])
        out.append(Part(p['id'], p['class'], gpus, mib, str(p.get('memory', '')), float(est)))
    return out


def timeout_minutes(estimate_minutes: float, slowdown: float = 1.0, cap: int = TIMEOUT_CAP) -> int:
    """A part's ``timeout-minutes``: twice its estimate on this host (the estimate times
    the host's CPU slowdown) plus 30, rounded up, at most ``cap``. Pure."""
    return min(cap, math.ceil(2 * max(0.0, estimate_minutes) * max(1.0, slowdown) + 30))


def host_fault(p: Probe | None) -> str:
    """Why a host gets no part, or '' if it passes its probes. Pure."""
    if p is None or not p.reachable:
        return 'unreachable'
    if not p.proxy:
        return 'proxy down'
    if p.reserved:
        return 'reserved'
    if p.quarantined:
        return 'quarantined'
    return ''


def card_fits(c: Card, need_mib: int, max_temp_c: float) -> bool:
    return c.healthy and not c.others and c.free_mib >= need_mib and c.temp_c < max_temp_c


def _remote_card(part: Part, p: Probe, taken: Iterable[int]) -> Card | None:
    """The coolest fitting card of a remote host that has an active runner and is not
    taken; ties: most free memory, then the lower index."""
    ok = [c for c in p.cards if c.index not in set(taken) and f'{REMOTE_POOL}-gpu{c.index}' in p.runners
          and card_fits(c, part.gpu_mem_mib, p.max_temp_c)]
    return min(ok, key=lambda c: (c.temp_c, -c.free_mib, c.index), default=None)


def _fits(part: Part, p: Probe, taken: Iterable[int]) -> bool:
    if p.remote:  # GPU parts only, one card each (decision 3, D7)
        return part.cls == 'gpu' and part.gpus == 1 and _remote_card(part, p, taken) is not None
    if POOL[part.cls] not in p.runners:
        return False
    return sum(card_fits(c, part.gpu_mem_mib, p.max_temp_c) for c in p.cards) >= part.gpus


def place(parts: Sequence[Part], probes: Mapping[str, Probe], prefer: Mapping[str, Sequence[str]],
          exclude: Iterable[str] = ()) -> list[dict]:
    """One entry per part, in order: ``{id, runs-on, fallback, timeout-minutes}``.

    A part's candidates are the hosts of ``prefer[class]``, in that order, that are not
    excluded, pass their probes and fit it. It goes to the first candidate with room
    (slots, or on a remote host a card no earlier part took), else to the first
    candidate, where it waits; its fallback is the next candidate on another host. A
    part with no candidate gets ``runs-on: None`` and a ``reason``. Pure.
    """
    exclude = set(exclude)
    load = {h: p.busy for h, p in probes.items()}
    taken: dict[str, set[int]] = {h: set() for h in probes}
    out = []
    for part in parts:
        order = [h for h in prefer.get(part.cls, ()) if h not in exclude]
        cands = [h for h in order if not host_fault(probes.get(h)) and _fits(part, probes[h], taken[h])]
        if not cands:
            faults = {h: host_fault(probes.get(h)) or 'does not fit' for h in order}
            out.append({'id': part.id, 'runs-on': None, 'fallback': None, 'timeout-minutes': None,
                        'reason': '; '.join(f'{h}: {f}' for h, f in faults.items()) or 'no host for this class'})
            continue
        room = [h for h in cands if probes[h].remote or load[h] < probes[h].slots]
        host = room[0] if room else cands[0]
        rest = cands[cands.index(host) + 1:] + [h for h in cands[:cands.index(host)]]
        fb = rest[0] if rest else None
        entry = {'id': part.id, 'runs-on': _labels(part, host, probes[host], taken[host], take=True),
                 'fallback': _labels(part, fb, probes[fb], taken[fb], take=False) if fb else None,
                 'timeout-minutes': timeout_minutes(part.estimate_minutes, probes[host].slowdown)}
        load[host] += 1
        out.append(entry)
    return out


def _labels(part: Part, host: str, p: Probe, taken: set[int], take: bool) -> list[str]:
    if p.remote:
        card = _remote_card(part, p, taken)
        if take:
            taken.add(card.index)
        return ['self-hosted', f'{REMOTE_POOL}-gpu{card.index}']
    return ['self-hosted', POOL[part.cls], host]
