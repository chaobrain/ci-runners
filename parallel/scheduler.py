"""The plan service: admission, durable allocation and the workflow coordination loop."""
from __future__ import annotations

import dataclasses
import fcntl
import math
import os
import signal
import threading
import time
from pathlib import Path
from parallel import health, layout
from ci_runners import __version__

from ci_runners import inventory, policy, registry, setup
from parallel import protocol, transport, github_coordinator, scheduler_store


def registration(inv):
    pools, runners = {}, []
    for host, h in inv.hosts.items():
        local = dataclasses.replace(inv, this=host)
        names = setup._runners(local)
        keys = setup.runner_pools(local)
        for (tag, _, cls, _, _), key in zip(names, keys, strict=True):
            if cls != 'executor':
                continue
            device = h['pools'][key]['device']
            pools[device] = pools.get(device, 0) + 1
            runners.append({'tag': tag, 'pool': device, 'host': host})
    return pools, runners


class Scheduler:
    def __init__(self, inv, etc, store, api):
        self.inv, self.etc, self.store, self.api = inv, etc, store, api
        self.ready = False
        self.pools, self.runners = registration(inv)
        self.gpu_inventory = {r['pool']: inv.hosts[r['host']]['gpu_uuids'] for r in self.runners
                              if r['pool'] != 'slurm' and inv.hosts[r['host']].get('gpu_uuids')}
        store.configure(self.pools, self.runners, self.gpu_inventory)

    def prepare(self, requested):
        project = registry.load_file(self.etc, requested['project'], self.inv.this)
        if requested['repository'].lower() != project.repository:
            raise protocol.Error('project belongs to another repository')
        pol = github_coordinator.load_policy(self.etc)
        source = self.api.source(requested, pol)
        source['slurm_target'] = project.slurm_target
        return source, project, pol

    def submit(self, requested, raw_plan, *, plan_only=False):
        source, project, pol = self.prepare(requested)
        plan = protocol.load_plan(raw_plan, max_minutes=project.max_minutes)
        files = plan['files']
        if files and not plan_only:
            artifact = source.get('files_artifact_id')
            if not protocol.positive(artifact):
                raise protocol.Error('plan files must be uploaded before submission')
            info = self.api.api('GET', f'/repos/{project.repository}/actions/artifacts/{artifact}')
            if (info.get('expired') is not False or info.get('workflow_run', {}).get('id') != source['run_id']
                    or info.get('name') != f'ci-plan-files-{source["run_attempt"]}'):
                raise protocol.Error('plan files do not belong to this source attempt')
        eligible = {task['id']: self.eligible_task_devices(task, project, pol) for task in plan['parts']}
        if plan_only:
            if not source['context'].endswith('/plan-only'):
                raise protocol.Error('plan-only validation needs its own status context')
            self.store.notice(source, 'success', 'Test plan validated')
            return {'batch_id': '', 'submission_state': 'validated', 'task_matrix': '[]', 'task_count': 0,
                    'cpu_task_matrix': '[]', 'cpu_task_count': 0, 'gpu_task_matrix': '[]', 'gpu_task_count': 0, 'max_parallel': 1}
        batch = self.store.submit(source, plan, eligible, max_parallel=project.max_parallel, retries=project.retries,
                                  gpu_mem_mib=project.gpu_mem_mib)
        batch_record = self.store.batch(batch)
        task_matrix, cpu_matrix, gpu_matrix = [], [], []
        for task in batch_record['tasks']:
            part = task['part']
            entry = {'task_id': task['id'], 'part_id': part['id'],
                     'allocation_timeout_minutes': math.ceil(part['queue_minutes'] + 5)}
            task_matrix.append(entry)
            if part.get('gpu'):
                gpu_matrix.append({**entry, 'gpu_lane': part['device'] or 'auto'})
            else:
                cpu_matrix.append(entry)
        return {'batch_id': batch, 'submission_state': 'accepted', 'task_matrix': protocol.dumps(task_matrix),
                'task_count': len(task_matrix), 'cpu_task_matrix': protocol.dumps(cpu_matrix),
                'cpu_task_count': len(cpu_matrix), 'gpu_task_matrix': protocol.dumps(gpu_matrix),
                'gpu_task_count': len(gpu_matrix), 'max_parallel': batch_record['max_parallel']}

    def eligible_task_devices(self, task, project, current_policy):
        eligible_devices = []
        requested_groups = task['cores'] // protocol.GROUP_CPUS
        required_memory = task['cores'] * (1 << 30)
        for device in project.devices:
            if device not in self.pools or requested_groups > self.pools[device]:
                continue
            if task['device'] and device != task['device']:
                continue
            if task.get('gpu') and device not in self.gpu_inventory:
                continue
            if device == 'slurm' and ('slurm' not in current_policy.repos[project.repository].classes or not project.slurm_target):
                continue
            device_hosts = {runner['host'] for runner in self.runners if runner['pool'] == device}
            try:
                memory_limits = [registry.memory_bytes(registry.load_file(self.etc, project.name, host).memory_max)
                                 for host in device_hosts]
            except registry.RegistryError:
                continue
            if memory_limits and min(memory_limits) >= required_memory:
                eligible_devices.append(device)
        if not eligible_devices:
            raise protocol.Error(f'{task["id"]}: no configured, admitted device satisfies the resource request')
        return eligible_devices

    def checked_batch(self, params):
        batch = self.store.batch(params['batch_id'])
        requested = params['source_identity']
        source = batch['source']
        if requested.get('repository', '').lower() != source['repository'].lower():
            raise protocol.Error('batch belongs to another repository')
        if (requested.get('run_id'), requested.get('run_attempt')) != (source['run_id'], source['run_attempt']):
            raise protocol.Error('batch belongs to another source attempt; re-run all jobs to build a new plan')
        return batch

    def request_allocation(self, params):
        batch = self.checked_batch(params)
        if batch['source_done'] or self.api.run_attempt(batch['source']).get('status') == 'completed':
            raise protocol.Error('source attempt has ended')
        expected_name = protocol.allocation_job_name(params['task_id'], params['task_attempt'])
        job = github_coordinator.find_job(self.api.jobs(batch['source']), expected_name)
        if job is None or job.get('status') != 'in_progress':
            raise protocol.Error('allocation request job is not active')
        task = next((t['part'] for t in batch['tasks'] if t['id'] == params['task_id']), None)
        if task and task.get('gpu') and protocol.gpu_wait_label(task['device']) not in job.get('labels', []):
            raise protocol.Error('GPU request job is on another allocation lane')
        return self.store.request_task_allocation(batch['id'], params['task_id'], params['task_attempt'])

    def claim_allocation(self, params, identity):
        if 'grant' in params:
            grant = protocol.validate_allocation(params['grant'])
            allocation = self.store.allocation(grant['allocation_id'])
            if grant != allocation['grant'] or allocation['host'] != identity:
                raise protocol.Error('resource JSON does not match the central authorization')
        else:
            allocation = self.store.allocation_for_runner(identity, params['runner'], params['repository'],
                                                         params['run_id'], params['run_attempt'])
        admission = allocation['source']['admission']
        current_policy = github_coordinator.load_policy(self.etc)
        source_env = dict(admission['env'], CI_RUNNER_CLASS='slurm' if allocation['pool'] == 'slurm' else 'plan')
        if not policy.decide(current_policy, source_env, admission['event']).accepted:
            raise protocol.Error('original source is no longer admitted')
        job_id = self.api.verify_claim(allocation, params['run_id'], params['run_attempt'])
        return self.store.claim(allocation['id'], identity, params['runner'], params['repository'],
                                params['run_id'], params['run_attempt'], job_id=job_id)

    def call(self, method, params, identity):
        if method == 'health':
            return {'version': __version__, 'ready': self.ready}
        if identity == 'local':
            if method == 'prepare':
                source, _, _ = self.prepare(params['source'])
                self.store.notice(source, 'pending', 'Building test plan')
                return {'sha': source['sha'], 'project': source['project']}
            if method == 'failure':
                source, _, _ = self.prepare(params['source'])
                self.store.notice(source, 'failure', 'test plan construction or submission failed')
                return {'accepted': True}
            if method == 'submit':
                return self.submit(params['source'], params['plan'], plan_only=params.get('plan_only', False))
            if method == 'batch':
                return self.checked_batch(params) if 'source_identity' in params else self.store.batch(params['batch_id'])
            if method == 'request_allocation':
                return self.request_allocation(params)
            if method == 'task':
                self.checked_batch(params)
                return self.store.task_status(params['batch_id'], params['task_id'], params.get('task_attempt', 1))
            if method == 'capacity':
                return self.store.capacity()
            # Source cancellation comes from verified GitHub state, not an arbitrary local caller.
            raise protocol.Error('unknown plan control operation')
        if method == 'heartbeat':
            gpu_free_mib = params.get('gpu_free_mib')
            version = params.get('allocation_schema', 1)
            gpu_capable = type(version) is int and version == 2
            if gpu_free_mib is not None and not gpu_capable:
                raise protocol.Error('GPU observations require allocation schema 2')
            self.store.heartbeat(identity, params['runners'], drained=params.get('drained', False), gpu_free_mib=gpu_free_mib)
            return [{k: a[k] for k in ('id', 'grant', 'cancelled', 'runner_done')}
                    for a in self.store.allocations(host=identity) if gpu_capable or a['gpu_uuid'] is None]
        if method == 'event':
            self.store.event(identity, params['event_id'], params['allocation_id'], params['kind'], params.get('result'))
            return {'accepted': True}
        if method == 'claim':
            return self.claim_allocation(params, identity)
        raise protocol.Error('unknown executor control operation')


