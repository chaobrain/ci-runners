"""Schema 1 storage uses real bundles with the network replaced by a memory fixture."""
from __future__ import annotations

import json
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import zipfile

from ci_runners import storage, storage_v1


RUN = dict(repository_id=17, repository='example/project', id=100, run_attempt=1, head_sha='a' * 40)


class MemoryStore:
    def __init__(self):
        self.limits, self.objects = dict(storage.DEFAULTS), {}

    def publish_bundle(self, identity, source, retention='diagnostic'):
        with tempfile.TemporaryDirectory() as directory:
            payload = Path(directory) / 'bundle.zip'
            manifest = storage.pack(source, payload, identity, self.limits, retention)
            key = storage.dumps(identity)
            if key in self.objects:
                old = self.objects[key]
                if old[0]['sha256'] != manifest['sha256']:
                    raise storage.Error('conflict')
            else:
                ref = dict(schema=1, store='pilot', identity=identity,
                           manifest_sha256=storage.digest(storage.dumps(manifest)))
                self.objects[key] = (manifest, payload.read_bytes(), ref)
            return self.objects[key][2]

    def read_bundle(self, reference, identity, destination):
        storage.reference(reference, identity, 'pilot')
        manifest, raw, _ = self.objects[storage.dumps(identity)]
        with tempfile.TemporaryDirectory() as directory:
            payload = Path(directory) / 'bundle.zip'
            payload.write_bytes(raw)
            return storage.unpack(storage.dumps(manifest), payload, identity, destination,
                                  self.limits, reference['manifest_sha256'])

    def list_completed(self, run, after=''):
        return {'references': [ref for _, _, ref in self.objects.values()
                               if all(ref['identity'][k] == v for k, v in run.items())], 'next': ''}


