"""The org snapshot (design §6.5, decision 11.2): policy schema 2's ``[org]`` table,
``with_snapshot``, the hook's use of ``org.json`` and ``ci-run org-sync``."""
from __future__ import annotations

import json
import threading
import sys
import subprocess
import http.server
import os
import pathlib
import stat
import tempfile
import contextlib
import io
import time
import unittest
import unittest.mock

from ci_runners import github, hook, orgsync, policy
from tests.test_policy import EVENTS, event, reason

NOW = 1_800_000_000.0
POLICY2 = """
schema = 2
actors = ["alice"]
[org]
name = "chaobrain"
members = true
private_repositories = true
max_age_minutes = 60
[repositories."chaobrain/paused-one"]
id = 3003
visibility = "private"
paused = true
[repositories."chaobrain/brainevent"]
id = 2002
visibility = "public"
"""
SNAP = {'schema': 1, 'org': 'chaobrain', 'fetched_at': NOW - 600, 'members': ['dave', 'eve'],
        'repositories': {'chaobrain/newproj': 5005, 'chaobrain/paused-one': 3003, 'chaobrain/brainevent': 2002,
                         'chaobrain/ci-runners': 9009}}


def snap(**kw) -> policy.Snapshot:
    return policy.load_snapshot(json.dumps({**SNAP, **kw}))


P2 = policy.load(POLICY2)
MERGED = policy.with_snapshot(P2, snap(), NOW)


def retarget(ev, name: str, rid: int):
    """``ev`` with every repository object of brainkit made ``name``/``rid``."""
    if isinstance(ev, dict):
        if ev.get('full_name') == 'chaobrain/brainkit':
            return {**ev, 'full_name': name, 'id': rid, 'name': name.split('/')[1]}
        return {k: retarget(v, name, rid) for k, v in ev.items()}
    return ev


def env(repo='chaobrain/newproj', rid='5005', event='push', actor='dave', cls='part'):
    return {'GITHUB_REPOSITORY': repo, 'GITHUB_REPOSITORY_ID': rid, 'GITHUB_EVENT_NAME': event,
            'GITHUB_ACTOR': actor, 'GITHUB_TRIGGERING_ACTOR': actor, 'CI_RUNNER_CLASS': cls}


def push(actor='dave', name='chaobrain/newproj', rid=5005):
    ev = retarget(event('push'), name, rid)
    ev['pusher']['name'] = ev['sender']['login'] = actor
    return ev


