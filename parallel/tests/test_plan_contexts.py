"""Several source contexts in one workflow keep independent plan records."""
import functools
import json
from types import SimpleNamespace
import unittest
from unittest import mock

from ci_runners import part
from parallel import workflow_client
from parallel.tests import test_organization_execution as execution
from parallel.tests.test_central import plan


class PlanContexts(unittest.TestCase):
    def test_distinct_contexts_execute_and_same_context_cannot_reexecute(self):
        fixture = execution.RepositoryEnvironments()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.environment('alpha')
        origin = fixture.claim('example/staging')['source']
        env = {**fixture.env, 'GITHUB_REPOSITORY': 'example/staging'}
        document = plan()
        document['parts'][0]['minutes'] = 1
        (fixture.workspace/'requested-plan.json').write_text(json.dumps(document))
        engine = functools.partial(part.Executor, scope=False, own_cap=False)
        with mock.patch.object(part, 'Executor', engine), mock.patch.object(part.signal, 'signal'):
            for name in ('first', 'second'):
                context = {**origin, 'context': 'test/ci/' + name}
                workflow_client.state_file(env, 'source').write_text(json.dumps(context))
                args = SimpleNamespace(plan_command='echo ' + name + ' >> executed; cat requested-plan.json')
                self.assertEqual(workflow_client.build(args, fixture.inv, str(fixture.etc), env), 0)
            with self.assertRaisesRegex(part.Refused, 'already ran'):
                workflow_client.build(args, fixture.inv, str(fixture.etc), env)
        self.assertEqual((fixture.workspace/'executed').read_text().splitlines(), ['first', 'second'])


if __name__ == '__main__':
    unittest.main()
