"""The hook's decision (design §6.2, §6.3), on sanitised event payloads, hostile ones
included: a fork head, a bot actor, an unlisted repository, ``pull_request_target`` and
a public repository on a ``part`` runner (RUN-1 acceptance 1)."""
from __future__ import annotations

import json
import pathlib
import unittest

from ci_runners import policy

ROOT = pathlib.Path(__file__).resolve().parents[1]
EVENTS = ROOT / 'tests' / 'fixtures' / 'events'

POLICY = """
schema = 1
actors = ["alice", "Carol"]
[repositories."chaobrain/brainspike"]
id = 1001
visibility = "private"
[repositories."chaobrain/brainevent"]
id = 2002
visibility = "public"
[repositories."chaobrain/paused-one"]
id = 3003
visibility = "private"
paused = true
"""
P = policy.load(POLICY)
P_TARGET = policy.load(POLICY.replace('id = 1001\n', 'id = 1001\nextra_events = ["pull_request_target"]\n'))

REPOS = {'chaobrain/brainspike': '1001', 'chaobrain/brainevent': '2002'}


def event(name: str) -> dict:
    return json.loads((EVENTS / f'{name}.json').read_text())


def env(repo='chaobrain/brainspike', event='pull_request', actor='alice', triggering=None, cls='part', rid=...):
    e = {'GITHUB_REPOSITORY': repo, 'GITHUB_REPOSITORY_ID': REPOS.get(repo, '4004') if rid is ... else rid,
         'GITHUB_EVENT_NAME': event, 'GITHUB_ACTOR': actor, 'GITHUB_TRIGGERING_ACTOR': triggering or actor,
         'CI_RUNNER_CLASS': cls}
    return {k: v for k, v in e.items() if v is not None}


def reason(p, e, ev):
    d = policy.decide(p, e, ev)
    assert d.accepted == (d.reason == 'ok'), d
    return d.reason


class Accepted(unittest.TestCase):
    def test_same_repository_pull_request_by_a_member(self):
        for cls in sorted(policy.CLASSES):
            self.assertEqual(reason(P, env(cls=cls), event('pr_same_repo')), 'ok', cls)

    def test_push_dispatch_schedule_workflow_run(self):
        self.assertEqual(reason(P, env(event='push'), event('push')), 'ok')
        self.assertEqual(reason(P, env(event='workflow_dispatch'), event('dispatch_member')), 'ok')
        self.assertEqual(reason(P, env(event='schedule'), event('schedule')), 'ok')  # no repository in it
        self.assertEqual(reason(P, env(event='workflow_run'), event('workflow_run_same')), 'ok')

    def test_logins_compare_without_case(self):
        ev = event('dispatch_member')
        ev['sender']['login'] = 'CAROL'
        self.assertEqual(reason(P, env(event='workflow_dispatch', actor='carol', triggering='Alice'), ev), 'ok')

    def test_the_bot_may_dispatch_in_a_private_repository(self):
        bot = 'github-actions[bot]'
        for name in ('workflow_dispatch', 'repository_dispatch'):
            self.assertEqual(reason(P, env(event=name, actor=bot), event('dispatch_bot')), 'ok', name)

    def test_a_public_repository_on_its_gpu_runners(self):
        for cls in ('gpu', 'h100'):
            self.assertEqual(reason(P, env('chaobrain/brainevent', cls=cls), event('pr_public_same_repo')), 'ok')
            self.assertEqual(reason(P, env('chaobrain/brainevent', 'push', cls=cls), event('push_public')), 'ok')

    def test_hostile_strings_change_nothing_and_never_reach_the_line(self):
        ev = event('pr_hostile_strings')
        d = policy.decide(P, env(), ev)
        self.assertEqual(d, policy.Decision(True, 'ok'))
        out = policy.line(d, P.digest)
        self.assertRegex(out, r'\Aci-runners: accepted ok policy=[0-9a-f]{12}\Z')


