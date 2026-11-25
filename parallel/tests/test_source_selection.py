"""Dispatch overrides preserve strict defaults and authoritative source provenance."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from ci_runners import policy
from parallel import github_coordinator, protocol, source_selection


def repo(ident=11, name='example/project'):
    return dict(id=ident, full_name=name, private=True, visibility='private',
                owner=dict(login='example', type='Organization'))


class SourceSelection(unittest.TestCase):
    def setUp(self):
        self.github = github_coordinator.GitHub('unused')
        self.request = dict(repository='example/project', project='project', sha='b'*40,
                            run_id=20, run_attempt=1, context='project/ci', concurrency_key='ignored',
                            test_source=dict(kind='pull-request', number=7))
        self.run = dict(run_attempt=1, repository=repo(), head_repository=repo(), event='workflow_dispatch',
                        head_sha='a'*40, head_branch='main', actor=dict(login='alice'),
                        triggering_actor=dict(login='bob'))
        self.pr = dict(number=7, head=dict(sha='b'*40, repo=repo()), base=dict(repo=repo()))
        self.enabled = {11: dict(repository='example/project', kinds=['pull-request', 'commit-replay'])}
        self.calls = []

        def api(method, path):
            self.calls.append(path)
            if path == '/repos/example/project':
                return repo()
            if '/actions/runs/' in path:
                return deepcopy(self.run)
            if path.endswith('/permission'):
                login = path.split('/')[-2]
                return dict(permission='write', user=dict(login=login, type='User'))
            if path.endswith('/pulls/7'):
                return deepcopy(self.pr)
            if '/git/commits/' in path:
                return dict(sha=path.rsplit('/', 1)[-1])
            if '/compare/' in path:
                return dict(status='ahead', merge_base_commit=dict(sha='c'*40))
            raise AssertionError(path)
        self.api = api
        self.github.api = api

    def admit(self, **kw):
        return self.github.source(self.request, None, organization='example',
                                  source_selectors=self.enabled, **kw)

    def test_authorized_pr_dispatch_keeps_both_source_and_test_sha(self):
        admitted = self.admit()
        self.assertEqual(admitted['sha'], 'b'*40)
        self.assertEqual(admitted['run_head_sha'], 'a'*40)
        self.assertEqual(admitted['concurrency_key'], 'pr:7')
        self.assertEqual(admitted['source_selection']['number'], 7)
        self.assertEqual([a['login'] for a in admitted['source_selection']['actors']], ['alice', 'bob'])
        self.assertEqual(admitted['admission']['env']['GITHUB_EVENT_NAME'], 'workflow_dispatch')
        self.assertNotIn('pull_request', admitted['admission']['event'])

    def test_run_head_default_is_not_weakened_or_actor_expanded(self):
        del self.request['test_source']
        with self.assertRaisesRegex(protocol.Error, 'source run head'):
            self.admit()
        self.request['sha'] = 'a'*40
        self.assertEqual(self.admit()['concurrency_key'], 'branch:main')
        self.assertFalse(any(p.endswith('/permission') for p in self.calls))

    def test_override_is_opt_in_to_exact_repository_id_and_name(self):
        for enabled in ({}, {11: dict(repository='example/other', kinds=['pull-request'])},
                        {12: self.enabled[11]}, {11: dict(repository='example/project', kinds=['commit-replay'])}):
            with self.subTest(enabled=enabled), self.assertRaises(protocol.Error):
                self.github.source(self.request, None, organization='example', source_selectors=enabled)

    def test_original_and_triggering_actor_each_require_human_write(self):
        for field in ('actor', 'triggering_actor'):
            for actor in (None, dict(login='dependabot[bot]'), dict(login='other[bot]')):
                original = self.run[field]
                self.run[field] = actor
                with self.subTest(field=field, actor=actor), self.assertRaises(protocol.Error):
                    self.admit()
                self.run[field] = original
        for denied in ('alice', 'bob'):
            for access in (dict(permission='read', user=dict(login=denied, type='User')),
                           dict(permission='admin', user=dict(login='impostor', type='User')),
                           dict(permission='admin', user=dict(login=denied, type='Bot'))):
                self.github.api = lambda m, p: access if f'/{denied}/permission' in p else self.api(m, p)
                with self.subTest(denied=denied, access=access), self.assertRaises(protocol.Error):
                    self.admit()
        self.github.api = self.api

    def test_fork_wrong_or_moved_pr_head_and_source_event_are_refused(self):
        original = deepcopy(self.pr)
        for side in ('base', 'head'):
            self.pr[side]['repo'] = repo(12, 'example/fork')
            with self.subTest(side=side), self.assertRaises(protocol.Error):
                self.admit()
            self.pr = deepcopy(original)
        self.pr['head']['sha'] = 'd'*40
        with self.assertRaises(protocol.Error):
            self.admit()
        self.pr = original
        for event in ('push', 'schedule', 'pull_request', 'pull_request_target'):
            self.run['event'] = event
            with self.subTest(event=event), self.assertRaises(protocol.Error):
                self.admit()

    def test_replay_resolves_exact_commit_and_ancestor_without_retargeting(self):
        self.request.update(test_source=dict(kind='commit-replay'), range_base='c'*40)
        admitted = self.admit()
        self.assertEqual(admitted['sha'], 'b'*40)
        self.assertEqual(admitted['source_selection']['range_base'], 'c'*40)
        self.assertTrue(admitted['concurrency_key'].startswith('replay:'))
        for corrupt in (dict(status='behind', merge_base_commit=dict(sha='c'*40)),
                        dict(status='ahead', merge_base_commit=dict(sha='d'*40))):
            self.github.api = lambda m, p: corrupt if '/compare/' in p else self.api(m, p)
            with self.subTest(corrupt=corrupt), self.assertRaises(protocol.Error):
                self.admit()
        self.github.api = lambda m, p: dict(sha='d'*40) if '/git/commits/' in p else self.api(m, p)
        with self.assertRaises(protocol.Error):
            self.admit()

    def test_selector_types_and_range_cannot_smuggle_overrides(self):
        for value in (None, [], {}, dict(kind='other'), dict(kind='pull-request', number=True),
                      dict(kind='pull-request', number='7'), dict(kind='commit-replay', repository='other/repo')):
            with self.subTest(value=value), self.assertRaises(protocol.Error):
                source_selection.selector(dict(test_source=value))
        for value in ('c'*7, True, None):
            with self.subTest(base=value), self.assertRaises(protocol.Error):
                source_selection.selector(dict(test_source=dict(kind='commit-replay'), range_base=value))
        with self.assertRaises(protocol.Error):
            source_selection.selector(dict(range_base='c'*40))

    def test_private_policy_types_duplicates_and_exact_binding(self):
        document = dict(schema=1, repositories=[dict(id=11, repository='Example/project', kinds=['pull-request'])])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'selectors.json'
            def read(doc):
                path.write_text(json.dumps(doc))
                path.chmod(0o600)
                return source_selection.read_policy(path)
            self.assertEqual(read(document)[11]['repository'], 'example/project')
            self.assertEqual(source_selection.read_policy(None), {})
            for bad in (dict(schema=True, repositories=[]),
                        dict(schema=1, repositories=document['repositories'] * 2),
                        dict(schema=1, repositories=[dict(id=True, repository='example/project', kinds=['pull-request'])]),
                        dict(schema=1, repositories=[dict(id=11, repository='example/project', kinds=['pull-request']*2)])):
                with self.subTest(document=bad), self.assertRaises(protocol.Error):
                    read(bad)
