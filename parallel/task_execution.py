"""Execute an authorized resource allocation using the existing part supervisor."""
from __future__ import annotations

import dataclasses
import functools
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
from parallel import protocol, layout, gpu_resources, organization, transport


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
        for card in protocol.grant_cards(grant):
            if not db.execute('SELECT 1 FROM held_gpus WHERE uuid=? AND allocation=?',
                              (card, grant['allocation_id'])).fetchone():
                raise protocol.Error('allocation no longer holds every authorized GPU')
        db.commit()
    finally:
        db.close()


def supervise(record, grant, cpus, command, fence=None, max_temp_c=80, gpu_mem_mib=0, *, launch_argv=None,
              verify_resource_limits=True):
    """Verify configured resource limits before acknowledging or starting a task."""
    try:
        is_slurm = grant.get('backend') == 'slurm'
        supervisor_memory_cap = registry.memory_bytes(grant['RAM']) if is_slurm else 0
        if is_slurm or verify_resource_limits:
            quota, memory = resource_limits()
            width = slurm_cpu_width() if is_slurm else None
            check_limits(grant, quota, memory, slurm_width=width, supervisor_memory_cap=supervisor_memory_cap)
        cards = protocol.grant_cards(grant)
        ready = gpu_resources.available(cards, max_temp_c) if cards else {}
        if any(ready.get(card, -1) < gpu_mem_mib for card in cards):
            raise protocol.Error('authorized GPU is no longer available')
        if fence is not None:
            check_fence(fence, grant, cpus)
        os.environ.update(CUDA_VISIBLE_DEVICES=','.join(cards), CI_CPUS=part.cpu_list(os.sched_getaffinity(0)),
                          CI_CORES=str(grant['cores']), CI_WORKERS=str(grant['workers']),
                          CI_CPU_GROUPS=str(grant['cpu_groups']), CI_RAM=grant['RAM'])
        part._write_json(os.path.join(record, 'allocation-ready.json'), {'allocation_id': grant['allocation_id'],
                         'cores': grant['cores'], 'RAM': grant['RAM'], 'workers': grant['workers'],
                         'gpu_uuid': cards[0] if len(cards) == 1 else '', 'gpu_uuids': cards,
                         'memory_enforcement': ('process-tree' if supervisor_memory_cap else
                                                'cgroup' if verify_resource_limits else 'unverified')})
    except (OSError, ValueError, KeyError, sqlite3.Error) as e:
        part._write_json(os.path.join(record, 'exit.json'), {'rc': None, 'unavailable': f'resources: {type(e).__name__}',
                         'end_epoch': time.time(), 'boot_end': part.boot_id()})
        return 2
    argv = launch_argv if launch_argv is not None else part.command_argv(command, nice=10)
    options = {'cap': supervisor_memory_cap}
    if len(cards) == 2:
        options['p2p'] = part.p2p_argv(str(Path(__file__).resolve().parents[1]))
    return part.supervise(record, grant['minutes']*60, argv, **options)


