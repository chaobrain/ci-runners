"""``ci-run stats`` (design decision 16.8, RUN-10 R7): ci_runners/stats.py against a fake GitHub
that serves the synthetic batch ``tests/fixtures/stats/batch.json`` (8 runs: a composing workflow's
and the reusable workflow's, with retries, a salvage, a red, a re-run and a job never taken)."""
from __future__ import annotations

import datetime
import io
import json
import os
import pathlib
import re
import tempfile
import unittest
import unittest.mock
import urllib.parse
import zipfile

from ci_runners import cli, stats

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT / 'tests' / 'fixtures' / 'stats' / 'batch.json').read_text())
T0 = FIXTURE['t0']
NOW = T0 + 20000
TOKEN = 'secret-login'
BLOB = 'https://blob.example/'


def epoch(v):
    return datetime.datetime.fromisoformat(v.replace('Z', '+00:00')).timestamp()


def zipped(doc):
    b = io.BytesIO()
    with zipfile.ZipFile(b, 'w') as z:
        z.writestr('outcome.json', json.dumps(doc))
        z.writestr('summary.md', 'a summary\n')
    return b.getvalue()


class Hub:
    """A fake GitHub of the batch: small pages, ``Link`` headers (page numbers for runs and
    jobs, cursors for the activity), artifacts behind a redirect to a signed URL."""

    def __init__(self, runs_page=3, jobs_page=5, activity_page=2, activity_status=200, total=None, dup=False,
                 link_host=''):
        self.runs = {r['id']: r for r in FIXTURE['runs']}
        self.calls: list[tuple[str, bool]] = []
        self.runs_page, self.jobs_page, self.activity_page = runs_page, jobs_page, activity_page
        self.activity_status = activity_status
        self.total, self.dup, self.link_host = total, dup, link_host
        self.arts: dict[int, tuple[int, str, dict]] = {}
        n = 9000
        for rid, docs in FIXTURE['artifacts'].items():
            for name, doc in docs.items():
                n += 1
                self.arts[n] = (int(rid), name, doc)

    @staticmethod
    def _page(rows, url, size, cursor=False):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        start = int(q.get('after', ['0'])[0]) if cursor else (int(q.get('page', ['1'])[0]) - 1) * size
        got = rows[start:start + size]
        h = {}
        if start + size < len(rows):
            base = re.sub(r'&(page|after)=[^&]*', '', url)
            nxt = f'{base}&after={start + size}' if cursor else f'{base}&page={start // size + 2}'
            h['Link'] = f'<{nxt}>; rel="next", <{base}>; rel="first"'
        return got, h

    def __call__(self, url, headers):
        auth = 'Authorization' in headers
        self.calls.append((url, auth))
        if url.startswith(BLOB):
            assert not auth, 'the signed URL got the login'
            return 200, {}, zipped(self.arts[int(url[len(BLOB):])][2])
        assert headers.get('Authorization') == f'Bearer {TOKEN}'
        path = urllib.parse.urlsplit(url).path
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        m = re.fullmatch(r'/repos/([^/]+/[^/]+)/actions/runs', path)
        if m:
            since = epoch(q['created'][0][2:])
            rows = [self._run(r) for r in FIXTURE['runs'] if r['repo'] == m.group(1) and epoch(r['created_at']) >= since]
            if self.dup:  # a run created while the pages are read: the next page starts with the last one again
                rows = rows[:self.runs_page] + rows[self.runs_page - 1:]
            got, h = self._page(rows, url, self.runs_page)
            if self.link_host and 'Link' in h:
                h['Link'] = h['Link'].replace('https://api.example', self.link_host)
            return 200, h, json.dumps({'total_count': self.total or len(rows), 'workflow_runs': got}).encode()
        m = re.fullmatch(r'/repos/([^/]+/[^/]+)/actions/runs/([0-9]+)', path)
        if m:
            return (200, {}, json.dumps(self._run(self.runs[int(m.group(2))])).encode()) if int(m.group(2)) in self.runs \
                else (404, {}, b'')
        m = re.fullmatch(r'/repos/([^/]+/[^/]+)/actions/runs/([0-9]+)/jobs', path)
        if m:
            assert q.get('filter') == ['all'], 'every run attempt\'s jobs'
            got, h = self._page(self.runs[int(m.group(2))]['jobs'], url, self.jobs_page)
            return 200, h, json.dumps({'total_count': len(self.runs[int(m.group(2))]['jobs']), 'jobs': got}).encode()
        m = re.fullmatch(r'/repos/([^/]+/[^/]+)/activity', path)
        if m:
            if self.activity_status != 200:
                return self.activity_status, {}, b''
            rows = sorted(FIXTURE['pushes'].get(m.group(1), []), key=lambda p: p['timestamp'], reverse=True)
            got, h = self._page(rows, url, self.activity_page, cursor=True)
            return 200, h, json.dumps(got).encode()
        m = re.fullmatch(r'/repos/([^/]+/[^/]+)/actions/runs/([0-9]+)/artifacts', path)
        if m:
            got = [{'id': n, 'name': name, 'expired': False} for n, (rid, name, _) in self.arts.items()
                   if rid == int(m.group(2))]
            return 200, {}, json.dumps({'total_count': len(got), 'artifacts': got}).encode()
        m = re.fullmatch(r'/repos/[^/]+/[^/]+/actions/artifacts/([0-9]+)/zip', path)
        if m:
            return 302, {'Location': f'{BLOB}{m.group(1)}'}, b''
        return 404, {}, b''

    @staticmethod
    def _run(r):
        return {k: v for k, v in r.items() if k not in ('jobs', 'repo')}

    def paths(self, pattern):
        return [u for u, _ in self.calls if re.search(pattern, u)]