class Merge(unittest.TestCase):
    def test_a_member_pushes_to_a_private_repository_of_the_snapshot(self):
        self.assertEqual(reason(MERGED, env(), push()), 'ok')
        self.assertEqual(reason(MERGED, env(actor='alice'), push('alice')), 'ok')  # the hand list stays
        ev = retarget(event('pr_same_repo'), 'chaobrain/newproj', 5005)
        ev['sender']['login'] = ev['pull_request']['user']['login'] = 'eve'
        self.assertEqual(reason(MERGED, env(event='pull_request', actor='eve'), ev), 'ok')

    def test_members_act_only_in_the_snapshots_private_repositories(self):
        # a hand-listed public repository is never opened to members, and is refused anyway
        self.assertFalse(MERGED.repos['chaobrain/brainevent'].org_members)
        self.assertEqual(reason(MERGED, env('chaobrain/brainevent', '2002', actor='alice', cls='gpu'),
                                push('alice', 'chaobrain/brainevent', 2002)), 'repository')
        # a hand-listed private repository: open to members only if the snapshot holds it with its id
        text = POLICY2 + ('[repositories."chaobrain/handkept"]\nid = 6006\nvisibility = "private"\n'
                          '[repositories."chaobrain/moved"]\nid = 7007\nvisibility = "private"\n')
        snap2 = snap(repositories={**SNAP['repositories'], 'chaobrain/moved': 7008})
        m = policy.with_snapshot(policy.load(text), snap2, NOW)
        self.assertEqual(reason(m, env('chaobrain/handkept', '6006'), push(name='chaobrain/handkept', rid=6006)),
                         'actor')
        self.assertEqual(reason(m, env('chaobrain/moved', '7007'), push(name='chaobrain/moved', rid=7007)), 'actor')
        self.assertFalse(m.repos['chaobrain/handkept'].org_members)
        self.assertTrue(m.repos['chaobrain/newproj'].org_members)
        self.assertEqual(m.actors | m.members, frozenset({'alice', 'dave', 'eve'}))
        self.assertNotIn('chaobrain/nowhere', m.repos)
        snap3 = snap(repositories={**SNAP['repositories'], 'chaobrain/handkept': 6006})
        m = policy.with_snapshot(policy.load(text), snap3, NOW)
        self.assertTrue(m.repos['chaobrain/handkept'].org_members)
        self.assertEqual(reason(m, env('chaobrain/handkept', '6006'), push(name='chaobrain/handkept', rid=6006)), 'ok')
        self.assertFalse(m.repos['chaobrain/brainevent'].org_members)  # the snapshot lists it, but it is public

    def test_members_act_in_an_opted_in_public_repository(self):
        """Decision 16.7: an opted-in public repository's actors are the snapshot's members, on
        its own pool's GPU runners (18.7); its entry and id are the policy's (the snapshot
        holds private repositories only)."""
        text = POLICY2.replace('id = 2002\n', 'id = 2002\npublic_runners = true\n')
        private_only = {k: v for k, v in SNAP['repositories'].items() if k != 'chaobrain/brainevent'}
        m = policy.with_snapshot(policy.load(text), snap(repositories=private_only), NOW)
        self.assertTrue(m.repos['chaobrain/brainevent'].org_members)
        public = push('dave', 'chaobrain/brainevent', 2002)
        public['repository']['private'] = False
        e = env('chaobrain/brainevent', '2002', 'push', 'dave', 'gpu')
        e.update(CI_RUNNER_AUDIENCE='public', GITHUB_REF_PROTECTED='true')
        self.assertEqual(reason(m, e, public), 'ok')
        e.update(GITHUB_ACTOR='mallory', GITHUB_TRIGGERING_ACTOR='mallory')
        public['pusher']['name'] = public['sender']['login'] = 'mallory'
        self.assertEqual(reason(m, e, public), 'actor')
        # not opted in, or the snapshot says it is private now: the hand-kept actors alone
        for t, repos in ((POLICY2, private_only), (text, SNAP['repositories'])):
            m = policy.with_snapshot(policy.load(t), snap(repositories=repos), NOW)
            self.assertFalse(m.repos['chaobrain/brainevent'].org_members)
        # members off: the hand-kept actors alone
        m = policy.with_snapshot(policy.load(text.replace('members = true', 'members = false')),
                                 snap(repositories=private_only), NOW)
        self.assertFalse(m.repos['chaobrain/brainevent'].org_members)

    def test_without_the_merge_nothing_of_the_snapshot_counts(self):
        self.assertEqual(reason(P2, env(), push()), 'repository')
        self.assertEqual(reason(P2, env(repo='chaobrain/paused-one', rid='3003', actor='dave'),
                                push(name='chaobrain/paused-one', rid=3003)), 'paused')

    def test_non_members_outside_collaborators_and_bots_are_refused(self):
        for who in ('mallory', 'outside-collab'):
            self.assertEqual(reason(MERGED, env(actor=who), push(who)), 'actor', who)
        ev = push()
        ev['pusher']['name'] = 'mallory'  # a member's re-run of an outsider's push
        self.assertEqual(reason(MERGED, env(), ev), 'actor')
        bot = 'github-actions[bot]'
        self.assertEqual(reason(MERGED, env(event='workflow_dispatch', actor=bot),
                                retarget(event('dispatch_bot'), 'chaobrain/newproj', 5005)), 'bot')

    def test_a_fork_pull_request_is_refused(self):
        ev = retarget(event('pr_fork'), 'chaobrain/newproj', 5005)
        self.assertEqual(reason(MERGED, env(event='pull_request', actor='alice'), ev), 'fork')

    def test_public_repositories_stay_public_and_unlisted_ones_stay_out(self):
        self.assertEqual(MERGED.repos['chaobrain/brainevent'].visibility, 'public')
        self.assertEqual(reason(MERGED, env('chaobrain/brainevent', '2002'), push(name='chaobrain/brainevent', rid=2002)),
                         'repository')
        self.assertEqual(reason(MERGED, env('chaobrain/other', '7007'), push(name='chaobrain/other', rid=7007)),
                         'repository')
        self.assertEqual(reason(MERGED, env('chaobrain/ci-runners', '9009'), push(name='chaobrain/ci-runners', rid=9009)),
                         'self')
        self.assertNotIn('chaobrain/ci-runners', MERGED.repos)

    def test_the_snapshot_id_is_checked_against_the_name(self):
        self.assertEqual(reason(MERGED, env(rid='5006'), push()), 'repository-id')
        self.assertEqual(reason(MERGED, env(), push(rid=5006)), 'repository-id')

    def test_a_listed_repository_keeps_the_policy_entry(self):
        self.assertTrue(MERGED.repos['chaobrain/paused-one'].paused)
        self.assertEqual(reason(MERGED, env('chaobrain/paused-one', '3003'), push(name='chaobrain/paused-one', rid=3003)),
                         'paused')

    def test_the_exclusive_classes_need_the_lead_s_entry(self):
        """Decision 12.14: a repository the snapshot adds may not use compile or nightly; a
        listed entry that names them may, for the snapshot's members too."""
        for cls in ('compile', 'nightly'):
            self.assertEqual(reason(MERGED, env(cls=cls), push()), 'runner-class', cls)
        self.assertEqual(reason(MERGED, env(cls='multi'), push()), 'ok')
        listed = POLICY2 + '[repositories."chaobrain/newproj"]\nid = 5005\nvisibility = "private"\nclasses = ["nightly"]\n'
        m = policy.with_snapshot(policy.load(listed), snap(), NOW)
        self.assertEqual(reason(m, env(cls='nightly'), push()), 'ok')  # dave is a member of the snapshot
        self.assertEqual(reason(m, env(cls='compile'), push()), 'runner-class')

    def test_the_switches_are_honoured(self):
        p = policy.load(POLICY2.replace('members = true', 'members = false'))
        m = policy.with_snapshot(p, snap(), NOW)
        self.assertEqual(reason(m, env(), push()), 'actor')
        self.assertEqual(reason(m, env(actor='alice'), push('alice')), 'ok')
        p = policy.load(POLICY2.replace('private_repositories = true', 'private_repositories = false'))
        self.assertEqual(reason(policy.with_snapshot(p, snap(), NOW), env(), push()), 'repository')

    def test_stale_or_future_snapshots(self):
        policy.with_snapshot(P2, snap(fetched_at=NOW - 3600), NOW)  # at the bound: still fresh
        for t in (NOW - 3601, NOW + policy.CLOCK_SKEW + 1, 1.0):
            with self.assertRaises(policy.StaleSnapshot, msg=t):
                policy.with_snapshot(P2, snap(fetched_at=t), NOW)
        policy.with_snapshot(P2, snap(fetched_at=NOW + policy.CLOCK_SKEW), NOW)
        p = policy.load(POLICY2.replace('max_age_minutes = 60', 'max_age_minutes = 20'))
        with self.assertRaises(policy.StaleSnapshot):
            policy.with_snapshot(p, snap(fetched_at=NOW - 1201), NOW)

    def test_another_orgs_snapshot(self):
        other = snap(org='elsewhere', repositories={'elsewhere/x': 1})
        with self.assertRaises(policy.PolicyError):
            policy.with_snapshot(P2, other, NOW)

    def test_a_policy_without_org_is_unchanged(self):
        p = policy.load(POLICY2.split('[org]')[0]
                        + POLICY2.split('max_age_minutes = 60\n')[1])
        self.assertIs(policy.with_snapshot(p, snap(), NOW), p)

    def test_the_line_names_both_hashes(self):
        line = policy.line(policy.Decision(True, 'ok'), MERGED.digest, MERGED.snapshot_digest)
        self.assertRegex(line, r'\Aci-runners: accepted ok policy=[0-9a-f]{12} org=[0-9a-f]{12}\Z')
        self.assertEqual(policy.line(policy.Decision(False, 'stale'), 'ab' * 32),
                         'ci-runners: refused stale policy=abababababab')


