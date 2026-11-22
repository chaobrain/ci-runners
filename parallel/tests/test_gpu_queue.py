"""Schema 2 GPU scheduling, using synthetic physical UUIDs and temporary ledgers."""
import concurrent.futures
import io
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

from ci_runners import inventory, registry
from parallel import deploy, deployment_config, deployment_plan, executor_store, gpu_resources
from parallel import protocol, scheduler, scheduler_store, task_execution
from parallel.tests.test_central import plan, source
from parallel.tests.test_central_services import inv_text
from parallel.tests.test_deployment import DeploymentFixture
from tests import yamlsub

ROOT = Path(__file__).resolve().parents[2]
QUOTAS = {'pro6000': 3, '3090a': 2, '3090b': 2, 'h100': 3}


def card(number):
    return f'GPU-00000000-0000-0000-0000-{number:012x}'


def grant(aid, uuid=None):
    value = dict(allocation_schema=1, allocation_id=aid, task_id=aid, host='worker',
                 device='3090a', backend='local', runner_tag='3090a-0', requested_device='3090a',
                 cpu_groups=1, cores=4, RAM='4G', workers=3, minutes=1)
    if uuid is not None:
        value.update(allocation_schema=2, gpu_uuid=uuid)
    return value


class Queue(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = str(Path(directory.name)/'central.sqlite')
        self.now = 1000
        self.store = self.open_store()
        self.pools = dict(protocol.DEVICE_GROUPS)
        self.runners = [dict(tag=name, pool=device, host=device)
                        for device in self.pools for name in protocol.runner_names(device)]
        self.cards = {}
        sequence = iter(range(1, 11))
        for device, count in QUOTAS.items():
            self.cards[device] = [card(next(sequence)) for _ in range(count)]
        self.store.configure(self.pools, self.runners, self.cards)
        for host in self.pools:
            self.heartbeat(host)

    def open_store(self):
        store = scheduler_store.Store(self.path, clock=lambda: self.now)
        self.addCleanup(store.close)
        return store

    def statuses(self, host):
        return [dict(tag=r['tag'], online=True, busy=False) for r in self.runners if r['host'] == host]

    def heartbeat(self, host, **options):
        options.setdefault('gpu_free_mib', dict.fromkeys(self.cards.get(host, []), 8192))
        self.store.heartbeat(host, self.statuses(host), **options)

    def submit(self, run=1, count=1, device='', gpu=True, choices=None, **limits):
        document = plan(count, device=device)
        for part in document['parts']:
            part['gpu'] = gpu
        if choices is None:
            choices = [device] if device else list(self.cards)
        batch = self.store.submit(source(run), document, {p['id']: choices for p in document['parts']}, **limits)
        for task in self.store.batch(batch)['tasks']:
            self.store.request_task_allocation(batch, task['id'], 1)
        return batch

    def capacity(self, device):
        return next(row for row in self.store.capacity() if row['device'] == device)

    def release(self, row, event='cleanup'):
        self.store.event(row['host'], event, row['id'], 'released', {'state': 'success'})

    def test_ten_concurrent_grants_and_the_eleventh_waits_without_holding_cpus(self):
        batch = self.submit(count=11)
        other = self.open_store()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as workers:
            reservations = list(workers.map(lambda s: s.reserve(), [self.store, other] * 6))
        rows = [a for a in reservations if a]
        self.assertEqual(Counter(a['pool'] for a in rows), QUOTAS)
        self.assertEqual(len({a['grant']['gpu_uuid'] for a in rows}), 10)
        for row in rows:
            self.assertIn(protocol.validate_allocation(row['grant'])['gpu_uuid'], self.cards[row['host']])
        self.assertEqual(sum(r['groups_reserved'] for r in self.store.capacity()), 10)
        self.assertEqual(sum(t['state'] == 'queued' for t in self.store.batch(batch)['tasks']), 1)
        first = rows[0]
        self.store.claim(first['id'], first['host'], first['runner'], 'example/project', 1, 1, job_id=501)
        self.store.event(first['host'], 'ack', first['id'], 'allocated')
        self.assertEqual(self.capacity(first['host'])['gpus_allocated'], 1)
        self.store.runner_finished(first['id'], 1, 1, job_id=501, conclusion='success')
        self.assertIsNone(other.reserve())
        self.release(first)
        last = other.reserve()
        self.assertEqual(last['gpu_uuid'], first['gpu_uuid'])
        self.assertIsNone(self.store.reserve())

    def test_fifo_survives_restart_and_duplicate_or_late_release(self):
        self.submit(count=4, device='pro6000')
        first = self.store.reserve()
        self.release(first)
        self.assertFalse(self.store.allocation(first['id'])['runner_done'])
        second = self.store.reserve()
        self.release(second, 'cleanup2')
        self.release(first)
        self.release(first, 'duplicate')
        self.store.event(first['host'], 'delayed-ack', first['id'], 'allocated')
        self.store = self.open_store()
        self.store.configure(self.pools, self.runners, self.cards)
        self.assertIsNone(self.store.reserve())
        self.heartbeat('pro6000')
        third, fourth = self.store.reserve(), self.store.reserve()
        self.assertEqual([r['gpu_uuid'] for r in (first, second, third, fourth)],
                         [*self.cards['pro6000'], self.cards['pro6000'][0]])
        self.assertNotEqual(first['runner'], fourth['runner'])

    def test_pinned_gpu_wait_does_not_block_other_devices_or_cpu(self):
        self.submit(count=4, device='h100')
        for _ in range(3):
            self.assertEqual(self.store.reserve()['grant']['requested_device'], 'h100')
        self.assertIsNone(self.store.reserve())
        self.submit(run=2, device='h100', gpu=False)
        self.submit(run=3, choices=['3090b'])
        cpu, automatic = self.store.reserve(), self.store.reserve()
        self.assertEqual(cpu['grant']['allocation_schema'], 1)
        self.assertNotIn('gpu_uuid', cpu['grant'])
        self.assertEqual((automatic['pool'], automatic['grant']['requested_device']), ('3090b', ''))
        self.assertEqual(self.capacity('h100')['gpus_reserved'], 3)
        self.assertEqual(self.capacity('h100')['groups_reserved'], 4)
        self.assertIsNone(self.store.reserve())
        with self.assertRaises(protocol.Error):
            self.submit(run=4, choices=['slurm'])

    def test_memory_heartbeat_and_drain_gate_new_leases_but_keep_ownership(self):
        host = '3090a'
        one, two = self.cards[host]
        self.submit(device=host, count=2, gpu_mem_mib=6000)
        self.heartbeat(host, gpu_free_mib={one: 5999})
        self.assertIsNone(self.store.reserve())
        for invalid in ({card(100): 6000}, {two: True}, {self.cards['h100'][0]: 8192}):
            with self.subTest(observation=invalid), self.assertRaises(protocol.Error):
                self.heartbeat(host, gpu_free_mib=invalid)
        self.heartbeat(host, gpu_free_mib={one: 6000})
        first = self.store.reserve()
        self.assertEqual(first['gpu_uuid'], one)
        self.now += 121
        self.assertIsNone(self.store.reserve())
        self.assertEqual(self.capacity(host)['gpus_reserved'], 1)
        self.assertEqual(self.capacity(host)['gpus_unavailable'], 1)
        self.heartbeat(host, drained=True)
        self.assertIsNone(self.store.reserve())
        with self.assertRaisesRegex(protocol.Error, 'drain'):
            self.store.configure(self.pools, self.runners, {**self.cards, host: [two]})
        app = scheduler.Scheduler.__new__(scheduler.Scheduler)
        app.store = self.store
        params = {'runners': self.statuses(host)}
        self.assertEqual(app.call('heartbeat', params, host), [])
        self.assertIsNone(self.store.reserve())
        params['gpu_free_mib'] = {two: 6000}
        with self.assertRaises(protocol.Error):
            app.call('heartbeat', params, host)
        leases = app.call('heartbeat', dict(params, allocation_schema=2), host)
        self.assertEqual([lease['id'] for lease in leases], [first['id']])
        self.assertEqual(self.store.reserve()['gpu_uuid'], two)

    def test_mixed_tasks_share_batch_limit_and_retry_keeps_device(self):
        document = plan(2, device='3090a')
        document['parts'][0]['gpu'] = True
        batch = self.store.submit(source(), document, {p['id']: ['3090a'] for p in document['parts']}, max_parallel=1)
        tasks = {p['part']['id']: p['id'] for p in self.store.batch(batch)['tasks']}
        gpu = tasks['test-0']
        self.store.request_task_allocation(batch, gpu, 1)
        row = self.store.reserve()
        self.store.request_task_allocation(batch, tasks['test-1'], 1)
        self.assertIsNone(self.store.reserve())
        self.store.event('3090a', 'failed', row['id'], 'released', {'state': 'error', 'retryable': True})
        self.store.runner_finished(row['id'], 1, 1, job_id=100, conclusion='failure')
        cpu = self.store.reserve()
        self.assertNotIn('gpu_uuid', cpu['grant'])
        self.store.request_task_allocation(batch, gpu, 2)
        self.assertIsNone(self.store.reserve())
        self.release(cpu)
        self.store.runner_finished(cpu['id'], 1, 1, job_id=101, conclusion='success')
        retry = self.store.reserve()['grant']
        self.assertEqual((retry['requested_device'], retry['device'], retry['task_attempt']), ('3090a', '3090a', 2))
        self.assertEqual(retry['gpu_uuid'], self.cards['3090a'][1])
        self.store.event('3090a', 'late-first-ack', row['id'], 'allocated')
        self.store.event('3090a', 'repeat-first-cleanup', row['id'], 'released', {'state': 'error', 'retryable': True})
        self.assertEqual(self.store.task_status(batch, gpu, 2)['state'], 'reserved')
        self.assertEqual(self.capacity('3090a')['gpus_reserved'], 1)


class Executor(unittest.TestCase):
    def test_local_gpu_and_cpu_reservations_are_atomic_and_require_verified_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp)/'local.sqlite')
            groups = list(range(2))
            first = grant('first', card(1))
            second = grant('second', card(2))
            ledger = executor_store.Ledger(path, groups, [card(1), card(2)])
            cpus = ledger.reserve(first, {})['cpus']
            ledger.close()
            ledger = executor_store.Ledger(path, groups, [card(2), card(1)])
            self.addCleanup(ledger.close)
            task_execution.check_fence(path, first, cpus)
            for invalid in (grant('duplicate', card(1)), grant('foreign', card(7))):
                with self.assertRaises(protocol.Error):
                    ledger.reserve(invalid, {})
            with self.assertRaisesRegex(protocol.Error, 'drain'):
                executor_store.Ledger(path, groups, [card(2)])
            self.assertEqual(ledger.reserve(second, {})['cpus'], [])
            ledger.release('first', {'state': 'success'}, cleaned=True, verify=lambda: False)
            self.assertEqual(ledger.get('first')['state'], 'reserved')
            ledger.release('first', {'state': 'success'}, cleaned=True, verify=lambda: True)
            ledger.release('first', {'state': 'success'}, cleaned=True)
            self.assertEqual([r['id'] for r in ledger.reports()], ['first-released'])
            with self.assertRaises(protocol.Error):
                task_execution.check_fence(path, first, cpus)
            self.assertEqual(ledger.reserve(grant('next', card(1)), {})['cpus'], cpus)
            ledger.db.execute('DELETE FROM held_gpus WHERE allocation=?', ('next',))
            with self.assertRaisesRegex(protocol.Error, 'GPU'):
                task_execution.check_fence(path, grant('next', card(1)), cpus)

    def test_cancel_before_launch_without_the_card_preserves_other_leases(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = executor_store.Ledger(str(Path(tmp)/'local.db'), [0], [card(1)])
            self.addCleanup(ledger.close)
            ledger.reserve(grant('running', card(1)), {})
            late = grant('late', card(9))
            for _ in range(2):
                ledger.cancel_or_close_unstarted(late, {'state': 'cancelled'})
            self.assertFalse(ledger.start_worker('late', 12, 1., 'record'))
            self.assertEqual(ledger.reserve(late, {})['state'], 'released')
            self.assertEqual([row['id'] for row in ledger.active()], ['running'])
            self.assertEqual([row['id'] for row in ledger.reports()], ['late-released'])
            ledger.cancel_or_close_unstarted(grant('running', card(1)), {'state': 'cancelled'})
            self.assertTrue(ledger.get('running')['cancelled'])
            self.assertFalse(ledger.start_worker('running', 13, 2., 'record'))
            self.assertEqual(ledger.db.execute('SELECT uuid FROM held_gpus').fetchone()[0], card(1))

    def test_visibility_and_memory_are_rechecked_before_acknowledgement(self):
        cpus = [0, 1, 2, 3]
        with (tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, CUDA_VISIBLE_DEVICES='0,1'),
              mock.patch.object(task_execution, 'resource_limits', return_value=(4, 4 << 30)),
              mock.patch('os.sched_setaffinity'), mock.patch('os.sched_getaffinity', return_value=set(cpus)),
              mock.patch.object(gpu_resources, 'available', return_value={card(1): 2048}) as available,
              mock.patch.object(task_execution.part, 'supervise', return_value=0) as command):
            for aid, gpu, minimum, rc in [('cpu', None, 0, 0), ('gpu', card(1), 2048, 0),
                                          ('low', card(1), 2049, 2), ('missing', card(2), 0, 2)]:
                record = Path(tmp)/aid
                record.mkdir()
                command.reset_mock()
                self.assertEqual(task_execution.supervise(record, grant(aid, gpu), [], 'true',
                                                        gpu_mem_mib=minimum), rc)
                ready = record/'allocation-ready.json'
                if rc:
                    command.assert_not_called()
                    self.assertFalse(ready.exists())
                else:
                    command.assert_called_once()
                    self.assertEqual(os.environ['CUDA_VISIBLE_DEVICES'], gpu or '')
                    self.assertEqual(os.environ['CI_CORES'], '4')
                    self.assertEqual(json.loads(ready.read_text())['gpu_uuid'], gpu or '')
            self.assertEqual(available.call_count, 3)

    def test_probe_excludes_unknown_hot_busy_and_unreadable_cards(self):
        def output(text):
            return subprocess.CompletedProcess([], 0, text)
        status = '\n'.join([f'{card(1)}, 79, 1024', f'{card(2)}, 80, 2048', f'{card(3)}, 43, 8192',
                            f'{card(4)}, N/A, 4096', f'{card(5)}, 31, N/A', f'{card(6)}, 25, 100'])
        run = mock.Mock(side_effect=[output(status), output(card(3)+'\n')])
        cards = [card(i) for i in range(1, 6)]
        self.assertEqual(gpu_resources.available(cards, 80, run=run), {card(1): 1024})
        run.side_effect = [output(status), subprocess.TimeoutExpired('nvidia-smi', 5)]
        self.assertEqual(gpu_resources.available(cards, 80, run=run), {})
        run.side_effect = [output(status), output('[Unknown Error]')]
        self.assertEqual(gpu_resources.available(cards, 80, run=run), {})
        run.reset_mock()
        self.assertEqual(gpu_resources.available([], 80, run=run), {})
        run.assert_not_called()


