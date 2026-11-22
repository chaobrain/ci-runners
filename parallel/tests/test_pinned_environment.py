"""The GitHub-trusted recipe is bound before collection and survives dispatch and restart."""
import base64
import functools
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

from ci_runners import part
from parallel import protocol, scheduler_store, task_execution, transport, workflow_client
from parallel.tests import test_organization_execution as execution
from parallel.tests import test_organization_scheduler as scheduling
from parallel.tests.test_central import plan, source


class PinnedEnvironment(unittest.TestCase):
    def test_one_trusted_resolution_across_prepare_submit_and_restart(self):
        fixture = scheduling.OrganizationScheduler()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        app = fixture.app
        app.organization_config.pop('env')
        app.organization_config['env_cache'] = str(fixture.root/'cache')
        path = fixture.root / 'pins.db'
        store = scheduler_store.Store(str(path))
        app.store = store
        self.addCleanup(lambda: app.store.close())
        repo = scheduling.repository('example/project', 11)
        revision = 'c' * 40
        calls = []

        def github_get(method, path):
            calls.append(path)
            if path.endswith('/commits?per_page=1'):
                return [{'sha': revision}]
            if '/contents/' in path:
                self.assertTrue(path.endswith('?ref=' + revision))
                data = b'schema: 1\npython: "3.12"\n'
                return {'type': 'file', 'encoding': 'base64', 'content': base64.b64encode(data).decode()}
            if path == '/repos/example/project':
                return repo
            self.assertIn('/attempts/1', path)
            return {'run_attempt': 1, 'repository': repo, 'head_repository': repo, 'event': 'push',
                    'head_sha': 'a'*40, 'head_branch': 'topic'}

        app.api.api = github_get
        supplied = dict(source(), environment_recipe={'revision': 'spoof'})
        with self.assertRaisesRegex(protocol.Error, 'prepared test plan'):
            app.submit(supplied, plan())
        receipt = app.call('prepare', {'source': supplied}, 'local')
        store.close()
        app.store = scheduler_store.Store(str(path))
        revision = 'd'*40
        again = app.call('prepare', {'source': supplied}, 'local')
        self.assertEqual(again, receipt)
        with self.assertRaisesRegex(protocol.Error, 'digest|pinned environment'):
            app.call('plan_environment', {'source': supplied, 'digest': 'invalid'}, 'local')
        recipe = app.call('plan_environment', {'source': supplied, 'digest': receipt['environment_digest']}, 'local')
        with self.assertRaisesRegex(protocol.Error, 'does not match its prepared'):
            app.submit({**supplied, 'environment_digest': 'wrong'}, plan())
        result = app.submit(dict(supplied, **receipt), plan())
        self.assertEqual(app.store.batch(result['batch_id'])['source']['environment_recipe'], recipe)
        self.assertEqual(recipe['revision'], 'c'*40)
        self.assertEqual(sum('/contents/' in p for p in calls), 1)
        app.organization_config.pop('env_cache')
        app.organization_config['env'] = '/old'
        with self.assertRaisesRegex(protocol.Error, 'changed'):
            app.submit(supplied, plan())

    def test_plan_and_worker_prepare_inside_their_scopes(self):
        fx = execution.RepositoryEnvironments()
        fx.setUp()
        self.addCleanup(fx.doCleanups)
        conda = fx.root / 'conda'
        conda.write_text(f'#!{sys.executable}\nimport sys, venv\n'
                         'venv.EnvBuilder(symlinks=True).create(sys.argv[sys.argv.index("--prefix") + 1])\n')
        conda.chmod(0o700)
        cfg = fx.settings['organization']
        cfg.pop('env_root')
        cfg.update(env_cache=str(fx.root/'cache'), conda=str(conda))
        claim = fx.claim('example/staging')
        recipe = dict(format=1, spec={'schema': 1, 'python': '3.12', 'variables': {'CHECKED_ENV': 'ok'}},
                      revision='a'*40, repository='example/staging', files={}, commits={})
        claim['source']['environment_recipe'] = recipe
        origin = {k: v for k, v in claim['source'].items() if k != 'environment_recipe'}
        origin['environment_digest'] = protocol.fingerprint(recipe)
        env = dict(fx.env, GITHUB_REPOSITORY=recipe['repository'])
        workflow_client.state_file(env, 'source').write_text(json.dumps(origin))
        doc = plan()
        doc['parts'][0]['minutes'] = 1
        (fx.workspace/'expected.json').write_text(json.dumps(doc))
        cmd = claim['part']['command'] + ' && test "$CHECKED_ENV" = ok && test -x "$CI_PYTEST_PLAN" && cat expected.json'
        engine = functools.partial(part.Executor, scope=False, own_cap=False)
        with (mock.patch.object(part, 'Executor', engine), mock.patch.object(part.signal, 'signal'),
              mock.patch.object(transport, 'Client') as rpc):
            rpc.return_value.call.return_value = recipe
            self.assertEqual(workflow_client.build(SimpleNamespace(plan_command=cmd), fx.inv, str(fx.etc), env), 0)
        prefix = (fx.workspace/'prefixes').read_text().splitlines()
        self.assertEqual(prefix[0], prefix[1])
        self.assertTrue(Path(prefix[0]).is_relative_to(cfg['env_cache']))
        # The worker uses the already published prefix, even with the builder removed.
        conda.unlink()
        (fx.workspace/'prefixes').unlink()

        def enter_scope(argv, **kwargs):
            record, _, _, _, _, _, _, wrapper = json.loads(argv[-1])
            result = subprocess.run(wrapper, **kwargs)
            part._write_json(str(Path(record)/'exit.json'), {'rc': result.returncode})
            return mock.Mock()

        with mock.patch.object(part.signal, 'signal'):
            _, outcome = task_execution.execute(fx.inv, str(fx.etc), env, claim, [],
                                                run=mock.Mock(return_value=SimpleNamespace(stdout='')), spawn=enter_scope)
        self.assertEqual(outcome['outcome'], 'pass')
        self.assertEqual((fx.workspace/'prefixes').read_text().splitlines(), prefix)
        cfg['env_cache'] = str(fx.root/'empty-other-device')
        broken = fx.claim('example/staging', repo_id=2)
        broken['source']['environment_recipe'] = recipe
        broken['part']['id'] = 'cold'
        with mock.patch.object(part.signal, 'signal'):
            _, outcome = task_execution.execute(fx.inv, str(fx.etc), env, broken, [],
                                                run=mock.Mock(return_value=SimpleNamespace(stdout='')), spawn=enter_scope)
        self.assertEqual(outcome['outcome'], 'unavailable')
        self.assertIn('environment preparation failed', outcome['reason'])
