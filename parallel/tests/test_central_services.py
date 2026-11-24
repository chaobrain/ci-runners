"""Central protocol tests use local temporary files and fake GitHub/systemd only."""
import dataclasses
import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from ci_runners import cli, flow, hook, inventory, policy, setup, slurmgate, slurmpart, slurmsite
from parallel import task_execution, protocol, transport, executor_store, github_coordinator, executor, scheduler, scheduler_store
from tests import yamlsub
from parallel.tests.test_central import plan, source

ROOT = Path(__file__).resolve().parents[2]


def inv_text(root, *, device='3090a', count=4, cpus='0-15'):
    return f'''schema = 2
this = "worker"
[hosts.worker]
account = "acct-x"
home = "{root}/home"
data = "{root}/data"
slots = 1
memory_max = "80G"
disk_min_gib = 1
gpus = []
max_temp_c = 80
cpus = "{cpus}"
pools = {{ cpu = {{ class = "executor", device = "{device}", count = {count} }} }}
files = {{ slot_lock = "{root}/s.lock", slot_queue = "{root}/s.queue" }}
'''


class StagingStartup(unittest.TestCase):
    def test_scheduler_accepts_shared_uid_but_requires_authorized_clients(self):
        config = {'plan': {'runner_uids': [os.getuid()], 'database': '/test/db'}}
        with (mock.patch.object(transport, 'settings', return_value=config),
              mock.patch.object(cli, 'load_inventory'), mock.patch.object(cli, 'etc_dir'),
              mock.patch.object(scheduler.Path, 'mkdir', side_effect=InterruptedError) as mkdir):
            with self.assertRaises(InterruptedError):
                cli.main(['scheduler', 'serve'])
            mkdir.assert_called_once()
            mkdir.reset_mock()
            config['plan']['runner_uids'] = []
            with self.assertRaisesRegex(protocol.Error, 'authorized runner UID'):
                cli.main(['scheduler', 'serve'])
            mkdir.assert_not_called()