def execute(inv, etc, env, allocation, cpus, *, run=subprocess.run, spawn=subprocess.Popen):
    grant = protocol.validate_allocation(allocation['grant'])
    source, task = allocation['source'], allocation['part']
    settings = transport.settings(etc) if layout.settings_path(etc).is_file() else {}
    verify_resource_limits = settings.get('executor', {}).get('verify_resource_limits', True)
    if type(verify_resource_limits) is not bool:
        raise protocol.Error('executor resource verification setting must be boolean')
    if allocation.get('repository_environment') is True and 'env_root' not in settings.get('organization', {}):
        raise protocol.Error('repository environment mode changed on the execution host')
    cache_mode = 'env_cache' in settings.get('organization', {})
    if (cache_mode != ('environment_recipe' in source) or
            allocation.get('environment_cache') is True and not cache_mode):
        raise protocol.Error('environment cache mode changed on the execution host')
    organization_mode = 'organization' in settings
    isolated_scope = organization.standalone_executor(settings)
    organization_slurm = organization_mode and grant['backend'] == 'slurm'
    if organization_mode:
        if allocation.get('organization_repository_verified') is not True:
            raise protocol.Error('allocation is not a verified organization job')
        proj = organization.project(settings, inv, source)
        if organization_slurm:
            if allocation.get('organization_slurm_verified') is not True:
                raise protocol.Error('allocation is not authorized for organization Slurm execution')
            organization.validate_slurm_route(source)
    else:
        proj = registry.load_file(etc, source['project'], inv.this)
    if source['repository'] != proj.repository or grant['host'] != inv.this:
        raise protocol.Error('allocation belongs to another project or execution host')
    exceeds_host_memory = (not organization_slurm
                           and registry.memory_bytes(grant['RAM']) > registry.memory_bytes(proj.memory_max))
    if grant['minutes'] > proj.max_minutes or exceeds_host_memory:
        raise protocol.Error('allocation exceeds the current project limits')
    if organization_mode and not organization_slurm and grant['device'] not in proj.devices:
        raise protocol.Error('allocation device is outside the organization project')
    if (grant['workers'] != task['workers'] or grant['cores'] != task['cores']
            or grant['RAM'] != f'{protocol.part_memory_gib(task)}G'):
        raise protocol.Error('allocation changed the requested resources')
    cards = protocol.grant_cards(grant)
    if (protocol.gpu_count(task) != len(cards)
            or cards and (not set(cards) <= set(inv.host.get('gpu_uuids', [])) or grant['device'] not in proj.devices)
            or task.get('device') and task['device'] != grant['device']):
        raise protocol.Error('allocation changed the requested GPU or device')
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
        options = {}
        if cache_mode:
            from ci_runners import environment_cache
            recipe = source['environment_recipe']
            if recipe.get('repository') != source['repository'].lower():
                raise protocol.Error('cluster environment belongs to another repository')
            proj = dataclasses.replace(proj, variables=environment_cache.validate(recipe).variables)
            options['environment_recipe'] = recipe
        return engine._slurm(args, proj, grant['RAM'], record, rec, work, 'none',
                             isolated_scope=isolated_scope,
                             organization_slurm_project=source['slurm_project'] if organization_mode else None, **options)
    if cpus:
        raise protocol.Error('CPU-number allocations must be drained before upgrading')
    validate = functools.partial(organization.build_ready, settings, proj.repository) if organization_mode else None
    build, fd = envuse.hold(proj.env, validate=validate) if proj.env else ('', None)
    try:
        if organization_mode and not cache_mode and not build:
            return engine._end(record, rec, args,
                               unavailable=f'organization environment for {proj.repository} is not ready on the execution host')
        if 'env_root' in settings.get('organization', {}):
            rec['env_build'] = os.path.basename(build)
        command_env = part.part_env(env, role=inv.this, project=dataclasses.replace(proj, env=build),
                                    part_dir=work, results=results, cards=cards, cpus='',
                                    files=inv.host['files'], slot='', resume=False)
        # v2 never re-enters the legacy CPU slot or host-load protocol.
        for name in ('CI_SLOT_LOCK', 'CI_SLOT_QUEUE', 'CI_SLOT_HELD', 'CI_HOST_LOCK', 'LOCAL_CI_HOST_LOCK'):
            command_env.pop(name, None)
        if source.get('files_artifact_id') or source.get('files_bundle'):
            command_env['CI_PLAN_FILES'] = str(Path(env['RUNNER_TEMP'])/'ci-plan-files')
        call = ('import json,sys; sys.path.insert(0,sys.argv.pop(1)); from parallel.task_execution import supervise; '
            'a=json.loads(sys.argv[1]); sys.exit(supervise(*a[:-1], verify_resource_limits=a[-1]))')
        fence = str(layout.executor_paths(inv)[0])
        args_for_scope = [record, grant, cpus, task['command'], fence, inv.host['max_temp_c'], proj.gpu_mem_mib]
        if cache_mode:
            from parallel import job_environment
            rec['uses_environment_cache'] = True
            preparation = job_environment.request(settings['organization'], inv, source)
            launch = job_environment.wrap(record, preparation, release, part.command_argv(task['command'], nice=10))
            # The existing supervisor covers both cold builds and the subsequent test.
            args_for_scope.append(launch)
            call = ('import json,sys; sys.path.insert(0,sys.argv.pop(1)); '
                    'from parallel.task_execution import supervise; a=json.loads(sys.argv[1]); '
                    'sys.exit(supervise(*a[:-2], launch_argv=a[-2], verify_resource_limits=a[-1]))')
        args_for_scope.append(verify_resource_limits)
        inner = [sys.executable, '-I', '-c', call, release, protocol.dumps(args_for_scope)]
        engine.unit = part.unit_name(grant['runner_tag'], task['id'], f'{env["GITHUB_RUN_ID"]}-{env["GITHUB_RUN_ATTEMPT"]}-{task_attempt}',
                                     organization=isolated_scope)
        rec.update(unit=engine.unit, run_epoch=time.time(), queue_seconds=0)
        part._write_json(os.path.join(record, 'record.json'), rec)
        slice_options = {'slice_name': 'civ2.slice' if isolated_scope else 'ci.slice'} if organization_mode else {}
        argv = part.scope_argv(engine.unit, proj.name, grant['RAM'], [], engine.oom_policy(), **slice_options)
        argv += ['-p', f'CPUQuota={grant["cores"] * 100}%', *inner]
        engine.proc = spawn(argv, cwd=env['GITHUB_WORKSPACE'], env=command_env, pass_fds=() if fd is None else (fd,))
        engine.proc.wait()
        return engine._end(record, rec, args, ex=part._read_json(os.path.join(record, 'exit.json')),
                           stopped=engine.stopped)
    finally:
        if fd is not None:
            os.close(fd)
