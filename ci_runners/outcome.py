"""How a part ended, and what the consumer does next (design §4.4, D6, decision 8).

``classify`` is a pure function of the evidence the executor gathers after a part: the
exit status, the scope's OOM kills, the boot ids, the GPU watchdog's log. ``hint`` maps an outcome to ``next``. Infrastructure ends are
never reported as test failures.
"""
from __future__ import annotations

import dataclasses
import re
from collections.abc import Iterable

#: every outcome (design §4.4); CLASSIFIED are the ones ``classify`` returns, in
#: precedence order, highest first; ``lost`` comes from a record that never finished
KINDS = ('pass', 'red', 'oom', 'thermal', 'reboot', 'unavailable', 'lost', 'stopped')
PRECEDENCE = ('stopped', 'reboot', 'thermal', 'oom', 'unavailable', 'lost', 'red', 'pass')
#: outcomes after which nothing is retried
FINAL = frozenset({'pass', 'red', 'stopped'})
NEXT = ('none', 'retry', 'fallback')


# ------------------------------------------------------------------ the evidence

#: the watchdog's kill line: ``2026-11-05 10:00:00 CRIT GPU a8 still 91C -> killed
#: user1:12345, user2:222``; its 88 C warning ("its jobs get killed if still hot") is not one
_KILL = re.compile(r'^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) \w+ GPU ([0-9A-Fa-f:.]+) .*-> killed (.*)$')
_VICTIM = re.compile(r'([\w.-]+):(\d+)')


@dataclasses.dataclass(frozen=True)
class Kill:
    when: str  # host-local ``YYYY-MM-DD HH:MM:SS``, sortable
    bus: str  # bus_key of the card the watchdog names
    victims: tuple[tuple[str, int], ...]  # (user, pid)


def bus_key(bus: str) -> str:
    """A card's PCI bus number as a short lower-case hex string, from ``nvidia-smi``'s
    ``00000000:A8:00.0`` or the watchdog's ``a8``. Pure."""
    s = bus.strip().lower()
    if ':' in s:  # domain:bus:device.function, or bus:device.function
        parts = s.split(':')
        s = parts[-2] if len(parts) >= 2 else s
    return s.lstrip('0') or '0'


def watchdog_kills(log: str, start: str = '', end: str = '') -> list[Kill]:
    """The watchdog's kills between ``start`` and ``end`` inclusive (host-local times;
    empty: open). A line that names no process gives a kill with no victims. Pure."""
    out = []
    for line in log.splitlines():
        m = _KILL.match(line.strip())
        if m and (not start or m.group(1) >= start) and (not end or m.group(1) <= end):
            victims = tuple((u, int(p)) for u, p in _VICTIM.findall(m.group(3)))
            out.append(Kill(m.group(1), bus_key(m.group(2)), victims))
    return out


def killed_ours(kills: Iterable[Kill], pids: Iterable[int], buses: Iterable[str]) -> list[Kill]:
    """The kills that name one of our cards and one of the PIDs the sampler recorded for
    our scope. The watchdog names the shared account, so the user does not count. Pure."""
    ours, cards = set(pids), {bus_key(b) for b in buses}
    return [k for k in kills if k.bus in cards and any(p in ours for _, p in k.victims)]


def memory_oom_kills(events: str) -> int:
    """``oom_kill`` of a cgroup's ``memory.events`` text (0 if absent). Pure."""
    for line in events.splitlines():
        k, _, v = line.partition(' ')
        if k == 'oom_kill' and v.strip().isdigit():
            return int(v)
    return 0


# ------------------------------------------------------------------ classification


@dataclasses.dataclass(frozen=True)
class Evidence:
    rc: int | None = None  # the command's exit status; None: the record never got one
    timed_out: bool = False  # the inner timeout ended it
    stopped: bool = False  # cancelled: the runner's TERM, a concurrency group
    unavailable: str = ''  # why it did not start (empty: it started)
    boot_start: str = ''  # boot id at the start
    boot_end: str = ''  # boot id after
    oom_kills: int = 0  # memory.events oom_kill of the part's scope
    watchdog: str = ''  # the GPU watchdog's log (h100)
    start: str = ''  # host-local start, as the watchdog writes times
    end: str = ''
    pids: tuple[int, ...] = ()  # the PIDs the sampler recorded for the scope
    buses: tuple[str, ...] = ()  # the bus ids of our cards
    early_stop: bool = False  # our own thermal stop (§5.4 phase 5)


@dataclasses.dataclass(frozen=True)
class Outcome:
    kind: str
    reason: str


def classify(e: Evidence) -> Outcome:
    """The outcome of a part, by the precedence of design §4.4: stopped, reboot, thermal,
    oom, unavailable, lost (no exit status), red, pass. Pure."""
    if e.stopped:
        return Outcome('stopped', 'cancelled')
    if e.boot_start and e.boot_end and e.boot_start != e.boot_end:
        return Outcome('reboot', 'the host rebooted during the part')
    if e.early_stop:
        return Outcome('thermal', 'our card stayed hot: stopped early')
    hit = killed_ours(watchdog_kills(e.watchdog, e.start, e.end), e.pids, e.buses)
    if hit:
        return Outcome('thermal', f'the GPU watchdog killed our process on bus {hit[0].bus} at {hit[0].when}')
    if e.oom_kills > 0:
        return Outcome('oom', 'the part\'s memory scope was OOM-killed')
    if e.unavailable:
        return Outcome('unavailable', e.unavailable)
    if e.rc is None:
        return Outcome('lost', 'the part ended without an exit status')
    if e.timed_out:
        return Outcome('red', 'the inner timeout ended the command')
    if e.rc != 0:
        return Outcome('red', f'exit {e.rc}')
    return Outcome('pass', 'exit 0')


# ------------------------------------------------------------------ what next


@dataclasses.dataclass(frozen=True)
class Hint:
    next: str  # none, retry or fallback
    resume: bool = False  # the retry sets CI_RESUME=1
    where: str = ''  # cool-card (same host), same-host, more-memory, any; empty for none and fallback


def hint(kind: str, *, attempt: int = 1, retries: int = 1, remote: bool = False, cool_card: bool = False,
         host_fault: bool = False) -> Hint:
    """``next`` for an outcome (design §4.4). ``attempt`` is 1 for the first run of a
    part; a part is retried at most ``retries`` times (the registry's 0 or 1), so an
    infrastructure end of the last allowed attempt is final, the consumer's verdict.
    ``remote`` marks an h100 part, where a reboot counts as thermal; ``cool_card``
    whether the host has a cool card for a thermal retry. ``host_fault`` marks an end
    that a retry does not cure, a failed P2P self-test (decision 12.6): ``none``. Pure."""
    if kind not in KINDS:
        raise ValueError(f'unknown outcome {kind!r}')
    if kind in FINAL or attempt > retries or host_fault:
        return Hint('none')
    if kind == 'reboot' and remote:
        kind = 'thermal'  # the board powers off at 101 C
    if kind == 'thermal':
        return Hint('retry', where='cool-card') if cool_card else Hint('fallback')
    if kind == 'oom':
        return Hint('retry', resume=True, where='more-memory')
    if kind == 'reboot':
        return Hint('retry', where='same-host')
    if kind == 'lost':
        return Hint('retry', where='any')
    return Hint('fallback')  # unavailable
