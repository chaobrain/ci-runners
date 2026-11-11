"""The ``claim`` decision: does a head still need the runners (design §4.1, lead decision 6)?

A head whose status context already holds the runners' green verdict is skipped. Only
statuses posted by the runners' own identity, the workflow bot (the job token's login), count
(T11, decision 15.9): anyone with write access can post a status on GitHub Free, and a
person's pending or success, such as an old local run's, neither holds nor skips a head.
"""
from __future__ import annotations

import dataclasses
import datetime
from collections.abc import Sequence


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


def decide(statuses: Sequence[Status], *, bot: str) -> Claim:
    """The rule of design §4.1, on the newest status the runners posted.

    A status counts only if the bot posted it. Then:

    1. success: skip, the head is green;
    2. anything else (pending, failure, error, or none): run.

    Among statuses with the same time, the first in ``statuses`` (the API's newest
    first) wins. Pure.
    """
    bot = bot.lower()
    counted = [s for s in statuses if s.creator.lower() == bot]
    if not counted:
        return Claim(True, 'no verdict on this head')
    newest = max(counted, key=lambda s: s.time)  # max keeps the first of equal times
    if newest.state == 'success':
        return Claim(False, 'green')
    return Claim(True, f'{newest.state} by the runners')
