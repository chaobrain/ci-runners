"""Two-card grants must retain both leases, evidence and launch fences."""
import concurrent.futures
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from parallel import executor_store, gpu_resources, protocol, scheduler_store, task_execution
from parallel.tests.test_central import plan, source
from parallel.tests.test_gpu_queue import Queue, card, grant


def pair(aid='pair', cards=None):
    return dict(grant(aid), allocation_schema=3, gpu_count=2,
                gpu_uuids=cards if cards is not None else [card(1), card(2)])


class Contracts(unittest.TestCase):
    def test_counts_are_exact_and_do_not_change_old_defaults(self):
        doc = plan()
        self.assertEqual(protocol.gpu_count(protocol.load_plan(doc)['parts'][0]), 0)
        doc['parts'][0]['gpu'] = True
        self.assertEqual(protocol.gpu_count(protocol.load_plan(doc)['parts'][0]), 1)
        for value in (True, False, None, '2', 2.0, -1, 3, 0):
            with self.subTest(value=value), self.assertRaises(protocol.Error):
                protocol.load_plan({'schema': 2, 'parts': [dict(doc['parts'][0], gpu_count=value)]})
        with self.assertRaises(protocol.Error):
            protocol.gpu_count({'gpu_count': 2})
        self.assertEqual(protocol.gpu_count({'gpu': True, 'gpu_count': 2}), 2)

    def test_pair_cannot_be_truncated_duplicated_or_disguised_as_old_schema(self):
        self.assertEqual(protocol.grant_cards(protocol.validate_allocation(pair())), [card(1), card(2)])
        for change in ({'gpu_uuids': [card(1)]}, {'gpu_uuids': [card(1), card(1)]},
                       {'gpu_uuids': [card(1), 'GPU-unknown']}, {'gpu_uuid': card(1)},
                       {'gpu_count': True}, {'gpu_count': 1}, {'allocation_schema': 2},
                       {'allocation_schema': 1}, {'device': 'slurm', 'backend': 'slurm', 'runner_tag': 'slurm0'}):
            with self.subTest(change=change), self.assertRaises(protocol.Error):
                protocol.validate_allocation(dict(pair(), **change))


