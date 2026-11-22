"""Exercise private organization scheduling with SQLite and simulated GitHub."""
import concurrent.futures
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from ci_runners import github, inventory
from parallel import github_coordinator, organization, protocol, scheduler, scheduler_store
from parallel.tests.test_central import plan, source
from parallel.tests.test_central_services import inv_text
from parallel.tests.test_device_registration import profile


def repository(name, identifier, visibility='private'):
    return {'id': identifier, 'full_name': name, 'visibility': visibility,
            'private': visibility == 'private', 'owner': {'login': 'example', 'type': 'Organization'}}


class OrganizationScheduler(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        inv = inventory.load(inv_text(self.root, count=1, cpus='0-3').replace('worker', '3090a'))
        self.store = scheduler_store.Store(':memory:')
        self.addCleanup(self.store.close)
        self.api = github_coordinator.GitHub('fake')
        limits = organization.profile({'max_parallel': 3, 'retries': 0})
        self.app = scheduler.Scheduler(inv, str(self.root), self.store, self.api, device_admission_file='fixture',
                                       organization_config={'name': 'example', 'env': str(self.root / 'env'),
                                                            'profile': limits})

    def test_two_repositories_dispatch_to_a_later_device_using_verified_ids(self):
        repos = {'example/first': repository('example/first', 11), 'example/second': repository('example/second', 22)}
        jobs = {}
        def github_get(method, path):
            self.assertEqual(method, 'GET')
            name = '/'.join(path.split('/')[2:4])
            if path.endswith('/jobs?per_page=100&page=1'):
                return {'jobs': jobs[name]}
            self.assertIn(name, repos)
            if path.count('/') == 3:
                return repos[name]
            self.assertIn('/attempts/1', path)
            return {'run_attempt': 1, 'status': 'in_progress', 'repository': repos[name],
                    'head_repository': repos[name], 'event': 'push', 'head_sha': 'a'*40, 'head_branch': 'main'}
        self.api.api = mock.Mock(side_effect=github_get)

        batches = {}
        for run, name in enumerate(repos, 1):
            requested = {**source(run), 'repository': name, 'project': 'Ignored', 'organization_profile': {'retries': 1}}
            prepared = self.app.call('prepare', {'source': requested}, 'local')
            self.assertEqual(prepared['project'], f'repo-{repos[name]["id"]}')
            self.assertEqual(prepared['organization_profile']['retries'], 0)
            batch_id = self.app.submit({**requested, **prepared}, plan())['batch_id']
            batch = self.store.batch(batch_id)
            task = batch['tasks'][0]
            self.assertEqual(batch['source']['repository_id'], repos[name]['id'])
            self.store.request_task_allocation(batch_id, task['id'], 1)
            batches[batch_id] = (name, run)

        authorized = {'3090b', 'h100'}
        self.assertIsNone(self.store.reserve(allowed_devices=authorized, organization=True))
        self.assertFalse((self.root / 'data/output/repositories.json').exists())
        for device in ('3090b', 'h100'):
            self.app.call('register', profile(device), device)
            runners = [dict(tag=f'{device}-0', online=True, busy=False)]
            self.app.call('heartbeat', {'runners': runners, 'organization_ready': False}, device)
        self.assertIsNone(self.store.reserve(allowed_devices=authorized, organization=True))

        for device in ('3090b', 'h100'):
            runners = [dict(tag=f'{device}-0', online=True, busy=False)]
            self.app.call('heartbeat', {'runners': runners, 'organization_ready': True}, device)
        for unused in range(2):
            allocation = self.store.reserve(allowed_devices=authorized, organization=True)
            self.assertIsNotNone(allocation)
            name, run = batches.pop(allocation['batch_id'])
            runner = allocation['runner']
            jobs[name] = [{'id': 100 + run, 'run_id': run, 'runner_name': runner, 'labels': [runner],
                           'status': 'in_progress',
                           'name': protocol.execution_job_name(allocation['task'], allocation['task_attempt'])}]
            claim = {'grant': allocation['grant'], 'runner': runner, 'repository': name,
                     'run_id': run, 'run_attempt': 1}
            result = self.app.call('claim', claim, allocation['host'])
            self.assertIs(result['organization_repository_verified'], True)
            repos[name] = repository(name, repos[name]['id'], 'public')
            with self.assertRaisesRegex(protocol.Error, 'no longer admitted'):
                self.app.call('claim', claim, allocation['host'])
        self.assertFalse(batches)

    def test_sync_retries_after_a_partial_fetch_and_retains_the_last_full_snapshot(self):
        old = repository('example/old', 1)
        full_page = [repository(f'example/new-{identifier}', identifier) for identifier in range(2, 102)]
        published = repository('example/public', 102, 'public')
        self.api.api = mock.Mock(side_effect=[[old], full_page, github.ApiError('HTTP 503'), [published]])
        path = self.root / 'data/output/repositories.json'
        snapshots = []
        stopped = mock.Mock()
        stopped.is_set.return_value = False

        def wait(seconds):
            self.assertEqual(seconds, 600)
            snapshots.append(json.loads(path.read_text()))
            return len(snapshots) == 3

        stopped.wait.side_effect = wait
        with self.assertLogs(scheduler.__name__, 'WARNING'):
            self.app.sync_repositories(stopped)

        first, after_failure, recovered = snapshots
        self.assertEqual(first, after_failure)
        self.assertEqual(first['repositories'], [{'full_name': 'example/old', 'id': 1, 'visibility': 'private'}])
        self.assertEqual(recovered['repositories'], [{'full_name': 'example/public', 'id': 102, 'visibility': 'public'}])
        self.assertEqual(recovered['organization'], 'example')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class OrganizationSlurm(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.now = 1000.
        self.store = scheduler_store.Store(':memory:', clock=lambda: self.now)
        self.addCleanup(self.store.close)
        self.repo = repository('example/project', 11)
        self.api = github_coordinator.GitHub('unused')
        self.api.api = self.github_get
        self.jobs = []
        self.allowlist = self.root / 'allowlist.json'
        self.binding = dict(repository='example/project', repository_id=11,
                            slurm_project='project-a', slurm_target='cluster1/cpu')
        self.authorize(self.binding)
        self.site = self.root / 'slurm/cluster1.toml'
        self.site.parent.mkdir()
        self.site.write_text((Path(__file__).resolve().parents[2] / 'setup/slurm/site.example.toml').read_text())
        self.site.chmod(0o600)
        inv = inv_text(self.root, count=1, cpus='0-3').replace('worker', '3090a').replace(
            'count = 1 } }', 'count = 1 }, driver = { class = "executor", device = "slurm", count = 4 } }')
        manifest = self.root / 'device-admission.toml'
        manifest.write_text(''.join(f'[devices.{device}]\ntoken_file = "{device}.key"\n'
                                    for device in ('3090a', '3090b', 'h100', 'pro6000')))
        manifest.chmod(0o600)
        self.app = scheduler.Scheduler(inventory.load(inv), str(self.root), self.store, self.api,
                                       device_admission_file=str(manifest), organization_config={
                                           'name': 'example', 'env': str(self.root/'env'),
                                           'profile': organization.profile({}), 'slurm_allowlist_file': str(self.allowlist)})

    def github_get(self, method, path):
        self.assertEqual(method, 'GET')
        if path == '/repos/' + self.repo['full_name']:
            return self.repo
        if path.endswith('/jobs?per_page=100&page=1'):
            return {'jobs': self.jobs}
        self.assertTrue(path.startswith('/repos/'+self.repo['full_name']+'/actions/runs/'))
        self.assertTrue(path.endswith('/attempts/1'))
        return {'run_attempt': 1, 'status': 'in_progress', 'repository': self.repo,
                'head_repository': self.repo, 'event': 'push', 'head_sha': 'a'*40, 'head_branch': 'main'}

    def authorize(self, *entries):
        self.allowlist.write_text(json.dumps({'schema': 1, 'repositories': entries}))
        self.allowlist.chmod(0o600)

    def submit(self, run=1, device='', cores=4, minutes=10, gpu=False, command='true', **source_fields):
        document = plan(cores=cores, device=device)
        document['parts'][0].update(minutes=minutes, gpu=gpu, command=command)
        outcome = self.app.submit({**source(run), **source_fields}, document)
        batch = self.store.batch(outcome['batch_id'])
        self.store.request_task_allocation(batch['id'], batch['tasks'][0]['id'], 1)
        return self.store.batch(batch['id'])

    def host(self, device='3090a', *, ready=True, local_busy=False, slurm_online=True, repositories=None, cache=False):
        rank = {'3090a': 4, '3090b': 3, 'pro6000': 2, 'h100': 1}
        p = rank[device]
        gpu = f'GPU-00000000-0000-0000-0000-{p:012d}'
        self.app.call('register', profile(device, priority=p, gpu_uuids=[gpu]), device)
        runners = [dict(tag=device+'-0', online=True, busy=local_busy)]
        if device == '3090a':
            runners.extend(dict(tag=tag, online=slurm_online, busy=False) for tag in protocol.runner_names('slurm', 4))
        report = dict(runners=runners, organization_ready=ready, allocation_schema=2, gpu_free_mib={gpu: 9000})
        if cache:
            report.update(organization_ready=False, environment_cache=True)
        if repositories is not None:
            report.update(organization_ready=False, organization_repositories=repositories)
        self.app.call('heartbeat', report, device)

    def reserve(self, allowed=('3090a', '3090b', 'pro6000', 'h100')):
        return self.store.reserve(allowed_devices=set(allowed), organization=True, slurm_permitted=self.app.slurm_permitted,
                                  require_repository_environment='env_root' in self.app.organization_config,
                                  require_environment_cache='env_cache' in self.app.organization_config)

    def claim(self, allocation, *, repository_environment=None, environment_cache=None):
        runner, run = allocation['runner'], allocation['run_id']
        self.jobs = [{'id': 101, 'run_id': run, 'runner_name': runner, 'labels': [runner], 'status': 'in_progress',
                      'name': protocol.execution_job_name(allocation['task'], allocation['task_attempt'])}]
        params = dict(runner=runner, repository=allocation['source']['repository'], run_id=run, run_attempt=1,
                      grant=allocation['grant'])
        if environment_cache is not None:
            params['environment_cache'] = environment_cache
        if repository_environment is not None:
            params['repository_environment'] = repository_environment
        return self.app.call('claim', params, allocation['host'])

    def use_repository_environments(self):
        self.app.organization_config.pop('env')
        self.app.organization_config['env_root'] = str(self.root / 'envs')

    def cache_batch(self, *, run=1, device=''):
        self.app.organization_config.pop('env', None)
        self.app.organization_config['env_cache'] = str(self.root/'cache')
        prepared, _, _ = self.app.prepare(source(run))
        self.store.prepared_environment(prepared, {'schema': 1, 'test': 'pinned'})
        receipt = self.app.call('prepare', {'source': source(run)}, 'local')
        self.submit(run, device=device, environment_digest=receipt['environment_digest'])

    def test_cold_cache_allocates_by_capacity_and_rechecks_builder_on_claim(self):
        self.cache_batch()
        self.host('h100', cache=True)
        self.host('3090b', cache=True)
        self.host('3090a', repositories=['example/project'])
        allocation = self.reserve()
        self.assertEqual(allocation['host'], 'h100')
        self.assertEqual(self.claim(allocation, environment_cache=True)['source']['environment_recipe']['test'], 'pinned')
        with self.assertRaisesRegex(protocol.Error, 'environment cache mode'):
            self.claim(allocation)
        self.host('h100', repositories=['example/project'])
        with self.assertRaisesRegex(protocol.Error, 'capability'):
            self.claim(allocation, environment_cache=True)
        self.host('h100', cache=True)
        del self.app.organization_config['env_cache']
        self.app.organization_config['env'] = str(self.root/'shared')
        with self.assertRaisesRegex(protocol.Error, 'cache mode'):
            self.claim(allocation, environment_cache=True)

    def test_cache_device_pin_and_slurm_driver_need_cache_capability(self):
        self.cache_batch(device='3090b')
        self.host('h100', cache=True)
        self.host('3090b', repositories=['example/project'])
        self.assertIsNone(self.reserve())
        self.host('3090b', cache=True)
        self.assertEqual(self.reserve()['host'], '3090b')
        self.cache_batch(run=2, device='slurm')
        self.host('3090a', local_busy=True, ready=True)
        self.assertIsNone(self.reserve())
        self.host('3090a', local_busy=True, cache=True)
        allocation = self.reserve()
        self.assertEqual(allocation['pool'], 'slurm')
        self.assertTrue(self.claim(allocation, environment_cache=True)['organization_slurm_verified'])
        self.authorize()
        with self.assertRaisesRegex(protocol.Error, 'Slurm'):
            self.claim(allocation, environment_cache=True)

    def test_legacy_slurm_submission_skips_a_driver_switched_to_cache_mode(self):
        self.submit(device='slurm')
        self.host('3090a', cache=True)
        self.assertIsNone(self.reserve())
        self.host('3090a', ready=False)
        self.assertEqual(self.reserve()['pool'], 'slurm')

    def test_prepare_uses_controller_environment_mode_and_ignores_caller_mode(self):
        request = {**source(), 'repository_environment': True}
        self.assertNotIn('repository_environment', self.app.call('prepare', {'source': request}, 'local'))
        self.use_repository_environments()
        request['repository_environment'] = False
        self.assertIs(self.app.call('prepare', {'source': request}, 'local')['repository_environment'], True)

    def test_reserved_strict_environment_survives_controller_restart_in_shared_mode(self):
        self.use_repository_environments()
        self.host('3090b', repositories=['example/project'])
        self.submit(device='3090b')
        allocation = self.reserve()
        self.assertNotIn('repository_environment', allocation['grant'])
        self.assertNotIn('repository_environment', allocation['source'])
        db_path = self.root / 'restarted.sqlite'
        with sqlite3.connect(db_path) as copy:
            self.store.db.backup(copy)
        self.store = scheduler_store.Store(str(db_path), clock=lambda: self.now)
        self.addCleanup(self.store.close)
        shared = {**self.app.organization_config, 'env': str(self.root / 'env')}
        del shared['env_root']
        self.app = scheduler.Scheduler(self.app.inv, str(self.root), self.store, self.api,
                                       device_admission_file=self.app.device_admission_file, organization_config=shared)
        self.host('3090b')
        with self.assertRaisesRegex(protocol.Error, 'must confirm'):
            self.claim(allocation)
        with self.assertRaisesRegex(protocol.Error, 'no longer ready'):
            self.claim(allocation, repository_environment=True)
        self.host('3090b', repositories=['example/project'])
        self.assertEqual(self.claim(allocation, repository_environment=True)['id'], allocation['id'])

    def test_shared_grant_keeps_legacy_claim_until_strict_worker_takes_it(self):
        self.host('3090b')
        self.submit(device='3090b')
        allocation = self.reserve()
        self.now += scheduler_store.HEARTBEAT_TTL
        self.assertEqual(self.claim(allocation)['id'], allocation['id'])
        self.host('3090b', repositories=['example/project'])
        self.assertEqual(self.claim(allocation, repository_environment=True)['id'], allocation['id'])
        self.host('3090b')
        with self.assertRaisesRegex(protocol.Error, 'must confirm'):
            self.claim(allocation)

    def test_verified_repositories_use_their_own_registered_host_environments(self):
        self.use_repository_environments()
        self.host('h100', repositories=['example/other'])
        self.host('pro6000')  # Legacy shared Python only.
        self.host('3090b', repositories=['example/project'])
        self.submit()
        first = self.reserve()
        for reported in (None, False, 1, 'true'):
            with self.subTest(reported=reported), self.assertRaisesRegex(protocol.Error, 'must confirm'):
                self.claim(first, repository_environment=reported)
        self.assertEqual(self.claim(first, repository_environment=True)['host'], '3090b')
        self.repo = repository('example/other', 22)
        self.submit(run=2, repository='example/other', gpu=True)
        second = self.reserve()
        self.assertEqual((second['host'], self.claim(second, repository_environment=True)['pool']), ('h100', 'h100'))
        self.host('h100', repositories=['example/project'])
        with self.assertRaisesRegex(protocol.Error, 'no longer ready'):
            self.claim(second, repository_environment=True)
        self.host('h100', repositories=['example/other'], local_busy=True)
        self.assertEqual(self.claim(second, repository_environment=True)['id'], second['id'])  # Runner is now busy.
        self.now += scheduler_store.HEARTBEAT_TTL
        with self.assertRaisesRegex(protocol.Error, 'no longer ready'):
            self.claim(second, repository_environment=True)

    def test_legacy_or_empty_heartbeat_removes_permission_and_retry_stays_pinned(self):
        self.use_repository_environments()
        self.host('h100', repositories=['example/project'])
        self.host('3090b', repositories=['example/project'])
        batch = self.submit(device='3090b')
        self.host('3090b')
        self.assertIsNone(self.reserve())
        self.host('3090b', repositories=['example/project'])
        first = self.claim(self.reserve(), repository_environment=True)
        self.store.event('3090b', 'release', first['id'], 'released', {'state': 'error', 'retryable': True})
        self.store.runner_finished(first['id'], first['run_id'], 1, job_id=first['job_id'])
        self.store.request_task_allocation(batch['id'], first['task'], 2)
        self.host('3090b', repositories=[])
        self.assertIsNone(self.reserve())
        self.host('3090b', repositories=['example/project'])
        second = self.reserve()
        self.assertEqual((second['task_attempt'], second['host']), (2, '3090b'))
        Path(self.app.device_admission_file).write_text('[devices]\n')
        with self.assertRaisesRegex(protocol.Error, 'no longer admitted'):
            self.claim(second, repository_environment=True)

    def test_authorized_slurm_still_runs_without_a_matching_local_environment(self):
        self.use_repository_environments()
        self.host('3090a', repositories=['example/other'])
        self.submit()
        allocation = self.reserve()
        self.assertEqual(allocation['pool'], 'slurm')
        self.assertTrue(self.claim(allocation)['organization_slurm_verified'])

    def test_automatic_gpu_retry_follows_updated_repo_reports(self):
        self.use_repository_environments()
        self.host('h100', repositories=['example/project'])
        self.host('3090a', repositories=['example/other'])
        batch = self.submit(gpu=True)
        first = self.claim(self.reserve(), repository_environment=True)
        self.assertEqual(first['host'], 'h100')
        self.store.event('h100', 'release', first['id'], 'released', {'state': 'error', 'retryable': True})
        self.store.runner_finished(first['id'], first['run_id'], 1, job_id=first['job_id'])
        self.store.request_task_allocation(batch['id'], first['task'], 2)
        self.host('h100', repositories=[])
        self.assertIsNone(self.reserve())
        self.host('3090a', repositories=['example/project'])
        retry = self.reserve()
        self.assertEqual((retry['pool'], retry['task_attempt']), ('3090a', 2))

    def test_shared_controller_accepts_strict_reports_only_for_the_matching_repo(self):
        self.host('h100', repositories=['example/other'])
        self.host('3090b', repositories=['example/project'])
        self.submit()
        allocation = self.reserve()
        self.host('3090b')
        with self.assertRaisesRegex(protocol.Error, 'must confirm'):
            self.claim(allocation)
        self.host('3090b', repositories=['example/project'])
        self.assertEqual(self.claim(allocation, repository_environment=True)['host'], '3090b')

    def test_malformed_environment_report_cannot_replace_a_valid_report(self):
        self.use_repository_environments()
        self.host('3090b', repositories=['example/project'])
        for names in (None, 'example/project', [2], ['outside/project'], ['example/../tmp'],
                      ['example/..'], ['Example/project'], ['example/project', 'example/project']):
            with self.subTest(names=names), self.assertRaisesRegex(protocol.Error, 'invalid organization repository'):
                self.app.call('heartbeat', {'runners': [], 'organization_ready': False,
                                            'organization_repositories': names}, '3090b')
        with self.assertRaisesRegex(protocol.Error, 'invalid organization repository'):
            self.app.call('heartbeat', {'runners': [], 'organization_ready': True,
                                        'organization_repositories': ['example/project']}, '3090b')
        self.submit(device='3090b')
        self.assertEqual(self.reserve()['host'], '3090b')

    def test_upgrading_ledger_preserves_registrations_without_inventing_repo_readiness(self):
        # A 2.1.2 ledger can hold both a shared readiness report and pending tasks.
        path = self.root/'schedule.sqlite'
        old = scheduler_store.Store(str(path), clock=lambda: self.now)
        old.register_device('3090b', profile('3090b'))
        old.heartbeat('3090b', [dict(tag='3090b-0', online=True, busy=False)], organization_ready=True)
        trusted = {**source(), 'organization_profile': organization.profile({})}
        batch = old.submit(trusted, plan(device='3090b'), {'test-0': ['3090b']})
        old.request_task_allocation(batch, old.batch(batch)['tasks'][0]['id'], 1)
        old.close()
        with sqlite3.connect(path) as db:
            db.execute('DROP TABLE organization_environments')
            db.execute('DROP TABLE repository_environment_allocations')
        upgraded = scheduler_store.Store(str(path), clock=lambda: self.now)
        self.addCleanup(upgraded.close)
        self.assertEqual(upgraded.registrations(), [profile('3090b')])
        options = dict(allowed_devices={'3090b'}, organization=True)
        self.assertIsNone(upgraded.reserve(**options, require_repository_environment=True))
        self.assertEqual(upgraded.reserve(**options)['pool'], '3090b')

    def test_fixed_cpu_order_then_authorized_remote_pool(self):
        for device in ('3090a', '3090b', 'pro6000', 'h100'):
            self.host(device)
        expected = ['h100', 'pro6000', '3090b', '3090a', 'slurm']
        for run, device in enumerate(expected, 1):
            self.submit(run=run)
            allocation = self.reserve()
            self.assertEqual(allocation['pool'], device)
            self.assertEqual(self.claim(allocation).get('organization_slurm_verified'), True if device == 'slurm' else None)

    def test_gpu_priority_and_no_remote_fallback(self):
        for device in ('3090a', '3090b', 'pro6000', 'h100'):
            self.host(device)
        for run, device in enumerate(('h100', 'pro6000', '3090b', '3090a'), 1):
            self.submit(run=run, gpu=True)
            self.assertEqual(self.reserve()['pool'], device)
        self.submit(run=5, gpu=True)
        self.assertIsNone(self.reserve())

    def test_remote_capacity_does_not_depend_on_local_python_or_memory(self):
        self.host(ready=False, slurm_online=False)
        self.submit(device='slurm', cores=8)
        self.assertIsNone(self.reserve())
        self.host(ready=False)
        self.assertIsNone(self.reserve({'h100'}))
        self.now += scheduler_store.HEARTBEAT_TTL
        self.assertIsNone(self.reserve())
        self.host(ready=False)
        allocation = self.reserve()
        self.assertEqual((allocation['pool'], allocation['groups']), ('slurm', 2))
        for cores, minutes in [(12, 10), (4, 121)]:
            with self.subTest(cores=cores, minutes=minutes), self.assertRaisesRegex(protocol.Error, 'no configured'):
                self.submit(run=2, device='slurm', cores=cores, minutes=minutes)
        self.site.write_text(self.site.read_text().replace('mem_per_node = "480G"', 'mem_per_node = "4G"'))
        with self.assertRaisesRegex(protocol.Error, 'no longer admitted'):
            self.claim(allocation)

    def test_revocation_and_changed_binding_on_retry(self):
        self.host(local_busy=True)
        batch = self.submit(device='slurm', slurm_target='forged/cpu', slurm_project='forged',
                            organization_slurm_verified=True)
        self.assertNotIn('organization_slurm_verified', batch['source'])
        self.assertEqual(batch['source']['slurm_project'], self.binding['slurm_project'])
        allocation = self.reserve()
        self.authorize()
        with self.assertRaisesRegex(protocol.Error, 'no longer admitted'):
            self.claim(allocation)
        self.authorize(self.binding)
        self.repo = repository('example/project', 11, visibility='public')
        with self.assertRaisesRegex(protocol.Error, 'no longer admitted'):
            self.claim(allocation)
        self.repo = repository('example/project', 11)
        claimed = self.claim(allocation)
        self.store.event('3090a', 'release', claimed['id'], 'released', {'state': 'error', 'retryable': True})
        self.store.runner_finished(claimed['id'], claimed['run_id'], 1, job_id=claimed['job_id'])
        self.store.request_task_allocation(batch['id'], claimed['task'], 2)
        self.authorize({**self.binding, 'slurm_project': 'replacement'})
        self.host(local_busy=False)
        self.assertIsNone(self.reserve())
        self.authorize(self.binding)
        self.assertEqual(self.reserve()['pool'], 'slurm')

    def test_auto_retry_uses_local_after_remote_permission_is_removed(self):
        self.host(local_busy=True)
        batch = self.submit()
        first = self.claim(self.reserve())
        self.assertEqual(first['pool'], 'slurm')
        self.store.event('3090a', 'release', first['id'], 'released', {'state': 'error', 'retryable': True})
        self.store.runner_finished(first['id'], first['run_id'], 1, job_id=first['job_id'])
        self.store.request_task_allocation(batch['id'], first['task'], 2)
        self.authorize()
        self.assertIsNone(self.reserve())
        self.host()
        second = self.reserve()
        self.assertEqual((second['pool'], second['task_attempt']), ('3090a', 2))

    def test_identical_submission_after_revocation_or_grant_keeps_original_batch(self):
        for run, device in enumerate(('', '3090a', 'slurm'), 1):
            self.authorize(self.binding)
            original = self.submit(run=run, device=device)
            self.authorize()
            self.assertEqual(self.submit(run=run, device=device), original)
            with self.assertRaisesRegex(protocol.Error, 'different content'):
                self.submit(run=run, device=device, command='another command')
            with self.assertRaisesRegex(protocol.Error, 'different content'):
                self.submit(run=run, device=device, cancel_in_progress=True)
        # The pre-Slurm organization source had an empty target and no cluster project.
        unbound = self.submit(run=4)
        self.assertNotIn('slurm_project', unbound['source'])
        self.authorize(self.binding)
        self.assertEqual(self.submit(run=4), unbound)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM batches').fetchone()[0], 4)
        self.repo = repository('example/project', 11, visibility='public')
        with self.assertRaisesRegex(protocol.Error, 'not admitted'):
            self.submit(run=4)

    def test_concurrent_first_submission_pins_one_routing_decision(self):
        prepared = threading.Event()
        resume = threading.Event()
        original_submit = self.store.submit

        def persist(source, *args, **kwargs):
            if source.get('slurm_project'):
                prepared.set()
                self.assertTrue(resume.wait(5))
            return original_submit(source, *args, **kwargs)

        with mock.patch.object(self.store, 'submit', side_effect=persist), concurrent.futures.ThreadPoolExecutor(1) as workers:
            pending = workers.submit(self.submit)
            try:
                self.assertTrue(prepared.wait(5))
                self.authorize()
                first = self.submit()
            finally:
                resume.set()
            self.assertEqual(pending.result(timeout=5), first)
        self.assertNotIn('slurm_project', first['source'])
        self.host(local_busy=True)
        self.authorize(self.binding)
        self.assertIsNone(self.reserve())

    def test_bad_or_missing_permission_leaves_local_available(self):
        self.host(local_busy=True)
        self.submit()
        self.authorize({**self.binding, 'repository_id': 12})
        self.assertIsNone(self.reserve())
        self.host()
        for invalid in ('broken json', None):
            if invalid:
                self.allowlist.write_text(invalid)
            else:
                self.allowlist.unlink()
            with self.assertRaisesRegex(protocol.Error, 'no configured'):
                self.submit(run=2, device='slurm', slurm_project='project-a', slurm_target='cluster1/cpu')
        self.assertEqual(self.reserve()['pool'], '3090a')
