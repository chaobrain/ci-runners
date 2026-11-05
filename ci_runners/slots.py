"""The fair order of a host's CI slot queue (design §7, rules 1, 2 and 4).

Pure; the queue directory, the tickets and the slot locks are the host layer's
(``ci-slot``, interoperating with brainspike's ``ci_slot.sh``, RUN-1b).
"""
from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence


@dataclasses.dataclass(frozen=True)
class Ticket:
    seq: int  # the queue's number: lower is older
    project: str = ''  # empty: a ticket of brainspike's ci_slot.sh, which names none (rule 4)


def grants(waiting: Sequence[Ticket], held: Mapping[str, int], caps: Mapping[str, int], free: int,
           default_project: str) -> list[int]:
    """The ``seq`` of the tickets that take the ``free`` slots now, in order.

    A ticket without a project counts as ``default_project``. A ticket whose project
    already holds its cap (``caps``; a project not listed has cap 0) is skipped. Each
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