def run_stats(*args, hub=None, environ=None, now=NOW):
    hub = hub or Hub()
    out = io.StringIO()
    rc = stats.main(['--json', '--since', stats.iso(T0 - 60), *args], environ={'GH_TOKEN': TOKEN} if environ is None
                    else environ, send=hub, now=lambda: now, out=out, base='https://api.example')
    return rc, (json.loads(out.getvalue()) if rc == 0 else None), hub


REPOS = ['--repo', 'acme/web', '--repo', 'acme/lib']


def independent(fixture):
    """The delays counted straight from the jobs, without the module: per label and per
    repository, every job a runner took (not skipped, a start and a runner)."""
    per_label, per_repo, verdict = {}, {}, {}
    for r in fixture['runs']:
        for j in r['jobs']:
            if j['conclusion'] == 'skipped' or not j['started_at'] or not j['runner_name']:
                continue
            key = '+'.join(x for x in j['labels'] if x != 'self-hosted')
            q = epoch(j['started_at']) - epoch(j['created_at'])
            d = epoch(j['completed_at']) - epoch(j['started_at'])
            for bucket in (per_label.setdefault(key, ([], [])), per_repo.setdefault(r['repo'], ([], []))):
                bucket[0].append(q)
                bucket[1].append(d)
        pushed = [epoch(p['timestamp']) for p in fixture['pushes'][r['repo']] if p['after'] == r['head_sha']
                  and p['ref'] == 'refs/heads/' + r['head_branch'] and r['event'] != 'workflow_dispatch'
                  and 0 <= epoch(r['created_at']) - epoch(p['timestamp']) <= 600]
        if r['run_attempt'] > 1:  # a re-run: its own attempt, from its start
            start = epoch(r['run_started_at'])
        else:
            start = pushed[0] if pushed else epoch(r['created_at'])
        last = [j for j in r['jobs'] if j['name'].split(' / ')[-1] == 'verdict'
                and j['run_attempt'] == r['run_attempt']][0]
        verdict.setdefault(r['repo'], []).append(epoch(last['completed_at']) - start)
    return per_label, per_repo, verdict


def nearest(v, p):
    v = sorted(v)
    return v[int(round(p * (len(v) - 1)))]


