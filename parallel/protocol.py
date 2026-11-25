"""Schema 2 task plans and allocation contracts. No I/O or machine inventory."""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping

GROUP_CPUS = GROUP_GIB = 4
DEVICE_GROUPS = {'3090a': 4, '3090b': 4, 'pro6000': 20, 'h100': 10, 'slurm': 20}
DEVICES = frozenset(DEVICE_GROUPS)
GPU_QUOTAS = {'pro6000': 3, '3090a': 2, '3090b': 2, 'h100': 3}
GPU_WAIT_LANES = (*GPU_QUOTAS, 'auto')
GPU_UUID = re.compile(r'GPU-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z')
MAX_MINUTES = 1440
ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z')
REPO = re.compile(r'[A-Za-z0-9-]{1,39}/[A-Za-z0-9_.-]{1,100}\Z')
SHA = re.compile(r'[0-9a-f]{40}\Z')
TERMINAL = frozenset({'success', 'failure', 'error', 'cancelled'})


class Error(ValueError):
    """A malformed request or a state transition not justified by evidence."""


def positive(value: object) -> bool:
    return type(value) is int and value > 0


def gpu_count(part: Mapping) -> int:
    """The original plan alone defines card demand; never infer it from workers."""
    count = part.get('gpu_count', 1 if part.get('gpu', False) else 0)
    if type(count) is not int or count not in (0, 1, 2) or bool(count) != part.get('gpu', False):
        raise Error('gpu_count must be 0, 1 or 2 and agree with gpu')
    return count


def grant_cards(grant: Mapping) -> list[str]:
    """Read an already validated grant, retaining the single-card wire format."""
    if grant.get('allocation_schema', 1) == 3:
        return list(grant['gpu_uuids'])
    return [grant['gpu_uuid']] if grant.get('gpu_uuid') else []


def number(value: object, maximum: float) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and 0 < value <= maximum


