"""Organization repository trust uses only controller-verified GitHub facts."""
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from ci_runners import github, hook, policy
from parallel import github_coordinator, protocol, transport


POLICY = policy.Policy(frozenset(), {})


def repository(name='Example/project', ident=11, visibility='private'):
    return {
        'id': ident, 'full_name': name, 'visibility': visibility,
        'private': visibility == 'private',
        'owner': {'login': 'Example', 'type': 'Organization'},
    }


def facts(event='push'):
    env = {
        'GITHUB_REPOSITORY': 'example/project', 'GITHUB_REPOSITORY_ID': '11',
        'GITHUB_EVENT_NAME': event, 'GITHUB_RUN_ID': '20', 'GITHUB_RUN_ATTEMPT': '1',
        'CI_RUNNER_CLASS': 'plan', 'CI_RUNNER_AUDIENCE': 'private',
    }
    return env, {'repository': repository()}


class OrganizationAdmission(unittest.TestCase):
    def setUp(self):
        self.github = github_coordinator.GitHub('unused')
        self.github.api = mock.Mock(return_value=repository())

    def test_private_repository_without_any_repository_or_actor_list(self):
        env, event = facts()
        decision = self.github.admit(POLICY, env, event, repository_members=True, organization='Example')
        self.assertEqual(decision, policy.Decision(True, 'ok'))
        self.github.api.assert_called_once_with('GET', '/repos/example/project')
        schedule, _ = facts('schedule')
        self.assertTrue(self.github.admit(POLICY, schedule, {}, organization='example').accepted)

    def test_checks_current_github_owner_visibility_and_id(self):
        env, event = facts()
        for item, reason in (
                (repository(visibility='public'), 'repository'),
                (repository(visibility='internal'), 'repository'),
                ({**repository(), 'private': False}, 'repository'),
                ({**repository(), 'owner': {'login': 'elsewhere', 'type': 'Organization'}}, 'repository'),
                ({**repository(), 'owner': {'login': 'example', 'type': 'User'}}, 'repository'),
                (repository(name='Example/renamed'), 'repository'),
                (repository(ident=99), 'repository-id'),
        ):
            with self.subTest(item=item):
                self.github.api.return_value = item
                self.assertEqual(self.github.admit(POLICY, env, event, organization='example').reason, reason)
        self.github.api.reset_mock()
        self.assertEqual(self.github.admit(POLICY, {**env, 'GITHUB_REPOSITORY': 'elsewhere/project'},
                                          event, organization='example').reason, 'repository')
        self.github.api.assert_not_called()
        self.github.api.side_effect = github.ApiError('HTTP 403')
        self.assertEqual(self.github.admit(POLICY, env, event, organization='example').reason, 'policy')

    def test_keeps_event_fork_and_local_pool_boundaries(self):
        env, event = facts('pull_request')
        same_repo = {'repo': event['repository']}
        fork = {'repo': repository(name='Example/fork', ident=77)}
        pr = {'base': same_repo, 'head': fork}
        self.assertEqual(self.github.admit(POLICY, env, {**event, 'pull_request': pr},
                                          organization='example').reason, 'fork')
        self.assertEqual(self.github.admit(POLICY, {**env, 'GITHUB_EVENT_NAME': 'pull_request_target'},
                                          event, organization='example').reason, 'event')
        env, event = facts()
        self.assertEqual(self.github.admit(POLICY, {**env, 'CI_RUNNER_CLASS': 'slurm'},
                                          event, organization='example').reason, 'runner-class')

    def test_slurm_claim_requires_authorized_current_private_repository_id(self):
        env, event = facts()
        env['CI_RUNNER_CLASS'] = 'slurm'
        for ident in (None, 99, True, '11'):
            with self.subTest(ident=ident):
                self.assertEqual(self.github.admit(POLICY, env, event, organization='example',
                                                   organization_slurm_repository_id=ident).reason, 'runner-class')
        self.assertTrue(self.github.admit(POLICY, env, event, organization='example',
                                         organization_slurm_repository_id=11).accepted)
        self.github.api.return_value = repository(visibility='public')
        self.assertEqual(self.github.admit(POLICY, env, event, organization='example',
                                          organization_slurm_repository_id=11).reason, 'repository')

    def test_source_still_verifies_the_run_without_actor_fields(self):
        known = repository()
        run = dict(run_attempt=1, repository=known, head_repository=known, event='push',
                   head_sha='a' * 40, head_branch='topic')
        source = dict(run_id=20, run_attempt=1, sha='a' * 40, project='project',
                      repository='example/project', context='test', concurrency_key='test')
        self.github.api.side_effect = [known, run]
        admitted = self.github.source(source, POLICY, organization='example')
        self.assertEqual(admitted['admission']['env']['GITHUB_REPOSITORY_ID'], '11')
        self.assertNotIn('GITHUB_ACTOR', admitted['admission']['env'])
        self.github.api.side_effect = [known, {**run, 'head_repository': repository(ident=98)}]
        with self.assertRaisesRegex(protocol.Error, 'run is not from this repository'):
            self.github.source(source, POLICY, organization='example')
        self.github.api.side_effect = [known, {**run, 'run_attempt': 2}]
        with self.assertRaisesRegex(protocol.Error, 'run attempt does not exist'):
            self.github.source(source, POLICY, organization='example')
        self.github.api.side_effect = [known, {**run, 'head_sha': 'b' * 40}]
        with self.assertRaisesRegex(protocol.Error, 'SHA is not the source run head'):
            self.github.source(source, POLICY, organization='example')

    def test_catalog_paginates_and_preserves_each_visibility(self):
        first = [repository(name=f'Example/project-{n}', ident=n+1) for n in range(100)]
        public = repository(name='Example/A-public', ident=101, visibility='public')
        internal = repository(name='Example/B-internal', ident=102, visibility='internal')
        self.github.api.side_effect = [first, [internal, public]]
        records = self.github.repositories('Example')
        self.assertEqual(len(records), 102)
        self.assertEqual(records[:2], [
            {'id': 101, 'full_name': 'Example/A-public', 'visibility': 'public'},
            {'id': 102, 'full_name': 'Example/B-internal', 'visibility': 'internal'},
        ])
        self.assertEqual(records[2]['visibility'], 'private')
        self.assertEqual(self.github.api.call_args_list, [
            mock.call('GET', '/orgs/example/repos?type=all&per_page=100&page=1'),
            mock.call('GET', '/orgs/example/repos?type=all&per_page=100&page=2'),
        ])

    def test_catalog_refuses_duplicate_or_out_of_scope_records(self):
        for page in (
                [repository(), repository(name='Example/duplicate-id')],
                [repository(), repository(name='example/PROJECT', ident=12)],
                [repository(name='outside/project')],
        ):
            with self.subTest(page=page):
                self.github.api.return_value = page
                with self.assertRaises(protocol.Error):
                    self.github.repositories('example')


