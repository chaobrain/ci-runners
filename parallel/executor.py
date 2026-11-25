"""One durable execution controller per physical host, independent of GitHub jobs."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from parallel import layout, health

from ci_runners import drain, part, pool, registry, setup, slots, slurmpart, slurmsite
from parallel import task_execution, protocol, transport, executor_store, gpu_resources, organization


def paths(inv):
    return layout.executor_paths(inv)


def groups(inv):
    count = sum(v['count'] for key, v in inv.host['pools'].items()
                if isinstance(v, dict) and v.get('class', key) == 'executor' and v['device'] != 'slurm')
    return list(range(count))


def identity(params):
    env = params['env']
    return dict(runner=env['RUNNER_NAME'], repository=env['GITHUB_REPOSITORY'],
                run_id=int(env['GITHUB_RUN_ID']), run_attempt=int(env['GITHUB_RUN_ATTEMPT']))


def record_path(inv, metadata):
    e, p = metadata['env'], metadata['part']
    task_attempt = metadata.get('task_attempt', 1)
    return str(Path(inv.host['data']) / 'ci-runners' / 'state' / 'parts' / e['GITHUB_REPOSITORY'].lower()
               / f'{e["GITHUB_RUN_ID"]}-{e["GITHUB_RUN_ATTEMPT"]}' / f'{p["id"]}-{task_attempt}')


def safe_workspace(inv, runner, path):
    root = (Path(inv.host['data']) / 'ci-work' / runner).resolve()
    candidate = Path(path).resolve()
    if root not in candidate.parents or not candidate.is_dir():
        raise protocol.Error('workspace or temporary directory is outside this runner')
    return str(candidate)


class ExecutorService:
    def __init__(self, inv, etc, ledger, remote, *, registration=None, settings=None, run=subprocess.run, spawn=subprocess.Popen):
        self.inv, self.etc, self.ledger, self.remote = inv, etc, ledger, remote
        self.run, self.spawn = run, spawn
        self.registration = registration
        self.settings = settings or {}
        self.registered = False
        self.names = {name for name, _, cls in setup.runners(inv) if cls == 'executor'}
        self.children = {}
        self.mutex = threading.RLock()
        self.workroot = paths(inv)[2]
        self.workroot.mkdir(parents=True, exist_ok=True, mode=0o700)

    def admit(self, params):
        who = identity(params)
        if who['runner'] not in self.names:
            raise protocol.Error('runner is not registered on this physical host')
        if 'grant' in params:
            grant = protocol.validate_allocation(params['grant'])
            if grant['host'] != self.inv.this or grant['runner_tag'] != who['runner']:
                raise protocol.Error('allocation names another host or runner')
            who['grant'] = grant
        config = self.settings.get('organization', {})
        repository_environment = 'env_root' in config
        cache = 'env_cache' in config
        if cache:
            who['environment_cache'] = True
        elif repository_environment:
            who['repository_environment'] = True
        allocation = self.remote.call('claim', **who)
        if cache:
            if (allocation.get('organization_repository_verified') is not True or
                    not isinstance(allocation.get('source', {}).get('environment_recipe'), dict)):
                raise protocol.Error('controller did not authorize a pinned environment recipe')
            return allocation
        if not repository_environment:
            return allocation
        grant = allocation['grant']
        if grant['backend'] == 'local':
            if allocation.get('organization_repository_verified') is not True:
                raise protocol.Error('controller did not verify the organization repository')
            repository = allocation['source']['repository']
            with self.mutex:
                row = self.ledger.get(grant['allocation_id'])
                if row is not None and row['state'] == 'released':
                    raise protocol.Error('allocation already released on this host')
                if row is None and not organization.environment_ready(self.settings, repository):
                    reason = f'organization environment for {repository} is not ready on the execution host'
                    self.ledger.cancel_or_close_unstarted(grant, {'state': 'error', 'reason': reason, 'retryable': True})
                    raise protocol.Error(reason)
        return allocation

    def _owned(self, params):
        aid = params['allocation_id']
        row = self.ledger.get(aid)
        if row is None:
            raise protocol.Error('unknown local allocation')
        who = identity(params)
        e = row['metadata'].get('env', {})
        stored_repository = e.get('GITHUB_REPOSITORY')
        caller_repository = who['repository']
        if (row['grant']['runner_tag'] != who['runner']
                or not isinstance(stored_repository, str) or not isinstance(caller_repository, str)
                or not protocol.REPO.fullmatch(caller_repository)
                or stored_repository.lower() != caller_repository.lower()
                or str(who['run_id']) != e.get('GITHUB_RUN_ID') or str(who['run_attempt']) != e.get('GITHUB_RUN_ATTEMPT')):
            raise protocol.Error('allocation belongs to another GitHub job')
        return row

    def call(self, method, params, _identity='local'):
        if method == 'health':
            return {'host': self.inv.this, 'pending_reports': len(self.ledger.reports())}
        if method in ('admit', 'inspect', 'start'):
            allocation = self.admit(params)
            if method != 'start':
                return allocation
            env = params['env']
            grant = allocation['grant']
            ws = safe_workspace(self.inv, grant['runner_tag'], env['GITHUB_WORKSPACE'])
            temp = safe_workspace(self.inv, grant['runner_tag'], env['RUNNER_TEMP'])
            # A command never inherits the daemon's credential or the job's token.
            clean = {k: os.environ[k] for k in part.USER_BUS if k in os.environ}
            clean.update(HOME=self.inv.host['home'], PATH='/usr/local/bin:/usr/bin:/bin', LANG='C.UTF-8',
                         GITHUB_WORKSPACE=ws, RUNNER_TEMP=temp, RUNNER_NAME=grant['runner_tag'],
                         GITHUB_REPOSITORY=allocation['source']['repository'], GITHUB_SHA=allocation['source']['sha'],
                         GITHUB_RUN_ID=str(allocation['run_id']), GITHUB_RUN_ATTEMPT=str(allocation['run_attempt']))
            metadata = {k: allocation[k] for k in ('part', 'source', 'deadline')}
            if 'organization' in self.settings:
                if allocation.get('organization_repository_verified') is not True:
                    raise protocol.Error('controller did not verify the organization repository')
                metadata['organization_repository_verified'] = True
                if 'env_cache' in self.settings['organization']:
                    metadata['environment_cache'] = True
                if grant['backend'] == 'slurm':
                    if allocation.get('organization_slurm_verified') is not True:
                        raise protocol.Error('controller did not authorize organization Slurm execution')
                    metadata['organization_slurm_verified'] = True
                elif 'env_root' in self.settings['organization']:
                    metadata['repository_environment'] = True
            metadata['task_attempt'] = grant['task_attempt']
            metadata['env'] = clean
            with self.mutex:
                row = self.ledger.reserve(grant, metadata)
                if row['state'] == 'reserved':
                    self.launch(row)
            return {'allocation_id': grant['allocation_id']}
        if method == 'poll':
            row = self._owned(params)
            offset = params.get('offset', 0)
            if type(offset) is not int or offset < 0:
                raise protocol.Error('invalid log offset')
            logfile = self.workroot / (row['id']+'.log')
            text = b''
            if logfile.exists():
                with logfile.open('rb') as stream:
                    stream.seek(offset)
                    text = stream.read(128 << 10)
            return {'state': row['state'], 'result': row['result'], 'log': text.decode('utf-8', 'replace'),
                    'offset': offset+len(text), 'part_id': row['metadata']['part']['id']}
        if method == 'cancel':
            row = self._owned(params)
            self.ledger.cancel(row['id'])
            return {'accepted': True}
        raise protocol.Error('unknown local executor operation')

    def launch(self, row):
        with self.mutex:
            child = self.children.get(row['id'])
            if child is not None and child.poll() is None:
                return
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            call = 'import sys;sys.path.insert(0,sys.argv.pop(1));from parallel.executor import worker;sys.exit(worker(*sys.argv[1:]))'
            env = {k: os.environ[k] for k in ('HOME', 'PATH', *part.USER_BUS) if k in os.environ}
            with (self.workroot / (row['id']+'.log')).open('ab', buffering=0) as log:
                self.children[row['id']] = self.spawn([sys.executable, '-I', '-c', call, root, self.etc, row['id']],
                                                      stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=True)

    def stop_scope(self, row, *, stop=False):
        rec = part._read_json(os.path.join(row['record'], 'record.json')) or {}
        unit = rec.get('unit')
        organization_mode = organization.standalone_executor(self.settings)
        if not unit:
            env = row['metadata'].get('env', {})
            p = row['metadata'].get('part', {})
            if not env:
                return True
            attempt = row['grant'].get('task_attempt', 1)
            unit = part.unit_name(row['grant']['runner_tag'], p['id'], f'{env["GITHUB_RUN_ID"]}-{env["GITHUB_RUN_ATTEMPT"]}-{attempt}',
                                  organization=organization_mode)
        prefix = 'civ2-part-' if organization_mode else 'ci-part-'
        if not part.SCOPE.fullmatch(unit) or not unit.startswith(prefix):
            raise protocol.Error('invalid recorded task scope')
        if stop:
            self.run(['systemctl', '--user', '--no-block', 'stop', unit], capture_output=True, timeout=10)
        status = self.run(['systemctl', '--user', 'show', unit, '-p', 'LoadState', '-p', 'ActiveState', '-p', 'ControlGroup'],
                          capture_output=True, text=True, timeout=30)
        values = dict(line.split('=', 1) for line in (status.stdout or '').splitlines() if '=' in line)
        if values.get('LoadState') == 'not-found':
            return True
        if values.get('ActiveState') not in ('inactive', 'failed'):
            return False
        cg = values.get('ControlGroup')
        if cg:
            for base in ('/sys/fs/cgroup', '/sys/fs/cgroup/systemd'):
                procs = Path(base) / cg.lstrip('/') / 'cgroup.procs'
                try:
                    if procs.read_text().strip():
                        return False
                except FileNotFoundError:
                    pass
        return True

    def remote_clean(self, row):
        if row['grant']['backend'] != 'slurm':
            return True
        block = part._read_json(os.path.join(row['record'], 'slurm.json')) or {}
        opened = block.get('open', {})
        if not opened:
            return True
        plan = part._read_json(os.path.join(row['record'], 'slurm-plan.json')) or {}
        try:
            site = slurmsite.load_file(plan['site_file'])
            for alias in list(opened):
                gate = slurmpart.Gate(self.etc, self.inv.host['data'], site, alias, run=self.run)
                if slurmpart.cancel_job(gate, plan['key']) is not None:
                    del opened[alias]
            part._write_json(os.path.join(row['record'], 'slurm.json'), block)
        except (KeyError, OSError, slurmsite.SiteError, slurmpart.Unreachable, slurmpart.Refused):
            return False
        return not opened

    def poll_local(self):
        with self.mutex:
            for aid, proc in list(self.children.items()):
                if proc.poll() is not None:
                    del self.children[aid]
        for row in self.ledger.active():
            try:
                if row['pid'] is None and row['metadata'].get('env'):
                    if not row['cancelled']:
                        self.launch(row)
                        continue
                record = row['record']
                ready = part._read_json(os.path.join(record, 'allocation-ready.json')) if record else None
                if ready and ready.get('allocation_id') == row['id']:
                    self.ledger.allocated(row['id'])
                alive = row['pid'] is not None and slots.alive(row['pid'], row['started'])
                if row['cancelled']:
                    if record and os.path.isdir(record):
                        from ci_runners import outcome
                        outcome.mark_cancelled(record, 'central cancellation')
                    self.stop_scope(row, stop=True)
                    if alive:
                        os.kill(row['pid'], signal.SIGTERM)
                        continue
                if alive:
                    continue
                ex = part._read_json(os.path.join(record, 'exit.json')) if record else None
                if not self.stop_scope(row, stop=bool(ex) or bool(row['cancelled'])) or not self.remote_clean(row):
                    continue
                out = part._read_json(os.path.join(record, 'outcome.json')) if record else None
                if out is None and record:
                    rec = part._read_json(os.path.join(record, 'record.json'))
                    if rec is not None:
                        out = part.finish(record, rec, ex, stopped=bool(row['cancelled']))
                        part._publish(record, out, rec['id'], row['metadata']['env'])
                state = {'pass': 'success', 'red': 'failure', 'stopped': 'cancelled', 'oom': 'failure'}.get((out or {}).get('outcome'), 'error')
                result = {'state': 'cancelled' if row['cancelled'] else state,
                          'reason': (out or {}).get('reason', 'executor ended before writing a result'),
                          'retryable': state == 'error', 'outcome': out or {}}
                self.ledger.release(row['id'], result, cleaned=True, verify=lambda: self.stop_scope(row))
            except (OSError, ValueError, subprocess.SubprocessError):
                continue  # unknown cleanup evidence keeps the reservation

    def ready_projects(self):
        ready = {}
        memory = registry.memory_bytes(self.registration['memory_max'])
        for path in sorted(Path(self.etc, 'projects').glob('*.toml')):
            try:
                project = registry.load_file(self.etc, path.stem, self.inv.this)
            except registry.RegistryError:
                continue
            if project.env and (Path(project.env) / 'bin/python').is_file():
                ready[path.stem] = {'repository': project.repository,
                                    'memory_bytes': min(memory, registry.memory_bytes(project.memory_max)),
                                    'max_minutes': project.max_minutes, 'devices': list(project.devices)}
        return ready

    def sync(self):
        extra = {}
        if self.registration is not None:
            if not self.registered:
                self.remote.call('register', **self.registration)
                self.registered = True
            if 'organization' in self.settings:
                extra['ready_projects'] = {}
                if 'env_cache' in self.settings['organization']:
                    extra.update(organization_ready=False, environment_cache=True)
                elif 'env_root' in self.settings['organization']:
                    extra.update(organization_ready=False,
                                 organization_repositories=organization.ready_repositories(self.settings))
                else:
                    extra['organization_ready'] = organization.environment_ready(self.settings)
            else:
                extra['ready_projects'] = self.ready_projects()
        observed = pool.runner_procs(str(Path(self.inv.host['data'])/'actions-runner'))
        alive, busy = observed if observed is not None else (set(), self.names)
        drained = bool(drain.refusal(drain.read(str(self.inv.host['data'])), self.inv.host, 'executor'))
        cards = self.inv.host.get('gpu_uuids', [])
        ready = gpu_resources.available(cards, self.inv.host['max_temp_c'], run=self.run)
        try:
            allocations = self.remote.call('heartbeat', runners=[dict(tag=n, online=n in alive, busy=n in busy) for n in sorted(self.names)],
                                           drained=drained, allocation_schema=3, gpu_free_mib=ready, **extra)
        except Exception:
            self.registered = False
            raise
        for a in allocations:
            if a['cancelled'] or a['runner_done']:
                state = 'cancelled' if a['cancelled'] else 'error'
                self.ledger.cancel_or_close_unstarted(a['grant'], {'state': state, 'reason': 'execution job ended before starting',
                                                                 'retryable': state == 'error'})
        for report in self.ledger.reports():
            self.remote.call('event', event_id=report['id'], allocation_id=report['allocation'], kind=report['kind'], result=report['result'])
            self.ledger.delivered(report['id'])


def worker(etc: str, aid: str):
    inv = __import__('ci_runners.inventory', fromlist=['load']).load(Path(etc, 'inventory.toml').read_text())
    ledger = executor_store.Ledger(str(paths(inv)[0]), groups(inv), inv.host.get('gpu_uuids', []))
    try:
        row = ledger.get(aid)
        if row is None or row['cancelled']:
            return 2
        record = record_path(inv, row['metadata'])
        if not ledger.start_worker(aid, os.getpid(), slots._start_of(os.getpid()), record):
            return 0
        allocation = {**row['metadata'], 'grant': row['grant']}
        try:
            code, _ = task_execution.execute(inv, etc, row['metadata']['env'], allocation, row['cpus'])
            return code
        except (OSError, ValueError, part.Refused):
            return 2
    finally:
        ledger.close()


def serve(inv, etc):
    settings = transport.settings(etc)
    cfg = settings['executor']
    registration = protocol.load_device_registration(cfg['registration']) if 'registration' in cfg else None
    if registration is not None and (
            registration['device'] != inv.this or registration['runner_count'] != len(groups(inv))
            or registration['gpu_uuids'] != inv.host.get('gpu_uuids', [])
            or registration['memory_max'] != inv.host['memory_max']):
        raise protocol.Error('device registration does not match its local inventory')
    remote = transport.Client(url=cfg['url'], token_file=cfg['token_file'], ca_file=cfg.get('ca_file'),
                              device=registration['device'] if registration is not None else '')
    ledger = executor_store.Ledger(str(paths(inv)[0]), groups(inv), inv.host.get('gpu_uuids', []))
    app = ExecutorService(inv, etc, ledger, remote, registration=registration, settings=settings)
    server = transport.unix_server(str(paths(inv)[1]), app.call, uids=cfg.get('runner_uids'))
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection_health = health.ConnectionHealth(layout.control_root(inv)/'state/executor-health.json')
    def synchronize():
        while not stop.is_set():
            try:
                app.sync()
            except Exception as error:
                connection_health.record(error, pending_reports=len(ledger.reports()))
            else:
                connection_health.record(pending_reports=len(ledger.reports()))
            stop.wait(connection_health.retry_delay())
    sync_thread = threading.Thread(target=synchronize, daemon=True)
    sync_thread.start()
    try:
        while not stop.wait(1):
            app.poll_local()
    finally:
        server.shutdown()
        server.server_close()
        stop.set()
        sync_thread.join(timeout=35)
        if not sync_thread.is_alive():
            ledger.close()
    return 0