def dumps(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def fingerprint(value: object) -> str:
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def validate_gpu_list(device: str, cards: object) -> None:
    if (not isinstance(cards, list) or len(cards) > GPU_QUOTAS.get(device, 0)
            or any(not isinstance(card, str) or not GPU_UUID.fullmatch(card) for card in cards)
            or len(set(cards)) != len(cards)):
        raise Error(f'{device}: gpu_uuids must list distinct full GPU UUIDs within the device quota')


def gpu_wait_label(device: str) -> str:
    lane = device or 'auto'
    if lane not in GPU_WAIT_LANES:
        raise Error('invalid GPU allocation lane')
    return f'ci-gpu-wait-{lane}'


def runner_names(device: str, count: int | None = None, start: int = 0) -> list[str]:
    if device not in DEVICES | {'plan'}:
        raise Error('unknown device')
    count = (2 if device == 'plan' else DEVICE_GROUPS[device]) if count is None else count
    limit = 64 if device == 'plan' else DEVICE_GROUPS[device]
    if not positive(count) or type(start) is not int or start < 0 or start + count > limit:
        raise Error('runner range exceeds the device quota')
    return [f'{device}{"" if device == "slurm" else "-"}{i}' for i in range(start, start + count)]


def load_device_registration(value: object) -> dict:
    fields = {'device', 'cpu_count', 'memory_max', 'gpu_uuids', 'runner_count', 'priority'}
    if not isinstance(value, dict) or set(value) != fields:
        raise Error('invalid device registration fields')
    device = value['device']
    if not isinstance(device, str) or device not in DEVICES - {'slurm'}:
        raise Error('unsupported execution device')
    groups = value['runner_count']
    if not positive(groups):
        raise Error('invalid device runner count')
    runner_names(device, groups)
    if not positive(value['cpu_count']) or value['cpu_count'] != groups * GROUP_CPUS:
        raise Error('device runner count must match its four-core CPU groups')
    memory = value['memory_max']
    if not isinstance(memory, str) or not memory:
        raise Error('invalid device memory limit')
    if type(value['priority']) is not int or not -(1 << 31) <= value['priority'] < (1 << 31):
        raise Error('invalid device priority')
    validate_gpu_list(device, value['gpu_uuids'])
    return dict(value)


def load_plan(doc: object, *, max_minutes: float = MAX_MINUTES) -> dict:
    if not isinstance(doc, dict) or set(doc) - {'schema', 'parts', 'files', 'max_parallel'}:
        raise Error('plan is an object containing schema, parts, optional files and max_parallel')
    if type(doc.get('schema')) is not int or doc['schema'] != 2:
        raise Error('central scheduling requires plan schema 2')
    parts = doc.get('parts')
    if not isinstance(parts, list) or len(parts) > 256:
        raise Error('parts is a list of at most 256 tasks')
    maximum = doc.get('max_parallel', 256)
    if not positive(maximum) or maximum > 256:
        raise Error('max_parallel is an integer in [1, 256]')
    files = doc.get('files', '')
    if not isinstance(files, str) or files and not relative(files):
        raise Error('files must be a relative directory below the checkout')
    out, seen = [], set()
    keys = {'id', 'needs', 'device', 'gpu', 'gpu_count', 'cores', 'workers', 'minutes', 'queue_minutes', 'memory_gib',
            'command', 'results', 'artifacts', 'history'}
    for p in parts:
        if not isinstance(p, dict) or set(p) - keys:
            raise Error('part has unknown fields (RAM and cpu_groups are scheduler outputs)')
        name = p.get('id')
        if not isinstance(name, str) or not ID.fullmatch(name) or name in seen:
            raise Error('part id must be valid and unique')
        seen.add(name)
        if p.get('needs') != 'cpu':
            raise Error(f'{name}: schema 2 currently accepts needs cpu only')
        device = p.get('device', '')
        if not isinstance(device, str) or device not in DEVICES | {''}:
            raise Error(f'{name}: device must be empty or one of {", ".join(sorted(DEVICES))}')
        if type(p.get('gpu', False)) is not bool:
            raise Error(f'{name}: gpu must be boolean')
        gpu_count(p)
        if p.get('gpu') and device == 'slurm':
            raise Error(f'{name}: Slurm does not provide central GPU cards')
        cores = p.get('cores', GROUP_CPUS)
        if not positive(cores) or cores % GROUP_CPUS:
            raise Error(f'{name}: cores must be a positive multiple of {GROUP_CPUS}')
        limit = DEVICE_GROUPS.get(device, max(DEVICE_GROUPS.values()))
        if cores > limit * GROUP_CPUS:
            raise Error(f'{name}: cores exceeds the requested device capacity')
        if not positive(p.get('workers')):
            raise Error(f'{name}: workers must be an explicit positive integer')
        if 'memory_gib' in p and (not positive(p['memory_gib']) or p['memory_gib'] > (1 << 20)):
            raise Error(f'{name}: memory_gib must be a positive integer no greater than 1048576')
        if not number(p.get('minutes'), min(max_minutes, MAX_MINUTES)):
            raise Error(f'{name}: minutes exceeds the project or platform limit')
        queue = p.get('queue_minutes', 15)
        if not number(queue, MAX_MINUTES):
            raise Error(f'{name}: queue_minutes must be in (0, {MAX_MINUTES}]')
        if math.ceil(p['minutes'] + queue + 30) > 1440:
            raise Error(f'{name}: runtime, queue and cleanup must fit within the execution token lifetime')
        if not isinstance(p.get('command'), str) or not p['command'].strip() or '\0' in p['command']:
            raise Error(f'{name}: command must be a nonempty string')
        for key in ('results',):
            if key in p and (not isinstance(p[key], str) or '\0' in p[key]):
                raise Error(f'{name}: {key} must be a string')
        artifacts = p.get('artifacts', {})
        if (not isinstance(artifacts, dict) or len(artifacts) > 16 or any(
                not isinstance(k, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.]{0,63}', k)
                or k == 'ci' or not relative(v) for k, v in artifacts.items())):
            raise Error(f'{name}: artifacts must map names to relative paths below RUNNER_TEMP')
        if len(p['command'].encode()) > 64 << 10:
            raise Error(f'{name}: command exceeds 64 KiB')
        if type(p.get('history', False)) is not bool:
            raise Error(f'{name}: history must be boolean')
        out.append({**p, 'device': device, 'cores': cores, 'queue_minutes': queue})
    return {'schema': 2, 'parts': out, 'files': files, 'max_parallel': maximum}


def part_memory_gib(part: Mapping) -> int:
    """RAM requested independently, or the unchanged one-GiB-per-core default."""
    return part.get('memory_gib', part['cores'])


def source_key(source: Mapping) -> str:
    repo, sha = source.get('repository'), source.get('sha')
    if not isinstance(repo, str) or not REPO.fullmatch(repo):
        raise Error('source repository must be owner/name')
    if not isinstance(sha, str) or not SHA.fullmatch(sha):
        raise Error('source sha must be a full commit SHA')
    if not positive(source.get('run_id')) or not positive(source.get('run_attempt')):
        raise Error('source run_id and run_attempt must be positive integers')
    context = source.get('context')
    if not isinstance(context, str) or not re.fullmatch(r'[A-Za-z0-9._/-]{1,100}', context):
        raise Error('source context must be a status context')
    for key in ('project', 'concurrency_key'):
        if not isinstance(source.get(key), str) or not source[key] or len(source[key]) > 256:
            raise Error(f'source {key} must be a nonempty string')
    if not ID.fullmatch(source['project']):
        raise Error('invalid project')
    if type(source.get('cancel_in_progress', False)) is not bool:
        raise Error('cancel_in_progress must be boolean')
    return fingerprint([repo.lower(), source['run_id'], source['run_attempt'], context])[:32]


def relative(value: object) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= 200 and bool(re.fullmatch(r'[A-Za-z0-9_./-]+', value))
            and not value.startswith('/') and all(p not in ('', '.', '..') for p in value.rstrip('/').split('/')))