class Contracts(unittest.TestCase):
    def test_upgrade_requires_drained_scheduler_and_delivered_executor_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp)/'central.sqlite')
            with sqlite3.connect(path) as db:
                db.executescript(scheduler_store.SCHEMA)
                db.execute("INSERT INTO batches(id,source,digest,created,state,max_parallel,retries,cancel_key) "
                           "VALUES('old',?,'hash',0,'success',1,1,'key')", (json.dumps(source()),))
                db.execute("INSERT INTO tasks(id,batch,part,eligible,state,created,deadline,wait_remaining) "
                           "VALUES('t','old',?,'[]','success',0,10,0)", (json.dumps(protocol.load_plan(plan())['parts'][0]),))
                db.execute("INSERT INTO allocations(id,task,pool,runner,host,groups,state,created,updated,released,runner_done) "
                           "VALUES('a','t','3090a','3090a-0','worker',1,'released',0,0,1,0)")
            with self.assertRaisesRegex(protocol.Error, 'drain'):
                scheduler_store.Store(path)
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute('SELECT version FROM meta').fetchone()[0], 2)
                db.execute("UPDATE allocations SET runner_done=1")
            store = scheduler_store.Store(path)
            self.addCleanup(store.close)
            self.assertEqual(store.allocation('a')['grant']['allocation_schema'], 1)
            self.assertIsNone(store.allocation('a')['gpu_uuid'])
            local = str(Path(tmp)/'local.sqlite')
            with sqlite3.connect(local) as db:
                db.execute('CREATE TABLE config(id INTEGER PRIMARY KEY, groups_json TEXT)')
                db.execute('CREATE TABLE reports(id TEXT PRIMARY KEY, allocation TEXT, kind TEXT, result TEXT, delivered INTEGER)')
                db.execute("INSERT INTO reports VALUES('a-released','a','released','{}',0)")
            with self.assertRaisesRegex(protocol.Error, 'deliver reports'):
                executor_store.Ledger(local, [], [])
            with sqlite3.connect(local) as db:
                db.execute('UPDATE reports SET delivered=1')
            ledger = executor_store.Ledger(local, [0], [card(1)])
            self.addCleanup(ledger.close)
            self.assertEqual(ledger.reserve(grant('new', card(1)), {})['cpus'], [])

    def test_execute_checks_local_uuid_and_requested_device_before_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            inv = inventory.load(inv_text(tmp).replace('gpus = []', f'gpus = []\ngpu_uuids = ["{card(1)}"]'))
            template = plan(device='3090a')['parts'][0]
            env = dict(GITHUB_REPOSITORY='example/project', GITHUB_RUN_ID='1', GITHUB_RUN_ATTEMPT='1',
                       RUNNER_NAME='3090a-0', GITHUB_WORKSPACE=tmp, RUNNER_TEMP=tmp)
            project = registry.parse(dict(name='project', repository='example/project', context='project/ci',
                                          devices=['3090a'], hosts={'worker': {'memory_max': '8G'}}), 'worker')
            with (mock.patch.object(registry, 'load_file', return_value=project) as load_project,
                  mock.patch.object(task_execution.signal, 'signal'), mock.patch.object(task_execution, 'envuse') as envuse,
                  mock.patch.object(task_execution.shutil, 'disk_usage', return_value=mock.Mock(free=8 << 30)),
                  mock.patch.object(task_execution.os, 'sched_getaffinity', return_value=set(range(4))),
                  mock.patch.object(task_execution.part.Executor, '_end', return_value=(0, {})),
                  mock.patch.object(task_execution.part.Executor, 'oom_policy', return_value=True)):
                issue = grant('a', card(1))
                task = dict(template, gpu=True)
                allocation = dict(grant=issue, part=task, source=source(), deadline=1e12)
                spawn = mock.Mock()
                for changes in (dict(grant=grant('a', card(2))), dict(part=dict(task, device='h100')),
                                dict(part=dict(task, gpu=False)), dict(grant=grant('a'))):
                    with self.assertRaisesRegex(protocol.Error, 'GPU or device'):
                        task_execution.execute(inv, tmp, env, dict(allocation, **changes), [], spawn=spawn)
                load_project.return_value = registry.parse(dict(name='project', repository='example/project',
                    context='project/ci', devices=['h100'], hosts={'worker': {'memory_max': '8G'}}), 'worker')
                with self.assertRaisesRegex(protocol.Error, 'GPU or device'):
                    task_execution.execute(inv, tmp, env, allocation, [], spawn=spawn)
                spawn.assert_not_called()
                load_project.return_value = project
                task_execution.execute(inv, tmp, env, allocation, [], spawn=spawn)
                self.assertEqual(spawn.call_args.kwargs['env']['CUDA_VISIBLE_DEVICES'], card(1))
                scope = spawn.call_args.args[0]
                self.assertIn('CPUQuota=400%', scope)
                self.assertFalse(any('AllowedCPUs' in item or item == 'taskset' for item in scope))
                scope_args = json.loads(scope[-1])
                self.assertEqual(scope_args[1]['gpu_uuid'], card(1))
                record = json.loads((Path(scope_args[0])/'record.json').read_text())
                self.assertEqual(record['cards'], [card(1)])
                envuse.hold.assert_not_called()

    def test_gpu_is_boolean_and_grants_identify_one_local_physical_card(self):
        document = plan()
        self.assertFalse(protocol.load_plan(document)['parts'][0].get('gpu', False))
        for invalid in (0, 1, 'true', None, [0]):
            document['parts'][0]['gpu'] = invalid
            with self.subTest(value=invalid), self.assertRaises(protocol.Error):
                protocol.load_plan(document)
        document['parts'][0].update(device='slurm', gpu=True)
        with self.assertRaises(protocol.Error):
            protocol.load_plan(document)
        gpu = grant('test', card(1))
        self.assertEqual(protocol.validate_allocation(gpu), gpu)
        for invalid in (dict(gpu, allocation_schema=1), dict(grant('cpu'), allocation_schema=2),
                        dict(gpu, gpu_uuid='0'), dict(gpu, gpu_uuid=[card(1)]),
                        dict(gpu, device='slurm', requested_device='', backend='slurm', runner_tag='slurm0')):
            with self.subTest(grant=invalid), self.assertRaises(protocol.Error):
                protocol.validate_allocation(invalid)

    def test_split_matrices_device_admission_and_waiter_job_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            inv = inventory.load(inv_text(tmp).replace('gpus = []', f'gpus = []\ngpu_uuids = ["{card(1)}"]'))
            etc = Path(tmp)/'etc'
            (etc/'projects').mkdir(parents=True)
            (etc/'projects/project.toml').write_text('name="project"\nrepository="example/project"\n'
                'context="project/ci"\ndevices=["3090a","h100"]\ngpu_mem_mib=6144\n[hosts.worker]\nmemory_max="16G"\n')
            store = scheduler_store.Store(':memory:')
            self.addCleanup(store.close)
            app = scheduler.Scheduler(inv, etc, store, mock.Mock())
            project = registry.load_file(etc, 'project', 'worker')
            document = plan(3)
            document['parts'][1]['gpu'] = True
            document['parts'][2].update(gpu=True, device='3090a')
            with mock.patch.object(app, 'prepare', return_value=(source(), project, None)):
                result = app.submit(source(), document)
                document['parts'][2]['device'] = 'h100'
                with self.assertRaisesRegex(protocol.Error, 'no configured'):
                    app.submit(source(), document)
            self.assertEqual((result['task_count'], result['cpu_task_count'], result['gpu_task_count']), (3, 1, 2))
            self.assertEqual([row['part_id'] for row in json.loads(result['cpu_task_matrix'])], ['test-0'])
            matrix = {row['part_id']: row for row in json.loads(result['gpu_task_matrix'])}
            self.assertEqual({name: row['gpu_lane'] for name, row in matrix.items()}, {'test-1': 'auto', 'test-2': '3090a'})
            task = matrix['test-2']['task_id']
            params = dict(batch_id=result['batch_id'], task_id=task, task_attempt=1, source_identity=source())
            job = {'name': protocol.allocation_job_name(task, 1), 'status': 'in_progress', 'labels': ['ci-gpu-wait-auto']}
            app.api.jobs.return_value = [job]
            with self.assertRaisesRegex(protocol.Error, 'lane'):
                app.request_allocation(params)
            job['labels'] = ['self-hosted', 'ci-gpu-wait-3090a']
            self.assertEqual(app.request_allocation(params)['gpu_mem_mib'], 6144)
            document['parts'][2]['device'] = '3090a'
            validation = dict(source(), context='project/plan-only')
            with mock.patch.object(app, 'prepare', return_value=(validation, project, None)):
                checked = app.submit(validation, document, plan_only=True)
            self.assertEqual((checked['batch_id'], checked['cpu_task_count'], checked['gpu_task_count']), ('', 0, 0))
            self.assertEqual(len(store.batches()), 1)


