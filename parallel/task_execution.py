"""Execute an authorized resource allocation using the existing part supervisor."""
from __future__ import annotations

import dataclasses
import json
import os
import signal
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from fractions import Fraction

from ci_runners import drain, envuse, inventory, part, registry
from parallel import protocol, layout, gpu_resources


def _controller(name):
    """Return this controller's root, membership and version in unified or hybrid cgroups."""
    memberships = {}
    unified = None
    for line in Path('/proc/self/cgroup').read_text().splitlines():
        _, controllers, location = line.split(':', 2)
        if not controllers:
            unified = location
        for controller in controllers.split(','):
            if controller:
                memberships[controller] = location
    if name in memberships:
        root = Path('/sys/fs/cgroup') / name
        return root, root / memberships[name].lstrip('/'), 1
    if unified is not None:
        root = Path('/sys/fs/cgroup')
        return root, root / unified.lstrip('/'), 2
    return None


def _ancestors(controller):
    if controller is None:
        return
    root, here, version = controller
    while here == root or root in here.parents:
        yield here, version
        if here == root:
            break
        here = here.parent


def resource_limits():
    """Return the tightest ancestor CPU-time quota (cores) and memory cap (bytes)."""
    quotas = []
    for here, version in _ancestors(_controller('cpu')):
        try:
            if version == 2:
                quota, period = (here / 'cpu.max').read_text().split()
                if quota == 'max':
                    continue
            else:
                quota = (here / 'cpu.cfs_quota_us').read_text().strip()
                if quota == '-1':
                    continue
                period = (here / 'cpu.cfs_period_us').read_text().strip()
            quota, period = int(quota), int(period)
            if quota <= 0 or period <= 0:
                raise protocol.Error('invalid CPU cgroup quota')
            quotas.append(Fraction(quota, period))
        except FileNotFoundError:
            continue
    memory = []
    for here, version in _ancestors(_controller('memory')):
        try:
            raw = (here / ('memory.max' if version == 2 else 'memory.limit_in_bytes')).read_text().strip()
        except FileNotFoundError:
            continue
        if raw == 'max':
            continue
        if not raw.isdigit() or int(raw) <= 0:
            raise protocol.Error('invalid memory cgroup limit')
        memory.append(int(raw))
    return min(quotas, default=None), min(memory, default=None)


def slurm_cpu_width():
    """Read a Slurm-managed cgroup's CPU-set width without choosing or binding CPUs."""
    for here, version in _ancestors(_controller('cpuset')):
        try:
            raw = (here / ('cpuset.cpus.effective' if version == 2 else 'cpuset.cpus')).read_text().strip()
        except FileNotFoundError:
            continue
        if raw:
            ranges = inventory.cpu_ranges(raw)
            if ranges is None:
                raise protocol.Error('invalid Slurm CPU allocation')
            return len({cpu for lo, hi in ranges for cpu in range(lo, hi + 1)})
    return None


def check_limits(grant, cpu_quota, memory, *, slurm_width=None, supervisor_memory_cap=0):
    quota_ok = cpu_quota is not None and 0 < cpu_quota <= grant['cores']
    slurm_ok = grant.get('backend') == 'slurm' and slurm_width is not None and 0 < slurm_width <= grant['cores']
    if not (quota_ok or slurm_ok):
        raise protocol.Error('aggregate CPU limit is not enforced')
    required_memory = registry.memory_bytes(grant['RAM'])
    if supervisor_memory_cap:
        if grant.get('backend') != 'slurm' or supervisor_memory_cap != required_memory:
            raise protocol.Error('invalid Slurm supervisor memory limit')
    elif memory is None or not 0 < memory <= required_memory:
        raise protocol.Error('aggregate memory limit is not enforced')