def allocation_job_name(task_id: str, task_attempt: int) -> str:
    return f'allocate/{task_id}/{task_attempt}'


def execution_job_name(task_id: str, task_attempt: int) -> str:
    return f'execute/{task_id}/{task_attempt}'


def validate_allocation(value: object) -> dict:
    if not isinstance(value, dict) or type(value.get('allocation_schema')) is not int or value['allocation_schema'] not in (1, 2, 3):
        raise Error('unknown allocation schema')
    for key in ('allocation_id', 'task_id', 'runner_tag', 'host'):
        if not isinstance(value.get(key), str) or not ID.fullmatch(value[key]):
            raise Error(f'invalid allocation {key}')
    device = value.get('device')
    if not isinstance(device, str) or device not in DEVICES:
        raise Error('invalid allocation device')
    if value['runner_tag'] not in runner_names(device):
        raise Error('runner tag does not belong to allocation device')
    if value.get('backend') != ('slurm' if device == 'slurm' else 'local'):
        raise Error('allocation backend does not match device')
    if value['allocation_schema'] == 2:
        if device not in GPU_QUOTAS or not isinstance(value.get('gpu_uuid'), str) or not GPU_UUID.fullmatch(value['gpu_uuid']):
            raise Error('GPU allocation requires one physical GPU UUID on a local device')
        if 'gpu_uuids' in value or 'gpu_count' in value:
            raise Error('single-card allocation cannot carry a multi-card envelope')
    elif value['allocation_schema'] == 3:
        cards = value.get('gpu_uuids')
        if (device not in GPU_QUOTAS or 'gpu_uuid' in value
                or type(value.get('gpu_count')) is not int or value['gpu_count'] != 2
                or not isinstance(cards, list) or len(cards) != 2):
            raise Error('two-card allocation requires its complete physical UUID pair')
        validate_gpu_list(device, cards)
    elif any(k in value for k in ('gpu_uuid', 'gpu_uuids', 'gpu_count')):
        raise Error('CPU allocation cannot carry a GPU UUID')
    groups = value.get('cpu_groups')
    if not positive(groups) or groups > DEVICE_GROUPS[device]:
        raise Error('invalid allocation group count')
    if type(value.get('cores')) is not int or value['cores'] != groups * GROUP_CPUS:
        raise Error('allocation core count does not match groups')
    memory = value.get('memory_gib', groups * GROUP_GIB)
    if not positive(memory) or memory > (1 << 20) or value.get('RAM') != f'{memory}G':
        raise Error('allocation RAM does not match groups')
    if not positive(value.get('workers')) or not number(value.get('minutes'), MAX_MINUTES):
        raise Error('invalid allocation workers or minutes')
    if type(value.get('task_attempt', 1)) is not int or value.get('task_attempt', 1) not in (1, 2):
        raise Error('invalid allocation task attempt')
    requested = value.get('requested_device', '')
    if requested not in ('', device):
        raise Error('allocation changed the requested device')
    return dict(value)