class Refused(unittest.TestCase):
    def test_fork_pull_requests(self):
        self.assertEqual(reason(P, env(actor='mallory'), event('pr_fork')), 'fork')
        self.assertEqual(reason(P, env(), event('pr_fork_by_member')), 'fork')  # a member's fork is a fork
        self.assertEqual(reason(P, env(), event('pr_head_repo_deleted')), 'fork')
        ev = event('pr_same_repo')
        ev['pull_request']['base']['repo'] = event('pr_fork')['pull_request']['head']['repo']
        self.assertEqual(reason(P, env(), ev), 'fork')  # the base is checked as well
        ev = event('pr_same_repo')
        del ev['pull_request']
        self.assertEqual(reason(P, env(), ev), 'fork')  # a pull_request event without one
        self.assertEqual(reason(P, env('chaobrain/brainevent', cls='gpu'), {
            **event('pr_fork'), 'repository': event('pr_public_same_repo')['repository'],
            'pull_request': {**event('pr_fork')['pull_request'],
                             'base': event('pr_public_same_repo')['pull_request']['base']}}), 'fork')

    def test_a_workflow_run_of_a_fork_or_an_outsider(self):
        self.assertEqual(reason(P, env(event='workflow_run'), event('workflow_run_fork')), 'fork')
        for key in ('actor', 'triggering_actor'):
            ev = event('workflow_run_same')
            ev['workflow_run'][key] = {'login': 'mallory'}
            self.assertEqual(reason(P, env(event='workflow_run'), ev), 'actor', key)
            del ev['workflow_run'][key]
            self.assertEqual(reason(P, env(event='workflow_run'), ev), 'actor', key)

    def test_pull_request_target_is_refused_unless_listed_and_then_still_not_from_a_fork(self):
        self.assertEqual(reason(P, env(event='pull_request_target'), event('pr_target_same_repo')), 'event')
        self.assertEqual(reason(P, env(event='pull_request_target', actor='mallory'), event('pr_target_fork')),
                         'event')
        self.assertEqual(reason(P, env('chaobrain/brainevent', 'pull_request_target', cls='gpu'),
                                event('pr_public_same_repo')), 'event')
        self.assertEqual(reason(P_TARGET, env(event='pull_request_target'), event('pr_target_same_repo')), 'ok')
        self.assertEqual(reason(P_TARGET, env(event='pull_request_target', actor='mallory'),
                                event('pr_target_fork')), 'fork')
        ev = event('pr_target_same_repo')
        ev['pull_request']['user']['login'] = 'mallory'
        self.assertEqual(reason(P_TARGET, env(event='pull_request_target'), ev), 'actor')  # its author counts

    def test_other_events(self):
        self.assertEqual(reason(P, env(event='issue_comment'), event('issue_comment')), 'event')
        self.assertEqual(reason(P, env(event='Push'), event('push')), 'event')
        for name in ('schedule', 'workflow_run', 'status', 'repository_dispatch'):  # private only
            self.assertEqual(reason(P, env('chaobrain/brainevent', name, cls='gpu'), event('push_public')),
                             'event', name)

    def test_an_unlisted_repository_and_this_one(self):
        ev = event('push')
        ev['repository'] = {'id': 4004, 'full_name': 'chaobrain/other'}
        self.assertEqual(reason(P, env('chaobrain/other', 'push'), ev), 'repository')
        self.assertEqual(reason(P, env('chaobrain/ci-runners', 'push', rid='5005'), ev), 'self')
        self.assertEqual(reason(P, env('Chaobrain/CI-Runners', 'push', rid='5005'), ev), 'self')
        self.assertEqual(reason(P, env(repo=None), event('push')), 'repository')
        self.assertEqual(reason(P, env(repo='chaobrain/brainspike\n'), event('push')), 'repository')

    def test_the_repository_id_is_checked_against_the_name(self):
        self.assertEqual(reason(P, env(rid='2002'), event('pr_same_repo')), 'repository-id')
        self.assertEqual(reason(P, env(rid=''), event('pr_same_repo')), 'repository-id')
        self.assertEqual(reason(P, env(rid=None), event('pr_same_repo')), 'repository-id')
        self.assertEqual(reason(P, env(rid=' 1001'), event('pr_same_repo')), 'repository-id')
        for name in ('pr_kelvin_name', 'pr_bool_id', 'pr_string_id', 'pr_float_id'):
            self.assertEqual(reason(P, env(), event(name)), 'repository-id', name)
        ev = event('pr_same_repo')
        ev['repository'] = event('pr_public_same_repo')['repository']
        self.assertEqual(reason(P, env(), ev), 'repository-id')  # the payload names another repository
        ev = event('pr_same_repo')
        ev['repository']['id'] = 1002
        self.assertEqual(reason(P, env(), ev), 'repository-id')  # the right name with another id

    def test_a_payload_without_a_repository_or_not_an_object(self):
        ev = event('push')
        del ev['repository']
        self.assertEqual(reason(P, env(event='push'), ev), 'payload')
        ev = {**event('schedule'), 'repository': event('pr_public_same_repo')['repository']}
        self.assertEqual(reason(P, env(event='schedule'), ev), 'repository-id')  # a schedule naming another
        self.assertEqual(reason(P, env(event='push'), [ev]), 'payload')
        self.assertEqual(reason(P, env(event='push'), None), 'payload')

    def test_a_public_repository_on_a_part_runner(self):
        for cls in ('light', 'part', 'compile', 'nightly'):
            self.assertEqual(reason(P, env('chaobrain/brainevent', cls=cls), event('pr_public_same_repo')),
                             'runner-class', cls)

    def test_an_unknown_runner_class(self):
        for cls in (None, '', 'PART', 'part ', 'h100-gpu0'):
            self.assertEqual(reason(P, env(cls=cls), event('pr_same_repo')), 'runner-class', cls)

    def test_the_bot(self):
        bot = 'github-actions[bot]'
        self.assertEqual(reason(P, env(event='push', actor=bot), event('push')), 'bot')
        self.assertEqual(reason(P, env(event='schedule', actor=bot), event('schedule')), 'bot')
        self.assertEqual(reason(P, env(event='push', triggering=bot), event('push')), 'bot')  # the bot re-ran a push
        ev = event('pr_same_repo')
        ev['pull_request']['user']['login'] = bot
        self.assertEqual(reason(P, env(), ev), 'bot')  # a pull request the bot opened
        ev = event('push_public')
        self.assertEqual(reason(P, env('chaobrain/brainevent', 'workflow_dispatch', actor=bot, cls='gpu'),
                                {**ev, 'sender': {'login': bot}}), 'bot')  # public: humans only
        self.assertEqual(reason(P, env(event='workflow_dispatch', actor='dependabot[bot]'),
                                event('dispatch_member')), 'actor')  # another bot is just not listed

    def test_unlisted_actors(self):
        self.assertEqual(reason(P, env(event='push', actor='mallory'), event('push')), 'actor')
        self.assertEqual(reason(P, env(event='push', triggering='mallory'), event('push')), 'actor')  # a re-run
        ev = event('push')
        ev['pusher']['name'] = 'mallory'
        self.assertEqual(reason(P, env(event='push'), ev), 'actor')
        del ev['pusher']
        self.assertEqual(reason(P, env(event='push'), ev), 'actor')  # a push names its pusher
        ev = event('pr_same_repo')
        ev['pull_request']['user']['login'] = 'mallory'
        self.assertEqual(reason(P, env(), ev), 'actor')  # a member's push to an outsider's pull request
        ev = event('dispatch_member')
        ev['sender'] = {'login': 'mallory'}
        self.assertEqual(reason(P, env(event='workflow_dispatch'), ev), 'actor')
        ev['sender'] = 'alice'
        self.assertEqual(reason(P, env(event='workflow_dispatch'), ev), 'actor')  # malformed sender
        e = env()
        del e['GITHUB_TRIGGERING_ACTOR']
        self.assertEqual(reason(P, e, event('pr_same_repo')), 'actor')
        self.assertEqual(reason(P, env(), event('pr_newline_login')), 'actor')
        self.assertEqual(reason(P, env(actor='alicK'.replace('K', 'é')), event('pr_same_repo')),
                         'actor')

    def test_a_paused_repository(self):
        ev = event('push')
        ev['repository'] = {'id': 3003, 'full_name': 'chaobrain/paused-one'}
        self.assertEqual(reason(P, env('chaobrain/paused-one', 'push', rid='3003'), ev), 'paused')


