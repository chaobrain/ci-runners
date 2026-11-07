"""The actions (design §4, §9): each passes exactly its declared inputs to the matching
``ci-run`` command, and the launcher runs the release the action names, on our
runners only."""
from __future__ import annotations

import pathlib
import json
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
ACTIONS = ('claim', 'part', 'salvage', 'verdict')


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
        modules = {'claim': 'github', 'part': 'part', 'salvage': 'verdict', 'verdict': 'verdict'}
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

    def test_the_action_refs_follow_the_release_stamp(self):
        """Design §9: a canary pinning a workflow at @vX.Y.Z runs that release's actions only
        if the workflow names them so. On main (stamp ``hook``) a workflow's refs read @v1; a
        stamped release names itself. This repository's own workflows call no action since
        ``h100-test.yml`` went (decision 12.17), so the docs' examples are checked too: they
        always read @v1, the tag consumers pin."""
        stamp = (ROOT / 'actions' / 'RELEASE').read_text().strip()
        want = 'v1' if stamp == 'hook' else stamp
        ref = re.compile(r'chaobrain/ci-runners/actions/([a-z]+)@(\S+)')
        refs = [(f.name, m.group(1), m.group(2), want) for f in sorted((ROOT / '.github' / 'workflows').glob('*.yml'))
                for m in ref.finditer(f.read_text())]
        refs += [(f.name, m.group(1), m.group(2), 'v1') for f in sorted((ROOT / 'docs').glob('*.md'))
                 for m in ref.finditer(f.read_text())]
        self.assertTrue(refs)
        for name, action, got, expect in refs:
            self.assertEqual(got, expect, f'{name}: actions/{action}@{got} with RELEASE {stamp}')

    def test_the_launcher_and_the_release_stamp(self):
        self.assertRegex((ROOT / 'actions' / 'RELEASE').read_text().strip(), r'\A(hook|v\d+\.\d+\.\d+)\Z')
        for name in ACTIONS:
            self.assertIn('"$GITHUB_ACTION_PATH/../lib/ci-run.sh" ' + name,
                          (ROOT / 'actions' / name / 'action.yml').read_text().replace('\n        ', ' '))