class Acceptance(unittest.TestCase):
    """R7's acceptance in the repository: on the recorded batch of 8 runs the report's queue,
    run and verdict delays per label and per repository equal an independent count."""

    def setUp(self):
        self.rc, self.rep, self.hub = run_stats(*REPOS)
        self.assertEqual(self.rc, 0)

    def test_the_delays_per_label_and_per_repository_equal_an_independent_count(self):
        per_label, per_repo, verdict = independent(FIXTURE)
        self.assertEqual(set(self.rep['labels']), set(per_label))
        for k, (q, d) in per_label.items():
            for name, vals in (('queue', q), ('run', d)):
                got = self.rep['labels'][k][name]
                self.assertEqual((got['n'], got['p50'], got['p90'], got['max']),
                                 (len(vals), nearest(vals, .5), nearest(vals, .9), max(vals)), (k, name))
        for repo, (q, d) in per_repo.items():
            got = self.rep['repos'][repo]
            for name, vals in (('queue', q), ('run', d)):
                self.assertEqual((got[name]['n'], got[name]['p50'], got[name]['p90'], got[name]['max']),
                                 (len(vals), nearest(vals, .5), nearest(vals, .9), max(vals)), (repo, name))
            v = verdict[repo]
            self.assertEqual((got['verdict']['n'], got['verdict']['p50'], got['verdict']['p90'], got['verdict']['max']),
                             (len(v), nearest(v, .5), nearest(v, .9), max(v)), repo)

    def test_the_numbers_by_hand(self):
        cpu = self.rep['labels']['ci-cpu']['queue']  # [2, 2, 2, 3, 4, 5, 9, 9, 20, 29, 39, 60, 60]
        self.assertEqual((cpu['n'], cpu['p50'], cpu['p90'], cpu['max']), (13, 9, 60, 60))
        web, lib = self.rep['repos']['acme/web'], self.rep['repos']['acme/lib']
        # web: 101 from its push (533), 102 from its branch's push (435, not the other branch's at 990),
        # 103's push an hour before: its creation (330); 104's re-run: attempt 2 from its start (440)
        self.assertEqual(sorted(independent(FIXTURE)[2]['acme/web']), [330, 435, 440, 533])
        self.assertEqual((web['verdict']['n'], web['verdict']['p50'], web['verdict']['p90']), (4, 440, 533))
        self.assertEqual(web['verdict_from'], {'push': 2, 'created': 1, 'rerun': 1})
        # lib: a dispatch from its creation (330), a force push (655)
        self.assertEqual((lib['verdict']['n'], lib['verdict']['p50'], lib['verdict']['p90']), (4, 655, 1332))
        self.assertEqual(lib['verdict_from'], {'push': 3, 'created': 1, 'rerun': 0})
        # web: 530, 430, 330 and 104's attempt 2, 5440 - 5000, not 5440 - its creation
        self.assertEqual((web['wall']['n'], web['wall']['p50'], web['wall']['max']), (4, 440, 530))
        self.assertEqual(web['reruns'], 1)
        # lib: 330, 430, 1330, 650; 201's red job, skipped after its verdict, is not its end
        self.assertEqual((lib['wall']['n'], lib['wall']['p50'], lib['wall']['max']), (4, 650, 1330))
        self.assertEqual(lib['parts']['queue']['n'], 8)  # 5 first attempts and 3 retries, no locate, salvage or red
        self.assertEqual(web['parts']['queue']['n'], 11)  # the flat run's cpu, cpu-retry and gpu among them

    def test_both_shapes_count_their_retries_by_row(self):
        self.assertEqual(self.rep['repos']['acme/web']['retries'], {'fallback': 2, 'any': 1})  # 103's cpu-retry
        self.assertEqual(self.rep['repos']['acme/lib']['retries'], {'more-memory': 1, 'cool-card': 1, 'any': 1})

    def test_a_refusal_after_a_start_is_counted_by_its_head(self):
        self.assertEqual(self.rep['repos']['acme/web']['refusals'], {'busy': 1, 'drained': 1})
        self.assertEqual(self.rep['repos']['acme/lib']['refusals'], {})

    def test_a_job_no_runner_took_is_in_no_delay(self):
        web = self.rep['repos']['acme/web']
        self.assertEqual(web['unstarted'], 2)  # one without started_at, one with it but no runner
        taken = sum(1 for r in FIXTURE['runs'] if r['repo'] == 'acme/web' for j in r['jobs']
                    if j['conclusion'] != 'skipped' and j['started_at'] and j['runner_name'])
        self.assertEqual(web['jobs'], taken)
        self.assertEqual(web['queue']['n'], taken)

    def test_every_run_attempt_is_read_and_the_latest_verdict_counts(self):
        web = self.rep['repos']['acme/web']
        self.assertEqual(web['no_verdict'], 0)
        # run attempt 2's verdict from its own start (440): not attempt 1's from the push (1040), nor 5440 − 2990
        self.assertEqual(web['verdict']['max'], 533)
        self.assertEqual(web['verdict_from']['rerun'], 1)

    def test_the_text_report(self):
        out = io.StringIO()
        rc = stats.main(['--since', stats.iso(T0 - 60), *REPOS], environ={'GH_TOKEN': TOKEN}, send=Hub(),
                        now=lambda: NOW, out=out, base='https://api.example')
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn('acme/web: 4 runs (4 completed)', text)
        self.assertIn('retries by row: cool-card 1, more-memory 1, any 1', text)  # in 16.5's order
        self.assertRegex(text, r'ci-cpu queue\s+13\s+9s\s+60s\s+60s')
        self.assertIn('presence: not read (no --records)', text)


