import functools
import json
import os
import subprocess
import tempfile
import unittest
import venv
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ci_runners import envuse, inventory, part
from parallel import executor, executor_store, protocol, scheduler_store, task_execution, transport, workflow_client
from parallel.tests.test_central import plan, source
from parallel.tests.test_central_services import inv_text
from parallel.tests.test_device_registration import profile
from parallel.tests.test_gpu_queue import grant
from tests.test_slurmgate import EXAMPLE


class OrganizationDispatch(unittest.TestCase):
    def test_requires_current_shared_readiness_without_repo_project_entries(self):
        store = scheduler_store.Store(':memory:')
        self.addCleanup(store.close)
        device = '3090a'
        store.register_device(device, profile(device))
        listeners = [dict(tag='3090a-0', online=True, busy=False)]

        def queue(run, repository):
            document = protocol.load_plan(plan())
            task_id = document['parts'][0]['id']
            origin = {**source(run), 'repository': repository, 'project': f'repo-{run}',
                      'repository_id': run, 'organization_profile': {'max_minutes': 600}}
            batch = store.submit(origin, document, {task_id: [device]})
            store.request_task_allocation(batch, store.batch(batch)['tasks'][0]['id'], 1)

        queue(1, 'example/first')
        store.heartbeat(device, listeners, ready_projects={})
        self.assertIsNone(store.reserve(allowed_devices={device}, organization=True))
        with self.assertRaisesRegex(protocol.Error, 'invalid heartbeat'):
            store.heartbeat(device, listeners, organization_ready=1)
        store.heartbeat(device, listeners, ready_projects={}, organization_ready=True)
        self.assertIsNone(store.reserve(allowed_devices={device}))
        allocation = store.reserve(allowed_devices={device}, organization=True)
        self.assertEqual(allocation['source']['repository'], 'example/first')
        self.assertFalse(allocation['released'])

        queue(2, 'example/second')
        store.heartbeat(device, listeners, ready_projects={}, organization_ready=False)
        self.assertIsNone(store.reserve(allowed_devices={device}, organization=True))
        self.assertFalse(store.allocation(allocation['id'])['released'])


