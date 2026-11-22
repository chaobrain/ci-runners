"""Read the physical cards offered to the scheduler on this executor."""
from __future__ import annotations

import subprocess

from parallel import protocol


def available(cards, max_temp_c, *, run=subprocess.run):
    """Return free MiB for idle, cool, configured UUIDs. Unreadable cards stay out."""
    if not cards:
        return {}
    try:
        status = run(['nvidia-smi', '--query-gpu=uuid,temperature.gpu,memory.free',
                      '--format=csv,noheader,nounits'], capture_output=True, text=True, timeout=5, check=True)
        processes = run(['nvidia-smi', '--query-compute-apps=gpu_uuid', '--format=csv,noheader'],
                        capture_output=True, text=True, timeout=5, check=True)
    except (OSError, subprocess.SubprocessError):
        return {}
    busy = {line.strip() for line in processes.stdout.splitlines() if line.strip()}
    if any(not protocol.GPU_UUID.fullmatch(uuid) for uuid in busy):
        return {}
    ready = {}
    for line in status.stdout.splitlines():
        fields = [field.strip() for field in line.split(',')]
        if len(fields) != 3:
            continue
        uuid, temperature, free = fields
        if (uuid in cards and uuid not in busy and temperature.isdecimal() and free.isdecimal()
                and int(temperature) < max_temp_c):
            ready[uuid] = int(free)
    return ready
