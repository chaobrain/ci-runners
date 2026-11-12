"""The hook's decision (design §6.2, §6.3), on sanitised event payloads, hostile ones
included: a fork head, a bot actor, an unlisted repository, ``pull_request_target`` and
a public repository, which no runner class takes (decision 11.12)."""
from __future__ import annotations

import dataclasses
import json
import pathlib
import unittest

from ci_runners import policy

ROOT = pathlib.Path(__file__).resolve().parents[1]
EVENTS = ROOT / 'tests' / 'fixtures' / 'events'

POLICY = """
schema = 2
actors = ["alice", "Carol"]
[repositories."chaobrain/brainkit"]
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
P_BOT = policy.load(POLICY.replace('id = 1001\n', 'id = 1001\nbot_dispatch = true\n'))
P_TARGET = policy.load(POLICY.replace('id = 1001\n', 'id = 1001\nextra_events = ["pull_request_target"]\n'))
#: brainkit may use the exclusive classes (decision 12.14): compile and nightly, or compile alone
P_BOTH = policy.load(POLICY.replace('id = 1001\n', 'id = 1001\nclasses = ["compile", "nightly", "slurm"]\n'))
P_COMPILE = policy.load(POLICY.replace('id = 1001\n', 'id = 1001\nclasses = ["compile"]\n'))

REPOS = {'chaobrain/brainkit': '1001', 'chaobrain/brainevent': '2002'}
#: the public repository, as a payload names it
PUBLIC = {'id': 2002, 'full_name': 'chaobrain/brainevent', 'private': False}


def event(name: str) -> dict:
    return json.loads((EVENTS / f'{name}.json').read_text())


def public(name: str) -> dict:
    """A same-repository payload of the public repository."""
    ev = event(name)
    ev['repository'] = dict(PUBLIC)
    for side in ('head', 'base'):
        if side in ev.get('pull_request', {}):
            ev['pull_request'][side]['repo'] = dict(PUBLIC)
    return ev


def env(repo='chaobrain/brainkit', event='pull_request', actor='alice', triggering=None, cls='part', rid=...):
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
        for cls in sorted(policy.CLASSES - policy.OPT_IN):
            self.assertEqual(reason(P, env(cls=cls), event('pr_same_repo')), 'ok', cls)
        for cls in sorted(policy.CLASSES):
            self.assertEqual(reason(P_BOTH, env(cls=cls), event('pr_same_repo')), 'ok', cls)

    def test_the_slurm_class_is_opt_in(self):
        """Decision 14.2: a repository uses the slurm runners only where its entry lists them."""
        self.assertNotEqual(reason(P, env(cls='slurm'), event('pr_same_repo')), 'ok')
        self.assertNotEqual(reason(P_COMPILE, env(cls='slurm'), event('pr_same_repo')), 'ok')
        self.assertEqual(reason(P_BOTH, env(cls='slurm'), event('pr_same_repo')), 'ok')

    def test_the_3090_classes_are_open(self):
        """Decision 12.5: gpu and multi are open to every private repository, as part is."""
        for cls in ('gpu', 'multi'):
            self.assertIn(cls, policy.CLASSES)
            self.assertEqual(reason(P, env(cls=cls, event='push'), event('push')), 'ok', cls)

    def test_push_dispatch_schedule_workflow_run(self):
        self.assertEqual(reason(P, env(event='push'), event('push')), 'ok')
        self.assertEqual(reason(P, env(event='workflow_dispatch'), event('dispatch_member')), 'ok')
        self.assertEqual(reason(P, env(event='schedule'), event('schedule')), 'ok')  # no repository in it
        self.assertEqual(reason(P, env(event='workflow_run'), event('workflow_run_same')), 'ok')

    def test_logins_compare_without_case(self):
        ev = event('dispatch_member')
        ev['sender']['login'] = 'CAROL'
        self.assertEqual(reason(P, env(event='workflow_dispatch', actor='carol', triggering='Alice'), ev), 'ok')

    def test_the_bot_may_dispatch_where_the_switch_is_on(self):
        bot = 'github-actions[bot]'
        for name in ('workflow_dispatch', 'repository_dispatch'):
            self.assertEqual(reason(P_BOT, env(event=name, actor=bot), event('dispatch_bot')), 'ok', name)
            self.assertEqual(reason(P_BOT, env(event=name, actor='mallory'), event('dispatch_bot')), 'actor', name)

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
        self.assertEqual(reason(P_TARGET, env(event='pull_request_target'), event('pr_target_same_repo')), 'ok')
        self.assertEqual(reason(P_TARGET, env(event='pull_request_target', actor='mallory'),
                                event('pr_target_fork')), 'fork')
        ev = event('pr_target_same_repo')
        ev['pull_request']['user']['login'] = 'mallory'
        self.assertEqual(reason(P_TARGET, env(event='pull_request_target'), ev), 'actor')  # its author counts

    def test_other_events(self):
        self.assertEqual(reason(P, env(event='issue_comment'), event('issue_comment')), 'event')
        self.assertEqual(reason(P, env(event='Push'), event('push')), 'event')

    def test_an_unlisted_repository_and_this_one(self):
        ev = event('push')
        ev['repository'] = {'id': 4004, 'full_name': 'chaobrain/other'}
        self.assertEqual(reason(P, env('chaobrain/other', 'push'), ev), 'repository')
        self.assertEqual(reason(P, env('chaobrain/ci-runners', 'push', rid='5005'), ev), 'self')
        self.assertEqual(reason(P, env('Chaobrain/CI-Runners', 'push', rid='5005'), ev), 'self')
        self.assertEqual(reason(P, env(repo=None), event('push')), 'repository')
        self.assertEqual(reason(P, env(repo='chaobrain/brainkit\n'), event('push')), 'repository')

    def test_the_repository_id_is_checked_against_the_name(self):
        self.assertEqual(reason(P, env(rid='2002'), event('pr_same_repo')), 'repository-id')
        self.assertEqual(reason(P, env(rid=''), event('pr_same_repo')), 'repository-id')
        self.assertEqual(reason(P, env(rid=None), event('pr_same_repo')), 'repository-id')
        self.assertEqual(reason(P, env(rid=' 1001'), event('pr_same_repo')), 'repository-id')
        for name in ('pr_kelvin_name', 'pr_bool_id', 'pr_string_id', 'pr_float_id'):
            self.assertEqual(reason(P, env(), event(name)), 'repository-id', name)
        ev = event('pr_same_repo')
        ev['repository'] = dict(PUBLIC)
        self.assertEqual(reason(P, env(), ev), 'repository-id')  # the payload names another repository
        ev = event('pr_same_repo')
        ev['repository']['id'] = 1002
        self.assertEqual(reason(P, env(), ev), 'repository-id')  # the right name with another id

    def test_a_payload_without_a_repository_or_not_an_object(self):
        ev = event('push')
        del ev['repository']
        self.assertEqual(reason(P, env(event='push'), ev), 'payload')
        ev = {**event('schedule'), 'repository': dict(PUBLIC)}
        self.assertEqual(reason(P, env(event='schedule'), ev), 'repository-id')  # a schedule naming another
        self.assertEqual(reason(P, env(event='push'), [ev]), 'payload')
        self.assertEqual(reason(P, env(event='push'), None), 'payload')

    def test_a_public_repository_on_any_runner(self):
        for cls in sorted(policy.CLASSES):
            self.assertEqual(reason(P, env('chaobrain/brainevent', cls=cls), public('pr_same_repo')), 'repository', cls)
            self.assertEqual(reason(P, env('chaobrain/brainevent', 'push', cls=cls), public('push')), 'repository', cls)

    def test_the_exclusive_classes_only_where_listed(self):
        """Decision 12.14: compile and nightly only for a repository whose entry lists them."""
        for cls in ('compile', 'nightly'):
            self.assertEqual(reason(P, env(cls=cls), event('pr_same_repo')), 'runner-class', cls)
            self.assertEqual(reason(P, env(cls=cls, event='push'), event('push')), 'runner-class', cls)
        self.assertEqual(reason(P_COMPILE, env(cls='compile'), event('pr_same_repo')), 'ok')
        self.assertEqual(reason(P_COMPILE, env(cls='nightly'), event('pr_same_repo')), 'runner-class')
        self.assertEqual(P_BOTH.repos['chaobrain/brainkit'].classes, {'compile', 'nightly', 'slurm'})
        self.assertEqual(P.repos['chaobrain/brainkit'].classes, frozenset())

    def test_an_unknown_runner_class(self):
        for cls in (None, '', 'PART', 'part ', 'h100-gpu0', 'h100'):  # h100: gone with decision 13.4
            self.assertEqual(reason(P, env(cls=cls), event('pr_same_repo')), 'runner-class', cls)
        self.assertEqual(reason(P, env(cls='gpu'), event('pr_same_repo')), 'ok')

    def test_the_bot_dispatch_switch_is_off_by_default(self):
        bot = 'github-actions[bot]'
        self.assertFalse(P.repos['chaobrain/brainkit'].bot_dispatch)
        for name in ('workflow_dispatch', 'repository_dispatch'):
            self.assertEqual(reason(P, env(event=name, actor=bot), event('dispatch_bot')), 'bot', name)
            self.assertEqual(reason(P, env(event=name, triggering=bot), event('dispatch_bot')), 'bot', name)
        self.assertEqual(reason(P_BOT, env(event='push', actor=bot), event('push')), 'bot')  # on: dispatch only

    def test_the_bot(self):
        bot = 'github-actions[bot]'
        self.assertEqual(reason(P, env(event='push', actor=bot), event('push')), 'bot')
        self.assertEqual(reason(P, env(event='schedule', actor=bot), event('schedule')), 'bot')
        self.assertEqual(reason(P, env(event='push', triggering=bot), event('push')), 'bot')  # the bot re-ran a push
        ev = event('pr_same_repo')
        ev['pull_request']['user']['login'] = bot
        self.assertEqual(reason(P, env(), ev), 'bot')  # a pull request the bot opened
        repos = dict(P.repos)  # a public repository with the switch set (load refuses it) is still refused
        repos['chaobrain/brainevent'] = dataclasses.replace(repos['chaobrain/brainevent'], bot_dispatch=True)
        self.assertEqual(reason(dataclasses.replace(P, repos=repos),
                                env('chaobrain/brainevent', 'workflow_dispatch', actor=bot),
                                {**public('push'), 'sender': {'login': bot}}), 'repository')
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


#: brainevent opted in to the public pool by the lead (decision 16.7, 18.7)
P_PUB = policy.load(POLICY.replace('id = 2002\n', 'id = 2002\npublic_runners = true\n'))


def pub_env(event='pull_request', actor='alice', cls='gpu', audience='public', protected=None, **kw):
    """The environment of a job of the opted-in public repository on a runner of its pool."""
    e = env('chaobrain/brainevent', event, actor, cls=cls, **kw)
    if audience is not None:
        e['CI_RUNNER_AUDIENCE'] = audience
    if protected is not None:
        e['GITHUB_REF_PROTECTED'] = protected
    return e


class Public(unittest.TestCase):
    """Decision 16.7 as amended by 18.7 (C2): P1's event fixtures on an opted-in public
    repository, and the runner audience."""

    def test_a_same_repository_pull_request_and_a_protected_push_on_a_public_gpu_runner(self):
        self.assertEqual(reason(P_PUB, pub_env(), public('pr_same_repo')), 'ok')
        self.assertEqual(reason(P_PUB, pub_env('push', protected='true'), public('push')), 'ok')

    def test_p1_s_event_fixtures_are_refused_with_their_reason(self):
        cases = {  # fixture: (event name, actor, reason)
            'pr_fork': ('pull_request', 'mallory', 'fork'),
            'pr_fork_by_member': ('pull_request', 'alice', 'fork'),
            'pr_head_repo_deleted': ('pull_request', 'alice', 'fork'),
            'pr_target_fork': ('pull_request_target', 'mallory', 'event'),
            'pr_target_same_repo': ('pull_request_target', 'alice', 'event'),
            'dispatch_bot': ('workflow_dispatch', 'github-actions[bot]', 'event'),
            'dispatch_member': ('workflow_dispatch', 'alice', 'event'),
            'schedule': ('schedule', 'alice', 'event'),
            'workflow_run_fork': ('workflow_run', 'alice', 'event'),
            'workflow_run_same': ('workflow_run', 'alice', 'event'),
            'issue_comment': ('issue_comment', 'alice', 'event'),
        }
        forks = ('pr_fork', 'pr_fork_by_member', 'pr_target_fork', 'pr_head_repo_deleted')
        for fixture, (name, actor, why) in cases.items():
            ev = event(fixture)
            if 'repository' in ev:
                ev['repository'] = dict(PUBLIC)
            if fixture in forks:  # the public repository is the base; the head stays the fork's
                ev['pull_request']['base']['repo'] = dict(PUBLIC)
            elif 'pull_request' in ev:
                ev = public(fixture)
            self.assertEqual(reason(P_PUB, pub_env(name, actor), ev), why, fixture)
            # the same on a protected ref: the event decides, not the ref
            self.assertEqual(reason(P_PUB, pub_env(name, actor, protected='true'), ev), why, fixture)

    def test_a_push_to_a_ref_that_is_not_protected(self):
        for protected in (None, 'false', '', 'True', 'true ', '1'):
            self.assertEqual(reason(P_PUB, pub_env('push', protected=protected), public('push')), 'unprotected',
                             protected)

    def test_a_bot_or_a_non_member_never_runs_there(self):
        self.assertEqual(reason(P_PUB, pub_env(actor='mallory'), public('pr_same_repo')), 'actor')
        self.assertEqual(reason(P_PUB, pub_env(triggering='mallory'), public('pr_same_repo')), 'actor')  # a re-run
        for bot in ('github-actions[bot]', 'dependabot[bot]'):
            self.assertEqual(reason(P_PUB, pub_env(actor=bot), public('pr_same_repo')), 'bot', bot)
            self.assertEqual(reason(P_PUB, pub_env(triggering=bot), public('pr_same_repo')), 'bot', bot)
            self.assertEqual(reason(P_PUB, pub_env('push', bot, protected='true'), public('push')), 'bot', bot)
        ev = public('pr_same_repo')
        ev['pull_request']['user']['login'] = 'mallory'  # its author counts
        self.assertEqual(reason(P_PUB, pub_env(), ev), 'actor')
        # the bot switch never opens it, even on an entry the loader would refuse
        repo = dataclasses.replace(P_PUB.repos['chaobrain/brainevent'], bot_dispatch=True)
        p = dataclasses.replace(P_PUB, repos={**P_PUB.repos, 'chaobrain/brainevent': repo})
        self.assertEqual(reason(p, pub_env(actor='github-actions[bot]'), public('pr_same_repo')), 'bot')

    def test_only_the_gpu_class_of_the_public_audience(self):
        for cls in sorted(policy.CLASSES - {'gpu'}):
            self.assertEqual(reason(P_PUB, pub_env(cls=cls), public('pr_same_repo')), 'runner-class', cls)
            self.assertEqual(reason(P_PUB, pub_env('push', cls=cls, protected='true'), public('push')),
                             'runner-class', cls)
        for audience in (None, 'private'):  # a private runner, whatever its class
            for cls in sorted(policy.CLASSES):
                self.assertEqual(reason(P_PUB, pub_env(cls=cls, audience=audience), public('pr_same_repo')),
                                 'audience', (audience, cls))

    def test_a_private_repository_is_refused_on_a_public_runner(self):
        for cls in sorted(policy.CLASSES):
            e = env(cls=cls)
            e['CI_RUNNER_AUDIENCE'] = 'public'
            self.assertEqual(reason(P_BOTH, e, event('pr_same_repo')), 'audience', cls)
        e = env(cls='part')
        e['CI_RUNNER_AUDIENCE'] = 'private'  # said, as unset
        self.assertEqual(reason(P, e, event('pr_same_repo')), 'ok')

    def test_an_audience_that_is_neither_is_refused(self):
        for audience in ('', 'Public', 'public ', 'all', 'privat'):
            e = env(cls='gpu')
            e['CI_RUNNER_AUDIENCE'] = audience
            self.assertEqual(reason(P_PUB, e, event('pr_same_repo')), 'audience', audience)
            self.assertEqual(reason(P_PUB, pub_env(audience=audience), public('pr_same_repo')), 'audience', audience)
            e = env('chaobrain/nowhere', cls='gpu')  # the runner's own fault, said before the repository's
            e['CI_RUNNER_AUDIENCE'] = audience
            self.assertEqual(reason(P_PUB, e, event('pr_same_repo')), 'audience', audience)

    def test_a_public_repository_not_opted_in_is_refused_as_before(self):
        for audience in (None, 'public'):
            self.assertEqual(reason(P, pub_env(audience=audience), public('pr_same_repo')), 'repository', audience)
        paused = policy.load(POLICY.replace('id = 2002\n', 'id = 2002\npublic_runners = true\npaused = true\n'))
        self.assertEqual(reason(paused, pub_env(), public('pr_same_repo')), 'paused')

    def test_the_payload_must_name_the_public_repository(self):
        for private in (True, None, 'false', 0):  # its payload must say it is public
            ev = public('pr_same_repo')
            ev['repository'] = {**ev['repository'], 'private': private}
            if private is None:
                del ev['repository']['private']
            self.assertEqual(reason(P_PUB, pub_env(), ev), 'repository', private)
        ev = public('pr_same_repo')
        ev['repository'] = {'id': 1001, 'full_name': 'chaobrain/brainkit'}
        self.assertEqual(reason(P_PUB, pub_env(), ev), 'repository-id')
        self.assertEqual(reason(P_PUB, pub_env(rid='1001'), public('pr_same_repo')), 'repository-id')

    def test_the_opt_in_is_the_policy_s_alone(self):
        """Nothing of the job or its payload opts a repository in: only the policy's entry."""
        ev = public('pr_same_repo')
        ev['repository']['public_runners'] = True
        ev['repository']['visibility'] = 'private'
        ev['repository']['private'] = True
        e = pub_env()
        e['CI_RUNNER_PUBLIC_RUNNERS'] = 'true'
        self.assertEqual(reason(P, e, ev), 'repository')
        self.assertEqual(reason(P, env('chaobrain/brainevent', cls='gpu'), ev), 'repository')

    def test_the_entry_s_key(self):
        self.assertTrue(P_PUB.repos['chaobrain/brainevent'].public_runners)
        self.assertFalse(P.repos['chaobrain/brainevent'].public_runners)
        for bad in ('public_runners = "true"', 'public_runners = 1'):
            with self.assertRaises(policy.PolicyError, msg=bad):
                policy.load(POLICY.replace('id = 2002\n', f'id = 2002\n{bad}\n'))
        with self.assertRaises(policy.PolicyError):  # a private repository is never opted in
            policy.load(POLICY.replace('id = 1001\n', 'id = 1001\npublic_runners = true\n'))
        policy.load(POLICY.replace('id = 1001\n', 'id = 1001\npublic_runners = false\n'))

    def test_the_reasons_the_hook_may_print(self):
        for why in ('audience', 'unprotected', 'runner'):
            self.assertIn(why, policy.REASONS)
            self.assertLess(policy.REASONS.index(why), policy.REASONS.index('stale'))  # a decision, not an infra end
            self.assertEqual(policy.line(policy.Decision(False, why), ''), f'ci-runners: refused {why} policy=none')
        self.assertGreater(policy.REASONS.index('inventory'), policy.REASONS.index('stale'))  # the hook could not decide
        self.assertEqual(policy.line(policy.Decision(False, 'inventory'), ''), 'ci-runners: refused inventory policy=none')
        from ci_runners import presence
        self.assertEqual(set(presence.HOOK_INFRA), set(policy.REASONS[policy.REASONS.index('stale'):]))