class Api(unittest.TestCase):
    def test_pagination_follows_link_and_reads_each_page_once(self):
        _, rep, hub = run_stats(*REPOS)
        self.assertEqual(len(hub.paths(r'/actions/runs\?')), 2 + 2)  # 4 runs a repository, 3 a page
        jobs = hub.paths(r'/jobs\?')
        want = sum(-(-len(r['jobs']) // 5) for r in FIXTURE['runs'])
        self.assertEqual(len(jobs), want)
        self.assertEqual(len(set(jobs)), want)
        self.assertGreater(len(hub.paths(r'/activity')), 2)  # a cursor page after the first
        self.assertEqual(rep['requests'], len(hub.calls))

    def test_the_activity_stops_at_the_window(self):
        hub = Hub(activity_page=1)
        run_stats('--repo', 'acme/lib', hub=hub)
        # lib's pushes newest first: 13995, 11998, 10999, 9000; T0 - 60 - 600 is reached at 9000
        self.assertEqual(len(hub.paths(r'/activity')), 4)
        hub = Hub(activity_page=1)
        run_stats('--repo', 'acme/lib', '--since', stats.iso(T0 + 12000), hub=hub)  # 10999 is the first before
        self.assertEqual(len(hub.paths(r'/activity')), 3)

    def test_a_download_follows_the_redirect_without_the_login(self):
        _, _, hub = run_stats(*REPOS)
        blobs = [(u, a) for u, a in hub.calls if u.startswith(BLOB)]
        self.assertEqual(len(blobs), 6)  # the retried parts' first attempts: 102, 103, 104 cpu, 202 a and g, 203 b
        self.assertFalse(any(a for _, a in blobs))
        self.assertEqual(len(hub.paths(r'/artifacts\?')), 5)  # only the runs with a retry

    def test_no_artifacts_infers_the_rows_from_the_jobs(self):
        _, rep, hub = run_stats(*REPOS, '--no-artifacts')
        self.assertFalse(hub.paths(r'artifacts'))
        # a fallback job; where ran; the flat run's first attempt shows nothing
        self.assertEqual(rep['repos']['acme/web']['retries'], {'fallback': 1, 'any': 1, 'unknown': 1})
        self.assertEqual(rep['repos']['acme/lib']['retries'], {'any': 1, 'unknown': 2})  # locate ran; oom, thermal
        self.assertEqual(rep['repos']['acme/web']['refusals'], {})

    def test_the_request_bound_refuses_the_report(self):
        rc, _, hub = run_stats(*REPOS, '--max-requests', '10')
        self.assertEqual(rc, 1)
        self.assertEqual(len(hub.calls), 10)

    def test_an_unreadable_activity_takes_the_run_creation(self):
        rc, rep, _ = run_stats(*REPOS, hub=Hub(activity_status=403))
        self.assertEqual(rc, 0)
        self.assertEqual(rep['repos']['acme/web']['verdict_from'], {'push': 0, 'created': 3, 'rerun': 1})
        self.assertFalse(rep['repos']['acme/web']['pushes_read'])

    def test_a_failed_read_of_the_jobs_is_status_1(self):
        hub = Hub()
        real = hub.__call__

        def send(url, headers):
            return (500, {}, b'') if '/jobs' in url else real(url, headers)
        api = stats.Api(TOKEN, 'https://api.example', send, pause=0)
        with self.assertRaises(stats.StatsError):
            stats.collect(api, 'acme/web', T0 - 60)
        self.assertEqual(sum(1 for u, _ in hub.calls if '/jobs' in u), 0)

    def test_a_refusal_is_not_retried_and_a_server_error_is(self):
        n = []

        def send(url, headers):
            n.append(url)
            return (404 if 'four' in url else 502), {}, b''
        api = stats.Api(TOKEN, 'https://api.example', send, pause=0)
        for path, calls in (('/four', 1), ('/five', 3)):
            n.clear()
            with self.assertRaises(stats.StatsError):
                api.one(path)
            self.assertEqual(len(n), calls)

    def test_more_runs_than_github_lists_are_refused(self):
        rc, _, hub = run_stats(*REPOS, hub=Hub(total=1001))
        self.assertEqual(rc, 1)
        self.assertEqual(len(hub.calls), 1)  # the first page says so
        self.assertEqual(run_stats(*REPOS, hub=Hub(total=1000))[0], 0)

    def test_a_run_on_two_pages_counts_once(self):
        _, rep, _ = run_stats(*REPOS, hub=Hub(dup=True))
        self.assertEqual(rep['repos']['acme/web']['runs'], 4)

    def test_a_next_page_off_github_is_refused(self):
        rc, _, hub = run_stats(*REPOS, hub=Hub(link_host='https://elsewhere.example'))
        self.assertEqual(rc, 1)
        self.assertFalse([u for u, _ in hub.calls if 'elsewhere' in u])

    def test_a_failed_activity_read_is_an_error_not_a_creation_time(self):
        with unittest.mock.patch.object(stats.time, 'sleep'):  # its three tries
            rc, _, _ = run_stats(*REPOS, hub=Hub(activity_status=502))
        self.assertEqual(rc, 1)
        self.assertEqual(run_stats(*REPOS, hub=Hub(activity_status=404))[0], 0)

    def test_listed_runs_only(self):
        rc, rep, hub = run_stats('--repo', 'acme/web', '--run', '102', '--run', '104')
        self.assertEqual(rc, 0)
        self.assertEqual(rep['repos']['acme/web']['runs'], 2)
        self.assertFalse(hub.paths(r'/actions/runs\?'))
        self.assertEqual(rep['repos']['acme/web']['retries'], {'fallback': 1, 'any': 1})


class Shapes(unittest.TestCase):
    def test_kinds(self):
        for name, want in (('ci / part cpu / first', ('ci / part cpu', 'first')),
                           ('part (cpu) / salvage', ('part (cpu)', 'salvage')),
                           ('part gpu / where', ('part gpu', 'where')),
                           ('ci / part a / red', ('ci / part a', 'red')),
                           ('cpu-retry', ('cpu', 'retry')), ('ci / cpu-fallback', ('ci / cpu', 'fallback')),
                           ('ci / verdict', ('', 'verdict')), ('verdict', ('', 'verdict')),
                           ('-retry', ('-retry', 'other')), ('claim', ('claim', 'other')), ('first', ('first', 'other'))):
            self.assertEqual(stats.kind(name), want, name)

    def test_part_ids(self):
        for part, want in (('ci / part cpu', 'cpu'), ('part (gpu-2)', 'gpu-2'), ('cpu', 'cpu'), ('ci / oom', 'oom')):
            self.assertEqual(stats.part_id(part), want)

    def test_a_flat_part_is_known_by_its_retry(self):
        jobs = [{'id': 1, 'name': 'cpu', 'run_attempt': 1}, {'id': 2, 'name': 'cpu-retry', 'run_attempt': 1},
                {'id': 3, 'name': 'gpu', 'run_attempt': 1}]
        got = stats.jobs_by_part(jobs)
        self.assertEqual({k: {x: [j['id'] for j in v] for x, v in ks.items()} for k, ks in got.items()},
                         {(1, 'cpu'): {'retry': [2], 'first': [1]}})

    def test_rows_from_the_outcome(self):
        R = stats.row
        for o, nxt, want in (('thermal', 'retry', 'cool-card'), ('thermal', 'fallback', 'fallback'),
                             ('reboot', 'retry', 'same-host'), ('oom', 'retry', 'more-memory'), ('lost', 'retry', 'any'),
                             ('stopped', 'retry', 'any'), ('unavailable', 'retry', 'same-host'),
                             ('unavailable', 'fallback', 'fallback'), ('pass', 'none', 'unknown'), ('red', 'none', 'unknown')):
            self.assertEqual(R('retry', {'outcome': o, 'next': nxt}, [], []), want, (o, nxt))
        # the hint's row, as the route reads it: a cool card's label in retry-on does not make a fallback one
        self.assertEqual(R('retry', {'outcome': 'thermal', 'next': 'fallback', 'retry-on': ['self-hosted', 'hosta-gpu1']},
                           [], []), 'fallback')
        self.assertEqual(R('fallback', {'outcome': 'oom', 'next': 'retry'}, [], []), 'fallback')
        taken = {'conclusion': 'success', 'started_at': '2027-01-15T08:00:00Z', 'runner_name': 'r'}
        self.assertEqual(R('retry', None, [], [taken]), 'any')
        self.assertEqual(R('retry', None, [{'conclusion': 'cancelled'}], []), 'any')
        self.assertEqual(R('retry', None, [{'conclusion': 'success'}], [dict(taken, conclusion='skipped')]), 'unknown')

    def test_labels(self):
        self.assertEqual(stats.labels_of({'labels': ['self-hosted', 'ci-cpu', 'mem-big']}), 'ci-cpu+mem-big')
        self.assertEqual(stats.labels_of({'labels': ['self-hosted']}), 'self-hosted')
        self.assertEqual(stats.labels_of({'labels': ['ubuntu-latest']}), 'ubuntu-latest')

    def test_quantile(self):
        self.assertIsNone(stats.quantile([], .5))
        self.assertEqual(stats.quantile([5], .9), 5)
        self.assertEqual(stats.quantile([4, 1, 3, 2], .5), 3)  # index round(1.5) = 2
        self.assertEqual(stats.quantile(list(range(1, 7)), .5), 3)  # index round(2.5) = 2, half to even
        self.assertEqual(stats.quantile(list(range(10)), .9), 8)  # index round(8.1)

    def test_the_push_window(self):
        run = {'event': 'push', 'head_sha': 'x', 'created_at': '2027-01-15T08:10:00Z'}
        for at, want in (('2027-01-15T08:00:00Z', True), ('2027-01-15T07:59:59Z', False),
                         ('2027-01-15T08:11:00Z', True), ('2027-01-15T08:11:01Z', False)):
            got = stats.push_time(run, [{'after': 'x', 'timestamp': at, 'activity_type': 'push'}])
            self.assertEqual(got is not None, want, at)
        self.assertIsNone(stats.push_time(run, [{'after': 'x', 'timestamp': '2027-01-15T08:09:00Z',
                                                 'activity_type': 'branch_deletion'}]))
        on = dict(run, head_branch='topic')  # the same sha pushed to another branch is not this run's push
        self.assertIsNone(stats.push_time(on, [{'after': 'x', 'timestamp': '2027-01-15T08:09:00Z', 'activity_type': 'push',
                                                'ref': 'refs/heads/other'}]))
        self.assertIsNotNone(stats.push_time(on, [{'after': 'x', 'timestamp': '2027-01-15T08:09:00Z',
                                                   'activity_type': 'push', 'ref': 'refs/heads/topic'}]))
        self.assertIsNone(stats.push_time(dict(run, event='schedule'), [{'after': 'x', 'timestamp': '2027-01-15T08:09:00Z',
                                                                          'activity_type': 'push'}]))

    def test_reason_heads(self):
        for reason, want in (('card 3: held by a process that is not ours', 'card'), ('busy: the host is busy', 'busy'),
                             ('tier: hostb (tier 1) has 2 idle runner(s)', 'tier'), ('drained (pool part): x', 'drained'),
                             ('health gate: card 0 at or above 85 C', 'health gate'), ('', '?')):
            self.assertEqual(stats.reason_head(reason), want)


class Report(unittest.TestCase):
    """``report`` on runs made up in place: the cases the batch does not hold."""

    @staticmethod
    def j(name, c, st, e, att=1, labels=('self-hosted', 'ci-cpu'), concl='success', status='completed'):
        return {'name': name, 'labels': list(labels), 'run_attempt': att, 'status': status, 'conclusion': concl,
                'created_at': stats.iso(T0 + c), 'started_at': stats.iso(T0 + st),
                'completed_at': stats.iso(T0 + e) if e is not None else None, 'runner_name': 'r'}

    def one(self, run, outcomes=None):
        return stats.report([{'repo': 'acme/x', 'runs': [run], 'pushes': [], 'outcomes': outcomes or {}}])['repos']['acme/x']

    def test_a_rerun_of_the_retry_alone_takes_the_first_attempts_outcome(self):
        run = {'id': 7, 'event': 'push', 'created_at': stats.iso(T0), 'run_started_at': stats.iso(T0 + 900),
               'run_attempt': 2, 'status': 'completed', 'jobs': [
                   self.j('part p / first', 0, 5, 100), self.j('part p / retry', 101, 110, 200, concl='failure'),
                   self.j('part p / retry', 901, 905, 950, att=2), self.j('verdict', 951, 952, 960, att=2)]}
        got = self.one(run, {(7, 1, 'p'): {'outcome': 'oom', 'next': 'retry'}})
        self.assertEqual(got['retries'], {'more-memory': 2})
        self.assertEqual((got['wall']['max'], got['verdict']['max'], got['verdict_from']['rerun']), (60, 60, 1))

    def test_a_run_in_progress_has_no_wall_and_no_verdict_yet(self):
        run = {'id': 8, 'event': 'push', 'created_at': stats.iso(T0), 'run_attempt': 1, 'status': 'in_progress',
               'jobs': [self.j('part p / first', 0, 5, None, status='in_progress')]}
        got = self.one(run)
        self.assertEqual((got['wall']['n'], got['verdict']['n'], got['no_verdict'], got['queue']['n'], got['run']['n']),
                         (0, 0, 0, 1, 0))

    def test_a_difference_below_zero_is_counted_not_used(self):
        run = {'id': 9, 'event': 'workflow_dispatch', 'created_at': stats.iso(T0 + 100), 'run_attempt': 1,
               'status': 'completed', 'jobs': [self.j('part p / first', 120, 110, 150), self.j('verdict', 50, 60, 90)]}
        got = self.one(run)
        self.assertEqual((got['wall']['n'], got['verdict']['n'], got['queue']['n'], got['negative']), (1, 0, 1, 2))  # the verdict job's queue


class Records(unittest.TestCase):
    """``--records DIR``: a host's part records and presence's log (16.4, 19.8)."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(__import__('shutil').rmtree, self.d)

    def record(self, repo, run, att, part, n, doc, at=T0 + 100):
        p = os.path.join(self.d, 'ci-runners', 'state', 'parts', *repo.split('/'), f'{run}-{att}', f'{part}-{n}')
        os.makedirs(p)
        with open(os.path.join(p, 'outcome.json'), 'w') as f:
            json.dump(doc, f)
        os.utime(os.path.join(p, 'outcome.json'), (at, at))

    def log(self, day, lines):
        p = os.path.join(self.d, 'ci-runners', 'logs')
        os.makedirs(p, exist_ok=True)
        with open(os.path.join(p, f'presence-{day}.log'), 'a') as f:
            for t, line in lines:
                f.write(json.dumps({'time': datetime.datetime.fromtimestamp(T0 + t, datetime.timezone.utc).isoformat(
                    timespec='seconds'), **line}) + '\n')
            f.write('not json\n')

    def test_a_record_gives_the_row_and_no_artifact_is_read_for_it(self):
        self.record('acme/lib', 202, 1, 'a', 1, {'outcome': 'reboot', 'reason': 'the host rebooted', 'next': 'retry'})
        rc, rep, hub = run_stats(*REPOS, '--records', self.d)
        self.assertEqual(rc, 0)
        self.assertEqual(rep['repos']['acme/lib']['retries'], {'same-host': 1, 'cool-card': 1, 'any': 1})
        self.assertEqual(len([u for u, _ in hub.calls if u.startswith(BLOB)]), 5)

    def test_a_later_attempts_refusal_counts_and_is_no_first_attempts_outcome(self):
        self.record('acme/lib', 202, 1, 'a', 2, {'outcome': 'unavailable', 'reason': 'drained: by hand'})
        self.record('acme/lib', 204, 1, 'c', 1, {'outcome': 'unavailable', 'reason': 'drained: by hand'}, at=T0 - 7200)
        self.record('acme/other', 9, 1, 'c', 1, {'outcome': 'unavailable', 'reason': 'busy'})
        _, rep, _ = run_stats(*REPOS, '--records', self.d)
        self.assertEqual(rep['repos']['acme/lib']['refusals'], {'drained': 1})  # the old one is before --since
        self.assertEqual(rep['repos']['acme/lib']['retries'], {'more-memory': 1, 'cool-card': 1, 'any': 1})

    def test_presence_stops_and_starts_by_cause(self):
        self.log('2027-01-15', [
            (-120, {'runner': 'h-part-1', 'action': 'stop', 'cause': 'load: 9 cores that are not ours'}),  # before T
            (10, {'runner': 'h-part-1', 'action': 'start', 'cause': 'no cause'}),
            (20, {'runner': 'h-gpu0', 'action': 'stop', 'cause': 'card 0: 900 MiB that are not ours; card 0: held by x'}),
            (30, {'runner': 'h-part-2', 'action': 'stop', 'cause': 'tier: hostb (tier 1) has 1 idle runner(s) of ci-cpu'}),
            (35, {'runner': 'h-part-3', 'action': 'kept', 'cause': 'slot: no free CI slot (1 of 2 held by others)',
                  'why': 'a job started'}),
            (40, {'runner': 'h-gpu0', 'action': 'start', 'cause': 'no cause'}),
            (50, {'runner': 'h-part-2', 'action': 'start', 'cause': 'no cause'}),
            (60, {'hold': 'set', 'by': 'maintainer', 'until': 'x'}), (70, {'hold': 'released', 'by': 'maintainer'}),
            (80, {'infra': 'lost', 'record': 'r'}), (81, {'infra': 'not counted', 'error': 'x'}),
            (90, {'drain': 'drained: infra', 'ends': []}), (91, {'drain': 'drained: infra', 'error': 'x'}),
            (25000, {'runner': 'h-part-9', 'action': 'stop', 'cause': 'slot: x'}),  # after now
        ])
        _, rep, _ = run_stats(*REPOS, '--records', self.d)
        self.assertEqual(rep['presence'][self.d], {'stop': {'card': 1, 'tier': 1}, 'start': {'card': 1, 'tier': 1, 'no cause': 1},
                                                   'kept': {'slot': 1}, 'holds': 1, 'infra': 1, 'drains': 1})

    def test_no_records_reads_no_presence(self):
        _, rep, _ = run_stats(*REPOS)
        self.assertIsNone(rep['presence'])


class Cli(unittest.TestCase):
    def test_no_login_is_status_2_and_nothing_is_called(self):
        for env in ({}, {'GH_TOKEN': 'two words'}, {'GH_TOKEN': 'x' * 256}):
            hub = Hub()
            rc, _, _ = run_stats(*REPOS, hub=hub, environ=env)
            self.assertEqual(rc, 2)
            self.assertEqual(hub.calls, [])

    def test_bad_arguments_are_status_2(self):
        for args in (['--repo', 'not a repo'], ['--repo', 'acme/web', '--since', 'yesterday'],
                     ['--repo', 'acme/web', '--max-requests', '0'], ['--repo', 'acme/web', '--records', '/nonexistent'],
                     [*REPOS, '--run', '1'], ['--repo', 'acme/web', '--run', '0'], ['--bogus']):
            hub = Hub()
            out = io.StringIO()
            self.assertEqual(stats.main(args, environ={'GH_TOKEN': TOKEN}, send=hub, now=lambda: NOW, out=out), 2, args)
            self.assertEqual(hub.calls, [])

    def test_since(self):
        self.assertEqual(stats.parse_since('90m', 10000.0), 10000.0 - 5400)
        self.assertEqual(stats.parse_since('24h', 100000.0), 100000.0 - 86400)
        self.assertEqual(stats.parse_since('7d', 1e6), 1e6 - 7 * 86400)
        self.assertEqual(stats.parse_since('2027-01-15', NOW), T0 - 8 * 3600)
        for bad in ('0h', '1w', 'soon', stats.iso(NOW + 60)):
            with self.assertRaises(ValueError):
                stats.parse_since(bad, NOW)

    def test_the_repositories_default_to_the_project_registry(self):
        with tempfile.TemporaryDirectory() as etc:
            os.makedirs(os.path.join(etc, 'projects'))
            for n, body in (('web', 'repository = "acme/web"\n'), ('lib', 'repository = "acme/lib"\n'),
                            ('bad', 'repository = "not one"\n'), ('broken', 'repository = \n')):
                with open(os.path.join(etc, 'projects', f'{n}.toml'), 'w') as f:
                    f.write(body)
            self.assertEqual(stats.default_repos(etc), ['acme/lib', 'acme/web'])
            out = io.StringIO()
            rc = stats.main(['--json', '--since', stats.iso(T0 - 60)], etc, {'GH_TOKEN': TOKEN}, Hub(), lambda: NOW, out,
                            'https://api.example')
            self.assertEqual(rc, 0)
            self.assertEqual(sorted(json.loads(out.getvalue())['repos']), ['acme/lib', 'acme/web'])
            self.assertEqual(stats.main([], os.path.join(etc, 'none'), {'GH_TOKEN': TOKEN}, Hub(), lambda: NOW,
                                        io.StringIO()), 2)

    def test_ci_run_dispatches_stats(self):
        import contextlib
        old = os.environ.get('GH_TOKEN')
        os.environ.pop('GH_TOKEN', None)
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                self.assertEqual(cli.main(['stats', '--repo', 'acme/web']), 2)  # no login: no network
        finally:
            if old is not None:
                os.environ['GH_TOKEN'] = old
        self.assertIn('stats: no login', err.getvalue())


if __name__ == '__main__':
    unittest.main()
