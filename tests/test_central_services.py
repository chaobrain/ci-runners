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

from ci_runners import (allocated_part, central, control, cpu_groups, dispatch,
                        executor_service, flow, hook, inventory, policy, scheduler, scheduler_store, setup,
                        slurmgate, slurmpart, slurmsite)
from tests import yamlsub
from tests.test_central import plan, source

ROOT = Path(__file__).resolve().parents[1]


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


class Registration(unittest.TestCase):
    def test_executor_setup_plan_is_opt_in_and_lists_the_service(self):
        from tests.test_setup import Setup
        fixture = Setup()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        (fixture.etc/'inventory.toml').write_text(inv_text(fixture.tmp.name))
        (fixture.etc/'projects/p.toml').write_text('name="p"\n[hosts.worker]\nmemory_max="80G"\n')
        with mock.patch.object(setup, 'linger_on', return_value=True):
            text = setup.build(str(fixture.etc)).text()
        self.assertIn('ci-executor.service', text)
        self.assertIn('enable --now ci-executor.service', text)
        self.assertIn('labels 3090a-0', text)
        self.assertIn('CI_RUNNER_CLASS=executor', text)
        self.assertNotIn('labels ci-cpu', text)
        self.assertEqual(fixture.ran, [])

    def test_inventory_and_setup_use_numbered_runners_and_explicit_roles(self):
        example = inventory.load((ROOT/'setup/central/inventory.example.toml').read_text())
        pools, rs = scheduler.registration(example)
        self.assertEqual(pools, central.DEVICE_GROUPS)
        self.assertEqual(len(rs), 58)
        self.assertEqual(setup.runners(example), [('plan-0',['plan-0','ci-plan'],'plan'), ('plan-1',['plan-1','ci-plan'],'plan')])
        inv = inventory.load(inv_text('/example'))
        self.assertEqual(setup.runners(inv), [(f'3090a-{i}', [f'3090a-{i}'], 'executor') for i in range(4)])
        self.assertIn(('ci-executor.service', 'default.target'), setup.wanted(inv.host, ['3090a-0']))
        env = setup.env_file(inv, 'executor', '/example/etc', pool='cpu')
        self.assertIn('CI_RUNNER_CLASS=executor', env)
        self.assertNotIn('CI_RUNNER_SLOT', env)
        pools, runners = scheduler.registration(inv)
        self.assertEqual(pools, {'3090a': 4})
        self.assertEqual(len(runners), 4)
        for bad in (inv_text('/example', cpus='0-16'), inv_text('/example').replace('schema = 2', 'schema = 1')):
            with self.assertRaises(inventory.InventoryError):
                inventory.load(bad)
        with self.assertRaisesRegex(Exception, 'schema 2 requires'):
            flow.load(plan())

    def test_workflows_keep_original_sha_and_no_write_token_in_test_job(self):
        entry = yamlsub.load((ROOT/'.github/workflows/ci-v2.yml').read_text())
        work = yamlsub.load((ROOT/'.github/workflows/execute.yml').read_text())
        consumer = yamlsub.load((ROOT/'setup/consumer-ci-execute.yml').read_text())
        for doc in (entry, work):
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
        self.assertEqual(consumer['run-name'], 'ci-execute/${{ fromJSON(inputs.allocation_json).allocation_id }}')
        self.assertEqual(set(consumer['on']), {'workflow_dispatch'})


class Rpc(unittest.TestCase):
    def test_socket_authentication_and_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp)/'control.sock')
            app = lambda method, params, identity: [method, params, identity]
            server = control.unix_server(path, app)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                self.assertEqual(control.Client(path=path).call('capacity', n=4), ['capacity', {'n':4}, 'local'])
                with self.assertRaisesRegex(central.Error, 'already running'):
                    control.unix_server(path, app)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
            denied = control.unix_server(path, app, uids=[])
            thread = threading.Thread(target=denied.serve_forever, daemon=True)
            thread.start()
            try:
                with self.assertRaisesRegex(central.Error, 'not authorized'):
                    control.Client(path=path).call('capacity')
            finally:
                denied.shutdown()
                denied.server_close()
                thread.join()

    def test_credentials_cannot_be_links_public_files_or_http(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp)/'credential'
            f.write_text('example-secret')
            f.chmod(0o600)
            self.assertEqual(control.secret(str(f)), 'example-secret')
            link = Path(tmp)/'link'
            link.symlink_to(f)
            with self.assertRaises(OSError):
                control.secret(str(link))
            f.chmod(0o644)
            with self.assertRaises(central.Error):
                control.secret(str(f))
        with self.assertRaises(central.Error):
            control.Client(url='http://example.invalid')


