"""The ``claim`` decision: does a head still need the runners (design §4.1, lead decision 6)?

A head whose status context already holds a verdict, or a live claim by a local run, is
skipped. Only statuses posted by the workflow bot or by an allowlisted actor count (T11):
anyone with write access can post a status on GitHub Free.
"""
from __future__ import annotations

import dataclasses
import datetime
from collections.abc import Iterable, Sequence


@dataclasses.dataclass(frozen=True)
class Status:
    state: str  # success, failure, error or pending
    creator: str  # the login that posted it
    time: float  # epoch seconds
    context: str = ''
    description: str = ''


@dataclasses.dataclass(frozen=True)
class Claim:
    run: bool
    reason: str


def _epoch(s: object) -> float | None:
    if not isinstance(s, str):
        return None
    try:
        dt = datetime.datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt.timestamp() if dt.tzinfo is not None else None  # a naive time would be read as local


def parse_statuses(api: object, context: str) -> list[Status]:
    """The statuses of ``context`` in a ``GET /repos/{o}/{r}/commits/{sha}/statuses``
    answer, in the API's order (newest first). Entries that do not parse are dropped.
    Pure."""
    out = []
    for s in api if isinstance(api, list) else []:
        if not isinstance(s, dict) or s.get('context') != context:
            continue
        creator = s.get('creator')
        login = creator.get('login') if isinstance(creator, dict) else None
        t = _epoch(s.get('created_at'))
        if isinstance(s.get('state'), str) and isinstance(login, str) and t is not None:
            out.append(Status(s['state'], login, t, context, str(s.get('description') or '')))
    return out


def decide(statuses: Sequence[Status], *, actors: Iterable[str], bot: str, now: float,
           lapse_minutes: float = 45, hosted: bool = False) -> Claim:
    """The rule of design §4.1, on the newest status that counts.

    A status counts if the bot posted it, or, on a self-hosted runner, an allowlisted
    actor (``actors``; a hosted runner has no allowlist, so ``hosted`` ignores them).
    Then:

    1. success: skip, the head is green;
    2. failure by an allowlisted actor (a local run): skip, the fix is a new push;
    3. pending by an allowlisted actor less than ``lapse_minutes`` old: skip, a live
       local claim;
    4. anything else: run.

    Among statuses with the same time, the first in ``statuses`` (the API's newest
    first) wins. Pure.
    """
    bot = bot.lower()
    people = frozenset() if hosted else frozenset(a.lower() for a in actors) - {bot}
    counted = [s for s in statuses if s.creator.lower() == bot or s.creator.lower() in people]
    if not counted:
        return Claim(True, 'no verdict on this head')
    newest = max(counted, key=lambda s: s.time)  # max keeps the first of equal times
    local = newest.creator.lower() in people
    if newest.state == 'success':
        return Claim(False, 'green')
    if newest.state == 'failure' and local:
        return Claim(False, 'red in a local run')
    if newest.state == 'pending' and local:
        if now - newest.time < lapse_minutes * 60:
            return Claim(False, 'claimed by a local run')
        return Claim(True, 'a local claim lapsed')
    return Claim(True, f'{newest.state} by the runners' if not local else f'{newest.state} in a local run')