class Central(unittest.TestCase):
    setUp = Queue.setUp
    open_store = Queue.open_store
    statuses = Queue.statuses
    capacity = Queue.capacity
    release = Queue.release

    def heartbeat(self, host, **options):
        options.setdefault('allocation_schema', 3)
        Queue.heartbeat(self, host, **options)

    def submit(self, run=1, count=1, device='3090a', cards=2):
        doc = plan(count, device=device)
        for part in doc['parts']:
            part.update(gpu=True, gpu_count=cards)
        choices = [device] if device else list(self.cards)
        batch = self.store.submit(source(run), doc, {p['id']: choices for p in doc['parts']})
        for task in self.store.batch(batch)['tasks']:
            self.store.request_task_allocation(batch, task['id'], 1)
        return batch

    def test_one_available_card_cannot_be_partially_held_by_a_waiting_pair(self):
        host = '3090a'
        self.heartbeat(host, gpu_free_mib={self.cards[host][0]: 8192})
        batch = self.submit()
        self.assertIsNone(self.store.reserve())
        self.assertEqual(self.capacity(host)['gpus_reserved'], 0)
        self.assertEqual(self.capacity(host)['groups_reserved'], 0)
        self.submit(run=2, cards=1)
        self.assertEqual(self.store.reserve()['grant']['allocation_schema'], 2)
        self.assertEqual(self.store.batch(batch)['tasks'][0]['attempts'], 0)

    def test_pair_does_not_span_devices_or_reach_old_executors(self):
        for host in self.cards:
            self.heartbeat(host, gpu_free_mib={self.cards[host][0]: 8192})
        self.submit(device='')
        self.assertIsNone(self.store.reserve())
        self.heartbeat('3090a', allocation_schema=2)
        self.assertIsNone(self.store.reserve())
        self.heartbeat('3090a')
        allocation = self.store.reserve()
        self.assertEqual(allocation['grant']['allocation_schema'], 3)
        self.assertEqual(allocation['grant']['gpu_uuids'], self.cards['3090a'])

    def test_concurrent_pairs_have_disjoint_cards_and_preserve_memory_and_cpus(self):
        self.submit(count=10, device='')
        other = self.open_store()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            allocations = [a for a in pool.map(lambda s: s.reserve(), [self.store, other] * 6) if a]
        self.assertEqual(len(allocations), 4)
        cards = [c for a in allocations for c in a['grant']['gpu_uuids']]
        self.assertEqual(len(cards), len(set(cards)))
        self.assertEqual(sum(c['gpus_reserved'] for c in self.store.capacity()), 8)
        self.assertEqual(sum(c['groups_reserved'] for c in self.store.capacity()), 4)
        self.assertIsNone(other.reserve())

    def test_restart_cancel_and_duplicate_release_retain_and_free_both_cards(self):
        batch = self.submit()
        allocation = self.store.reserve()
        self.store = self.open_store()
        self.store.cancel(batch)
        self.assertEqual(self.capacity('3090a')['gpus_reserved'], 2)
        with self.assertRaisesRegex(protocol.Error, 'drain'):
            self.store.configure(self.pools, self.runners, {**self.cards, '3090a': self.cards['3090a'][:1]})
        result = {'state': 'cancelled'}
        self.store.event('3090a', 'cancel-cleaned', allocation['id'], 'released', result)
        self.store.event('3090a', 'cancel-cleaned-again', allocation['id'], 'released', result)
        self.assertEqual(self.capacity('3090a')['gpus_reserved'], 0)
        self.assertEqual(self.capacity('3090a')['groups_reserved'], 0)
        self.assertFalse(self.store.allocation(allocation['id'])['runner_done'])
        self.assertEqual(self.store.allocation(allocation['id'])['grant']['gpu_uuids'], self.cards['3090a'])

    def test_card_insert_interruption_rolls_back_the_entire_central_reservation(self):
        batch = self.submit()
        self.store.db.execute('''CREATE TRIGGER interrupt_pair BEFORE INSERT ON allocation_cards
                                  WHEN NEW.position=1 BEGIN SELECT RAISE(ABORT,'interrupted'); END''')
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.reserve()
        self.assertEqual(self.capacity('3090a')['gpus_reserved'], 0)
        self.assertEqual(self.capacity('3090a')['groups_reserved'], 0)
        self.assertEqual(self.store.batch(batch)['tasks'][0]['attempts'], 0)
        self.store.db.execute('DROP TRIGGER interrupt_pair')
        self.assertEqual(len(self.store.reserve()['grant']['gpu_uuids']), 2)

    def test_released_pair_rotates_both_cards_without_duplicate_capacity_rows(self):
        self.submit(count=2, device='pro6000')
        first = self.store.reserve()
        self.release(first)
        second = self.store.reserve()
        cards = self.cards['pro6000']
        self.assertEqual(first['grant']['gpu_uuids'], cards[:2])
        self.assertEqual(second['grant']['gpu_uuids'], [cards[2], cards[0]])
        self.assertEqual(self.capacity('pro6000')['gpus_total'], 3)
        self.assertEqual(self.capacity('pro6000')['gpus_reserved'], 2)

    def test_old_database_upgrade_requires_drain_and_preserves_single_card_history(self):
        self.submit(cards=1)
        allocation = self.store.reserve()
        self.store.db.execute('DROP TABLE allocation_cards')
        self.store.db.execute('DROP TABLE allocation_capabilities')
        self.store.db.execute('UPDATE meta SET version=3')
        with self.assertRaisesRegex(protocol.Error, 'drain'):
            scheduler_store.Store(self.path)
        # Restore the v4 bookkeeping so the original process can finish this fixture.
        self.store.db.execute('''CREATE TABLE allocation_cards(allocation TEXT,uuid TEXT,position INTEGER)''')
        self.store.db.execute('INSERT INTO allocation_cards VALUES(?,?,0)',
                              (allocation['id'], allocation['gpu_uuid']))
        self.store.claim(allocation['id'], allocation['host'], allocation['runner'], 'example/project', 1, 1, job_id=501)
        self.release(allocation)
        self.store.runner_finished(allocation['id'], 1, 1, job_id=501, conclusion='success')
        self.store.db.execute('DROP TABLE allocation_cards')
        upgraded = self.open_store()
        self.assertEqual(upgraded.allocation(allocation['id'])['grant'], allocation['grant'])
        self.assertEqual(upgraded.db.execute('SELECT version FROM meta').fetchone()[0], 4)