class Salvage(unittest.TestCase):
    def test_one_upload_per_salvaged_part_up_to_the_bound(self):
        """Design §4.5: salvage uploads each part as ``ci-part-<id>-<attempt>``. A composite
        action has no loop, so it has one upload step per part the command may name, and
        the command refuses more (``MAX_SALVAGED``)."""
        from ci_runners import verdict
        text = (ROOT / 'actions' / 'salvage' / 'action.yml').read_text()
        steps = re.findall(r"- if: fromJSON\(steps\.salvage\.outputs\.count \|\| '0'\) >= ([0-9]+)\n"
                           r"      uses: actions/upload-artifact@v4\n      with:\n"
                           r"        name: \$\{\{ steps\.salvage\.outputs\.name-([0-9]+) \}\}\n"
                           r"        path: \$\{\{ runner\.temp \}\}/ci-salvage-out/\$\{\{ steps\.salvage\.outputs\.name-([0-9]+) \}\}\n",
                           text)
        self.assertEqual(steps, [(str(i), str(i), str(i)) for i in range(1, verdict.MAX_SALVAGED + 1)])
        self.assertIn('pattern: ci-part-*', text)


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

    def test_the_release_is_found_beside_ci_runners_etc(self):
        """h100's data root is not ~/data: the runner's CI_RUNNERS_ETC names it."""
        self.stamp('hook')
        self.stub('hook')
        b = pathlib.Path(self.tmp.name) / 'sub' / 'ci-runners' / 'releases' / 'hook' / 'bin'
        b.mkdir(parents=True)
        (b / 'ci-run').write_text('#!/bin/sh\necho "sub $*"\n')
        (b / 'ci-run').chmod(0o755)
        etc = pathlib.Path(self.tmp.name) / 'sub' / 'ci-runners' / 'etc'
        self.assertEqual(self.launch('part', '--id', 'x', CI_RUNNERS_ETC=str(etc)).stdout, 'sub part --id x\n')
        self.assertEqual(self.launch('part', '--id', 'x', CI_RUNNERS_ETC='sub/ci-runners/etc').stdout,
                         'hook part --id x\n')  # not absolute: the default
        self.assertEqual(self.launch('part', '--id', 'x').stdout, 'hook part --id x\n')

    def test_a_bad_stamp_is_refused(self):
        self.stub('hook')
        self.stub('v1.2.3')  # so that v1.2.3/../.. resolves
        planted = self.home / 'data/ci-runners/tmp/e.1.1/bin'  # a glob would pass v1.2.3/../../tmp/e.1.1
        planted.mkdir(parents=True)
        (planted / 'ci-run').write_text('#!/bin/sh\necho planted\n')
        (planted / 'ci-run').chmod(0o755)
        for bad in ('../hook', 'main', 'v1', 'v1.2.3/../../tmp/e.1.1', 'v1.2.3x'):
            self.stamp(bad)
            r = self.launch('part', '--id', 'x')
            self.assertEqual(r.returncode, 2, bad)
            self.assertIn('bad RELEASE', r.stdout)
            self.assertNotIn('planted', r.stdout)

    def test_a_missing_release_makes_a_part_unavailable(self):
        self.stamp('v1.2.3')
        self.stub('hook')
        temp = pathlib.Path(self.tmp.name) / 'runner-temp'
        r = self.launch('part', '--id', 'x', '--command', '--id', '--timeout-minutes', '5', RUNNER_TEMP=str(temp),
                        CI_RUNNER_CLASS='part', CI_RUNNER_POOL_LABELS='h100-cpu')  # a command that reads '--id' is not the id
        self.assertEqual(r.returncode, 0)
        out = self.out.read_text()
        self.assertIn('outcome=unavailable\n', out)
        self.assertIn('next=fallback\n', out)
        self.assertIn('retry-on=["self-hosted","h100-cpu"]\n', out)  # its own pool's labels
        self.assertNotIn(str(self.home), out + r.stdout)  # no home path reaches the job
        art = json.loads((temp / 'ci-runners' / 'x' / 'outcome.json').read_text())  # what the action uploads
        self.assertEqual((art['outcome'], art['next'], art['retry-on']),
                         ('unavailable', 'fallback', ['self-hosted', 'h100-cpu']))
        self.out.write_text('')
        self.launch('part', '--id', '../x', RUNNER_TEMP=str(temp))  # no pool labels: no fallback; a bad id: no artifact
        self.assertIn('next=none\n', self.out.read_text())
        self.assertEqual(sorted(str(p.relative_to(temp)) for p in temp.rglob('*')), ['ci-runners', 'ci-runners/x', 'ci-runners/x/outcome.json'])
        self.assertEqual(self.launch('verdict', '--parts', 'cpu').returncode, 2)  # only a part has a fallback
        for own, want in (('h100-cpu', '["self-hosted","h100-cpu"]'), ('a,b-1', '["self-hosted","a","b-1"]'),
                          ('bad label', '[]'), ('x"]', '[]')):
            self.out.write_text('')
            self.launch('part', '--id', 'y', CI_RUNNER_CLASS='part', CI_RUNNER_POOL_LABELS=own)
            self.assertIn(f'retry-on={want}\n', self.out.read_text(), own)

    def test_a_hosted_runner_has_no_path_of_its_own(self):
        """Decision 11.12: the hosted claim is retired; every action needs our host layer."""
        self.stamp('v1.2.3')
        for cmd in ('claim', 'part'):
            r = self.launch(cmd, '--context', 'p/ci', RUNNER_ENVIRONMENT='github-hosted')
            self.assertIn('is not installed on this host', r.stdout, cmd)
        self.assertEqual(self.launch('claim', '--context', 'p/ci', RUNNER_ENVIRONMENT='github-hosted').returncode, 2)


if __name__ == '__main__':
    unittest.main()
