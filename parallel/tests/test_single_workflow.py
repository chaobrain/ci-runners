"""Behavioral checks for CPU tasks sharing their source workflow run."""
from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from parallel import protocol, workflow_client, github_coordinator, executor, scheduler, scheduler_store
from tests import yamlsub
from parallel.tests.test_central import plan, source

ROOT = Path(__file__).resolve().parents[2]


class SingleRunScheduling(unittest.TestCase):
    def setUp(self):
        self.now = 1000.
        self.store = scheduler_store.Store(':memory:', clock=lambda: self.now)
        self.addCleanup(self.store.close)
        self.store.configure({'3090a': 1}, [{'tag': '3090a-0', 'pool': '3090a', 'host': 'worker'}])
        self.store.heartbeat('worker', [{'tag': '3090a-0', 'online': True, 'busy': False}])
        self.api = mock.Mock()
        self.api.run_attempt.return_value = {'status': 'in_progress', 'run_attempt': 1}
        self.api.jobs.return_value = []
        self.api.artifacts.return_value = []
        self.coordinator = github_coordinator.WorkflowCoordinator(self.store, self.api, clock=lambda: self.now)

    def submit(self, count=1, *, run=1, retries=1, max_parallel=1):
        batch_id = self.store.submit(source(run), plan(count),
                                     {f'test-{index}': ['3090a'] for index in range(count)},
                                     retries=retries, max_parallel=max_parallel)
        return batch_id, self.store.batch(batch_id)['tasks']

    def allocate(self, batch_id, task, attempt=1, job_id=101):
        self.store.request_task_allocation(batch_id, task['id'], attempt)
        allocation = self.store.reserve()
        return self.store.claim(allocation['id'], 'worker', allocation['runner'], 'example/project',
                                allocation['run_id'], 1, job_id=job_id)

    def release(self, allocation, result=None):
        self.store.event('worker', 'release-'+allocation['id'], allocation['id'], 'released', result or {'state': 'success'})

    def complete(self, allocation, *, conclusion='success'):
        self.store.runner_finished(allocation['id'], allocation['run_id'], 1,
                                   job_id=allocation['job_id'], conclusion=conclusion)

    def job(self, allocation, *, status='completed', conclusion='success'):
        return {'id': allocation['job_id'], 'run_id': allocation['run_id'],
                'name': 'CPU test / first / '+protocol.execution_job_name(allocation['task'], allocation['task_attempt']),
                'runner_name': allocation['runner'], 'labels': [allocation['runner']],
                'status': status, 'conclusion': conclusion}

    def test_only_requested_tasks_reserve_and_duplicate_requests_are_idempotent(self):
        batch_id, tasks = self.submit(3)
        self.assertIsNone(self.store.reserve())
        allocation = self.allocate(batch_id, tasks[1])
        repeated = self.store.request_task_allocation(batch_id, tasks[1]['id'], 1)
        self.assertEqual(repeated['allocation']['id'], allocation['id'])
        self.assertEqual(len(self.store.allocations()), 1)
        self.assertEqual(self.store.capacity()[0]['groups_free'], 0)
        self.assertEqual(allocation['run_id'], 1)
        self.assertIsNone(self.store.reserve())

    def test_three_tasks_reuse_one_runner_without_waiting_for_the_run_to_end(self):
        for max_parallel in (1, 3):
            batch_id, tasks = self.submit(3, run=max_parallel, max_parallel=max_parallel)
            for task in tasks:
                self.store.request_task_allocation(batch_id, task['id'], 1)
            for index, task in enumerate(tasks):
                allocation = self.allocate(batch_id, task, job_id=101+index)
                self.release(allocation)
                self.assertIsNone(self.store.reserve())
                self.api.jobs.return_value = [self.job(allocation)]
                self.api.artifacts.return_value = [{'id': index+1, 'name': 'ci-allocation-'+allocation['id'], 'expired': False}]
                self.coordinator.reconcile()
                self.assertTrue(self.store.allocation(allocation['id'])['runner_done'])
            self.assertEqual(self.store.batch(batch_id)['state'], 'success')
            self.assertEqual(self.api.run_attempt.return_value['status'], 'in_progress')
            self.assertFalse(self.api.api.called)

    def test_assertion_failure_keeps_other_tasks_runnable_and_fails_the_batch(self):
        batch_id, tasks = self.submit(2)
        failed = self.allocate(batch_id, tasks[0])
        self.release(failed, {'state': 'failure', 'retryable': True})
        self.complete(failed, conclusion='failure')
        self.assertEqual(self.store.task_status(batch_id, tasks[0]['id'])['state'], 'failure')
        self.assertEqual(self.store.batch(batch_id)['state'], 'pending')
        second = self.allocate(batch_id, tasks[1], job_id=102)
        self.release(second)
        self.complete(second)
        self.assertEqual(self.store.batch(batch_id)['state'], 'failure')

    def test_retry_requires_a_new_request_and_keeps_the_same_source_run(self):
        batch_id, tasks = self.submit()
        first = self.allocate(batch_id, tasks[0])
        self.store.event('worker', 'started', first['id'], 'allocated')
        self.release(first, {'state': 'error', 'retryable': True})
        self.complete(first, conclusion='failure')
        self.assertIsNone(self.store.reserve())
        second = self.allocate(batch_id, tasks[0], attempt=2, job_id=102)
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual((second['run_id'], second['run_attempt'], second['task_attempt']), (1, 1, 2))
        self.release(second, {'state': 'error', 'retryable': True})
        self.complete(second, conclusion='failure')
        self.assertEqual(self.store.batch(batch_id)['state'], 'error')
        self.assertIsNone(self.store.reserve())

    def test_another_job_or_source_attempt_cannot_reuse_a_claim(self):
        batch_id, tasks = self.submit()
        allocation = self.allocate(batch_id, tasks[0])
        for run_id, run_attempt, job_id in [(2, 1, 101), (1, 2, 101), (1, 1, 102)]:
            with self.subTest(identity=(run_id, run_attempt, job_id)), self.assertRaises(protocol.Error):
                self.store.claim(allocation['id'], 'worker', allocation['runner'], 'example/project',
                                 run_id, run_attempt, job_id=job_id)
        with self.assertRaises(protocol.Error):
            self.store.runner_finished(allocation['id'], 1, 1, job_id=102)
        self.assertFalse(self.store.allocation(allocation['id'])['runner_done'])

    def test_missing_execution_job_does_not_free_a_reservation_while_source_is_active(self):
        batch_id, tasks = self.submit()
        allocation = self.allocate(batch_id, tasks[0])
        self.coordinator.reconcile()
        self.assertFalse(self.store.allocation(allocation['id'])['runner_done'])
        self.assertEqual(self.store.capacity()[0]['groups_free'], 0)
        self.api.run_attempt.return_value = {'status': 'completed', 'conclusion': 'failure'}
        self.coordinator.reconcile()
        self.assertTrue(self.store.allocation(allocation['id'])['runner_done'])
        self.assertEqual(self.store.capacity()[0]['groups_free'], 0)
        self.release(allocation, {'state': 'error', 'retryable': True})
        self.assertEqual(self.store.batch(batch_id)['state'], 'error')
        self.assertIsNone(self.store.reserve())

    def test_queue_deadline_revokes_a_grant_without_cancelling_the_shared_run(self):
        batch_id, tasks = self.submit()
        allocation = self.allocate(batch_id, tasks[0])
        self.now += 901
        self.coordinator.reconcile()
        self.assertTrue(self.store.allocation(allocation['id'])['cancelled'])
        self.assertFalse(self.store.allocation(allocation['id'])['runner_done'])
        self.assertEqual(self.store.capacity()[0]['groups_free'], 0)
        self.assertEqual(self.store.batch(batch_id)['state'], 'pending')
        self.api.api.assert_not_called()

    def test_queued_job_without_runner_does_not_block_other_task_reconciliation(self):
        batch_id, tasks = self.submit(2)
        self.store.request_task_allocation(batch_id, tasks[0]['id'], 1)
        allocation = self.store.reserve()
        queued_job = dict(self.job(allocation, status='queued'), id=101, runner_name=None)
        failed_request = {'name': protocol.allocation_job_name(tasks[1]['id'], 1),
                          'status': 'completed', 'conclusion': 'failure'}
        self.api.jobs.return_value = [queued_job, failed_request]
        self.coordinator.reconcile()
        self.assertFalse(self.store.allocation(allocation['id'])['runner_done'])
        self.assertEqual(self.store.capacity()[0]['groups_free'], 0)
        self.assertEqual(self.store.task_status(batch_id, tasks[1]['id'])['state'], 'error')

    def test_completed_job_waits_for_cleanup_and_upload_failure_cannot_be_green(self):
        batch_id, tasks = self.submit()
        allocation = self.allocate(batch_id, tasks[0])
        self.api.jobs.return_value = [self.job(allocation, conclusion='failure')]
        self.coordinator.reconcile()
        self.assertEqual(self.store.batch(batch_id)['state'], 'pending')
        self.assertEqual(self.store.capacity()[0]['groups_free'], 0)
        self.release(allocation)
        self.assertEqual(self.store.batch(batch_id)['state'], 'error')

    def test_cancelled_source_cancels_waiting_and_running_tasks_without_starting_retry(self):
        batch_id, tasks = self.submit(2)
        allocation = self.allocate(batch_id, tasks[0])
        self.api.run_attempt.return_value = {'status': 'completed', 'conclusion': 'cancelled'}
        self.api.jobs.return_value = [self.job(allocation, conclusion='cancelled')]
        self.coordinator.reconcile()
        self.assertTrue(self.store.allocation(allocation['id'])['cancelled'])
        self.assertEqual(self.store.task_status(batch_id, tasks[1]['id'])['state'], 'cancelled')
        self.release(allocation, {'state': 'cancelled'})
        self.assertEqual(self.store.batch(batch_id)['state'], 'cancelled')
        self.assertIsNone(self.store.reserve())
        self.assertFalse(self.api.api.called)

    def test_failed_allocation_request_closes_the_task_without_waiting_for_deadline(self):
        batch_id, tasks = self.submit()
        self.api.jobs.return_value = [{'name': protocol.allocation_job_name(tasks[0]['id'], 1),
                                      'status': 'completed', 'conclusion': 'failure'}]
        self.coordinator.reconcile()
        self.assertEqual(self.store.batch(batch_id)['state'], 'error')

    def test_empty_plan_is_success_and_new_push_has_a_distinct_batch(self):
        first, _ = self.submit(0)
        second, _ = self.submit(0, run=2)
        self.assertNotEqual(first, second)
        self.assertEqual(self.store.batch(first)['state'], 'success')
        self.assertEqual(self.store.batch(second)['state'], 'success')
        self.assertIsNone(self.store.reserve())

    def test_request_rejects_another_source_attempt_and_requires_an_active_request_job(self):
        batch_id, tasks = self.submit()
        app = scheduler.Scheduler.__new__(scheduler.Scheduler)
        app.store, app.api = self.store, self.api
        params = {'batch_id': batch_id, 'task_id': tasks[0]['id'], 'task_attempt': 1,
                  'source_identity': {'repository': 'example/project', 'run_id': 1, 'run_attempt': 2}}
        with self.assertRaisesRegex(protocol.Error, 're-run all jobs'):
            app.request_allocation(params)
        params['source_identity']['run_attempt'] = 1
        with self.assertRaisesRegex(protocol.Error, 'not active'):
            app.request_allocation(params)
        self.api.jobs.return_value = [{'name': protocol.allocation_job_name(tasks[0]['id'], 1), 'status': 'in_progress'}]
        app.request_allocation(params)
        self.assertIsNotNone(self.store.reserve())


