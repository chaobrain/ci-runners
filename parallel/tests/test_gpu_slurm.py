"""GPU cluster authorization stays separate from organization source admission."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from ci_runners import slurmgate, slurmsite
from parallel import gpu_resources, organization, protocol, task_execution, workflow_client
from parallel.tests import test_gpu_queue as local, test_organization_scheduler as fixtures
from parallel.tests.test_central import plan, source


def remote_grant():
    return dict(local.grant('cluster'), allocation_schema=4, device='slurm', backend='slurm',
                requested_device='slurm', runner_tag='slurm0', gpu_count=2,
                cluster_target='architecture', slurm_target='cluster1/a40', gpu_model='a40', variant='cuda13',
                cluster_max_parallel=2)


class TargetAuthorization(unittest.TestCase):
    github_get = fixtures.OrganizationSlurm.github_get
    authorize = fixtures.OrganizationSlurm.authorize
    claim = fixtures.OrganizationSlurm.claim

    def reserve(self):
        return self.store.reserve(allowed_devices={'3090a'}, organization=True,
            slurm_permitted=self.app.slurm_permitted, slurm_capacity=self.app.slurm_capacity)

    def setUp(self):
        fixtures.OrganizationSlurm.setUp(self)
        self.binding['gpu_targets'] = {'architecture': 'cluster1/a40'}
        self.authorize(self.binding)
        fixtures.OrganizationSlurm.host(self)
        self.store.heartbeat('3090a', [dict(tag=r['tag'], online=True, busy=False) for r in
                             self.app.runners if r['host'] == '3090a'], allocation_schema=4,
                             gpu_free_mib={}, organization_ready=True)

    def submit(self, run=1, **part_changes):
        document = plan(device='slurm')
        document['parts'][0].update({'gpu': True, 'gpu_count': 2, 'cluster_target': 'architecture', 'minutes': 10, **part_changes})
        result = self.app.submit(source(run), document)
        batch = self.store.batch(result['batch_id'])
        self.store.request_task_allocation(batch['id'], batch['tasks'][0]['id'], 1)
        return batch

    def test_named_target_gets_a_versioned_profile_without_local_card_leases(self):
        batch = self.submit()
        allocation = self.reserve()
        self.assertIsNotNone(allocation)
        grant = protocol.validate_allocation(allocation['grant'])
        self.assertEqual(grant['allocation_schema'], 4)
        self.assertEqual((grant['gpu_count'], grant['slurm_target'], grant['gpu_model'], grant['variant']),
                         (2, 'cluster1/a40', 'a40', 'cuda13'))
        self.assertNotIn('gpu_uuid', grant)
        self.assertNotIn('gpu_uuids', grant)
        record = self.store.batch(batch['id'])['allocations'][0]
        self.assertEqual(record['grant'], grant)
        self.assertIsNone(record['storage_reference'])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM allocation_cards').fetchone()[0], 0)
        self.assertTrue(self.claim(allocation)['organization_slurm_verified'])
        self.assertEqual(batch['source']['slurm_gpu_targets']['architecture']['variant'], 'cuda13')

    def test_unlisted_cpu_disabled_and_foreign_targets_fail_before_submission(self):
        for mapping in ({}, {'architecture': 'cluster1/cpu'}, {'architecture': 'cluster1/h100'},
                        {'architecture': 'other/a40'}):
            self.authorize(dict(self.binding, gpu_targets=mapping))
            with self.subTest(mapping=mapping), self.assertRaises(protocol.Error):
                self.submit()
        self.authorize(dict(self.binding, repository_id=99))
        with self.assertRaises(protocol.Error):
            self.submit()

    def test_exact_resources_and_current_target_are_revalidated_at_reserve_and_claim(self):
        self.submit()
        original = self.site.read_text()
        self.site.write_text(original.replace('max_cards = 2', 'max_cards = 1'))
        self.assertIsNone(self.reserve())
        self.site.write_text(original)
        allocation = self.reserve()
        self.site.write_text(original.replace('gpu_model = "a40"', 'gpu_model = "v100"'))
        with self.assertRaisesRegex(protocol.Error, 'no longer admitted'):
            self.claim(allocation)
        self.site.write_text(original)
        self.authorize(dict(self.binding, gpu_targets={}))
        with self.assertRaisesRegex(protocol.Error, 'no longer admitted'):
            self.claim(allocation)

    def test_old_executor_never_receives_remote_gpu_allocation(self):
        self.submit()
        self.store.db.execute('UPDATE allocation_capabilities SET schema=3')
        self.assertIsNone(self.reserve())
        self.store.db.execute('UPDATE allocation_capabilities SET schema=4')
        self.assertIsNotNone(self.reserve())

    def test_site_concurrency_counts_cpu_and_gpu_jobs_until_cleanup(self):
        original = self.site.read_text()
        import re
        self.site.write_text(re.sub(r'max_parts\s*=\s*\d+', 'max_parts = 1', original))
        self.submit()
        first = self.reserve()
        self.assertIsNotNone(first)
        fixtures.OrganizationSlurm.submit(self, run=2, device='slurm')
        self.assertIsNone(self.reserve())
        self.store.event(first['host'], 'gpu-cleaned', first['id'], 'released', {'state': 'failure'})
        self.assertIsNotNone(self.reserve())

    def test_new_allowlist_fields_cannot_carry_hosts_flags_or_unbounded_names(self):
        for mapping in ([], {'bad/target': 'cluster1/a40'}, {'architecture': '--gpus=2'},
                        {'architecture': 'cluster1/a40/extra'}, {'architecture': '../target'}):
            self.authorize(dict(self.binding, gpu_targets=mapping))
            with self.subTest(mapping=mapping), self.assertRaises(protocol.Error):
                organization.read_slurm_allowlist(self.allowlist, 'example')


class ComputeNode(unittest.TestCase):
    def test_cuda_mask_count_identity_and_model_must_match_the_authorized_job(self):
        grant = remote_grant()
        env = {'SLURM_JOB_ID': '123', 'CUDA_VISIBLE_DEVICES': '0,1'}
        query = lambda: [(local.card(1), 'NVIDIA A40'), (local.card(2), 'NVIDIA A40')]
        self.assertEqual(gpu_resources.slurm_cards(grant, env, query=query), [local.card(1), local.card(2)])
        for bad in ({}, dict(env, SLURM_JOB_ID=''), dict(env, CUDA_VISIBLE_DEVICES='0'),
                    dict(env, CUDA_VISIBLE_DEVICES='0,0'), dict(env, CUDA_VISIBLE_DEVICES='all')):
            with self.subTest(env=bad), self.assertRaises(protocol.Error):
                gpu_resources.slurm_cards(grant, bad, query=query)
        for result in (query()[:1], [(local.card(1), 'NVIDIA A40')] * 2,
                       [(local.card(1), 'NVIDIA A100'), (local.card(2), 'NVIDIA A100')]):
            with self.subTest(result=result), self.assertRaises(protocol.Error):
                gpu_resources.slurm_cards(grant, env, query=lambda: result)
        with self.assertRaises(protocol.Error):
            gpu_resources.slurm_cards(dict(grant, gpu_model='a10', gpu_count=1),
                dict(env, CUDA_VISIBLE_DEVICES='0'), query=lambda: [(local.card(1), 'NVIDIA A100')])
        self.assertEqual(gpu_resources.slurm_cards(dict(grant, gpu_model='h100sxm', gpu_count=1),
            dict(env, CUDA_VISIBLE_DEVICES='0'), query=lambda: [(local.card(1), 'NVIDIA H100 SXM5')]), [local.card(1)])

    def test_gate_rechecks_site_target_count_model_variant_and_cpu_bounds(self):
        example = (Path(__file__).resolve().parents[2] / 'setup/slurm/site.example.toml').read_text()
        account = slurmsite.account_entry(slurmsite.load(example), 'acct1')
        grant = remote_grant()
        request = dict(project='project-a', target='a40', cards=2, minutes=grant['minutes'], queue_minutes=10,
                       memory=grant['RAM'], command='true', variables={}, resume=False, host='worker',
                       run_url='https://github.com/example/project/actions/runs/1',
                       allocation=grant, cores=grant['cores'], workers=grant['workers'])
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'envs/project-a-cuda13').mkdir(parents=True)
            self.assertEqual(slurmgate.check_request(request, account, directory), request)
            for changes in ({'gpu_count': 1}, {'variant': 'cpu'}, {'gpu_model': 'v100'},
                            {'slurm_target': 'other/a40'}, {'cores': 20, 'cpu_groups': 5},
                            {'allocation_schema': 1}):
                changed = dict(request, allocation=dict(grant, **changes))
                with self.subTest(changes=changes), self.assertRaises(slurmgate.GateError):
                    slurmgate.check_request(changed, account, directory)

    def test_actual_gpu_receipt_and_original_failure_survive_supervision(self):
        grant = remote_grant()
        observed = [(local.card(1), 'NVIDIA A40'), (local.card(2), 'NVIDIA A40')]
        with (tempfile.TemporaryDirectory() as directory,
              mock.patch.dict(os.environ, SLURM_JOB_ID='123', CUDA_VISIBLE_DEVICES='0,1'),
              mock.patch.object(task_execution, 'resource_limits', return_value=(None, 1 << 63)),
              mock.patch.object(task_execution, 'slurm_cpu_width', return_value=4),
              mock.patch.object(gpu_resources, 'slurm_cards', return_value=observed),
              mock.patch.object(gpu_resources, 'available', return_value=dict.fromkeys([r[0] for r in observed], 8192)),
              mock.patch.object(task_execution.part, 'supervise', return_value=1) as supervisor):
            self.assertEqual(task_execution.supervise(directory, grant, [], 'false'), 1)
            self.assertTrue(supervisor.call_args.kwargs['p2p'])
            self.assertEqual(supervisor.call_args.kwargs['cap'], 4 << 30)
            ready = json.loads((Path(directory) / 'allocation-ready.json').read_text())
            self.assertEqual(ready['gpu_models'], ['NVIDIA A40', 'NVIDIA A40'])
            self.assertEqual(ready['gpu_uuids'], [local.card(1), local.card(2)])
            self.assertEqual(ready['variant'], 'cuda13')
            self.assertEqual(ready['slurm_job_id'], '123')


class Publication(unittest.TestCase):
    def test_success_requires_resource_evidence_but_a_test_failure_stays_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = root / 'record'
            record.mkdir()
            (record / 'outcome.json').write_text('{"outcome":"pass"}')
            (record / 'summary.md').write_text('execution')
            grant = dict(local.grant('pair'), allocation_schema=3, gpu_count=2,
                         gpu_uuids=[local.card(1), local.card(2)])
            allocation = dict(id='pair', part={'id': 'pair'}, source=source(), task_attempt=1, job_id=123, grant=grant)
            with mock.patch.object(workflow_client.executor, 'record_path', return_value=str(record)):
                with self.assertRaisesRegex(protocol.Error, 'resource receipt'):
                    workflow_client.collect_allocation_results(None, allocation, {'RUNNER_TEMP': directory}, {'state': 'success'})
                (record / 'outcome.json').write_text('{"outcome":"red"}')
                workflow_client.collect_allocation_results(None, allocation, {'RUNNER_TEMP': directory}, {'state': 'failure'})
                self.assertEqual(json.loads((root / 'ci-allocation/pair/outcome.json').read_text())['outcome'], 'red')
                receipt = {key: grant[key] for key in ('allocation_id', 'cores', 'RAM', 'workers', 'gpu_uuids')}
                (record / 'allocation-ready.json').write_text(json.dumps(receipt))
                (record / 'outcome.json').write_text('{"outcome":"pass"}')
                workflow_client.collect_allocation_results(None, allocation, {'RUNNER_TEMP': directory}, {'state': 'success'})
                self.assertEqual(json.loads((root / 'ci-allocation/pair/allocation-ready.json').read_text()), receipt)
                allocation['source']['environment_recipe'] = {'revision': 'a' * 40}
                workflow_client.collect_allocation_results(None, allocation, {'RUNNER_TEMP': directory}, {'state': 'failure'})
                metadata = json.loads((root / 'ci-allocation/pair/allocation-result.json').read_text())
                self.assertEqual(metadata['environment_preparation'],
                                 {'mode': 'pinned-cache', 'ready': False, 'revision': None})
                self.assertNotIn(str(root), json.dumps(metadata['environment_preparation']))
                (record / 'environment-preparation.json').write_text(json.dumps(
                    {'prefix': str(root / 'private-prefix'), 'revision': 'a' * 40}))
                workflow_client.collect_allocation_results(None, allocation, {'RUNNER_TEMP': directory}, {'state': 'failure'})
                metadata = json.loads((root / 'ci-allocation/pair/allocation-result.json').read_text())
                self.assertEqual(metadata['environment_preparation'],
                                 {'mode': 'pinned-cache', 'ready': True, 'revision': 'a' * 40})
                self.assertNotIn(str(root), json.dumps(metadata['environment_preparation']))


if __name__ == '__main__':
    unittest.main()