class Deployment(DeploymentFixture):
    def test_first_cutover_refuses_old_parallel_runner_units(self):
        config = self.config()
        reviewed = deployment_plan.build(config)
        base_run = self.fake_run
        def with_old_runner(command, **options):
            if 'list-units' in command and 'ci-parallel-*.service' in command:
                return subprocess.CompletedProcess(command, 0, 'ci-parallel-runner@previous.service active')
            return base_run(command, **options)
        self.fake_run = with_old_runner
        with self.assertRaisesRegex(ValueError, 'before cutover'):
            self.apply(reviewed)
        self.assertFalse(self.registrations)
        self.assertFalse((config.control/'state/deployment.json').exists())

    def test_inventory_only_and_five_waiters_without_registration(self):
        host = self.document['hosts']['3090a']
        host['gpu_uuids'] = [card(1), card(2)]
        self.write_inputs()
        (self.input/'projects/example.toml').unlink()
        config = deployment_config.load(self.input, '3090a', inventory_only=True)
        specs = {name: labels for name, labels, role in config.runner_specs() if role == 'plan'}
        lanes = ['pro6000', '3090a', '3090b', 'h100', 'auto']
        self.assertEqual(specs, {'plan-0': ['plan-0', 'ci-plan'], **{
            f'plan-{i}': [f'plan-{i}', 'ci-gpu-wait-'+lane] for i, lane in enumerate(lanes, 1)}})
        args = ['check', '--host', '3090a', '--inventory-only', '--input', str(self.input)]
        previous_mask = os.umask(0o022)
        self.addCleanup(os.umask, previous_mask)
        with (mock.patch.object(gpu_resources, 'available', return_value={card(1): 2000, card(2): 1000}) as probe,
              mock.patch.object(deployment_plan, 'check_host') as ordinary, mock.patch('sys.stdout', new_callable=io.StringIO) as out):
            self.assertEqual(deploy.main(args), 0)
            self.assertEqual(json.loads(out.getvalue())['configured_gpus'], 2)
            probe.return_value = {card(1): 2000}
            self.assertEqual(deploy.main(args), 1)
            ordinary.assert_not_called()
        self.assertEqual(os.umask(0o022), 0o022)
        with self.assertRaisesRegex(ValueError, 'project'):
            deployment_config.load(self.input, '3090a')
        workflow = yamlsub.load((ROOT/'.github/workflows/ci-v2.yml').read_text())['jobs']
        request = yamlsub.load((ROOT/'.github/workflows/cpu-task.yml').read_text())['jobs']
        self.assertNotIn('max-parallel', workflow['gpu_parts']['strategy'])
        self.assertIn('matrix.task.gpu_lane', workflow['gpu_parts']['with']['gpu_lane'])
        for attempt in ('allocate_first', 'allocate_retry'):
            self.assertIn("format('ci-gpu-wait-{0}', inputs.gpu_lane)", request[attempt]['runs-on'][1])

    def test_quotas_duplicate_uuids_and_legacy_card_ownership(self):
        self.assertEqual(protocol.GPU_QUOTAS, QUOTAS)
        for device, quota in QUOTAS.items():
            cards = [card(i) for i in range(quota)]
            protocol.validate_gpu_list(device, cards)
            for invalid in (cards+[card(99)], [card(1), card(1)], ['GPU-0000'], [1]):
                with self.subTest(device=device, invalid=invalid), self.assertRaises(protocol.Error):
                    protocol.validate_gpu_list(device, invalid)
        host = self.document['hosts']['3090a']
        host['gpu_uuids'] = [card(1)]
        self.document['hosts']['3090b'] = dict(host, roles=['executor'])
        self.write_inputs()
        with self.assertRaisesRegex(inventory.InventoryError, 'one host'):
            self.config()
        del self.document['hosts']['3090b']
        self.write_inputs()
        document = self.config().inventory_document()
        document['hosts']['3090a']['gpus'] = [0]
        with self.assertRaisesRegex(inventory.InventoryError, 'legacy GPU'):
            inventory.load(deployment_config.toml_document(document))
