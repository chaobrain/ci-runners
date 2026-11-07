"""The verdict of ``h100-test.yml`` (design §4.7, job 5): a red part fails the head; an
infrastructure end the retry did not cure is an error; a part with no artifact is read
from its record, else it is lost."""
from __future__ import annotations

import io
import json
import os
import pathlib
import tempfile
import time
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

    def test_an_infrastructure_end_is_an_error_not_a_failure(self):
        for kind in ('oom', 'thermal', 'reboot', 'unavailable', 'lost', 'stopped'):
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
account = "acct-x"
home = "{t}"
data = "{t}/data"
slots = 1
memory_max = "120G"
disk_min_gib = 0
gpus = [0]
max_temp_c = 80
pools = {{ light = 1 }}
files = {{ slot_lock = "{t}/s.lock", slot_queue = "{t}/s.queue" }}
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


class Salvage(unittest.TestCase):
    """The salvage action's command (design §4.5, decision 12.13): this host's parts of
    the run whose artifacts stop short, and nothing else."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = pathlib.Path(self.tmp.name)
        self.arts, self.outdir, self.output = t / 'arts', t / 'out', t / 'output'
        self.arts.mkdir()
        self.parts = t / 'data/ci-runners/state/parts/org/repo'
        self.inv = inventory.load(f'''schema = 1
this = "hb"
[hosts.hb]
account = "acct-x"
home = "{t}"
data = "{t}/data"
slots = 1
memory_max = "120G"
disk_min_gib = 0
gpus = [0]
max_temp_c = 80
pools = {{ light = 1 }}
files = {{ slot_lock = "{t}/s.lock", slot_queue = "{t}/s.queue" }}
''')
        self.env = {'GITHUB_REPOSITORY': 'Org/Repo', 'GITHUB_RUN_ID': '77', 'GITHUB_RUN_ATTEMPT': '1',
                    'GITHUB_OUTPUT': str(self.output)}

    def tearDown(self):
        self.tmp.cleanup()

    def record(self, run, pid, n=1, *, start=1000.0, outcome='pass', unit=''):
        d = self.parts / run / f'{pid}-{n}'
        d.mkdir(parents=True)
        rec = {'id': pid, 'attempt': n, 'start_epoch': start, 'host': 'hb', 'class': 'part', 'unit': unit,
               'run_epoch': start}
        (d / 'record.json').write_text(json.dumps(rec))
        if outcome:
            (d / 'outcome.json').write_text(json.dumps({'outcome': outcome, 'reason': 'exit 0', 'next': 'none',
                                                        'attempt': n}))
        return d

    def main(self, parts, *, wait=0, run=None):
        with mock.patch.dict(os.environ, self.env, clear=True), redirect_stderr(io.StringIO()):
            return verdict.salvage_main(['--parts', parts, '--artifacts', str(self.arts), '--out', str(self.outdir),
                                         '--wait-seconds', str(wait)], self.inv, systemctl=run, poll=0.05)

    def uploaded(self):
        return sorted(os.listdir(self.outdir)) if self.outdir.exists() else []

    def test_only_this_hosts_parts_that_stop_short(self):
        art(self.arts, 'ci-part-done-1', {'outcome': 'pass', 'next': 'none'})
        self.record('77-1', 'done')  # its artifact is there: nothing to do
        self.record('77-1', 'here', outcome='red')  # ran here, its job left no artifact
        # 'away' ran on another host: no record of it here, only one shaped like it
        d = self.record('77-1', 'away')
        rec = json.loads((d / 'record.json').read_text())
        (d / 'record.json').write_text(json.dumps({**rec, 'id': 'other'}))
        self.assertEqual(self.main('done here away'), 0)
        self.assertEqual(self.uploaded(), ['ci-part-here-1'])
        got = json.loads((self.outdir / 'ci-part-here-1' / 'outcome.json').read_text())
        self.assertEqual((got['outcome'], got['salvaged']), ('red', True))
        self.assertEqual(self.output.read_text(), 'count=1\nname-1=ci-part-here-1\n')

    def test_a_newer_record_of_another_run_or_attempt_is_left_alone(self):
        self.record('77-1', 'cpu', start=1000)
        self.record('78-1', 'cpu', start=2000)  # a later run ran its own cpu here
        self.record('77-1', 'gpu', start=1000)
        self.record('77-2', 'gpu', start=2000)  # this run's second attempt did too
        self.record('77-1', 'lnt', start=2000)
        self.record('9-1', 'lnt', start=1000)  # an older run's record does not matter (start time, not name)
        self.assertEqual(self.main('cpu gpu lnt'), 0)
        self.assertEqual(self.uploaded(), ['ci-part-lnt-1'])

    def test_the_retry_whose_artifact_is_missing(self):
        art(self.arts, 'ci-part-cpu-1', {'outcome': 'thermal', 'next': 'retry'})
        self.record('77-1', 'cpu', 1, start=1000)
        self.record('77-1', 'cpu', 2, start=2000)
        self.assertEqual(self.main('cpu'), 0)
        self.assertEqual(self.uploaded(), ['ci-part-cpu-2'])

    def test_a_retry_that_ran_elsewhere_is_left_alone(self):
        art(self.arts, 'ci-part-cpu-1', {'outcome': 'thermal', 'next': 'retry'})
        self.record('77-1', 'cpu', 1)  # attempt 1 ran here and uploaded; attempt 2 ran on another host
        self.assertEqual(self.main('cpu'), 0)
        self.assertEqual(self.uploaded(), [])

    def test_more_parts_than_the_action_uploads_is_refused(self):
        ids = [f'p{i}' for i in range(verdict.MAX_SALVAGED + 1)]
        for i in ids:
            self.record('77-1', i)
        self.assertEqual(self.main(' '.join(ids)), 2)
        self.assertEqual(self.uploaded(), [])
        self.assertEqual(self.main(' '.join(ids[1:])), 0)  # the bound itself is fine
        self.assertEqual(len(self.uploaded()), verdict.MAX_SALVAGED)

    def test_a_part_still_running_is_waited_for_then_lost(self):
        self.record('77-1', 'cpu', outcome='', unit='ci-part-x.scope')
        calls = []

        def active(argv, **kw):
            calls.append(argv)
            return mock.Mock(stdout='active\n', returncode=0)
        t0 = time.monotonic()
        self.assertEqual(self.main('cpu', wait=0.5, run=active), 0)
        self.assertGreaterEqual(time.monotonic() - t0, 0.5)
        self.assertGreater(len(calls), 1)
        got = json.loads((self.outdir / 'ci-part-cpu-1' / 'outcome.json').read_text())
        self.assertEqual((got['outcome'], got['salvaged']), ('lost', True))

    def test_a_part_that_ends_in_the_wait_gives_its_outcome(self):
        d = self.record('77-1', 'cpu', outcome='', unit='ci-part-x.scope')

        def active(argv, **kw):
            (d / 'exit.json').write_text(json.dumps({'rc': 0, 'end_epoch': 1010.0}))
            return mock.Mock(stdout='inactive\n', returncode=3)
        self.assertEqual(self.main('cpu', wait=30, run=active), 0)
        got = json.loads((self.outdir / 'ci-part-cpu-1' / 'outcome.json').read_text())
        self.assertEqual((got['outcome'], got['salvaged']), ('pass', True))

    def test_malformed_inputs(self):
        self.assertEqual(self.main('a a'), 2)
        self.assertEqual(self.main('../x'), 2)
        self.env['GITHUB_RUN_ID'] = 'x'
        self.assertEqual(self.main('cpu'), 2)