def check_fence(path, grant, cpus):
    """Serialize the final in-scope launch check with the controller's release check."""
    db = sqlite3.connect(Path(path).resolve().as_uri()+'?mode=rw', uri=True, isolation_level=None, timeout=30)
    try:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT state,cancelled,grant_json,cpus FROM executions WHERE id=?', (grant['allocation_id'],)).fetchone()
        if (row is None or row[0] == 'released' or row[1] or json.loads(row[2]) != grant or json.loads(row[3]) != cpus):
            raise protocol.Error('allocation was closed before the scope started')
        if grant.get('gpu_uuid') and not db.execute('SELECT 1 FROM held_gpus WHERE uuid=? AND allocation=?',
                                                  (grant['gpu_uuid'], grant['allocation_id'])).fetchone():
            raise protocol.Error('allocation no longer holds the authorized GPU')
        db.commit()
    finally:
        db.close()


def supervise(record, grant, cpus, command, fence=None, max_temp_c=80, gpu_mem_mib=0):
    """Prove resource enforcement before acknowledging or starting any test process."""
    try:
        quota, memory = resource_limits()
        is_slurm = grant.get('backend') == 'slurm'
        supervisor_memory_cap = registry.memory_bytes(grant['RAM']) if is_slurm else 0
        width = slurm_cpu_width() if is_slurm else None
        check_limits(grant, quota, memory, slurm_width=width, supervisor_memory_cap=supervisor_memory_cap)
        card = grant.get('gpu_uuid', '')
        if card and gpu_resources.available([card], max_temp_c).get(card, -1) < gpu_mem_mib:
            raise protocol.Error('authorized GPU is no longer available')
        if fence is not None:
            check_fence(fence, grant, cpus)
        os.environ.update(CUDA_VISIBLE_DEVICES=card, CI_CPUS=part.cpu_list(os.sched_getaffinity(0)),
                          CI_CORES=str(grant['cores']), CI_WORKERS=str(grant['workers']),
                          CI_CPU_GROUPS=str(grant['cpu_groups']), CI_RAM=grant['RAM'])
        part._write_json(os.path.join(record, 'allocation-ready.json'), {'allocation_id': grant['allocation_id'],
                         'cores': grant['cores'], 'RAM': grant['RAM'], 'workers': grant['workers'], 'gpu_uuid': card,
                         'memory_enforcement': 'process-tree' if supervisor_memory_cap else 'cgroup'})
    except (OSError, ValueError, KeyError, sqlite3.Error) as e:
        part._write_json(os.path.join(record, 'exit.json'), {'rc': None, 'unavailable': f'resources: {type(e).__name__}',
                         'end_epoch': time.time(), 'boot_end': part.boot_id()})
        return 2
    return part.supervise(record, grant['minutes']*60, part.command_argv(command, nice=10),
                          cap=supervisor_memory_cap)