class Load(unittest.TestCase):
    def test_a_schema_1_policy_is_refused(self):
        with self.assertRaises(policy.PolicyError):
            policy.load(POLICY.replace('schema = 2', 'schema = 1'))
        self.assertEqual(len(P.digest), 64)

    def test_malformed_policies(self):
        bad = {
            'no schema': POLICY.replace('schema = 2', ''),
            'schema 1': POLICY.replace('schema = 2', 'schema = 1'),
            'schema 3': POLICY.replace('schema = 2', 'schema = 3'),
            'schema as str': POLICY.replace('schema = 2', 'schema = "2"'),
            'schema as bool': POLICY.replace('schema = 2', 'schema = true'),
            'schema as float': POLICY.replace('schema = 2', 'schema = 2.0'),
            'org in schema 1': POLICY.replace('schema = 2', 'schema = 1') + '[org]\nname = "chaobrain"\n',
            'unknown key': POLICY + 'allow_all = true\n',
            'a bot as actor': POLICY.replace('"Carol"', '"dependabot[bot]"'),
            'a human as bot': 'bot = "alice"\n' + POLICY,
            'not a login': POLICY.replace('"Carol"', '"a b"'),
            'self listed': POLICY + '[repositories."chaobrain/ci-runners"]\nid = 9\nvisibility = "private"\n',
            'id as bool': POLICY.replace('id = 1001', 'id = true'),
            'id zero': POLICY.replace('id = 1001', 'id = 0'),
            'visibility': POLICY.replace('visibility = "public"', 'visibility = "internal"'),
            'paused as str': POLICY.replace('paused = true', 'paused = "yes"'),
            'bot_dispatch as str': POLICY.replace('id = 1001\n', 'id = 1001\nbot_dispatch = "yes"\n'),
            'bot_dispatch as int': POLICY.replace('id = 1001\n', 'id = 1001\nbot_dispatch = 1\n'),
            'paused as int': POLICY.replace('paused = true', 'paused = 1'),
            'public bot_dispatch': POLICY.replace('id = 2002\n', 'id = 2002\nbot_dispatch = true\n'),
            'public extra events': POLICY.replace('id = 2002\n', 'id = 2002\nextra_events = ["pull_request_target"]\n'),
            'extra event malformed': POLICY.replace('id = 1001\n', 'id = 1001\nextra_events = ["a b"]\n'),
            'extra event unchecked': POLICY.replace('id = 1001\n', 'id = 1001\nextra_events = ["issue_comment"]\n'),
            'extra event check_suite': POLICY.replace('id = 1001\n', 'id = 1001\nextra_events = ["check_suite"]\n'),
            'listed twice': POLICY + '[repositories."Chaobrain/Brainkit"]\nid = 9\nvisibility = "private"\n',
            'repo key': POLICY + '[repositories."brainkit"]\nid = 9\nvisibility = "private"\n',
            'repo unknown key': POLICY.replace('id = 1001\n', 'id = 1001\nactors = ["x"]\n'),
            'classes not a list': POLICY.replace('id = 1001\n', 'id = 1001\nclasses = "compile"\n'),
            'classes an open class': POLICY.replace('id = 1001\n', 'id = 1001\nclasses = ["part"]\n'),
            'classes unknown': POLICY.replace('id = 1001\n', 'id = 1001\nclasses = ["Compile"]\n'),
            'classes not str': POLICY.replace('id = 1001\n', 'id = 1001\nclasses = [1]\n'),
            'public classes': POLICY.replace('id = 2002\n', 'id = 2002\nclasses = ["nightly"]\n'),
            'not toml': 'schema = = 1',
            'not utf-8': b'\xff\xfe',
        }
        for what, text in bad.items():
            with self.assertRaises(policy.PolicyError, msg=what):
                policy.load(text)


    def test_the_org_example_loads(self):
        p = policy.load((ROOT / 'setup' / 'policy.org.example.toml').read_bytes())
        self.assertEqual(p.org, policy.Org('chaobrain', True, True, 60))


class Line(unittest.TestCase):
    def test_the_printed_line(self):
        self.assertEqual(policy.line(policy.Decision(False, 'actor'), 'ab' * 32),
                         'ci-runners: refused actor policy=abababababab')
        self.assertEqual(policy.line(policy.Decision(False, 'x\ny'), ''), 'ci-runners: refused unknown policy=none')


if __name__ == '__main__':
    unittest.main()
