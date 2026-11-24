"""Real bundle files and SQLite records; transport and GitHub remain fixture boundaries."""
from __future__ import annotations

import dataclasses
import io
import json
import os
import shutil
import stat
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ci_runners import cli, storage, storage_host, storage_ssh
from parallel import bundles, github_coordinator, protocol, scheduler, scheduler_store, workflow_client
from parallel.tests import test_single_workflow
from tests import yamlsub

ROOT = Path(__file__).resolve().parents[2]
BOUND = {'schema': 1, 'backend': 'ssh', 'store': 'pilot'}


def ident(**updates):
    return dict(repository_id=17, repository='example/project', run_id=12, run_attempt=1,
                kind='plan', part_id='plan', part_attempt=1, bundle_name='plan-test',
                sha='a'*40, context='project/ci', **updates)


class LocalTransport:
    """Use the production publication/verification helper without a network fixture."""
    def __init__(self, host):
        self.host, self.limits = host, dict(host.limits)

    def publish_bundle(self, identity, source, retention='diagnostic'):
        stage = self.host.begin()['stage']
        directory = self.host.root/'.staging'/stage
        doc = storage.pack(source, directory/'bundle.zip', identity, self.limits, retention)
        (directory/'manifest.json').write_bytes(storage.dumps(doc))
        return self.host.commit(stage, identity)['reference']

    def read_bundle(self, reference, identity, destination):
        storage.reference(reference, identity, self.host.store)
        directory = self.host.location(identity)
        return storage.unpack((directory/'manifest.json').read_bytes(), directory/'bundle.zip', identity,
                              destination, self.limits, reference['manifest_sha256'])

    def describe(self, identity):
        return self.host.describe(identity)


class BundleFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.host = storage_host.Store(self.root)
        self.transport = LocalTransport(self.host)
        self.files = self.root/'files'
        self.files.mkdir()
        (self.files/'outcome.json').write_text('{"outcome":"pass"}')
        (self.files/'nested').mkdir()
        (self.files/'nested/data').write_bytes(b'\0bytes\xff')

    def test_roundtrip_idempotency_and_staging_visibility(self):
        stage = self.host.begin()['stage']
        self.assertTrue((self.root/'.staging'/stage).is_dir())
        run = {k: ident()[k] for k in ('repository_id', 'repository', 'run_id', 'run_attempt')}
        self.assertEqual(self.host.list_completed(run)['references'], [])
        with self.assertRaises(storage.Error):
            self.host.describe(ident())
        first = self.transport.publish_bundle(ident(), self.files)
        self.assertEqual(self.transport.publish_bundle(ident(), self.files), first)
        self.transport.read_bundle(first, ident(), self.root/'download')
        self.assertEqual((self.root/'download/nested/data').read_bytes(), b'\0bytes\xff')
        self.assertEqual(self.host.list_completed(run)['references'], [first])

    def test_conflicting_content_never_overwrites(self):
        first = self.transport.publish_bundle(ident(), self.files)
        (self.files/'outcome.json').write_text('{"outcome":"red"}')
        with self.assertRaisesRegex(storage.Error, 'conflict'):
            self.transport.publish_bundle(ident(), self.files)
        self.assertEqual(self.host.describe(ident())['reference'], first)

    def test_missing_config_defaults_and_invalid_config_refuses(self):
        self.assertEqual(storage.read_config(self.root)['default_backend'], 'github')
        file = self.root/'storage.toml'
        file.write_text('schema=1\ndefault_backend="ssh"\n')
        with self.assertRaises(storage.Error):
            storage.read_config(self.root)
        file.write_text((ROOT/'parallel/examples/storage.example.toml').read_text())
        self.assertEqual(storage.select(storage.read_config(self.root), 'example/project'), BOUND)

    def test_digest_and_identity_mismatch_are_rejected(self):
        self.transport.publish_bundle(ident(), self.files)
        location = self.host.location(ident())
        manifest = json.loads((location/'manifest.json').read_bytes())
        manifest['sha256'] = '0'*64
        with self.assertRaises(storage.Error):
            storage.verify(storage.dumps(manifest), location/'bundle.zip', ident(), storage.DEFAULTS)
        with self.assertRaises(storage.Error):
            storage.verify((location/'manifest.json').read_bytes(), location/'bundle.zip',
                           {**ident(), 'sha': 'b'*40}, storage.DEFAULTS)

    def test_manifest_reference_cannot_select_another_attempt(self):
        reference = self.transport.publish_bundle(ident(), self.files)
        for field, value in [('repository_id', 18), ('run_id', 13), ('run_attempt', 2), ('part_attempt', 2)]:
            with self.subTest(field=field), self.assertRaises(storage.Error):
                storage.reference(reference, {**ident(), field: value})

    def test_limits_and_symlink_inputs_are_rejected(self):
        with self.assertRaisesRegex(storage.Error, '^CI storage: limit$'):
            storage.pack(self.files, self.root/'limited.zip', ident(), {**storage.DEFAULTS, 'unpacked_bytes': 1})
        (self.files/'link').symlink_to(self.files/'outcome.json')
        with self.assertRaisesRegex(storage.Error, '^CI storage: corrupt$'):
            storage.pack(self.files, self.root/'linked.zip', ident(), storage.DEFAULTS)

    def test_packing_deadline_is_inside_the_operation_budget(self):
        with self.assertRaisesRegex(storage.Error, 'timeout'):
            storage.pack(self.files, self.root/'expired.zip', ident(), storage.DEFAULTS, deadline=time.monotonic()-1)

    def test_zip64_publication_and_local_failures_keep_the_contract(self):
        # Lower the ZIP64 boundary to exercise large-file encoding with a tiny fixture.
        with mock.patch.object(zipfile, 'ZIP64_LIMIT', 16):
            reference = self.transport.publish_bundle(ident(), self.files)
            self.transport.read_bundle(reference, ident(), self.root/'zip64')
        self.assertEqual((self.root/'zip64/nested/data').read_bytes(), b'\0bytes\xff')
        with mock.patch.object(storage.os, 'open', side_effect=PermissionError(13, 'denied', '/private-endpoint/file')):
            with self.assertRaisesRegex(storage.Error, '^CI storage: permission$'):
                storage.pack(self.files, self.root/'denied.zip', ident(), storage.DEFAULTS)

    def test_zip_traversal_duplicates_and_links_are_rejected(self):
        for names, mode in [(['../escape'], stat.S_IFREG), (['/escape'], stat.S_IFREG),
                            (['same', 'same'], stat.S_IFREG), (['link'], stat.S_IFLNK)]:
            payload = self.root/'unsafe.zip'
            members = []
            with zipfile.ZipFile(payload, 'w') as archive:
                for name in names:
                    info = zipfile.ZipInfo(name)
                    info.external_attr = mode << 16
                    archive.writestr(info, b'bad')
                    members.append({'name': name, 'size': 3, 'sha256': storage.digest(b'bad')})
            manifest = dict(schema=1, identity=ident(), state='complete', size=payload.stat().st_size,
                            sha256=storage.file_digest(payload), members=members, created_at=1., retention='run')
            with self.subTest(names=names, mode=mode), self.assertRaises(storage.Error):
                storage.verify(storage.dumps(manifest), payload, ident(), storage.DEFAULTS)

    def test_cleanup_is_protected_and_destructive_mode_refuses(self):
        self.transport.publish_bundle(ident(), self.files)
        run = {k: ident()[k] for k in ('repository_id', 'repository', 'run_id', 'run_attempt')}
        report = self.host.cleanup(run, time.time())
        self.assertFalse(report['objects'][0]['delete'])
        with self.assertRaises(storage.Error):
            self.host.cleanup(run, time.time(), dry_run=False)
        self.assertIsNotNone(self.host.describe(ident()))

    def test_existing_destination_is_not_merged(self):
        reference = self.transport.publish_bundle(ident(), self.files)
        destination = self.root/'existing'
        destination.mkdir()
        (destination/'sentinel').write_text('keep')
        with self.assertRaisesRegex(storage.Error, '^CI storage: conflict$'):
            self.transport.read_bundle(reference, ident(), destination)
        self.assertEqual((destination/'sentinel').read_text(), 'keep')

    def test_legacy_opt_in_refuses_before_execution(self):
        (self.root/'storage.toml').write_text((ROOT/'parallel/examples/storage.example.toml').read_text())
        with (mock.patch.dict(os.environ, GITHUB_REPOSITORY='example/project', CI_RUNNERS_ETC=str(self.root)),
              mock.patch.object(cli, 'load_inventory') as load, mock.patch('sys.stderr', new=io.StringIO())):
            for command in ('part', 'flow', 'verdict', 'salvage'):
                self.assertEqual(cli.main([command]), 2)
            load.assert_not_called()


