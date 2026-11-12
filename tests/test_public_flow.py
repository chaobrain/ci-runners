"""A public caller's run (decision 18.7, C2): ``flow admit``, the public route and retry, the
contract, claim, plan and verdict on a hosted runner, and the hosted part executor."""
from __future__ import annotations

import json
import os
import pathlib
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from ci_runners import flow, hosted, policy, verdict
from ci_runners import route as rt
from tests.test_flow import REPO, SHA, Api, Env, run
from tests.test_route import part

PUB = {'CIRUNNERS_PUBLIC': 'true'}


def payload(name='pull_request', head=REPO, base=REPO, user='alice', pusher='alice', sender='alice'):
    ev = {'repository': {'full_name': head if name != 'pull_request' else base, 'private': False}, 'sender': {'login': sender}}
    if name == 'pull_request':
        ev['pull_request'] = {'head': {'repo': {'full_name': head}}, 'base': {'repo': {'full_name': base}},
                              'user': {'login': user}}
    if name == 'push':
        ev['pusher'] = {'name': pusher}
    return ev


def env(name='pull_request', actor='alice', triggering=None, protected=None, repo=REPO):
    e = {'GITHUB_REPOSITORY': repo, 'GITHUB_EVENT_NAME': name, 'GITHUB_ACTOR': actor,
         'GITHUB_TRIGGERING_ACTOR': triggering or actor, **PUB}
    if protected is not None:
        e['GITHUB_REF_PROTECTED'] = protected
    return e