class Bundles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.transport = MemoryStore()
        self.bundles = storage_v1.Bundles(self.transport, 'pilot', RUN, 100)
        self.source = self.root / 'source'
        self.source.mkdir()
        (self.source / 'pytest.xml').write_text('<testsuite tests="1"/>')

    def test_roundtrip_includes_hidden_files_when_requested(self):
        (self.source / '.coverage').write_bytes(b'coverage')
        self.bundles.publish('report', str(self.source), hidden=True)
        target = self.root / 'download'
        self.bundles.download(target, name='report')
        self.assertEqual((target / '.coverage').read_bytes(), b'coverage')
        self.assertTrue((target / 'pytest.xml').is_file())
        self.assertFalse((target / '.ci-storage-source.json').exists())

    def test_conflict_and_corruption_do_not_replace_existing_files(self):
        self.bundles.publish('report', str(self.source))
        (self.source / 'pytest.xml').write_text('different')
        with self.assertRaisesRegex(storage.Error, 'conflict'):
            self.bundles.publish('report', str(self.source))
        target = self.root / 'download'
        target.mkdir()
        (target / 'pytest.xml').write_text('keep')
        with self.assertRaisesRegex(storage.Error, 'conflict'):
            self.bundles.download(target, name='report')
        self.assertEqual((target / 'pytest.xml').read_text(), 'keep')

    def test_new_runs_never_fall_back_when_binding_or_bundle_is_missing(self):
        with self.assertRaises(storage.Error):
            self.bundles.download(self.root / 'missing', name='report')
        self.bundles.publish('report', str(self.source))
        del self.transport.objects[storage.dumps(self.bundles.identity('run-backend'))]
        with self.assertRaises(storage.Error):
            self.bundles.references()
        self.assertIsNone(self.bundles.references({**RUN, 'id': 99}))

    def test_rerun_uses_new_outcomes_and_retains_skipped_job_reports(self):
        self.bundles.publish('report', str(self.source))
        self.bundles.publish('unchanged', str(self.source))
        next_attempt = storage_v1.Bundles(self.transport, 'pilot', {**RUN, 'run_attempt': 2}, 100)
        (self.source / 'pytest.xml').write_text('new')
        next_attempt.publish('report', str(self.source))
        refs = next_attempt.references()
        self.assertEqual(refs['report']['identity']['run_attempt'], 2)
        self.assertEqual(refs['unchanged']['identity']['run_attempt'], 1)

    def test_pattern_collision_and_corruption_leave_no_partial_download(self):
        self.bundles.publish('rows-one', str(self.source))
        (self.source / 'pytest.xml').write_text('different')
        ref = self.bundles.publish('rows-two', str(self.source))
        with self.assertRaisesRegex(storage.Error, 'conflict'):
            self.bundles.download(self.root / 'merged', pattern='rows-*', merge=True)
        self.assertFalse((self.root / 'merged').exists())
        key = storage.dumps(ref['identity'])
        manifest, raw, reference = self.transport.objects[key]
        self.transport.objects[key] = (manifest, raw + b'corrupt', reference)
        with self.assertRaises(storage.Error):
            self.bundles.download(self.root / 'corrupt', name='rows-two')
        self.assertFalse((self.root / 'corrupt').exists())

    def test_optional_absence_shadows_earlier_reports_but_skipped_jobs_keep_theirs(self):
        self.bundles.publish('rows-cpu', str(self.source))
        self.bundles.publish('rows-skipped', str(self.source))
        rerun = storage_v1.Bundles(self.transport, 'pilot', {**RUN, 'run_attempt': 2}, 100)
        rerun.publish('rows-cpu', str(self.root / 'absent'), missing='ignore')
        refs = rerun.references()
        self.assertNotIn('rows-cpu', refs)
        self.assertIn('rows-skipped', refs)
        self.assertFalse(any(n.startswith('ci-absent-') for n in refs))

    def test_workflow_sha_and_backend_binding_cannot_be_substituted(self):
        self.bundles.publish('report', str(self.source))
        wrong = storage_v1.Bundles(self.transport, 'pilot', {**RUN, 'head_sha': 'b' * 40}, 100)
        with self.assertRaises(storage.Error):
            wrong.references()
        wrong = storage_v1.Bundles(self.transport, 'other-store', RUN, 100)
        with self.assertRaises(storage.Error):
            wrong.references()

    def test_enumeration_obeys_the_shared_operation_deadline(self):
        with mock.patch.object(storage_v1.time, 'monotonic', side_effect=[1, 300]):
            with self.assertRaisesRegex(storage.Error, 'timeout'):
                self.bundles.publish('report', str(self.source))

    def test_history_adapter_preserves_pre_cutover_reads_and_verifies_new_bytes(self):
        self.bundles.publish('report', str(self.source))
        api = mock.Mock(repo='example/project')
        api.pages.return_value = [{'id': 7, 'name': 'legacy'}]
        adapter = storage_v1.ArtifactApi(api, self.bundles)
        self.assertEqual(adapter.listing({**RUN, 'id': 99}), api.pages.return_value)
        api.json.assert_not_called()
        listed = adapter.json('GET', '/repos/example/project/actions/runs/100/artifacts')['artifacts']
        aid = next(a['id'] for a in listed if a['name'] == 'report')
        data = adapter.download(f'/repos/example/project/actions/artifacts/{aid}/zip')
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            self.assertIn('pytest.xml', archive.namelist())
        api.download.assert_not_called()
        self.transport.objects.clear()
        with self.assertRaises(storage_v1.ReadError):
            adapter.json('GET', '/repos/example/project/actions/runs/100/artifacts')

    def test_completed_history_without_any_publication_is_an_empty_listing(self):
        adapter = storage_v1.ArtifactApi(mock.Mock(repo='example/project'), self.bundles)
        self.assertEqual(adapter.listing({**RUN, 'status': 'completed'}), [])
        with self.assertRaises(storage_v1.ReadError):
            adapter.listing(RUN)

    def test_empty_optional_upload_and_required_download_have_distinct_results(self):
        self.assertIsNone(self.bundles.publish('optional', str(self.root / 'absent*'), missing='warn'))
        with self.assertRaises(storage.Error):
            self.bundles.publish('required', str(self.root / 'absent*'))
        with self.assertRaises(storage.Error):
            self.bundles.download(self.root / 'download', pattern='report-*')

    def test_seal_is_required_for_a_successful_outcome(self):
        (self.source / 'outcome.json').write_text(json.dumps({'outcome': 'pass', 'run-attempt': 1}))
        self.bundles.publish('ci-part-cpu-1', str(self.source))
        found = {'cpu': {1: ({'outcome': 'pass', 'run-attempt': 1}, '')}}
        self.assertTrue(self.bundles.report_gap(found))
        self.bundles.seal('cpu', 1, 'success', [])
        self.assertEqual(self.bundles.report_gap(found), '')

    def test_failed_or_incomplete_report_publication_cannot_be_green(self):
        self.bundles.publish('ci-part-cpu-1', str(self.source))
        found = {'cpu': {1: ({'outcome': 'pass', 'run-attempt': 1}, '')}}
        with self.assertRaises(storage.Error):
            self.bundles.seal('cpu', 1, 'success', ['missing-report'])
        with self.assertRaises(storage.Error):
            self.bundles.seal('cpu', 2, 'failure', [])
        self.assertTrue(self.bundles.report_gap(found))

    @unittest.skipIf(os.name == 'nt', 'symlink privilege is not required on the maintenance workstation')
    def test_links_in_producers_and_destination_parents_are_rejected(self):
        (self.source / 'link').symlink_to(self.source / 'pytest.xml')
        with self.assertRaises(storage.Error):
            self.bundles.publish('report', str(self.source))
        (self.source / 'link').unlink()
        self.bundles.publish('report', str(self.source))
        (self.root / 'target').symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(storage.Error):
            self.bundles.download(self.root / 'target', name='report')