class Transport(unittest.TestCase):
    def setUp(self):
        self.client = storage_ssh.SSHStore(dict(ssh_alias='example.invalid', identity_file='/example/key',
                                              known_hosts_file='/example/hosts', root='/example/store', helper='/example/helper'),
                                           'pilot', dict(storage.DEFAULTS))

    def test_ssh_is_noninteractive_pinned_and_does_not_expose_stderr(self):
        args = self.client.options()
        self.assertIn('StrictHostKeyChecking=yes', args)
        self.assertIn('IdentitiesOnly=yes', args)
        self.assertIn('ForwardAgent=no', args)
        self.assertIn('ClearAllForwardings=yes', args)
        with mock.patch.object(storage_ssh.subprocess, 'Popen', return_value=mock.Mock(returncode=255)):
            with self.assertRaisesRegex(storage.Error, '^CI storage: transport$'):
                self.client.run(['ssh'], b'', time.monotonic()+10)
        def refused(argv, **options):
            options['stderr'].write(b'private-endpoint: Permission denied (publickey,password).')
            return mock.Mock(returncode=255)
        with mock.patch.object(storage_ssh.subprocess, 'Popen', side_effect=refused):
            with self.assertRaisesRegex(storage.Error, '^CI storage: authentication$'):
                self.client.run(['ssh'], b'', time.monotonic()+10)

    def test_timeout_kills_the_transport_session_and_waits_for_it(self):
        process = mock.Mock(pid=123)
        process.communicate.side_effect = storage_ssh.subprocess.TimeoutExpired(['sftp'], 1)
        with (mock.patch.object(storage_ssh.subprocess, 'Popen', return_value=process),
              mock.patch.object(storage_ssh.os, 'killpg') as kill):
            with self.assertRaisesRegex(storage.Error, 'timeout'):
                self.client.run(['sftp'], b'', time.monotonic()+1)
        kill.assert_called_once_with(123, storage_ssh.signal.SIGKILL)
        process.wait.assert_called_once()

    def test_retry_budget_is_shared_and_permanent_errors_do_not_retry(self):
        deadlines = []
        def fail(deadline):
            deadlines.append(deadline)
            raise storage.Error('transport')
        with mock.patch.object(storage_ssh.time, 'sleep'):
            with self.assertRaises(storage.Error):
                self.client.attempt(fail)
        self.assertEqual(len(deadlines), 3)
        self.assertEqual(len(set(deadlines)), 1)
        operation = mock.Mock(side_effect=storage.Error('corrupt'))
        with self.assertRaises(storage.Error):
            self.client.attempt(operation)
        operation.assert_called_once()


