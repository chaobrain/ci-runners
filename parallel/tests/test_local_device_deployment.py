"""Schema 2 deployment from independent, per-host inputs."""
from contextlib import redirect_stdout
from io import StringIO
import tomllib
from unittest import mock

from parallel import deploy, deployment_credentials, deployment_output, deployment_plan
from parallel import deployment_files as files
from parallel import protocol
from parallel.tests.test_deployment import DeploymentFixture


class LocalDeviceDeployment(DeploymentFixture):
    def controller(self, *, executor=False):
        self.document['schema'] = 2
        host = self.document['hosts']['3090a']
        if executor:
            host.update(runner_count=1, priority=15)
        else:
            host['roles'] = ['controller']
        self.project.pop('actors', None)
        self.project.pop('repository_id', None)
        self.write_inputs()
        files.atomic_write(self.input/'device-admission.toml', '[devices]\n')

    def executor(self):
        self.document['schema'] = 2
        self.document['controller'].pop('bind', None)
        host = self.document['hosts'].pop('3090a')
        host.update(roles=['executor'], runner_count=1, priority=25)
        self.document['hosts']['3090b'] = host
        self.project.update(devices=['3090b'])
        self.project.pop('actors', None)
        self.project.pop('repository_id', None)
        self.write_inputs()
        (self.input/'credentials/pat/github-pat').unlink()
        files.atomic_write(self.input/'credentials/hosts/3090b', 'executor-device-token\n')
        files.atomic_write(self.input/'credentials/tls/ca.crt', 'controller-ca\n')
        return self.config('3090b')

    def read_rendered(self, plan, path):
        return tomllib.loads(plan['files'][str(path)]['content'])

    def test_controller_only_preserves_waiter_lanes_without_remote_topology(self):
        self.controller()
        config = self.config()
        self.assertEqual(list(config.inventory_document()['hosts']), ['3090a'])
        self.assertEqual(len(config.runner_specs()), 1 + len(protocol.GPU_WAIT_LANES))
        api = mock.Mock(return_value={'id': 42, 'private': True})
        ids = deployment_plan.resolve_repository_ids(config, call=api)
        api.assert_called_once_with('GET', 'https://api.github.com/repos/example/project', 'test-private-pat')
        plan = deployment_plan.build(config, repository_ids=ids)
        control = self.read_rendered(plan, config.control/'etc/central.toml')
        self.assertTrue(control['repository_members'])
        self.assertNotIn('hosts', control)
        self.assertNotIn('executor', control)
        self.assertEqual(control['plan']['device_admission_file'], str(self.input/'device-admission.toml'))
        policy = self.read_rendered(plan, config.etc/'policy.toml')
        self.assertEqual(policy['actors'], [])
        self.assertEqual(policy['repositories']['example/project']['id'], 42)

    def test_executor_plan_requires_neither_controller_host_nor_pat_nor_manual_repo_ids(self):
        config = self.executor()
        self.assertEqual(list(config.inventory_document()['hosts']), ['3090b'])
        self.assertEqual(config.controller_name, None)
        self.assertEqual([name for name, *_ in config.runner_specs()], ['3090b-0'])
        api = mock.Mock()
        self.assertEqual(deployment_plan.resolve_repository_ids(config, call=api), {})
        api.assert_not_called()
        plan = deployment_plan.build(config)
        control = self.read_rendered(plan, config.control/'etc/central.toml')
        self.assertTrue(control['repository_members'])
        self.assertNotIn('plan', control)
        self.assertEqual(control['executor']['registration'], {
            'device': '3090b', 'cpu_count': 4, 'memory_max': '8G', 'gpu_uuids': [],
            'runner_count': 1, 'priority': 25,
        })
        policy = self.read_rendered(plan, config.etc/'policy.toml')
        self.assertEqual(policy['repositories'], {})
        self.assertEqual(self.read_rendered(plan, config.etc/'projects/example.toml')['repository'], 'example/project')
        self.assertEqual(set(deployment_plan.secret_fingerprints(config)), {'host-token', 'ca.crt'})
        self.assertNotIn('executor-device-token', str(plan))

    def test_combined_host_uses_its_allowlisted_token_for_the_local_executor(self):
        self.controller(executor=True)
        config = self.config()
        with self.assertRaisesRegex(ValueError, 'add the local executor'):
            deployment_credentials.credential_sources(config)
        files.atomic_write(self.input/'device-admission.toml',
                           '[devices.3090a]\ntoken_file = "credentials/hosts/local-key"\n')
        key = self.input/'credentials/hosts/local-key'
        files.atomic_write(key, 'local-device-key\n')
        self.assertEqual(deployment_credentials.credential_sources(config)['host-token'], key)

    def test_schema_two_rejects_remote_topology_and_inconsistent_capacity(self):
        self.controller(executor=True)
        local = self.document['hosts']['3090a']
        self.document['hosts']['3090b'] = dict(local)
        self.write_inputs()
        with self.assertRaisesRegex(ValueError, 'only the local host'):
            self.config()
        del self.document['hosts']['3090b']
        local['runner_count'] = 2
        self.write_inputs()
        with self.assertRaisesRegex(ValueError, 'runner_count'):
            self.config()
        local.update(runner_count=1, priority=True)
        self.write_inputs()
        with self.assertRaisesRegex(ValueError, 'priority'):
            self.config()

    def test_executor_preflight_checks_controller_as_the_named_device(self):
        config = self.executor()
        with mock.patch.object(deployment_plan.transport, 'Client') as client:
            client.return_value.call.return_value = {'ready': True}
            deployment_plan.check_host(config, run=self.fake_run)
        client.assert_called_once_with(
            url=self.document['controller']['url'],
            token_file=str(self.input/'credentials/hosts/3090b'),
            ca_file=str(self.input/'credentials/tls/ca.crt'),
            device='3090b',
        )
        client.return_value.call.assert_called_once_with('health')

    def test_executor_flow_applies_locally_without_controller_export(self):
        config = self.executor()
        receipt = self.apply(deployment_plan.build(config), config)
        self.assertEqual(self.registrations, ['3090b-0'])
        self.assertEqual(receipt['status'], 'installed')
        self.assertEqual(receipt['repository_ids'], {})
        self.assertTrue((config.etc/'projects/example.toml').is_file())

    def test_output_does_not_require_projects_or_credentials(self):
        config = self.executor()
        (self.input/'projects/example.toml').unlink()
        (self.input/'credentials/hosts/3090b').unlink()
        with mock.patch.object(deployment_output, 'refresh', return_value={'files': []}) as refresh:
            with redirect_stdout(StringIO()):
                self.assertEqual(deploy.main(['output', '--host', '3090b', '--input', str(self.input)]), 0)
        loaded = refresh.call_args.args[0]
        self.assertEqual(loaded.runtime, config.runtime)
        self.assertFalse(loaded.projects)

    def test_apply_reports_view_failure_as_warning_after_installation(self):
        self.controller()
        completed = dict(version='2.1.1', host='3090a', status='installed', source_digest='digest')
        with (mock.patch.object(deploy.deployment_steps, 'apply', return_value=completed),
              mock.patch.object(deployment_output, 'refresh', side_effect=OSError('disk')),
              redirect_stdout(StringIO()) as output):
            self.assertEqual(deploy.main(['apply', '--host', '3090a', '--input', str(self.input),
                                          '--plan', str(self.root/'plan.json')]), 0)
        self.assertIn('installed', output.getvalue())
        self.assertIn('output_warning', output.getvalue())
