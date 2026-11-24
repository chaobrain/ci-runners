"""Synthetic pilot boundary and real OpenSSH signature tests; no host deployment."""
from __future__ import annotations

import concurrent.futures
import contextlib
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

from ci_runners import storage_protocol as p

if os.name == 'posix':
    from ci_runners import storage_auth as auth, storage_gateway, storage_issuer, storage_probe
    from ci_runners.storage_store import Store

ROOT = Path(__file__).resolve().parents[1]
IDENTITY = {'repository_id': 910001, 'repository': 'pilot-a/probe', 'run_id': 920001,
            'run_attempt': 1, 'part_id': 'probe', 'part_attempt': 1, 'sha': '0' * 40}


@unittest.skipUnless(os.name == 'posix' and Path('/usr/bin/ssh-keygen').is_file(), 'Linux/OpenSSH pilot')
class Pilot(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys = tempfile.TemporaryDirectory(prefix='pilot-keys-')
        cls.key = Path(cls.keys.name) / 'issuer'
        subprocess.run(['/usr/bin/ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(cls.key)], check=True)
        public = Path(str(cls.key) + '.pub').read_text().split()
        cls.trust = (p.SIGNER + ' namespaces="' + p.NAMESPACE + '" ' + ' '.join(public[:2]) + '\n').encode()
        import base64
        cls.client = 'SHA256:' + base64.b64encode(hashlib.sha256(base64.b64decode(public[1])).digest()).decode().rstrip('=')

    @classmethod
    def tearDownClass(cls):
        cls.keys.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pilot-data-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'store'
        self.root.mkdir(mode=0o700)
        for name in ('staging', 'objects', 'ledger'):
            (self.root / name).mkdir(mode=0o700)
        self.store = Store(self.root, free_floor=0)
        self.payload = b'synthetic probe data\x00\xff'
        self.manifest = p.make_manifest(dict(IDENTITY), self.payload)

    def grant(self, operation='publish', *, identity=None, manifest=None, payload=None, **changes):
        identity = dict(IDENTITY) if identity is None else identity
        listing = operation == 'list'
        if listing:
            identity = {k: identity[k] for k in p.RUN_FIELDS}
        raw_payload = self.payload if payload is None else payload
        raw_manifest = self.manifest if manifest is None else manifest
        now = int(time.time())
        doc = {'schema': 1, 'audience': p.AUDIENCE, 'key_id': p.KEY_ID, 'jti': uuid.uuid4().hex,
               'nbf': now - 1, 'exp': now + 599, 'operation': operation, 'client': self.client,
               'identity': identity, 'manifest_sha256': '' if listing else p.digest(raw_manifest),
               'payload_sha256': '' if listing else p.digest(raw_payload), 'size': 0 if listing else len(raw_payload), **changes}
        return p.grant(p.canonical(doc), client=self.client)

    def header(self, grant):
        raw = p.canonical(grant)
        return {'schema': 1, 'operation': grant['operation'], 'grant': p.b64(raw),
                'signature': p.b64(auth.sign(raw, self.key, client=self.client))}

    def handle(self, grant, manifest=b'', payload=b'', store=None):
        store = store or self.store
        deadline = p.Deadline(total=5)
        store.admit(grant, deadline=deadline)
        return store.handle(grant, manifest, payload, deadline=deadline)

    def refuses(self, code, function, *args, **kwargs):
        with self.assertRaises(p.Error) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def local_client(self, *, total=5):
        trust = self.base / 'trust'
        trust.write_bytes(self.trust)
        # Test-owned trust and zero free-space floor exist only in this test bootstrap.
        # Production CLI loads root-owned config and fixed 50 GiB policy instead.
        script = ('import os,sys; from pathlib import Path; '
                  'from ci_runners.storage_store import Store; '
                  'from ci_runners.storage_protocol import Deadline; '
                  'from ci_runners.storage_gateway import serve; '
                  'os.set_blocking(0,False); os.set_blocking(1,False); '
                  'raise SystemExit(serve(0,1,store=Store(sys.argv[1],free_floor=0),'
                  'client=sys.argv[2],allowed_signers=Path(sys.argv[3]).read_bytes(),'
                  'deadline=Deadline(total=float(sys.argv[4]),idle=1)))')
        client = storage_probe.Client.__new__(storage_probe.Client)
        client.client = self.client
        client.command = [sys.executable, '-B', '-c', script, str(self.root), self.client, str(trust), str(total)]
        return client

    def test_real_signatures_reject_tamper_wrong_namespace_signer_and_client(self):
        header = self.header(self.grant())
        self.assertEqual(auth.authorize(header, client=self.client, allowed_signers=self.trust,
                                        deadline=p.Deadline())['identity'], IDENTITY)
        bad = dict(header, signature=p.b64(b'not a signature'))
        self.refuses('signature', auth.authorize, bad, client=self.client, allowed_signers=self.trust, deadline=p.Deadline())
        raw = p.unb64(header['grant'])
        with patch.object(auth.tempfile, 'tempdir', str(self.base / 'does-not-exist')):
            auth.verify(raw, p.unb64(header['signature']), self.trust, deadline=p.Deadline())
        wrong = subprocess.run(['/usr/bin/ssh-keygen', '-Y', 'sign', '-n', 'wrong-namespace', '-f', str(self.key)],
                               input=raw, capture_output=True, check=True).stdout
        self.refuses('signature', auth.verify, raw, wrong, self.trust, deadline=p.Deadline())
        self.refuses('signature', auth.verify, raw + b' ', p.unb64(header['signature']), self.trust, deadline=p.Deadline())
        self.refuses('signature', auth.verify, raw, p.unb64(header['signature']),
                     self.trust.replace(p.SIGNER.encode(), b'other-signer'), deadline=p.Deadline())
        self.refuses('authorization', auth.authorize, header, client='SHA256:other', allowed_signers=self.trust, deadline=p.Deadline())

    def test_synthetic_only_scope_expiry_types_and_operation_are_strict(self):
        good = self.grant()
        mutations = [({'schema': True}, 'format'), ({'audience': 'production'}, 'authorization'),
                     ({'key_id': 'other'}, 'authorization'), ({'jti': '../file'}, 'authorization'),
                     ({'nbf': True}, 'authorization'), ({'exp': good['nbf'] + 601}, 'authorization'),
                     ({'nbf': good['nbf'] - 1000, 'exp': good['nbf'] - 500}, 'expired'),
                     ({'nbf': int(time.time()) + 31, 'exp': int(time.time()) + 500}, 'expired'),
                     ({'size': True}, 'format'), ({'operation': 'delete'}, 'authorization'),
                     ({'identity': {**IDENTITY, 'repository': 'real/project'}}, 'identity'),
                     ({'identity': {**IDENTITY, 'run_id': True}}, 'identity'),
                     ({'identity': {**IDENTITY, 'run_attempt': 3}}, 'identity'),
                     ({'identity': {**IDENTITY, 'part_attempt': 3}}, 'identity'),
                     ({'identity': {**IDENTITY, 'sha': 'a' * 40}}, 'identity')]
        for changes, code in mutations:
            with self.subTest(changes=changes):
                self.refuses(code, p.grant, p.canonical({**good, **changes}), client=self.client)
        raw = p.canonical(good)
        self.refuses('format', p.grant, raw + b' ', client=self.client)
        self.refuses('format', p.parse, b'{"schema":1,"schema":1}')
        self.refuses('format', p.parse, b'{"x":NaN}')
        header = self.header(good)
        self.refuses('authorization', auth.authorize, {**header, 'operation': 'read'},
                     client=self.client, allowed_signers=self.trust, deadline=p.Deadline())

    def test_publish_read_and_scoped_list_preserve_exact_bytes(self):
        result, _, _ = self.handle(self.grant(), self.manifest, self.payload)
        self.assertEqual(result['manifest_sha256'], p.digest(self.manifest))
        _, manifest, payload = self.handle(self.grant('read'))
        self.assertEqual((manifest, payload), (self.manifest, self.payload))
        other = {**IDENTITY, 'repository_id': 910002, 'repository': 'pilot-b/probe'}
        other_manifest = p.make_manifest(other, self.payload)
        self.handle(self.grant(identity=other, manifest=other_manifest), other_manifest, self.payload)
        found, _, _ = self.handle(self.grant('list'))
        self.assertEqual(len(found['objects']), 1)
        self.assertEqual(found['objects'][0]['identity'], IDENTITY)
        other = {**IDENTITY, 'repository_id': 910002, 'repository': 'pilot-b/probe'}
        self.assertEqual(self.handle(self.grant('list', identity={**other, 'run_id': 920002}))[0], {'objects': []})
        self.refuses('missing', self.handle, self.grant('read', identity={**other, 'run_id': 920002}))

    def test_replay_and_changed_scope_are_durable_across_store_instances(self):
        grant = self.grant()
        self.handle(grant, self.manifest, self.payload)
        self.refuses('replay', self.handle, {**grant, 'operation': 'read'})
        for _ in range(2):
            self.handle(grant, self.manifest, self.payload, store=Store(self.root, free_floor=0))
        self.refuses('replay', self.handle, grant, self.manifest, self.payload)
        self.refuses('replay', self.handle, {**grant, 'operation': 'read'})
        self.assertEqual(len(list(self.store.objects.iterdir())), 1)

    def test_conflict_corruption_and_missing_completion_never_use_old_objects(self):
        self.handle(self.grant(), self.manifest, self.payload)
        changed = self.payload + b'new'
        manifest = p.make_manifest(dict(IDENTITY), changed)
        self.refuses('conflict', self.handle, self.grant(manifest=manifest, payload=changed), manifest, changed)
        self.refuses('missing', self.handle, self.grant('read', identity={**IDENTITY, 'run_attempt': 2}))
        path = next(self.store.objects.iterdir())
        (path / 'payload').write_bytes(b'x' * len(self.payload))
        self.refuses('digest', self.handle, self.grant('read'))
        (path / 'manifest.json').unlink()
        self.refuses('missing', self.handle, self.grant('read'))

    def test_atomic_publication_failure_and_lost_response_retry(self):
        grant = self.grant()
        with patch.object(Store, '_persist', side_effect=OSError('injected interruption')):
            with self.assertRaises(OSError):
                self.handle(grant, self.manifest, self.payload)
        self.assertEqual(list(self.store.objects.iterdir()), [])
        self.assertEqual(list(self.store.staging.iterdir()), [])
        original = Store._sync_dir
        def fail_after_rename(path):
            if path == self.store.objects:
                raise OSError('response lost after commit')
            original(path)
        with patch.object(Store, '_sync_dir', side_effect=fail_after_rename):
            with self.assertRaises(OSError):
                self.handle(grant, self.manifest, self.payload)
        result, _, _ = self.handle(grant, self.manifest, self.payload)
        self.assertEqual(result['manifest_sha256'], p.digest(self.manifest))
        self.assertEqual(len(list(self.store.objects.iterdir())), 1)

    def test_capacity_counts_staging_and_disk_floor_fails_closed(self):
        tiny = Store(self.root, capacity=1024, free_floor=0)
        self.refuses('quota', self.handle, self.grant(), self.manifest, self.payload, store=tiny)
        full_disk = Store(self.root, free_floor=2**62)
        self.refuses('space', self.handle, self.grant(), self.manifest, self.payload, store=full_disk)
        abandoned = self.store.staging / 'abandoned'
        abandoned.write_bytes(b'x' * 65536)
        abandoned.chmod(0o600)
        reserved = Store(self.root, capacity=65536, free_floor=0)
        self.refuses('quota', self.handle, self.grant(), self.manifest, self.payload, store=reserved)
        self.assertEqual(list(self.store.objects.iterdir()), [])

    def test_concurrent_publish_is_immutable_and_slots_are_cross_process(self):
        grants = [self.grant() for _ in range(4)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda grant: self.handle(grant, self.manifest, self.payload), grants))
        self.assertTrue(all(result[0]['manifest_sha256'] == p.digest(self.manifest) for result in results))
        self.assertEqual(len(list(self.store.objects.iterdir())), 1)
        script = ('import sys; from ci_runners.storage_store import Store; '
                  's=Store(sys.argv[1],free_floor=0); '\
                  's.slot().__enter__()')
        with contextlib.ExitStack() as stack:
            for _ in range(4):
                stack.enter_context(self.store.slot())
            result = subprocess.run([sys.executable, '-B', '-c', script, str(self.root)], cwd=ROOT,
                                    capture_output=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b'busy', result.stderr)
        with self.store.slot():
            pass

    def test_wire_subprocess_large_upload_download_and_bad_signature(self):
        client = self.local_client(total=20)
        payload = (b'probe-stream-' * ((64 << 20) // 13 + 1))[:64 << 20]
        manifest = p.make_manifest(dict(IDENTITY), payload)
        client.call(self.header(self.grant(manifest=manifest, payload=payload)), manifest, payload)
        _, got_manifest, got_payload = client.call(self.header(self.grant('read', manifest=manifest, payload=payload)))
        self.assertEqual((got_manifest, got_payload), (manifest, payload))
        listed, _, _ = client.call(self.header(self.grant('list')))
        self.assertEqual(len(listed['objects']), 1)
        bad = {**self.header(self.grant()), 'signature': p.b64(b'invalid')}
        self.refuses('signature', client.call, bad, self.manifest, self.payload)

    def test_manifest_scope_bytes_and_expiry_after_wait_are_rechecked(self):
        grant = self.grant()
        self.refuses('digest', p.validate_manifest, self.manifest + b' ', self.payload, grant)
        wrong = p.make_manifest({**IDENTITY, 'part_attempt': 2}, self.payload)
        self.refuses('identity', p.validate_manifest, wrong, self.payload,
                     {**grant, 'manifest_sha256': p.digest(wrong)})
        self.refuses('size', p.validate_manifest, self.manifest, self.payload + b'x', grant)
        self.refuses('digest', p.validate_manifest, self.manifest,
                     b'x' * len(self.payload), grant)
        self.handle(grant, self.manifest, self.payload)
        read = self.grant('read')
        self.store.admit(read, deadline=p.Deadline())
        with patch.object(p.time, 'time', return_value=read['exp']):
            self.refuses('expired', self.store.handle, read, deadline=p.Deadline())

    def test_authenticated_truncated_uploads_consume_replay_budget(self):
        client = self.local_client()
        grant = self.grant()
        header = p.canonical(self.header(grant))
        frame = p.PREFIX.pack(p.MAGIC, len(header), len(self.manifest), len(self.payload)) + header + self.manifest
        for _ in range(3):
            got = subprocess.run(client.command, input=frame, capture_output=True, timeout=5)
            self.assertEqual(got.returncode, 2)
            self.assertIn(b'truncated', got.stdout)
        self.refuses('replay', client.call, self.header(grant), self.manifest, self.payload)
        self.assertEqual(list(self.store.objects.iterdir()), [])

    def test_grant_ledger_is_bounded_without_automatic_deletion(self):
        import sqlite3
        self.handle(self.grant(), self.manifest, self.payload)
        db = sqlite3.connect(self.store.ledger / 'grants.sqlite')
        try:
            db.executemany('INSERT INTO grants VALUES(?,?,?)',
                           [(f'{n:032x}', '0' * 64, 1) for n in range(4095)])
            db.commit()
        finally:
            db.close()
        self.refuses('quota', self.handle, self.grant('read'))

    def test_entrypoint_refuses_shell_and_client_pins_ssh_policy(self):
        import io
        with patch.dict(os.environ, SSH_ORIGINAL_COMMAND='bash'), \
             patch.object(auth, 'config', side_effect=AssertionError('configuration must not load')), \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(storage_gateway.main(['--config', '/unused']), 2)
        cfg = {'host': 'example.invalid', 'user': 'probe', 'key': '/private/key',
               'known_hosts': '/private/known_hosts', 'client': self.client}
        with patch.object(auth, 'protected'), patch.object(auth, 'read_file'):
            client = storage_probe.Client(cfg)
        command = client.command
        self.assertIn('StrictHostKeyChecking=yes', command)
        self.assertIn('ForwardAgent=no', command)
        self.assertIn('UserKnownHostsFile=/private/known_hosts', command)
        self.assertEqual(command[command.index('-F') + 1], '/dev/null')
        self.refuses('configuration', storage_probe.Client, {**cfg, 'host': '-oBad'})

    def test_single_key_policy_rejects_relaxations_and_duplicate_keys(self):
        public = self.key.with_suffix('.pub').read_text().split()
        prefix = 'restrict,from="example.invalid",command="/fixed --config /policy" '
        line = (prefix + ' '.join(public[:2]) + '\n').encode()
        with patch.object(auth, 'read_file', return_value=line):
            self.assertEqual(auth.client_fingerprint('/policy'), self.client)
        for wrong in (line + line, line.replace(b'restrict,', b'restrict,pty,'),
                      line.replace(b'from="example.invalid"', b'pty'), b'not a public key'):
            with self.subTest(wrong=wrong[:32]), patch.object(auth, 'read_file', return_value=wrong):
                self.refuses('configuration', auth.client_fingerprint, '/policy')

    def test_wire_bounds_truncation_trailing_and_idle_deadline(self):
        def head(raw):
            r, w = os.pipe()
            os.write(w, raw)
            os.close(w)
            try:
                return p.read_head(r, p.Deadline(total=1))
            finally:
                os.close(r)
        self.refuses('framing', head, p.PREFIX.pack(p.MAGIC, p.HEADER_MAX + 1, 0, 0))
        self.refuses('framing', head, p.PREFIX.pack(p.MAGIC, 1, 0, p.PAYLOAD_MAX + 1))
        self.refuses('truncated', head, b'CISP')
        r, w = os.pipe()
        try:
            os.write(w, b'xx')
            os.close(w)
            w = None
            self.refuses('trailing', p.read_body, r, p.Deadline(total=1), 1, 0)
        finally:
            os.close(r)
            if w is not None:
                os.close(w)
        r, w = os.pipe()
        try:
            started = time.monotonic()
            self.refuses('timeout', p.Deadline(total=.05, idle=.02).read, r, 1)
            self.assertLess(time.monotonic() - started, 1)
        finally:
            os.close(r)
            os.close(w)

    def test_client_retries_only_transport_and_validates_server_results(self):
        client = self.local_client()
        header = self.header(self.grant())
        with patch.object(client, '_exchange', side_effect=p.Error('transport')) as exchange:
            self.refuses('transport', client.call, header, self.manifest, self.payload)
            self.assertEqual(exchange.call_count, 3)
        with patch.object(client, '_exchange', side_effect=p.Error('authorization')) as exchange:
            self.refuses('authorization', client.call, header, self.manifest, self.payload)
            self.assertEqual(exchange.call_count, 1)
        wrong = ({'identity': {**IDENTITY, 'run_attempt': 2}, 'manifest_sha256': p.digest(self.manifest)}, b'', b'')
        with patch.object(client, '_exchange', return_value=wrong):
            self.refuses('identity', client.call, header, self.manifest, self.payload)

    def test_protected_paths_reject_writable_files_links_and_parent_replacement(self):
        path = self.base / 'private'
        path.write_bytes(b'key')
        path.chmod(0o600)
        self.assertEqual(auth.read_file(path, owner=os.getuid(), private=True), b'key')
        path.chmod(0o644)
        self.refuses('permissions', auth.read_file, path, owner=os.getuid(), private=True)
        path.chmod(0o600)
        linked = self.base / 'link'
        linked.symlink_to(path)
        self.refuses('permissions', auth.read_file, linked, owner=os.getuid(), private=True)
        parent = self.base / 'writable'
        parent.mkdir(mode=0o777)
        parent.chmod(0o777)
        child = parent / 'secret'
        child.write_bytes(b'key')
        child.chmod(0o600)
        self.refuses('permissions', auth.read_file, child, owner=os.getuid(), private=True)

    def test_issuer_audit_is_exclusive_and_rejects_production_scope(self):
        audit = self.base / 'audit'
        audit.mkdir(mode=0o700)
        cfg = {'client': self.client, 'key': str(self.key), 'audit_root': str(audit)}
        raw = p.canonical(self.grant())
        issued = storage_issuer.issue(raw, cfg)
        self.assertEqual(p.unb64(issued['grant']), raw)
        self.assertEqual(len(list(audit.iterdir())), 1)
        with self.assertRaises(FileExistsError):
            storage_issuer.issue(raw, cfg)
        wrong = p.parse(raw)
        wrong['identity']['repository'] = 'real/project'
        self.refuses('identity', storage_issuer.issue, p.canonical(wrong), cfg)

    def test_local_results_are_fresh_and_failed_writes_leave_no_completion(self):
        target = self.base / 'result'
        with patch.object(storage_probe.os, 'fsync', side_effect=OSError('disk error')):
            with self.assertRaises(OSError):
                storage_probe.save_result(target, {'accepted': True}, self.manifest, self.payload)
        self.assertFalse(target.exists())
        storage_probe.save_result(target, {'accepted': True}, self.manifest, self.payload)
        self.assertEqual((target / 'payload').read_bytes(), self.payload)
        with self.assertRaises(FileExistsError):
            storage_probe.save_result(target, {'accepted': False}, b'', b'wrong')
        self.assertEqual(p.parse((target / 'result.json').read_bytes()), {'accepted': True})


if __name__ == '__main__':
    unittest.main()
