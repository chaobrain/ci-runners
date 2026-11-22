"""The plan service: admission, durable allocation and the workflow coordination loop."""
from __future__ import annotations

import dataclasses
import fcntl
import logging
import math
import os
import signal
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from parallel import health, layout, organization
from ci_runners import __version__

from ci_runners import github, inventory, policy, registry, setup
from parallel import deployment_files, protocol, transport, github_coordinator, scheduler_store


REPOSITORY_SYNC_INTERVAL = 10 * 60


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
    device_admission_file: str | None = None
    repository_members = False
    organization_config = None

    def __init__(self, inv, etc, store, api, *, device_admission_file=None, repository_members=False,
                 organization_config=None):
        self.inv, self.etc, self.store, self.api = inv, etc, store, api
        self.ready = False
        self.device_admission_file = device_admission_file
        self.repository_members = repository_members
        self.organization_config = organization_config
        if organization_config is not None:
            organization.validate_settings(organization_config, controller=True)
            if not self.device_admission_file:
                raise protocol.Error('organization scheduling needs a device admission file')
        self.pools, self.runners = registration(inv)
        self.gpu_inventory = {r['pool']: inv.hosts[r['host']]['gpu_uuids'] for r in self.runners
                              if r['pool'] != 'slurm' and inv.hosts[r['host']].get('gpu_uuids')}
        if self.device_admission_file:
            self.pools = {name: total for name, total in self.pools.items() if name == 'slurm'}
            self.runners = [runner for runner in self.runners if runner['pool'] == 'slurm']
            self.gpu_inventory = {}
            pools, runners, cards = dict(self.pools), list(self.runners), {}
            for profile in store.registrations():
                device = profile['device']
                pools[device] = profile['runner_count']
                runners.extend({'tag': tag, 'pool': device, 'host': device}
                               for tag in protocol.runner_names(device, profile['runner_count']))
                if profile['gpu_uuids']:
                    cards[device] = profile['gpu_uuids']
            store.configure(pools, runners, cards)
        else:
            store.configure(self.pools, self.runners, self.gpu_inventory)

    def publish_devices(self, authorized):
        path = Path(self.inv.host['data']) / 'output/devices.json'
        data = protocol.dumps(self.store.devices(authorized)) + '\n'
        try:
            unchanged = path.read_text() == data
        except FileNotFoundError:
            unchanged = False
        if not unchanged:
            deployment_files.atomic_write(path, data, 0o600)

    def publish_repositories(self, stopped=None):
        name = self.organization_config['name']
        fetched_at = datetime.now(UTC).isoformat(timespec='seconds')
        records = self.api.repositories(name)
        if stopped is not None and stopped.is_set():
            return
        path = Path(self.inv.host['data']) / 'output/repositories.json'
        document = {'schema': 1, 'organization': name, 'fetched_at': fetched_at, 'repositories': records}
        deployment_files.write_json(path, document)

    def sync_repositories(self, stopped):
        log = logging.getLogger(__name__)
        while not stopped.is_set():
            try:
                self.publish_repositories(stopped)
            except (github.ApiError, OSError, ValueError) as error:
                log.warning('cannot refresh the organization repository catalog (%s)', type(error).__name__)
            if stopped.wait(REPOSITORY_SYNC_INTERVAL):
                return

    def slurm_binding(self, source):
        path = self.organization_config.get('slurm_allowlist_file')
        if path and 'slurm' in self.pools:
            try:
                entries = organization.read_slurm_allowlist(path, self.organization_config['name'])
            except (OSError, ValueError):
                return None
            return organization.slurm_binding(entries, source)
        return None

    def slurm_permitted(self, source, task):
        if not source or task.get('gpu') or not source.get('slurm_project') or not source.get('slurm_target'):
            return False
        binding = self.slurm_binding(source)
        if (binding is None or binding['slurm_project'] != source['slurm_project']
                or binding['slurm_target'] != source['slurm_target']):
            return False
        target = organization.read_slurm_target(self.etc, binding)
        return (target is not None and task['cores'] <= target.cpus(0)
                and task['minutes'] <= target.max_minutes
                and task['cores'] * (1 << 30) <= registry.memory_bytes(target.t['mem_per_node']))

    def prepare(self, requested):
        if self.organization_config is not None:
            organization_name = self.organization_config['name']
            fields = ('repository', 'sha', 'run_id', 'run_attempt', 'context', 'concurrency_key',
                      'cancel_in_progress', 'files_artifact_id')
            request = {key: requested[key] for key in fields if key in requested}
            source = self.api.source({**request, 'project': 'organization'}, None, organization=organization_name)
            repository_id = source['admission']['event']['repository']['id']
            if 'repository_id' in requested and requested['repository_id'] != repository_id:
                raise protocol.Error('repository ID changed after plan preparation')
            source.update(project=f'repo-{repository_id}', repository_id=repository_id,
                          organization_profile=self.organization_config['profile'], slurm_target='')
            binding = self.slurm_binding(source)
            if binding:
                source.update(slurm_project=binding['slurm_project'], slurm_target=binding['slurm_target'])
            project = organization.project({'organization': self.organization_config}, self.inv, source)
            return source, project, None
        project = registry.load_file(self.etc, requested['project'], self.inv.this)
        if requested['repository'].lower() != project.repository:
            raise protocol.Error('project belongs to another repository')
        pol = github_coordinator.load_policy(self.etc)
        if self.repository_members:
            source = self.api.source(requested, pol, repository_members=True)
        else:
            source = self.api.source(requested, pol)
        source['slurm_target'] = project.slurm_target
        return source, project, pol

    def bind_environment(self, source, *, create=False):
        """Pin trusted inputs independently of anything supplied by the plan process."""
        if self.organization_config is None:
            return
        recipe = self.store.prepared_environment(source)
        if 'env_cache' not in self.organization_config:
            if recipe is not None:
                raise protocol.Error('environment cache mode changed after plan preparation')
            return
        if recipe is None:
            if not create:
                raise protocol.Error('cache mode requires a prepared test plan')
            from ci_runners import environment_cache
            repository = source['repository']
            # The repository commits API defaults to the default branch. Pin this
            # head before fetching any recipe input, including during PR runs.
            commits = self.api.api('GET', f'/repos/{repository}/commits?per_page=1')
            if (not isinstance(commits, list) or not commits or not isinstance(commits[0], dict)
                    or not isinstance(commits[0].get('sha'), str) or not protocol.SHA.fullmatch(commits[0]['sha'])):
                raise protocol.Error('cannot resolve the trusted default branch environment revision')
            try:
                candidate = environment_cache.resolve(self.api.api, repository, commits[0]['sha'])
            except (environment_cache.ERROR, github.ApiError) as exc:
                raise protocol.Error(f'cannot prepare {environment_cache.MANIFEST}: {str(exc)[:512]}') from exc
            recipe = self.store.prepared_environment(source, candidate)
        source['environment_recipe'] = recipe

    def submit(self, requested, raw_plan, *, plan_only=False):
        source, project, pol = self.prepare(requested)
        self.bind_environment(source)
        if 'environment_recipe' in source and requested.get('environment_digest') != protocol.fingerprint(source['environment_recipe']):
            raise protocol.Error('submitted plan does not match its prepared environment')
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
        organization_submit = self.organization_config is not None and not plan_only
        eligible = {task['id']: self.eligible_task_devices(task, project, pol, source, allow_empty=organization_submit)
                    for task in plan['parts']}
        if plan_only:
            if not source['context'].endswith('/plan-only'):
                raise protocol.Error('plan-only validation needs its own status context')
            self.store.notice(source, 'success', 'Test plan validated')
            return {'batch_id': '', 'submission_state': 'validated', 'task_matrix': '[]', 'task_count': 0,
                    'cpu_task_matrix': '[]', 'cpu_task_count': 0, 'gpu_task_matrix': '[]', 'gpu_task_count': 0, 'max_parallel': 1}
        batch = self.store.submit(source, plan, eligible, max_parallel=project.max_parallel, retries=project.retries,
                                  gpu_mem_mib=project.gpu_mem_mib, organization=organization_submit)
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

    def eligible_task_devices(self, task, project, current_policy, source=None, *, allow_empty=False):
        eligible_devices = []
        requested_groups = task['cores'] // protocol.GROUP_CPUS
        required_memory = task['cores'] * (1 << 30)
        for device in project.devices:
            if task['device'] and device != task['device']:
                continue
            if self.device_admission_file and device != 'slurm':
                if requested_groups <= protocol.DEVICE_GROUPS[device] and (not task.get('gpu') or device in protocol.GPU_QUOTAS):
                    eligible_devices.append(device)
                continue
            if device not in self.pools or requested_groups > self.pools[device]:
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
        if (self.organization_config is not None and task['device'] in ('', 'slurm')
                and requested_groups <= self.pools.get('slurm', 0) and self.slurm_permitted(source, task)):
            eligible_devices.append('slurm')
        if not eligible_devices and not allow_empty:
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
        source_env = dict(admission['env'], CI_RUNNER_CLASS='slurm' if allocation['pool'] == 'slurm' else 'plan')
        if self.organization_config is not None:
            organization.project({'organization': self.organization_config}, self.inv, allocation['source'])
            options = {'organization': self.organization_config['name']}
            if allocation['pool'] == 'slurm':
                if not self.slurm_permitted(allocation['source'], allocation['part']):
                    raise protocol.Error('organization Slurm binding is no longer admitted')
                options['organization_slurm_repository_id'] = allocation['source']['repository_id']
            decision = self.api.admit(None, source_env, admission['event'], **options)
        else:
            current_policy = github_coordinator.load_policy(self.etc)
            decision = (self.api.admit(current_policy, source_env, admission['event'], repository_members=True)
                        if self.repository_members else policy.decide(current_policy, source_env, admission['event']))
        if not decision.accepted:
            raise protocol.Error('original source is no longer admitted')
        job_id = self.api.verify_claim(allocation, params['run_id'], params['run_attempt'])
        if (self.organization_config is not None and allocation['pool'] == 'slurm'
                and not self.slurm_permitted(allocation['source'], allocation['part'])):
            raise protocol.Error('organization Slurm binding is no longer admitted')
        claim_options = {}
        if self.organization_config is not None:
            cached = params.get('environment_cache') is True
            confirmed = params.get('repository_environment') is True and allocation['pool'] != 'slurm'
            if (cached or confirmed) and identity not in transport.device_tokens(self.device_admission_file):
                raise protocol.Error('organization executor is no longer admitted')
            claim_options = dict(require_repository_environment='env_root' in self.organization_config,
                                 repository_environment_confirmed=confirmed,
                                 require_environment_cache='env_cache' in self.organization_config,
                                 environment_cache_confirmed=cached)
        claimed = self.store.claim(allocation['id'], identity, params['runner'], params['repository'],
                                   params['run_id'], params['run_attempt'], job_id=job_id, **claim_options)
        if self.organization_config is not None:
            claimed = {**claimed, 'organization_repository_verified': True}
            if allocation['pool'] == 'slurm':
                claimed['organization_slurm_verified'] = True
            return claimed
        if self.repository_members:
            return {**claimed, 'repository_members_verified': True}
        return claimed

    def call(self, method, params, identity):
        if method == 'health':
            return {'version': __version__, 'ready': self.ready}
        if identity == 'local':
            if method == 'admit_plan' and (self.organization_config is not None or self.repository_members):
                if self.organization_config is not None:
                    decision = self.api.admit(None, params['env'], params['event'],
                                              organization=self.organization_config['name'])
                else:
                    pol = github_coordinator.load_policy(self.etc)
                    decision = self.api.admit(pol, params['env'], params['event'], repository_members=True)
                return {'accepted': decision.accepted, 'reason': decision.reason}
            if method == 'prepare':
                source, _, _ = self.prepare(params['source'])
                self.store.notice(source, 'pending', 'Building test plan')
                try:
                    self.bind_environment(source, create=True)
                except Exception:
                    self.store.notice(source, 'error', 'Failed to prepare the trusted environment specification')
                    raise
                keys = ('sha', 'project')
                if self.organization_config is not None:
                    keys += ('repository_id', 'organization_profile')
                prepared = {key: source[key] for key in keys}
                if self.organization_config is not None and 'env_root' in self.organization_config:
                    prepared['repository_environment'] = True
                if 'environment_recipe' in source:
                    prepared['environment_digest'] = protocol.fingerprint(source['environment_recipe'])
                return prepared
            if method == 'plan_environment':
                source, _, _ = self.prepare(params['source'])
                self.bind_environment(source)
                recipe = source.get('environment_recipe')
                if recipe is None or protocol.fingerprint(recipe) != params.get('digest'):
                    raise protocol.Error('the plan does not match its pinned environment')
                return recipe
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
        if method == 'register' and self.device_admission_file:
            self.store.register_device(identity, params)
            return {'registered': True}
        if method == 'heartbeat':
            gpu_free_mib = params.get('gpu_free_mib')
            version = params.get('allocation_schema', 1)
            gpu_capable = type(version) is int and version == 2
            if gpu_free_mib is not None and not gpu_capable:
                raise protocol.Error('GPU observations require allocation schema 2')
            readiness = {'ready_projects': params.get('ready_projects')}
            if self.organization_config is not None:
                if type(params.get('organization_ready')) is not bool:
                    raise protocol.Error('report the organization environment with device heartbeats')
                readiness = {'organization_ready': params['organization_ready']}
                if 'environment_cache' in params:
                    readiness['environment_cache'] = params['environment_cache']
                if 'organization_repositories' in params:
                    names = params['organization_repositories']
                    prefix = self.organization_config['name'].lower() + '/'
                    if (params['organization_ready'] or not isinstance(names, list)
                            or any(not isinstance(name, str) or name != name.lower() or not protocol.REPO.fullmatch(name)
                                   or not name.startswith(prefix) or name.split('/')[1] in ('.', '..') for name in names)
                            or len(names) != len(set(names))):
                        raise protocol.Error('invalid organization repository environment report')
                    readiness['organization_repositories'] = names
            elif self.device_admission_file and not isinstance(readiness['ready_projects'], dict):
                raise protocol.Error('report ready projects with device heartbeats')
            self.store.heartbeat(identity, params['runners'], drained=params.get('drained', False),
                                 gpu_free_mib=gpu_free_mib, **readiness)
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
        app = Scheduler(inv, etc, store, api, device_admission_file=p.get('device_admission_file'),
                        repository_members=cfg.get('repository_members', False),
                        organization_config=cfg.get('organization'))
        coordinator = github_coordinator.WorkflowCoordinator(store, api)
        servers = [transport.unix_server(p['socket'], app.call, uids=uids),
                   transport.https_server(p, cfg.get('hosts', {}), app.call)]
        stopped = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stopped.set())
        threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
        for thread in threads:
            thread.start()
        if app.organization_config is not None:
            catalog = threading.Thread(target=app.sync_repositories, args=(stopped,), daemon=True,
                                       name='organization-repositories')
            catalog.start()
        ready, next_reconcile, next_publication = False, 0., 0.
        allowed_devices = None
        publication_error = False
        admission_error = False
        log = logging.getLogger(__name__)
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
                if app.device_admission_file and now >= next_publication:
                    next_publication = now + 1
                    try:
                        allowed_devices = set(transport.device_tokens(app.device_admission_file))
                    except protocol.Error:
                        allowed_devices = set()
                        if not admission_error:
                            log.warning('cannot read the device admission file. New allocations are paused')
                        admission_error = True
                    else:
                        admission_error = False
                    try:
                        app.publish_devices(allowed_devices)
                    except (OSError, ValueError) as error:
                        if not publication_error:
                            log.warning('cannot publish the device registry (%s)', type(error).__name__)
                        publication_error = True
                    else:
                        publication_error = False
                if ready:
                    if app.organization_config is not None:
                        app.store.reserve(allowed_devices=allowed_devices, organization=True,
                                          slurm_permitted=app.slurm_permitted,
                                          require_repository_environment='env_root' in app.organization_config,
                                          require_environment_cache='env_cache' in app.organization_config)
                    else:
                        app.store.reserve(allowed_devices=allowed_devices)
                    coordinator.send_one()
        finally:
            stopped.set()
            for server in servers:
                server.shutdown()
                server.server_close()
            store.close()
    finally:
        os.close(lock)
    return 0