class WorkflowBridge(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.env = {'GITHUB_REPOSITORY': 'example/project', 'GITHUB_RUN_ID': '1', 'GITHUB_RUN_ATTEMPT': '1',
                    'GITHUB_OUTPUT': str(Path(self.directory.name)/'outputs'),
                    'GITHUB_STEP_SUMMARY': str(Path(self.directory.name)/'summary')}
        self.args = argparse.Namespace(batch_id='batch', task_id='task', task_attempt=1,
                                       operation='batch-result', submission_state='accepted')
        self.connection = mock.Mock()

    def test_allocation_wait_uses_one_request_and_polls_without_resubmitting(self):
        task = {'state': 'queued', 'part': {'queue_minutes': 1}, 'allocation': None}
        allocation = {'runner': '3090a-0', 'grant': {'allocation_id': 'assigned'}}
        self.connection.call.side_effect = [task, {**task, 'allocation': allocation}]
        with mock.patch.object(workflow_client.time, 'sleep'):
            self.assertEqual(workflow_client.wait_for_task_allocation(self.args, self.connection, self.env), 0)
        self.assertEqual([call.args[0] for call in self.connection.call.call_args_list], ['request_allocation', 'task'])
        outputs = Path(self.env['GITHUB_OUTPUT']).read_text()
        self.assertIn('3090a-0', outputs)
        self.assertIn('assigned', outputs)

    def test_batch_verdict_fails_on_failure_error_and_cancelled(self):
        for state in protocol.TERMINAL:
            self.connection.call.return_value = {'state': state}
            with self.subTest(state=state):
                self.assertEqual(workflow_client.report_final_result(self.args, self.connection, self.env),
                                 0 if state == 'success' else 1)

    def test_missing_plan_is_failure_but_plan_only_validation_needs_no_batch(self):
        self.args.batch_id = ''
        self.args.submission_state = ''
        self.assertEqual(workflow_client.report_final_result(self.args, self.connection, self.env), 1)
        self.args.submission_state = 'validated'
        self.assertEqual(workflow_client.report_final_result(self.args, self.connection, self.env), 0)
        self.connection.call.assert_not_called()

    def test_final_result_waits_for_terminal_evidence_and_has_a_bound(self):
        self.connection.call.side_effect = [{'state': 'pending'}, {'state': 'success'}]
        with mock.patch.object(workflow_client.time, 'sleep'):
            self.assertEqual(workflow_client.report_final_result(self.args, self.connection, self.env), 0)
        self.connection.call.side_effect = None
        self.connection.call.return_value = {'state': 'pending'}
        with mock.patch.object(workflow_client.time, 'monotonic', side_effect=[0, 601]):
            with self.assertRaisesRegex(protocol.Error, 'still pending'):
                workflow_client.wait_for_terminal_result(self.connection, 'batch', {})

    def test_task_attempts_have_separate_records(self):
        inv = SimpleNamespace(host={'data': self.directory.name})
        metadata = {'env': self.env, 'part': {'id': 'pytest'}, 'task_attempt': 1}
        first = executor.record_path(inv, metadata)
        second = executor.record_path(inv, {**metadata, 'task_attempt': 2})
        self.assertNotEqual(first, second)
        self.assertTrue(first.endswith('/pytest-1'))
        self.assertTrue(second.endswith('/pytest-2'))

    def test_retry_artifact_contains_the_retry_record_and_job_identity(self):
        inv = SimpleNamespace(host={'data': self.directory.name})
        env = {**self.env, 'RUNNER_TEMP': str(Path(self.directory.name)/'runner-temp')}
        for attempt, state in [(1, 'error'), (2, 'success')]:
            record = Path(executor.record_path(inv, {'env': env, 'part': {'id': 'pytest'}, 'task_attempt': attempt}))
            record.mkdir(parents=True)
            (record/'outcome.json').write_text(json.dumps({'state': state}))
            (record/'summary.md').write_text(f'attempt {attempt}')
        allocation = {'id': 'retry-allocation', 'part': {'id': 'pytest'}, 'grant': {'task_attempt': 2},
                      'source': source(), 'job_id': 102, 'task_attempt': 2}
        workflow_client.collect_allocation_results(inv, allocation, env, {'state': 'success'})
        artifact = Path(env['RUNNER_TEMP'])/'ci-allocation/retry-allocation'
        self.assertEqual(json.loads((artifact/'outcome.json').read_text())['state'], 'success')
        self.assertEqual((artifact/'summary.md').read_text(), 'attempt 2')
        metadata = json.loads((artifact/'allocation-result.json').read_text())
        self.assertEqual((metadata['source_run_id'], metadata['execution_job_id'], metadata['task_attempt']), (1, 102, 2))

    def test_workflow_dependency_chain_carries_failure_to_the_source_run(self):
        workflow = yamlsub.load((ROOT/'.github/workflows/ci-v2.yml').read_text())
        task = yamlsub.load((ROOT/'.github/workflows/cpu-task.yml').read_text())
        execute = yamlsub.load((ROOT/'.github/workflows/execute.yml').read_text())
        self.assertEqual(workflow['jobs']['parts']['needs'], 'plan')
        self.assertFalse(workflow['jobs']['parts']['strategy']['fail-fast'])
        self.assertEqual(workflow['jobs']['verdict']['needs'], ['plan', 'parts', 'gpu_parts'])
        self.assertIn('always()', workflow['jobs']['verdict']['if'])
        self.assertIn("task_count != '0'", workflow['jobs']['parts']['if'])
        self.assertNotIn('continue-on-error', workflow['jobs']['parts'])
        self.assertNotIn('continue-on-error', task['jobs']['result'])
        self.assertEqual(task['jobs']['first']['needs'], 'allocate_first')
        self.assertEqual(task['jobs']['retry']['needs'], 'allocate_retry')
        self.assertEqual(task['jobs']['result']['needs'], ['allocate_first', 'first', 'allocate_retry', 'retry'])
        self.assertTrue(execute['jobs']['execute']['continue-on-error'])
        for template in (workflow, task, execute):
            self.assertEqual(set(template['on']), {'workflow_call'})


class GitHubIdentity(unittest.TestCase):
    def test_job_listing_is_scoped_to_the_attempt_and_paginates(self):
        api = github_coordinator.GitHub('unused')
        api.api = mock.Mock(side_effect=[{'jobs': [{'id': index} for index in range(100)]}, {'jobs': [{'id': 100}]}])
        self.assertEqual(len(api.jobs(source())), 101)
        paths = [call.args[1] for call in api.api.call_args_list]
        self.assertIn('/runs/1/attempts/1/jobs?', paths[0])
        self.assertTrue(paths[1].endswith('page=2'))
        with self.assertRaises(protocol.Error):
            github_coordinator.find_job([{'name': 'execute/task/1'}, {'name': 'other / execute/task/1'}], 'execute/task/1')


class DatabaseUpgrade(unittest.TestCase):
    def test_v1_upgrade_preserves_history_and_refuses_pending_work(self):
        schema = (ROOT/'tests/fixtures/scheduler-v1.sql').read_text()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'scheduler.db'
            with sqlite3.connect(path) as database:
                database.executescript(schema)
                database.execute("INSERT INTO batches(id,source,digest,created,state,max_parallel,retries,cancel_key) VALUES('old',?,'hash',0,'pending',1,1,'key')",
                                 (json.dumps(source()),))
            with self.assertRaisesRegex(protocol.Error, 'drain'):
                scheduler_store.Store(str(path))
            with sqlite3.connect(path) as database:
                self.assertEqual(database.execute('SELECT version FROM meta').fetchone()[0], 1)
                database.execute("UPDATE batches SET state='success'")
            store = scheduler_store.Store(str(path))
            self.addCleanup(store.close)
            self.assertEqual(store.batch('old')['state'], 'success')
            self.assertEqual(store.db.execute('SELECT version FROM meta').fetchone()[0], 4)
            self.assertTrue(store.batch('old')['source_done'])


if __name__ == '__main__':
    unittest.main()
