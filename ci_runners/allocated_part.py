"""Execute an already authorized CPU allocation using the existing part supervisor."""
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

from ci_runners import central, drain, envuse, inventory, part, registry


def resource_limits():
    """The current cgroup's effective CPU set and smallest ancestral memory ceiling."""
    lines = Path('/proc/self/cgroup').read_text().splitlines()
    unified = next((line[3:] for line in lines if line.startswith('0::/')), '')
    if unified:
        root = Path('/sys/fs/cgroup')
        here = root / unified.lstrip('/')
        cpus = (here / 'cpuset.cpus.effective').read_text().strip()
        filename = 'memory.max'
    else:
        entries = {ctl: path for line in lines for _, controls, path in [line.split(':', 2)] for ctl in controls.split(',')}
        cpus = (Path('/sys/fs/cgroup/cpuset') / entries['cpuset'].lstrip('/') / 'cpuset.cpus').read_text().strip()
        root = Path('/sys/fs/cgroup/memory')
        here = root / entries['memory'].lstrip('/')
        filename = 'memory.limit_in_bytes'
    values = []
    while here == root or root in here.parents:
        try:
            raw = (here / filename).read_text().strip()
            if raw.isdigit():
                values.append(int(raw))
        except FileNotFoundError:
            pass
        if here == root:
            break
        here = here.parent
    ranges = inventory.cpu_ranges(cpus)
    if ranges is None or not values:
        raise central.Error('CPU or memory cgroup limit is unavailable')
    return {c for lo, hi in ranges for c in range(lo, hi+1)}, min(values)


def check_limits(grant, cpus, effective, memory):
    wanted = set(cpus)
    if len(wanted) != grant['cores'] or effective != wanted:
        raise central.Error('effective CPU set does not match the authorized allocation')
    if not 0 < memory <= registry.memory_bytes(grant['RAM']):
        raise central.Error('aggregate memory limit is not enforced')


def check_fence(path, grant, cpus):
    """Serialize the final in-scope launch check with the controller's release check."""
    db = sqlite3.connect(Path(path).resolve().as_uri()+'?mode=rw', uri=True, isolation_level=None, timeout=30)
    try:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT state,cancelled,grant_json,cpus FROM executions WHERE id=?', (grant['allocation_id'],)).fetchone()
        if (row is None or row[0] == 'released' or row[1] or json.loads(row[2]) != grant or json.loads(row[3]) != cpus):
            raise central.Error('allocation was closed before the scope started')
        db.commit()
    finally:
        db.close()


def supervise(record, grant, cpus, command, fence=None):
    """Prove resource enforcement before acknowledging or starting any test process."""
    try:
        effective, memory = resource_limits()
        check_limits(grant, cpus, effective, memory)
        os.sched_setaffinity(0, cpus)
        if os.sched_getaffinity(0) != set(cpus):
            raise central.Error('effective affinity differs from the allocation')
        if fence is not None:
            check_fence(fence, grant, cpus)
        os.environ.update(CI_CPUS=part.cpu_list(cpus), CI_WORKERS=str(grant['workers']),
                          CI_CPU_GROUPS=str(grant['cpu_groups']), CI_RAM=grant['RAM'])
        part._write_json(os.path.join(record, 'allocation-ready.json'), {'allocation_id': grant['allocation_id'],
                         'cpus': cpus, 'RAM': grant['RAM'], 'workers': grant['workers']})
    except (OSError, ValueError, KeyError, sqlite3.Error) as e:
        part._write_json(os.path.join(record, 'exit.json'), {'rc': None, 'unavailable': f'resources: {type(e).__name__}',
                         'end_epoch': time.time(), 'boot_end': part.boot_id()})
        return 2
    return part.supervise(record, grant['minutes']*60, part.command_argv(command, nice=10))


