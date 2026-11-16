"""The read-only consumer entry point and malformed part outcomes."""
from __future__ import annotations

import io
import json
import pathlib
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout

from ci_runners import artifacts, cli


def write(root: pathlib.Path, name: str, outcome: dict, summary: str = '') -> None:
    path = root / name
    path.mkdir()
    (path / 'outcome.json').write_text(json.dumps(outcome), encoding='utf-8')
    if summary:
        (path / 'summary.md').write_text(summary, encoding='utf-8')


class Reader(unittest.TestCase):
    def test_cli_reports_current_attempts_in_the_versioned_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            first = {'outcome': 'pass', 'run-attempt': 2, 'run-seconds': 12}
            write(root, 'ci-part-cpu-smoke-1', first, 'CPU passed')
            write(root, 'ci-part-cpu-smoke-2', {'outcome': 'oom', 'run-attempt': 1})
            write(root, 'rows-cpu-smoke-1', {'outcome': 'red'})
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(cli.main(['artifacts', str(root)]), 0)
        self.assertEqual(json.loads(out.getvalue()), {'schema': 1, 'parts': {
            'cpu-smoke': {'1': {'outcome': first, 'summary': 'CPU passed'}},
        }})

    def test_a_downloaded_outcome_with_a_list_kind_is_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            write(root, 'ci-part-bad-1', {'outcome': ['pass']})
            valid = {'outcome': 'pass'}
            write(root, 'ci-part-good-1', valid)
            self.assertEqual(artifacts.collect(directory), {'good': {1: (valid, '')}})

    def test_a_zip_outcome_with_an_object_kind_is_skipped(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w') as archive:
            archive.writestr('outcome.json', '{"outcome": {"kind": "pass"}}')
        self.assertIsNone(artifacts.members('ci-part-bad-1', data.getvalue()))