def serve(inv, etc):
    cfg = transport.settings(etc)
    p = cfg['plan']
    uids = p['runner_uids']
    if not uids:
        raise protocol.Error('plan.runner_uids must name at least one authorized runner UID')
    Path(p['database']).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = os.open(p['database']+'.service.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        store = scheduler_store.Store(p['database'])
        api = github_coordinator.GitHub(p['github_token_file'])
        app = Scheduler(inv, etc, store, api)
        coordinator = github_coordinator.WorkflowCoordinator(store, api)
        servers = [transport.unix_server(p['socket'], app.call, uids=uids),
                   transport.https_server(p, cfg['hosts'], app.call)]
        stopped = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stopped.set())
        threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
        for thread in threads:
            thread.start()
        ready, next_reconcile = False, 0.
        connection_health = health.ConnectionHealth(layout.control_root(inv)/'state/scheduler-health.json')
        try:
            while not stopped.wait(.25):
                now = time.monotonic()
                if now >= next_reconcile:
                    try:
                        coordinator.reconcile()
                    except Exception as error:
                        ready = False
                        next_reconcile = now + 30
                        connection_health.record(error)
                    else:
                        ready, next_reconcile = True, now + 15
                        connection_health.record()
                    app.ready = ready
                if ready:
                    app.store.reserve()
                    coordinator.send_one()
        finally:
            for server in servers:
                server.shutdown()
                server.server_close()
            store.close()
    finally:
        os.close(lock)
    return 0