class Registration(unittest.TestCase):
    def test_executor_setup_plan_is_opt_in_and_lists_the_service(self):
        from tests.test_setup import Setup
        fixture = Setup()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        mixed = inv_text(fixture.tmp.name).replace(
            'count = 4 } }',
            'count = 4 }, plan = { class = "plan", count = 1 }, '
            'slurm = { class = "executor", device = "slurm", count = 20 } }',
        )
        (fixture.etc/'inventory.toml').write_text(mixed)
        (fixture.etc/'projects/p.toml').write_text('name="p"\n[hosts.worker]\nmemory_max="80G"\n')
        with mock.patch.object(setup, 'linger_on', return_value=True):
            text = setup.build(str(fixture.etc)).text()
        self.assertIn('ci-executor.service', text)
        self.assertIn('enable --now ci-executor.service', text)
        self.assertIn('labels 3090a-0', text)
        self.assertIn('labels plan-0,ci-plan', text)
        self.assertIn('labels slurm19', text)
        self.assertIn('CI_RUNNER_CLASS=executor', text)
        self.assertNotIn('labels ci-cpu', text)
        self.assertEqual(fixture.ran, [])

    def test_inventory_and_setup_use_numbered_runners_and_explicit_roles(self):
        example = inventory.load((ROOT/'parallel/examples/inventory.example.toml').read_text())
        pools, rs = scheduler.registration(example)
        self.assertEqual(pools, protocol.DEVICE_GROUPS)
        self.assertEqual(len(rs), 58)
        self.assertEqual(example.this, '3090a')
        self.assertCountEqual(setup.runners(example), [
            ('plan-0', ['plan-0', 'ci-plan'], 'plan'),
            *((f'3090a-{i}', [f'3090a-{i}'], 'executor') for i in range(4)),
            *((f'slurm{i}', [f'slurm{i}'], 'executor') for i in range(20)),
        ])
        self.assertEqual({runner['host'] for runner in rs if runner['pool'] == 'slurm'}, {'3090a'})
        inv = inventory.load(inv_text('/example'))
        self.assertEqual(setup.runners(inv), [(f'3090a-{i}', [f'3090a-{i}'], 'executor') for i in range(4)])
        self.assertIn(('ci-executor.service', 'default.target'), setup.wanted(inv.host, ['3090a-0']))
        env = setup.env_file(inv, 'executor', '/example/etc', pool='cpu')
        self.assertIn('CI_RUNNER_CLASS=executor', env)
        self.assertNotIn('CI_RUNNER_SLOT', env)
        pools, runners = scheduler.registration(inv)
        self.assertEqual(pools, {'3090a': 4})
        self.assertEqual(len(runners), 4)
        self.assertEqual(executor.groups(inventory.load(inv_text('/example', cpus='100-116'))), list(range(4)))
        for bad in (inv_text('/example').replace('schema = 2', 'schema = 1'),):
            with self.assertRaises(inventory.InventoryError):
                inventory.load(bad)
        with self.assertRaisesRegex(Exception, 'schema 2 requires'):
            flow.load(plan())

    def test_workflows_keep_original_sha_and_no_write_token_in_test_job(self):
        entry = yamlsub.load((ROOT/'.github/workflows/ci-v2.yml').read_text())
        work = yamlsub.load((ROOT/'.github/workflows/execute.yml').read_text())
        task = yamlsub.load((ROOT/'.github/workflows/cpu-task.yml').read_text())
        for doc in (entry, work, task):
            self.assertEqual(set(doc['on']), {'workflow_call'})
            for job in doc['jobs'].values():
                self.assertLessEqual(set(job['permissions'].values()), {'read'})
        self.assertEqual(entry['jobs']['plan']['runs-on'], ['self-hosted', 'ci-plan'])
        job = work['jobs']['execute']
        self.assertEqual(job['runs-on'], ['self-hosted', '${{ inputs.runner_tag }}'])
        checkout = next(s for s in job['steps'] if s.get('uses', '').startswith('actions/checkout@'))
        self.assertEqual(checkout['with']['ref'], '${{ steps.allocation.outputs.sha }}')
        self.assertIs(checkout['with']['persist-credentials'], False)
        self.assertIn('job_timeout_minutes', job['timeout-minutes'])
        self.assertFalse(entry['jobs']['parts']['strategy']['fail-fast'])
        self.assertEqual(entry['jobs']['verdict']['needs'], ['plan', 'parts', 'gpu_parts'])
        self.assertIn('task-result', str(task))
        self.assertFalse((ROOT/'setup/consumer-ci-execute.yml').exists())


class Rpc(unittest.TestCase):
    def test_socket_authentication_and_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp)/'control.sock')
            app = lambda method, params, identity: [method, params, identity]
            server = transport.unix_server(path, app)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                self.assertEqual(transport.Client(path=path).call('capacity', n=4), ['capacity', {'n':4}, 'local'])
                with self.assertRaisesRegex(protocol.Error, 'already running'):
                    transport.unix_server(path, app)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
            denied = transport.unix_server(path, app, uids=[])
            thread = threading.Thread(target=denied.serve_forever, daemon=True)
            thread.start()
            try:
                with self.assertRaisesRegex(protocol.Error, 'not authorized'):
                    transport.Client(path=path).call('capacity')
            finally:
                denied.shutdown()
                denied.server_close()
                thread.join()

    def test_credentials_cannot_be_links_public_files_or_http(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp)/'credential'
            f.write_text('example-secret')
            f.chmod(0o600)
            self.assertEqual(transport.secret(str(f)), 'example-secret')
            link = Path(tmp)/'link'
            link.symlink_to(f)
            with self.assertRaises(OSError):
                transport.secret(str(link))
            f.chmod(0o644)
            with self.assertRaises(protocol.Error):
                transport.secret(str(f))
        with self.assertRaises(protocol.Error):
            transport.Client(url='http://example.invalid')


