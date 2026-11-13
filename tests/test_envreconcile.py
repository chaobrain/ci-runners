"""Host-side environment reconciliation using the fixture contents API and builder."""
import json
import os
import time

from ci_runners import envbuild, registry
from tests.test_envbuild import Api, Base, HEAD, OTHER


class Reconcile(Base):
    def setUp(self):
        super().setUp()
        (self.etc / 'projects' / 'p.toml').unlink()
        (self.etc / 'envreconcile.toml').write_text('memory_max = "2G"\n')
        (self.etc / 'policy.toml').write_text(
            'schema = 2\n[org]\nname = "org"\nprivate_repositories = true\n')
        self.snapshot()
        self.spec = 'schema = 1\npython = "3.12"\n[variables]\nEXAMPLE = "first"\n'
        self.entry_path = self.etc / 'projects' / 'repo.toml'
        self.link = self.envs / 'repo'

    def snapshot(self, age=0, rid=15):
        (self.etc / 'org.json').write_text(json.dumps({
            'schema': 1, 'org': 'org', 'fetched_at': time.time() - age,
            'members': [], 'repositories': {'org/repo': rid},
        }))

    def reconcile(self, *args, head=HEAD, spec=None, rid=15):
        api = Api({head: {envbuild.SPEC: self.spec if spec is None else spec}})

        def request(method, url, token):
            if url.endswith('/commits/trunk'):
                return {'sha': head}
            data = api(method, url, token)
            if url.endswith('/repos/org/repo'):
                data.update(id=rid, full_name='Org/Repo', private=True)
            return data

        self.api = request
        return self.main(*args, verb='reconcile', project=None)[0]

    def test_enrollment_and_spec_changes_are_idempotent(self):
        self.assertEqual(self.reconcile('--dry-run'), 0, self.err)
        self.assertFalse(self.entry_path.exists())
        self.assertFalse(self.link.exists())
        self.assertEqual(self.reconcile(), 0, self.err)
        project = registry.load_file(str(self.etc), 'repo', self.inv.this)
        self.assertEqual(project.repository_id, 15)
        original = os.readlink(self.link)
        self.assertEqual(self.reconcile(head=OTHER), 0, self.err)
        self.assertEqual(os.readlink(self.link), original)
        changed = self.spec.replace('first', 'second')
        self.assertEqual(self.reconcile(head=OTHER, spec=changed), 0, self.err)
        self.assertNotEqual(os.readlink(self.link), original)
        sha, applied, _, _ = envbuild.kept(str(self.link), 'org/repo')
        self.assertEqual((sha, applied.variables), (OTHER, {'EXAMPLE': 'second'}))
        self.assertTrue(envbuild.applies_variables(str(self.link)))

    def test_failed_build_keeps_active_environment_and_retries(self):
        self.assertEqual(self.reconcile(), 0, self.err)
        original = os.readlink(self.link)
        changed = self.spec.replace('3.12', '3.13')
        self.host.fail = lambda cmd, _: 'create' in cmd
        self.assertEqual(self.reconcile(head=OTHER, spec=changed), 2)
        self.assertEqual(os.readlink(self.link), original)
        self.host.fail = None
        self.assertEqual(self.reconcile(head=OTHER, spec=changed), 0, self.err)
        self.assertNotEqual(os.readlink(self.link), original)

    def test_stale_snapshot_and_replaced_repository_are_refused(self):
        self.snapshot(age=7200)
        self.assertEqual(self.reconcile(), 2)
        self.assertFalse(self.entry_path.exists())
        self.snapshot()
        self.assertEqual(self.reconcile(rid=16), 2)
        self.assertFalse(self.entry_path.exists())
        self.assertEqual(self.reconcile(), 0, self.err)
        original = self.entry_path.read_text()
        self.snapshot(rid=16)
        self.assertEqual(self.reconcile(rid=16), 2)
        self.assertEqual(self.entry_path.read_text(), original)