def execute(inv, etc, env, allocation, cpus, *, run=subprocess.run, spawn=subprocess.Popen):
    grant = protocol.validate_allocation(allocation['grant'])
    source, task = allocation['source'], allocation['part']
    proj = registry.load_file(etc, source['project'], inv.this)
    if source['repository'] != proj.repository or grant['host'] != inv.this:
        raise protocol.Error('allocation belongs to another project or execution host')
    if grant['minutes'] > proj.max_minutes or registry.memory_bytes(grant['RAM']) > registry.memory_bytes(proj.memory_max):
        raise protocol.Error('allocation exceeds the current project limits')
    if grant['workers'] != task['workers'] or grant['cores'] != task['cores']:
        raise protocol.Error('allocation changed the requested resources')
    card = grant.get('gpu_uuid')
    if (task.get('gpu', False) != (card is not None)
            or card and (card not in inv.host.get('gpu_uuids', []) or grant['device'] not in proj.devices)
            or task.get('device') and task['device'] != grant['device']):
        raise protocol.Error('allocation changed the requested GPU or device')
    cards = [card] if card else []
    release = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    engine = part.Executor(inv, etc, env, release=release, run=run, spawn=spawn)
    slurm_target = source.get('slurm_target', '').replace('/', ':', 1)
    args = part.Inputs(project=proj.name, command=task['command'], id=task['id'], timeout_minutes=grant['minutes'],
                       memory=grant['RAM'], workers=grant['workers'], cores=grant['cores'],
                       queue_timeout_minutes=max(0., (allocation['deadline']-time.time())/60),
                       results=task.get('results', ''), target=slurm_target, gpus=len(cards))
    task_attempt = grant.get('task_attempt', 1)
    record = engine.record_dir(args, task_attempt)
    engine.record = record
    os.makedirs(os.path.dirname(record), mode=0o700, exist_ok=True)
    os.mkdir(record, 0o700)
    work = os.path.join(record, 'work')
    results = os.path.join(work, 'results')
    os.makedirs(results, mode=0o700)
    rec = {'project': proj.name, 'id': task['id'], 'class': 'executor', 'host': inv.this,
           'runner': grant['runner_tag'], 'attempt': task_attempt, 'retries': proj.retries,
           'inputs': dataclasses.asdict(args), 'allocation': grant, 'start_epoch': time.time(),
           'boot_start': part.boot_id(), 'executor': os.getpid(), 'release': release, 'cards': cards}
    part._write_json(os.path.join(record, 'record.json'), rec)
    free = shutil.disk_usage(inv.host['data']).free / (1 << 30)
    if free < inv.host['disk_min_gib']:
        return engine._end(record, rec, args, unavailable='low disk on execution host')
    refusal = drain.refusal(drain.read(inv.host['data']), inv.host, 'executor')
    if refusal:
        return engine._end(record, rec, args, unavailable=refusal)
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, engine._on_signal)
    if grant['backend'] == 'slurm':
        engine.central_allocation = grant
        return engine._slurm(args, proj, grant['RAM'], record, rec, work, 'none')
    if cpus:
        raise protocol.Error('CPU-number allocations must be drained before upgrading')
    build, fd = envuse.hold(proj.env) if proj.env else ('', None)
    try:
        command_env = part.part_env(env, role=inv.this, project=dataclasses.replace(proj, env=build),
                                    part_dir=work, results=results, cards=cards, cpus='',
                                    files=inv.host['files'], slot='', resume=False)
        # v2 never re-enters the legacy CPU slot or host-load protocol.
        for name in ('CI_SLOT_LOCK', 'CI_SLOT_QUEUE', 'CI_SLOT_HELD', 'CI_HOST_LOCK', 'LOCAL_CI_HOST_LOCK'):
            command_env.pop(name, None)
        if source.get('files_artifact_id'):
            command_env['CI_PLAN_FILES'] = str(Path(env['RUNNER_TEMP'])/'ci-plan-files')
        call = 'import json,sys; sys.path.insert(0,sys.argv.pop(1)); from parallel.task_execution import supervise; sys.exit(supervise(*json.loads(sys.argv[1])))'
        fence = str(layout.executor_paths(inv)[0])
        args_for_scope = [record, grant, cpus, task['command'], fence, inv.host['max_temp_c'], proj.gpu_mem_mib]
        inner = [sys.executable, '-I', '-c', call, release, protocol.dumps(args_for_scope)]
        engine.unit = part.unit_name(grant['runner_tag'], task['id'], f'{env["GITHUB_RUN_ID"]}-{env["GITHUB_RUN_ATTEMPT"]}-{task_attempt}')
        rec.update(unit=engine.unit, run_epoch=time.time(), queue_seconds=0)
        part._write_json(os.path.join(record, 'record.json'), rec)
        argv = part.scope_argv(engine.unit, proj.name, grant['RAM'], [], engine.oom_policy())
        argv += ['-p', f'CPUQuota={grant["cores"] * 100}%', *inner]
        engine.proc = spawn(argv, cwd=env['GITHUB_WORKSPACE'], env=command_env, pass_fds=() if fd is None else (fd,))
        engine.proc.wait()
        return engine._end(record, rec, args, ex=part._read_json(os.path.join(record, 'exit.json')),
                           stopped=engine.stopped)
    finally:
        if fd is not None:
            os.close(fd)
