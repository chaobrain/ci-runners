"""The runner watch (design decision 15.5): ci_runners/runnerwatch.py against a fake GitHub,
setup/workstation/runner-watch.sh with a fake gh and ci-run, and its units."""
from __future__ import annotations

import io
import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

from ci_runners import github, runnerwatch
from tests.test_backstop import ticks, unit

ROOT = pathlib.Path(__file__).resolve().parents[1]
WS = ROOT / 'setup' / 'workstation'
SCRIPT = WS / 'runner-watch.sh'
T0 = 1_800_000_000.0  # 2027-01-15 08:00 UTC
Q = 15 * 60
ORG, REPO = 'acme', 'acme/ci-runners'
TITLE_A, TITLE_B = 'No runner of hosta online', 'No runner of hostb online'


def runner(name, status='online', labels=(), busy=False):
    return {'name': name, 'status': status, 'busy': busy,
            'labels': [{'name': x} for x in ('self-hosted', 'Linux', 'X64', *labels)]}


class Hub:
    """A fake GitHub: the org's runners, the repository's open issues, and every call."""

    def __init__(self, runners=(), issues=()):
        self.runners = list(runners)
        self.issues = [dict(i) for i in issues]
        self.calls = []
        self.tries = []  # (method, tries) of each call
        self.fail = set()  # methods that fail
        self.broken = None  # a malformed answer for the runners
        self.next = 100

    def __call__(self, method, url, token, body=None, tries=3):
        self.calls.append((method, url, body))
        self.tries.append((method, tries))
        assert token == 'tok', token
        if method in self.fail:
            raise github.ApiError(f'{method} {url}: HTTP 502')
        path, _, query = url.partition('?')
        q = dict(kv.split('=') for kv in query.split('&')) if query else {}
        if method == 'GET' and path == f'{runnerwatch.API}/orgs/{ORG}/actions/runners':
            if self.broken is not None:
                return self.broken
            p, n = int(q['page']), int(q['per_page'])
            return {'total_count': len(self.runners), 'runners': self.runners[(p - 1) * n:p * n]}
        if method == 'GET' and path == f'{runnerwatch.API}/repos/{REPO}/issues':
            assert q.get('state') == 'open', q
            p, n = int(q['page']), int(q['per_page'])
            return [i for i in self.issues if i['state'] == 'open'][(p - 1) * n:p * n]
        if method == 'POST' and path == f'{runnerwatch.API}/repos/{REPO}/issues':
            self.next += 1
            self.issues.append({'number': self.next, 'title': body['title'], 'state': 'open'})
            return {'number': self.next}
        if method == 'POST' and path.endswith('/comments'):
            return {}
        if method == 'PATCH':
            n = int(path.rsplit('/', 1)[1])
            for i in self.issues:
                if i['number'] == n:
                    i['state'] = body['state']
            return {}
        raise AssertionError(url)

    def writes(self):
        return [(m, u.replace(f'{runnerwatch.API}/repos/{REPO}', ''), b) for m, u, b in self.calls if m != 'GET']


