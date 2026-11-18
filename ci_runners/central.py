"""Schema 2 CPU plans and allocation contracts. No I/O or machine inventory."""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping

GROUP_CPUS = GROUP_GIB = 4
DEVICE_GROUPS = {'3090a': 4, '3090b': 4, 'pro6000': 20, 'h100': 10, 'slurm': 20}
DEVICES = frozenset(DEVICE_GROUPS)
MAX_MINUTES = 1440
ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z')
REPO = re.compile(r'[A-Za-z0-9-]{1,39}/[A-Za-z0-9_.-]{1,100}\Z')
SHA = re.compile(r'[0-9a-f]{40}\Z')
TERMINAL = frozenset({'success', 'failure', 'error', 'cancelled'})


class Error(ValueError):
    """A malformed request or a state transition not justified by evidence."""


def positive(value: object) -> bool:
    return type(value) is int and value > 0


def number(value: object, maximum: float) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and 0 < value <= maximum


def dumps(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def fingerprint(value: object) -> str:
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def runner_names(device: str, count: int | None = None, start: int = 0) -> list[str]:
    if device not in DEVICES | {'plan'}:
        raise Error('unknown device')
    count = (2 if device == 'plan' else DEVICE_GROUPS[device]) if count is None else count
    limit = 64 if device == 'plan' else DEVICE_GROUPS[device]
    if not positive(count) or type(start) is not int or start < 0 or start + count > limit:
        raise Error('runner range exceeds the device quota')
    return [f'{device}{"" if device == "slurm" else "-"}{i}' for i in range(start, start + count)]


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
    keys = {'id', 'needs', 'device', 'cores', 'workers', 'minutes', 'queue_minutes',
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
        cores = p.get('cores', GROUP_CPUS)
        if not positive(cores) or cores % GROUP_CPUS:
            raise Error(f'{name}: cores must be a positive multiple of {GROUP_CPUS}')
        limit = DEVICE_GROUPS.get(device, max(DEVICE_GROUPS.values()))
        if cores > limit * GROUP_CPUS:
            raise Error(f'{name}: cores exceeds the requested device capacity')
        if not positive(p.get('workers')):
            raise Error(f'{name}: workers must be an explicit positive integer')
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


def validate_allocation(value: object) -> dict:
    if not isinstance(value, dict) or type(value.get('allocation_schema')) is not int or value['allocation_schema'] != 1:
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
    groups = value.get('cpu_groups')
    if not positive(groups) or groups > DEVICE_GROUPS[device]:
        raise Error('invalid allocation group count')
    if type(value.get('cores')) is not int or value['cores'] != groups * GROUP_CPUS:
        raise Error('allocation core count does not match groups')
    if value.get('RAM') != f'{groups * GROUP_GIB}G':
        raise Error('allocation RAM does not match groups')
    if not positive(value.get('workers')) or not number(value.get('minutes'), MAX_MINUTES):
        raise Error('invalid allocation workers or minutes')
    requested = value.get('requested_device', '')
    if requested not in ('', device):
        raise Error('allocation changed the requested device')
    return dict(value)