class LoadOrg(unittest.TestCase):
    def test_malformed_org_tables(self):
        bad = {
            'not a table': POLICY2.replace('[org]\nname = "chaobrain"', 'org = 1\n[x]\nname = "chaobrain"'),
            'no name': POLICY2.replace('name = "chaobrain"\n', ''),
            'bad name': POLICY2.replace('name = "chaobrain"', 'name = "chao brain"'),
            'members as str': POLICY2.replace('members = true', 'members = "yes"'),
            'repos as int': POLICY2.replace('private_repositories = true', 'private_repositories = 1'),
            'age too small': POLICY2.replace('max_age_minutes = 60', 'max_age_minutes = 5'),
            'age too large': POLICY2.replace('max_age_minutes = 60', 'max_age_minutes = 2000'),
            'age as float': POLICY2.replace('max_age_minutes = 60', 'max_age_minutes = 60.0'),
            'age as bool': POLICY2.replace('max_age_minutes = 60', 'max_age_minutes = true'),
            'unknown key': POLICY2.replace('max_age_minutes = 60', 'max_age_minutes = 60\nall = true'),
        }
        for what, text in bad.items():
            with self.assertRaises(policy.PolicyError, msg=what):
                policy.load(text)

    def test_defaults(self):
        p = policy.load('schema = 2\n[org]\nname = "ChaoBrain"\n')
        self.assertEqual(p.org, policy.Org('chaobrain', False, False, 60))


