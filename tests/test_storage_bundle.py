"""The approved bundle protocol backported without the centralized scheduler."""
from __future__ import annotations
import io
import json
import os
import stat
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock
from ci_runners import storage, storage_host, storage_ssh
ROOT=Path(__file__).resolve().parents[1]
BOUND={'schema':1,'backend':'ssh','store':'ci-files-v1'}


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
        file.chmod(0o600)
        with self.assertRaises(storage.Error):
            storage.read_config(self.root)
        file.write_text((ROOT/'setup/storage.example.toml').read_text())
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
        def unwritable(argv, **options):
            options['stderr'].write(b'private-endpoint: remote open /private/file: Permission denied')
            return mock.Mock(returncode=1)
        with mock.patch.object(storage_ssh.subprocess, 'Popen', side_effect=unwritable):
            with self.assertRaisesRegex(storage.Error, '^CI storage: permission$'):
                self.client.run(['sftp'], b'', time.monotonic()+10)

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