class Admit(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(flow, 'PUBLIC_CALLERS', frozenset({REPO}))
        p.start()
        self.addCleanup(p.stop)

    def test_a_same_repository_pull_request_and_a_protected_push(self):
        self.assertEqual(flow.admit(env(), payload()), '')
        self.assertEqual(flow.admit(env('push', protected='true'), payload('push')), '')
        self.assertEqual(flow.admit(env(repo='Chaobrain/MyProj'), payload()), '')  # GitHub's case

    def test_a_repository_the_lead_did_not_opt_in(self):
        for repo, opted in (('chaobrain/other', {REPO}), (REPO, set()), (policy.SELF, {policy.SELF})):
            with mock.patch.object(flow, 'PUBLIC_CALLERS', frozenset(opted)):
                self.assertIn('not opted in', flow.admit(env(repo=repo), payload(head=repo, base=repo)), repo)

    def test_every_other_event(self):
        for name in ('workflow_dispatch', 'schedule', 'pull_request_target', 'workflow_run', 'issue_comment',
                     'merge_group', 'repository_dispatch', 'Push', ''):
            self.assertIn('a same-repository pull_request or a protected push only', flow.admit(env(name), payload(name)), name)

    def test_a_fork_or_a_bot(self):
        self.assertIn('fork', flow.admit(env(), payload(head='mallory/myproj')))
        self.assertIn('fork', flow.admit(env(), payload(base='mallory/myproj')))
        ev = payload()
        ev['pull_request']['head'] = {'repo': None}  # a deleted head
        self.assertIn('fork', flow.admit(env(), ev))
        for kw in ({'actor': 'dependabot[bot]'}, {'triggering': 'github-actions[bot]'}):
            self.assertIn('bot', flow.admit(env(**kw), payload()), kw)
        self.assertIn('bot', flow.admit(env(), payload(user='renovate[bot]')))
        self.assertIn('bot', flow.admit(env(), payload(sender='renovate[bot]')))
        self.assertIn('bot', flow.admit(env('push', protected='true'), payload('push', pusher='x[bot]')))
        self.assertIn('bot', flow.admit(env(actor=''), payload()))  # an actor not named
        for side in ('sender', 'user'):  # a bot by its account type, whatever its login
            ev = payload()
            (ev['sender'] if side == 'sender' else ev['pull_request']['user'])['type'] = 'Bot'
            self.assertIn('bot', flow.admit(env(), ev), side)
            (ev['sender'] if side == 'sender' else ev['pull_request']['user'])['type'] = 'User'
            self.assertEqual(flow.admit(env(), ev), '', side)

    def test_a_push_to_a_ref_that_is_not_protected(self):
        for protected in (None, 'false', 'True', ''):
            self.assertIn('not protected', flow.admit(env('push', protected=protected), payload('push')), protected)

    def test_the_command_prints_its_refusal_and_fails(self):
        with tempfile.TemporaryDirectory() as d:
            e = Env(d)
            path = os.path.join(d, 'event.json')
            with open(path, 'w') as f:
                json.dump(payload(head='mallory/myproj'), f)
            code, out, _ = run(['admit'], {**e.env, **env(), 'GITHUB_EVENT_PATH': path}, Api())
            self.assertEqual(code, 1)
            self.assertTrue(out.startswith('::error::ci-runners: a fork'))
            code, out, _ = run(['admit'], {**e.env, **env(), 'GITHUB_EVENT_PATH': os.path.join(d, 'none')}, Api())
            self.assertEqual((code, out), (1, '::error::ci-runners: the event payload does not load\n'))
            self.assertEqual(flow.admit(env(), ['not', 'an', 'object']), 'the event payload does not load')
            with open(path, 'w') as f:
                json.dump(payload(), f)
            api = Api()
            code, out, _ = run(['admit'], {**e.env, **env(), 'GITHUB_EVENT_PATH': path}, api)
            self.assertEqual((code, out, api.calls), (0, '', []))


class Callers(unittest.TestCase):
    def test_nobody_is_opted_in_yet_and_never_this_repository(self):
        self.assertNotIn(policy.SELF, flow.PUBLIC_CALLERS)
        self.assertEqual(flow.PUBLIC_CALLERS, frozenset())  # C2: the lead's entries come with C4


class Route(unittest.TestCase):
    def test_cpu_and_light_hosted_gpu_on_its_own_pool(self):
        got = flow.route_public([part('a', 'cpu', 10), part('b', 'gpu', 20), part('c', 'light', 5)])
        self.assertEqual([p['labels'] for p in got], [['ubuntu-latest'], ['self-hosted', 'ci-pub-gpu'], ['ubuntu-latest']])
        self.assertEqual([(p['where'], p['target']) for p in got], [('host', '')] * 3)  # never the cluster
        self.assertEqual(got[1]['timeout_minutes'], 20 + rt.GRACE_MIN)

    def test_gpu2_and_timing_are_red_at_plan_time(self):
        for need in ('gpu2', 'timing'):
            with self.assertRaisesRegex(rt.PlanError, f"b: needs '{need}': a public repository runs"):
                flow.route_public([part('a', 'cpu', 10), part('b', need, 10)])

    def test_the_public_plan_reads_no_host_and_ends_red_for_gpu2_timing(self):
        for need in ('gpu2', 'timing'):
            with tempfile.TemporaryDirectory() as d:
                e = Env(d)
                pf = os.path.join(d, 'plan.json')
                with open(pf, 'w') as f:
                    json.dump({'schema': 1, 'parts': [part('a', 'cpu', 10), part('b', need, 10)]}, f)
                with mock.patch.object(flow, '_rows', side_effect=AssertionError('read the capacity')), \
                        mock.patch.object(flow, '_project', side_effect=AssertionError('read the registry')):
                    code, _, _ = run(['plan', '--context', 'p/ci', '--sha', SHA, '--project', 'p', '--plan-file', pf],
                                     {**e.env, **PUB}, Api())
                o = e.outputs()
                self.assertEqual((code, o['state']), (flow.EXIT_PLAN, 'failure'), need)
                self.assertIn(f"needs '{need}'", o['description'])
                self.assertNotIn('parts', o)

    def test_a_public_plan_routes_without_the_capacity(self):
        with tempfile.TemporaryDirectory() as d:
            e = Env(d)
            pf = os.path.join(d, 'plan.json')
            with open(pf, 'w') as f:
                json.dump({'schema': 1, 'parts': [part('a', 'cpu', 10), part('b', 'gpu', 10)]}, f)
            with mock.patch.object(flow, '_rows', side_effect=AssertionError('read the capacity')), \
                    mock.patch.object(flow, '_project', side_effect=AssertionError('read the registry')):
                code, _, _ = run(['plan', '--context', 'p/ci', '--sha', SHA, '--project', 'p', '--plan-file', pf],
                                 {**e.env, **PUB}, Api())
            o = e.outputs()
            self.assertEqual((code, o['routed'], o['max_parallel']), (0, 'true', str(flow.MAX_PARALLEL)))
            a, b = json.loads(o['parts'])
            self.assertEqual((a['labels'], a['gpus'], b['labels'], b['gpus']),
                             (['ubuntu-latest'], 0, ['self-hosted', 'ci-pub-gpu'], 1))
            self.assertIn('| `a` | cpu | ubuntu-latest |', e.summary())


class Retry(unittest.TestCase):
    def routed(self, need='cpu'):
        return flow.entry(flow.route_public([part('p', need, 10)])[0])

    def test_every_row_is_the_part_s_own_labels(self):
        for need in ('cpu', 'gpu'):
            p = self.routed(need)
            for outcome, nxt, row in (('thermal', 'retry', 'cool-card'), ('reboot', 'retry', 'same-host'),
                                      ('oom', 'retry', 'more-memory'), ('stopped', 'retry', 'any'),
                                      ('unavailable', 'fallback', 'fallback')):
                doc = {'outcome': outcome, 'next': nxt, 'attempt': 1, 'host': 'hosted', 'cool-card': '1',
                       'pool-labels': ['ci-gpu', 'h-gpu']}
                r = flow.decide(p, doc, '', None, '', 1, {**PUB, 'GITHUB_RUN_ID': '7', 'GITHUB_REPOSITORY': REPO})
                self.assertEqual((r['row'], r['labels']), (row, p['labels']), (need, outcome))
                self.assertEqual(r['resume'], 'true' if row == 'more-memory' else 'false')
                self.assertEqual(r['target'], '')

    def test_red_and_a_second_attempt_are_final_and_lost_is_retried(self):
        p = self.routed()
        e = {**PUB, 'GITHUB_RUN_ID': '7', 'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_REPOSITORY': REPO}
        for doc in ({'outcome': 'red', 'next': 'none'}, {'outcome': 'pass', 'next': 'none'},
                    {'outcome': 'stopped', 'next': 'retry', 'attempt': 2}):
            self.assertIsNone(flow.decide(p, doc, '', None, '', 1, e), doc)
        r = flow.decide(p, None, 'public', None, '', 1, e)
        self.assertEqual((r['labels'], r['first'], r['attempt_of']), (['ubuntu-latest'], 'lost', 'public:chaobrain/myproj/7-1/p-1'))

    def test_the_command_refuses_a_part_a_public_route_never_makes(self):
        with tempfile.TemporaryDirectory() as d:
            e = Env(d)
            bad = dict(self.routed(), needs='gpu2')
            code, _, err = run(['retry', '--part', json.dumps(bad)], {**e.env, **PUB}, Api())
            self.assertEqual(code, 2)
            code, out, _ = run(['retry', '--part', json.dumps(self.routed('gpu'))], {**e.env, **PUB}, Api())
            self.assertEqual((code, json.loads(out)), (0, ['self-hosted', 'ci-pub-gpu']))


class Legs(unittest.TestCase):
    def test_the_contract_keeps_each_caller_to_its_own_legs(self):
        for private, public, want in ((True, False, 0), (False, True, 0), (False, False, 1), (True, True, 1)):
            with tempfile.TemporaryDirectory() as d:
                e = Env(d)
                api = Api({'main': 'contract = 2\n', SHA: 'contract = 2\n'}, private=private)
                code, _, _ = run(['contract', '--context', 'p/ci', '--sha', SHA], {**e.env, **(PUB if public else {})}, api)
                self.assertEqual(code, want, (private, public))
                if want:
                    self.assertEqual(e.outputs()['description'], flow.PUBLIC if not private else flow.PRIVATE_ON_PUBLIC)
        for v, want in (('true', True), ('false', False), ('1', False), ('', False), (None, False)):
            self.assertIs(flow.is_public({} if v is None else {'CIRUNNERS_PUBLIC': v}), want, v)

    def test_the_claim_counts_the_job_token_s_statuses_without_a_policy(self):
        class Statuses(Api):
            def __call__(self, method, url, token, body=None):
                if '/statuses' in url:
                    return [{'state': 'success', 'context': 'p/ci', 'created_at': '2026-11-12T10:00:00Z',
                             'creator': {'login': 'github-actions[bot]'}, 'description': 'ok'}]
                return super().__call__(method, url, token, body)
        with tempfile.TemporaryDirectory() as d:
            e = Env(d)
            code, _, err = run(['claim', '--context', 'p/ci', '--sha', SHA], {**e.env, **PUB}, Statuses(), etc=d)
            self.assertEqual((code, e.outputs()['run']), (0, 'false'), err)  # green by the job token's identity
            api = Statuses()
            code, _, err = run(['pending', '--context', 'p/ci', '--sha', SHA, '--seen', '0'], {**e.env, **PUB}, api, etc=d)
            self.assertEqual((code, e.outputs()['posted']), (0, 'false'), err)  # the pending, by the same identity
            code, _, err = run(['pending', '--context', 'p/ci', '--sha', SHA, '--seen', '0'], e.env, Statuses(), etc=d)
            self.assertEqual(code, 2)  # a private caller's reads the policy's bot, as before
            code, _, _ = run(['claim', '--context', 'p/ci', '--sha', SHA], e.env, Api(), etc=d)
            self.assertEqual(code, 2)  # a private caller's claim reads the policy's bot, as before

    def test_the_verdict_reads_artifacts_and_never_a_host(self):
        with tempfile.TemporaryDirectory() as d:
            e = Env(d)
            arts = os.path.join(d, 'arts')
            os.makedirs(os.path.join(arts, 'ci-part-a-1'))
            with open(os.path.join(arts, 'ci-part-a-1', 'outcome.json'), 'w') as f:
                json.dump({'outcome': 'pass', 'run-attempt': 1}, f)
            api = Api()
            with mock.patch.dict(os.environ, {**e.env, **PUB}, clear=True), \
                    mock.patch.object(verdict.part, 'salvage', side_effect=AssertionError('salvaged')), \
                    mock.patch('ci_runners.github.api', api):
                code, _, err = run(['verdict', '--context', 'p/ci', '--sha', SHA, '--parts', 'a b', '--artifacts', arts],
                                   {**e.env, **PUB}, api)
            self.assertEqual(code, 1, err)  # b has no artifact: lost, no record read
            [posted] = api.posted()
            self.assertEqual(posted['state'], 'error')
            self.assertIn('b lost', posted['description'])
            with mock.patch.dict(os.environ, e.env, clear=True):
                code, _, err = run(['verdict', '--context', 'p/ci', '--sha', SHA, '--parts', 'a', '--artifacts', arts],
                                   e.env, Api())
            self.assertEqual(code, 2)  # not a public caller's: actions/verdict is ci.yml's
            self.assertIn("verdict is a public caller's", err)


class Hosted(unittest.TestCase):
    def run_part(self, command, *extra, env=None):
        with tempfile.TemporaryDirectory() as d:
            ws = os.path.join(d, 'ws')
            os.makedirs(ws)
            out, summ = os.path.join(d, 'out'), os.path.join(d, 'summ')
            e = {'RUNNER_ENVIRONMENT': 'github-hosted', 'RUNNER_TEMP': d, 'GITHUB_WORKSPACE': ws, 'GITHUB_OUTPUT': out,
                 'GITHUB_STEP_SUMMARY': summ, 'GITHUB_RUN_ATTEMPT': '2', 'PATH': os.environ['PATH'],
                 'GITHUB_TOKEN': 'secret-1', 'GH_TOKEN': 'secret-2', 'ACTIONS_RUNTIME_TOKEN': 'secret-3',
                 'ACTIONS_ID_TOKEN_REQUEST_URL': 'u', **(env or {})}
            code = hosted.run(['--id', 'p', '--command', command, '--timeout-minutes', '1', *extra], e)
            doc = None
            path = os.path.join(d, 'ci-runners', 'p', 'outcome.json')
            if os.path.exists(path):
                with open(path) as f:
                    doc = json.load(f)
            outputs = {}
            if os.path.exists(out):
                with open(out) as f:
                    outputs = dict(x.split('=', 1) for x in f.read().splitlines())
            res = os.path.join(d, 'ci-runners', 'p', 'results')
            files = {n: pathlib.Path(res, n).read_text() for n in os.listdir(res)} if os.path.isdir(res) else {}
            return code, doc, outputs, {**files, 'ran': os.path.exists(os.path.join(ws, 'ran')), 'ws': ws}

    def test_pass_red_and_the_outcome_the_verdict_reads(self):
        code, doc, outputs, _ = self.run_part('true')
        self.assertEqual((code, doc['outcome'], doc['attempt'], doc['run-attempt'], doc['next']), (0, 'pass', 1, 2, 'none'))
        self.assertEqual((outputs['outcome'], outputs['attempt']), ('pass', '1'))
        self.assertIn(doc['outcome'], verdict.KINDS)
        code, doc, outputs, _ = self.run_part('exit 3', '--attempt-of', 'public:x/1-1/p-1')
        self.assertEqual((code, doc['outcome'], doc['reason'], doc['attempt']), (1, 'red', 'exit 3', 2))

    def test_no_token_reaches_the_command_and_it_runs_in_the_checkout(self):
        code, doc, _, got = self.run_part('env > "$CI_RESULTS_DIR/env.txt"; pwd > "$CI_RESULTS_DIR/pwd.txt"')
        self.assertEqual(code, 0)
        self.assertNotIn('secret-', got['env.txt'])
        self.assertNotIn('ACTIONS_ID_TOKEN', got['env.txt'])
        self.assertEqual(got['pwd.txt'].strip(), os.path.realpath(got['ws']))
        self.assertEqual(hosted.command_env({'GITHUB_TOKEN': 'x', 'A': 'b', 'MY_SECRET': 's', 'ACTIONS_ID_TOKEN_REQUEST_URL': 'u'},
                                            '/r', True), {'A': 'b', 'CI_RESULTS_DIR': '/r', 'CI_RESUME': '1'})
        self.assertNotIn('CI_RESUME', hosted.command_env({'CI_RESUME': '1'}, '/r', False))

    def test_nothing_the_command_leaves_behind_outlives_it(self):
        with tempfile.TemporaryDirectory() as d:
            pidfile = os.path.join(d, 'pid')
            code, doc, _, _ = self.run_part(f'sleep 300 & echo $! > {pidfile}')
            self.assertEqual((code, doc['outcome']), (0, 'pass'))
            pid = int(pathlib.Path(pidfile).read_text())
            for _ in range(100):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)  # reaped by init, a moment after the kill
            else:
                os.kill(pid, signal.SIGKILL)
                self.fail('the background child outlived the command')

    def test_the_bound_ends_the_command_red(self):
        with mock.patch.object(hosted, 'KILL_GRACE', 1):
            p = mock.MagicMock()
            p.wait.side_effect = [subprocess.TimeoutExpired('bash', 60), subprocess.TimeoutExpired('bash', 1), None]
            p.returncode = -15
            with mock.patch('os.killpg') as kill:
                with tempfile.TemporaryDirectory() as d:
                    code = hosted.run(['--id', 'p', '--command', 'sleep 999', '--timeout-minutes', '1'],
                                      {'RUNNER_ENVIRONMENT': 'github-hosted', 'RUNNER_TEMP': d}, popen=lambda *a, **k: p)
                    with open(os.path.join(d, 'ci-runners', 'p', 'outcome.json')) as f:
                        doc = json.load(f)
            self.assertEqual((code, doc['outcome']), (1, 'red'))
            self.assertIn('timeout', doc['reason'])
            self.assertEqual([c.args[1] for c in kill.call_args_list], [signal.SIGTERM, signal.SIGKILL])

    def test_it_refuses_to_run_on_one_of_our_runners(self):
        for e in ({'RUNNER_ENVIRONMENT': 'self-hosted'}, {}, {'RUNNER_ENVIRONMENT': 'github-hosted', 'CI_RUNNERS_ETC': '/x/etc'}):
            code, doc, _, got = self.run_part('touch "$GITHUB_WORKSPACE/ran"', env={'RUNNER_ENVIRONMENT': '', **e})
            self.assertEqual((code, doc, got['ran']), (2, None, False), e)
        for bad in (['--id', '../x'], ['--timeout-minutes', '0'], ['--timeout-minutes', '241']):
            args = dict(zip(*[iter(['--id', 'p', '--timeout-minutes', '1', *bad])] * 2))
            with tempfile.TemporaryDirectory() as d:
                code = hosted.run([*sum(([k, v] for k, v in args.items()), []), '--command', 'true'],
                                  {'RUNNER_ENVIRONMENT': 'github-hosted', 'RUNNER_TEMP': d})
                self.assertEqual(code, 2, bad)


if __name__ == '__main__':
    unittest.main()
