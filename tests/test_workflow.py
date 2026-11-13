"""The workflows of this repository and the YAML examples of its docs: a flow mapping
holds an expression only quoted (design §4.8). The reusable ``h100-test.yml`` was
deleted with h100 (decision 12.17)."""
from __future__ import annotations

import pathlib
import re
import unittest
import unittest.mock

ROOT = pathlib.Path(__file__).resolve().parents[1]


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
            # a block may sit indented in a list item; its closing fence has the same indent
            blocks = [b for _, b in re.findall(r'^( *)```yaml\n(.*?)^\1```', f.read_text(), re.M | re.S)]
            self.assertTrue(blocks or f.name != 'consumers.md')
            for b in blocks:
                self.assertEqual(self.bad(b.splitlines()), [], f.name)


class OwnCheck(unittest.TestCase):
    """This repository's check (test.yml): the unit tests and every mutant shard side by
    side, each job bounded at 15 min, and the required ``unit (3.12)`` green only where all are."""

    def test_the_shards_cover_every_mutant_and_the_required_check_needs_them_all(self):
        import sys
        sys.path.insert(0, str(ROOT / 'tests'))
        import mutants
        from tests import yamlsub
        doc = yamlsub.load((ROOT / '.github' / 'workflows' / 'test.yml').read_text())
        jobs = doc['jobs']
        self.assertEqual(jobs['mutants']['strategy']['matrix']['shard'], list(range(1, mutants.SHARDS + 1)))
        self.assertIs(jobs['mutants']['strategy']['fail-fast'], False)
        run = jobs['mutants']['steps'][-1]['run']
        self.assertEqual(run, f'python tests/mutants.py --shard ${{{{ matrix.shard }}}}/{mutants.SHARDS}')
        self.assertEqual(jobs['mutants']['name'], f'mutants (${{{{ matrix.shard }}}}/{mutants.SHARDS})')
        for jid in ('tests', 'mutants'):
            self.assertLessEqual(jobs[jid]['timeout-minutes'], 15, jid)
        unit = jobs['unit']
        self.assertEqual(unit['name'], 'unit (3.12)')
        self.assertEqual((unit['needs'], unit['if']), (['tests', 'mutants'], 'always()'))
        self.assertEqual(unit['steps'][0]['env'], {'TESTS': '${{ needs.tests.result }}',
                                                   'MUTANTS': '${{ needs.mutants.result }}'})
        self.assertTrue(unit['steps'][0]['run'].endswith('[ "$TESTS" = success ] && [ "$MUTANTS" = success ]\n'))
        tests_run = jobs['tests']['steps'][-1]['run']
        self.assertIn('python -I -m unittest discover -s tests -t .', tests_run)
        self.assertIn('python -m ci_runners.inventory lint . --deny tests/deny.sha256', tests_run)

    def test_a_shard_is_every_nth_mutant(self):
        import sys
        sys.path.insert(0, str(ROOT / 'tests'))
        import mutants
        self.assertEqual(mutants.shard([]), (1, 1))
        self.assertEqual(mutants.shard(['--shard', '3/4']), (3, 4))
        for bad in (['--shard', '0/4'], ['--shard', '5/4'], ['--shard', '1'], ['--shard', 'a/b'], ['-x'],
                    ['--shard', '1/2', 'x']):
            with self.assertRaises(SystemExit, msg=bad):
                mutants.shard(bad)
        calls = []
        with unittest.mock.patch.object(mutants, 'MUTANTS', [('README.md', 'x', 'y', [f't{i}']) for i in range(10)]), \
                unittest.mock.patch.object(mutants, 'run', side_effect=lambda tree, tests, hang=0: calls.append(tests) or 0), \
                unittest.mock.patch.object(mutants, 'one', side_effect=lambda tree, *m: (True, 0.0)), \
                unittest.mock.patch.object(mutants.stamp, 'stamp'), \
                unittest.mock.patch('pathlib.Path.read_text', return_value='x'), \
                unittest.mock.patch('shutil.copytree'), unittest.mock.patch('builtins.print'):
            self.assertEqual(mutants.main(['--shard', '2/4']), 0)
        self.assertEqual(calls, [['t1', 't5', 't9']])  # the shard's own tests, once, unmutated


if __name__ == '__main__':
    unittest.main()