class OrganizationHooks(unittest.TestCase):
    def test_plan_uses_controller_without_local_repo_and_omits_actors(self):
        env, event = facts()
        settings = {'organization': {'name': 'example'}, 'plan': {'socket': '/tmp/ci-fixture.sock'}}
        with mock.patch.object(transport, 'settings', return_value=settings), \
                mock.patch.object(transport.Client, 'call', return_value={'accepted': True, 'reason': 'ok'}) as call:
            approval = hook._started_decision('/tmp', POLICY, {**env, 'GITHUB_ACTOR': 'unused'},
                                              {**event, 'sender': {'login': 'unused'}})
            self.assertTrue(approval.accepted)
            call.assert_called_once_with('admit_plan', env=env, event=event)

    def test_executor_binds_scheduled_run_without_payload_repository(self):
        env, event = facts('schedule')
        execution = {**env, 'CI_RUNNER_CLASS': 'executor', 'RUNNER_NAME': 'fixture-1'}
        allocation = {
            'grant': {'runner_tag': 'fixture-1', 'backend': 'cpu'},
            'source': dict(repository='example/project', run_id=20, run_attempt=1,
                           admission={'env': env, 'event': event}),
            'organization_repository_verified': True,
        }
        with tempfile.TemporaryDirectory() as path, \
                mock.patch('ci_runners.inventory.load', return_value=mock.sentinel.inventory), \
                mock.patch('parallel.executor.paths', return_value=(None, '/tmp/ci-fixture.sock')), \
                mock.patch.object(transport.Client, 'call', return_value=allocation):
            (Path(path) / 'inventory.toml').touch()
            self.assertTrue(hook.central_decision(path, POLICY, execution, {}, organization='example').accepted)
            self.assertEqual(hook.central_decision(path, POLICY, {**execution, 'GITHUB_REPOSITORY_ID': '99'},
                                                    {}, organization='example').reason, 'repository-id')
            allocation['grant']['backend'] = 'slurm'
            self.assertEqual(hook.central_decision(path, POLICY, execution, {}, organization='example').reason,
                             'runner-class')
            allocation['organization_slurm_verified'] = True
            self.assertTrue(hook.central_decision(path, POLICY, execution, {}, organization='example').accepted)
            self.assertEqual(hook.central_decision(path, POLICY, {**execution, 'GITHUB_REPOSITORY_ID': '99'}, {},
                                                    organization='example').reason, 'repository-id')
            allocation['grant']['backend'] = 'cpu'
            allocation['repository_members_verified'] = allocation.pop('organization_repository_verified')
            self.assertEqual(hook.central_decision(path, POLICY, execution, {}, organization='example').reason,
                             'policy')