def execute(inv, etc, env, allocation, cpus, *, run=subprocess.run, spawn=subprocess.Popen):
    grant = central.validate_allocation(allocation['grant'])
    source, task = allocation['source'], allocation['part']
    proj = registry.load_file(etc, source['project'], inv.this)
    if source['repository'] != proj.repository or grant['host'] != inv.this:
        raise central.Error('allocation belongs to another project or execution host')
    if grant['minutes'] > proj.max_minutes or registry.memory_bytes(grant['RAM']) > registry.memory_bytes(proj.memory_max):
        raise central.Error('allocation exceeds the current project limits')
    if grant['workers'] != task['workers'] or grant['cores'] != task['cores']:
        raise central.Error('allocation changed the requested resources')
    release = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    engine = part.Executor(inv, etc, env, release=release, run=run, spawn=spawn)
    args = part.Inputs(project=proj.name, command=task['command'], id=task['id'], timeout_minutes=grant['minutes'],
                       memory=grant['RAM'], workers=grant['workers'], cores=grant['cores'],
                       queue_timeout_minutes=max(0., (allocation['deadline']-time.time())/60),
                       results=task.get('results', ''), target=source.get('slurm_target', ''))
    record = engine.record_dir(args, 1)
    engine.record = record
    os.makedirs(os.path.dirname(record), mode=0o700, exist_ok=True)
    os.mkdir(record, 0o700)
    work = os.path.join(record, 'work')
    results = os.path.join(work, 'results')
    os.makedirs(results, mode=0o700)
    rec = {'project': proj.name, 'id': task['id'], 'class': 'executor', 'host': inv.this,
           'runner': grant['runner_tag'], 'attempt': 1, 'retries': proj.retries,
           'inputs': dataclasses.asdict(args), 'allocation': grant, 'start_epoch': time.time(),
           'boot_start': part.boot_id(), 'executor': os.getpid(), 'release': release, 'cards': []}
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
    if len(set(cpus)) != grant['cores'] or not set(cpus) <= os.sched_getaffinity(0):
        raise central.Error('authorized CPUs are unavailable on this host')
    build, fd = envuse.hold(proj.env) if proj.env else ('', None)
    try:
        command_env = part.part_env(env, role=inv.this, project=dataclasses.replace(proj, env=build),
                                    part_dir=work, results=results, cards=[], cpus=part.cpu_list(cpus),
                                    files=inv.host['files'], slot='', resume=False)
        # v2 never re-enters the legacy CPU slot or host-load protocol.
        for name in ('CI_SLOT_LOCK', 'CI_SLOT_QUEUE', 'CI_SLOT_HELD', 'CI_HOST_LOCK', 'LOCAL_CI_HOST_LOCK'):
            command_env.pop(name, None)
        if source.get('files_artifact_id'):
            command_env['CI_PLAN_FILES'] = str(Path(env['RUNNER_TEMP'])/'ci-plan-files')
        call = 'import json,sys; sys.path.insert(0,sys.argv.pop(1)); from ci_runners.allocated_part import supervise; sys.exit(supervise(*json.loads(sys.argv[1])))'
        fence = str(Path(inv.host['data'])/'ci-runners/state/executor.sqlite')
        inner = [sys.executable, '-I', '-c', call, release, central.dumps([record, grant, cpus, task['command'], fence])]
        engine.unit = part.unit_name(grant['runner_tag'], task['id'], f'{env["GITHUB_RUN_ID"]}-{env["GITHUB_RUN_ATTEMPT"]}-1')
        rec.update(unit=engine.unit, run_epoch=time.time(), queue_seconds=0)
        part._write_json(os.path.join(record, 'record.json'), rec)
        argv = part.scope_argv(engine.unit, proj.name, grant['RAM'], [], engine.oom_policy())
        argv += ['-p', f'AllowedCPUs={part.cpu_list(cpus)}', *inner]
        engine.proc = spawn(argv, cwd=env['GITHUB_WORKSPACE'], env=command_env, pass_fds=() if fd is None else (fd,))
        engine.proc.wait()
        return engine._end(record, rec, args, ex=part._read_json(os.path.join(record, 'exit.json')),
                           stopped=engine.stopped)
    finally:
        if fd is not None:
            os.close(fd)