class Watch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, 'state.json')
        self.hub = Hub([runner('hosta-light-1', labels=['hosta-light']), runner('hosta-gpu0', labels=['hosta-gpu']),
                        runner('hostb-cpu-1', labels=['hostb-cpu']), runner('elsewhere-1')])

    def run_watch(self, t, *extra, hosts=('hosta', 'hostb'), env=None):
        out = io.StringIO()
        rc = runnerwatch.main(['--org', ORG, '--state', self.state, *extra, '--', *hosts],
                              environ={'GH_TOKEN': 'tok'} if env is None else env, call=self.hub,
                              now=lambda: t, out=out)
        return rc, out.getvalue()

    def down(self, prefix):
        for r in self.hub.runners:
            if r['name'].startswith(prefix):
                r['status'] = 'offline'

    def up(self, name):
        for r in self.hub.runners:
            if r['name'] == name:
                r['status'] = 'online'

    def saved(self):
        return json.loads(pathlib.Path(self.state).read_text())['hosts']

    def test_runners_belong_to_a_host_by_name_or_label_the_longer_match_first(self):
        hosts = ['a', 'a-b', 'c']
        self.assertEqual(runnerwatch.host_of(runner('a-light-1'), hosts), 'a')
        self.assertEqual(runnerwatch.host_of(runner('a-b-cpu-1'), hosts), 'a-b')
        self.assertEqual(runnerwatch.host_of(runner('a-b-cpu-1'), list(reversed(hosts))), 'a-b')
        self.assertEqual(runnerwatch.host_of(runner('node7', labels=['c-gpu']), hosts), 'c')  # a label only
        self.assertEqual(runnerwatch.host_of(runner('c'), hosts), 'c')
        self.assertIsNone(runnerwatch.host_of(runner('ca-1', labels=['self-c', 'xc-gpu']), hosts))
        self.assertIsNone(runnerwatch.host_of({'name': None, 'labels': None}, hosts))

    def test_two_consecutive_checks_open_one_issue_and_no_more(self):
        self.down('hosta')
        rc, out = self.run_watch(T0)
        self.assertEqual(rc, 0)
        self.assertIn('runner-watch: hosta: 0 of 2 online; none at 1 consecutive check\n', out)
        self.assertIn('runner-watch: hostb: 1 of 1 online\n', out)
        self.assertIn('runner-watch: 1 runner of no listed host\n', out)
        self.assertEqual(self.hub.writes(), [])
        self.assertEqual(self.saved()['hosta'], {'offline': 1, 'last': T0, 'alerted': False})
        rc, out = self.run_watch(T0 + Q)
        self.assertEqual(rc, 0)
        self.assertIn('runner-watch: hosta: 0 of 2 online; none at 2 consecutive checks; opened #101\n', out)
        (m, u, body), = self.hub.writes()
        self.assertEqual((m, u, body['title']), ('POST', '/issues', TITLE_A))
        self.assertIn('2027-01-15 08:00 UTC and 2027-01-15 08:15 UTC', body['body'])
        self.assertIn('- `hosta-gpu0`: offline\n- `hosta-light-1`: offline\n', body['body'])
        self.assertNotIn('hostb', body['body'])
        self.assertEqual(self.saved()['hosta'], {'offline': 2, 'last': T0 + Q, 'alerted': True})
        for k in (2, 3, 4):  # still down: no duplicate
            rc, out = self.run_watch(T0 + k * Q)
            self.assertEqual(rc, 0)
        self.assertEqual(len(self.hub.writes()), 1)
        self.assertEqual(self.saved()['hosta']['offline'], 5)

    def test_a_host_with_no_runner_registered_counts_as_none_online(self):
        self.hub.runners = [r for r in self.hub.runners if not r['name'].startswith('hostb')]
        self.run_watch(T0)
        rc, out = self.run_watch(T0 + Q)
        self.assertIn('runner-watch: hostb: 0 of 0 online; none at 2 consecutive checks; opened #101\n', out)

    def test_a_busy_runner_is_online_and_one_online_runner_is_enough(self):
        self.down('hosta')
        self.hub.runners[1].update(status='online', busy=True)
        self.run_watch(T0)
        self.run_watch(T0 + Q)
        self.assertEqual(self.hub.writes(), [])
        self.assertEqual(self.saved()['hosta']['offline'], 0)

    def test_an_open_issue_with_the_title_is_adopted_and_pull_requests_are_not(self):
        self.down('hosta')
        self.hub.issues = [{'number': 7, 'title': TITLE_A, 'state': 'open', 'pull_request': {}},
                           {'number': 8, 'title': TITLE_A + '!', 'state': 'open'},
                           {'number': 9, 'title': TITLE_A, 'state': 'open'}]
        self.run_watch(T0)
        rc, out = self.run_watch(T0 + Q)
        self.assertIn('none at 2 consecutive checks; adopted #9\n', out)
        self.assertEqual(self.hub.writes(), [])
        self.assertTrue(self.saved()['hosta']['alerted'])

    def test_back_online_comments_and_closes_and_a_new_outage_alerts_again(self):
        self.down('hosta')
        self.run_watch(T0)
        self.run_watch(T0 + Q)
        self.up('hosta-gpu0')
        rc, out = self.run_watch(T0 + 2 * Q)
        self.assertEqual(rc, 0)
        self.assertIn('runner-watch: hosta: 1 of 2 online; closed #101\n', out)
        _, (m1, u1, b1), (m2, u2, b2) = self.hub.writes()
        self.assertEqual((m1, u1), ('POST', '/issues/101/comments'))
        self.assertIn('1 of 2 runners of `hosta` online at the check of 2027-01-15 08:30 UTC', b1['body'])
        self.assertEqual((m2, u2, b2), ('PATCH', '/issues/101', {'state': 'closed', 'state_reason': 'completed'}))
        self.assertEqual(self.saved()['hosta'], {'offline': 0, 'last': T0 + 2 * Q, 'alerted': False})
        self.run_watch(T0 + 3 * Q)  # nothing more to close
        self.assertEqual(len(self.hub.writes()), 3)
        self.down('hosta')
        self.run_watch(T0 + 4 * Q)
        rc, out = self.run_watch(T0 + 5 * Q)
        self.assertIn('opened #102', out)

    def test_an_issue_a_person_closed_is_not_reopened_during_the_outage(self):
        self.down('hosta')
        self.run_watch(T0)
        self.run_watch(T0 + Q)
        self.hub.issues[0]['state'] = 'closed'
        rc, out = self.run_watch(T0 + 2 * Q)
        self.assertIn('none at 3 consecutive checks; alerted\n', out)
        self.assertEqual(len(self.hub.writes()), 1)

    def test_a_gap_of_more_than_40_min_starts_the_count_again(self):
        self.assertEqual(runnerwatch.GAP, 40 * 60)
        self.down('hosta')
        self.run_watch(T0)
        rc, out = self.run_watch(T0 + runnerwatch.GAP + 1)
        self.assertIn('none at 1 consecutive check\n', out)
        self.assertEqual(self.hub.writes(), [])
        rc, out = self.run_watch(T0 + runnerwatch.GAP + 1 + 2 * Q)  # one missed tick still counts
        self.assertIn('none at 2 consecutive checks; opened #101', out)

    def test_a_failed_read_writes_nothing(self):
        self.down('hosta')
        self.run_watch(T0)
        before = pathlib.Path(self.state).read_text()
        for broken in ({'runners': None}, [], {'runners': [1]}):
            self.hub.broken = broken
            rc, _ = self.run_watch(T0 + Q)
            self.assertEqual(rc, 1, broken)
            self.assertEqual(self.hub.writes(), [])
            self.assertEqual(pathlib.Path(self.state).read_text(), before)
        self.hub.broken = None
        self.hub.fail = {'GET'}
        rc, _ = self.run_watch(T0 + Q)
        self.assertEqual(rc, 1)
        self.assertEqual(pathlib.Path(self.state).read_text(), before)

    def test_a_full_last_page_is_an_error_not_a_partial_list(self):
        self.hub.runners = [runner(f'hosta-x-{i}') for i in range(runnerwatch.PAGE * runnerwatch.PAGES)]
        rc, _ = self.run_watch(T0)
        self.assertEqual(rc, 1)
        self.assertFalse(os.path.exists(self.state))
        self.hub.runners = [runner(f'hosta-x-{i}', 'offline') for i in range(runnerwatch.PAGE)] + [runner('hostb-1')]
        rc, out = self.run_watch(T0)  # the second page is read
        self.assertEqual(rc, 0)
        self.assertIn('runner-watch: hostb: 1 of 1 online\n', out)
        self.assertIn('runner-watch: hosta: 0 of 100 online', out)

    def test_a_failed_issue_call_is_retried_at_the_next_tick_and_other_hosts_go_on(self):
        self.down('hosta')
        self.down('hostb')
        self.run_watch(T0)
        self.hub.fail = {'POST'}
        rc, out = self.run_watch(T0 + Q)
        self.assertEqual(rc, 1)
        self.assertEqual([u for m, u, b in self.hub.writes()], ['/issues', '/issues'])  # both hosts tried
        self.assertFalse(self.saved()['hosta']['alerted'])
        self.hub.fail = set()
        rc, out = self.run_watch(T0 + 2 * Q)
        self.assertEqual(rc, 0)
        self.assertIn('opened #101', out)
        self.assertIn('opened #102', out)

    def test_issue_writes_are_not_retried_within_a_run(self):
        self.down('hosta')
        self.run_watch(T0)
        self.run_watch(T0 + Q)
        self.up('hosta-gpu0')
        self.run_watch(T0 + 2 * Q)
        self.assertEqual(len(self.hub.writes()), 3)  # open, comment, close
        self.assertEqual({t for m, t in self.hub.tries if m != 'GET'}, {1})  # a timeout may have written
        self.assertEqual({t for m, t in self.hub.tries if m == 'GET'}, {3})

    def test_report_only_prints_and_writes_nothing(self):
        self.down('hosta')
        self.run_watch(T0)
        before = pathlib.Path(self.state).read_text()
        rc, out = self.run_watch(T0 + Q, '--report-only')
        self.assertEqual(rc, 0)
        self.assertIn(f'none at 2 consecutive checks; would open "{TITLE_A}" in {REPO}\n', out)
        self.assertIn('report only: no issue and no state written', out)
        self.assertEqual(self.hub.writes(), [])
        self.assertEqual(pathlib.Path(self.state).read_text(), before)
        self.hub.issues = [{'number': 5, 'title': TITLE_B, 'state': 'open'}]
        rc, out = self.run_watch(T0 + Q, '--report-only')
        self.assertIn('runner-watch: hostb: 1 of 1 online; would close #5\n', out)
        self.assertEqual(self.hub.writes(), [])

    def test_the_state_is_private_and_a_bad_one_starts_again(self):
        self.run_watch(T0)
        self.assertEqual(stat.S_IMODE(os.stat(self.state).st_mode), 0o600)
        self.down('hosta')
        for bad in ('{', '[]', '{"hosts": {"hosta": {"offline": "1", "last": 0}}}'):
            pathlib.Path(self.state).write_text(bad)
            rc, out = self.run_watch(T0 + Q)
            self.assertEqual(rc, 0)
            self.assertIn('hosta: 0 of 2 online; none at 1 consecutive check\n', out)

    def test_bad_arguments_and_no_login_call_nothing(self):
        for hosts in (('-x',), ('a b',), ('a', 'a'), ('a/b',)):
            self.assertEqual(self.run_watch(T0, hosts=hosts)[0], 2, hosts)
        for env in ({}, {'GH_TOKEN': ''}, {'GH_TOKEN': 'a b'}, {'GH_TOKEN': 'x' * 300}):
            self.assertEqual(self.run_watch(T0, env=env)[0], 2, env)
        out = io.StringIO()
        for args in (['--org', 'a/b', '--state', self.state, 'h'], ['--org', ORG, '--state', 'rel.json', 'h'],
                     ['--org', ORG, '--state', self.state], ['--state', self.state, 'h']):
            self.assertEqual(runnerwatch.main(args, {'GH_TOKEN': 'tok'}, self.hub, lambda: T0, out), 2, args)
        self.assertEqual(self.hub.calls, [])
        self.assertFalse(os.path.exists(self.state))


