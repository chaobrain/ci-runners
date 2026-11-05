"""GPU facts from ``nvidia-smi`` and the lease choice (design §7, "GPU leases"; S2).

Pure functions; the lease files and the ``nvidia-smi`` calls are the host layer's.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence


def gpu_free_mib(csv: str) -> dict[int, int]:
    """Free MiB per card, ``memory.total - memory.used``, from ``nvidia-smi
    --query-gpu=index,memory.total,memory.used --format=csv,noheader,nounits``. A line
    that does not parse is skipped, so a card missing from the output is missing here.
    Pure."""
    out = {}
    for line in csv.splitlines():
        parts = [p.strip() for p in line.split(',')]
        if len(parts) == 3 and all(p.isdigit() for p in parts):
            out[int(parts[0])] = int(parts[1]) - int(parts[2])
    return out


def gpu_buses(csv: str) -> dict[int, str]:
    """Bus id per card, from ``nvidia-smi --query-gpu=index,pci.bus_id
    --format=csv,noheader``. Pure."""
    out = {}
    for line in csv.splitlines():
        parts = [p.strip() for p in line.split(',')]
        if len(parts) == 2 and parts[0].isdigit() and parts[1]:
            out[int(parts[0])] = parts[1]
    return out


def usable_gpus(want: Sequence[int], free: Mapping[int, int], need_mib: int) -> list[int]:
    """The cards of ``want`` with at least ``need_mib`` MiB free; a card absent from
    ``free`` (``nvidia-smi`` failed or did not list it) is not usable. Pure."""
    return [i for i in want if i in free and free[i] >= need_mib]


def rank_gpus(free: Mapping[int, int], need_mib: int) -> list[int]:
    """The usable cards, most free memory first; ties go to the lower index. Pure."""
    return sorted(usable_gpus(sorted(free), free, need_mib), key=lambda i: -free[i])  # stable: ties keep index order


def choose_lease(n: int, *, cards: Sequence[int], free: Mapping[int, int], need_mib: int,
                 leased: Mapping[int, int], max_parts: int = 1) -> list[int] | None:
    """``n`` cards for a part: among the host's ``cards``, those with fewer than
    ``max_parts`` leases and ``need_mib`` free, most free first. None if fewer than
    ``n`` qualify (the part waits, or ends ``unavailable`` at its queue timeout). Pure."""
    if n < 1:
        return []
    ok = [i for i in rank_gpus({i: free[i] for i in cards if i in free}, need_mib) if leased.get(i, 0) < max_parts]
    return ok[:n] if len(ok) >= n else None


def _num(s: str) -> float | None:
    try:
        v = float(s)
    except ValueError:
        return None
    return v if v == v and v not in (float('inf'), float('-inf')) else None


def gpu_status(csv: str) -> dict[int, tuple[str, float, float]]:
    """``index -> (bus id, temperature C, utilisation %)`` from ``nvidia-smi
    --query-gpu=index,pci.bus_id,temperature.gpu,utilization.gpu --format=csv,noheader,nounits``.
    A card whose temperature does not parse is missing (it then counts against itself);
    an unparsable utilisation reads -1. Pure."""
    out = {}
    for line in csv.splitlines():
        parts = [p.strip() for p in line.split(',')]
        if len(parts) != 4 or not parts[0].isdigit() or not parts[1]:
            continue
        temp, util = _num(parts[2]), _num(parts[3].rstrip(' %'))
        if temp is not None:
            out[int(parts[0])] = (parts[1], temp, -1.0 if util is None else util)
    return out


def compute_apps(csv: str) -> list[tuple[int, str]]:
    """``(pid, bus id)`` per compute process, from ``nvidia-smi
    --query-compute-apps=pid,gpu_bus_id --format=csv,noheader``. Pure."""
    out = []
    for line in csv.splitlines():
        parts = [p.strip() for p in line.split(',')]
        if len(parts) == 2 and parts[0].isdigit() and parts[1]:
            out.append((int(parts[0]), parts[1]))
    return out


def cool_cards(cards: Sequence[int], status: Mapping[int, tuple[str, float, float]], max_temp_c: float) -> list[int]:
    """The cards of ``cards`` with a reading below ``max_temp_c``, coolest first; ties go
    to the lower index. A card without a reading is not cool (design §5.4 health gate).
    Pure."""
    ok = [c for c in cards if c in status and status[c][1] < max_temp_c]
    return sorted(ok, key=lambda c: (status[c][1], c))
