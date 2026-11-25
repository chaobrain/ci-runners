"""Read the physical cards offered to the scheduler on this executor."""
from __future__ import annotations

import subprocess
import ctypes
import os
import re
import uuid

from parallel import protocol


def visible_cards():
    """Enumerate the CUDA job mask, without creating a context or choosing cards."""
    try:
        driver = ctypes.CDLL('libcuda.so.1')
        signatures = {'cuInit': [ctypes.c_uint], 'cuDeviceGetCount': [ctypes.POINTER(ctypes.c_int)],
                      'cuDeviceGet': [ctypes.POINTER(ctypes.c_int), ctypes.c_int],
                      'cuDeviceGetName': [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]}
        get_uuid = getattr(driver, 'cuDeviceGetUuid_v2', None) or driver.cuDeviceGetUuid
        get_uuid.argtypes, get_uuid.restype = [ctypes.c_void_p, ctypes.c_int], ctypes.c_int
        for name, args in signatures.items():
            function = getattr(driver, name)
            function.argtypes, function.restype = args, ctypes.c_int
        if driver.cuInit(0):
            raise protocol.Error('cannot initialize assigned CUDA devices')
        count = ctypes.c_int()
        if driver.cuDeviceGetCount(ctypes.byref(count)) or count.value not in (1, 2):
            raise protocol.Error('unexpected assigned CUDA device count')
        cards = []
        for ordinal in range(count.value):
            device = ctypes.c_int()
            raw, name = ctypes.create_string_buffer(16), ctypes.create_string_buffer(256)
            if (driver.cuDeviceGet(ctypes.byref(device), ordinal) or get_uuid(raw, device)
                    or driver.cuDeviceGetName(name, len(name), device)):
                raise protocol.Error('cannot identify an assigned CUDA device')
            cards.append(('GPU-' + str(uuid.UUID(bytes=raw.raw)), name.value.decode('ascii')))
        return cards
    except (OSError, AttributeError, UnicodeError) as error:
        raise protocol.Error('assigned CUDA identity is unavailable') from error


def slurm_cards(grant, environ=None, *, query=visible_cards, details=False):
    """Validate Slurm's clean pre-command environment against the signed profile."""
    environ = os.environ if environ is None else environ
    job = environ.get('SLURM_JOB_ID', '')
    mask = environ.get('CUDA_VISIBLE_DEVICES', '').split(',')
    if (not job.isdecimal() or int(job) <= 0 or not mask
            or any(not (item.isdecimal() or protocol.GPU_UUID.fullmatch(item)) for item in mask)
            or len(mask) != grant['gpu_count'] or len(set(mask)) != len(mask)):
        raise protocol.Error('GPU Slurm lacks its exact scheduler-assigned device mask')
    observations = query()
    components = re.findall(r'[A-Za-z]+|[0-9]+', grant['gpu_model'])
    model_pattern = r'(?<![A-Za-z0-9])' + r'[ _-]*'.join(map(re.escape, components))
    model_pattern += r'(?![A-Za-z])' if components and components[-1].isalpha() else r'(?![A-Za-z0-9])'
    if (not isinstance(observations, list) or len(observations) != grant['gpu_count']
            or any(not isinstance(item, tuple) or len(item) != 2
                   or not isinstance(item[0], str) or not protocol.GPU_UUID.fullmatch(item[0])
                   or not isinstance(item[1], str) or not re.search(model_pattern, item[1], re.I)
                   for item in observations)):
        raise protocol.Error('assigned CUDA count or model differs from the authorized target')
    cards = [item[0] for item in observations]
    if len(set(cards)) != len(cards) or all(protocol.GPU_UUID.fullmatch(item) for item in mask) and cards != mask:
        raise protocol.Error('assigned CUDA identities differ from the scheduler mask')
    return observations if details else cards


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
