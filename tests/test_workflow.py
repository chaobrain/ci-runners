"""The workflows of this repository and the YAML examples of its docs: a flow mapping
holds an expression only quoted (design §4.8). The reusable ``h100-test.yml`` was
deleted with h100 (decision 12.17)."""
from __future__ import annotations

import pathlib
import re
import unittest

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


if __name__ == '__main__':
    unittest.main()
