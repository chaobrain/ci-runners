import concurrent.futures
import json
import tempfile
import unittest
from pathlib import Path

from ci_runners import central, cpu_groups, scheduler_store


def source(run=1, **kw):
    return dict(repository='example/project', sha='a'*40, run_id=run, run_attempt=1,
                context='project/ci', project='project', concurrency_key=str(run), **kw)


def plan(n=1, cores=4, device=''):
    return {'schema': 2, 'parts': [dict(id=f'test-{i}', needs='cpu', cores=cores, workers=3,
                                     device=device, minutes=600, command='true') for i in range(n)]}


class Contracts(unittest.TestCase):
    def test_names_and_plan_values(self):
        names = [n for d in central.DEVICES for n in central.runner_names(d)]
        self.assertEqual(len(names), 58)
        self.assertEqual(len(set(names)), 58)
        self.assertIn('slurm19', names)
        self.assertIn('pro6000-9', names)
        p = plan(cores=12)
        self.assertEqual(central.load_plan(p)['parts'][0]['workers'], 3)
        del p['parts'][0]['cores']
        self.assertEqual(central.load_plan(p)['parts'][0]['cores'], 4)
        for value in (None, 'slum', 'H100', 'pro6000-9', [], 4):
            with self.subTest(device=value), self.assertRaises(central.Error):
                central.load_plan(plan(device=value))
        for device in central.DEVICES:
            self.assertEqual(central.load_plan(plan(device=device))['parts'][0]['device'], device)

    def test_resource_errors(self):
        for key, value in [('cores', 6), ('cores', True), ('cores', 0), ('workers', None),
                           ('workers', True), ('workers', 0), ('minutes', float('nan')),
                           ('minutes', 0), ('queue_minutes', -1), ('RAM', '100G')]:
            p = plan()
            p['parts'][0][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(central.Error):
                central.load_plan(p)
        with self.assertRaises(central.Error):
            central.load_plan(plan(cores=20, device='3090a'))


class Scheduling(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = 1000.
        self.store = scheduler_store.Store(str(Path(self.tmp.name)/'scheduler.db'), clock=lambda: self.now)
        self.addCleanup(self.store.close)
        self.pools = {'3090a': 4, 'pro6000': 20, 'slurm': 20}
        self.runners = [dict(tag=n, pool=p, host='driver' if p == 'slurm' else p)
                        for p in self.pools for n in central.runner_names(p)]
        self.store.configure(self.pools, self.runners)
        for host in {r['host'] for r in self.runners}:
            self.store.heartbeat(host, [dict(tag=r['tag'], online=True, busy=False) for r in self.runners if r['host'] == host])

    def submit(self, run=1, n=1, cores=4, device='', **kw):
        p = plan(n, cores, device)
        choices = [device] if device else list(self.pools)
        return self.store.submit(source(run, **kw), p, {p['id']: choices for p in p['parts']})

    def claim(self, a, run=100):
        return self.store.claim(a['id'], a['host'], a['runner'], 'example/project', run, 1)

    def test_four_prs_atomic_and_fair(self):
        batches = {self.submit(i, 8) for i in range(1, 5)}
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            allocs = list(ex.map(lambda _: self.store.reserve(), range(32)))
        ordered = [self.store.allocation(json.loads(r[0])['allocation_id']) for r in
                   self.store.db.execute("SELECT payload FROM outbox WHERE kind='dispatch' ORDER BY id")]
        self.assertEqual({a['batch_id'] for a in ordered[:4]}, batches)
        self.assertEqual(len({a['runner'] for a in allocs}), 32)
        self.assertEqual(sum(c['groups_free'] for c in self.store.capacity()), 12)
        self.assertTrue(all(c['groups_free'] >= 0 for c in self.store.capacity()))

    def test_reservation_ack_release_and_runner_finish_are_distinct(self):
        batch = self.submit(cores=12, device='3090a')
        a = self.claim(self.store.reserve())
        cap = lambda: next(c for c in self.store.capacity() if c['device'] == '3090a')
        self.assertEqual((cap()['groups_free'], cap()['runners_free']), (1, 3))
        self.store.event(a['host'], 'ack1', a['id'], 'allocated')
        self.store.event(a['host'], 'ack1', a['id'], 'allocated')
        self.assertEqual((cap()['groups_reserved'], cap()['groups_allocated'], cap()['groups_free']), (0, 3, 1))
        result = {'state': 'success'}
        self.store.event(a['host'], 'release1', a['id'], 'released', result)
        self.store.event(a['host'], 'late-ack', a['id'], 'allocated')
        self.assertEqual((cap()['groups_free'], cap()['runners_free']), (4, 3))
        self.assertEqual(self.store.batch(batch)['state'], 'pending')
        self.store.runner_finished(a['id'], 100, 1)
        self.assertEqual(self.store.batch(batch)['state'], 'success')
        self.assertEqual(cap()['runners_free'], 4)

    def test_fixed_device_waits_and_cancellation_holds_resources(self):
        self.submit(1, cores=16, device='3090a')
        a = self.claim(self.store.reserve())
        b = self.submit(2, device='3090a')
        self.assertIsNone(self.store.reserve())
        self.store.cancel(a['batch_id'])
        self.assertIsNone(self.store.reserve())
        with self.assertRaises(central.Error):
            self.claim(a, 101)
        self.store.event(a['host'], 'release', a['id'], 'released', {'state': 'cancelled'})
        self.assertEqual(self.store.reserve()['batch_id'], b)

    def test_reopen_keeps_reservations_and_events(self):
        self.submit(cores=16, device='3090a')
        a = self.claim(self.store.reserve())
        other = scheduler_store.Store(str(Path(self.tmp.name)/'scheduler.db'), clock=lambda: self.now)
        self.addCleanup(other.close)
        self.assertEqual(other.allocation(a['id'])['groups'], 4)
        self.assertEqual(next(c for c in other.capacity() if c['device']=='3090a')['groups_free'], 0)
        with self.assertRaises(central.Error):
            other.claim(a['id'], a['host'], a['runner'], 'another/project', 100, 1)

    def test_submit_idempotency_and_queue_expiry(self):
        b = self.submit(device='slurm')
        self.assertEqual(self.submit(device='slurm'), b)
        with self.assertRaises(central.Error):
            self.submit(cores=12, device='slurm')
        self.now += 901
        self.assertIsNone(self.store.reserve())
        self.assertEqual(self.store.batch(b)['state'], 'error')

    def test_slurm_request_and_retry_keep_device(self):
        b = self.submit(device='slurm')
        a = self.claim(self.store.reserve())
        self.assertEqual(a['grant']['device'], 'slurm')
        self.store.event(a['host'], 'released', a['id'], 'released', {'state':'error', 'retryable':True})
        self.store.runner_finished(a['id'], 100, 1)
        again = self.store.reserve()
        self.assertEqual(again['grant']['device'], 'slurm')
        self.assertNotEqual(a['id'], again['id'])
        self.assertEqual(again['batch_id'], b)

    def test_execution_time_does_not_spend_the_retry_queue_budget(self):
        self.submit(device='3090a')
        a = self.claim(self.store.reserve())
        self.now += 30
        self.store.event(a['host'], 'ack', a['id'], 'allocated')
        self.now += 36000
        self.store.event(a['host'], 'late-ack', a['id'], 'allocated')
        self.store.event(a['host'], 'release', a['id'], 'released', {'state': 'error', 'retryable': True})
        self.store.runner_finished(a['id'], 100, 1)
        for host in {r['host'] for r in self.runners}:
            self.store.heartbeat(host, [dict(tag=r['tag'], online=True, busy=False) for r in self.runners if r['host']==host])
        again = self.store.reserve()
        self.assertIsNotNone(again)
        self.assertEqual(again['deadline']-self.now, 870)

    def test_upload_failure_cannot_turn_a_batch_green(self):
        batch = self.submit()
        a = self.claim(self.store.reserve())
        self.store.event(a['host'], 'release', a['id'], 'released', {'state': 'success'})
        self.store.runner_finished(a['id'], 100, 1, conclusion='failure')
        self.assertEqual(self.store.batch(batch)['state'], 'error')

    def test_old_source_does_not_cancel_newer_batch_or_overwrite_its_status(self):
        new = source(10, cancel_in_progress=True)
        new['concurrency_key'] = 'pr:7'
        old = dict(new, run_id=9)
        eligible = {'test-0': ['3090a']}
        a = self.store.submit(new, plan(), eligible)
        b = self.store.submit(old, plan(), eligible)
        self.assertEqual(self.store.batch(a)['state'], 'pending')
        self.assertEqual(self.store.batch(b)['state'], 'cancelled')
        self.assertFalse(self.store.is_latest_status({'source': old}))
        self.store.notice(source(11), 'failure', 'plan failed')
        self.store.notice(source(11), 'pending', 'late prepare')
        self.assertEqual(self.store.notices()[0]['state'], 'failure')

    def test_host_cannot_ack_another_host_and_timeout_does_not_free(self):
        self.submit(device='3090a')
        a = self.claim(self.store.reserve())
        with self.assertRaises(central.Error):
            self.store.event('pro6000', 'forged', a['id'], 'allocated')
        self.now += 901
        self.store.expire(a['id'])
        a = self.store.allocation(a['id'])
        self.assertTrue(a['cancelled'])
        self.assertFalse(a['released'])
        self.assertEqual(next(c for c in self.store.capacity() if c['device']=='3090a')['groups_free'], 3)

    def test_late_executor_tombstone_after_unsent_dispatch_is_acknowledged(self):
        batch = self.submit()
        a = self.store.reserve()
        self.store.cancel(batch)
        while True:
            msg = self.store.next_message()
            if msg['kind'] == 'dispatch':
                break
            self.store.message_done(msg['id'])
        self.store.dispatch_aborted(a['id'])
        self.store.event(a['host'], 'tombstone', a['id'], 'released',
                         {'state':'cancelled', 'reason':'execution job ended before starting'})
        self.assertEqual(self.store.batch(batch)['state'], 'cancelled')
        self.assertEqual(self.store.allocation(a['id'])['result']['reason'], 'not dispatched')


class LocalGroups(unittest.TestCase):
    def test_nonoverlap_restart_and_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp)/'local.db')
            ledger = cpu_groups.Ledger(path, cpu_groups.split('0-15', 4))
            self.addCleanup(ledger.close)
            def grant(aid, groups, runner):
                return dict(allocation_schema=1, allocation_id=aid, task_id=aid, host='worker',
                            device='3090a', backend='local', requested_device='', runner_tag=runner,
                            cpu_groups=groups, cores=groups*4, RAM=f'{groups*4}G', workers=9, minutes=600)
            a, b = grant('first', 3, '3090a-3'), grant('second', 1, '3090a-0')
            one, two = ledger.reserve(a, {}), ledger.reserve(b, {})
            self.assertFalse(set(one['cpus']) & set(two['cpus']))
            self.assertEqual(ledger.reserve(a, {})['cpus'], one['cpus'])
            with self.assertRaises(central.Error):
                ledger.reserve(grant('third', 1, '3090a-1'), {})
            with self.assertRaises(central.Error):
                ledger.release('first', {'state':'success'}, cleaned=False)
            self.assertTrue(ledger.start_worker('first', 123, 1000., 'record'))
            self.assertFalse(ledger.start_worker('first', 124, 1001., 'record'))
            ledger.allocated('first')
            ledger.release('first', {'state':'success'}, cleaned=True)
            reopened = cpu_groups.Ledger(path, cpu_groups.split('0-15', 4))
            self.addCleanup(reopened.close)
            self.assertEqual(len(reopened.reports()), 2)
            self.assertEqual(len(reopened.reserve(grant('third', 3, '3090a-1'), {})['cpus']), 12)
            self.assertEqual(reopened.get('first')['state'], 'released')


if __name__ == '__main__':
    unittest.main()