class Admission(unittest.TestCase):
    def test_project_device_and_slurm_admission_cannot_be_bypassed(self):
        with tempfile.TemporaryDirectory() as tmp:
            inv = inventory.load(inv_text(tmp).replace('count = 4 } }', 'count = 4 }, slurm = { class="executor", device="slurm", count=20 } }'))
            etc = Path(tmp)/'etc'
            (etc/'projects').mkdir(parents=True)
            (etc/'projects/project.toml').write_text('''name="project"
repository="example/project"
context="project/ci"
devices=["3090a", "slurm"]
slurm_target="site/cpu"
[hosts.worker]
memory_max="16G"
''')
            pol = policy.load(b'schema=2\nactors=["alice"]\n[repositories."example/project"]\nid=11\nvisibility="private"\n')
            store = scheduler_store.Store(':memory:')
            self.addCleanup(store.close)
            api = mock.Mock()
            api.source.side_effect = lambda s, p: dict(s, admission={})
            app = scheduler.Scheduler(inv, str(etc), store, api)
            with mock.patch.object(github_coordinator, 'load_policy', return_value=pol):
                with self.assertRaisesRegex(protocol.Error, 'no configured'):
                    app.submit(source(), plan(device='slurm'))
                submitted = app.submit(source(), plan(cores=12))
                store.heartbeat('worker', [dict(tag=n,online=True,busy=False) for n,_,_ in setup.runners(inv)])
                task_id = store.batch(submitted['batch_id'])['tasks'][0]['id']
                store.request_task_allocation(submitted['batch_id'], task_id, 1)
                a = store.reserve()
                self.assertEqual(submitted['task_count'], 1)
                self.assertEqual((a['grant']['cores'],a['grant']['RAM'],a['grant']['workers']), (12,'12G',3))
                with self.assertRaisesRegex(protocol.Error, 'authorization'):
                    app.call('claim', {'grant':dict(a['grant'], workers=4)}, 'worker')
                api.verify_claim.assert_not_called()
                self.assertEqual(store.batch(submitted['batch_id'])['state'], 'pending')