class LoadSnapshot(unittest.TestCase):
    def test_malformed_snapshots(self):
        bad = {
            'not json': '{"schema": ',
            'not utf-8': b'\xff',
            'a list': '[]',
            'schema 2': {**SNAP, 'schema': 2},
            'schema as str': {**SNAP, 'schema': '1'},
            'schema as bool': {**SNAP, 'schema': True},
            'extra key': {**SNAP, 'all': True},
            'missing key': {k: v for k, v in SNAP.items() if k != 'members'},
            'time as str': {**SNAP, 'fetched_at': '1'},
            'time as bool': {**SNAP, 'fetched_at': True},
            'members as dict': {**SNAP, 'members': {'dave': 1}},
            'a bot member': {**SNAP, 'members': ['dave', 'dependabot[bot]']},
            'a malformed login': {**SNAP, 'members': ['dave\n']},
            'a non-ascii login': {**SNAP, 'members': ['dav\u212a']},
            'another org repository': {**SNAP, 'repositories': {'elsewhere/x': 1}},
            'a prefix org': {**SNAP, 'repositories': {'chaobrainx/x': 1}},
            'an id as str': {**SNAP, 'repositories': {'chaobrain/x': '1'}},
            'an id as bool': {**SNAP, 'repositories': {'chaobrain/x': True}},
            'an id zero': {**SNAP, 'repositories': {'chaobrain/x': 0}},
            'a bad name': {**SNAP, 'repositories': {'chaobrain/x y': 1}},
        }
        for what, doc in bad.items():
            data = doc if isinstance(doc, (str, bytes)) else json.dumps(doc)
            with self.assertRaises(policy.PolicyError, msg=what):
                policy.load_snapshot(data)

    def test_names_compare_without_case(self):
        s = policy.load_snapshot(json.dumps({**SNAP, 'members': ['Dave'], 'repositories': {'ChaoBrain/NewProj': 5}}))
        self.assertEqual((s.members, dict(s.repos)), (frozenset({'dave'}), {'chaobrain/newproj': 5}))
        self.assertEqual(len(s.digest), 64)


class Hook(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.etc = pathlib.Path(self.tmp.name) / 'etc'
        self.etc.mkdir()
        (self.etc / 'policy.toml').write_text(POLICY2)
        self.ev = pathlib.Path(self.tmp.name) / 'event.json'
        self.ev.write_text(json.dumps(push()))
        self.env = {**env(), 'CI_RUNNERS_ETC': str(self.etc), 'GITHUB_EVENT_PATH': str(self.ev)}

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, **kw):
        (self.etc / 'org.json').write_text(json.dumps({**SNAP, 'fetched_at': time.time() - 60, **kw}))

    def test_a_fresh_snapshot_accepts_a_member(self):
        self.write()
        code, line = hook.started(self.env)
        self.assertEqual(code, 0, line)
        self.assertRegex(line, r'\Aci-runners: accepted ok policy=[0-9a-f]{12} org=[0-9a-f]{12}\Z')
        code, line = hook.started({**self.env, 'GITHUB_ACTOR': 'mallory'})
        self.assertEqual((code, line.split()[2]), (1, 'actor'))

    def test_a_stale_missing_or_malformed_snapshot_refuses(self):
        self.write(fetched_at=time.time() - 3700)
        self.assertEqual(hook.started(self.env)[1].split()[1:3], ['refused', 'stale'])
        (self.etc / 'org.json').unlink()
        self.assertEqual(hook.started(self.env)[1].split()[1:3], ['refused', 'policy'])
        (self.etc / 'org.json').write_text('{"schema": 1')
        self.assertEqual(hook.started(self.env)[1].split()[1:3], ['refused', 'policy'])

    def test_an_oversized_snapshot_refuses(self):
        self.write()
        saved = hook.MAX_SNAPSHOT
        hook.MAX_SNAPSHOT = 100
        try:
            self.assertEqual(hook.started(self.env)[1].split()[1:3], ['refused', 'policy'])
        finally:
            hook.MAX_SNAPSHOT = saved

    def test_a_policy_without_org_reads_no_snapshot(self):
        (self.etc / 'policy.toml').write_text('schema = 2\nactors = ["dave"]\n[repositories."chaobrain/newproj"]\n'
                                              'id = 5005\nvisibility = "private"\n')
        (self.etc / 'org.json').write_text('not json')  # never read
        code, line = hook.started(self.env)
        self.assertEqual(code, 0, line)
        self.assertNotIn('org=', line)

    def test_claim_counts_no_member(self):
        """Decision 15.9: the claim counts the runners' statuses only. A member's green, even
        one the hook would accept, does not skip the head, and the claim reads no snapshot."""
        self.write()
        calls = []

        def call(method, url, token, body=None):
            calls.append((method, url))
            if method == 'GET':
                return [{'context': 'newproj/ci', 'state': 'success', 'creator': {'login': 'eve'},
                         'created_at': '2026-11-06T00:00:00Z', 'updated_at': '2026-11-06T00:00:00Z'}]
            return {}
        saved = dict(os.environ)
        try:
            for snapshot in (None, b' ' * ((4 << 20) + 1)):  # a fresh one, then one no reader would take
                if snapshot is not None:
                    (self.etc / 'org.json').write_bytes(snapshot)
                calls.clear()
                os.environ.update({'GITHUB_REPOSITORY': 'chaobrain/newproj', 'GITHUB_TOKEN': 't', 'GITHUB_OUTPUT': ''})
                with contextlib.redirect_stderr(io.StringIO()):
                    code = github.main(['--context', 'newproj/ci', '--sha', 'a' * 40], str(self.etc), call=call)
                self.assertEqual(code, 0)
                self.assertEqual([m for m, _ in calls], ['GET', 'POST'])  # eve's green ignored: run, post pending
        finally:
            os.environ.clear()
            os.environ.update(saved)


