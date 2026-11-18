"""The plan service: admission, durable allocation and the dispatcher loop."""
from __future__ import annotations

import dataclasses
import fcntl
import os
import signal
import threading
import time
from pathlib import Path

from ci_runners import central, control, dispatch, inventory, registry, scheduler_store, setup


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
        self.pools, self.runners = registration(inv)
        store.configure(self.pools, self.runners)

    def prepare(self, requested):
        project = registry.load_file(self.etc, requested['project'], self.inv.this)
        if requested['repository'].lower() != project.repository:
            raise central.Error('project belongs to another repository')
        pol = dispatch.load_policy(self.etc)
        source = self.api.source(requested, pol)
        if not project.execution_ref:
            raise central.Error('project needs a trusted execution_ref')
        source.update(execution_ref=project.execution_ref, execution_workflow=project.execution_workflow,
                      slurm_target=project.slurm_target)
        return source, project, pol

    def submit(self, requested, raw_plan, *, plan_only=False):
        source, project, pol = self.prepare(requested)
        plan = central.load_plan(raw_plan, max_minutes=project.max_minutes)
        files = plan['files']
        if files and not plan_only:
            artifact = source.get('files_artifact_id')
            if not central.positive(artifact):
                raise central.Error('plan files must be uploaded before submission')
            info = self.api.api('GET', f'/repos/{project.repository}/actions/artifacts/{artifact}')
            if (info.get('expired') is not False or info.get('workflow_run', {}).get('id') != source['run_id']
                    or info.get('name') != f'ci-plan-files-{source["run_attempt"]}'):
                raise central.Error('plan files do not belong to this source attempt')
        eligible = {}
        for p in plan['parts']:
            choices = []
            for device in project.devices:
                if device not in self.pools or p['device'] and device != p['device'] or p['cores']//4 > self.pools[device]:
                    continue
                if device == 'slurm' and ('slurm' not in pol.repos[project.repository].classes or not project.slurm_target):
                    continue
                hosts = {r['host'] for r in self.runners if r['pool'] == device}
                for host in hosts:
                    try:
                        local = registry.load_file(self.etc, project.name, host)
                    except registry.RegistryError:
                        break
                    if p['cores'] * (1 << 30) > registry.memory_bytes(local.memory_max):
                        break
                else:
                    choices.append(device)
            if not choices:
                raise central.Error(f'{p["id"]}: no configured, admitted device satisfies CPU/RAM demand')
            eligible[p['id']] = choices
        if plan_only:
            if not source['context'].endswith('/plan-only'):
                raise central.Error('plan-only validation needs its own status context')
            self.store.notice(source, 'success', 'CPU plan validated; no tests dispatched')
            return {'batch_id': '', 'submission_state': 'validated'}
        batch = self.store.submit(source, plan, eligible, max_parallel=project.max_parallel, retries=project.retries)
        return {'batch_id': batch, 'submission_state': 'accepted'}

    def call(self, method, params, identity):
        if identity == 'local':
            if method == 'prepare':
                source, _, _ = self.prepare(params['source'])
                self.store.notice(source, 'pending', 'Building CPU plan')
                return {'sha': source['sha'], 'project': source['project']}
            if method == 'failure':
                source, _, _ = self.prepare(params['source'])
                self.store.notice(source, 'failure', 'CPU plan construction or submission failed')
                return {'accepted': True}
            if method == 'submit':
                return self.submit(params['source'], params['plan'], plan_only=params.get('plan_only', False))
            if method == 'batch':
                return self.store.batch(params['batch_id'])
            if method == 'capacity':
                return self.store.capacity()
            # Source cancellation comes from verified GitHub state, not an arbitrary local caller.
            raise central.Error('unknown plan control operation')
        if method == 'heartbeat':
            self.store.heartbeat(identity, params['runners'], drained=params.get('drained', False))
            return [{k: a[k] for k in ('id', 'grant', 'cancelled', 'runner_done')}
                    for a in self.store.allocations(host=identity)]
        if method == 'event':
            self.store.event(identity, params['event_id'], params['allocation_id'], params['kind'], params.get('result'))
            return {'accepted': True}
        if method == 'claim':
            grant = central.validate_allocation(params['grant'])
            a = self.store.allocation(grant['allocation_id'])
            if grant != a['grant'] or a['host'] != identity:
                raise central.Error('resource JSON does not match the central authorization')
            admission = a['source']['admission']
            pol = dispatch.load_policy(self.etc)
            from ci_runners import policy
            env = dict(admission['env'], CI_RUNNER_CLASS='slurm' if a['pool'] == 'slurm' else 'plan')
            if not policy.decide(pol, env, admission['event']).accepted:
                raise central.Error('original source is no longer admitted')
            self.api.verify_claim(a, params['run_id'], params['run_attempt'])
            return self.store.claim(a['id'], identity, params['runner'], params['repository'], params['run_id'], params['run_attempt'])
        raise central.Error('unknown executor control operation')


def serve(inv, etc):
    cfg = control.settings(etc)
    p = cfg['plan']
    # The service's write credential must not be readable by code in a plan runner.
    uids = p['runner_uids']
    if not uids or os.getuid() in uids:
        raise central.Error('run ci-plan.service as a separate service account from the plan runners')
    Path(p['database']).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = os.open(p['database']+'.service.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        store = scheduler_store.Store(p['database'])
        api = dispatch.GitHub(p['github_token_file'], p['dispatch_actor'])
        app = Scheduler(inv, etc, store, api)
        dispatcher = dispatch.Dispatcher(store, api)
        servers = [control.unix_server(p['socket'], app.call, uids=uids),
                   control.https_server(p, cfg['hosts'], app.call)]
        stopped = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stopped.set())
        threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
        for thread in threads:
            thread.start()
        ready, next_reconcile = False, 0.
        try:
            while not stopped.wait(.25):
                now = time.monotonic()
                if now >= next_reconcile:
                    try:
                        dispatcher.reconcile()
                    except Exception:
                        next_reconcile = now + 30
                    else:
                        ready, next_reconcile = True, now + 60
                if ready:
                    app.store.reserve()
                    dispatcher.send_one()
        finally:
            for server in servers:
                server.shutdown()
                server.server_close()
            store.close()
    finally:
        os.close(lock)
    return 0