class GitHubProtocol(unittest.TestCase):
    def setUp(self):
        self.store = scheduler_store.Store(':memory:')
        self.addCleanup(self.store.close)
        self.store.configure({'3090a': 4}, [dict(tag=name, pool='3090a', host='worker') for name in protocol.runner_names('3090a')])
        self.store.heartbeat('worker', [dict(tag=name, online=True, busy=False) for name in protocol.runner_names('3090a')])
        self.api = github_coordinator.GitHub('unused')
        self.api.api = mock.Mock()

    def allocation(self):
        batch_id = self.store.submit(source(), plan(), {'test-0': ['3090a']})
        task = self.store.batch(batch_id)['tasks'][0]
        self.store.request_task_allocation(batch_id, task['id'], 1)
        return self.store.reserve()

    def execution_job(self, allocation, **changes):
        return dict(id=100, run_id=allocation['run_id'],
                    name='CPU test / first / '+protocol.execution_job_name(allocation['task'], allocation['task_attempt']),
                    runner_name=allocation['runner'], labels=['self-hosted', allocation['runner']], **changes)

    def test_claim_needs_the_expected_job_runner_and_attempt(self):
        allocation = self.allocation()
        job = self.execution_job(allocation, status='in_progress')
        self.api.run_attempt = mock.Mock(return_value={'status': 'in_progress'})
        self.api.jobs = mock.Mock(return_value=[job])
        self.assertEqual(self.api.verify_claim(allocation, 1, 1), 100)
        for key, value in [('name', 'execute/another/1'), ('runner_name', '3090a-3'), ('runner_name', ''),
                           ('run_id', 2), ('status', 'completed'), ('labels', ['ci-cpu'])]:
            self.api.jobs.return_value = [dict(job, **{key: value})]
            with self.subTest(key=key), self.assertRaises(protocol.Error):
                self.api.verify_claim(allocation, 1, 1)
        self.api.jobs.return_value = [job]
        with self.assertRaises(protocol.Error):
            self.api.verify_claim(allocation, 1, 2)

    def test_source_identity_uses_the_original_actor_and_sha(self):
        pol = policy.load(b'schema=2\nactors=["alice"]\n[repositories."example/project"]\nid=11\nvisibility="private"\n')
        repo = {'full_name':'example/project', 'id':11}
        run = dict(run_attempt=1, repository=repo, head_repository=repo, event='push',
                   actor={'login':'alice'}, triggering_actor={'login':'alice'}, head_sha='a'*40, head_branch='topic')
        self.api.api.return_value = run
        admitted = self.api.source(source(), pol)
        self.assertEqual(admitted['admission']['env']['GITHUB_ACTOR'], 'alice')
        self.assertEqual(admitted['concurrency_key'], 'branch:topic')
        for changed in ({'head_sha':'b'*40}, {'head_repository':dict(repo, id=12)},
                        {'triggering_actor':{'login':'unknown'}}, {'conclusion':'cancelled'}):
            self.api.api.return_value = dict(run, **changed)
            with self.assertRaises(protocol.Error):
                self.api.source(source(), pol)

    def test_numbered_runner_hook_requires_allocation_and_original_admission(self):
        pol = policy.load(b'schema=2\nactors=["alice"]\n[repositories."example/project"]\nid=11\nvisibility="private"\n')
        allocation = self.allocation()
        env = dict(RUNNER_NAME=allocation['runner'], GITHUB_REPOSITORY='example/project', GITHUB_REPOSITORY_ID='11',
                   GITHUB_EVENT_NAME='push', GITHUB_RUN_ID='1', GITHUB_RUN_ATTEMPT='1')
        event = {'repository': {'full_name': 'example/project', 'id': 11}}
        original = dict(env, GITHUB_ACTOR='alice', GITHUB_TRIGGERING_ACTOR='alice',
                        CI_RUNNER_CLASS='plan', CI_RUNNER_AUDIENCE='private')
        allocation['source']['admission'] = {'env': original, 'event': {**event, 'pusher': {'name': 'alice'}}}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(transport.Client, 'call', return_value=allocation) as call:
            (Path(tmp)/'inventory.toml').write_text(inv_text(tmp))
            self.assertTrue(hook.central_decision(tmp, pol, env, event).accepted)
            self.assertNotIn('grant', call.call_args.kwargs)
            for key, value in [('RUNNER_NAME', '3090a-3'), ('GITHUB_RUN_ID', '2'),
                               ('GITHUB_RUN_ATTEMPT', '2'), ('GITHUB_EVENT_NAME', 'pull_request_target')]:
                with self.subTest(key=key):
                    self.assertFalse(hook.central_decision(tmp, pol, dict(env, **{key: value}), event).accepted)
            original['GITHUB_TRIGGERING_ACTOR'] = 'unknown'
            self.assertFalse(hook.central_decision(tmp, pol, env, event).accepted)
            call.side_effect = OSError('plan offline')
            self.assertFalse(hook.central_decision(tmp, pol, env, event).accepted)

    def test_a_failed_plan_cannot_be_overwritten_by_delayed_pending(self):
        self.store.notice(source(), 'pending', 'building')
        self.store.notice(source(), 'failure', 'failed')
        github_coordinator.WorkflowCoordinator(self.store, self.api, clock=lambda: 1000.).send_one()
        self.assertEqual(self.api.api.call_args.args[2]['state'], 'failure')

    def test_missing_artifact_and_source_cancellation_are_reconciled(self):
        allocation = self.allocation()
        self.store.claim(allocation['id'], 'worker', allocation['runner'], 'example/project', 1, 1, job_id=100)
        self.store.event('worker', 'release', allocation['id'], 'released', {'state': 'success'})
        self.api.run_attempt = mock.Mock(return_value={'status': 'in_progress'})
        self.api.jobs = mock.Mock(return_value=[self.execution_job(allocation, status='completed', conclusion='success')])
        self.api.artifacts = mock.Mock(return_value=[])
        coordinator = github_coordinator.WorkflowCoordinator(self.store, self.api)
        coordinator.reconcile()
        self.assertEqual(self.store.batch(allocation['batch_id'])['state'], 'error')
        self.store.notice(source(2), 'pending', 'building')
        self.api.run_attempt.return_value = {'status': 'completed', 'conclusion': 'cancelled'}
        coordinator.reconcile()
        self.assertEqual(self.store.notices()[0]['state'], 'error')