class OrganizationWorkflow(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.etc = Path(directory.name)
        (self.etc / 'central.toml').touch()
        self.settings = {'organization': {'env': '/valid/environment', 'name': 'example'}}
        self.inv = SimpleNamespace(host={'data': str(self.etc)})
        self.env = {'RUNNER_TEMP': str(self.etc), 'GITHUB_REPOSITORY': 'example/' + 'r' * 75,
                    'GITHUB_SHA': 'a' * 40, 'GITHUB_RUN_ID': '1', 'GITHUB_RUN_ATTEMPT': '1'}
        self.approved = {'sha': 'a' * 40, 'project': 'repo-42', 'repository_id': 42,
                         'organization_profile': {'max_parallel': 2, 'max_minutes': 10,
                                                  'retries': 0, 'gpu_mem_mib': 0}}

    def test_prepare_persists_server_binding_and_does_not_require_repo_name_as_slug(self):
        with (mock.patch.object(transport, 'settings', return_value=self.settings),
              mock.patch.object(transport, 'Client') as client):
            client.return_value.call.return_value = self.approved
            result = workflow_client.main(['prepare', '--project', 'caller project is ignored', '--context', 'ci/tests'],
                                          self.inv, str(self.etc), self.env)
        self.assertEqual(result, 0)
        self.assertEqual(client.return_value.call.call_args.kwargs['source']['project'], 'organization')
        persisted = json.loads(workflow_client.state_file(self.env, 'source').read_text())
        self.assertEqual(persisted['repository'], self.env['GITHUB_REPOSITORY'])
        self.assertEqual({key: persisted[key] for key in self.approved}, self.approved)

    def test_build_supplies_resolved_project_and_enforces_plan_limit(self):
        origin = {**self.approved, 'repository': self.env['GITHUB_REPOSITORY']}
        workflow_client.state_file(self.env, 'source').write_text(json.dumps(origin))
        requested_plan = plan()
        requested_plan['parts'][0]['minutes'] = 5
        workflow_client.state_file(self.env, 'plan').write_text(json.dumps(requested_plan))
        project = SimpleNamespace(name='repo-42', max_minutes=10)
        with (mock.patch.object(transport, 'settings', return_value=self.settings),
              mock.patch.object(workflow_client.organization, 'project', return_value=project) as resolver,
              mock.patch.object(workflow_client.organization, 'environment_ready', return_value=True),
              mock.patch.object(part, 'Executor') as engine):
            engine.return_value.execute.return_value = 0, {'outcome': 'pass'}
            self.assertEqual(workflow_client.build(SimpleNamespace(plan_command='true'), self.inv,
                                                   str(self.etc), self.env), 0)
        resolver.assert_called_once_with(self.settings, self.inv, origin)
        self.assertEqual(engine.return_value.execute.call_args.args[0].project, 'repo-42')
        self.assertIs(engine.return_value.execute.call_args.kwargs['organization_project'], project)

    def test_prepared_repository_plan_cannot_switch_to_a_shared_python(self):
        command = ['prepare', '--context', 'env/check']
        strict = {'organization': {'name': 'example', 'env_root': str(self.etc / 'envs')}}
        with (mock.patch.object(transport, 'Client') as client,
              mock.patch.object(transport, 'settings', return_value=strict) as settings):
            client.return_value.call.return_value = {**self.approved, 'repository_environment': True}
            self.assertEqual(workflow_client.main(command, self.inv, str(self.etc), self.env), 0)
            saved = json.loads(workflow_client.state_file(self.env, 'source').read_text())
            self.assertIs(saved['repository_environment'], True)
            settings.return_value = self.settings
            with self.assertRaisesRegex(protocol.Error, 'mode changed on the plan runner'):
                workflow_client.build(SimpleNamespace(plan_command='true'), self.inv, str(self.etc), self.env)
            with mock.patch('sys.stderr'):
                self.assertEqual(workflow_client.main(command, self.inv, str(self.etc), self.env), 2)

    def test_worker_checks_controller_verification_before_using_profile(self):
        allocated = {'grant': {'backend': 'local'}, 'source': {}, 'part': {}}
        with (mock.patch.object(transport, 'settings', return_value=self.settings),
              mock.patch.object(protocol, 'validate_allocation', return_value=allocated['grant']),
              self.assertRaisesRegex(protocol.Error, 'not a verified organization job')):
            task_execution.execute(self.inv, str(self.etc), {}, allocated, [])

    def test_slurm_worker_uses_authorized_remote_environment_and_organization_driver_slice(self):
        self.settings = {'organization': {'name': 'example', 'env_root': str(self.etc / 'absent')}}
        inv = inventory.load(inv_text(self.etc, device='slurm').replace('memory_max = "80G"', 'memory_max = "4G"'))
        workspace = Path(inv.host['data']) / 'ci-work/slurm0/workspace'
        workspace.mkdir(parents=True)
        env = {**self.env, 'GITHUB_WORKSPACE': str(workspace), 'RUNNER_TEMP': str(workspace),
               'RUNNER_NAME': 'slurm0', 'GITHUB_REPOSITORY': 'example/organization'}
        grant = dict(allocation_schema=1, allocation_id='alloc', task_id='test', host=inv.this,
                     device='slurm', backend='slurm', requested_device='', runner_tag='slurm0',
                     cpu_groups=2, cores=8, RAM='8G', workers=2, minutes=1, task_attempt=1)
        origin = {**self.approved, 'repository': env['GITHUB_REPOSITORY'], 'context': 'test/ci',
                  'slurm_project': 'cluster-env', 'slurm_target': 'cluster1/cpu'}
        claim = dict(grant=grant, source=origin, part=dict(id='test', command='true', cores=8, workers=2),
                     deadline=1e12, run_id=1, run_attempt=1, organization_repository_verified=True)
        site = self.etc / 'slurm/cluster1.toml'
        site.parent.mkdir()
        site.write_text(EXAMPLE.replace('enabled = true', 'enabled = ["cluster-env"]'))
        site.chmod(0o600)
        remote = mock.Mock()
        remote.call.return_value = claim
        ledger = executor_store.Ledger(str(self.etc / 'worker.sqlite'), [])
        self.addCleanup(ledger.close)
        app = executor.ExecutorService(inv, str(self.etc), ledger, remote, settings=self.settings)
        with mock.patch.object(app, 'launch') as launch:
            with self.assertRaisesRegex(protocol.Error, 'did not authorize'):
                app.call('start', {'env': env, 'grant': grant})
            launch.assert_not_called()
            claim['organization_slurm_verified'] = True
            app.call('start', {'env': env, 'grant': grant})
        metadata = ledger.get('alloc')['metadata']
        self.assertIs(metadata['organization_slurm_verified'], True)
        run = mock.Mock(return_value=SimpleNamespace(stdout=''))
        spawn = mock.Mock()
        with (mock.patch.object(transport, 'settings', return_value=self.settings),
              mock.patch.object(part.Executor, '_end', return_value=(0, {})),
              mock.patch.object(task_execution.signal, 'signal'),
              mock.patch.object(task_execution.shutil, 'disk_usage', return_value=mock.Mock(free=8 << 30))):
            allocation = {**metadata, 'grant': grant}
            with self.assertRaisesRegex(protocol.Error, 'not authorized'):
                task_execution.execute(inv, str(self.etc), env, {**allocation, 'organization_slurm_verified': False}, [],
                                       run=run, spawn=spawn)
            task_execution.execute(inv, str(self.etc), metadata['env'], allocation, [], run=run, spawn=spawn)
        driver = json.loads((Path(executor.record_path(inv, metadata)) / 'slurm-plan.json').read_text())
        self.assertEqual(driver['request']['project'], 'cluster-env')
        self.assertEqual(driver['targets'], ['cpu'])
        self.assertEqual(driver['request']['allocation'], grant)
        self.assertEqual(driver['request']['variables']['GITHUB_REPOSITORY'], env['GITHUB_REPOSITORY'])
        self.assertIn('--slice=civ2.slice', spawn.call_args.args[0])

    def test_organization_scope_keeps_per_job_memory_limit_and_host_slice(self):
        arguments = part.scope_argv('job.scope', 'repo-42', '4G', [], False, slice_name='civ2.slice')
        self.assertIn('--slice=civ2.slice', arguments)
        self.assertIn('MemoryMax=4G', arguments)
        self.assertIn('MemorySwapMax=0', arguments)
        self.assertIn('--slice=ci-repo-42.slice', part.scope_argv('job.scope', 'repo-42', '4G', []))


class OrganizationHeartbeat(unittest.TestCase):
    def heartbeat(self, **fields):
        app = object.__new__(executor.ExecutorService)
        app.names = {'3090a-0'}
        app.inv = SimpleNamespace(host={'data': '/fixture', 'max_temp_c': 80})
        app.ledger = mock.Mock()
        app.ledger.reports.return_value = []
        app.remote = mock.Mock()
        app.remote.call.return_value = []
        app.registration = profile('3090a')
        app.registered = False
        app.settings = {'organization': {'name': 'example', **fields}}
        app.run = mock.Mock()
        with (mock.patch.object(executor.pool, 'runner_procs', return_value=(app.names, set())),
              mock.patch.object(executor.drain, 'read'),
              mock.patch.object(executor.drain, 'refusal', return_value=False),
              mock.patch.object(executor.gpu_resources, 'available', return_value={}),
              mock.patch.object(executor.organization, 'environment_ready', return_value=True) as ready,
              mock.patch.object(app, 'ready_projects', side_effect=AssertionError('per-repo readiness was read'))):
            app.sync()
        registration, heartbeat = app.remote.call.call_args_list
        self.assertEqual(registration, mock.call('register', **app.registration))
        self.assertEqual(heartbeat.args, ('heartbeat',))
        self.assertEqual(heartbeat.kwargs['ready_projects'], {})
        return heartbeat.kwargs, ready

    def test_shared_environment_readiness_uses_existing_heartbeat(self):
        heartbeat, ready = self.heartbeat(env='/valid/environment')
        self.assertIs(heartbeat['organization_ready'], True)
        self.assertNotIn('organization_repositories', heartbeat)
        ready.assert_called_once()

    def test_cold_cache_heartbeat_advertises_only_builder_capability(self):
        heartbeat, ready = self.heartbeat(env_cache='/absent/cache')
        self.assertIs(heartbeat['environment_cache'], True)
        self.assertIs(heartbeat['organization_ready'], False)
        self.assertNotIn('organization_repositories', heartbeat)
        ready.assert_not_called()


class RepositoryEnvironments(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.etc = self.root / 'etc'
        self.etc.mkdir()
        (self.etc / 'central.toml').touch()
        inventory_text = inv_text(self.root, cpus=str(min(os.sched_getaffinity(0))))
        (self.etc / 'inventory.toml').write_text(inventory_text)
        self.inv = inventory.load(inventory_text)
        self.workspace = Path(self.inv.host['data']) / 'ci-work/3090a-0/workspace'
        self.workspace.mkdir(parents=True)
        self.env_root = self.root / 'envs'
        self.settings = {'organization': {'name': 'example', 'env_root': str(self.env_root),
                                         'env_aliases': {'example/staging': 'alpha'}}}
        self.env = dict(RUNNER_NAME='3090a-0', GITHUB_WORKSPACE=str(self.workspace),
                        RUNNER_TEMP=str(self.workspace), GITHUB_RUN_ID='1', GITHUB_RUN_ATTEMPT='1',
                        PATH=os.environ['PATH'])
        self.profile = dict(max_parallel=2, max_minutes=30, retries=0, gpu_mem_mib=0)
        self.ledger = executor_store.Ledger(str(executor.paths(self.inv)[0]), executor.groups(self.inv), [])
        self.addCleanup(self.ledger.close)
        self.remote = mock.Mock()
        self.app = executor.ExecutorService(self.inv, str(self.etc), self.ledger, self.remote, settings=self.settings)
        patch = mock.patch.object(transport, 'settings', return_value=self.settings)
        self.control = patch.start()
        self.addCleanup(patch.stop)

    def environment(self, name):
        build = self.env_root / (name + '-20261117T000000Z')
        venv.EnvBuilder(symlinks=True).create(build)
        (self.env_root / name).symlink_to(build.name)
        return build

    def claim(self, repository, repo_id=1):
        source = dict(repository=repository, repository_id=repo_id, project=f'repo-{repo_id}',
                      organization_profile=self.profile, context='test/ci', sha='a' * 40)
        issue = {**grant('task-' + str(repo_id)), 'task_attempt': 1}
        command = "python -c 'import os,sys; print(sys.prefix, os.environ[\"CONDA_PREFIX\"], sep=\"\\n\")' > prefixes"
        return dict(grant=issue, part=dict(id='test', workers=3, cores=4, command=command),
                    source=source, deadline=1e12, run_id=1, run_attempt=1, organization_repository_verified=True)

    def test_cache_executor_claims_without_inspecting_repository_prefixes(self):
        self.app.settings = {'organization': {'name': 'example', 'env_cache': str(self.root/'empty')}}
        allocation = self.claim('example/alpha')
        env = {**self.env, 'GITHUB_REPOSITORY': 'example/alpha'}
        self.remote.call.return_value = allocation
        with self.assertRaisesRegex(protocol.Error, 'pinned environment recipe'):
            self.app.call('admit', {'env': env})
        allocation['source']['environment_recipe'] = {'schema': 1}
        with mock.patch.object(executor.organization, 'environment_ready', side_effect=AssertionError('inspected prefix')):
            self.assertEqual(self.app.call('admit', {'env': env}), allocation)
        self.assertTrue(self.remote.call.call_args.kwargs['environment_cache'])

    def test_allocation_ownership_accepts_mixed_case_repository_names(self):
        self.environment('alpha')
        claim = self.claim('example/staging')
        self.remote.call.return_value = claim
        env = dict(self.env, GITHUB_REPOSITORY='Example/Staging')
        with mock.patch.object(self.app, 'launch'):
            allocation = self.app.call('start', {'env': env, 'grant': claim['grant']})

        request = dict(allocation, env=env)
        self.assertEqual(self.app.call('poll', request)['state'], 'reserved')

        mismatches = (('GITHUB_REPOSITORY', 'example/other'), ('GITHUB_REPOSITORY', None),
                      ('RUNNER_NAME', '3090a-1'), ('GITHUB_RUN_ID', '2'), ('GITHUB_RUN_ATTEMPT', '2'))
        for field, value in mismatches:
            wrong = dict(request, env={**env, field: value})
            with self.subTest(field=field, value=value):
                for operation in ('poll', 'cancel'):
                    with self.assertRaisesRegex(protocol.Error, 'allocation belongs to another GitHub job'):
                        self.app.call(operation, wrong)

        self.assertEqual(self.app.call('cancel', request), {'accepted': True})

    def test_plan_and_worker_use_the_same_named_environment_for_each_repository(self):
        engine_without_systemd = functools.partial(part.Executor, scope=False)
        for repo_id, (repository, directory) in enumerate((('example/staging', 'alpha'), ('example/beta', 'beta')), 1):
            with self.subTest(repository=repository):
                build = self.environment(directory)
                claim = self.claim(repository, repo_id)
                env = {**self.env, 'GITHUB_REPOSITORY': repository}
                workflow_client.state_file(env, 'source').write_text(json.dumps(claim['source']))
                doc = plan()
                doc['parts'][0]['minutes'] = 1
                (self.workspace / 'requested-plan.json').write_text(json.dumps(doc))
                plan_command = claim['part']['command'] + ' && cat requested-plan.json'
                with (mock.patch.object(part, 'Executor', engine_without_systemd),
                      mock.patch.object(part.signal, 'signal')):
                    self.assertEqual(workflow_client.build(SimpleNamespace(plan_command=plan_command),
                                                           self.inv, str(self.etc), env), 0)
                prefixes = self.workspace / 'prefixes'
                self.assertEqual(prefixes.read_text().splitlines(), [str(build), str(build)])
                prefixes.unlink()

                def local_scope(argv, **kwargs):
                    record, _, _, command, *_ = json.loads(argv[-1])
                    result = subprocess.run(['/bin/bash', '-ec', command], **kwargs)
                    part._write_json(str(Path(record) / 'exit.json'), {'rc': result.returncode})
                    return mock.Mock()

                with mock.patch.object(part.signal, 'signal'):
                    _, outcome = task_execution.execute(self.inv, str(self.etc), env, claim, [],
                                                        run=mock.Mock(return_value=SimpleNamespace(stdout='')),
                                                        spawn=local_scope)
                self.assertEqual(outcome['outcome'], 'pass')
                self.assertEqual(prefixes.read_text().splitlines(), [str(build), str(build)])

    def test_claim_reports_stale_readiness_and_ignores_caller_mode(self):
        claim = self.claim('example/staging')
        env = {**self.env, 'GITHUB_REPOSITORY': 'example/staging'}
        self.remote.call.return_value = claim
        with self.assertRaisesRegex(protocol.Error, 'example/staging.*not ready'):
            self.app.call('admit', {'env': env, 'repository_environment': False})
        self.assertIs(self.remote.call.call_args.kwargs['repository_environment'], True)
        report, = self.ledger.reports()
        self.assertEqual((report['kind'], report['result']['state'], report['result']['retryable']), ('released', 'error', True))
        self.assertEqual(self.ledger.active(), [])
        self.app.settings = {'organization': {'name': 'example', 'env': '/shared'}}
        self.app.call('admit', {'env': env, 'repository_environment': True})
        self.assertNotIn('repository_environment', self.remote.call.call_args.kwargs)

    def test_worker_rechecks_environment_after_start_and_releases_on_failure(self):
        self.environment('alpha')
        claim = self.claim('example/staging')
        env = {**self.env, 'GITHUB_REPOSITORY': 'example/staging'}
        self.remote.call.return_value = claim
        aid = claim['grant']['allocation_id']
        with mock.patch.object(self.app, 'launch'):
            self.app.call('start', {'env': env, 'grant': claim['grant']})
        self.assertIs(self.ledger.get(aid)['metadata']['repository_environment'], True)
        (self.env_root / 'alpha').unlink()
        spawn = mock.Mock(side_effect=AssertionError('launched a process without its environment'))
        guarded_worker = functools.partial(task_execution.execute, spawn=spawn)
        with mock.patch.object(part.signal, 'signal'), mock.patch.object(task_execution, 'execute', guarded_worker):
            executor.worker(str(self.etc), aid)
            spawn.assert_not_called()
        with mock.patch.object(executor.slots, 'alive', return_value=False), mock.patch.object(self.app, 'stop_scope', return_value=True):
            self.app.poll_local()
        report, = self.ledger.reports()
        self.assertEqual(report['result']['state'], 'error')
        self.assertIn('example/staging', report['result']['reason'])
        self.assertEqual(self.ledger.active(), [])

    def test_failed_remote_claim_reconciles_only_after_trusted_completion(self):
        claim = self.claim('example/staging')
        env = {**self.env, 'GITHUB_REPOSITORY': 'example/staging'}
        def remote(method, **_):
            if method == 'claim':
                raise protocol.Error('claim refused')
            if method == 'heartbeat':
                return [{**claim, 'cancelled': False, 'runner_done': True}]
        self.remote.call.side_effect = remote
        with self.assertRaisesRegex(protocol.Error, 'claim refused'):
            self.app.call('admit', {'env': env, 'grant': claim['grant']})
        self.assertIsNone(self.ledger.get(claim['grant']['allocation_id']))
        with mock.patch.object(executor.pool, 'runner_procs', return_value=(self.app.names, set())):
            self.app.sync()
        self.assertEqual(self.ledger.get(claim['grant']['allocation_id'])['state'], 'released')
        self.assertEqual(self.remote.call.call_args.args, ('event',))
        self.assertEqual(self.remote.call.call_args.kwargs['kind'], 'released')

    def test_changed_service_mode_cannot_downgrade_a_reserved_task(self):
        self.environment('alpha')
        claim = self.claim('example/staging')
        self.remote.call.return_value = claim
        env = {**self.env, 'GITHUB_REPOSITORY': 'example/staging'}
        with mock.patch.object(self.app, 'launch'):
            self.app.call('start', {'env': env, 'grant': claim['grant']})
        aid = claim['grant']['allocation_id']
        self.control.return_value = {'organization': {'name': 'example', 'env': str(self.env_root / 'alpha')}}
        with mock.patch.object(task_execution.organization, 'project', side_effect=AssertionError('selected shared environment')):
            self.assertEqual(executor.worker(str(self.etc), aid), 2)
        with mock.patch.object(executor.slots, 'alive', return_value=False), mock.patch.object(self.app, 'stop_scope', return_value=True):
            self.app.poll_local()
        self.assertEqual(self.ledger.active(), [])
        self.assertEqual(self.ledger.reports()[0]['result']['state'], 'error')

    def test_plan_rechecks_build_after_initial_readiness_and_rejects_an_escape(self):
        build = self.environment('alpha')
        repository = 'example/staging'
        env = {**self.env, 'GITHUB_REPOSITORY': repository}
        workflow_client.state_file(env, 'source').write_text(json.dumps(self.claim(repository)['source']))
        outside = self.root / 'outside'
        build.rename(outside)
        link = self.env_root / 'alpha'
        link.unlink()
        link.symlink_to(outside)
        engine_without_systemd = functools.partial(part.Executor, scope=False)
        with (mock.patch.object(part, 'Executor', engine_without_systemd), mock.patch.object(part.signal, 'signal'),
              mock.patch.object(workflow_client.organization, 'environment_ready', return_value=True),
              self.assertRaisesRegex(protocol.Error, 'example/staging.*not ready')):
            workflow_client.build(SimpleNamespace(plan_command='touch escaped'), self.inv, str(self.etc), env)
        self.assertFalse((self.workspace / 'escaped').exists())
        self.assertFalse((outside / envuse.INUSE).exists())

    def test_strict_heartbeat_reports_only_usable_local_repository_prefixes(self):
        self.environment('alpha')
        self.app.registered = True
        self.app.registration = profile('3090a')
        self.remote.call.return_value = []
        with (mock.patch.object(executor.pool, 'runner_procs', return_value=(self.app.names, set())),
              mock.patch.object(executor.gpu_resources, 'available', return_value={})):
            self.app.sync()
        pulse = self.remote.call.call_args.kwargs
        self.assertIs(pulse['organization_ready'], False)
        self.assertEqual(pulse['organization_repositories'], ['example/alpha', 'example/staging'])