# the fake ci-run: what it was run with
CIRUN = '''#!/bin/sh
{ printf '%s\\n' "$@"; echo "GH_TOKEN=${GH_TOKEN-unset} GITHUB_TOKEN=${GITHUB_TOKEN-unset}\
 GITHUB_REPOSITORY=${GITHUB_REPOSITORY-unset} GITHUB_ACTIONS=${GITHUB_ACTIONS-unset} WRAPPED=${WRAPPED-unset}"; } > "$RECORD"
echo 'runner-watch: fake'
exit "${CIRUN_EXIT:-0}"
'''


class Script(unittest.TestCase):
    """The login read from gh for this run only and passed to ci-run in its environment; ci-run
    of the script's own release, run under the wrapper; nothing run on bad arguments."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = self.t = pathlib.Path(self.tmp.name)
        rel = self.rel = t / 'release'
        (rel / 'setup' / 'workstation').mkdir(parents=True)
        (rel / 'bin').mkdir()
        (rel / 'actions').mkdir()
        (rel / 'actions' / 'RELEASE').write_text('v9.9.9\n')
        shutil.copy2(SCRIPT, rel / 'setup' / 'workstation' / 'runner-watch.sh')
        (rel / 'bin' / 'ci-run').write_text(CIRUN)
        (rel / 'bin' / 'ci-run').chmod(0o755)
        self.bin = t / 'bin'
        self.bin.mkdir()
        self.gh('echo tok-xyz')
        self.wrap = t / 'wrap'
        self.wrap.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {t}/wrap.args\nWRAPPED=1 exec "$@"\n')
        self.wrap.chmod(0o755)
        self.record = t / 'record'

    def gh(self, body: str):
        f = self.bin / 'gh'
        f.write_text(f'#!/bin/sh\necho "$*" >> {self.t}/gh.calls\n[ "$*" = "auth token" ] || exit 9\n{body}\n')
        f.chmod(0o755)

    def run_script(self, *args, **extra):
        env = {'PATH': f'{self.bin}:/usr/bin:/bin', 'HOME': self.tmp.name, 'LANG': 'C.UTF-8',
               'RECORD': str(self.record), **extra}
        return subprocess.run([str(self.rel / 'setup' / 'workstation' / 'runner-watch.sh'), *args], env=env,
                              cwd=self.tmp.name, capture_output=True, text=True, timeout=60)

    def args(self):
        return str(self.t / 'state.json'), str(self.wrap), 'acme', 'hosta', 'host-b.2'

    def test_ci_run_of_the_release_runs_under_the_wrapper_with_the_login(self):
        r = self.run_script(*self.args(), GITHUB_TOKEN='job-token', GITHUB_REPOSITORY='someone/else',
                            GITHUB_ACTIONS='true')
        self.assertEqual(r.returncode, 0, r.stderr)
        rec = self.record.read_text().splitlines()
        st = str(self.t / 'state.json')
        self.assertEqual(rec[:-1], ['runner-watch', '--org', 'acme', '--state', st, '--', 'hosta', 'host-b.2'])
        self.assertEqual(rec[-1], 'GH_TOKEN=tok-xyz GITHUB_TOKEN=unset GITHUB_REPOSITORY=unset GITHUB_ACTIONS=unset '
                                  'WRAPPED=1')
        wrapped = (self.t / 'wrap.args').read_text().splitlines()
        self.assertEqual(wrapped[0], str((self.rel / 'bin' / 'ci-run').resolve()))
        for text in (r.stdout, r.stderr, '\n'.join(wrapped)):  # never on a command line or printed
            self.assertNotIn('tok-xyz', text)
        self.assertIn('runner-watch.sh: release v9.9.9', r.stdout)
        self.assertIn('runner-watch: fake', r.stdout)
        self.assertEqual((self.t / 'gh.calls').read_text(), 'auth token\n')

    def test_report_only_is_passed_on_and_the_status_is_ci_runs(self):
        r = self.run_script('--report-only', *self.args())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('--report-only', self.record.read_text().splitlines()[:-1])
        self.assertEqual(self.run_script(*self.args(), CIRUN_EXIT='1').returncode, 1)

    def test_bad_arguments_read_no_login_and_run_nothing(self):
        s, w, o, h1, h2 = self.args()
        (self.t / 'plain').write_text('')
        for args in ((), (s, w, o), ('--report-only', s, w, o), (s, '--report-only', w, o, h1), ('state.json', w, o, h1),
                     (s, 'proxy', o, h1), (s + ' x', w, o, h1), (s + ';x', w, o, h1), (s, w + '$(id)', o, h1),
                     (s, w, 'a/b', h1), (s, w, '-o', h1), (s, w, o, '-x'), (s, w, o, h1, 'a b'), (s, w, o, 'a;b'),
                     (s, str(self.t / 'plain'), o, h1), (s, str(self.t / 'none'), o, h1), (s, str(self.t), o, h1)):
            r = self.run_script(*args)
            self.assertEqual(r.returncode, 2, args)
            self.assertFalse((self.t / 'gh.calls').exists(), args)
            self.assertFalse(self.record.exists(), args)

    def test_no_login_runs_nothing(self):
        for body in ('exit 1', 'exit 0'):  # gh fails; gh prints nothing
            self.gh(body)
            r = self.run_script(*self.args())
            self.assertNotEqual(r.returncode, 0, body)
            self.assertFalse(self.record.exists(), body)

    def test_the_real_ci_run_takes_the_scripts_arguments(self):
        """The script's command line parses in ci-run itself: a malformed login is refused
        before any call, so this needs no network."""
        (self.rel / 'bin' / 'ci-run').unlink()
        shutil.copy2(ROOT / 'bin' / 'ci-run', self.rel / 'bin' / 'ci-run')
        shutil.copytree(ROOT / 'ci_runners', self.rel / 'ci_runners')
        self.gh('echo "two words"')
        r = self.run_script('--report-only', *self.args(), CI_RUNNERS_PYTHON=sys.executable)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn('runner-watch: no login (GH_TOKEN is empty or malformed)', r.stderr)


class Units(unittest.TestCase):
    def test_the_timer_ticks_every_quarter_hour_in_utc(self):
        st = unit('ci-runner-watch.timer')
        spec = [v for k, v in st if k == 'OnCalendar']
        self.assertEqual(spec, ['*-*-* *:00,15,30,45:00 UTC'])
        minutes = sorted({t.minute for t in ticks(spec[0].replace('*:', '0:'))})
        self.assertEqual(minutes, [0, 15, 30, 45])
        self.assertIn(('AccuracySec', '1s'), st)
        keys = {k for k, _ in st}
        self.assertFalse(keys & {'RandomizedDelaySec', 'Persistent', 'OnBootSec', 'OnUnitActiveSec'}, keys)
        self.assertIn(('WantedBy', 'timers.target'), st)

    def test_the_service_runs_the_script_with_its_placeholders_and_a_private_state(self):
        st = dict(unit('ci-runner-watch.service'))
        self.assertEqual(st['ExecStart'], '@RELEASE@/setup/workstation/runner-watch.sh '
                                          '%S/ci-runners/runner-watch.json @NET_WRAP@ @ORG@ @HOSTS@')
        self.assertEqual((st['Type'], st['NoNewPrivileges'], st['TimeoutStartSec']), ('oneshot', 'yes', '5min'))
        self.assertEqual((st['StateDirectory'], st['StateDirectoryMode'], st['UMask']), ('ci-runners', '0700', '0077'))
        self.assertTrue(os.access(SCRIPT, os.X_OK))


if __name__ == '__main__':
    unittest.main()