class LocalExecution(unittest.TestCase):
    def test_local_resource_verification_can_be_disabled(self):
        grant = {'allocation_id': 'one', 'backend': 'local', 'cores': 4,
                 'cpu_groups': 1, 'RAM': '4G', 'workers': 1, 'minutes': 1}
        with (tempfile.TemporaryDirectory() as record, mock.patch.dict(os.environ),
              mock.patch.object(task_execution, 'resource_limits', side_effect=AssertionError('unexpected resource check')),
              mock.patch('ci_runners.part.supervise', return_value=0) as run):
            self.assertEqual(task_execution.supervise(record, grant, [], 'true', verify_resource_limits=False), 0)
            self.assertEqual(json.loads((Path(record)/'allocation-ready.json').read_text())['memory_enforcement'],
                             'unverified')
            run.assert_called_once()

    def test_resource_reader_distinguishes_unified_legacy_and_hybrid_cgroups(self):
        layouts = {
            'unified': ('0::/task/child\n', {
                'task/child/cpu.max': '800000 100000', 'task/cpu.max': '300000 100000',
                'task/child/memory.max': str(8 << 30), 'task/memory.max': str(4 << 30)}),
            'legacy': ('3:cpu,cpuacct:/task/child\n2:cpuset:/task\n1:memory:/daemon\n', {
                'cpu/task/child/cpu.cfs_quota_us': '-1', 'cpu/task/cpu.cfs_quota_us': '300000',
                'cpu/task/cpu.cfs_period_us': '100000', 'memory/daemon/memory.limit_in_bytes': str(4 << 30)}),
            'hybrid': ('3:cpu,cpuacct:/task/child\n2:cpuset:/task\n1:memory:/daemon\n0::/daemon\n', {
                'cpu/task/child/cpu.cfs_quota_us': '300000', 'cpu/task/child/cpu.cfs_period_us': '100000',
                'memory/daemon/memory.limit_in_bytes': str(4 << 30)}),
        }
        for layout, (membership, files) in layouts.items():
            with self.subTest(layout=layout), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                cgroup_file = root/'proc/self/cgroup'
                cgroup_file.parent.mkdir(parents=True)
                cgroup_file.write_text(membership)
                for relative, value in files.items():
                    path = root/'sys/fs/cgroup'/relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(value)
                with mock.patch.object(task_execution, 'Path', side_effect=lambda path: root/str(path).lstrip('/')):
                    quota, memory = task_execution.resource_limits()
                self.assertEqual((quota, memory), (3, 4 << 30))

    def test_slurm_reads_inherited_cpuset_width_without_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            membership = root/'proc/self/cgroup'
            membership.parent.mkdir(parents=True)
            membership.write_text('2:cpuset:/task/child\n0::/daemon\n')
            child = root/'sys/fs/cgroup/cpuset/task/child/cpuset.cpus'
            child.parent.mkdir(parents=True)
            child.write_text('')
            (child.parent.parent/'cpuset.cpus').write_text('10-13')
            with mock.patch.object(task_execution, 'Path', side_effect=lambda path: root/str(path).lstrip('/')):
                self.assertEqual(task_execution.slurm_cpu_width(), 4)

    def test_a_delayed_scope_cannot_launch_after_groups_are_released(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp)/'ledger')
            ledger = executor_store.Ledger(db, [0])
            self.addCleanup(ledger.close)
            grant = dict(allocation_schema=1, allocation_id='one', task_id='one', host='worker',
                         device='3090a', backend='local', requested_device='', runner_tag='3090a-0',
                         cpu_groups=1, cores=4, RAM='4G', workers=7, minutes=60)
            ledger.reserve(grant, {})
            task_execution.check_fence(db, grant, [])
            ledger.release('one', {'state':'error'}, cleaned=True, verify=lambda:False)
            self.assertEqual(ledger.get('one')['state'], 'reserved')
            ledger.release('one', {'state':'error'}, cleaned=True, verify=lambda:True)
            with self.assertRaisesRegex(protocol.Error, 'closed'):
                task_execution.check_fence(db, grant, [])

    def test_resource_guard_checks_cpu_quota_and_aggregate_memory(self):
        grant = {'cores': 4, 'RAM': '4G'}
        task_execution.check_limits(grant, 4, 4 << 30)
        task_execution.check_limits(grant, 2, 3 << 30)
        for quota, memory in [(None, 4 << 30), (5, 4 << 30), (0, 4 << 30),
                              (4, None), (4, 5 << 30), (4, 0)]:
            with self.subTest(quota=quota, memory=memory), self.assertRaises(protocol.Error):
                task_execution.check_limits(grant, quota, memory)

    def test_upgrade_from_cpu_number_ledger_requires_draining(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp)/'ledger')
            ledger = executor_store.Ledger(path, [0])
            grant = dict(allocation_schema=1, allocation_id='old', task_id='old', host='worker',
                         device='3090a', backend='local', requested_device='', runner_tag='3090a-0',
                         cpu_groups=1, cores=4, RAM='4G', workers=1, minutes=1)
            ledger.reserve(grant, {})
            ledger.db.execute('UPDATE config SET groups_json=? WHERE id=1', ('[[0,1,2,3]]',))
            with self.assertRaisesRegex(protocol.Error, 'drain'):
                executor_store.Ledger(path, [0])
            ledger.release('old', {'state': 'success'}, cleaned=True)
            ledger.close()
            upgraded = executor_store.Ledger(path, [0])
            self.addCleanup(upgraded.close)
            self.assertEqual(upgraded.db.execute('SELECT groups_json FROM config').fetchone()[0], '[0]')

    def test_ci_work_path_and_cleanup_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            inv = inventory.load(inv_text(tmp))
            root = Path(inv.host['data'])/'ci-work'/'3090a-0'
            workspace = root/'project'
            workspace.mkdir(parents=True)
            self.assertEqual(executor.safe_workspace(inv, '3090a-0', str(workspace)), str(workspace))
            with self.assertRaises(protocol.Error):
                executor.safe_workspace(inv, '3090a-1', str(workspace))
            ledger = executor_store.Ledger(str(Path(tmp)/'ledger'), executor.groups(inv))
            self.addCleanup(ledger.close)
            run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, 'LoadState=loaded\nActiveState=active\n', ''))
            app = executor.ExecutorService(inv, str(Path(tmp)/'etc'), ledger, mock.Mock(), run=run)
            grant = dict(allocation_schema=1, allocation_id='one', task_id='one', host='worker',
                         device='3090a', backend='local', requested_device='', runner_tag='3090a-0',
                         cpu_groups=1, cores=4, RAM='4G', workers=8, minutes=600)
            row = ledger.reserve(grant, {'env':{'GITHUB_RUN_ID':'100','GITHUB_RUN_ATTEMPT':'1'}, 'part':{'id':'t'}})
            ledger.start_worker('one', 123, 1., str(Path(tmp)/'record'))
            with mock.patch('ci_runners.slots.alive', return_value=False):
                app.poll_local()
                self.assertEqual(ledger.get('one')['state'], 'starting')
                run.return_value = subprocess.CompletedProcess([], 0, 'LoadState=not-found\nActiveState=inactive\n', '')
                app.poll_local()
            self.assertEqual(ledger.get('one')['state'], 'released')
            self.assertEqual(ledger.get('one')['result']['state'], 'error')

    def test_workers_are_forwarded_without_clamping_and_ack_follows_limits(self):
        grant = {'allocation_id':'one', 'cores':4, 'cpu_groups':1, 'RAM':'4G', 'workers':7, 'minutes':600}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ):
            with (mock.patch.object(task_execution, 'resource_limits', return_value=(4, 4 << 30)),
                  mock.patch('os.sched_setaffinity') as bind,
                  mock.patch('os.sched_getaffinity', return_value={2, 4, 6, 8, 10}),
                  mock.patch('ci_runners.part.supervise', return_value=0) as run):
                self.assertEqual(task_execution.supervise(tmp, grant, [], 'true'), 0)
                self.assertEqual((os.environ['CI_WORKERS'], os.environ['CI_CORES'], os.environ['CI_CPUS']),
                                 ('7', '4', '2,4,6,8,10'))
                self.assertEqual(run.call_args.args[1], 36000)
                ready = json.loads((Path(tmp)/'allocation-ready.json').read_text())
                self.assertEqual((ready['workers'], ready['cores']), (7, 4))
                bind.assert_not_called()
            for quota, memory in [(4, 8 << 30), (5, 4 << 30), (None, 4 << 30)]:
                with tempfile.TemporaryDirectory() as denied, \
                     mock.patch.object(task_execution, 'resource_limits', return_value=(quota, memory)), \
                     mock.patch('ci_runners.part.supervise') as run:
                    self.assertEqual(task_execution.supervise(denied, grant, [], 'true'), 2)
                    run.assert_not_called()
                    self.assertFalse((Path(denied)/'allocation-ready.json').exists())


