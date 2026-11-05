"""The verdict of ``h100-test.yml`` (design §4.7, job 5): a red part fails the head; an
infrastructure end the retry did not cure is an error; a part with no artifact is read
from its record, else it is lost."""
from __future__ import annotations

import io
import json
import os
import pathlib
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock

from ci_runners import inventory, verdict
from ci_runners.verdict import End

SHA = 'a' * 40


def art(root: pathlib.Path, name: str, outcome: dict | str, summary: str = '') -> None:
    d = root / name
    d.mkdir(parents=True)
    (d / 'outcome.json').write_text(outcome if isinstance(outcome, str) else json.dumps(outcome))
    if summary:
        (d / 'summary.md').write_text(summary)


class Decide(unittest.TestCase):
    def test_every_part_passes(self):
        v = verdict.decide([End('gpu', 'pass'), End('cpu', 'pass')])
        self.assertEqual(v.state, 'success')
        self.assertEqual(v.description, 'passed: cpu, gpu')

    def test_a_red_part_fails_the_head_whatever_the_others_did(self):
        for other in ('pass', 'oom', 'lost'):
            v = verdict.decide([End('cpu', 'red'), End('gpu', other)])
            self.assertEqual(v.state, 'failure', other)
            self.assertIn('cpu red', v.description)
        self.assertEqual(verdict.decide([End('gpu', 'disagreement')]).state, 'failure')

    def test_an_infrastructure_end_is_an_error_not_a_failure(self):
        for kind in ('oom', 'thermal', 'reboot', 'yielded', 'unavailable', 'lost', 'stopped'):
            v = verdict.decide([End('cpu', 'pass'), End('gpu', kind)])
            self.assertEqual(v.state, 'error', kind)
            self.assertEqual(v.description, f'infrastructure: gpu {kind}')

    def test_no_part_is_an_error(self):
        self.assertEqual(verdict.decide([]).state, 'error')

    def test_the_description_fits_a_status(self):
        v = verdict.decide([End(f'p{i:02d}-' + 'x' * 20, 'red') for i in range(20)])
        self.assertLessEqual(len(v.description), 140)


class Artifacts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_collect_by_id_and_attempt(self):
        art(self.root, 'ci-part-cpu-1', {'outcome': 'oom', 'next': 'retry'}, '### cpu 1\n')
        art(self.root, 'ci-part-cpu-2', {'outcome': 'pass', 'next': 'none'}, '### cpu 2\n')
        art(self.root, 'ci-part-gpu-1', {'outcome': 'pass'})
        got = verdict.collect(str(self.root))
        self.assertEqual(sorted(got), ['cpu', 'gpu'])
        self.assertEqual(sorted(got['cpu']), [1, 2])
        self.assertEqual(got['cpu'][2], ({'outcome': 'pass', 'next': 'none'}, '### cpu 2\n'))

    def test_what_is_not_an_artifact_is_skipped(self):
        art(self.root, 'other-cpu-1', {'outcome': 'pass'})
        art(self.root, 'ci-part-cpu-x', {'outcome': 'pass'})
        art(self.root, 'ci-part-gpu-1', {'outcome': 'great'})
        art(self.root, 'ci-part-tpu-1', '[1, 2]')
        art(self.root, 'ci-part-npu-1', 'not json')
        real = self.root / 'elsewhere'
        art(real, 'x', {'outcome': 'pass'})
        (self.root / 'ci-part-lnk-1').symlink_to(real / 'x')
        self.assertEqual(verdict.collect(str(self.root)), {})
        self.assertEqual(verdict.collect(str(self.root / 'missing')), {})

    def test_next_attempt(self):
        self.assertEqual(verdict.next_attempt({}), 1)
        self.assertEqual(verdict.next_attempt({1: ({'outcome': 'pass', 'next': 'none'}, '')}), 0)
        self.assertEqual(verdict.next_attempt({1: ({'outcome': 'oom', 'next': 'retry'}, '')}), 2)
        self.assertEqual(verdict.next_attempt({1: ({'outcome': 'thermal', 'next': 'fallback'}, '')}), 2)
        self.assertEqual(verdict.next_attempt({1: ({'next': 'retry'}, ''), 2: ({'outcome': 'oom', 'next': 'retry'}, '')}),
                         0)  # one retry, no more

    def test_ends_read_the_records_where_the_artifacts_stop(self):
        found = {'cpu': {1: ({'outcome': 'oom', 'next': 'retry'}, '')},
                 'gpu': {1: ({'outcome': 'pass', 'next': 'none'}, 'g')}}
        asked = []

        def salvage(pid, n):
            asked.append((pid, n))
            return {'cpu': {'outcome': 'pass'}, 'lnt': None}.get(pid)
        got = verdict.ends(['cpu', 'gpu', 'lnt'], found, salvage)
        self.assertEqual(asked, [('cpu', 2), ('lnt', 1)])  # gpu's artifacts are complete
        self.assertEqual([(e.id, e.outcome, e.attempt) for e in got],
                         [('cpu', 'pass', 2), ('gpu', 'pass', 1), ('lnt', 'lost', 1)])

    def test_a_retry_that_left_nothing_keeps_the_first_end(self):
        got = verdict.ends(['cpu'], {'cpu': {1: ({'outcome': 'oom', 'next': 'retry'}, '')}}, lambda *_: None)
        self.assertEqual((got[0].outcome, got[0].attempt), ('oom', 1))

    def test_part_ids(self):
        self.assertEqual(verdict.parse_ids(' cpu  gpu '), ['cpu', 'gpu'])
        for bad in ('cpu ../x', 'cpu cpu', '-x'):
            with self.assertRaises(ValueError, msg=bad):
                verdict.parse_ids(bad)


