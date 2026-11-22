import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ci_runners import inventory
from parallel import protocol, scheduler, scheduler_store, transport
from parallel.tests.test_central import plan, source
from parallel.tests.test_central_services import inv_text


def profile(device, *, priority=0, gpu_uuids=()):
    return dict(device=device, priority=priority, cpu_count=4, runner_count=1,
                memory_max='4G', gpu_uuids=list(gpu_uuids))


class DeviceRegistration(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.now = 1000.
        self.database = str(self.root / 'schedule.sqlite')
        self.store = scheduler_store.Store(self.database, clock=lambda: self.now)
        self.addCleanup(self.store.close)

    def heartbeat(self, device, *, ready=True, repository='example/project', max_minutes=600, devices=None):
        project = {'repository': repository, 'memory_bytes': 4 << 30, 'max_minutes': max_minutes,
                   'devices': devices if devices is not None else [device]}
        projects = {'project': project} if ready else {}
        self.store.heartbeat(device, [dict(tag=f'{device}-0', online=True, busy=False)], ready_projects=projects)

    def queue(self, run=1):
        document = protocol.load_plan(plan())
        part = document['parts'][0]
        batch = self.store.submit(source(run), document, {part['id']: ['3090a', '3090b']})
        task = self.store.batch(batch)['tasks'][0]
        self.store.request_task_allocation(batch, task['id'], 1)

    def test_late_registration_readiness_and_priority(self):
        allowed = {'3090a', '3090b'}
        self.queue()
        self.assertIsNone(self.store.reserve(allowed_devices=allowed))
        self.store.register_device('3090a', profile('3090a', priority=1))
        self.store.register_device('3090b', profile('3090b', priority=9))
        self.heartbeat('3090a', ready=False)
        self.heartbeat('3090b', repository='example/another')
        self.assertIsNone(self.store.reserve(allowed_devices=allowed))
        self.heartbeat('3090b', max_minutes=30)
        self.assertIsNone(self.store.reserve(allowed_devices=allowed))
        self.heartbeat('3090b', devices=['3090a'])
        self.assertIsNone(self.store.reserve(allowed_devices=allowed))
        self.heartbeat('3090a')
        self.heartbeat('3090b')
        self.assertEqual(self.store.reserve(allowed_devices=allowed)['host'], '3090b')
        self.queue(run=2)
        self.assertEqual(self.store.reserve(allowed_devices=allowed)['host'], '3090a')

    def test_identity_duplicates_and_resource_change_during_lease(self):
        card = 'GPU-00000000-0000-0000-0000-000000000001'
        registration = profile('3090a', gpu_uuids=[card])
        with self.assertRaisesRegex(protocol.Error, 'does not match'):
            self.store.register_device('3090b', registration)
        self.store.register_device('3090a', registration)
        with self.assertRaisesRegex(protocol.Error, 'duplicate'):
            self.store.register_device('3090b', profile('3090b', gpu_uuids=[card]))
        self.heartbeat('3090a')
        self.queue()
        self.assertEqual(self.store.reserve(allowed_devices={'3090a'})['host'], '3090a')
        self.store.register_device('3090a', registration)
        self.store.register_device('3090a', {**registration, 'priority': 10})
        with self.assertRaisesRegex(protocol.Error, 'drain active'):
            self.store.register_device('3090a', profile('3090a'))
        self.assertEqual(self.store.registrations(), [{**registration, 'priority': 10}])

    def test_expiry_and_revocation_keep_the_lease_and_registration(self):
        self.store.register_device('3090a', profile('3090a'))
        self.heartbeat('3090a')
        self.queue()
        allocation = self.store.reserve(allowed_devices={'3090a'})
        self.assertTrue(self.store.devices({'3090a'})['devices']['3090a']['online'])
        self.assertFalse(self.store.devices(set())['devices']['3090a']['online'])
        self.now += scheduler_store.HEARTBEAT_TTL
        self.queue(run=2)
        self.assertIsNone(self.store.reserve(allowed_devices={'3090a'}))
        self.assertFalse(self.store.devices({'3090a'})['devices']['3090a']['online'])
        reopened = scheduler_store.Store(self.database)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.registrations(), [profile('3090a')])
        self.assertFalse(reopened.allocation(allocation['id'])['released'])

    def test_scheduler_restart_restores_devices_and_waits_for_a_new_heartbeat(self):
        inv = inventory.load(inv_text(self.root, count=1, cpus='0-3').replace('worker', '3090a'))
        allowed = {'3090a', '3090b'}
        self.store.register_device('3090a', profile('3090a'))
        self.store.register_device('3090b', profile('3090b', priority=1))
        self.heartbeat('3090a')
        self.heartbeat('3090b')
        self.queue()
        allocation = self.store.reserve(allowed_devices=allowed)
        self.assertEqual(allocation['host'], '3090b')

        reopened = scheduler_store.Store(self.database, clock=lambda: self.now)
        self.addCleanup(reopened.close)
        app = scheduler.Scheduler(inv, str(self.root), reopened, None, device_admission_file='fixture')
        app.publish_devices(allowed)
        devices = json.loads((self.root / 'data/output/devices.json').read_text())['devices']
        self.assertEqual(set(devices), allowed)
        self.assertFalse(any(item['online'] for item in devices.values()))
        self.assertFalse(reopened.allocation(allocation['id'])['released'])
        self.queue(run=2)
        self.assertIsNone(reopened.reserve(allowed_devices=allowed))
        self.heartbeat('3090a')
        self.assertEqual(reopened.reserve(allowed_devices=allowed)['host'], '3090a')

    def test_slurm_heartbeat_requires_registration(self):
        self.store.configure({'slurm': 1}, [{'tag': 'slurm0', 'pool': 'slurm', 'host': '3090a'}])
        listeners = [dict(tag='slurm0', online=True, busy=False)]
        with self.assertRaisesRegex(protocol.Error, 'must register'):
            self.store.heartbeat('3090a', listeners, ready_projects={})
        self.store.register_device('3090a', profile('3090a'))
        self.store.heartbeat('3090a', listeners, ready_projects={})

    def test_tls_credentials_are_bound_to_the_declared_device(self):
        manifest = self.root / 'device-admission.toml'
        manifest.write_text('[devices.3090a]\ntoken_file="a.key"\n[devices.3090b]\ntoken_file="b.key"\n')
        manifest.chmod(0o600)
        for name, value in [('a.key', 'token-a'), ('b.key', 'token-b')]:
            path = self.root / name
            path.write_text(value)
            path.chmod(0o600)
        cfg = dict(device_admission_file=str(manifest), bind='127.0.0.1', port=0,
                   tls_certificate='certificate', tls_key='secret')
        with (mock.patch.object(transport, 'handler', side_effect=lambda app, authenticate: authenticate),
              mock.patch.object(transport.http.server, 'ThreadingHTTPServer') as server,
              mock.patch.object(transport.ssl, 'SSLContext')):
            transport.https_server(cfg, {}, None)
            authenticate = server.call_args.args[1]
        a = SimpleNamespace(headers={'X-CI-Device': '3090a', 'Authorization': 'Bearer token-a'})
        self.assertEqual(authenticate(a), '3090a')
        a.headers['X-CI-Device'] = '3090b'
        with self.assertRaises(protocol.Error):
            authenticate(a)
        (self.root / 'b.key').write_text('token-a')
        with self.assertRaises(protocol.Error):
            authenticate(a)
        self.assertNotIn('token-a', json.dumps(transport.device_tokens(manifest), default=str))