class SlurmResources(unittest.TestCase):
    def test_slurm_uses_the_existing_aggregate_memory_supervisor(self):
        grant = {'allocation_id': 'one', 'backend': 'slurm', 'cores': 4,
                 'cpu_groups': 1, 'RAM': '4G', 'workers': 7, 'minutes': 5}
        with (tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ),
              mock.patch.object(task_execution, 'resource_limits', return_value=(None, 1 << 63)),
              mock.patch.object(task_execution, 'slurm_cpu_width', return_value=4),
              mock.patch('os.sched_setaffinity') as bind,
              mock.patch('ci_runners.part.supervise', return_value=0) as supervise):
            self.assertEqual(task_execution.supervise(tmp, grant, [], 'true'), 0)
            self.assertEqual(supervise.call_args.kwargs['cap'], 4 << 30)
            ready = json.loads((Path(tmp)/'allocation-ready.json').read_text())
            self.assertEqual(ready['memory_enforcement'], 'process-tree')
            self.assertEqual(ready['RAM'], '4G')
            bind.assert_not_called()

    def test_supervisor_memory_limit_does_not_relax_cpu_or_local_resource_checks(self):
        grant = {'backend': 'slurm', 'cores': 4, 'RAM': '4G'}
        task_execution.check_limits(grant, None, None, slurm_width=4, supervisor_memory_cap=4 << 30)
        task_execution.check_limits(grant, 4, None, supervisor_memory_cap=4 << 30)
        for backend, quota, width, cap in [('local', None, 4, 4 << 30),
                                            ('local', 4, 4, 4 << 30),
                                            ('slurm', None, 5, 4 << 30),
                                            ('slurm', None, 4, 8 << 30)]:
            with self.subTest(backend=backend, quota=quota, width=width, cap=cap), self.assertRaises(protocol.Error):
                task_execution.check_limits(dict(grant, backend=backend), quota, None,
                                            slurm_width=width, supervisor_memory_cap=cap)

    def test_v2_target_resolves_through_the_existing_slurm_driver(self):
        grant = dict(allocation_schema=1, allocation_id='one', task_id='one', host='worker',
                     device='slurm', backend='slurm', requested_device='slurm', runner_tag='slurm0',
                     cpu_groups=1, cores=4, RAM='4G', workers=7, minutes=1)
        allocation = {'grant': grant, 'source': source(slurm_target='cluster1/cpu'),
                      'part': {'id': 'test', 'command': 'true', 'cores': 4, 'workers': 7},
                      'deadline': 1e12}
        env = dict(GITHUB_REPOSITORY='example/project', GITHUB_RUN_ID='1',
                   GITHUB_RUN_ATTEMPT='1', RUNNER_NAME='slurm0', CI_RUNNER_CLASS='executor')
        with tempfile.TemporaryDirectory() as tmp:
            inv = inventory.load(inv_text(tmp, device='slurm'))
            Path(inv.host['data']).mkdir()
            etc = Path(tmp)/'etc'
            (etc/'projects').mkdir(parents=True)
            (etc/'projects/project.toml').write_text(
                'name="project"\nrepository="example/project"\ncontext="project/ci"\n'
                '[hosts.worker]\nmemory_max="8G"\n')
            (etc/'slurm').mkdir()
            for site in ('cluster1', 'cluster2'):
                (etc/'slurm'/f'{site}.toml').touch()
            with (mock.patch.object(task_execution.part.Executor, '_slurm', return_value=(0, {})) as submit,
                  mock.patch.object(task_execution.signal, 'signal'),
                  mock.patch.object(task_execution.shutil, 'disk_usage', return_value=mock.Mock(free=8 << 30))):
                task_execution.execute(inv, str(etc), env, allocation, [])
            submitted_target = submit.call_args.args[0].target
            self.assertEqual(slurmpart.site_path(str(etc), submitted_target),
                             (str(etc/'slurm/cluster1.toml'), 'cpu'))

    def test_late_slurm_driver_must_check_the_local_allocation(self):
        driver = slurmpart.Driver.__new__(slurmpart.Driver)
        driver.plan = {'deadline':1000, 'central':True, 'central_fence':'unused', 'request':{'allocation':{}}}
        driver._drive = mock.Mock(return_value=0)
        with mock.patch.object(task_execution, 'check_fence', side_effect=protocol.Error('closed')):
            with self.assertRaises(protocol.Error):
                driver.drive()
            driver._drive.assert_not_called()
        with mock.patch.object(task_execution, 'check_fence') as check:
            self.assertEqual(driver.drive(), 0)
            check.assert_called_once_with('unused', {}, [])

    def test_slurm_request_and_budget_use_authorized_cpu_count(self):
        site = slurmsite.load((ROOT/'setup/slurm/site.example.toml').read_text())
        account = slurmsite.account_entry(site, 'acct1')
        grant = dict(allocation_schema=1, allocation_id='one', task_id='one', host='worker', device='slurm',
                     backend='slurm', requested_device='slurm', runner_tag='slurm0', cpu_groups=1,
                     cores=4, RAM='4G', workers=7, minutes=60)
        from tests.test_slurmgate import request
        req = request(cores=4, workers=7, allocation=grant, memory='4G', minutes=60)
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp)/'envs/p-cpu').mkdir(parents=True)
            checked = slurmgate.check_request(req, account, tmp)
            script = slurmgate.render(account, checked, 'o/r/7-1/p1-1', tmp, str(ROOT))
            self.assertIn('--cpus-per-task=4', script)
            self.assertIn('--mem=4096M', script)
            with self.assertRaises(slurmgate.GateError):
                slurmgate.check_request(dict(req, workers=4), account, tmp)
        limited = dataclasses.replace(site, site={**site.site, 'daily_cap':.3})
        self.assertEqual(slurmpart.budget_refusal(limited, 'acct1', 'cpu', 0, 60, {}, cores=4), '')
        self.assertIn('budget:', slurmpart.budget_refusal(limited, 'acct1', 'cpu', 0, 60, {}, cores=8))


if __name__ == '__main__':
    unittest.main()
