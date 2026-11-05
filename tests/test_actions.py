"""The actions (design §4, §9): each passes exactly its declared inputs to the matching
``ci-run`` command, and the launcher runs the release the action names, on our
runners, or the action's own checkout on a GitHub-hosted runner."""
from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
ACTIONS = ('claim', 'place', 'part', 'salvage')


def declared(text: str, section: str) -> list[str]:
    m = re.search(rf'^{section}:\n((?:  .*\n|\n)*)', text, re.M)
    return re.findall(r'^  ([a-z][a-z0-9-]*):', m.group(1), re.M) if m else []


def flags(module: str, cmd: str) -> set[str]:
    """The long options of ``ci-run <cmd>``, read from its argparse calls."""
    src = (ROOT / 'ci_runners' / f'{module}.py').read_text()
    return set(re.findall(r"add_argument\('(--[a-z-]+)'", src))


class Actions(unittest.TestCase):
    def test_every_input_is_used_and_every_use_declared(self):
        for name in ACTIONS:
            text = (ROOT / 'actions' / name / 'action.yml').read_text()
            ins = set(declared(text, 'inputs'))
            used = set(re.findall(r'inputs\.([a-z][a-z0-9-]*)', text))
            self.assertEqual(used, ins, name)

    def test_inputs_reach_their_flags(self):
        modules = {'claim': 'github', 'place': 'probe', 'part': 'part', 'salvage': 'part'}
        skip = {'token'}  # claim's token travels as GITHUB_TOKEN
        for name in ACTIONS:
            text = (ROOT / 'actions' / name / 'action.yml').read_text()
            passed = set(re.findall(r'(--[a-z-]+) "\$IN_', text)) | set(re.findall(r'\((--[a-z-]+) "\$IN_', text))
            want = {f'--{i}' for i in declared(text, 'inputs') if i not in skip}
            self.assertEqual(passed, want, name)
            self.assertLessEqual(passed, flags(modules[name], name), name)

    def test_no_flow_mappings(self):
        """A flow mapping (``{description: a, b, value: ...}``) reads a comma in the text as
        a new key, and the runner refuses the whole action; block style only."""
        for name in ACTIONS:
            for n, line in enumerate((ROOT / 'actions' / name / 'action.yml').read_text().splitlines(), 1):
                bare = re.sub(r'\$\{\{.*?\}\}|\'[^\']*\'|"[^"]*"', '', line)
                self.assertNotRegex(bare, r'[{}]', f'{name}/action.yml:{n}')

    def test_the_launcher_and_the_release_stamp(self):
        self.assertRegex((ROOT / 'actions' / 'RELEASE').read_text().strip(), r'\A(hook|v\d+\.\d+\.\d+)\Z')
        for name in ACTIONS:
            self.assertIn('"$GITHUB_ACTION_PATH/../lib/ci-run.sh" ' + name,
                          (ROOT / 'actions' / name / 'action.yml').read_text().replace('\n        ', ' '))


class Launcher(unittest.TestCase):
    """``actions/lib/ci-run.sh``, run from a copy of ``actions/`` with its own stamp."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.tmp.name) / 'home'
        self.home.mkdir()
        self.out = self.home / 'out'
        self.out.write_text('')
        self.repo = pathlib.Path(self.tmp.name) / 'repo'
        shutil.copytree(ROOT / 'actions', self.repo / 'actions')
        (self.repo / 'bin').symlink_to(ROOT / 'bin')  # the checkout's own package, for hosted claim

    def tearDown(self):
        self.tmp.cleanup()

    def stamp(self, release):
        (self.repo / 'actions' / 'RELEASE').write_text(release + '\n')

    def stub(self, release):
        b = self.home / 'data/ci-runners/releases' / release / 'bin'
        b.mkdir(parents=True)
        (b / 'ci-run').write_text(f'#!/bin/sh\necho "{release} $*"\n')
        (b / 'ci-run').chmod(0o755)

    def launch(self, *args, **env):
        return subprocess.run(['bash', str(self.repo / 'actions/lib/ci-run.sh'), *args], capture_output=True,
                              text=True, timeout=30, env={'HOME': str(self.home), 'PATH': '/usr/bin:/bin',
                                                          'GITHUB_OUTPUT': str(self.out), **env})

    def test_the_stamped_release_runs(self):
        self.stamp('v1.2.3')
        for release in ('hook', 'v1.2.3', 'v0.0.1'):
            self.stub(release)
        r = self.launch('part', '--id', 'x y')
        self.assertEqual((r.returncode, r.stdout), (0, 'v1.2.3 part --id x y\n'))

    def test_a_bad_stamp_is_refused(self):
        self.stub('hook')
        for bad in ('../hook', 'main', 'v1'):
            self.stamp(bad)
            r = self.launch('part', '--id', 'x')
            self.assertEqual(r.returncode, 2, bad)
            self.assertIn('bad RELEASE', r.stdout)

    def test_a_missing_release_makes_a_part_unavailable(self):
        self.stamp('v1.2.3')
        self.stub('hook')
        r = self.launch('part', '--id', 'x')
        self.assertEqual(r.returncode, 0)
        out = self.out.read_text()
        self.assertIn('outcome=unavailable\n', out)
        self.assertIn('next=fallback\n', out)
        self.assertNotIn(str(self.home), out + r.stdout)  # no home path reaches the job
        self.assertEqual(self.launch('place', '--parts', '[]').returncode, 2)  # only a part has a fallback

    def test_a_hosted_runner_runs_only_claim_from_the_checkout(self):
        r = self.launch('part', '--id', 'x', RUNNER_ENVIRONMENT='github-hosted')
        self.assertEqual(r.returncode, 2)
        self.assertIn('runs on our runners only', r.stdout)
        r = self.launch('claim', '--context', 'p/ci', '--sha', 'a' * 40, RUNNER_ENVIRONMENT='github-hosted')
        self.assertEqual(r.returncode, 2)  # the checkout's claim ran with --hosted: no policy read, the token missing
        self.assertIn('GITHUB_TOKEN', r.stderr)


if __name__ == '__main__':
    unittest.main()
