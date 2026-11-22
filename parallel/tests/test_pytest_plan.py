import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from parallel import protocol


HELPER = Path(__file__).resolve().parents[2] / 'bin/ci-pytest-plan'


class PytestPlan(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / 'pytest.py').write_text(
            "import os, sys, json\n"
            "from types import SimpleNamespace\n"
            "def main(args, plugins):\n"
            "    assert '--collect-only' in args and args[-3:] == ['--', 'unit', 'matrix']\n"
            "    os.write(1, b'native collection output\\n')\n"
            "    print('collection diagnostic')\n"
            "    ids = json.loads(os.environ['TEST_NODES'])\n"
            "    plugins[0].pytest_collection_finish(SimpleNamespace(items=[SimpleNamespace(nodeid=n) for n in ids]))\n"
            "    return int(os.environ['TEST_STATUS'])\n")

    def invoke(self, nodes, *args, status=0):
        return subprocess.run([sys.executable, '-B', str(HELPER), *args, '--', 'unit', 'matrix'],
                              capture_output=True, text=True, cwd=self.root,
                              env={**os.environ, 'TEST_NODES': json.dumps(nodes), 'TEST_STATUS': str(status)})

    def test_shards_by_case_count_with_quoted_node_ids_and_no_device_pin(self):
        names = ["tests/x.py::test_case[param '$(printf unsafe)' ; a b]", 'one', 'two', 'three', 'four']
        first = self.invoke(names, '--shards', '3', '--gpu', '--workers', '2')
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn('native collection output', first.stderr)
        result = json.loads(first.stdout)
        self.assertEqual(protocol.load_plan(result)['max_parallel'], 3)
        chunks = []
        for part in result['parts']:
            self.assertNotIn('device', part)
            self.assertTrue(part['gpu'])
            command = shlex.split(part['command'])
            self.assertIn('-n', command)
            chunks.append(command[command.index('--') + 1:])
        self.assertEqual(chunks, [sorted(names)[n::3] for n in range(3)])
        self.assertEqual(self.invoke(list(reversed(names)), '--shards', '3', '--gpu', '--workers', '2').stdout,
                         first.stdout)

    def test_fewer_cases_than_requested_yields_only_nonempty_tasks(self):
        run = self.invoke(['test.py::test_case'], '--shards', '24')
        self.assertEqual(run.returncode, 0, run.stderr)
        result = json.loads(run.stdout)
        self.assertEqual(len(result['parts']), 1)
        self.assertEqual(result['max_parallel'], 1)
        self.assertNotIn('-n', shlex.split(result['parts'][0]['command']))

    def test_collection_errors_and_invalid_inputs_emit_no_plan(self):
        for args, nodes, status in [
            ([], ['partial'], 2), ([], [], 5), ([], [], 0), ([], ['duplicate', 'duplicate'], 0),
            (['--shards', '0'], ['one'], 0), (['--cores', '3'], ['one'], 0)
        ]:
            with self.subTest(args=args, nodes=nodes, status=status):
                result = self.invoke(nodes, *args, status=status)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, '')