def api_pages(members, repos):
    """A fake ``github.api`` serving the two lists in pages of ``orgsync.PAGE``."""
    calls = []

    def call(method, url, token, body=None):
        calls.append(url)
        assert method == 'GET' and token == 'tok', (method, token)
        page = int(url.rsplit('page=', 1)[1])
        items = members if '/members?' in url else repos
        return items[(page - 1) * orgsync.PAGE:page * orgsync.PAGE]
    return call, calls


class Sync(unittest.TestCase):
    MEMBERS = [{'login': f'User{i}', 'type': 'User'} for i in range(150)] + [{'login': 'ci-app', 'type': 'Bot'}]
    REPOS = [{'full_name': 'chaobrain/Private1', 'id': 11, 'private': True},
             {'full_name': 'chaobrain/public1', 'id': 12, 'private': False},
             {'full_name': 'chaobrain/private2', 'id': 13, 'private': True},
             {'full_name': 'elsewhere/private3', 'id': 14, 'private': True}]

    def test_snapshot_of_every_page(self):
        call, calls = api_pages(self.MEMBERS, self.REPOS)
        doc = orgsync.fetch('chaobrain', 'tok', call, now=lambda: NOW)
        self.assertEqual(len(doc['members']), 150)  # both pages; the bot is not a member
        self.assertIn('user149', doc['members'])
        self.assertNotIn('ci-app', doc['members'])
        self.assertEqual(doc['repositories'], {'chaobrain/private1': 11, 'chaobrain/private2': 13})
        self.assertTrue(any('/orgs/chaobrain/repos?type=private&' in u for u in calls))
        self.assertTrue(any('/orgs/chaobrain/members?' in u for u in calls))  # members: no outside collaborators
        self.assertEqual(policy.load_snapshot(json.dumps(doc)).fetched_at, NOW)

    def test_strict_fields(self):
        with self.assertRaises(orgsync.SyncError):
            orgsync.snapshot('chaobrain', [{'login': 'dav\u212a', 'type': 'User'}], [], NOW)
        with self.assertRaises(orgsync.SyncError):
            orgsync.snapshot('chaobrain', self.MEMBERS, [{'full_name': 'chaobrain/\u212aa', 'id': 1, 'private': True}], NOW)
        doc = orgsync.snapshot('chaobrain', self.MEMBERS, [{'full_name': 'chaobrain/x', 'id': 1, 'private': 'yes'}], NOW)
        self.assertEqual(doc['repositories'], {})  # only a stated private counts
        clock = iter([NOW, NOW + 400])
        call, _ = api_pages(self.MEMBERS, self.REPOS)
        self.assertEqual(orgsync.fetch('chaobrain', 'tok', call, now=lambda: next(clock))['fetched_at'], NOW)

    def test_a_bad_fetch_raises(self):
        with self.assertRaises(orgsync.SyncError):
            orgsync.snapshot('chaobrain', [], self.REPOS, NOW)  # no members: keep the old snapshot
        with self.assertRaises(orgsync.SyncError):
            orgsync.snapshot('chaobrain', [{'type': 'User'}], [], NOW)
        with self.assertRaises(orgsync.SyncError):
            orgsync.snapshot('chaobrain', self.MEMBERS, [{'full_name': 'chaobrain/x', 'private': True}], NOW)
        with self.assertRaises(orgsync.SyncError):
            orgsync.pages(lambda *a: {'message': 'x'}, 'https://x/orgs/o/members?per_page=100', 't')
        full = lambda *a: [{}] * orgsync.PAGE  # noqa: E731 - an endless list
        with self.assertRaises(orgsync.SyncError):
            orgsync.pages(full, 'https://x/orgs/o/members?per_page=100', 't')

    def test_main_writes_the_snapshot_and_keeps_it_on_failure(self):
        with tempfile.TemporaryDirectory() as d:
            (pathlib.Path(d) / 'policy.toml').write_text(POLICY2)
            (pathlib.Path(d) / 'orgsync.token').write_text('tok\n')
            call, _ = api_pages(self.MEMBERS, self.REPOS)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(orgsync.main(d, call, now=lambda: NOW), 0)
            path = pathlib.Path(d) / 'org.json'
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            before = path.read_bytes()
            self.assertEqual(len(policy.load_snapshot(before).members), 150)

            def failing(method, url, token, body=None):
                if '/repos?' in url:
                    raise github.ApiError('GET repos: HTTP 502')
                return call(method, url, token)
            self.assertEqual(orgsync.main(d, failing, now=lambda: NOW + 600), 1)
            self.assertEqual(path.read_bytes(), before)  # a partial fetch writes nothing
            self.assertEqual(sorted(os.listdir(d)), ['org.json', 'orgsync.token', 'policy.toml'])
            (pathlib.Path(d) / 'orgsync.token').write_text('two words\n')
            self.assertEqual(orgsync.main(d, call, now=lambda: NOW), 1)
            (pathlib.Path(d) / 'orgsync.token').write_text('\n')
            self.assertEqual(orgsync.main(d, call, now=lambda: NOW), 1)
            (pathlib.Path(d) / 'orgsync.token').write_bytes(b'tok\xff\n')
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(orgsync.main(d, call, now=lambda: NOW), 1)
            self.assertIn('orgsync.token is empty or malformed', err.getvalue())
            self.assertNotIn('xff', err.getvalue())

    def test_main_without_org_does_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            (pathlib.Path(d) / 'policy.toml').write_text('schema = 2\n')
            self.assertEqual(orgsync.main(d, lambda *a: self.fail('no call')), 0)
            self.assertFalse((pathlib.Path(d) / 'org.json').exists())

    def test_write_does_not_follow_a_planted_link(self):
        with tempfile.TemporaryDirectory() as d:
            victim = pathlib.Path(d) / 'victim'
            victim.write_text('keep')
            os.symlink(victim, pathlib.Path(d) / f'org.json.{os.getpid()}.tmp')  # planted under our next name
            os.symlink(victim, pathlib.Path(d) / 'org.json.1.tmp')  # left by a killed run
            orgsync.write(d, {'x': 1})
            self.assertEqual(victim.read_text(), 'keep')
            self.assertTrue(stat.S_ISREG(os.lstat(pathlib.Path(d) / 'org.json').st_mode))
            self.assertEqual(sorted(os.listdir(d)), ['org.json', 'victim'])
        with tempfile.TemporaryDirectory() as d:  # a link raced in after the clean-up is refused
            victim = pathlib.Path(d) / 'victim'
            victim.write_text('keep')
            real = os.listdir

            def racing(path):
                names = real(path)
                os.symlink(victim, pathlib.Path(d) / f'org.json.{os.getpid()}.tmp')
                return [n for n in names if n != f'org.json.{os.getpid()}.tmp']
            with unittest.mock.patch.object(orgsync.os, 'listdir', racing):
                with self.assertRaises(OSError):
                    orgsync.write(d, {'x': 1})
            self.assertEqual(victim.read_text(), 'keep')


