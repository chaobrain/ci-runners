"""The verdict of ``h100-test.yml`` (design §4.7, job 5): a red part fails the head; an
infrastructure end the retry did not cure is an error; a part with no artifact is read
from its record, else it is lost."""
from __future__ import annotations

import http.client
import io
import json
import os
import pathlib
import tempfile
import time
import unittest
import zipfile
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


def zipped(members: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        for name, text in members.items():
            z.writestr(name, text)
    return buf.getvalue()


#: issue #169's run 43082337585: its gpu-3 artifacts, one per run attempt; run attempt 2's
#: ci-part-gpu-3-1 (19:38) has the lower id
ISSUE_169 = [
    {'id': 13887327475, 'name': 'rows-gpu-3-1', 'created_at': '2026-11-11T19:38:28Z', 'expired': False},
    {'id': 13887210679, 'name': 'rows-gpu-3-1', 'created_at': '2026-11-11T19:23:22Z', 'expired': False},
    {'id': 13887086600, 'name': 'ci-part-gpu-3-1', 'created_at': '2026-11-11T19:23:13Z', 'expired': False},
    {'id': 13886754673, 'name': 'impact-gpu-3-1', 'created_at': '2026-11-11T19:38:37Z', 'expired': False},
    {'id': 13886572319, 'name': 'impact-gpu-3-1', 'created_at': '2026-11-11T19:23:31Z', 'expired': False},
    {'id': 13886519664, 'name': 'ci-part-gpu-3-1', 'created_at': '2026-11-11T19:38:19Z', 'expired': False},
]


class Latest(unittest.TestCase):
    """Issue #169: each part's latest run attempt, whatever the artifacts' ids."""

    def test_the_newest_of_a_name_where_download_artifact_took_another(self):
        self.assertEqual(verdict.stale(ISSUE_169), {'ci-part-gpu-3-1': 13886519664})
        ordered = [{**a, 'id': 1 if a['created_at'] < '2026-11-11T19:30' else 2} for a in ISSUE_169]
        self.assertEqual(verdict.stale([a for a in ordered if a['name'].startswith('ci-part')]), {})  # it took the newest
        self.assertEqual(verdict.stale([ISSUE_169[2]]), {})  # one of a name
        expired = [ISSUE_169[2], {**ISSUE_169[5], 'expired': True}]
        self.assertEqual(verdict.stale(expired), {})
        same = [ISSUE_169[2], {**ISSUE_169[5], 'created_at': ISSUE_169[2]['created_at']}]  # a tie: the higher id
        self.assertEqual(verdict.stale(same), {})
        junk = [ISSUE_169[2], {**ISSUE_169[5], 'id': '1'}, {'name': 'ci-part-x-1'}, 'x', {**ISSUE_169[5], 'name': 3}]
        self.assertEqual(verdict.stale(junk), {})

    def test_the_listing_reads_every_page(self):
        pages = [{'artifacts': [{}] * 100}, {'artifacts': [{}] * 3}]
        urls = []

        def call(method, url, token):
            urls.append(url)
            return pages[len(urls) - 1]
        self.assertEqual(len(verdict.run_artifacts(call, 'https://api', 'o/r', '7', 't')), 103)
        self.assertEqual(urls, ['https://api/repos/o/r/actions/runs/7/artifacts?per_page=100&page=1',
                                'https://api/repos/o/r/actions/runs/7/artifacts?per_page=100&page=2'])
        with self.assertRaises(verdict.github.ApiError):
            verdict.run_artifacts(lambda *a: {'message': 'x'}, 'https://api', 'o/r', '7', 't')

    def test_the_zip_s_token_does_not_follow_the_redirect(self):
        seen = []

        class R(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def opener(req, timeout):
            seen.append(req)
            return R(b'zipdata')
        self.assertEqual(verdict.fetch_zip('https://api/x/zip', 'tok', opener), b'zipdata')
        self.assertEqual(seen[0].unredirected_hdrs, {'Authorization': 'Bearer tok'})  # not sent to the storage
        self.assertNotIn('Authorization', seen[0].headers)
        with mock.patch.object(verdict, 'MAX_ZIP', 3), self.assertRaisesRegex(ValueError, 'larger'):
            verdict.fetch_zip('https://api/x/zip', 'tok', opener)

    def test_unpack_replaces_the_outcome_and_the_summary_only(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            art(root, 'ci-part-a-1', {'outcome': 'red'}, 'old summary')
            verdict.unpack(d, 'ci-part-a-1', zipped({'outcome.json': '{"outcome": "pass"}', '../evil': 'x',
                                                     'sub/outcome.json': 'x'}))
            self.assertEqual(sorted(p.name for p in (root / 'ci-part-a-1').iterdir()), ['outcome.json'])
            self.assertFalse((root / 'evil').exists())
            verdict.unpack(d, 'ci-part-b-1', zipped({'outcome.json': '{"outcome": "pass"}', 'summary.md': 's'}))
            self.assertEqual(verdict.collect(d)['b'][1], ({'outcome': 'pass'}, 's'))
            with mock.patch.object(verdict, 'MAX_FILE', 3):  # an oversized member is left out
                verdict.unpack(d, 'ci-part-c-1', zipped({'outcome.json': '{"outcome": "pass"}'}))
            self.assertFalse((root / 'ci-part-c-1' / 'outcome.json').exists())
            with self.assertRaises(ValueError):
                verdict.latest(d, lambda *a: {'artifacts': ISSUE_169}, lambda url, tok: b'not a zip', 'https://api',
                               'o/r', '7', 't')
            # a zip that fails while it is read (here its CRC) leaves the downloaded files as they were
            art(root, 'ci-part-gpu-3-1', {'outcome': 'red'}, 'red')
            bad = bytearray(zipped({'outcome.json': '{"outcome": "pass"}'}))
            at = bad.index(b'{"outcome"')
            bad[at + 2] ^= 1
            with self.assertRaisesRegex(ValueError, 'cannot be read'):
                verdict.latest(d, lambda *a: {'artifacts': ISSUE_169}, lambda url, tok: bytes(bad), 'https://api',
                               'o/r', '7', 't')
            self.assertEqual(verdict.collect(d)['gpu-3'][1], ({'outcome': 'red'}, 'red'))
            buf = io.BytesIO()  # a broken deflate stream: zlib's own error, not zipfile's
            with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
                z.writestr('outcome.json', '{"outcome": "pass"}' * 50)
            broken = bytearray(buf.getvalue())
            broken[30 + len('outcome.json')] = 0xff  # the first byte of the data: an invalid block type
            with self.assertRaisesRegex(ValueError, 'cannot be read'):
                verdict.unpack(d, 'ci-part-gpu-3-1', bytes(broken))
            self.assertEqual(verdict.collect(d)['gpu-3'][1], ({'outcome': 'red'}, 'red'))

            def short(url, tok):
                raise http.client.IncompleteRead(b'')
            with self.assertRaises(verdict.github.ApiError):
                verdict.latest(d, lambda *a: {'artifacts': ISSUE_169}, short, 'https://api', 'o/r', '7', 't')
            os.rename(root / 'ci-part-gpu-3-1', root / 'elsewhere')  # a planted link in its place is replaced
            os.symlink(root / 'elsewhere', root / 'ci-part-gpu-3-1')
            verdict.unpack(d, 'ci-part-gpu-3-1', zipped({'outcome.json': '{"outcome": "pass"}'}))
            self.assertFalse((root / 'ci-part-gpu-3-1').is_symlink())
            self.assertEqual((root / 'elsewhere' / 'summary.md').read_text(), 'red')


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

    def test_the_description_fits_a_status_whatever_the_state(self):
        # 29 parts that all pass once made a 236-character description: HTTP 422, no status
        ids = ['plan'] + [f'cpu-{i}' for i in range(10)] + [f'sharding-{i}' for i in range(5)] \
            + [f'gpu-{i}' for i in range(11)] + ['gpu-multi', 'compile']
        for outcome, state in (('pass', 'success'), ('red', 'failure'), ('lost', 'error')):
            v = verdict.decide([End(i, outcome) for i in ids])
            self.assertEqual(v.state, state)
            self.assertLessEqual(len(v.description), 140, outcome)
        self.assertEqual(verdict.decide([End(i, 'pass') for i in ids]).description,
                         f'passed: all {len(ids)} parts')


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

    def test_a_retry_an_earlier_run_attempt_left_is_dropped(self):
        # issue #64: run attempt 1's cpu and its retry ended unavailable; run attempt 3
        # re-ran cpu alone, which passed, and the old ci-part-cpu-2 is still in the run
        rec = 'hb:org/repo/77-{}/{}-{}'.format
        art(self.root, 'ci-part-cpu-1', {'outcome': 'pass', 'next': 'none', 'record': rec(3, 'cpu', 1)})
        art(self.root, 'ci-part-cpu-2', {'outcome': 'unavailable', 'next': 'none', 'record': rec(1, 'cpu', 2)})
        # the same run attempt: the retry counts, as before
        art(self.root, 'ci-part-gpu-1', {'outcome': 'oom', 'next': 'retry', 'record': rec(2, 'gpu', 1)})
        art(self.root, 'ci-part-gpu-2', {'outcome': 'pass', 'next': 'none', 'record': rec(2, 'gpu', 2)})
        # a retry made in a later run attempt than its first attempt counts too
        art(self.root, 'ci-part-lnt-1', {'outcome': 'oom', 'next': 'retry', 'record': rec(1, 'lnt', 1)})
        art(self.root, 'ci-part-lnt-2', {'outcome': 'red', 'next': 'none', 'record': rec(2, 'lnt', 2)})
        got = verdict.collect(str(self.root))
        self.assertEqual({i: sorted(a) for i, a in got.items()}, {'cpu': [1], 'gpu': [1, 2], 'lnt': [1, 2]})
        items = verdict.ends(['cpu', 'gpu', 'lnt'], got, lambda pid, n: None)
        self.assertEqual([(e.id, e.outcome, e.attempt) for e in items],
                         [('cpu', 'pass', 1), ('gpu', 'pass', 2), ('lnt', 'red', 2)])
        self.assertEqual(verdict.decide(items).state, 'failure')
        self.assertEqual(verdict.run_attempt({'record': 'hb:org/repo/77-12/cpu-1'}), 12)
        self.assertEqual(verdict.run_attempt({}), 0)

    def test_a_first_attempt_without_a_record_still_drops_a_stale_retry(self):
        # the re-run's first attempt found no release installed (no record), so only its
        # run-attempt field names run attempt 3; run attempt 1's retry must not decide
        art(self.root, 'ci-part-cpu-1', {'outcome': 'unavailable', 'next': 'none', 'run-attempt': 3})
        art(self.root, 'ci-part-cpu-2', {'outcome': 'pass', 'next': 'none', 'record': 'hb:org/repo/77-1/cpu-2'})
        got = verdict.collect(str(self.root))
        self.assertEqual(sorted(got['cpu']), [1])
        self.assertEqual(verdict.ends(['cpu'], got, lambda pid, n: None)[0].outcome, 'unavailable')
        # the field wins over the record; a malformed one falls back to the record
        self.assertEqual(verdict.run_attempt({'run-attempt': 4, 'record': 'hb:org/repo/77-2/cpu-1'}), 4)
        self.assertEqual(verdict.run_attempt({'run-attempt': True, 'record': 'hb:org/repo/77-2/cpu-1'}), 2)
        self.assertEqual(verdict.run_attempt({'run-attempt': '5', 'record': 'hb:org/repo/77-2/cpu-1'}), 2)

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
        self.assertEqual(verdict.next_attempt({1: ({'outcome': 'pass', 'next': 'none'}, '')}), 2)
        self.assertEqual(verdict.next_attempt({1: ({'outcome': 'oom', 'next': 'retry'}, '')}), 2)
        self.assertEqual(verdict.next_attempt({1: ({'outcome': 'thermal', 'next': 'fallback'}, '')}), 2)
        # attempt 2 said none: a consumer's fallback job is attempt 3 (issue #161)
        self.assertEqual(verdict.next_attempt({1: ({'next': 'retry'}, ''), 2: ({'outcome': 'unavailable',
                                                                               'next': 'none'}, '')}), 3)

    def test_ends_read_the_records_where_the_artifacts_stop(self):
        found = {'cpu': {1: ({'outcome': 'oom', 'next': 'retry'}, '')},
                 'gpu': {1: ({'outcome': 'pass', 'next': 'none'}, 'g')}}
        asked = []
        have = {('cpu', 2): {'outcome': 'pass'}}

        def salvage(pid, n):
            asked.append((pid, n))
            return have.get((pid, n))
        got = verdict.ends(['cpu', 'gpu', 'lnt'], found, salvage)
        # each part's next attempt is asked for, until one has no record
        self.assertEqual(asked, [('cpu', 2), ('cpu', 3), ('gpu', 2), ('lnt', 1)])
        self.assertEqual([(e.id, e.outcome, e.attempt) for e in got],
                         [('cpu', 'pass', 2), ('gpu', 'pass', 1), ('lnt', 'lost', 1)])

    def test_a_fallback_after_a_retry_that_did_not_start_is_read(self):
        # issue #161: attempt 2 (a cluster retry pending past its bound) ended unavailable
        # with next none; the consumer's fallback job, attempt 3, lost its runner
        found = {'cpu': {1: ({'outcome': 'oom', 'next': 'retry'}, ''),
                         2: ({'outcome': 'unavailable', 'reason': 'pending past the bound', 'next': 'none'}, '')}}
        have = {('cpu', 3): {'outcome': 'red', 'reason': 'exit 1'}}
        got = verdict.ends(['cpu'], found, lambda pid, n: have.get((pid, n)))
        self.assertEqual((got[0].outcome, got[0].attempt), ('red', 3))
        self.assertEqual(verdict.decide(got).state, 'failure')
        # its own artifact, attempt 3, decides like any last attempt
        art(self.root, 'ci-part-gpu-1', {'outcome': 'oom', 'next': 'retry'})
        art(self.root, 'ci-part-gpu-2', {'outcome': 'unavailable', 'next': 'none'})
        art(self.root, 'ci-part-gpu-3', {'outcome': 'pass', 'next': 'none'})
        got = verdict.ends(['gpu'], verdict.collect(str(self.root)), lambda pid, n: None)
        self.assertEqual((got[0].outcome, got[0].attempt), ('pass', 3))

    def test_every_attempt_is_read_up_to_the_bound(self):
        asked = []

        def salvage(pid, n):
            asked.append(n)
            return {'outcome': 'pass'}
        got = verdict.ends(['cpu'], {}, salvage)
        self.assertEqual((got[0].attempt, asked[-1], len(asked)), (verdict.MAX_ATTEMPT, verdict.MAX_ATTEMPT,
                                                                   verdict.MAX_ATTEMPT))

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

    listing: list | Exception = []  # the run's artifacts, as the API lists them
    zips: dict = {}  # artifact id -> its zip

    def call(self, method, url, token, body=None):
        if method == 'GET':
            self.got.append(url)
            if isinstance(self.listing, Exception):
                raise self.listing
            return {'total_count': len(self.listing), 'artifacts': self.listing}
        self.posted.append((method, url, token, body))
        return {}

    def fetch(self, url, token):
        self.fetched.append((url, token))
        return self.zips[int(url.split('/')[-2])]

    def main(self, *args, env=None):
        self.got, self.fetched, self.err = [], [], io.StringIO()
        with mock.patch.dict(os.environ, env or self.env, clear=True), redirect_stderr(self.err):
            return verdict.main(['--context', 'p/ci', '--sha', SHA, '--artifacts', str(self.arts), *args], self.inv,
                                call=self.call, fetch=self.fetch)

    def test_a_part_re_run_in_a_later_run_attempt_counts(self):
        # issue #169, run 43082337585: attempt 1's gpu-3 red; "Re-run failed jobs" re-ran it,
        # and it passed. Run attempt 2's ci-part-gpu-3-1 has the lower id, so download-artifact
        # (the highest id of a name) gave the verdict run attempt 1's red one
        art(self.arts, 'ci-part-gpu-3-1', {'outcome': 'red', 'next': 'none', 'run-attempt': 1}, 'red summary')
        art(self.arts, 'ci-part-cpu-1', {'outcome': 'pass', 'next': 'none', 'run-attempt': 1})
        self.listing = ISSUE_169 + [{'id': 5, 'name': 'ci-part-cpu-1', 'created_at': '2026-11-11T19:03:00Z'}]
        self.zips = {13886519664: zipped({'outcome.json': json.dumps({'outcome': 'pass', 'next': 'none',
                                                                       'run-attempt': 2})})}
        env = {**self.env, 'GITHUB_RUN_ID': '43082337585', 'GITHUB_RUN_ATTEMPT': '2'}
        self.assertEqual(self.main('--parts', 'cpu gpu-3', env=env), 0)
        self.assertEqual(self.posted[0][3]['state'], 'success')
        self.assertEqual(self.got, ['https://api.github.com/repos/org/repo/actions/runs/43082337585/artifacts'
                                    '?per_page=100&page=1'])
        self.assertEqual(self.fetched, [('https://api.github.com/repos/org/repo/actions/artifacts/13886519664/zip',
                                         'tok')])
        self.assertFalse((self.arts / 'ci-part-gpu-3-1' / 'summary.md').exists())  # the old attempt's is gone
        self.assertIn('ci-part-gpu-3-1: the latest run attempt', self.err.getvalue())

    def test_without_actions_read_the_downloaded_artifacts_decide(self):
        art(self.arts, 'ci-part-gpu-3-1', {'outcome': 'red', 'next': 'none', 'run-attempt': 1})
        self.listing = verdict.github.ApiError('GET .../artifacts: HTTP 403')
        self.assertEqual(self.main('--parts', 'gpu-3'), 1)  # as before: the downloaded red decides
        self.assertEqual(self.posted[0][3]['state'], 'failure')
        self.assertIn('give the job actions: read', self.err.getvalue())
        self.assertEqual(self.fetched, [])
        self.listing = []  # one artifact a name: nothing fetched
        self.assertEqual(self.main('--parts', 'gpu-3'), 1)
        self.assertEqual(self.fetched, [])
        self.assertNotIn('actions: read', self.err.getvalue())

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

    def test_the_fallback_attempt_recorded_here_decides(self):
        # issue #161: artifacts stop at attempt 2 (unavailable, next none); attempt 3, the
        # consumer's fallback, ran on this host and left only its record
        art(self.arts, 'ci-part-cpu-1', {'outcome': 'oom', 'next': 'retry'})
        art(self.arts, 'ci-part-cpu-2', {'outcome': 'unavailable', 'reason': 'pending past the bound',
                                         'next': 'none'})
        d = pathlib.Path(self.tmp.name) / 'data/ci-runners/state/parts/org/repo/77-1/cpu-3'
        d.mkdir(parents=True)
        (d / 'record.json').write_text(json.dumps({'id': 'cpu', 'attempt': 3, 'start_epoch': 1000.0, 'host': 'hb',
                                                   'class': 'part', 'unit': '', 'run_epoch': 1000.0}))
        (d / 'outcome.json').write_text(json.dumps({'outcome': 'pass', 'reason': 'exit 0', 'next': 'none',
                                                    'attempt': 3}))
        self.assertEqual(self.main('--parts', 'cpu'), 0)
        self.assertEqual(self.posted[0][3]['state'], 'success')

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

    def test_another_runs_part_of_the_same_id_changes_nothing(self):
        # this run's cpu: its scope finished, its job was lost before the outcome was written
        old = self.record('77-1', 'cpu', start=1000, outcome='', unit='ci-part-a.scope')
        (old / 'exit.json').write_text(json.dumps({'rc': 0, 'end_epoch': 1010.0}))
        # a later run's cpu ran here too, and is just as unfinished
        new = self.record('78-1', 'cpu', start=2000, outcome='', unit='ci-part-b.scope')
        (new / 'exit.json').write_text(json.dumps({'rc': 1, 'end_epoch': 2010.0}))
        self.record('77-1', 'lnt', start=1000, outcome='red')
        self.record('77-2', 'lnt', start=2000)  # this run's re-run is another run
        art(self.arts, 'ci-part-gpu-1', {'outcome': 'pass', 'next': 'none'})
        self.record('77-1', 'gpu', 1, start=1000)
        self.record('78-1', 'gpu', 2, start=2000)  # a later run's attempt 2 is not this run's
        self.assertEqual(self.main('cpu gpu lnt'), 0)
        self.assertEqual(self.uploaded(), ['ci-part-cpu-1', 'ci-part-lnt-1'])
        got = {i: json.loads((self.outdir / f'ci-part-{i}-1' / 'outcome.json').read_text()) for i in ('cpu', 'lnt')}
        self.assertEqual([(g['outcome'], g['salvaged']) for g in got.values()], [('pass', True), ('red', True)])
        self.assertFalse((new / 'outcome.json').exists())  # the later run's record is untouched

    def test_the_retry_whose_artifact_is_missing(self):
        art(self.arts, 'ci-part-cpu-1', {'outcome': 'thermal', 'next': 'retry'})
        self.record('77-1', 'cpu', 1, start=1000)
        self.record('77-1', 'cpu', 2, start=2000)
        self.assertEqual(self.main('cpu'), 0)
        self.assertEqual(self.uploaded(), ['ci-part-cpu-2'])

    def test_the_fallback_after_a_retry_that_did_not_start(self):
        # issue #161: attempt 2 ended unavailable with next none; the consumer's fallback
        # job, attempt 3, ran here and lost its runner
        art(self.arts, 'ci-part-cpu-1', {'outcome': 'oom', 'next': 'retry'})
        art(self.arts, 'ci-part-cpu-2', {'outcome': 'unavailable', 'next': 'none'})
        self.record('77-1', 'cpu', 3, outcome='red')
        self.assertEqual(self.main('cpu'), 0)
        self.assertEqual(self.uploaded(), ['ci-part-cpu-3'])
        got = json.loads((self.outdir / 'ci-part-cpu-3' / 'outcome.json').read_text())
        self.assertEqual((got['outcome'], got['salvaged']), ('red', True))

    def test_each_attempt_after_the_last_artifact_recorded_here(self):
        # attempts 2 and 3 both ran here and both lost their runners; attempt 5 is no
        # chain's (attempt 4 is not here), and a malformed or linked entry is no record
        art(self.arts, 'ci-part-cpu-1', {'outcome': 'oom', 'next': 'retry'})
        for n in (1, 2, 3, 5):
            self.record('77-1', 'cpu', n)
        (self.parts / '77-1' / 'cpu-04').mkdir()
        (self.parts / '77-1' / 'cpu-4').symlink_to(self.parts / '77-1' / 'cpu-5')
        self.assertEqual(self.main('cpu'), 0)
        self.assertEqual(self.uploaded(), ['ci-part-cpu-2', 'ci-part-cpu-3'])
        self.assertEqual(sorted(verdict.records(str(self.parts.parent.parent), 'org/repo', '77-1', 'cpu')),
                         [1, 2, 3, 5])
        self.assertEqual(verdict.records(str(self.parts.parent.parent), 'org/repo', '79-1', 'cpu'), {})

    def test_an_earlier_run_attempts_artifact_hides_no_record_of_this_one(self):
        """A re-run of failed jobs: run attempt 1 left ``ci-part-cpu-1``; in run attempt 2
        the part's first attempt ran here, ended red and lost its runner before its upload.
        Its record is salvaged (red stays red), not shadowed by the old artifact; a re-run
        of the retry alone (attempt 2 here, attempt 1 from run attempt 1) is salvaged too."""
        self.env['GITHUB_RUN_ATTEMPT'] = '2'
        art(self.arts, 'ci-part-cpu-1', {'outcome': 'oom', 'next': 'retry', 'run-attempt': 1})
        self.record('77-2', 'cpu', 1, outcome='red')
        art(self.arts, 'ci-part-gpu-1', {'outcome': 'oom', 'next': 'retry', 'run-attempt': 1})
        self.record('77-2', 'gpu', 2)
        art(self.arts, 'ci-part-ok-1', {'outcome': 'pass', 'next': 'none', 'run-attempt': 2})
        self.record('77-2', 'ok', 1)  # this run attempt's own artifact holds it: nothing to do
        self.assertEqual(self.main('cpu gpu ok'), 0)
        self.assertEqual(self.uploaded(), ['ci-part-cpu-1', 'ci-part-gpu-2'])
        self.assertEqual(json.loads((self.outdir / 'ci-part-cpu-1' / 'outcome.json').read_text())['outcome'], 'red')

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
