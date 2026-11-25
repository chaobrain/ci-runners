"""Controller-owned reference timing profiles and inherited exclusive holds."""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import re
import time

from parallel import deployment_files, protocol


def read_profiles(path):
    if path is None:
        return {}
    try:
        doc = json.loads(deployment_files.read_private(path))
    except (OSError, ValueError) as error:
        raise protocol.Error('cannot read timing profiles') from error
    if (not isinstance(doc, dict) or set(doc) != {'schema', 'targets'}
            or type(doc['schema']) is not int or doc['schema'] != 1
            or not isinstance(doc['targets'], list) or len(doc['targets']) > 64):
        raise protocol.Error('invalid timing profile document')
    targets = {}
    fields = {'id', 'device', 'cpu_count', 'cpus', 'cpu_model', 'host_lock', 'enabled', 'reference_ready', 'repositories'}
    for p in doc['targets']:
        if (not isinstance(p, dict) or set(p) != fields or not isinstance(p['id'], str) or not protocol.ID.fullmatch(p['id'])
                or p['id'] in targets or not isinstance(p['device'], str) or p['device'] not in protocol.DEVICES - {'slurm'}
                or not protocol.positive(p['cpu_count']) or p['cpu_count'] % 4
                or p['cpu_count'] > protocol.DEVICE_GROUPS[p['device']] * 4
                or not isinstance(p['cpus'], list) or not p['cpus']
                or any(type(c) is not int or c < 0 for c in p['cpus']) or p['cpus'] != sorted(set(p['cpus']))
                or len(p['cpus']) > p['cpu_count']
                or not isinstance(p['cpu_model'], str) or not p['cpu_model'].strip() or len(p['cpu_model']) > 256
                or not isinstance(p['host_lock'], str) or not Path(p['host_lock']).is_absolute()
                or '..' in Path(p['host_lock']).parts or '\0' in p['host_lock']
                or type(p['enabled']) is not bool or type(p['reference_ready']) is not bool
                or not isinstance(p['repositories'], list) or not p['repositories']):
            raise protocol.Error('invalid reference timing profile')
        ids, names = set(), set()
        for item in p['repositories']:
            if (not isinstance(item, dict) or set(item) != {'id', 'repository'} or not protocol.positive(item['id'])
                    or not isinstance(item['repository'], str) or not protocol.REPO.fullmatch(item['repository'])
                    or item['id'] in ids or item['repository'].lower() in names):
                raise protocol.Error('invalid timing repository binding')
            ids.add(item['id']); names.add(item['repository'].lower())
        targets[p['id']] = p
    return targets


def authorized(p, source):
    return (p['enabled'] and p['reference_ready'] and any(
        entry['id'] == source.get('repository_id') and type(source.get('repository_id')) is int
        and entry['repository'].lower() == source['repository'].lower() for entry in p['repositories']))


def public_profile(p):
    return dict(device=p['device'], cores=p['cpu_count'], profile_sha256=protocol.fingerprint(p))


def snapshot(path, source):
    return {name: public_profile(p) for name, p in read_profiles(path).items() if authorized(p, source)}


def task_profile(source, task):
    p = source.get('timing_profiles', {}).get(task.get('timing_target'))
    if (task.get('isolation') != 'timing' or not isinstance(p, dict)
            or set(p) != {'device', 'cores', 'profile_sha256'}
            or p['device'] != task.get('device') or p['cores'] != task['cores']
            or not isinstance(p['profile_sha256'], str) or not re.fullmatch('[0-9a-f]{64}', p['profile_sha256'])):
        raise protocol.Error('timing task lacks its complete authorized device profile')
    return p


def permitted(path, source, task):
    try:
        frozen = task_profile(source, task)
        current = read_profiles(path).get(task['timing_target'])
        return current is not None and authorized(current, source) and public_profile(current) == frozen
    except (OSError, ValueError):
        return False


def cpu_model():
    values = {line.split(':', 1)[1].strip() for line in Path('/proc/cpuinfo').read_text().splitlines()
              if line.startswith('model name') and ':' in line}
    if len(values) != 1:
        raise protocol.Error('timing reference CPU identity is ambiguous')
    return values.pop()


@contextlib.contextmanager
def hold(grant, request):
    """Run inside the verified scope; a live ancestor retains the same old host lock."""
    from ci_runners import locks
    if not isinstance(request, dict):
        raise protocol.Error('timing execution request missing')
    p = read_profiles(request['profiles_file']).get(grant['timing_target'])
    if (p is None or not authorized(p, request['source']) or public_profile(p)['profile_sha256'] != grant['timing_profile_sha256']
            or p['device'] != grant['device'] or p['cpu_count'] != grant['cores']
            or request['cpu_count'] != grant['cores'] or p['host_lock'] != request['host_lock']):
        raise protocol.Error('timing profile changed before launch')
    original_affinity = os.sched_getaffinity(0)
    if not set(p['cpus']) <= original_affinity or cpu_model() != p['cpu_model']:
        raise protocol.Error('timing reference CPU or affinity is unavailable')
    if os.getpriority(os.PRIO_PROCESS, 0) != 0:
        raise protocol.Error('timing command must inherit nice zero')
    left = request['deadline'] - time.time()
    if left <= 0:
        raise protocol.Error('timing lock queue deadline expired')
    holder = locks.HostLock(p['host_lock'], timeout=left, held='')
    keys = ('LOCAL_CI_HOST_LOCK', 'CI_HOST_LOCK', locks.ENV)
    previous = {key: os.environ.get(key) for key in keys}
    try:
        holder.acquire('x')
        # Check again after a wait: a reconfigured profile cannot launch stale work.
        if read_profiles(request['profiles_file']).get(grant['timing_target']) != p:
            raise protocol.Error('timing profile changed while waiting for its lock')
        os.sched_setaffinity(0, p['cpus'])
        if os.sched_getaffinity(0) != set(p['cpus']) or os.getpriority(os.PRIO_PROCESS, 0) != 0:
            raise protocol.Error('timing reference affinity or priority was not preserved')
        os.environ.update(LOCAL_CI_HOST_LOCK=p['host_lock'], CI_HOST_LOCK=p['host_lock'], BRAINSPIKE_HOST_LOCK='x')
        yield dict(target=grant['timing_target'], profile_sha256=grant['timing_profile_sha256'],
                   affinity=sorted(os.sched_getaffinity(0)), required_affinity=p['cpus'], nice=0, host_lock='exclusive')
    finally:
        try:
            os.sched_setaffinity(0, original_affinity)
        finally:
            holder.release()
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