class Push(unittest.TestCase):
    """The snapshot pushed from the workstation (decision 11.9): ``--fetch`` prints it with
    ``GH_TOKEN``; ``--receive`` writes only a current snapshot of the policy's org."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.etc = pathlib.Path(self.tmp.name)
        (self.etc / 'policy.toml').write_text(POLICY2)

    def tearDown(self):
        self.tmp.cleanup()

    def doc(self, **kw):
        return json.dumps({**SNAP, **kw}).encode()

    def receive(self, data: bytes, now=NOW):
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            rc = orgsync.receive(str(self.etc), io.BytesIO(data), now=lambda: now)
        return rc, err.getvalue()

    def test_fetch_prints_what_receive_writes(self):
        call, calls = api_pages(Sync.MEMBERS, Sync.REPOS)
        out = io.StringIO()
        self.assertEqual(orgsync.fetch_out('chaobrain', {'GH_TOKEN': 'tok\n'}, out, call, now=lambda: NOW), 0)
        self.assertEqual(json.loads(out.getvalue())['repositories'], {'chaobrain/private1': 11, 'chaobrain/private2': 13})
        self.assertEqual(self.receive(out.getvalue().encode())[0], 0)
        written = policy.load_snapshot((self.etc / 'org.json').read_bytes())
        self.assertEqual((len(written.members), written.fetched_at), (150, NOW))
        self.assertEqual(stat.S_IMODE((self.etc / 'org.json').stat().st_mode), 0o600)

    def test_fetch_needs_a_token_and_an_org_name(self):
        for environ, org in (({}, 'chaobrain'), ({'GH_TOKEN': 'a b'}, 'chaobrain'),
                             ({'GH_TOKEN': 'tok', 'GITHUB_API_URL': 'http://evil.example'}, 'chaobrain'),
                             ({'GH_TOKEN': 'tok'}, 'chaobrain/x'), ({'GH_TOKEN': 'tok'}, '-x')):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = orgsync.fetch_out(org, environ, out, lambda *a: self.fail('no call'), now=lambda: NOW)
            self.assertEqual((rc, out.getvalue()), (1, ''), (environ, org))
            self.assertNotIn('a b', err.getvalue())

    def test_receive_refuses_and_keeps_the_old_snapshot(self):
        self.assertEqual(self.receive(self.doc())[0], 0)
        before = (self.etc / 'org.json').read_bytes()
        for data, why in ((self.doc(org='elsewhere', repositories={'elsewhere/x': 1}), "org 'elsewhere'"),
                          (self.doc(fetched_at=NOW + policy.CLOCK_SKEW + 1), 'from the future'),
                          (self.doc(fetched_at=NOW - 601), 'older than the one in place'),
                          (self.doc(members=['bot[bot]']), 'not a human login'),
                          (b'{not json', 'not JSON'),
                          (b' ' * (orgsync.MAX_SNAPSHOT + 1), 'larger than')):
            rc, err = self.receive(data)
            self.assertEqual(rc, 1, why)
            self.assertIn(why, err)
            self.assertIn('the old snapshot stays', err)
            self.assertEqual((self.etc / 'org.json').read_bytes(), before, why)
        self.assertEqual(self.receive(self.doc(fetched_at=NOW - 600))[0], 0)  # the same one again
        self.assertEqual(stat.S_IMODE((self.etc / 'org.json.lock').stat().st_mode), 0o600)
        self.assertEqual(self.receive(self.doc(fetched_at=NOW - 60, members=['dave']))[0], 0)  # a newer one
        self.assertEqual(policy.load_snapshot((self.etc / 'org.json').read_bytes()).members, {'dave'})

    def test_the_clock_skew_bound(self):
        self.assertEqual(self.receive(self.doc(fetched_at=NOW + policy.CLOCK_SKEW))[0], 0)

    def test_receives_take_turns(self):
        import fcntl
        fd = os.open(self.etc / 'org.json.lock', os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)  # another receive holds the lock
        done = []
        th = threading.Thread(target=lambda: done.append(self.receive(self.doc())[0]))
        th.start()
        th.join(0.5)
        self.assertEqual(done, [])  # it waits
        self.assertFalse((self.etc / 'org.json').exists())
        os.close(fd)
        th.join(10)
        self.assertEqual(done, [0])

    def test_a_broken_snapshot_in_place_is_replaced(self):
        (self.etc / 'org.json').write_text('{broken')
        self.assertEqual(self.receive(self.doc())[0], 0)

    def test_receive_needs_an_org_policy(self):
        (self.etc / 'policy.toml').write_text('schema = 2\n')
        rc, err = self.receive(self.doc())
        self.assertEqual(rc, 1)
        self.assertIn('no [org] table', err)
        self.assertFalse((self.etc / 'org.json').exists())

    def test_cli_forms(self):
        with unittest.mock.patch.object(orgsync, 'receive', return_value=7) as r:
            self.assertEqual(orgsync.cli(['--receive'], '/e'), 7)
        self.assertEqual(r.call_args[0][0], '/e')
        with unittest.mock.patch.object(orgsync, 'fetch_out', return_value=5) as f:
            self.assertEqual(orgsync.cli(['--fetch', '--org', 'chaobrain'], '/e'), 5)
        self.assertEqual(f.call_args[0][0], 'chaobrain')
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(orgsync.cli(['--fetch'], '/e'), 2)


class PushScript(unittest.TestCase):
    """setup/workstation/orgsync-push.sh: the login read from gh for the fetch only, the
    snapshot piped to --receive on the host, each argument one plain word."""
    ROOT = pathlib.Path(__file__).resolve().parents[1]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = pathlib.Path(self.tmp.name)
        self.bin = t / 'bin'
        self.bin.mkdir()
        (self.bin / 'gh').write_text('#!/bin/sh\n[ "$*" = "auth token" ] && echo tok-xyz\n')
        # the host: records its command line, then runs --receive into a local etc
        self.etc = t / 'data' / 'ci-runners' / 'etc'
        self.etc.mkdir(parents=True)
        (self.etc / 'policy.toml').write_text(POLICY2)
        rel = t / 'data' / 'ci-runners' / 'releases'
        rel.mkdir()
        os.symlink(self.ROOT, rel / 'hook')
        (self.bin / 'ssh').write_text(f'#!/bin/sh\nfor a; do host=$last; last=$a; done\n'
                                      f'echo "$host" >> {t}/ssh.hosts\n'
                                      f'case $host in bad) exit 255 ;; hang) exec sleep 60 ;; esac\n'
                                      f'printf "%s\\n" "$@" > {t}/ssh.args\n'
                                      'env | grep -c tok-xyz > ' + f'{t}/ssh.token\nexec sh -c "$last"\n')
        for f in ('gh', 'ssh'):
            (self.bin / f).chmod(0o755)
        self.t = t

    def tearDown(self):
        self.tmp.cleanup()

    def run_script(self, *args, api='', **extra):
        env = {'PATH': f'{self.bin}:/usr/bin:/bin', 'HOME': self.tmp.name, 'LANG': 'C.UTF-8', **extra}
        if api:
            env['GITHUB_API_URL'] = api
        return subprocess.run([str(self.ROOT / 'setup' / 'workstation' / 'orgsync-push.sh'), *args], env=env,
                              capture_output=True, text=True, timeout=60)

    def serve(self, seen):
        members = [{'login': 'Dave', 'type': 'User'}]
        repos = [{'full_name': 'chaobrain/p1', 'id': 7, 'private': True}]

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.headers.get('Authorization'))
                body = json.dumps(members if '/members?' in self.path else repos).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return f'http://127.0.0.1:{srv.server_address[1]}'

    def test_each_host_in_turn_bounded_on_its_own(self):
        """Decision 12.4: one fetch, then each host; a failed or hung host does not hold up
        the next one, and the run says it failed."""
        seen = []
        api = self.serve(seen)
        data = str(self.t / 'data')
        t0 = time.monotonic()
        r = self.run_script('chaobrain', 'bad', data, sys.executable, 'hang', data, sys.executable,
                            'good', data, sys.executable, api=api, PUSH_TIMEOUT='2')
        self.assertLess(time.monotonic() - t0, 30)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertEqual((self.t / 'ssh.hosts').read_text().split(), ['bad', 'hang', 'good'])
        self.assertIn('good: pushed', r.stdout)
        self.assertIn('bad: failed', r.stderr)
        self.assertIn('hang: failed', r.stderr)
        self.assertEqual(len(seen), 2)  # fetched once (members and repositories), not per host
        self.assertTrue((self.etc / 'org.json').exists())
        r = self.run_script('chaobrain', 'good', data, sys.executable, 'good', data, sys.executable, api=api)
        self.assertEqual(r.returncode, 0, r.stderr)
        for bad in (('chaobrain', 'h', data, sys.executable, 'h2'), ('chaobrain', 'h', data, sys.executable, 'h2', data)):
            self.assertEqual(self.run_script(*bad, api=api).returncode, 2, bad)  # three words per host

    def test_the_snapshot_reaches_receive(self):
        members = [{'login': 'Dave', 'type': 'User'}]
        repos = [{'full_name': 'chaobrain/p1', 'id': 7, 'private': True}]
        seen = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.headers.get('Authorization'))
                body = json.dumps(members if '/members?' in self.path else repos).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            r = self.run_script('chaobrain', 'h100-alias', str(self.t / 'data'), sys.executable,
                                api=f'http://127.0.0.1:{srv.server_address[1]}')
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('1 members, 1 private repositories', r.stdout)
        snap = policy.load_snapshot((self.etc / 'org.json').read_bytes())
        self.assertEqual((snap.members, dict(snap.repos)), ({'dave'}, {'chaobrain/p1': 7}))
        self.assertEqual(set(seen), {'Bearer tok-xyz'})
        args = (self.t / 'ssh.args').read_text().splitlines()
        self.assertEqual(args[:-1], ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=20', '-o', 'ServerAliveInterval=15',
                                     'h100-alias'])
        self.assertTrue(args[-1].endswith('/ci-runners/releases/hook/bin/ci-run org-sync --receive'), args[-1])
        self.assertEqual((self.t / 'ssh.token').read_text().strip(), '0')  # the login stays on the workstation

    def test_arguments_are_plain_words(self):
        for args in (('chaobrain', '-F/tmp/x', '/d', '/p'), ('chaobrain', 'h;x', '/d', '/p'), ('chaobrain', 'h', 'd', '/p'), ('chaobrain', 'h', '/d', 'p'),
                     ('chaobrain', 'h', '/d $(id)', '/p'), ('chaobrain', 'h', '/d')):
            r = self.run_script(*args, api='http://127.0.0.1:9')  # nothing leaves the machine
            self.assertEqual(r.returncode, 2, args)
            self.assertFalse((self.t / 'ssh.args').exists(), args)

    def test_a_failed_fetch_sends_nothing(self):
        r = self.run_script('chaobrain', 'h', str(self.t / 'data'), sys.executable, api='http://127.0.0.1:9')
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse((self.t / 'ssh.args').exists())

if __name__ == '__main__':
    unittest.main()
