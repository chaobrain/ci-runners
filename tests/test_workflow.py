"""The reusable ``h100-test.yml`` (design §4.7): every job carries the same-repository
guard; it runs only on h100's labels; permissions stay minimal; a part with no command
is skipped; every action it calls exists and gets only its declared inputs."""
from __future__ import annotations

import pathlib
import re
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
WF = ROOT / '.github' / 'workflows' / 'h100-test.yml'
GUARD = ("(github.event_name == 'pull_request' && github.event.pull_request.head.repo.full_name == github.repository)"
         " || github.event_name == 'push' || github.event_name == 'workflow_dispatch'")
RUNS_ON = {'claim': '[self-hosted, h100-light]', 'verdict': '[self-hosted, h100-light]',
           'cpu': '[self-hosted, h100-cpu]', 'cpu-retry': '[self-hosted, h100-cpu]', 'gpu': '[self-hosted, h100]'}


def text() -> str:
    return WF.read_text()


def jobs() -> dict[str, str]:
    """Each job's block, by name (the file keeps one job per two-space key under ``jobs:``)."""
    body = text().split('\njobs:\n', 1)[1]
    names = list(re.finditer(r'^  ([a-z][a-z0-9-]*):\n', body, re.M))
    return {m.group(1): body[m.end():(names[i + 1].start() if i + 1 < len(names) else len(body))]
            for i, m in enumerate(names)}


def key(block: str, name: str) -> str:
    m = re.search(rf'^    {name}: (.*)$', block, re.M)
    return m.group(1) if m else ''


def declared(action: str) -> set[str]:
    t = (ROOT / 'actions' / action / 'action.yml').read_text()
    m = re.search(r'^inputs:\n((?:  .*\n|\n)*)', t, re.M)
    return set(re.findall(r'^  ([a-z][a-z0-9-]*):', m.group(1), re.M))


class Workflow(unittest.TestCase):
    def test_only_called(self):
        t = text()
        self.assertRegex(t, r'\non:\n  workflow_call:\n')
        self.assertEqual(re.findall(r'^  ([a-z_]+):', t.split('\non:\n', 1)[1].split('\npermissions:', 1)[0], re.M),
                         ['workflow_call'])
        self.assertNotIn('pull_request_target', t)
        self.assertNotIn('secrets', t)

    def test_every_job_carries_the_guard(self):
        js = jobs()
        self.assertEqual(sorted(js), ['claim', 'cpu', 'cpu-retry', 'gpu', 'gpu-retry', 'verdict'])
        for name, block in js.items():
            self.assertIn(GUARD, key(block, 'if'), name)

    def test_the_jobs_run_on_h100_only(self):
        js = jobs()
        for name, want in RUNS_ON.items():
            self.assertEqual(key(js[name], 'runs-on'), want, name)
        retry = key(js['gpu-retry'], 'runs-on')
        self.assertIn('''contains(needs.gpu.outputs.retry-on, '"h100-gpu')''', retry)  # only a card of h100
        self.assertIn('''fromJSON('["self-hosted","h100"]')''', retry)
        self.assertNotIn('ubuntu', text())

    def test_permissions_stay_minimal(self):
        t = text()
        self.assertIn('\npermissions:\n  contents: read\njobs:', t)
        js = jobs()
        for name, block in js.items():
            writes = re.findall(r'^      ([a-z-]+): write$', block, re.M)
            self.assertEqual(writes, ['statuses'] if name in ('claim', 'verdict') else [], name)
        self.assertEqual(t.count(': write'), 2)

    def test_a_part_without_command_is_skipped(self):
        js = jobs()
        self.assertIn("&& inputs.cpu-command != ''", key(js['cpu'], 'if'))
        self.assertIn("&& inputs.gpu-command != ''", key(js['gpu'], 'if'))
        for name in ('cpu', 'gpu', 'verdict'):
            self.assertIn("needs.claim.outputs.run == 'true'", key(js[name], 'if'), name)
        for name in ('cpu-retry', 'gpu-retry', 'verdict'):
            self.assertIn('!cancelled()', key(js[name], 'if'), name)
        for name in ('cpu-retry', 'gpu-retry'):
            part = name.split('-')[0]
            self.assertIn(f"(needs.{part}.outputs.next == 'retry' || needs.{part}.outputs.next == 'fallback')",
                          key(js[name], 'if'), name)

    def test_checkouts_keep_no_credentials(self):
        js = jobs()
        for name, block in js.items():
            n = block.count('uses: actions/checkout@v4')
            self.assertEqual(block.count('persist-credentials: false'), n, name)
            self.assertEqual(block.count('ref: ${{ github.event.pull_request.head.sha || github.sha }}'), n, name)

    def test_the_actions_it_calls(self):
        t = text()
        for m in re.finditer(r'uses: chaobrain/ci-runners/actions/([a-z]+)@(\S+)\n((?:        .*\n)*)', t):
            action, ref, body = m.groups()
            self.assertEqual(ref, 'v1', action)
            given = set(re.findall(r'^          ([a-z][a-z0-9-]*):', body, re.M))
            self.assertLessEqual(given, declared(action), action)
        used = set(re.findall(r'chaobrain/ci-runners/actions/([a-z]+)@', t))
        self.assertEqual(used, {'claim', 'part', 'verdict'})
        for block in (b for b in jobs().values() if 'actions/part@' in b):
            for required in ('project', 'id', 'command', 'timeout-minutes'):
                self.assertRegex(block, rf'\n          {required}: ', required)

    def test_every_input_is_declared_and_used(self):
        t = text()
        decl = set(re.findall(r'^      ([a-z][a-z0-9-]*):\n        description:', t.split('\npermissions:', 1)[0], re.M))
        used = set(re.findall(r'inputs\.([a-z][a-z0-9-]*)', t))
        self.assertEqual(decl, {'project', 'cpu-command', 'gpu-command', 'gpus', 'timeout-minutes', 'results', 'context'})
        self.assertEqual(used, decl)


