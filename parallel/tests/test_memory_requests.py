"""Explicit RAM requests preserve consumer limits without oversubscribing hosts."""
import concurrent.futures
import tempfile
import unittest
from pathlib import Path

from parallel import protocol, scheduler_store
from parallel.tests.test_central import plan, source


class MemoryRequests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = str(Path(self.directory.name) / 'queue.sqlite')
        self.store = scheduler_store.Store(self.path, clock=lambda: 1000.)
        self.addCleanup(self.store.close)
        self.store.register_device('3090a', dict(device='3090a', priority=0, cpu_count=16,
                                                runner_count=4, memory_max='64G',
                                                gpu_uuids=['GPU-00000000-0000-0000-0000-000000000001']))
        project = dict(repository='example/project', memory_bytes=64 << 30,
                       max_minutes=600, devices=['3090a'])
        self.store.heartbeat('3090a', [dict(tag=f'3090a-{i}', online=True, busy=False) for i in range(4)],
                             ready_projects={'project': project},
                             gpu_free_mib={'GPU-00000000-0000-0000-0000-000000000001': 8192})

    def queue(self, run, memory=None, *, gpu=False, source_fields=None):
        document = plan(device='3090a')
        document['parts'][0]['gpu'] = gpu
        if memory is not None:
            document['parts'][0]['memory_gib'] = memory
        document = protocol.load_plan(document)
        batch = self.store.submit(source(run, **(source_fields or {})), document, {'test-0': ['3090a']})
        self.store.request_task_allocation(batch, self.store.batch(batch)['tasks'][0]['id'], 1)
        return batch

    def reserve(self):
        return self.store.reserve(allowed_devices={'3090a'})

    def test_explicit_ram_preserves_cores_and_workers(self):
        self.queue(1, 40)
        grant = self.reserve()['grant']
        self.assertEqual((grant['cores'], grant['workers'], grant['RAM']), (4, 3, '40G'))
        self.assertEqual(protocol.validate_allocation(grant), grant)
        with self.assertRaises(protocol.Error):
            protocol.validate_allocation({**grant, 'RAM': '4G'})

    def contract_capability(self, version):
        self.store.heartbeat('3090a', [dict(tag=f'3090a-{i}', online=True, busy=False) for i in range(4)],
                             allocation_schema=version,
                             ready_projects={'project': dict(repository='example/project', memory_bytes=64 << 30,
                                 max_minutes=600, devices=['3090a'])},
                             gpu_free_mib={'GPU-00000000-0000-0000-0000-000000000001': 8192})

    def test_report_gated_cpu_waits_for_matching_executor_codec(self):
        self.contract_capability(4)
        self.queue(1, 8, source_fields=dict(report_gate=True))
        self.assertIsNone(self.reserve())
        self.contract_capability(5)
        allocation = self.reserve()
        self.assertIsNotNone(allocation)
        self.assertEqual(allocation['grant']['RAM'], '8G')

    def test_overridden_cpu_source_waits_for_matching_executor_codec(self):
        self.contract_capability(4)
        self.queue(1, 8, source_fields=dict(source_selection=dict(kind='commit-replay')))
        self.assertIsNone(self.reserve())
        self.contract_capability(5)
        allocation = self.reserve()
        self.assertIsNotNone(allocation)
        self.assertEqual(allocation['grant']['RAM'], '8G')

    def test_new_contract_waits_without_executor_capability_advertisement(self):
        self.store.db.execute('DELETE FROM allocation_capabilities WHERE host=?', ('3090a',))
        self.queue(1, 8, source_fields=dict(report_gate=True))
        self.assertIsNone(self.reserve())
        self.contract_capability(5)
        self.assertIsNotNone(self.reserve())

    def test_old_plan_keeps_its_exact_default(self):
        self.queue(1)
        grant = self.reserve()['grant']
        self.assertEqual(grant['RAM'], '4G')
        self.assertNotIn('memory_gib', grant)
        protocol.validate_allocation(grant)

    def test_invalid_explicit_limits_fail_closed(self):
        for value in (0, -1, True, False, 1.5, '40', None, 1 << 21):
            document = plan()
            document['parts'][0]['memory_gib'] = value
            with self.subTest(value=value), self.assertRaises(protocol.Error):
                protocol.load_plan(document)

    def test_transaction_prevents_concurrent_ram_overcommit(self):
        for run in range(1, 5):
            self.queue(run, 40)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            allocations = list(pool.map(lambda _: self.reserve(), range(4)))
        self.assertEqual(len([a for a in allocations if a]), 1)

    def test_memory_becomes_available_only_after_release(self):
        self.queue(1, 40)
        allocation = self.reserve()
        self.queue(2, 40)
        self.store.cancel(allocation['batch_id'])
        self.assertIsNone(self.reserve())
        self.store.event('3090a', 'release', allocation['id'], 'released', {'state': 'cancelled'})
        self.assertIsNotNone(self.reserve())

    def test_restart_reconstructs_memory_from_persisted_parts(self):
        self.queue(1, 40)
        allocation = self.reserve()
        self.queue(2, 40)
        other = scheduler_store.Store(self.path, clock=lambda: 1000.)
        self.addCleanup(other.close)
        self.assertEqual(other.allocation(allocation['id'])['grant']['RAM'], '40G')
        self.assertIsNone(other.reserve(allowed_devices={'3090a'}))

    def test_request_larger_than_admitted_host_never_reserves(self):
        self.queue(1, 65)
        self.assertIsNone(self.reserve())

    def test_gpu_and_cpu_share_the_same_host_ram_budget(self):
        self.queue(1, 40, gpu=True)
        allocation = self.reserve()
        self.assertEqual(allocation['grant']['RAM'], '40G')
        self.assertIn('gpu_uuid', allocation['grant'])
        protocol.validate_allocation(allocation['grant'])
        self.queue(2, 40)
        self.assertIsNone(self.reserve())

    def test_mixed_default_and_explicit_requests_share_the_budget(self):
        self.queue(1, 62)
        self.reserve()
        self.queue(2)
        self.assertIsNone(self.reserve())


if __name__ == '__main__':
    unittest.main()