class Main(unittest.TestCase):
    """``ci-run verdict`` end to end, on a fake host without part records."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = pathlib.Path(self.tmp.name)
        self.arts = t / 'arts'
        self.arts.mkdir()
        self.summary, self.output = t / 'summary', t / 'output'
        self.inv = inventory.load(f'''schema = 1
this = "hb"
[hosts.hb]
ssh = "alias-x"
account = "acct-x"
home = "{t}"
data = "{t}/data"
trusted = true
cpu_slowdown = 1.0
slots = 1
memory_max = "120G"
disk_min_gib = 0
gpus = [0]
max_temp_c = 80
pools = {{ light = 1 }}
files = {{ host_lock = "{t}/h.lock", slot_lock = "{t}/s.lock", slot_queue = "{t}/s.queue" }}
''')
        self.env = {'GITHUB_REPOSITORY': 'Org/Repo', 'GITHUB_RUN_ID': '77', 'GITHUB_RUN_ATTEMPT': '1',
                    'GITHUB_TOKEN': 'tok', 'GITHUB_STEP_SUMMARY': str(self.summary), 'GITHUB_OUTPUT': str(self.output)}
        self.posted = []

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, method, url, token, body=None):
        self.posted.append((method, url, token, body))
        return {}

    def main(self, *args, env=None):
        with mock.patch.dict(os.environ, env or self.env, clear=True), redirect_stderr(io.StringIO()):
            return verdict.main(['--context', 'p/ci', '--sha', SHA, '--artifacts', str(self.arts), *args], self.inv,
                                call=self.call)

    def test_green_parts_post_success(self):
        art(self.arts, 'ci-part-cpu-1', {'outcome': 'pass', 'next': 'none'}, '### part `cpu`\n')
        art(self.arts, 'ci-part-gpu-1', {'outcome': 'pass', 'next': 'none'}, '### part `gpu`\n')
        self.assertEqual(self.main('--parts', 'cpu gpu'), 0)
        [(method, url, token, body)] = self.posted
        self.assertEqual((method, url, token), ('POST', f'https://api.github.com/repos/org/repo/statuses/{SHA}', 'tok'))
        self.assertEqual((body['state'], body['context']), ('success', 'p/ci'))
        self.assertTrue(body['target_url'].endswith('/org/repo/actions/runs/77'))
        self.assertIn('### part `gpu`', self.summary.read_text())
        self.assertEqual(self.output.read_text(), 'state=success\n')

    def test_a_part_without_artifact_or_record_is_lost(self):
        art(self.arts, 'ci-part-cpu-1', {'outcome': 'pass', 'next': 'none'})
        self.assertEqual(self.main('--parts', 'cpu gpu'), 1)
        self.assertEqual(self.posted[0][3]['state'], 'error')
        self.assertIn('gpu lost', self.posted[0][3]['description'])

    def test_a_red_part_posts_failure(self):
        art(self.arts, 'ci-part-cpu-1', {'outcome': 'red', 'next': 'none'})
        self.assertEqual(self.main('--parts', 'cpu'), 1)
        self.assertEqual(self.posted[0][3]['state'], 'failure')

    def test_malformed_inputs_post_nothing(self):
        self.assertEqual(self.main('--parts', 'cpu ../x'), 2)
        self.assertEqual(self.main('--parts', 'cpu', env={**self.env, 'GITHUB_RUN_ID': '7 7'}), 2)
        self.assertEqual(self.main('--parts', 'cpu', env={k: v for k, v in self.env.items() if k != 'GITHUB_TOKEN'}), 2)
        self.assertEqual(self.posted, [])


if __name__ == '__main__':
    unittest.main()