class Prep(unittest.TestCase):
    """The claim job's first step, run as the runner would run it."""

    def run_prep(self, **env):
        script = re.search(r'      - id: prep\n.*?        run: \|\n((?:          [^\n]*\n)+)', text(), re.S).group(1)
        script = '\n'.join(line[10:] for line in script.splitlines())
        with tempfile.NamedTemporaryFile('r') as out:
            base = {'IN_TIMEOUT': '60', 'IN_PROJECT': 'myproj', 'IN_CONTEXT': '', 'HAS_CPU': 'true', 'HAS_GPU': 'true',
                    'GITHUB_OUTPUT': out.name, 'PATH': '/usr/bin:/bin'}
            r = subprocess.run(['bash', '-e', '-c', script], env={**base, **env}, capture_output=True, text=True,
                               timeout=30)
            return r.returncode, dict(line.split('=', 1) for line in out.read().splitlines())

    def test_outputs(self):
        self.assertEqual(self.run_prep(), (0, {'parts': 'cpu gpu', 'context': 'myproj/ci', 'job-timeout': '75'}))
        self.assertEqual(self.run_prep(HAS_CPU='false', IN_CONTEXT='x/y')[1]['parts'], 'gpu')
        self.assertEqual(self.run_prep(HAS_GPU='false')[1]['parts'], 'cpu')
        self.assertEqual(self.run_prep(IN_CONTEXT='x/gpu')[1]['context'], 'x/gpu')

    def test_refusals(self):
        for env in ({'HAS_CPU': 'false', 'HAS_GPU': 'false'}, {'IN_TIMEOUT': '241'}, {'IN_TIMEOUT': '0'},
                    {'IN_TIMEOUT': '1; echo x'}, {'IN_PROJECT': '../p'}, {'IN_PROJECT': 'p\nq'}):
            code, out = self.run_prep(**env)
            self.assertEqual((code, out), (2, {}), env)


class FlowExpressions(unittest.TestCase):
    """A flow mapping holds an expression only quoted: unquoted, YAML reads the ``{`` of
    ``${{`` as a nested mapping and GitHub rejects the whole file before any job starts."""

    @staticmethod
    def bad(lines: list[str]) -> list[str]:
        out = []
        for line in lines:
            m = re.match(r'\s*(?:- )?[A-Za-z0-9_.-]+: (\{.*)$', line)
            if m and '${{' in re.sub(r'"[^"]*"|\'[^\']*\'', '', m.group(1)):
                out.append(line)
        return out

    def test_the_check_sees_an_unquoted_expression(self):
        self.assertEqual(self.bad(['concurrency: {group: a-${{ github.sha }}, x: 1}']), ['concurrency: {group: a-${{ github.sha }}, x: 1}'])
        self.assertEqual(self.bad(['    outputs: {run: "${{ steps.c.outputs.run }}"}', 'group: a-${{ github.sha }}']), [])

    def test_no_workflow_or_example_has_one(self):
        files = [*(ROOT / '.github' / 'workflows').glob('*.yml'), *(ROOT / 'actions').glob('*/action.yml')]
        for f in files:
            self.assertEqual(self.bad(f.read_text().splitlines()), [], f.name)
        for f in (ROOT / 'docs').glob('*.md'):
            blocks = re.findall(r'^```yaml\n(.*?)^```', f.read_text(), re.M | re.S)
            self.assertTrue(blocks or f.name != 'consumers.md')
            for b in blocks:
                self.assertEqual(self.bad(b.splitlines()), [], f.name)


if __name__ == '__main__':
    unittest.main()