class Load(unittest.TestCase):
    def test_the_example_loads(self):
        p = policy.load((ROOT / 'setup' / 'policy.example.toml').read_bytes())
        self.assertEqual(p.repos['chaobrain/brainevent'].visibility, 'public')
        self.assertEqual(len(p.digest), 64)

    def test_malformed_policies(self):
        bad = {
            'no schema': POLICY.replace('schema = 1', ''),
            'schema 2': POLICY.replace('schema = 1', 'schema = 2'),
            'unknown key': POLICY + 'allow_all = true\n',
            'a bot as actor': POLICY.replace('"Carol"', '"dependabot[bot]"'),
            'a human as bot': 'bot = "alice"\n' + POLICY,
            'not a login': POLICY.replace('"Carol"', '"a b"'),
            'self listed': POLICY + '[repositories."chaobrain/ci-runners"]\nid = 9\nvisibility = "private"\n',
            'id as bool': POLICY.replace('id = 1001', 'id = true'),
            'id zero': POLICY.replace('id = 1001', 'id = 0'),
            'visibility': POLICY.replace('visibility = "public"', 'visibility = "internal"'),
            'paused as str': POLICY.replace('paused = true', 'paused = "yes"'),
            'public extra events': POLICY.replace('id = 2002\n', 'id = 2002\nextra_events = ["pull_request_target"]\n'),
            'extra event malformed': POLICY.replace('id = 1001\n', 'id = 1001\nextra_events = ["a b"]\n'),
            'extra event unchecked': POLICY.replace('id = 1001\n', 'id = 1001\nextra_events = ["issue_comment"]\n'),
            'extra event check_suite': POLICY.replace('id = 1001\n', 'id = 1001\nextra_events = ["check_suite"]\n'),
            'listed twice': POLICY + '[repositories."Chaobrain/Brainspike"]\nid = 9\nvisibility = "private"\n',
            'repo key': POLICY + '[repositories."brainspike"]\nid = 9\nvisibility = "private"\n',
            'repo unknown key': POLICY.replace('id = 1001\n', 'id = 1001\nactors = ["x"]\n'),
            'not toml': 'schema = = 1',
            'not utf-8': b'\xff\xfe',
        }
        for what, text in bad.items():
            with self.assertRaises(policy.PolicyError, msg=what):
                policy.load(text)


class Line(unittest.TestCase):
    def test_the_printed_line(self):
        self.assertEqual(policy.line(policy.Decision(False, 'actor'), 'ab' * 32),
                         'ci-runners: refused actor policy=abababababab')
        self.assertEqual(policy.line(policy.Decision(False, 'x\ny'), ''), 'ci-runners: refused unknown policy=none')


if __name__ == '__main__':
    unittest.main()
