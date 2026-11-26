"""Report gates use real SQLite and immutable bundles; GitHub is a fixture boundary."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from ci_runners import storage, storage_host
from parallel import bundles, github_coordinator, protocol, reporting, scheduler, scheduler_store
from parallel import workflow_client
from parallel.tests.test_central import source, plan
from parallel.tests.test_storage import LocalTransport, BOUND


class Gate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = 1000.
        self.path = str(self.root/'controller.db')
        self.store = scheduler_store.Store(self.path, clock=lambda: self.now)
        self.addCleanup(lambda: self.store.close())
        self.src = source(storage=BOUND, repository_id=17, report_gate=True,
                          report_namespace='project/reports', report_plan_name='ci-plan-files')
        self.batch_id = self.store.submit(self.src, plan(0), {})
        self.api = mock.Mock()
        self.api.run_attempt.return_value = {'status': 'in_progress'}
        self.api.jobs.return_value = []
        self.coordinator = github_coordinator.WorkflowCoordinator(self.store, self.api,
                                                                  clock=lambda: self.now, storage_etc='/example/etc')
        self.ref = dict(schema=1, store='pilot', identity=reporting.receipt_identity(self.batch()),
                        manifest_sha256='a'*64)
        self.job = dict(id=101, run_id=1, name=reporting.job_name(self.batch_id), runner_name='control',
                        labels=['self-hosted', 'ci-plan'], status='in_progress', conclusion=None)

    def batch(self):
        return self.store.batch(self.batch_id)

    def test_success_waits_for_both_receipt_verification_and_job_completion(self):
        self.assertEqual(self.batch()['state'], 'success')
        self.assertEqual(self.batch()['publication_state'], 'pending')
        self.store.submit_report(self.batch_id, self.ref, 101)
        self.api.jobs.return_value = [self.job]
        with mock.patch.object(reporting, 'verify_receipt') as verify:
            self.coordinator.reconcile()
            verify.assert_not_called()
            self.assertEqual(self.batch()['publication_state'], 'pending')
            self.job.update(status='completed', conclusion='success')
            self.coordinator.reconcile()
            verify.assert_called_once()
        self.assertEqual(self.batch()['publication_state'], 'success')
        self.coordinator.publish_batch(self.batch_id)
        self.assertEqual(self.api.api.call_args.args[2]['state'], 'success')

    def test_missing_or_failed_reader_never_publishes_success(self):
        self.api.jobs.return_value = [dict(self.job, status='completed', conclusion='failure')]
        self.coordinator.reconcile()
        self.assertEqual(self.batch()['publication_state'], 'error')
        self.coordinator.publish_batch(self.batch_id)
        self.assertEqual(self.api.api.call_args.args[2]['state'], 'error')

    def test_successful_job_without_receipt_and_corrupt_receipt_fail_closed(self):
        self.api.jobs.return_value = [dict(self.job, status='completed', conclusion='success')]
        self.store.submit_report(self.batch_id, self.ref, 101)
        with mock.patch.object(reporting, 'verify_receipt', side_effect=storage.Error('missing')):
            self.coordinator.reconcile()
        self.assertEqual(self.batch()['publication_state'], 'error')

    def test_missing_reporter_has_bounded_source_completion_grace(self):
        self.api.run_attempt.return_value = dict(status='completed', conclusion='failure')
        self.coordinator.reconcile()
        self.now += 59
        self.coordinator.reconcile()
        self.assertEqual(self.batch()['publication_state'], 'pending')
        self.now += 1
        self.coordinator.reconcile()
        self.assertEqual(self.batch()['publication_state'], 'error')
        with self.assertRaises(protocol.Error):
            self.store.submit_report(self.batch_id, self.ref, 101)

    def test_immutable_submission_survives_restart_and_rejects_conflicts(self):
        self.store.submit_report(self.batch_id, self.ref, 101)
        self.store.close()
        self.store = scheduler_store.Store(self.path, clock=lambda: self.now)
        self.coordinator.store = self.store
        self.store.submit_report(self.batch_id, self.ref, 101)
        for ref, job in ((dict(self.ref, manifest_sha256='b'*64), 101), (self.ref, 102)):
            with self.subTest(job=job), self.assertRaises(protocol.Error):
                self.store.submit_report(self.batch_id, ref, job)
        self.assertEqual(self.batch()['report']['reference'], self.ref)

    def test_cancellation_after_reader_success_invalidates_publication(self):
        self.store.submit_report(self.batch_id, self.ref, 101)
        self.store.finish_report(self.batch_id, success=True)
        self.api.run_attempt.return_value = dict(status='completed', conclusion='cancelled')
        self.coordinator.reconcile()
        self.assertEqual(self.batch()['publication_state'], 'error')

    def test_supersession_closes_pending_gate_and_preserves_completed_history(self):
        new_source = dict(self.src, run_id=2, cancel_in_progress=True)
        self.store.submit(new_source, plan(0), {})
        self.assertEqual(self.batch()['publication_state'], 'error')
        self.batch_id = protocol.source_key(new_source)
        self.ref = dict(self.ref, identity=reporting.receipt_identity(self.batch()))
        self.store.submit_report(self.batch_id, self.ref, 101)
        self.store.finish_report(self.batch_id, success=True)
        self.store.source_finished(self.batch_id, cancelled=False)
        self.store.submit(dict(new_source, run_id=3), plan(0), {})
        self.assertEqual(self.batch()['publication_state'], 'success')

    def test_native_failure_stays_red_when_report_also_fails(self):
        self.store.db.execute("UPDATE batches SET state='failure' WHERE id=?", (self.batch_id,))
        self.store.finish_report(self.batch_id, success=False)
        self.assertEqual(self.batch()['report']['state'], 'error')
        self.assertEqual(self.batch()['publication_state'], 'failure')

    def test_ungated_success_remains_compatible(self):
        key = self.store.submit(source(2), plan(0), {})
        batch = self.store.batch(key)
        self.assertIsNone(batch['report'])
        self.assertEqual(batch['publication_state'], 'success')
        with self.assertRaises(protocol.Error):
            self.store.submit_report(key, self.ref, 101)

    def test_report_rpc_checks_source_attempt_job_runner_and_immutable_identity(self):
        app = scheduler.Scheduler.__new__(scheduler.Scheduler)
        app.store, app.api = self.store, self.api
        self.api.jobs.return_value = [self.job]
        args = dict(batch_id=self.batch_id, reference=self.ref, runner='control',
                    source_identity=dict(repository='example/project', run_id=1, run_attempt=1))
        for change in (dict(runner='other'),
                       dict(source_identity=dict(repository='example/project', run_id=1, run_attempt=2)),
                       dict(reference=dict(self.ref, identity=dict(self.ref['identity'], sha='b'*40)))):
            with self.subTest(change=change), self.assertRaises((protocol.Error, storage.Error)):
                app.call('report', {**args, **change}, 'local')
        for job in (dict(self.job, name='other'), dict(self.job, labels=[]), dict(self.job, run_id=2), dict(self.job, id=True),
                    dict(self.job, status='completed', conclusion='success')):
            self.api.jobs.return_value = [job]
            with self.subTest(job=job), self.assertRaises(protocol.Error):
                app.call('report', args, 'local')
        self.api.jobs.return_value = [self.job]
        self.assertEqual(app.call('report', args, 'local'), {'accepted': True})
        self.api.jobs.return_value = [dict(self.job, status='completed', conclusion='success')]
        self.assertEqual(app.call('report', args, 'local'), {'accepted': True})


class Receipt(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root/'store').mkdir(mode=0o700)
        self.host = storage_host.Store(self.root/'store')
        self.client = LocalTransport(self.host)
        self.client.store = self.host.store
        self.store = scheduler_store.Store(':memory:')
        self.addCleanup(self.store.close)
        self.store.configure({'3090a': 1}, [dict(tag='3090a-0', pool='3090a', host='worker')])
        self.store.heartbeat('worker', [dict(tag='3090a-0', online=True, busy=False)], allocation_schema=5)
        self.src = source(storage=BOUND, repository_id=17, report_gate=True,
                          report_namespace='project/reports', report_plan_name='ci-plan-files',
                          run_head_sha='c'*40)

    def publish(self, identity, files):
        directory = self.root/('files-'+str(len(list(self.root.glob('files-*')))))
        directory.mkdir()
        for name, data in files.items():
            path = directory/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data if isinstance(data, bytes) else json.dumps(data, sort_keys=True).encode())
        return self.client.publish_bundle(identity, directory)

    def project(self, name, files, *, source_change=None):
        meta = dict(workflow_sha=self.src['run_head_sha'], producer_checkout_sha=self.src['sha'], run_id=1,
                    run_attempt=1, repository_id=17, repository='example/project')
        meta.update(source_change or {})
        return self.publish(reporting.project_identity(self.src, name), {**files, '.ci-storage-source.json': meta})

    def fixture(self, *, missing=None, red=False, wrong_projection=False, wrong_source=False):
        files = {'selection.json': b'{"complete":true}'}
        self.src['files_bundle'] = self.publish(bundles.plan_identity(self.src), files)
        doc = plan()
        doc['parts'][0].update(artifacts=dict(rows='rows'),
                               required_files=[dict(pattern='results/junit.xml', min_bytes=1)])
        key = self.store.submit(self.src, doc, {'test-0': ['3090a']})
        task = self.store.batch(key)['tasks'][0]
        self.store.request_task_allocation(key, task['id'], 1)
        a = self.store.reserve()
        a = self.store.claim(a['id'], 'worker', a['runner'], 'example/project', 1, 1, job_id=99)
        result = dict(state='failure' if red else 'success')
        self.store.event('worker', 'release', a['id'], 'released', result)
        a = self.store.allocation(a['id'])
        native = {'outcome.json': b'{"outcome":"red"}' if red else b'{"outcome":"pass"}',
                  'summary.md': b'test result', 'results/junit.xml': b'<testsuite/>',
                  'artifacts/rows/local_ci.md': b'| pass |'}
        if missing:
            native.pop(missing)
        ref = self.publish(bundles.result_identity(a), native)
        self.store.runner_finished(a['id'], 1, 1, job_id=99, conclusion=result['state'], storage_reference=ref)
        projected = {k: v for k, v in native.items() if k in ('outcome.json', 'summary.md')}
        if wrong_projection:
            projected['summary.md'] = b'foreign result'
        outcome = self.project('ci-part-test-0-1', projected,
                               source_change=dict(producer_checkout_sha='b'*40) if wrong_source else None)
        rows = self.project('rows-test-0-1', {k[len('artifacts/rows/'):]: v for k, v in native.items()
                                            if k.startswith('artifacts/rows/')})
        seal = self.project('seal-test-0-1', {'receipt.json': dict(part='test-0', attempt=1,
                            job_state='failure' if red else 'success', missing=[], references={'rows-test-0-1': rows})})
        self.batch = self.store.batch(key)
        self.doc = dict(schema=1, batch_id=key, source={k: self.src[k] for k in reporting.SOURCE_FIELDS},
                        plan_reference=self.src['files_bundle'], plan_projection=self.project('ci-plan-files', files),
                        allocations={a['id']: dict(native_reference=ref, outcome=outcome, artifacts={'rows': rows}, seal=seal)},
                        selection_verified=True)

    def verify(self):
        reference = self.publish(reporting.receipt_identity(self.batch), {'receipt.json': self.doc})
        return reporting.verify_receipt('/example/etc', self.batch, reference, client=self.client)

    def test_complete_success_compares_real_native_and_projected_manifests(self):
        self.fixture()
        self.assertEqual(self.verify()['identity'], reporting.receipt_identity(self.batch))

    def test_success_without_junit_is_rejected(self):
        self.fixture(missing='results/junit.xml')
        with self.assertRaises(storage.Error):
            self.verify()

    def test_success_without_nonempty_rows_is_rejected(self):
        self.fixture(missing='artifacts/rows/local_ci.md')
        with self.assertRaises(storage.Error):
            self.verify()

    def test_changed_projection_is_rejected(self):
        self.fixture(wrong_projection=True)
        with self.assertRaises(storage.Error):
            self.verify()

    def test_wrong_checkout_provenance_is_rejected(self):
        self.fixture(wrong_source=True)
        with self.assertRaises(storage.Error):
            self.verify()

    def test_omitted_attempt_and_false_selection_are_rejected(self):
        self.fixture()
        self.doc['allocations'] = {}
        with self.assertRaises(storage.Error):
            self.verify()

    def test_foreign_source_attempt_is_rejected(self):
        self.fixture()
        self.doc['source']['run_attempt'] = 2
        with self.assertRaises(storage.Error):
            self.verify()

    def test_false_selection_flag_is_rejected(self):
        self.fixture()
        self.doc['selection_verified'] = False
        with self.assertRaises(storage.Error):
            self.verify()

    def test_required_projection_and_seal_cannot_be_omitted(self):
        self.fixture()
        next(iter(self.doc['allocations'].values()))['artifacts'] = {}
        with self.assertRaisesRegex(storage.Error, 'missing'):
            self.verify()

    def test_controller_native_reference_cannot_be_replaced(self):
        self.fixture()
        self.doc = deepcopy(self.doc)
        next(iter(self.doc['allocations'].values()))['native_reference']['manifest_sha256'] = '0'*64
        with self.assertRaises(storage.Error):
            self.verify()

    def test_genuine_red_receipt_can_verify_without_turning_execution_green(self):
        self.fixture(red=True)
        self.verify()
        self.assertEqual(self.batch['state'], 'failure')


class ReportPolicy(unittest.TestCase):
    def test_schema_four_upgrade_requires_drain_and_preserves_ungated_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'controller.db'
            store = scheduler_store.Store(str(path))
            key = store.submit(source(), plan(0), {})
            store.db.execute('DROP TABLE batch_reports')
            store.db.execute('UPDATE meta SET version=4')
            store.db.execute("UPDATE batches SET state='pending'")
            store.close()
            with self.assertRaisesRegex(protocol.Error, 'drain'):
                scheduler_store.Store(str(path))
            import sqlite3
            with sqlite3.connect(path) as database:
                self.assertEqual(database.execute('SELECT version FROM meta').fetchone()[0], 4)
                database.execute("UPDATE batches SET state='success'")
            store = scheduler_store.Store(str(path))
            try:
                self.assertEqual(store.db.execute('SELECT version FROM meta').fetchone()[0], 5)
                self.assertIsNone(store.batch(key)['report'])
                self.assertEqual(store.batch(key)['publication_state'], 'success')
            finally:
                store.close()

    def test_report_job_has_no_write_authority_and_verdict_has_no_cycle(self):
        from tests import yamlsub
        root = Path(__file__).resolve().parents[2]
        doc = yamlsub.load((root/'.github/workflows/ci-v2.yml').read_text())
        jobs = doc['jobs']
        self.assertEqual(jobs['execution_result']['needs'], ['plan', 'parts', 'gpu_parts'])
        self.assertEqual(jobs['verify_reports']['needs'], ['plan', 'execution_result'])
        self.assertIn('verify_reports', jobs['verdict']['needs'])
        self.assertEqual(jobs['verify_reports']['name'], 'verify-reports/${{ needs.plan.outputs.batch_id }}')
        self.assertEqual(set(jobs['verify_reports']['permissions'].values()), {'read'})
        self.assertIn('unset GH_TOKEN GITHUB_TOKEN', str(jobs['verify_reports']))
        self.assertIn("inputs.report-gate", jobs['verify_reports']['if'])
        self.assertIs(doc['on']['workflow_call']['inputs']['report-gate']['default'], False)

    def test_final_client_waits_for_publication_but_execution_reader_does_not(self):
        connection = mock.Mock()
        connection.call.side_effect = [dict(state='success', publication_state='pending'),
                                       dict(state='success', publication_state='error')]
        with mock.patch.object(workflow_client.time, 'sleep'):
            result = workflow_client.wait_for_terminal_result(connection, 'batch', {}, publication=True)
        self.assertEqual((result['state'], result['execution_state']), ('error', 'success'))
        connection.call.return_value = dict(state='success', publication_state='pending')
        connection.call.side_effect = None
        self.assertEqual(workflow_client.wait_for_terminal_result(connection, 'batch', {})['state'], 'success')

    def test_required_context_cannot_disable_or_spoof_namespace(self):
        p = {17: dict(repository='example/project', contexts=['project/ci'], namespace='project/reports', plan_name='plan')}
        src = source(repository_id=17)
        with self.assertRaises(protocol.Error):
            reporting.bind(src, p)
        bound = reporting.bind(dict(src, report_gate=True, report_namespace='forged'), p)
        self.assertEqual(bound['report_namespace'], 'project/reports')
        for change in (dict(repository_id=18), dict(repository='example/other'), dict(context='other'), dict(report_gate=1)):
            with self.subTest(change=change), self.assertRaises(protocol.Error):
                reporting.bind({**src, 'report_gate': True, **change}, p)

    def test_required_file_paths_are_bounded_relative_patterns(self):
        for pattern in ('../junit.xml', '/junit.xml', 'a//b', 'a/./b', 'a/../../b', 'x'*201):
            with self.subTest(pattern=pattern), self.assertRaises(protocol.Error):
                reporting.requirements([dict(pattern=pattern, min_bytes=1)])
        with self.assertRaises(protocol.Error):
            reporting.requirements([dict(pattern='results/*.xml', min_bytes=True)])
        reporting.require_members({'results/junit.xml': ('a'*64, 10)}, [dict(pattern='results/*.xml', min_bytes=10)])


if __name__ == '__main__':
    unittest.main()
