"""The job-started hook end to end, on files (design §6.3): it reads the policy and the
event payload, prints one fixed line, logs details on the host only, and refuses on
any error."""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

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
        return [json.loads(x) for p in sorted((self.base / 'logs').glob('hook-*.log')) for x in p.read_text().splitlines()]

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
        (self.etc / 'policy.toml').write_text('schema = 3\n')
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

    def _main(self, env, bound=1):
        """``hook.main(['job-started'])`` in a child, its bound lowered; the child itself
        is bounded, so a missing alarm fails the test instead of hanging it."""
        code = (f'import sys; sys.path.insert(0, {str(ROOT)!r}); from ci_runners import hook; '
                f'hook.START_BOUND = {bound}; sys.exit(hook.main(["job-started"]))')
        return subprocess.run([sys.executable, '-I', '-c', code], env={**env, 'PATH': '/usr/bin:/bin'},
                              capture_output=True, text=True, timeout=20)

    def test_a_hung_read_is_refused_at_the_bound(self):
        fifo = self.base / 'event.fifo'
        os.mkfifo(fifo)  # nobody writes: opening it blocks until the alarm
        r = self._main({**self.env, 'GITHUB_EVENT_PATH': str(fifo)})
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertRegex(r.stdout, r'\Aci-runners: refused timeout policy=[0-9a-f]{12}\n\Z')
        self.assertEqual(self.log()[-1]['reason'], 'timeout')

    def test_a_planted_log_fifo_neither_blocks_nor_breaks_the_line(self):
        (self.base / 'logs').mkdir()
        day = datetime.datetime.now(datetime.timezone.utc)
        logs = [self.base / 'logs' / f'hook-{d:%Y-%m-%d}.log' for d in (day, day + datetime.timedelta(days=1))]
        for log in logs:  # today's, and tomorrow's in case the date turns
            os.mkfifo(log)
        r = self._main(self.env, bound=5)
        self.assertEqual((r.returncode, r.stdout.split()[:3]), (0, ['ci-runners:', 'accepted', 'ok']), r.stderr)
        self.assertEqual(r.stderr, '')
        victim = self.base / 'victim'
        victim.write_text('kept\n')
        for log in logs:
            os.unlink(log)
            os.symlink(victim, log)  # a link is not followed either
        r = self._main(self.env, bound=5)
        self.assertEqual((r.returncode, r.stderr, victim.read_text()), (0, '', 'kept\n'))

    def test_the_bound_outside_the_decision_still_prints_the_fixed_line(self):
        saved = hook._log
        def slow(*_a):
            raise hook._Timeout()  # as if the alarm fired while the log line was written
        hook._log = slow
        try:
            with unittest.mock.patch('builtins.print') as out:
                self.assertEqual(hook.main(['job-started']), 1)
        finally:
            hook._log = saved
        out.assert_called_once_with('ci-runners: refused timeout policy=none', flush=True)

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

    def test_the_wrappers_run_the_inventorys_python(self):
        """CI_RUNNERS_PYTHON (h100: the system's Python is 3.10) when it is an absolute
        path to an executable file; the system's otherwise."""
        rel = self.base / 'releases'
        rel.mkdir()
        os.symlink(ROOT, rel / 'hook')
        hooks = self.base / 'hooks'
        hooks.mkdir()
        for n in ('job-started.sh', 'job-completed.sh'):
            (hooks / n).write_bytes((ROOT / 'hooks' / n).read_bytes())
        mark = self.base / 'ran'
        fake = self.base / 'py' / 'python3'
        fake.parent.mkdir()
        fake.write_text(f'#!/bin/sh\necho "$@" >> {mark}\nexec {sys.executable} "$@"\n')
        fake.chmod(0o755)
        plain = self.base / 'py' / 'notexec'
        plain.write_text('#!/bin/sh\nexit 9\n')
        for n in ('job-started.sh', 'job-completed.sh'):
            for py, used in ((str(fake), True), ('py/python3', False), (str(plain), False), ('', False)):
                if mark.exists():
                    mark.unlink()
                r = subprocess.run(['sh', str(hooks / n)], env={**self.env, 'PATH': '/usr/bin:/bin',
                                                                  'CI_RUNNERS_PYTHON': py},
                                   capture_output=True, text=True, timeout=60, cwd=self.base)
                self.assertEqual(r.returncode, 0, (n, py, r.stdout, r.stderr))
                self.assertEqual(mark.exists(), used, (n, py))
                if used:
                    self.assertTrue(mark.read_text().startswith('-I -c '), mark.read_text())


if __name__ == '__main__':
    unittest.main()