class Local(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = str(Path(directory.name) / 'executor.db')
        self.ledger = executor_store.Ledger(self.path, [0, 1], [card(1), card(2), card(3)])
        self.addCleanup(self.ledger.close)

    def test_partial_local_failure_rolls_back_cards_cpus_and_execution(self):
        with self.assertRaises(protocol.Error):
            self.ledger.reserve(pair(cards=[card(1), card(9)]), {})
        self.assertEqual(self.ledger.active(), [])
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM held_gpus').fetchone()[0], 0)
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM held_groups').fetchone()[0], 0)

    def test_scope_fence_requires_every_card_and_cleanup_releases_the_pair(self):
        value = pair()
        cpus = self.ledger.reserve(value, {})['cpus']
        task_execution.check_fence(self.path, value, cpus)
        self.ledger.reserve(grant('other', card(3)), {})
        self.ledger.db.execute('DELETE FROM held_gpus WHERE uuid=?', (card(2),))
        with self.assertRaisesRegex(protocol.Error, 'GPU'):
            task_execution.check_fence(self.path, value, cpus)
        with self.assertRaises(protocol.Error):
            self.ledger.release('pair', {'state': 'failure'}, cleaned=False)
        self.ledger.release('pair', {'state': 'failure'}, cleaned=True)
        self.assertEqual([r[0] for r in self.ledger.db.execute('SELECT uuid FROM held_gpus')], [card(3)])

    def test_p2p_gate_precedes_command_and_visibility_contains_the_complete_pair(self):
        with (tempfile.TemporaryDirectory() as directory,
              mock.patch.dict(os.environ, CUDA_VISIBLE_DEVICES='foreign'),
              mock.patch.object(task_execution, 'resource_limits', return_value=(4, 4 << 30)),
              mock.patch('os.sched_getaffinity', return_value={0, 1, 2, 3}),
              mock.patch.object(gpu_resources, 'available', return_value={card(1): 8192, card(2): 8192}),
              mock.patch.object(task_execution.part, 'supervise', return_value=1) as supervisor):
            self.assertEqual(task_execution.supervise(directory, pair(), [], 'false'), 1)
            self.assertEqual(os.environ['CUDA_VISIBLE_DEVICES'], card(1) + ',' + card(2))
            self.assertTrue(supervisor.call_args.kwargs['p2p'])
            record = json.loads((Path(directory) / 'allocation-ready.json').read_text())
            self.assertEqual(record['gpu_uuids'], [card(1), card(2)])
        with (tempfile.TemporaryDirectory() as directory,
              mock.patch.object(task_execution, 'resource_limits', return_value=(4, 4 << 30)),
              mock.patch.object(gpu_resources, 'available', return_value={card(1): 8192}),
              mock.patch.object(task_execution.part, 'supervise') as supervisor):
            self.assertEqual(task_execution.supervise(directory, pair(), [], 'true'), 2)
            supervisor.assert_not_called()


if __name__ == '__main__':
    unittest.main()