class Configuration(unittest.TestCase):
    def test_absent_configuration_keeps_github_without_needing_an_event(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(storage_v1.open_current(directory, {}))

    def test_a_required_internal_store_never_silently_selects_github(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(storage.Error):
                storage_v1.open_current(directory, {'CI_STORAGE_STORE': 'pilot'})

    def test_cutover_requires_a_positive_integer_and_a_matching_ssh_override(self):
        example = Path(__file__).resolve().parents[1] / 'setup/storage.example.toml'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'storage.toml'
            for value in ('true', '0', '-1', '"100"'):
                path.write_text(example.read_text().replace('"example/project" = 100', '"example/project" = ' + value))
                path.chmod(0o600)
                with self.assertRaises(storage.Error):
                    storage.read_config(directory)

    def test_current_source_rejects_public_fork_and_wrong_repository_id(self):
        with tempfile.TemporaryDirectory() as directory:
            event = Path(directory) / 'event.json'
            env = {'GITHUB_REPOSITORY': 'example/project', 'GITHUB_REPOSITORY_ID': '17',
                   'GITHUB_RUN_ID': '100', 'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_SHA': 'a' * 40,
                   'GITHUB_EVENT_NAME': 'pull_request', 'GITHUB_EVENT_PATH': str(event)}
            valid = {'repository': {'id': 17, 'private': True, 'full_name': 'example/project'}}
            event.write_text(json.dumps(valid))
            self.assertEqual(storage_v1.current_run(env), RUN)
            for invalid in ({'id': 17, 'private': False, 'full_name': 'example/project'},
                            {'id': 18, 'private': True, 'full_name': 'example/project'}):
                event.write_text(json.dumps({'repository': invalid}))
                with self.assertRaises(storage.Error):
                    storage_v1.current_run(env)
            event.write_text(json.dumps({**valid, 'pull_request': {
                'head': {'repo': {'full_name': 'other/project'}, 'sha': 'b' * 40}}}))
            with self.assertRaises(storage.Error):
                storage_v1.current_run(env)


if __name__ == '__main__':
    unittest.main()
