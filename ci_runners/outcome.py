"""How a part ended, and what the consumer does next (design §4.4, D6, decision 8).

``classify`` is a pure function of the evidence the executor gathers after a part: the
exit status, the scope's OOM kills, the boot ids and the early thermal stop. ``hint``
maps an outcome to ``next``. Infrastructure ends are never reported as test failures.
"""
from __future__ import annotations

import dataclasses

#: every outcome (design §4.4); CLASSIFIED are the ones ``classify`` returns, in
#: precedence order, highest first; ``lost`` comes from a record that never finished
KINDS = ('pass', 'red', 'oom', 'thermal', 'reboot', 'unavailable', 'lost', 'stopped')
PRECEDENCE = ('stopped', 'reboot', 'thermal', 'oom', 'unavailable', 'lost', 'red', 'pass')
#: outcomes after which nothing is retried
FINAL = frozenset({'pass', 'red', 'stopped'})
NEXT = ('none', 'retry', 'fallback')


# ------------------------------------------------------------------ the evidence

def bus_key(bus: str) -> str:
    """A card's PCI bus number as a short lower-case hex string, from ``nvidia-smi``'s
    ``00000000:A8:00.0``, or a bare ``a8``. Pure."""
    s = bus.strip().lower()
    if ':' in s:  # domain:bus:device.function, or bus:device.function
        parts = s.split(':')
        s = parts[-2] if len(parts) >= 2 else s
    return s.lstrip('0') or '0'


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
    early_stop: bool = False  # our own thermal stop (§5.5 item 5)


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


def hint(kind: str, *, attempt: int = 1, retries: int = 1, cool_card: bool = False,
         host_fault: bool = False) -> Hint:
    """``next`` for an outcome (design §4.4). ``attempt`` is 1 for the first run of a
    part; a part is retried at most ``retries`` times (the registry's 0 or 1), so an
    infrastructure end of the last allowed attempt is final, the consumer's verdict.
    ``cool_card`` is whether the host has a cool card for a thermal retry. ``host_fault`` marks an end
    that a retry does not cure, a failed P2P self-test (decision 12.6): ``none``. Pure."""
    if kind not in KINDS:
        raise ValueError(f'unknown outcome {kind!r}')
    if kind in FINAL or attempt > retries or host_fault:
        return Hint('none')
    if kind == 'thermal':
        return Hint('retry', where='cool-card') if cool_card else Hint('fallback')
    if kind == 'oom':
        return Hint('retry', resume=True, where='more-memory')
    if kind == 'reboot':
        return Hint('retry', where='same-host')
    if kind == 'lost':
        return Hint('retry', where='any')
    return Hint('fallback')  # unavailable
