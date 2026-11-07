"""The GPU sampler of a running part, and its early thermal stop (design §5.5 item 5,
§12 item 7).

Every ``SAMPLE_S`` the sampler records, per card of the part, the temperature, the
utilisation and our compute PIDs (``nvidia-smi``'s compute processes on that card that
are also in the part's scope) into the record's ``samples.csv``. Where the early stop is
on, a card at ``HOT_C`` or more for ``HOT_S`` stops the part: its outcome is ``thermal``.

``HotWatch`` is pure. ``Sampler`` takes its readers, its stop and its clock as callables.
"""
from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence

from ci_runners import outcome

SAMPLE_S = 30  # s between samples (design §8)
HOT_C = 85  # C: the early stop's threshold (§12 item 7)
HOT_S = 180  # s a card stays at HOT_C or more before the stop
CSV_HEADER = 'epoch,card,bus,temp_c,util_pct,our_pids\n'

#: ``(status, apps)``: gpus.gpu_status and gpus.compute_apps of one nvidia-smi round
Query = Callable[[], tuple[Mapping[int, tuple[str, float, float]], Sequence[tuple[int, str]]]]


class HotWatch:
    """When each card's current hot streak (``hot_c`` or more) began. A cooler reading
    ends the streak; a card missing from a sample keeps its state. Pure."""

    def __init__(self, hot_c: float = HOT_C, hold_s: float = HOT_S) -> None:
        self.hot_c, self.hold_s = hot_c, hold_s
        self.since: dict[int, float] = {}

    def update(self, t: float, temps: Mapping[int, float]) -> int | None:
        """Take one sample at ``t``; the lowest card hot for ``hold_s`` or longer, or None."""
        for card, temp in temps.items():
            if temp >= self.hot_c:
                self.since.setdefault(card, t)
            else:
                self.since.pop(card, None)
        hot = [c for c, s in self.since.items() if t - s >= self.hold_s]
        return min(hot) if hot else None


class Sampler:
    """Samples one part's cards until ``done`` is set or it stops the part."""

    def __init__(self, cards: Iterable[int], *, csv_path: str, query: Query, procs: Callable[[], set[int]],
                 stop: Callable[[], None], early_stop: bool, clock: Callable[[], float] = time.time) -> None:
        self.cards = sorted(set(cards))
        self.csv_path, self.query, self.procs, self.stop_part = csv_path, query, procs, stop
        self.early_stop, self.clock = early_stop, clock
        self.watch = HotWatch()
        self.pids: set[int] = set()  # every PID of ours seen on our cards
        self.buses: dict[int, str] = {}  # card -> bus id
        self.peak: dict[int, float] = {}  # card -> highest temperature seen
        self.stopped_card: int | None = None

    def sample(self) -> bool:
        """One round. True once it has stopped the part (it stops it once)."""
        if self.stopped_card is not None:
            return True
        try:
            status, apps = self.query()
            procs = self.procs()
        except Exception:  # noqa: BLE001 - a failed reading is a missing sample, never the part's end
            return False
        t = self.clock()
        mine = {c: status[c] for c in self.cards if c in status}
        for c, (bus, temp, _util) in mine.items():
            self.buses[c] = bus
            self.peak[c] = max(temp, self.peak.get(c, temp))
        rows = []
        for c, (bus, temp, util) in mine.items():
            ours = sorted({pid for pid, b in apps if outcome.bus_key(b) == outcome.bus_key(bus) and pid in procs})
            self.pids.update(ours)
            rows.append(f'{t:.0f},{c},{bus},{temp:g},{util:g},{";".join(map(str, ours))}\n')
        self._append(rows)
        if self.early_stop:
            card = self.watch.update(t, {c: v[1] for c, v in mine.items()})
            if card is not None:
                self.stopped_card = card
                self.stop_part()
                return True
        return False

    def _append(self, rows: list[str]) -> None:
        try:
            fd = os.open(self.csv_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            with os.fdopen(fd, 'a', encoding='utf-8') as f:
                if f.tell() == 0:
                    f.write(CSV_HEADER)
                f.writelines(rows)
        except OSError:
            pass  # the samples are evidence, not the part

    def loop(self, done: threading.Event, interval: float = SAMPLE_S) -> None:
        """Sample now, then every ``interval`` until ``done`` or a stop."""
        while not self.sample() and not done.wait(interval):
            pass
