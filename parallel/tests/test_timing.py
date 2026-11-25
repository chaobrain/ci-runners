"""Full-device reservations and the original reference lock/priority are inseparable."""
from copy import deepcopy
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from ci_runners import locks, slots
from parallel import protocol, scheduler_store, task_execution, timing
from parallel.tests.test_central import source


class Timing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root/'profiles.json'
        self.profile = dict(id='reference', device='3090a', cpu_count=16, cpus=[0, 1], cpu_model='reference CPU',
                            host_lock=str(self.root/'reference.lock'), enabled=True, reference_ready=True,
                            repositories=[dict(id=11, repository='example/project')])
        self.write()
        self.src = source(1, repository_id=11)
        self.src['timing_profiles'] = timing.snapshot(self.path, self.src)
        self.task = dict(id='compile', needs='cpu', device='3090a', cores=16, workers=8,
                         memory_gib=1, minutes=2, command='original compile', isolation='timing', timing_target='reference')
        self.store = scheduler_store.Store(':memory:')
        self.addCleanup(self.store.close)
        self.runners = [dict(tag=n, pool='3090a', host='3090a') for n in protocol.runner_names('3090a')]
        self.card = 'GPU-00000000-0000-0000-0000-000000000001'
        self.store.configure({'3090a': 4}, self.runners, {'3090a': [self.card]})
        self.heartbeat()

    def write(self):
        self.path.write_text(json.dumps(dict(schema=1, targets=[self.profile])))
        self.path.chmod(0o600)

    def heartbeat(self, version=5):
        self.store.heartbeat('3090a', [dict(tag=r['tag'], online=True, busy=False) for r in self.runners],
                             gpu_free_mib={self.card: 20000}, allocation_schema=version)

    def submit(self, task=None, run=1):
        task = self.task if task is None else task
        src = deepcopy(self.src)
        src['run_id'] = run
        batch = self.store.submit(src, dict(schema=2, parts=[task]), {task['id']: ['3090a']})
        row = self.store.batch(batch)['tasks'][0]
        self.store.request_task_allocation(batch, row['id'], 1)
        return batch

    def reserve(self):
        return self.store.reserve(timing_permitted=lambda s, p: timing.permitted(self.path, s, p))

    def finish(self, allocation):
        a = self.store.claim(allocation['id'], '3090a', allocation['runner'], 'example/project',
                             allocation['run_id'], 1, job_id=100 + allocation['run_id'])
        self.store.event('3090a', a['id']+'-release', a['id'], 'released', {'state': 'success'})
        self.store.runner_finished(a['id'], a['run_id'], 1, job_id=a['job_id'])

    def request(self):
        return dict(profiles_file=str(self.path), source=self.src, cpu_count=16,
                    host_lock=self.profile['host_lock'], deadline=time.time()+10)

    def grant(self):
        self.submit()
        return self.reserve()['grant']

    def test_timing_plan_and_grant_cannot_be_generic_gpu_or_old_schema(self):
        doc = dict(schema=2, parts=[self.task])
        self.assertEqual(protocol.load_plan(doc)['parts'][0]['isolation'], 'timing')
        for change in (dict(gpu=True), dict(device='slurm'), dict(device=''), dict(timing_target='../reference'),
                       dict(isolation='shared'), dict(isolation=True)):
            with self.subTest(change=change), self.assertRaises(protocol.Error):
                protocol.load_plan(dict(schema=2, parts=[{**self.task, **change}]))
        grant = self.grant()
        self.assertEqual(protocol.validate_allocation(grant)['allocation_schema'], 5)
        for change in (dict(allocation_schema=1), dict(gpu_count=1), dict(timing_profile_sha256='x'),
                       dict(timing_target='../reference')):
            with self.subTest(change=change), self.assertRaises(protocol.Error):
                protocol.validate_allocation({**grant, **change})

    def test_timing_blocks_cpu_and_gpu_until_full_release(self):
        self.submit()
        first = self.reserve()
        self.assertEqual((first['groups'], first['grant']['allocation_schema']), (4, 5))
        for run, gpu in ((2, False), (3, True)):
            self.submit(dict(id='ordinary', needs='cpu', device='3090a', cores=4, workers=1,
                             memory_gib=1, minutes=1, command='true', gpu=gpu), run)
        self.assertIsNone(self.reserve())
        self.finish(first)
        second = self.reserve()
        self.assertIsNotNone(second)
        self.assertNotEqual(second['grant']['allocation_schema'], 5)

    def test_timing_waits_for_existing_cpu_and_older_executor_or_changed_profile(self):
        self.submit(dict(id='ordinary', needs='cpu', cores=4, workers=1, memory_gib=1,
                         device='3090a', minutes=1, command='true'), 2)
        ordinary = self.reserve()
        self.submit()
        self.assertIsNone(self.reserve())
        self.finish(ordinary)
        self.heartbeat(4)
        self.assertIsNone(self.reserve())
        self.heartbeat(5)
        self.profile['reference_ready'] = False
        self.write()
        self.assertIsNone(self.reserve())
        self.profile['reference_ready'] = True
        self.write()
        self.assertEqual(self.reserve()['grant']['allocation_schema'], 5)

    def test_resized_device_cannot_claim_an_old_full_device_grant(self):
        self.submit()
        a = self.reserve()
        with self.assertRaisesRegex(protocol.Error, 'shrink'):
            self.store.configure({'3090a': 3}, self.runners[:3], {'3090a': [self.card]})
        # Exercise the independent claim fence against an externally stale profile.
        self.store.db.execute("UPDATE pools SET total=3 WHERE name='3090a'")
        with self.assertRaisesRegex(protocol.Error, 'complete device'):
            self.store.claim(a['id'], '3090a', a['runner'], 'example/project', 1, 1, job_id=101)

    def test_private_profile_disallows_partial_width_and_foreign_repository(self):
        self.task['cores'] = 8
        with self.assertRaises(protocol.Error):
            self.submit()
        self.task['cores'] = 16
        self.src['repository_id'] = 12
        self.assertFalse(timing.permitted(self.path, self.src, self.task))
        self.assertEqual(timing.snapshot(None, self.src), {})

    def test_real_lock_is_inherited_and_restored_on_failure(self):
        grant = self.grant()
        affinity = {0, 1, 2}
        before = {k: os.environ.get(k) for k in ('BRAINSPIKE_HOST_LOCK', 'LOCAL_CI_HOST_LOCK', 'CI_HOST_LOCK')}
        with mock.patch.object(timing.os, 'sched_getaffinity', side_effect=lambda _: set(affinity)), \
                mock.patch.object(timing.os, 'sched_setaffinity', side_effect=lambda _, cpus: (affinity.clear(), affinity.update(cpus))), \
                mock.patch.object(timing.os, 'getpriority', return_value=0), \
                mock.patch.object(timing, 'cpu_model', return_value='reference CPU'):
            with self.assertRaisesRegex(RuntimeError, 'simulated red'):
                with timing.hold(grant, self.request()) as ready:
                    self.assertEqual((ready['nice'], ready['host_lock']), (0, 'exclusive'))
                    self.assertEqual(affinity, {0, 1})
                    nested = locks.HostLock(self.profile['host_lock'], held='x', timeout=0)
                    nested.acquire('s')
                    self.assertTrue(nested.covered)
                    nested.release()
                    with open(self.profile['host_lock']) as probe:
                        with self.assertRaises(BlockingIOError):
                            fcntl.flock(probe, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    raise RuntimeError('simulated red')
        self.assertEqual({k: os.environ.get(k) for k in before}, before)
        self.assertEqual(affinity, {0, 1, 2})
        with open(self.profile['host_lock']) as probe:
            fcntl.flock(probe, fcntl.LOCK_SH | fcntl.LOCK_NB)

    def test_wrong_priority_affinity_model_or_profile_never_gets_lock_cover(self):
        grant = self.grant()
        for nice, cpus, model in ((10, {0, 1}, 'reference CPU'), (0, {0}, 'reference CPU'), (0, {0, 1}, 'other CPU')):
            with mock.patch.object(timing.os, 'sched_getaffinity', return_value=cpus), \
                    mock.patch.object(timing.os, 'getpriority', return_value=nice), \
                    mock.patch.object(timing, 'cpu_model', return_value=model), \
                    mock.patch.object(locks, 'HostLock', wraps=locks.HostLock) as lock:
                with self.subTest(nice=nice, cpus=cpus, model=model), self.assertRaises(protocol.Error):
                    with timing.hold(grant, self.request()):
                        self.fail('must not start')
                lock.assert_not_called()
        self.profile['enabled'] = False
        self.write()
        with self.assertRaises(protocol.Error):
            with timing.hold(grant, self.request()):
                self.fail('revoked profile started')

    def test_supervisor_preserves_nice_zero_and_publishes_isolation_receipt(self):
        grant = self.grant()
        affinity = {0, 1, 2}
        with mock.patch.object(task_execution, 'resource_limits', return_value=(16, 1 << 30)), \
                mock.patch.object(timing.os, 'sched_getaffinity', side_effect=lambda _: set(affinity)), \
                mock.patch.object(timing.os, 'sched_setaffinity', side_effect=lambda _, cpus: (affinity.clear(), affinity.update(cpus))), \
                mock.patch.object(timing.os, 'getpriority', return_value=0), \
                mock.patch.object(timing, 'cpu_model', return_value='reference CPU'), \
                mock.patch.object(task_execution.part, 'supervise', return_value=1) as command:
            self.assertEqual(task_execution.supervise(str(self.root), grant, [], 'original compile',
                                                     timing_request=self.request()), 1)
            self.assertEqual(command.call_args.args[2][:3], ['nice', '-n', '0'])
        ready = json.loads((self.root/'allocation-ready.json').read_text())
        self.assertEqual(ready['timing']['profile_sha256'], grant['timing_profile_sha256'])
        self.assertEqual(ready['timing']['required_affinity'], [0, 1])

    def test_process_termination_releases_exclusive_hold(self):
        grant = self.grant()
        marker = self.root/'entered'
        program = ('import json,pathlib,sys,time; from parallel import timing; '
                   'p=json.loads(sys.argv[1]); timing.cpu_model=lambda: "reference CPU"; '
                   'timing.os.getpriority=lambda *a:0; timing.os.sched_getaffinity=lambda *a:{0,1}; '
                   'timing.os.sched_setaffinity=lambda *a:None; '
                   'c=timing.hold(p["grant"],p["request"]);c.__enter__();'
                   'pathlib.Path(p["marker"]).write_text("entered");time.sleep(20)')
        child = subprocess.Popen([sys.executable, '-c', program,
                                  json.dumps(dict(grant=grant, request=self.request(), marker=str(marker)))],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        start = slots._start_of(child.pid)
        try:
            deadline = time.monotonic()+5
            while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(marker.exists())
            self.assertEqual(slots._start_of(child.pid), start)
            child.terminate()
            child.wait(timeout=5)
            with open(self.profile['host_lock']) as probe:
                fcntl.flock(probe, fcntl.LOCK_SH | fcntl.LOCK_NB)
        finally:
            if child.poll() is None:
                self.assertEqual(slots._start_of(child.pid), start)
                child.kill(); child.wait(timeout=5)
            child.stderr.close()