class CentralStorage(unittest.TestCase):
    setUp = test_single_workflow.SingleRunScheduling.setUp
    submit = test_single_workflow.SingleRunScheduling.submit
    allocate = test_single_workflow.SingleRunScheduling.allocate
    release = test_single_workflow.SingleRunScheduling.release
    job = test_single_workflow.SingleRunScheduling.job
    def test_binding_survives_configuration_changes_and_restart(self):
        src = dict(repository='example/project', sha='a'*40, run_id=1, run_attempt=1, repository_id=17)
        self.assertEqual(self.store.prepared_storage(src, BOUND), BOUND)
        self.assertEqual(self.store.prepared_storage(src, bundles.GITHUB), BOUND)
        with self.assertRaises(storage.Error):
            self.store.prepared_storage({**src, 'sha': 'b'*40})
        with self.assertRaises(storage.Error):
            bundles.repository_id({**src, 'repository_id': 99, 'admission': {'event': {'repository': {'id': 17}}}})

    def test_coordinator_requires_cleanup_job_and_verified_bundle(self):
        batch, tasks = self.submit()
        src = self.store.batch(batch)['source']
        src.update(storage=BOUND, repository_id=17)
        self.store.db.execute('UPDATE batches SET source=? WHERE id=?', (protocol.dumps(src), batch))
        allocation = self.allocate(batch, tasks[0])
        self.api.jobs.return_value = [self.job(allocation)]
        self.coordinator.storage_etc = '/example/etc'
        with mock.patch.object(bundles, 'verify_result', side_effect=storage.Error('missing')) as verify:
            self.coordinator.reconcile()
            verify.assert_not_called()
            self.assertFalse(self.store.allocation(allocation['id'])['runner_done'])
            self.release(allocation)
            self.coordinator.reconcile()
        self.assertEqual(self.store.batch(batch)['state'], 'error')
        self.api.artifacts.assert_not_called()

    def test_verified_result_can_finish_without_github_artifacts(self):
        batch, tasks = self.submit()
        src = self.store.batch(batch)['source']
        src.update(storage=BOUND, repository_id=17)
        self.store.db.execute('UPDATE batches SET source=? WHERE id=?', (protocol.dumps(src), batch))
        allocation = self.allocate(batch, tasks[0])
        self.release(allocation)
        self.api.jobs.return_value = [self.job(allocation)]
        receipt = {'schema': 1, 'store': 'pilot', 'identity': bundles.result_identity(allocation), 'manifest_sha256': 'a'*64}
        self.coordinator.storage_etc = '/example/etc'
        with mock.patch.object(bundles, 'verify_result', return_value=receipt):
            self.coordinator.reconcile()
        self.assertEqual(self.store.batch(batch)['state'], 'success')
        self.assertEqual(self.store.storage_reference(allocation['id']), receipt)
        self.api.artifacts.assert_not_called()

    def test_failed_test_stays_failed_when_publication_also_fails(self):
        batch, tasks = self.submit()
        src = self.store.batch(batch)['source']
        src.update(storage=BOUND, repository_id=17)
        self.store.db.execute('UPDATE batches SET source=? WHERE id=?', (protocol.dumps(src), batch))
        allocation = self.allocate(batch, tasks[0])
        self.release(allocation, {'state': 'failure', 'reason': 'assertion failed'})
        self.api.jobs.return_value = [self.job(allocation, conclusion='failure')]
        self.coordinator.storage_etc = '/example/etc'
        with mock.patch.object(bundles, 'verify_result', side_effect=storage.Error('missing')):
            self.coordinator.reconcile()
        self.assertEqual(self.store.batch(batch)['state'], 'failure')


