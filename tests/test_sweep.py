"""``ci-run sweep`` (design §4.10, decision 19 item 9): a run whose job has queued at GitHub
past the bound gets ``error`` on its context, then is cancelled, never over another run's
status, against a fake GitHub."""
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
from tests import yamlsub

ROOT = pathlib.Path(__file__).resolve().parents[1]
API = 'https://api.example'
SERVER = 'https://github.example'
REPO = 'o/r'
TOKEN = 't0k'
NOW = datetime.datetime(2026, 11, 13, 12, 0, tzinfo=datetime.timezone.utc).timestamp()
SHA, SHA2 = 'a' * 40, 'b' * 40
ENV = {'GITHUB_REPOSITORY': REPO, 'GITHUB_TOKEN': TOKEN, 'GITHUB_API_URL': API, 'GITHUB_SERVER_URL': SERVER,
       'GITHUB_RUN_ID': '999'}
EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)


def ago(minutes: float) -> str:
    return datetime.datetime.fromtimestamp(NOW - minutes * 60, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def run(rid=7, event='push', sha=SHA, created=90, status='in_progress', started=None):
    r = {'id': rid, 'event': event, 'head_sha': sha, 'created_at': ago(created), 'status': status,
         'html_url': f'{SERVER}/{REPO}/actions/runs/{rid}'}
    if started is not None:
        r['run_started_at'] = ago(started)
    return r


def job(name='ci / part gpu-a / first', status='queued', created=80, labels=('self-hosted', 'ci-gpu')):
    return {'name': name, 'status': status, 'created_at': ago(created), 'labels': list(labels)}


def status(state, rid=7, context='p/ci', description='queued on the runners', created=5, by='github-actions[bot]'):
    return {'state': state, 'context': context, 'description': description, 'created_at': ago(created),
            'target_url': f'{SERVER}/{REPO}/actions/runs/{rid}', 'creator': {'login': by}}


SWEPT = 'queued past 60 min: part gpu-a on ci-gpu'


class Hub:
    """A fake GitHub: the workflow's runs (by status and ``created``), each run's jobs and each
    head's statuses (newest first), paged with ``Link`` headers; posts and cancels recorded."""

    def __init__(self, runs=(), jobs=None, statuses=None, jobs_page=100, statuses_page=100, total=None,
                 cancel_code=202, link_host='', fail=None, tick=0.0, remaining=None):
        self.runs = list(runs)
        self.jobs = jobs or {}
        self.statuses = {k: list(v) for k, v in (statuses or {}).items()}
        self.jobs_page, self.statuses_page, self.total = jobs_page, statuses_page, total
        self.cancel_code = cancel_code if isinstance(cancel_code, dict) else {'cancel': cancel_code,
                                                                                'force-cancel': cancel_code}
        self.link_host, self.fail = link_host, fail or []
        self.calls: list[tuple[str, str, dict | None]] = []
        self.t, self.tick, self.remaining = 0.0, tick, remaining

    def clock(self):
        return self.t

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
        self.t += self.tick
        code, h, raw = self.answer(method, url, headers, body)
        if self.remaining is not None:
            h = {**h, 'X-RateLimit-Remaining': str(self.remaining(len(self.calls)))}
        return code, h, raw

    def answer(self, method, url, headers, body):
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
            op, when = re.fullmatch(r'(<|>=)([0-9T:Z-]+)', q['created'][0]).groups()
            cut = datetime.datetime.fromisoformat(when.replace('Z', '+00:00')).timestamp()
            rows = [r for r in self.runs if r['status'] == q['status'][0] and (
                (sweep.ts(r['created_at']) < cut) == (op == '<'))]
            rows.sort(key=lambda r: r['created_at'], reverse=True)  # GitHub's order: newest first
            return 200, {}, json.dumps({'total_count': self.total or len(rows), 'workflow_runs': rows[:100]}).encode()
        if method == 'GET' and (m := re.fullmatch(rf'/repos/{REPO}/actions/runs/([0-9]+)/jobs', u.path)):
            assert q['filter'] == ['latest']
            rows = self.jobs.get(int(m.group(1)), [])
            got, h = self.page(rows, url, self.jobs_page)
            return 200, h, json.dumps({'total_count': len(rows), 'jobs': got}).encode()
        if method == 'GET' and (m := re.fullmatch(rf'/repos/{REPO}/commits/([0-9a-f]{{40}})/statuses', u.path)):
            size = min(self.statuses_page, int(q['per_page'][0]))
            got, h = self.page(self.statuses.get(m.group(1), []), url, size)
            return 200, h, json.dumps(got).encode()
        if method == 'POST' and (m := re.fullmatch(rf'/repos/{REPO}/actions/runs/([0-9]+)/(cancel|force-cancel)', u.path)):
            code = self.cancel_code[m.group(2)]
            return code, {}, (b'{}' if code in (202, 409) else b'')
        if method == 'POST' and (m := re.fullmatch(rf'/repos/{REPO}/statuses/([0-9a-f]{{40}})', u.path)):
            self.statuses.setdefault(m.group(1), []).insert(0, {**doc, 'created_at': ago(0),
                                                                 'creator': {'login': 'github-actions[bot]'}})
            return 201, {}, json.dumps(doc).encode()
        return 404, {}, b''

    def posts(self):
        return [(u.rsplit('/', 1)[-1] if '/actions/runs/' in u else 'status', d) for m, u, d in self.calls if m == 'POST']

    def kinds(self):
        return [k for k, _ in self.posts()]

    def gets(self, part):
        return [u for m, u, _ in self.calls if m == 'GET' and part in u]


def sweep_(hub, *args, env=None, summary=False):
    out, err = io.StringIO(), io.StringIO()
    with tempfile.TemporaryDirectory() as d:
        e = dict(ENV if env is None else env)
        if summary:
            e['GITHUB_STEP_SUMMARY'] = str(pathlib.Path(d) / 'summary.md')
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = sweep.main(['--workflow', 'ci.yml', '--context', 'p/ci', *args], e, hub, now=NOW, pause=0,
                              clock=getattr(hub, 'clock', lambda: 0.0))
        text = pathlib.Path(e['GITHUB_STEP_SUMMARY']).read_text() if summary else ''
    return code, out.getvalue() + err.getvalue(), text


class Decide(unittest.TestCase):
    """Step 4: a newer run on the head, then the bot's newest status of the context, decide."""

    def test_no_status_yet_posts_error_on_the_swept_run_then_cancels(self):
        hub = Hub([run()], {7: [job(), job('ci / plan', status='completed')]})
        code, out, _ = sweep_(hub)
        self.assertEqual(code, 0, out)
        self.assertEqual(hub.posts(), [('status', {
            'state': 'error', 'context': 'p/ci', 'target_url': f'{SERVER}/{REPO}/actions/runs/7',
            'description': SWEPT}), ('cancel', None)])
        self.assertIn('run 7: error posted, cancelled', out)

    def test_its_own_pending_is_covered(self):
        hub = Hub([run(event='pull_request')], {7: [job()]}, {SHA: [status('pending'), status('pending', rid=3)]})
        self.assertEqual(sweep_(hub)[0], 0)
        self.assertEqual(hub.kinds(), ['status', 'cancel'])

    def test_its_own_red_stays_the_final_status(self):
        hub = Hub([run()], {7: [job()]}, {SHA: [status('failure', description='red at part x; the other parts run on')]})
        code, out, _ = sweep_(hub)
        self.assertEqual((code, hub.kinds()), (0, ['cancel']))
        self.assertIn('its own failure stays', out)
        hub = Hub([run()], {7: [job()]}, {SHA: [status('error', description='infrastructure: gpu-a lost')]})
        self.assertEqual((sweep_(hub)[0], hub.kinds()), (0, ['cancel']))  # not a sweep's: no force

    def test_its_own_earlier_sweep_error_force_cancels_and_posts_nothing(self):
        hub = Hub([run()], {7: [job()]}, {SHA: [status('error', description=SWEPT)]})
        self.assertEqual(sweep_(hub)[0], 0)
        self.assertEqual(hub.kinds(), ['force-cancel'])

    def test_another_runs_status_is_never_posted_over_and_the_run_is_cancelled(self):
        for st in (status('pending', rid=8), status('success', rid=8), status('error', rid=6)):
            hub = Hub([run()], {7: [job()]}, {SHA: [st, status('pending')]})
            code, out, _ = sweep_(hub)
            self.assertEqual((code, hub.kinds()), (0, ['cancel']), st)
            self.assertIn(f"run 7: cancelled ({SWEPT}; another run's status is the newest)", out)

    def test_a_newer_run_on_the_head_gets_the_context_and_the_old_one_is_cancelled_only(self):
        for newer in (run(12, created=10, status='queued'), run(12, created=70), run(12, created=10)):
            hub = Hub([run(), newer], {7: [job()], 12: [job(status='in_progress')]})
            code, out, _ = sweep_(hub)
            self.assertEqual((code, hub.kinds()), (0, ['cancel']), newer)
            self.assertIn('a newer run on its head owns the context', out)
        hub = Hub([run(), run(3, created=200, sha=SHA), run(12, created=10, sha=SHA2)], {7: [job()], 3: []})
        self.assertEqual(sweep_(hub)[0], 0)  # an older run on the head, or a newer one on another
        self.assertEqual(hub.kinds(), ['status', 'cancel'])
        hub = Hub([run(event='workflow_dispatch'), run(12, created=10)], {7: [job()]})
        self.assertEqual((sweep_(hub)[0], hub.kinds()), (0, []))  # a head not known: not even cancelled

    def test_a_dispatch_run_is_left_alone_unless_its_head_is_known(self):
        hub = Hub([run(event='workflow_dispatch')], {7: [job()]}, {SHA: [status('pending', rid=4)]})
        code, out, _ = sweep_(hub)
        self.assertEqual((code, hub.posts()), (0, []))
        self.assertIn('its head is not known', out)
        for event in ('schedule', 'workflow_dispatch'):  # a status of this run on its head_sha: the head is known
            hub = Hub([run(event=event)], {7: [job()]}, {SHA: [status('pending', context='p/other')]})
            self.assertEqual(sweep_(hub)[0], 0)
            self.assertEqual(hub.kinds(), ['status', 'cancel'], event)

    def test_a_status_of_another_run_with_a_longer_id_is_not_this_runs(self):
        hub = Hub([run(event='workflow_dispatch')], {7: [job()]}, {SHA: [status('pending', rid=17)]})
        self.assertEqual((sweep_(hub)[0], hub.posts()), (0, []))

    def test_only_the_bots_statuses_count(self):
        someone = 'a-person'
        hub = Hub([run()], {7: [job()]}, {SHA: [status('success', rid=8, by=someone), status('pending')]})
        self.assertEqual((sweep_(hub)[0], hub.kinds()), (0, ['status', 'cancel']))  # another's success: not the bot's
        hub = Hub([run()], {7: [job()]}, {SHA: [status('error', description=SWEPT, by=someone)]})
        self.assertEqual((sweep_(hub)[0], hub.kinds()), (0, ['status', 'cancel']))  # a forged sweep error: no force
        hub = Hub([run(event='workflow_dispatch')], {7: [job()]}, {SHA: [status('pending', by=someone)]})
        self.assertEqual((sweep_(hub)[0], hub.kinds()), (0, []))  # nor does it make the head known
        hub = Hub([run()], {7: [job()]}, {SHA: [{**status('pending', rid=8), 'creator': 'github-actions[bot]'}]})
        self.assertEqual((sweep_(hub)[0], hub.kinds()), (0, ['status', 'cancel']))  # a creator that is no object

    def test_an_earlier_attempts_status_is_none_of_this_attempts(self):
        stale = status('error', description=SWEPT, created=80)
        hub = Hub([run(started=70)], {7: [job(created=65)]}, {SHA: [stale]})
        self.assertEqual((sweep_(hub)[0], hub.kinds()), (0, ['status', 'cancel']))  # posted, not force-cancelled
        hub = Hub([run(started=70)], {7: [job(created=65)]}, {SHA: [status('failure', created=80)]})
        self.assertEqual((sweep_(hub)[0], hub.kinds()), (0, ['status', 'cancel']))
        hub = Hub([run(started=70)], {7: [job(created=65)]}, {SHA: [status('error', description=SWEPT, created=20)]})
        self.assertEqual((sweep_(hub)[0], hub.kinds()), (0, ['force-cancel']))  # this attempt's


class Stuck(unittest.TestCase):
    """Steps 1 and 2: which runs are read and which jobs are stuck."""

    def test_a_young_run_is_not_read(self):
        hub = Hub([run(created=59)], {7: [job(created=80)]})
        self.assertEqual(sweep_(hub)[0], 0)
        self.assertEqual(hub.gets('/jobs'), [])
        hub = Hub([run(created=61)], {7: [job(created=61)]})
        self.assertEqual(sweep_(hub)[0], 0)
        self.assertEqual(len(hub.gets('/jobs')), 1)

    def test_the_old_runs_are_listed_apart_from_the_young(self):
        young = [run(100 + i, sha=f'{i:040x}', created=1 + i * 0.1) for i in range(100)]
        hub = Hub([run(), *young], {7: [job()]})
        self.assertEqual(sweep_(hub)[0], 0)
        self.assertEqual(hub.kinds(), ['status', 'cancel'])  # a hundred young runs do not crowd it out
        lists = hub.gets('/actions/workflows/')
        self.assertEqual(len(lists), 4)
        when = urllib.parse.quote(ago(60), safe='')
        self.assertIn(f'status=in_progress&created=%3C{when}&per_page=100', lists[2])
        self.assertIn(f'status=in_progress&created=%3E%3D{when}&per_page=100', lists[3])

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

    def test_more_than_a_page_of_runs_is_reported(self):
        hub = Hub([run()], {7: [job()]}, total=250)
        code, out, text = sweep_(hub, summary=True)
        self.assertEqual(code, 0)
        self.assertIn(f'250 in_progress runs created < {ago(60)}: only the newest 1 read', out)
        self.assertIn('250 in_progress runs', text)

    def test_the_jobs_are_paged_by_link_to_their_bound(self):
        jobs = [job(status='completed') for _ in range(9)] + [job('ci / part late / first')]
        hub = Hub([run()], {7: jobs}, jobs_page=2)
        self.assertEqual(sweep_(hub)[0], 0)
        self.assertEqual(len(hub.gets('/jobs')), 5)
        self.assertEqual(hub.posts()[0][1]['description'], 'queued past 60 min: part late on ci-gpu')
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

    def test_the_statuses_first_page_only(self):
        hub = Hub([run()], {7: [job()]}, {SHA: [status('pending', rid=5, context='x/0'), status('success', rid=8)]},
                  statuses_page=1)
        self.assertEqual(sweep_(hub)[0], 0)
        self.assertEqual(len(hub.gets('/statuses')), 1)
        self.assertEqual(hub.kinds(), ['status', 'cancel'])  # the context not on it: none
        hub = Hub([run()], {7: [job()]}, {SHA: [status('pending', rid=5, context='x/0'), status('success', rid=8)]})
        self.assertEqual((sweep_(hub)[0], hub.kinds()), (0, ['cancel']))  # a page of 100: another run's newest


class Act(unittest.TestCase):
    """Steps 5 and 6: the post before the cancel, the force-cancel, the bounds."""

    def test_a_cancel_answered_409_is_followed_by_a_force_cancel(self):
        hub = Hub([run()], {7: [job()]}, cancel_code=409)
        code, out, _ = sweep_(hub)
        self.assertEqual((code, hub.kinds()), (0, ['status', 'cancel', 'force-cancel']))
        self.assertIn('error posted, not cancelled (it had completed)', out)
        hub = Hub([run()], {7: [job()]}, {SHA: [status('failure')]}, cancel_code={'cancel': 409, 'force-cancel': 202})
        code, out, _ = sweep_(hub)
        self.assertEqual((code, hub.kinds()), (0, ['cancel', 'force-cancel']))
        self.assertIn('run 7: force-cancelled', out)

    def test_a_sweep_stopped_after_its_post_is_finished_by_the_next(self):
        hub = Hub([run()], {7: [job()]}, cancel_code=503)
        code, out, _ = sweep_(hub)
        self.assertEqual((code, hub.kinds()), (1, ['status', 'cancel', 'cancel', 'cancel']))
        self.assertIn('run 7: failed', out)
        hub.calls, hub.cancel_code = [], {'cancel': 202, 'force-cancel': 202}
        self.assertEqual((sweep_(hub)[0], hub.kinds()), (0, ['force-cancel']))  # its own sweep error: no second post

    def test_the_request_bound_stops_the_sweep(self):
        runs = [run(i, sha=f'{i:040x}', created=100 + i) for i in range(1, 6)]
        hub = Hub(runs, {i: [job()] for i in range(1, 6)})
        code, out, _ = sweep_(hub, '--max-requests', '30')
        self.assertEqual(code, 1)
        self.assertIn('--max-requests', out)
        self.assertLessEqual(len(hub.calls), 30)
        self.assertEqual(hub.kinds(), ['status', 'cancel'] * 4)  # a run is acted on only with its calls left
        hub = Hub([run()], {7: [job()]})
        code, out, _ = sweep_(hub, '--max-requests', '3')  # the listing alone is one too many
        self.assertEqual((code, len(hub.calls), hub.posts()), (1, 3, []))
        self.assertIn('more than 3 requests', out)
        hub = Hub([run()], {7: [job()]})
        code, _, _ = sweep_(hub, '--max-requests', str(4 + 1 + 1 + sweep.ACTION_CALLS - 1))
        self.assertEqual((code, hub.posts()), (1, []))
        self.assertEqual(sweep.MAX_REQUESTS, 50)

    def test_the_deadline_stops_the_sweep_before_a_post(self):
        timeout = yamlsub.load((ROOT / '.github/workflows/sweep.yml').read_text())['jobs']['sweep']['timeout-minutes']
        self.assertLessEqual(sweep.DEADLINE, (timeout - 2) * 60)  # room for the runner's own start and end
        hub = Hub([run()], {7: [job()]}, tick=100)  # the statuses' call would start past 480 s
        code, out, _ = sweep_(hub)
        self.assertEqual((code, hub.posts()), (1, []))
        self.assertIn("past the sweep's deadline", out)
        hub = Hub([run()], {7: [job()]}, tick=sweep.DEADLINE / 5.9)  # past it just before the post
        code, out, _ = sweep_(hub)
        self.assertEqual((code, hub.posts(), len(hub.gets('/statuses'))), (1, [], 1))
        hub = Hub([run()], {7: [job()]}, tick=sweep.DEADLINE / 8)
        self.assertEqual((sweep_(hub)[0], hub.kinds()), (0, ['status', 'cancel']))

    def test_the_rate_floor_stops_the_sweep(self):
        hub = Hub([run(i, sha=f'{i:040x}', created=100 + i) for i in range(1, 4)], {i: [job()] for i in range(1, 4)},
                  remaining=lambda n: 99 if n >= 8 else 500)
        code, out, _ = sweep_(hub)
        self.assertEqual(code, 1)
        self.assertIn('99 API calls left this hour, under 100', out)
        self.assertEqual(hub.kinds(), ['status', 'cancel'])  # the first run's, and no call after the floor
        self.assertEqual(len(hub.calls), 8)
        hub = Hub([run()], {7: [job()]}, remaining=lambda n: 100)
        self.assertEqual((sweep_(hub)[0], hub.kinds()), (0, ['status', 'cancel']))

    def test_the_cancel_bound_stops_the_sweep(self):
        runs = [run(i, sha=f'{i:040x}', created=100 + i) for i in range(1, 5)]
        hub = Hub(runs, {i: [job()] for i in range(1, 5)})
        code, out, _ = sweep_(hub, '--max-cancels', '2')
        self.assertEqual(code, 1)
        self.assertEqual([u.rsplit('/', 2)[-2] for m, u, _ in hub.calls if m == 'POST' and 'cancel' in u], ['4', '3'])
        self.assertIn('--max-cancels', out)

    def test_a_5xx_is_tried_again_and_a_refusal_is_not(self):
        hub = Hub([run()], {7: [job()]}, fail=[502, 0])
        self.assertEqual(sweep_(hub)[0], 0)
        self.assertEqual(len(hub.gets('status=queued')), 3)
        hub = Hub([run()], {7: [job()]}, fail=[403])
        code, out, _ = sweep_(hub)
        self.assertEqual((code, len(hub.calls)), (1, 1))
        self.assertIn('HTTP 403', out)

    def test_a_failed_post_fails_the_sweep_and_the_others_still_run(self):
        hub = Hub([run(7, created=100), run(9, sha=SHA2)], {7: [job()], 9: [job()]})
        n = {'posts': 0}

        def send(method, url, headers, body):
            if method == 'POST' and '/statuses/' in url and not n['posts']:
                n['posts'] += 1
                return 422, {}, b''
            return hub(method, url, headers, body)
        code, out, _ = sweep_(send)
        self.assertEqual(code, 1)
        self.assertIn('run 7: failed', out)
        self.assertNotIn('runs/7/cancel', ' '.join(u for _, u, _ in hub.calls))  # no cancel without its error
        self.assertIn('run 9: error posted, cancelled', out)


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
        self.assertIn('| 7 | push | `aaaaaaaaaaaa` | error posted, cancelled | queued past 60 min: a job |', text)
        hub = Hub([run(event='push`|<b>')], {7: [job()]}, {SHA: [status('pending')]})
        code, out, text = sweep_(hub, summary=True)
        self.assertEqual((code, hub.kinds()), (0, ['status', 'cancel']))  # a status of this run: its head is known
        self.assertIn('| 7 | - | `aaaaaaaaaaaa` |', text)

    def test_a_malformed_head_sha_is_a_dash(self):
        for sha in ('z' * 40, 'a' * 39 + '|', 'A' * 40):
            hub = Hub([run(sha=sha)], {7: [job()]})
            code, _, text = sweep_(hub, summary=True)
            self.assertEqual((code, hub.posts()), (0, []), sha)
            self.assertIn('| 7 | push | `-` | left alone |', text)
            self.assertNotIn(sha[:12], text)


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