class Admission(unittest.TestCase):
    def test_project_device_and_slurm_admission_cannot_be_bypassed(self):
        with tempfile.TemporaryDirectory() as tmp:
            inv = inventory.load(inv_text(tmp).replace('count = 4 } }', 'count = 4 }, slurm = { class="executor", device="slurm", count=20 } }'))
            etc = Path(tmp)/'etc'
            (etc/'projects').mkdir(parents=True)
            (etc/'projects/project.toml').write_text('''name="project"
repository="example/project"
context="project/ci"
execution_ref="trusted"
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
            with mock.patch.object(dispatch, 'load_policy', return_value=pol):
                with self.assertRaisesRegex(central.Error, 'no configured'):
                    app.submit(source(), plan(device='slurm'))
                submitted = app.submit(source(), plan(cores=12))
                store.heartbeat('worker', [dict(tag=n,online=True,busy=False) for n,_,_ in setup.runners(inv)])
                a = store.reserve()
                self.assertEqual(a['source']['execution_ref'], 'trusted')
                self.assertEqual((a['grant']['cores'],a['grant']['RAM'],a['grant']['workers']), (12,'12G',3))
                with self.assertRaisesRegex(central.Error, 'authorization'):
                    app.call('claim', {'grant':dict(a['grant'], workers=4)}, 'worker')
                api.verify_claim.assert_not_called()
                self.assertEqual(store.batch(submitted['batch_id'])['state'], 'pending')


class GitHubProtocol(unittest.TestCase):
    def setUp(self):
        self.store = scheduler_store.Store(':memory:')
        self.addCleanup(self.store.close)
        self.store.configure({'3090a':4}, [dict(tag=n, pool='3090a', host='worker') for n in central.runner_names('3090a')])
        self.store.heartbeat('worker', [dict(tag=n, online=True, busy=False) for n in central.runner_names('3090a')])
        self.api = dispatch.GitHub('unused', 'service-bot')
        self.api.api = mock.Mock()

    def allocation(self):
        s = dict(source(), execution_ref='main', execution_workflow='ci-execute.yml')
        self.store.submit(s, plan(), {'test-0':['3090a']})
        return self.store.reserve()

    def execution_run(self, a, **kw):
        return dict(id=100, run_attempt=1, event='workflow_dispatch', display_title='ci-execute/'+a['id'],
                    actor={'login':'service-bot'}, repository={'full_name':'example/project'},
                    path='.github/workflows/ci-execute.yml', **kw)

    def test_claim_needs_the_expected_actor_workflow_and_attempt(self):
        a = self.allocation()
        run = self.execution_run(a, status='in_progress')
        self.api.api.return_value = run
        self.api.verify_claim(a, 100, 1)
        for key, value in [('actor', {'login':'other'}), ('path', '.github/workflows/forged.yml'),
                           ('display_title', 'ci-execute/another'), ('run_attempt', 2), ('status', 'completed')]:
            self.api.api.return_value = dict(run, **{key:value})
            with self.subTest(key=key), self.assertRaises(central.Error):
                self.api.verify_claim(a, 100, 1)

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
            with self.assertRaises(central.Error):
                self.api.source(source(), pol)

    def test_numbered_runner_hook_requires_allocation_and_original_admission(self):
        pol = policy.load(b'schema=2\nactors=["alice"]\n[repositories."example/project"]\nid=11\nvisibility="private"\n')
        a = self.allocation()
        env = dict(RUNNER_NAME=a['runner'], GITHUB_REPOSITORY='example/project', GITHUB_REPOSITORY_ID='11',
                   GITHUB_EVENT_NAME='workflow_dispatch', GITHUB_RUN_ID='100', GITHUB_RUN_ATTEMPT='1')
        event = dict(repository={'full_name':'example/project','id':11},
                     inputs={'runner_tag':a['runner'],'allocation_json':central.dumps(a['grant'])})
        original = dict(env, GITHUB_EVENT_NAME='push', GITHUB_ACTOR='alice', GITHUB_TRIGGERING_ACTOR='alice',
                        CI_RUNNER_CLASS='plan', CI_RUNNER_AUDIENCE='private')
        admission = {'env':original,'event':{'repository':event['repository'], 'pusher':{'name':'alice'}}}
        a['source']['admission'] = admission
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(control.Client, 'call', return_value=a) as call:
            (Path(tmp)/'inventory.toml').write_text(inv_text(tmp))
            self.assertTrue(hook.central_decision(tmp, pol, env, event).accepted)
            bad = dict(event, inputs=dict(event['inputs'], runner_tag='3090a-3'))
            self.assertFalse(hook.central_decision(tmp, pol, env, bad).accepted)
            original['GITHUB_TRIGGERING_ACTOR'] = 'unknown'
            self.assertFalse(hook.central_decision(tmp, pol, env, event).accepted)
            call.side_effect = OSError('plan offline')
            self.assertFalse(hook.central_decision(tmp, pol, env, event).accepted)

    def test_a_failed_plan_cannot_be_overwritten_by_delayed_pending(self):
        s = source()
        self.store.notice(s, 'pending', 'building')
        self.store.notice(s, 'failure', 'failed')
        dispatcher = dispatch.Dispatcher(self.store, self.api, clock=lambda: 1000.)
        dispatcher.send_one()
        self.assertEqual(self.api.api.call_args.args[2]['state'], 'failure')

    def test_dispatch_binds_returned_run_and_ambiguous_delivery_holds_groups(self):
        a = self.allocation()
        pending = self.store.next_message()
        self.store.message_done(pending['id'])
        self.api.api.return_value = {'workflow_run_id':100}
        dispatch.Dispatcher(self.store, self.api).send_one()
        self.assertEqual(self.store.allocation(a['id'])['run_id'], 100)
        body = self.api.api.call_args.args[2]
        self.assertIs(body['return_run_details'], True)
        self.assertEqual(json.loads(body['inputs']['allocation_json'])['workers'], 3)
        self.store.submit(dict(source(2), execution_ref='main', execution_workflow='ci-execute.yml'),
                          plan(), {'test-0':['3090a']})
        b = self.store.reserve()
        pending = self.store.next_message()
        self.store.message_done(pending['id'])
        self.api.api.side_effect = OSError('response lost')
        dispatch.Dispatcher(self.store, self.api).send_one()
        self.assertEqual(self.store.allocation(b['id'])['released'], 0)
        self.assertEqual(self.store.capacity()[0]['groups_free'], 2)

    def test_missing_artifact_and_source_cancellation_are_reconciled(self):
        a = self.allocation()
        self.store.claim(a['id'], 'worker', a['runner'], 'example/project', 100, 1)
        self.store.event('worker', 'release', a['id'], 'released', {'state':'success'})
        self.api.runs = mock.Mock(return_value=[self.execution_run(a, status='completed', conclusion='success')])
        self.api.api.return_value = {'artifacts': []}
        dispatch.Dispatcher(self.store, self.api).reconcile()
        self.assertEqual(self.store.batch(a['batch_id'])['state'], 'error')
        self.store.notice(source(2), 'pending', 'building')
        self.api.api.return_value = {'status':'completed', 'conclusion':'cancelled'}
        dispatch.Dispatcher(self.store, self.api).reconcile()
        self.assertEqual(self.store.notices()[0]['state'], 'error')


class LocalExecution(unittest.TestCase):
    def test_a_delayed_scope_cannot_launch_after_groups_are_released(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp)/'ledger')
            ledger = cpu_groups.Ledger(db, [[0,1,2,3]])
            self.addCleanup(ledger.close)
            grant = dict(allocation_schema=1, allocation_id='one', task_id='one', host='worker',
                         device='3090a', backend='local', requested_device='', runner_tag='3090a-0',
                         cpu_groups=1, cores=4, RAM='4G', workers=7, minutes=60)
            ledger.reserve(grant, {})
            allocated_part.check_fence(db, grant, [0,1,2,3])
            ledger.release('one', {'state':'error'}, cleaned=True, verify=lambda:False)
            self.assertEqual(ledger.get('one')['state'], 'reserved')
            ledger.release('one', {'state':'error'}, cleaned=True, verify=lambda:True)
            with self.assertRaisesRegex(central.Error, 'closed'):
                allocated_part.check_fence(db, grant, [0,1,2,3])

    def test_resource_guard_checks_the_full_set_and_aggregate_memory(self):
        grant = {'cores':4, 'RAM':'4G'}
        allocated_part.check_limits(grant, [1,3,5,7], {1,3,5,7}, 4 << 30)
        for cpus, memory in [({0,1,2,3}, 4 << 30), ({1,3,5,7}, 5 << 30), ({1,3,5,7}, 0)]:
            with self.assertRaises(central.Error):
                allocated_part.check_limits(grant, [1,3,5,7], cpus, memory)

    def test_ci_work_path_and_cleanup_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            inv = inventory.load(inv_text(tmp))
            root = Path(inv.host['data'])/'ci-work'/'3090a-0'
            workspace = root/'project'
            workspace.mkdir(parents=True)
            self.assertEqual(executor_service.safe_workspace(inv, '3090a-0', str(workspace)), str(workspace))
            with self.assertRaises(central.Error):
                executor_service.safe_workspace(inv, '3090a-1', str(workspace))
            ledger = cpu_groups.Ledger(str(Path(tmp)/'ledger'), executor_service.groups(inv))
            self.addCleanup(ledger.close)
            run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, 'LoadState=loaded\nActiveState=active\n', ''))
            app = executor_service.ExecutorService(inv, str(Path(tmp)/'etc'), ledger, mock.Mock(), run=run)
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
            with mock.patch.object(allocated_part, 'resource_limits', return_value=({0,1,2,3}, 4 << 30)), \
                 mock.patch('os.sched_setaffinity'), mock.patch('os.sched_getaffinity', return_value={0,1,2,3}), \
                 mock.patch('ci_runners.part.supervise', return_value=0) as run:
                self.assertEqual(allocated_part.supervise(tmp, grant, [0,1,2,3], 'true'), 0)
                self.assertEqual(os.environ['CI_WORKERS'], '7')
                self.assertEqual(run.call_args.args[1], 36000)
                self.assertEqual(json.loads((Path(tmp)/'allocation-ready.json').read_text())['workers'], 7)
            with tempfile.TemporaryDirectory() as denied, \
                 mock.patch.object(allocated_part, 'resource_limits', return_value=({0,1,2,3}, 8 << 30)), \
                 mock.patch('ci_runners.part.supervise') as run:
                self.assertEqual(allocated_part.supervise(denied, grant, [0,1,2,3], 'true'), 2)
                run.assert_not_called()
                self.assertFalse((Path(denied)/'allocation-ready.json').exists())


class SlurmResources(unittest.TestCase):
    def test_late_slurm_driver_must_check_the_local_allocation(self):
        driver = slurmpart.Driver.__new__(slurmpart.Driver)
        driver.plan = {'deadline':1000, 'central':True, 'central_fence':'unused', 'request':{'allocation':{}}}
        driver._drive = mock.Mock(return_value=0)
        with mock.patch.object(allocated_part, 'check_fence', side_effect=central.Error('closed')):
            with self.assertRaises(central.Error):
                driver.drive()
            driver._drive.assert_not_called()
        with mock.patch.object(allocated_part, 'check_fence') as check:
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
