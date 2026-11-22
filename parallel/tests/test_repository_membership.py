"""Schema 2 actor admission with a fake GitHub API and fake control replies."""
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from ci_runners import github, hook, policy
from parallel import github_coordinator, protocol, transport


REPOSITORIES = {
    path: {'id': identifier, 'full_name': path, 'private': True}
    for path, identifier in (('example/project', 11), ('example/other', 12))
}
POLICY = policy.Policy(frozenset({'old-login'}), {
    name: policy.Repo(name, repository['id'], 'private')
    for name, repository in REPOSITORIES.items()
})


def push(repo='example/project', actor='member', triggering_actor=None):
    env = {
        'GITHUB_ACTOR': actor, 'GITHUB_TRIGGERING_ACTOR': triggering_actor or actor,
        'GITHUB_EVENT_NAME': 'push', 'GITHUB_REPOSITORY': repo,
        'GITHUB_REPOSITORY_ID': str(REPOSITORIES[repo]['id']), 'GITHUB_RUN_ID': '20',
        'GITHUB_RUN_ATTEMPT': '1', 'CI_RUNNER_CLASS': 'plan', 'CI_RUNNER_AUDIENCE': 'private',
    }
    event = {'repository': REPOSITORIES[repo], 'sender': {'login': actor}, 'pusher': {'name': actor}}
    return env, event


class ControllerAdmission(unittest.TestCase):
    def setUp(self):
        self.api = github_coordinator.GitHub('unused')
        self.permissions = {('example/project', 'member'): 'read'}
        self.api.api = mock.Mock(side_effect=self.response)

    def response(self, method, path):
        self.assertEqual(method, 'GET')
        fields = path.split('/')
        repo = '/'.join(fields[2:4])
        if len(fields) == 4:
            return REPOSITORIES[repo]
        login = fields[5]
        return {'permission': self.permissions.get((repo, login), 'none'), 'user': {'login': login}}

    def admit(self, **kwargs):
        return self.api.admit(POLICY, *push(**kwargs), repository_members=True)

    def test_reads_effective_permission_for_each_repository_without_static_fallback(self):
        self.assertTrue(self.admit().accepted)
        self.assertEqual(self.api.api.call_count, 2)
        self.assertEqual(self.admit(repo='example/other').reason, 'actor')
        self.assertEqual(self.admit(actor='old-login').reason, 'actor')
        for permission in ('write', 'admin'):
            self.permissions['example/project', 'member'] = permission
            self.assertTrue(self.admit().accepted)
        self.permissions['example/project', 'member'] = 'none'
        self.assertEqual(self.admit().reason, 'actor')

    def test_original_actor_and_rerun_actor_are_both_checked(self):
        self.assertFalse(self.admit(triggering_actor='another').accepted)
        self.permissions['example/project', 'another'] = 'write'
        self.assertTrue(self.admit(triggering_actor='another').accepted)
        self.permissions['example/project', 'member'] = 'none'
        self.assertFalse(self.admit(triggering_actor='another').accepted)

    def test_wrong_repository_privacy_and_unverified_people_are_refused(self):
        repository = REPOSITORIES['example/project']
        for changed in ({'private': False}, {'id': 99}):
            with self.subTest(changed=changed):
                self.api.api.side_effect = None
                self.api.api.return_value = {**repository, **changed}
                self.assertEqual(self.admit().reason, 'repository')
        self.api.api.side_effect = [repository, {'permission': 'write', 'user': {'login': 'different'}}]
        self.assertEqual(self.admit().reason, 'actor')
        self.api.api.side_effect = github.ApiError('HTTP 403')
        self.assertEqual(self.admit().reason, 'policy')

    def test_bad_event_and_repository_id_are_refused_without_contacting_github(self):
        env, event = push()
        self.assertEqual(self.api.admit(POLICY, {**env, 'GITHUB_REPOSITORY_ID': '99'}, event,
                                        repository_members=True).reason, 'repository-id')
        pr = {'head': {'repo': REPOSITORIES['example/other']}, 'base': {'repo': event['repository']},
              'user': {'login': 'member'}}
        self.assertEqual(self.api.admit(POLICY, {**env, 'GITHUB_EVENT_NAME': 'pull_request'},
                                        {**event, 'pull_request': pr}, repository_members=True).reason, 'fork')
        self.api.api.assert_not_called()

    def test_source_uses_effective_permissions_before_submitting(self):
        repo = REPOSITORIES['example/project']
        run = dict(run_attempt=1, repository=repo, head_repository=repo, event='push',
                   actor={'login': 'member'}, triggering_actor={'login': 'member'},
                   head_sha='a' * 40, head_branch='topic')
        request = dict(run_id=20, run_attempt=1, sha='a' * 40, project='project',
                       repository='example/project', context='test', concurrency_key='test')
        self.api.api.side_effect = [run, repo, {'permission': 'read', 'user': {'login': 'member'}}]
        self.assertEqual(self.api.source(request, POLICY, repository_members=True)['concurrency_key'], 'branch:topic')
        self.api.api.side_effect = [run, repo, {'permission': 'none', 'user': {'login': 'member'}}]
        with self.assertRaisesRegex(protocol.Error, 'source admission refused: actor'):
            self.api.source(request, POLICY, repository_members=True)


class RunnerAdmission(unittest.TestCase):
    def test_plan_hook_calls_local_gate_and_requires_a_valid_reply(self):
        env, event = push()
        with mock.patch.object(transport, 'settings', return_value={
                'repository_members': True, 'plan': {'socket': '/tmp/ci-fixture.sock'}}), \
                mock.patch.object(transport.Client, 'call', return_value={'accepted': True, 'reason': 'ok'}) as call:
            self.assertTrue(hook._started_decision('/tmp', POLICY, env, event).accepted)
            call.assert_called_once_with('admit_plan', env=env, event=event)
            call.return_value = {'accepted': False, 'reason': 'actor'}
            self.assertEqual(hook._started_decision('/tmp', POLICY, env, event).reason, 'actor')
            call.return_value = {'accepted': True, 'reason': 'actor'}
            self.assertEqual(hook._started_decision('/tmp', POLICY, env, event).reason, 'policy')

    def test_executor_uses_verified_claim_without_local_repository_id(self):
        env, event = push()
        execution = {**env, 'CI_RUNNER_CLASS': 'executor', 'RUNNER_NAME': 'fixture-1'}
        allocation = {
            'grant': {'runner_tag': 'fixture-1', 'backend': 'cpu'},
            'source': dict(repository='example/project', run_id=20, run_attempt=1,
                           admission={'env': env, 'event': event}),
            'repository_members_verified': True,
        }
        empty = policy.Policy(frozenset(), {})
        with tempfile.TemporaryDirectory() as path, \
                mock.patch('ci_runners.inventory.load', return_value=mock.sentinel.inventory), \
                mock.patch('parallel.executor.paths', return_value=(None, '/tmp/ci-fixture.sock')), \
                mock.patch.object(transport.Client, 'call', return_value=allocation):
            (Path(path) / 'inventory.toml').touch()
            self.assertTrue(hook.central_decision(path, empty, execution, event, repository_members=True).accepted)
            self.assertFalse(hook.central_decision(path, empty, {**execution, 'GITHUB_REPOSITORY_ID': '99'},
                                                   event, repository_members=True).accepted)
            del allocation['repository_members_verified']
            self.assertFalse(hook.central_decision(path, empty, execution, event, repository_members=True).accepted)
