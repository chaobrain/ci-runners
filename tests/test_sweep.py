"""``ci-run sweep`` (design §4.10, decision 19 item 9): a run whose job has queued at GitHub
past the bound is cancelled and its context gets ``error``, never over another run's status,
against a fake GitHub."""
from __future__ import annotations

import contextlib
import datetime
import io
import json
import os
import pathlib
import re
import tempfile
import unittest
import urllib.parse
from unittest import mock

from ci_runners import cli, sweep

API = 'https://api.example'
SERVER = 'https://github.example'
REPO = 'o/r'
TOKEN = 't0k'
NOW = datetime.datetime(2026, 11, 13, 12, 0, tzinfo=datetime.timezone.utc).timestamp()
SHA, SHA2 = 'a' * 40, 'b' * 40
ENV = {'GITHUB_REPOSITORY': REPO, 'GITHUB_TOKEN': TOKEN, 'GITHUB_API_URL': API, 'GITHUB_SERVER_URL': SERVER,
       'GITHUB_RUN_ID': '999'}


def ago(minutes: float) -> str:
    return datetime.datetime.fromtimestamp(NOW - minutes * 60, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def run(rid=7, event='push', sha=SHA, created=90, status='in_progress'):
    return {'id': rid, 'event': event, 'head_sha': sha, 'created_at': ago(created), 'status': status,
            'html_url': f'{SERVER}/{REPO}/actions/runs/{rid}'}


def job(name='ci / part gpu-a / first', status='queued', created=80, labels=('self-hosted', 'ci-gpu')):
    return {'name': name, 'status': status, 'created_at': ago(created), 'labels': list(labels)}


def status(state, rid=7, context='p/ci', description='queued on the runners'):
    return {'state': state, 'context': context, 'description': description,
            'target_url': f'{SERVER}/{REPO}/actions/runs/{rid}'}


class Hub:
    """A fake GitHub: the workflow's runs, each run's jobs and each head's statuses (newest
    first), paged with ``Link`` headers; cancels and posts recorded."""

    def __init__(self, runs=(), jobs=None, statuses=None, jobs_page=100, statuses_page=100, total=None,
                 cancel_code=202, link_host='', fail=None):
        self.runs = list(runs)
        self.jobs = jobs or {}
        self.statuses = {k: list(v) for k, v in (statuses or {}).items()}
        self.jobs_page, self.statuses_page, self.total = jobs_page, statuses_page, total
        self.cancel_code, self.link_host, self.fail = cancel_code, link_host, fail or []
        self.calls: list[tuple[str, str, dict | None]] = []
        self.on_cancel = None

    def page(self, rows, url, size):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        start = (int(q.get('page', ['1'])[0]) - 1) * size
        h = {}
        if start + size < len(rows):
            base = re.sub(r'&page=[^&]*', '', url)
            h['Link'] = f'<{base}&page={start // size + 2}>; rel="next", <{base}>; rel="first"'
            if self.link_host:
                h['Link'] = h['Link'].replace(API, self.link_host)
        return rows[start:start + size], h

    def __call__(self, method, url, headers, body):
        doc = json.loads(body) if body else None
        self.calls.append((method, url, doc))
        assert headers['Authorization'] == f'Bearer {TOKEN}'
        assert url.startswith(API + '/'), url
        if self.fail:
            code = self.fail.pop(0)
            if code:
                return code, {}, b''
        u = urllib.parse.urlsplit(url)
        q = urllib.parse.parse_qs(u.query)
        if method == 'GET' and (m := re.fullmatch(rf'/repos/{REPO}/actions/workflows/([^/]+)/runs', u.path)):
            assert m.group(1) == 'ci.yml' and q['per_page'] == ['100']
            rows = [r for r in self.runs if r['status'] == q['status'][0]]
            return 200, {}, json.dumps({'total_count': self.total or len(rows), 'workflow_runs': rows[:100]}).encode()
        if method == 'GET' and (m := re.fullmatch(rf'/repos/{REPO}/actions/runs/([0-9]+)/jobs', u.path)):
            assert q['filter'] == ['latest']
            rows = self.jobs.get(int(m.group(1)), [])
            got, h = self.page(rows, url, self.jobs_page)
            return 200, h, json.dumps({'total_count': len(rows), 'jobs': got}).encode()
        if method == 'GET' and (m := re.fullmatch(rf'/repos/{REPO}/commits/([0-9a-f]{{40}})/statuses', u.path)):
            got, h = self.page(self.statuses.get(m.group(1), []), url, self.statuses_page)
            return 200, h, json.dumps(got).encode()
        if method == 'POST' and (m := re.fullmatch(rf'/repos/{REPO}/actions/runs/([0-9]+)/(cancel|force-cancel)', u.path)):
            if self.on_cancel:
                self.on_cancel(self)
            return (self.cancel_code, {}, b'{}') if self.cancel_code != 202 else (202, {}, b'{}')
        if method == 'POST' and (m := re.fullmatch(rf'/repos/{REPO}/statuses/([0-9a-f]{{40}})', u.path)):
            self.statuses.setdefault(m.group(1), []).insert(0, doc)
            return 201, {}, json.dumps(doc).encode()
        return 404, {}, b''

    def posts(self):
        return [(u.rsplit('/', 1)[-1] if '/actions/runs/' in u else 'status', d) for m, u, d in self.calls if m == 'POST']

    def gets(self, part):
        return [u for m, u, _ in self.calls if m == 'GET' and part in u]


def sweep_(hub, *args, env=None, summary=False):
    out, err = io.StringIO(), io.StringIO()
    with tempfile.TemporaryDirectory() as d:
        e = dict(ENV if env is None else env)
        if summary:
            e['GITHUB_STEP_SUMMARY'] = str(pathlib.Path(d) / 'summary.md')
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = sweep.main(['--workflow', 'ci.yml', '--context', 'p/ci', *args], e, hub, now=NOW, pause=0)
        text = pathlib.Path(e['GITHUB_STEP_SUMMARY']).read_text() if summary else ''
    return code, out.getvalue() + err.getvalue(), text


class Decide(unittest.TestCase):
    """Step 4: the newest status of the context decides."""

    def test_no_status_yet_cancels_and_posts_error_on_the_swept_run(self):
        hub = Hub([run()], {7: [job(), job('ci / plan', status='completed')]})
        code, out, _ = sweep_(hub)
        self.assertEqual(code, 0, out)
        self.assertEqual(hub.posts(), [('cancel', None), ('status', {
            'state': 'error', 'context': 'p/ci', 'target_url': f'{SERVER}/{REPO}/actions/runs/7',
            'description': 'queued past 60 min: part gpu-a on ci-gpu'})])
        self.assertIn('run 7: cancelled, error posted', out)

    def test_its_own_pending_is_covered(self):
        hub = Hub([run(event='pull_request')], {7: [job()]}, {SHA: [status('pending'), status('pending', rid=3)]})
        self.assertEqual(sweep_(hub)[0], 0)
        self.assertEqual([k for k, _ in hub.posts()], ['cancel', 'status'])

    def test_its_own_red_stays_the_final_status(self):
        hub = Hub([run()], {7: [job()]}, {SHA: [status('failure', description='red at part x; the other parts run on')]})
        code, out, _ = sweep_(hub)
        self.assertEqual((code, hub.posts()), (0, [('cancel', None)]))
        self.assertIn('its own failure stays', out)
        hub = Hub([run()], {7: [job()]}, {SHA: [status('error', description='infrastructure: gpu-a lost')]})
        self.assertEqual((sweep_(hub)[0], hub.posts()), (0, [('cancel', None)]))  # not a sweep's: no force

    def test_its_own_earlier_sweep_error_force_cancels_and_posts_nothing(self):
        hub = Hub([run()], {7: [job()]}, {SHA: [status('error', description='queued past 60 min: part gpu-a on ci-gpu')]})
        self.assertEqual(sweep_(hub)[0], 0)
        self.assertEqual(hub.posts(), [('force-cancel', None)])

    def test_another_runs_status_is_never_posted_over(self):
        for st in (status('pending', rid=8), status('success', rid=8), status('error', rid=6)):
            hub = Hub([run()], {7: [job()]}, {SHA: [st, status('pending')]})
            code, out, _ = sweep_(hub)
            self.assertEqual((code, hub.posts()), (0, []), st)
            self.assertIn("run 7: left alone (queued past 60 min: part gpu-a on ci-gpu; another run's status", out)

    def test_a_dispatch_run_is_left_alone_unless_its_head_is_known(self):
        hub = Hub([run(event='workflow_dispatch')], {7: [job()]}, {SHA: [status('pending', rid=4)]})
        code, out, _ = sweep_(hub)
        self.assertEqual((code, hub.posts()), (0, []))
        self.assertIn('its head is not known', out)
        for event in ('schedule', 'workflow_dispatch'):  # a status of this run on its head_sha: the head is known
            hub = Hub([run(event=event)], {7: [job()]}, {SHA: [status('pending', context='p/other')]})
            self.assertEqual(sweep_(hub)[0], 0)
            self.assertEqual([k for k, _ in hub.posts()], ['cancel', 'status'], event)

    def test_a_status_of_another_run_with_a_longer_id_is_not_this_runs(self):
        hub = Hub([run(event='workflow_dispatch')], {7: [job()]}, {SHA: [status('pending', rid=17)]})
        self.assertEqual((sweep_(hub)[0], hub.posts()), (0, []))


class Stuck(unittest.TestCase):
    """Steps 1 and 2: which runs are read and which jobs are stuck."""

    def test_a_young_run_is_not_read(self):
        hub = Hub([run(created=59)], {7: [job(created=80)]})
        self.assertEqual(sweep_(hub)[0], 0)
        self.assertEqual(hub.gets('/jobs'), [])
        hub = Hub([run(created=61)], {7: [job(created=61)]})
        self.assertEqual(sweep_(hub)[0], 0)
        self.assertEqual(len(hub.gets('/jobs')), 1)

    def test_only_a_queued_job_past_the_bound_is_stuck(self):
        jobs = [job(status=s) for s in ('waiting', 'pending', 'requested', 'in_progress', 'completed')]
        jobs.append(job(created=59))  # a re-run's job, younger than its run
        hub = Hub([run()], {7: jobs})
        self.assertEqual((sweep_(hub)[0], hub.posts()), (0, []))
        hub = Hub([run()], {7: [job(created=30)]})
        self.assertEqual((sweep_(hub, '--minutes', '20')[0], len(hub.posts())), (0, 2))

    def test_both_lists_and_each_run_once_oldest_first(self):
        runs = [run(9, sha=SHA2, created=70, status='queued'), run(7, created=200)]
        hub = Hub(runs, {7: [job()], 9: [job()]})
        self.assertEqual(sweep_(hub)[0], 0)
        self.assertEqual([u for m, u, _ in hub.calls if m == 'POST' and 'cancel' in u],
                         [f'{API}/repos/{REPO}/actions/runs/7/cancel', f'{API}/repos/{REPO}/actions/runs/9/cancel'])
        self.assertEqual(len(hub.gets('/actions/workflows/')), 2)

    def test_more_than_a_page_of_runs_is_reported(self):
        hub = Hub([run()], {7: [job()]}, total=250)
        code, out, text = sweep_(hub, summary=True)
        self.assertEqual(code, 0)
        self.assertIn('250 in_progress runs: only the newest 1 read', out)
        self.assertIn('250 in_progress runs', text)

    def test_the_jobs_are_paged_by_link_to_their_bound(self):
        jobs = [job(status='completed') for _ in range(9)] + [job('ci / part late / first')]
        hub = Hub([run()], {7: jobs}, jobs_page=2)
        self.assertEqual(sweep_(hub)[0], 0)
        self.assertEqual(len(hub.gets('/jobs')), 5)
        self.assertEqual(hub.posts()[-1][1]['description'], 'queued past 60 min: part late on ci-gpu')
        hub = Hub([run()], {7: [job(status='completed')] * 11 + [job()]}, jobs_page=1)  # 12 pages: past the bound
        code, out, _ = sweep_(hub)
        self.assertEqual((code, hub.posts()), (1, []))
        self.assertIn('more than 11 pages', out)

    def test_a_next_page_off_the_api_host_is_refused(self):
        hub = Hub([run()], {7: [job(status='completed'), job()]}, jobs_page=1, link_host='https://elsewhere.example')
        code, out, _ = sweep_(hub)
        self.assertEqual((code, hub.posts()), (1, []))
        self.assertIn('a next page off', out)
        self.assertFalse([u for _, u, _ in hub.calls if 'elsewhere' in u])

    def test_the_statuses_are_read_to_their_bound(self):
        old = [status('pending', rid=5, context=f'x/{i}') for i in range(3)]
        hub = Hub([run()], {7: [job()]}, {SHA: old + [status('pending', rid=8)]}, statuses_page=1)
        self.assertEqual(sweep_(hub)[0], 1)  # a fourth page: not read, the run not touched
        self.assertEqual(hub.posts(), [])


class Act(unittest.TestCase):
    """Steps 5 and 6: the cancel, the re-read before the post, the bounds."""

    def test_a_run_that_had_completed_gets_nothing_more(self):
        hub = Hub([run()], {7: [job()]}, cancel_code=409)
        code, out, _ = sweep_(hub)
        self.assertEqual((code, hub.posts()), (0, [('cancel', None)]))
        self.assertIn('not cancelled', out)

    def test_a_status_that_came_in_between_is_not_posted_over(self):
        for between in (status('failure'), status('pending', rid=8), status('success')):
            hub = Hub([run()], {7: [job()]})
            hub.on_cancel = lambda h, st=between: h.statuses.setdefault(SHA, []).insert(0, st)
            code, out, _ = sweep_(hub)
            self.assertEqual((code, hub.posts()), (0, [('cancel', None)]), between)
            self.assertIn('not posted: a status came in between', out)
        hub = Hub([run()], {7: [job()]})
        hub.on_cancel = lambda h: h.statuses.setdefault(SHA, []).insert(0, status('pending'))
        self.assertEqual([k for k, _ in (sweep_(hub), hub.posts())[1]], ['cancel', 'status'])

    def test_the_request_bound_stops_the_sweep(self):
        runs = [run(i, sha=f"{i:040x}", created=100 + i) for i in range(1, 6)]
        hub = Hub(runs, {i: [job()] for i in range(1, 6)})
        code, out, _ = sweep_(hub, '--max-requests', '30')
        self.assertEqual(code, 1)
        self.assertIn('--max-requests', out)
        self.assertLessEqual(len(hub.calls), 30)
        # every cancel has its post: a run is acted on only with the calls for its whole action left
        kinds = [k for k, _ in hub.posts()]
        self.assertEqual(kinds, ['cancel', 'status'] * (len(kinds) // 2))
        self.assertTrue(kinds)
        hub = Hub([run()], {7: [job()]})
        code, out, _ = sweep_(hub, '--max-requests', '3')  # the listing and the jobs; the statuses are one too many
        self.assertEqual((code, len(hub.calls), hub.posts()), (1, 3, []))
        self.assertIn('more than 3 requests', out)
        hub = Hub([run()], {7: [job()]})
        code, _, _ = sweep_(hub, '--max-requests', str(2 + 1 + 1 + sweep.ACTION_CALLS - 1))
        self.assertEqual((code, hub.posts()), (1, []))

    def test_the_cancel_bound_stops_the_sweep(self):
        runs = [run(i, sha=f"{i:040x}", created=100 + i) for i in range(1, 5)]
        hub = Hub(runs, {i: [job()] for i in range(1, 5)})
        code, out, _ = sweep_(hub, '--max-cancels', '2')
        self.assertEqual(code, 1)
        self.assertEqual([u.rsplit('/', 2)[-2] for m, u, _ in hub.calls if m == 'POST' and 'cancel' in u], ['4', '3'])
        self.assertIn('--max-cancels', out)

    def test_a_5xx_is_tried_again_and_a_refusal_is_not(self):
        hub = Hub([run()], {7: [job()]}, fail=[502, 0])
        self.assertEqual(sweep_(hub)[0], 0)
        self.assertEqual(len(hub.gets('status=queued')), 2)
        hub = Hub([run()], {7: [job()]}, fail=[403])
        code, out, _ = sweep_(hub)
        self.assertEqual((code, len(hub.calls)), (1, 1))
        self.assertIn('HTTP 403', out)

    def test_a_failed_post_fails_the_sweep_and_the_others_still_run(self):
        hub = Hub([run(7, created=100), run(9, sha=SHA2)], {7: [job()], 9: [job()]})
        n = {'posts': 0}
        real = hub.__call__

        def send(method, url, headers, body):
            if method == 'POST' and '/statuses/' in url and not n['posts']:
                n['posts'] += 1
                return 422, {}, b''
            return real(method, url, headers, body)
        code, out, _ = sweep_(send)
        self.assertEqual(code, 1)
        self.assertIn('run 7: failed', out)
        self.assertIn('run 9: cancelled, error posted', out)


class Describe(unittest.TestCase):
    """The description is built from validated pieces only."""

    def test_part_plan_several_and_both_run_shapes(self):
        d = sweep.describe
        self.assertEqual(d(60, [job('ci / plan', labels=['self-hosted', 'ci-light'])]), 'queued past 60 min: plan on ci-light')
        self.assertEqual(d(30, [job('ci / part a.b_c-1 / retry', labels=['self-hosted', 'ci-cpu']), job()]),
                         'queued past 30 min: 2 jobs, first part a.b_c-1 on ci-cpu')
        self.assertEqual(d(60, [job('part (x) / where', labels=['ci-gpu2'])]), 'queued past 60 min: part x on ci-gpu2')
        self.assertEqual(d(60, [job('pytest-retry', labels=['ci-cpu', 'h100-cpu'])]), 'queued past 60 min: part pytest on ci-cpu+h100-cpu')
        self.assertEqual(d(60, [job('ci / verdict')]), 'queued past 60 min: a job on ci-gpu')

    def test_a_hostile_name_or_label_never_reaches_the_status(self):
        d = sweep.describe
        for name in ('ci / part $(curl x) / first', 'ci / part ' + 'x' * 65 + ' / first', 'ci / part -x / first',
                     'ci / part a\nb / first', 'ci / part <a href> / red'):
            self.assertEqual(d(60, [job(name, labels=['ci-cpu'])]), 'queued past 60 min: a job on ci-cpu', name)
        for labels in (['ci-cpu', 'Bad Label'], ['ci-cpu', 'x;rm'], ['self-hosted'], [], ['ci-cpu', 7], ['-x'],
                       ['a' * 65]):
            self.assertEqual(d(60, [job(labels=labels)]), 'queued past 60 min: part gpu-a', labels)
        many = [f'l{i:02d}-' + 'x' * 40 for i in range(4)]
        got = d(60, [job('ci / part ' + 'p' * 64 + ' / first', labels=many)])
        self.assertEqual(got, 'queued past 60 min: part ' + 'p' * 64)  # the labels do not fit: left out
        self.assertLessEqual(len(got), 140)

    def test_a_hostile_job_never_reaches_the_summary_or_a_line(self):
        hub = Hub([run(event='push')], {7: [job('ci / part `x`|y / first', labels=['ci|cpu'])]})
        code, out, text = sweep_(hub, summary=True)
        self.assertEqual(code, 0)
        self.assertNotIn('`x`', out + text)
        self.assertNotIn('ci|cpu', out + text)
        self.assertIn('| 7 | push | `aaaaaaaaaaaa` | cancelled, error posted | queued past 60 min: a job |', text)
        hub = Hub([run(event='push`|<b>')], {7: [job()]}, {SHA: [status('pending')]})
        code, out, text = sweep_(hub, summary=True)
        self.assertEqual((code, hub.posts()[0][0]), (0, 'cancel'))  # a status of this run: its head is known
        self.assertIn('| 7 | - | `aaaaaaaaaaaa` |', text)


class Arguments(unittest.TestCase):
    def test_bad_arguments_and_no_token_exit_2(self):
        hub = Hub()
        for args in (['--minutes', '4'], ['--minutes', '1441'], ['--minutes', 'x'], ['--minutes', '60.5'],
                     ['--max-requests', '0'], ['--max-cancels', '-1']):
            with self.assertRaises(SystemExit) as c, contextlib.redirect_stderr(io.StringIO()):
                sweep_(hub, *args)
            self.assertEqual(c.exception.code, 2, args)
        for wf, ctx in (('../ci.yml', 'p/ci'), ('ci.txt', 'p/ci'), ('ci.yml', 'p ci'), ('ci.yml', '')):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(sweep.main(['--workflow', wf, '--context', ctx], ENV, hub, now=NOW), 2, (wf, ctx))
        for drop in ('GITHUB_TOKEN', 'GITHUB_REPOSITORY'):
            self.assertEqual(sweep_(hub, env={k: v for k, v in ENV.items() if k != drop})[0], 2, drop)
        self.assertEqual(hub.calls, [])
        self.assertEqual(sweep_(hub, '--minutes', '5', '--minutes', '1440')[0], 0)

    def test_ci_run_dispatches_sweep_without_an_inventory(self):
        with mock.patch.dict(os.environ, {'CI_RUNNERS_ETC': '/nonexistent'}, clear=True), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(cli.main(['sweep', '--workflow', 'ci.yml', '--context', 'p/ci']), 2)
        self.assertIn('GITHUB_TOKEN is missing', err.getvalue())

    def test_the_summary_without_a_stuck_run(self):
        code, _, text = sweep_(Hub([run()], {7: [job(status='completed')]}), summary=True)
        self.assertEqual(code, 0)
        self.assertIn('No run has a job queued past the bound.', text)


if __name__ == '__main__':
    unittest.main()