class ProductionBridge(unittest.TestCase):
    setUp = BundleFiles.setUp

    def test_plan_publication_fetch_and_submission_share_one_reference(self):
        src = dict(repository='example/project', repository_id=17, sha='a'*40, run_id=1, run_attempt=1,
                   context='project/ci', project='project', concurrency_key='1', storage=BOUND)
        workspace, temporary = self.root/'checkout', self.root/'temporary'
        (workspace/'plans').mkdir(parents=True)
        temporary.mkdir()
        (workspace/'plans/data.json').write_text('{"selected":["test"]}')
        env = dict(RUNNER_TEMP=str(temporary), GITHUB_WORKSPACE=str(workspace))
        workflow_client.state_file(env, 'source').write_bytes(storage.dumps(src))
        doc = {'schema': 2, 'parts': [], 'files': 'plans'}
        workflow_client.state_file(env, 'plan').write_bytes(storage.dumps(doc))
        (self.root/'storage.toml').write_text((ROOT/'parallel/examples/storage.example.toml').read_text())
        with mock.patch.object(storage, 'client', return_value=self.transport):
            workflow_client.publish_plan(str(self.root), env)
            published = json.loads(workflow_client.state_file(env, 'source').read_bytes())
            workflow_client.state_file(env, 'allocation').write_bytes(storage.dumps({'source': published, 'part': {}}))
            with (mock.patch.object(workflow_client.flow, 'check_checkout', return_value=True),
                  mock.patch('subprocess.run', return_value=SimpleNamespace(stdout='a'*40+'\n'))):
                workflow_client.fetch(env, str(self.root))
            self.assertEqual((temporary/'ci-plan-files/data.json').read_text(), '{"selected":["test"]}')
            ledger = scheduler_store.Store(':memory:')
            self.addCleanup(ledger.close)
            ledger.prepared_storage(src, BOUND)
            app = scheduler.Scheduler.__new__(scheduler.Scheduler)
            app.store, app.etc, app.api = ledger, str(self.root), mock.Mock()
            app.bind_environment = mock.Mock()
            project = SimpleNamespace(max_minutes=1440, max_parallel=4, retries=1, gpu_mem_mib=0)
            app.prepare = lambda request: (dict(request), project, None)
            with self.assertRaises(storage.Error):
                app.submit(published, {**doc, 'files': ''})
            bad = {**published, 'files_bundle': {**published['files_bundle'],
                    'identity': {**published['files_bundle']['identity'], 'run_attempt': 2}}}
            with self.assertRaises(storage.Error):
                app.submit(bad, doc)
            self.assertEqual(app.submit(published, doc)['submission_state'], 'accepted')
            app.api.api.assert_not_called()
    def test_result_content_is_checked_against_executor_evidence(self):
        source = dict(repository='example/project', repository_id=17, sha='a'*40, run_id=1, run_attempt=1,
                      context='project/ci', project='project', concurrency_key='1', storage=BOUND)
        allocation = dict(id='allocation-1', task_attempt=1, source=source, part={'id': 'test'}, job_id=10,
                          grant={'task_id': 'task-1'}, result={'state': 'success'})
        directory = self.root/'result'
        directory.mkdir()
        (directory/'outcome.json').write_text('{}')
        (directory/'summary.md').write_text('test')
        doc = dict(allocation=allocation['grant'], result={'state': 'failure'}, source_repository='example/project',
                   source_sha='a'*40, source_run_id=1, source_run_attempt=1, execution_job_id=10, task_attempt=1)
        (directory/'allocation-result.json').write_bytes(storage.dumps(doc))
        self.transport.publish_bundle(bundles.result_identity(allocation), directory)
        with mock.patch.object(storage, 'client', return_value=self.transport):
            with self.assertRaises(storage.Error):
                bundles.verify_result('/example/etc', allocation, 10)

    def test_workflow_routes_each_publication_to_exactly_one_backend(self):
        workflow = yamlsub.load((ROOT/'.github/workflows/ci-v2.yml').read_text())
        work = yamlsub.load((ROOT/'.github/workflows/execute.yml').read_text())
        steps = workflow['jobs']['plan']['steps']
        upload = next(s for s in steps if s.get('uses', '').startswith('actions/upload-artifact@'))
        publish = next(s for s in steps if s.get('with', {}).get('command') == 'publish-plan')
        self.assertIn("storage_backend != 'ssh'", upload['if'])
        self.assertIn("storage_backend == 'ssh'", publish['if'])
        self.assertIn('!inputs.plan-only', publish['if'])
        publish = next(s for s in work['jobs']['execute']['steps'] if s.get('with', {}).get('command') == 'publish-result')
        self.assertIn('always()', publish['if'])
        self.assertIn("storage_backend == 'ssh'", publish['if'])
