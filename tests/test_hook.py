"""The job-started hook end to end, on files (design §6.3): it reads the policy and the
event payload, prints one fixed line, logs details on the host only, and refuses on
any error."""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import tempfile
import unittest

from ci_runners import hook

ROOT = pathlib.Path(__file__).resolve().parents[1]
EVENTS = ROOT / 'tests' / 'fixtures' / 'events'
POLICY = '''schema = 1
actors = ["alice"]
[repositories."chaobrain/brainspike"]
id = 1001
visibility = "private"
'''


class Started(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.tmp.name)
        self.etc = self.base / 'etc'
        self.etc.mkdir()
        (self.etc / 'policy.toml').write_text(POLICY)
        self.env = {'CI_RUNNERS_ETC': str(self.etc), 'CI_RUNNER_CLASS': 'part', 'RUNNER_NAME': 'r-part-1',
                    'GITHUB_REPOSITORY': 'chaobrain/brainspike', 'GITHUB_REPOSITORY_ID': '1001',
                    'GITHUB_EVENT_NAME': 'pull_request', 'GITHUB_ACTOR': 'alice', 'GITHUB_TRIGGERING_ACTOR': 'alice',
                    'GITHUB_EVENT_PATH': str(EVENTS / 'pr_same_repo.json')}

    def tearDown(self):
        self.tmp.cleanup()

    def log(self):
        return [json.loads(x) for x in (self.base / 'logs' / 'hook.log').read_text().splitlines()]

    def test_accept_and_refuse(self):
        code, line = hook.started(self.env)
        self.assertEqual(code, 0)
        self.assertRegex(line, r'\Aci-runners: accepted ok policy=[0-9a-f]{12}\Z')
        code, line = hook.started({**self.env, 'GITHUB_EVENT_PATH': str(EVENTS / 'pr_fork.json')})
        self.assertEqual((code, line.split()[1:3]), (1, ['refused', 'fork']))
        self.assertEqual([r['reason'] for r in self.log()], ['ok', 'fork'])
        self.assertEqual(self.log()[0]['runner'], 'r-part-1')

    def test_errors_refuse(self):
        cases = {
            'no etc': ({**self.env, 'CI_RUNNERS_ETC': ''}, 'error'),
            'no policy': ({**self.env, 'CI_RUNNERS_ETC': str(self.base / 'nowhere')}, 'policy'),
            'no event file': ({**self.env, 'GITHUB_EVENT_PATH': str(self.base / 'missing.json')}, 'payload'),
        }
        bad = self.base / 'bad.json'
        bad.write_text('{"repository": ')
        cases['bad json'] = ({**self.env, 'GITHUB_EVENT_PATH': str(bad)}, 'payload')
        deep = self.base / 'deep.json'
        deep.write_text('[' * 100000 + ']' * 100000)
        cases['deep json'] = ({**self.env, 'GITHUB_EVENT_PATH': str(deep)}, 'payload')
        for what, (env, reason) in cases.items():
            code, line = hook.started(env)
            self.assertEqual((code, line.split()[2]), (1, reason), what)
        (self.etc / 'policy.toml').write_text('schema = 2\n')
        self.assertEqual(hook.started(self.env)[1].split()[1:3], ['refused', 'policy'])

    def test_an_oversized_event_is_refused(self):
        saved = hook.MAX_EVENT
        hook.MAX_EVENT = 100  # a valid payload, but over the bound
        try:
            code, line = hook.started(self.env)
        finally:
            hook.MAX_EVENT = saved
        self.assertEqual((code, line.split()[2]), (1, 'payload'))
        self.assertEqual(self.log()[-1]['error'], 'event: ValueError')  # the bound, not a parse error

    def test_the_wrapper_prints_one_line_and_exits_with_the_decision(self):
        rel = self.base / 'releases'
        rel.mkdir()
        os.symlink(ROOT, rel / 'hook')
        hooks = self.base / 'hooks'
        hooks.mkdir()
        for n in ('job-started.sh', 'job-completed.sh'):
            (hooks / n).write_bytes((ROOT / 'hooks' / n).read_bytes())
        r = subprocess.run(['bash', '-e', str(hooks / 'job-started.sh')], env={**self.env, 'PATH': '/usr/bin:/bin'},
                           capture_output=True, text=True, timeout=60)
        self.assertEqual((r.returncode, r.stdout.count('\n')), (0, 1), r.stderr)
        self.assertTrue(r.stdout.startswith('ci-runners: accepted ok'))
        os.unlink(rel / 'hook')
        r = subprocess.run(['bash', '-e', str(hooks / 'job-started.sh')], env=self.env, capture_output=True,
                           text=True, timeout=60)
        self.assertEqual((r.returncode, r.stdout), (1, 'ci-runners: refused error policy=none\n'))
        r = subprocess.run(['bash', '-e', str(hooks / 'job-completed.sh')], env=self.env, capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(r.returncode, 0)


if __name__ == '__main__':
    unittest.main()
