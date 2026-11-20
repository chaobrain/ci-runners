"""Deployment behavior against temporary roots and simulated external services."""
from __future__ import annotations

import json
import io
import os
import pwd
import sqlite3
import subprocess
import tarfile
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from ci_runners import __version__, slurmsetup, pool
from parallel import deploy, deployment_config as config_module, deployment_credentials as credentials
from parallel import deployment_files as files, deployment_plan as planning, deployment_steps as steps, health, slurm_install


class DeploymentFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='ci-deployment-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.input = self.root/'input'
        host = dict(account=pwd.getpwuid(os.getuid()).pw_name, home=str(self.root/'home'), runtime=str(self.root/'runtime'),
                    roles=['controller', 'executor'], cpu_count=4, memory_max='8G')
        self.document = {'schema': 1, 'github': {'organization': 'example', 'runner_group': 'ci-v2'},
                         'controller': {'url': 'https://controller.example.invalid:18444', 'bind': 'controller.example.invalid'},
                         'hosts': {'3090a': host}}
        self.project = dict(name='example', repository='example/project', repository_id=42, context='example/cpu',
                            actors=['trusted'], devices=['3090a'], environment={'mode': 'existing'})
        self.write_inputs()
        files.atomic_write(self.input/'credentials/pat/github-pat', 'test-private-pat\n')
        files.atomic_write(self.input/'credentials/runner/runner_token', 'test-registration-token\n')
        self.calls = []
        self.registrations = []
        self.fail_registration = ''
        self.prepare_env()

    def write_inputs(self):
        files.atomic_write(self.input/'host.toml', config_module.toml_document(self.document))
        files.atomic_write(self.input/'projects/example.toml', config_module.toml_document(self.project))

    def prepare_env(self):
        files.atomic_write(self.root/'runtime/ci-runners/envs/example/bin/python', '#!/bin/sh\nexit 0\n', 0o755)

    def config(self, host='3090a'):
        return config_module.load(self.input, host)

    def fake_run(self, command, **options):
        self.calls.append(list(map(str, command)))
        if str(command[0]).endswith('/config.sh'):
            name = command[command.index('--name')+1]
            if self.fail_registration == name:
                return subprocess.CompletedProcess(command, 1, b'', b'authentication failed')
            self.registrations.append(name)
            self.assertNotIn('test-registration-token', ' '.join(command))
            self.assertEqual(options['env']['ACTIONS_RUNNER_INPUT_TOKEN'], 'test-registration-token')
            directory = Path(options['cwd'])
            files.write_json(directory/'.runner', {'agentName': name, 'gitHubUrl': command[command.index('--url')+1],
                                                  'poolName': command[command.index('--runnergroup')+1]})
        return subprocess.CompletedProcess(command, 0, '' if options.get('text') else b'', b'')

    def fake_unpack(self, directory, *_args):
        for filename in ('config.sh', 'run.sh', 'bin/Runner.Listener'):
            files.atomic_write(Path(directory)/filename, '#!/bin/sh\nexit 0\n', 0o755)

    def apply(self, plan, config=None):
        with mock.patch.object(steps.deployment_credentials, 'install'), mock.patch.object(steps.setup, '_unpack', self.fake_unpack):
            return steps.apply(config or self.config(), plan, run=self.fake_run, preflight=lambda *a, **k: [], readiness=lambda c: None)


class Configuration(DeploymentFixture):
    def test_init_preserves_existing_configuration_and_private_modes(self):
        output = self.root/'new-input'
        deploy.initialize(output)
        for name in ('pat', 'runner', 'hosts', 'tls', 'slurm', 'adm_token'):
            self.assertEqual((output/'credentials'/name).stat().st_mode & 0o777, 0o700)
        manual = ('credentials/pat/github-pat', 'credentials/runner/runner_token')
        for path in manual:
            self.assertEqual((output/path).read_bytes(), b'')
            self.assertEqual((output/path).stat().st_mode & 0o777, 0o600)
            files.atomic_write(output/path, 'hand-filled\n')
        files.atomic_write(output/'host.toml', 'maintained = true\n')
        (output/'projects/example.toml').rename(output/'projects/example.toml.sample')
        deploy.initialize(output)
        self.assertFalse((output/'projects/example.toml').exists())
        for path in manual:
            self.assertEqual((output/path).read_text(), 'hand-filled\n')
        self.assertEqual((output/'host.toml').read_text(), 'maintained = true\n')
        self.assertEqual((output/'credentials').stat().st_mode & 0o777, 0o700)
        self.assertEqual((output/'host.toml').stat().st_mode & 0o777, 0o600)

    def test_legacy_input_remains_usable_after_init(self):
        flat_pat = self.input/'credentials/github-pat'
        flat_token = self.input/'credentials/runner-registration-token'
        (self.input/'credentials/pat/github-pat').rename(flat_pat)
        (self.input/'credentials/runner/runner_token').rename(flat_token)
        deploy.initialize(self.input)
        self.assertEqual(credentials.registration_path(self.input), flat_token)
        self.assertEqual(credentials.credential_sources(self.config())['github-pat'], flat_pat)
        self.apply(planning.build(self.config()))
        self.assertEqual(self.registrations, ['plan-0', '3090a-0'])
        files.atomic_write(self.input/'credentials/pat/github-pat', 'preferred-pat')
        self.assertEqual(credentials.pat_path(self.input), self.input/'credentials/pat/github-pat')
        files.atomic_write(self.input/'credentials/runner/runner_token', b'')
        self.assertEqual(credentials.registration_path(self.input), self.input/'credentials/runner/runner_token')

    def test_shared_repository_keeps_its_slurm_opt_in_and_identity(self):
        cluster = dict(self.project, name='example-full', devices=['3090a', 'slurm'], slurm_target='cluster1/cpu')
        other = dict(self.project, name='other', repository='example/other', repository_id=43)
        for project in (cluster, other):
            files.atomic_write(self.input/'projects'/(project['name']+'.toml'), config_module.toml_document(project))
        files.atomic_write(self.input/'slurm/cluster1.toml', (planning.SOURCE/'setup/slurm/site.example.toml').read_bytes())
        config = self.config()
        ids = {name: project['repository_id'] for name, project in config.projects.items()}
        for projects in (config.projects, dict(reversed(list(config.projects.items())))):
            config.projects = projects
            rendered = config_module.runtime_files(config, ids, planning.SOURCE)
            policy = tomllib.loads(rendered[str(config.etc/'policy.toml')]['content'])
            self.assertEqual(policy['repositories']['example/project']['classes'], ['slurm'])
            self.assertEqual(policy['repositories']['example/other']['classes'], [])
            self.assertEqual(config.projects['example']['devices'], ['3090a'])
        with self.assertRaisesRegex(ValueError, 'repository IDs'):
            config_module.runtime_files(config, dict(ids, example=99), planning.SOURCE)

    def test_repository_lookup_uses_the_controller_pat(self):
        del self.project['repository_id']
        self.write_inputs()
        api = mock.Mock(return_value={'id': 42, 'private': True})
        self.assertEqual(planning.resolve_repository_ids(self.config(), call=api), {'example': 42})
        api.assert_called_once_with('GET', 'https://api.github.com/repos/example/project', 'test-private-pat')

    def test_exactly_one_controller_and_independent_cpu_quota(self):
        config = self.config()
        self.assertEqual([name for name, _, _ in config.runner_specs()], ['plan-0', '3090a-0'])
        self.document['hosts']['3090b'] = dict(self.document['hosts']['3090a'], roles=['executor'], runtime=str(self.root/'node-runtime'))
        self.write_inputs()
        self.assertEqual([role for _, _, role in self.config('3090b').runner_specs()], ['executor'])
        self.document['hosts']['3090b']['roles'].append('controller')
        self.write_inputs()
        with self.assertRaisesRegex(ValueError, 'exactly one'):
            self.config()

    def test_invalid_cpu_and_memory_are_rejected_before_installation(self):
        for count, memory in [(0, '8G'), (6, '8G'), (-4, '8G'), (True, '8G'),
                              ('4', '8G'), (8, '4G'), (20, '32G')]:
            with self.subTest(count=count, memory=memory):
                self.document['hosts']['3090a'].update(cpu_count=count, memory_max=memory)
                self.write_inputs()
                with self.assertRaises(ValueError):
                    self.config()

    def test_legacy_cpu_list_only_supplies_a_count(self):
        host = self.document['hosts']['3090a']
        del host['cpu_count']
        host['cpus'] = '100-103'
        self.write_inputs()
        config = self.config()
        rendered = config.inventory_document()['hosts']['3090a']
        self.assertNotIn('cpus', rendered)
        self.assertEqual(rendered['pools']['cpu']['count'], 1)
        host['cpu_count'] = 4
        self.write_inputs()
        with self.assertRaisesRegex(ValueError, 'or legacy cpus'):
            self.config()
        del host['cpu_count']
        host['cpus'] = '0-3,2-5'
        self.write_inputs()
        with self.assertRaises(ValueError):
            self.config()

    def test_preflight_probes_quota_and_memory_without_binding(self):
        def fail_scope(command, **options):
            if command[0] == 'systemd-run':
                self.calls.append(list(command))
                return subprocess.CompletedProcess(command, 1, b'', b'')
            return self.fake_run(command, **options)
        with mock.patch.object(planning.os, 'sched_getaffinity', return_value=set(range(8))):
            problems = planning.check_host(self.config(), run=fail_scope, network=False)
        scope = next(command for command in self.calls if command[0] == 'systemd-run')
        self.assertIn('CPUQuota=100%', scope)
        self.assertIn('MemoryMax=64M', scope)
        self.assertFalse(any('AllowedCPUs' in word or word == 'sudo' for word in scope))
        self.assertTrue(any('CPU quota or memory limit' in problem for problem in problems))

    def test_rendered_paths_and_services_match_roles(self):
        config = self.config()
        plan = planning.build(config)
        control = plan['files'][str(config.control/'etc/central.toml')]['content']
        self.assertIn('parallel/state/scheduler.sqlite', control)
        self.assertIn('parallel/run/plan.sock', control)
        self.assertIn('parallel/run/executor.sock', plan['files'][str(config.runtime/'actions-runner/3090a-0/.env')]['content'])
        self.assertEqual(plan['services'], ['ci-parallel-scheduler.service', 'ci-parallel-executor.service'])
        self.assertNotIn('administrator_script', plan)
        self.assertFalse(any('.timer' in filename for filename in plan['files']))

    def test_secret_symlinks_or_public_modes_are_rejected(self):
        path = self.input/'credentials/pat/github-pat'
        path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'mode 600'):
            planning.build(self.config())
        path.unlink()
        path.symlink_to(self.input/'credentials/runner/runner_token')
        with self.assertRaises(OSError):
            planning.build(self.config())

    def test_parent_symlink_cannot_redirect_generated_files(self):
        outside = self.root/'outside'
        outside.mkdir()
        link = self.root/'redirect'
        link.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            files.atomic_write(link/'nested/config', 'must not escape')
        self.assertFalse((outside/'nested/config').exists())

    def test_plan_contains_references_but_no_secret_values(self):
        text = json.dumps(planning.build(self.config()))
        self.assertNotIn('test-private-pat', text)
        self.assertNotIn('test-registration-token', text)
        self.assertIn('github-pat', text)

    def test_status_requires_a_fresh_heartbeat_and_active_service(self):
        config = self.config()
        with mock.patch.object(health.pool, 'runner_procs', return_value=(set(), set())), mock.patch.object(health.time, 'time', return_value=1000):
            report = health.status(config, query_github=False, run=self.fake_run)
            self.assertEqual(set(report['services']), {'scheduler', 'executor'})
            self.assertFalse(report['services']['executor']['ready'])
            files.write_json(config.control/'state/executor-health.json', {'observed_at': 1000, 'connected': True})
            self.assertTrue(health.status(config, query_github=False, run=self.fake_run)['services']['executor']['ready'])
            stopped = mock.Mock(return_value=subprocess.CompletedProcess([], 3))
            self.assertFalse(health.status(config, query_github=False, run=stopped)['services']['executor']['ready'])
            files.write_json(config.control/'state/executor-health.json', {'observed_at': 800, 'connected': True})
            self.assertFalse(health.status(config, query_github=False, run=self.fake_run)['services']['executor']['ready'])


class Installation(DeploymentFixture):
    def test_interruption_after_file_write_can_resume_from_durable_intent(self):
        config = self.config()
        plan = planning.build(config)
        original = files.atomic_write
        target = config.etc/'policy.toml'
        def interrupted(path, data, mode=0o600):
            original(path, data, mode)
            if Path(path) == target:
                raise InterruptedError('simulated crash after rename')
        with mock.patch.object(files, 'atomic_write', side_effect=interrupted):
            with self.assertRaises(InterruptedError):
                self.apply(plan)
        receipt = files.read_json(config.control/'state/deployment.json')
        self.assertEqual(receipt['pending_files'][str(target)], files.file_state(target))
        self.apply(plan)
        receipt = files.read_json(config.control/'state/deployment.json')
        self.assertEqual(receipt['status'], 'installed')
        self.assertEqual(receipt['pending_files'], {})

    def test_active_allocations_prevent_deployment_without_clearing_them(self):
        config = self.config()
        files.private_directory(config.control/'state')
        path = config.control/'state/executor.sqlite'
        with sqlite3.connect(path) as database:
            database.execute('CREATE TABLE executions (state TEXT)')
            database.execute("INSERT INTO executions VALUES ('running')")
        with self.assertRaisesRegex(ValueError, 'drained'):
            self.apply(planning.build(config))
        with sqlite3.connect(path) as database:
            self.assertEqual(database.execute('SELECT state FROM executions').fetchone()[0], 'running')
        self.assertEqual(self.calls, [])

    def test_second_apply_preserves_identity_state_and_registration_count(self):
        config = self.config()
        plan = planning.build(config)
        self.assertEqual(self.apply(plan)['status'], 'installed')
        files.atomic_write(config.control/'state/keep-record', 'durable')
        identities = {name: (config.runtime/'actions-runner'/name/'.runner').read_bytes() for name, _, _ in config.runner_specs()}
        self.apply(plan)
        self.assertEqual(self.registrations, ['plan-0', '3090a-0'])
        self.assertEqual((config.control/'state/keep-record').read_text(), 'durable')
        for name, original in identities.items():
            self.assertEqual((config.runtime/'actions-runner'/name/'.runner').read_bytes(), original)

    def test_partial_registration_is_resumed_without_repeating_successes(self):
        plan = planning.build(self.config())
        self.fail_registration = '3090a-0'
        with self.assertRaisesRegex(ValueError, 'registration failed'):
            self.apply(plan)
        self.assertEqual(self.registrations, ['plan-0'])
        self.assertEqual(files.read_json(self.config().control/'state/deployment.json')['status'], 'incomplete')
        self.fail_registration = ''
        self.apply(plan)
        self.assertEqual(self.registrations, ['plan-0', '3090a-0'])

    def test_registration_token_refresh_does_not_invalidate_the_plan(self):
        config = self.config()
        plan = planning.build(config)
        files.atomic_write(self.input/'credentials/runner/runner_token', 'new-registration-token\n')
        planning.validate(plan, config)

    def test_input_changes_refuse_stale_plan_before_side_effects(self):
        config = self.config()
        plan = planning.build(config)
        self.document['github']['runner_group'] = 'another-group'
        self.write_inputs()
        with self.assertRaisesRegex(ValueError, 'stale'):
            self.apply(plan)
        self.assertEqual(self.calls, [])
        self.assertFalse((config.control/'state/deployment.json').exists())

    def test_manual_edit_after_apply_is_not_overwritten(self):
        config = self.config()
        plan = planning.build(config)
        self.apply(plan)
        files.atomic_write(config.etc/'policy.toml', 'operator edit\n')
        with self.assertRaisesRegex(ValueError, 'changed outside'):
            self.apply(plan)
        self.assertEqual((config.etc/'policy.toml').read_text(), 'operator edit\n')

    def test_foreign_units_and_runner_identities_are_not_adopted(self):
        config = self.config()
        unit = config.unit_dir/'ci-parallel-executor.service'
        files.atomic_write(unit, '[Service]\nExecStart=/bin/true\n')
        with self.assertRaisesRegex(ValueError, 'unrecorded'):
            planning.build(config)
        unit.unlink()
        directory = config.runtime/'actions-runner/plan-0'
        self.fake_unpack(directory)
        files.write_json(directory/'.runner', {'agentName': 'plan-0', 'gitHubUrl': 'https://github.com/another'})
        with self.assertRaisesRegex(ValueError, 'no deployment receipt'):
            self.apply(planning.build(config))

    def test_changed_code_under_same_version_is_not_overwritten(self):
        source, target = self.root/'source', self.root/'release'
        files.atomic_write(source/'VERSION', __version__+'\n')
        digest = files.release_digest(source)
        files.install_release(source, target, digest)
        files.atomic_write(source/'VERSION', 'different contents\n')
        with self.assertRaisesRegex(ValueError, 'different contents'):
            files.install_release(source, target, files.release_digest(source))
        self.assertEqual((target/'VERSION').read_text(), __version__+'\n')

    def test_preflight_failure_does_not_install_or_register(self):
        config = self.config()
        with mock.patch.object(credentials, 'install'), mock.patch.object(steps.setup, '_unpack', self.fake_unpack):
            with self.assertRaisesRegex(ValueError, 'preflight failed'):
                steps.apply(config, planning.build(config), preflight=lambda *a, **k: ['missing CPU quota'],
                            run=self.fake_run, readiness=lambda c: None)
        self.assertFalse(planning.release_path(config).exists())
        self.assertEqual(self.calls, [])

    def test_service_readiness_precedes_runner_start(self):
        config = self.config()
        plan = planning.build(config)
        def not_ready(_config):
            raise ValueError('controller not ready')
        with mock.patch.object(credentials, 'install'), mock.patch.object(steps.setup, '_unpack', self.fake_unpack):
            with self.assertRaisesRegex(ValueError, 'not ready'):
                steps.apply(config, plan, run=self.fake_run, preflight=lambda *a, **k: [], readiness=not_ready)
        self.assertFalse(any(any('ci-parallel-runner@' in argument for argument in call) for call in self.calls))

    def test_environment_records_cannot_export_private_credentials(self):
        self.project['environment'] = {'mode': 'kept', 'record': 'environments/example.json'}
        self.write_inputs()
        record = self.input/'environments/example.json'
        files.private_directory(record.parent)
        record.symlink_to(self.input/'credentials/pat/github-pat')
        with self.assertRaisesRegex(ValueError, 'credential directories'):
            planning.build(self.config())

    def test_host_export_excludes_controller_and_registration_secrets(self):
        self.document['hosts']['3090b'] = dict(self.document['hosts']['3090a'], roles=['executor'],
                                               home=str(self.root/'node-home'), runtime=str(self.root/'node-runtime'))
        self.write_inputs()
        config = self.config()
        plan = planning.build(config)
        credentials.install(config)
        self.apply(plan)
        issued_ca = (self.input/'credentials/tls/issued/ca.crt').read_bytes()
        node_token = (self.input/'credentials/hosts/3090b').read_bytes()
        self.assertNotEqual(node_token, (self.input/'credentials/hosts/3090a').read_bytes())
        files.atomic_write(self.input/'credentials/adm_token/runner_adm', 'admin-secret\n')
        output = self.root/'node-input'
        steps.export_host(config, '3090b', output)
        exported = {str(path.relative_to(output)): path.read_bytes()
                    for path in (output/'credentials').rglob('*') if path.is_file()}
        self.assertEqual(exported, {'credentials/pat/github-pat': b'', 'credentials/runner/runner_token': b'',
                                    'credentials/tls/ca.crt': issued_ca, 'credentials/hosts/3090b': node_token})
        node = config_module.load(output, '3090b')
        self.assertEqual(planning.resolve_repository_ids(node), {'example': 42})
        (output/'credentials/pat/github-pat').unlink()
        plan = planning.build(node)
        credentials.install(node)
        self.assertEqual((node.private/'host-token').read_bytes(), node_token)
        self.assertEqual((node.private/'ca.crt').read_bytes(), issued_ca)
        self.assertFalse((node.private/'github-pat').exists())
        token = output/'credentials/runner/runner_token'
        with mock.patch.object(planning.transport, 'Client') as client:
            client.return_value.call.return_value = {'ready': True}
            problems = planning.check_host(node, run=self.fake_run)
            self.assertTrue(any(str(token) in problem for problem in problems))
            client.assert_called_once_with(url=self.document['controller']['url'],
                                          token_file=str(output/'credentials/hosts/3090b'), ca_file=str(output/'credentials/tls/ca.crt'))
        files.atomic_write(token, 'test-registration-token\n')
        with mock.patch.object(planning.transport, 'secret', side_effect=AssertionError('preflight read the registration token')):
            problems = planning.check_host(node, run=self.fake_run, network=False)
            self.assertFalse(any(str(token) in problem for problem in problems))
        files.atomic_write(node.runtime/'ci-runners/envs/example/bin/python', 'python')
        self.apply(plan, node)
        self.assertIn('3090b-0', self.registrations)
        token.unlink()
        with mock.patch.object(credentials, 'registration_path', side_effect=AssertionError('already registered')):
            planning.check_host(node, run=self.fake_run, network=False)
            self.apply(plan, node)
        self.assertEqual(self.registrations.count('3090b-0'), 1)


class Packaging(DeploymentFixture):
    def test_slurm_package_installs_through_maintenance_login_without_private_key(self):
        from tests.test_slurmsetup import good_facts
        site = config_module.read_toml(planning.SOURCE/'setup/slurm/site.example.toml')
        cluster_root = self.root/'cluster'
        cluster_root.mkdir(mode=0o700)
        site['accounts']['acct1']['root'] = str(cluster_root)
        files.atomic_write(self.input/'slurm/cluster1.toml', config_module.toml_document(site))
        key = self.input/'credentials/slurm/acct1'
        files.private_directory(key.parent)
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)], check=True, timeout=15)
        output = self.root/'slurm-bundle'
        deploy.slurm_package(self.config(), 'cluster1', 'acct1', output)
        self.assertFalse((output/'credentials').exists())
        self.assertNotIn(key.read_text(), ''.join(path.read_text() for path in output.iterdir() if path.is_file()))
        self.assertIn('restrict,command=', (output/'authorized-key.txt').read_text())
        check = subprocess.run([str(output/'install.sh'), '--help'], cwd=self.root, capture_output=True, text=True, timeout=15)
        self.assertEqual(check.returncode, 0, check.stderr)
        with mock.patch.object(slurm_install.slurmlogin, 'facts', return_value=good_facts()), \
                mock.patch.object(slurm_install.slurmlogin, 'ci_jobs', return_value={'jobs': []}):
            self.assertTrue(slurm_install.install(output)['ready'])
            self.assertFalse((cluster_root/'ci-runners').exists())
            slurm_install.install(output, apply=True)
            slurm_install.install(output, apply=True)
        self.assertEqual((cluster_root/'ci-runners/releases/hook').resolve().name, 'v2.1.0')
        self.assertTrue((cluster_root/'ci-runners/releases/hook/parallel/task_execution.py').is_file())
        self.assertTrue((cluster_root/'ci-runners/etc/account.json').is_file())

    def test_pool_bundle_carries_protocol_and_version_with_legacy_helpers(self):
        archive = pool.bundle(str(self.root), [], pkg=str(planning.SOURCE/'ci_runners'))
        with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
            self.assertIn('pkg/parallel/protocol.py', bundle.getnames())
            self.assertIn('pkg/parallel/task_execution.py', bundle.getnames())
            self.assertEqual(bundle.extractfile('pkg/VERSION').read().decode().strip(), '2.1.0')

    def test_slurm_file_list_contains_new_package_and_version(self):
        names = slurmsetup.release_files(str(planning.SOURCE))
        self.assertIn('parallel/task_execution.py', names)
        self.assertIn('parallel/protocol.py', names)
        self.assertIn('VERSION', names)

    def test_copied_release_runs_without_original_source_or_git(self):
        copied = self.root/'portable'
        files.install_release(planning.SOURCE, copied, files.release_digest(planning.SOURCE))
        environment = {'PATH': '/usr/bin:/bin', 'HOME': str(self.root), 'PYTHONPATH': '/missing'}
        command = subprocess.run([str(copied/'parallel/deploy.sh'), '--version'], cwd=self.root,
                                 env=environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(command.returncode, 0, command.stderr)
        self.assertEqual(command.stdout.strip(), '2.1.0')
        command = subprocess.run([str(copied/'bin/ci-run'), '--version'], cwd=self.root,
                                 env=environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(command.returncode, 0, command.stderr)
        self.assertFalse((copied/'.git').exists())

    def test_installed_identities_survive_migration_and_a_new_runtime(self):
        config = self.config()
        credentials.install(config)
        issued = self.input/'credentials/tls/issued'
        token = self.input/'credentials/hosts/3090a'
        ca_key = (issued/'ca.key').read_bytes()
        host_token = token.read_bytes()
        issued.rename(config.private/'tls-material')
        token.unlink()
        with mock.patch.object(credentials, 'issue_tls', side_effect=AssertionError('rotated existing CA')):
            credentials.install(config)
            self.assertEqual((issued/'ca.key').read_bytes(), ca_key)
            self.assertEqual(token.read_bytes(), host_token)
            self.document['hosts']['3090a']['runtime'] = str(self.root/'fresh')
            self.write_inputs()
            fresh = self.config()
            credentials.install(fresh)
            self.assertEqual((fresh.private/'server.key').read_bytes(), (config.private/'server.key').read_bytes())
            self.assertEqual((fresh.private/'host-token').read_bytes(), host_token)
            self.assertFalse((fresh.private/'tls-material').exists())

    def test_imported_tls_is_used_as_a_complete_set(self):
        config = self.config()
        old = self.input/'tls'
        files.private_directory(old)
        credentials.issue_tls(config, old, subprocess.run)
        current = self.input/'credentials/tls'
        files.atomic_write(current/'ca.crt', (old/'ca.crt').read_bytes())
        with self.assertRaisesRegex(ValueError, 'together'):
            planning.build(config)
        for name in ('server.key', 'server.crt'):
            files.atomic_write(current/name, (old/name).read_bytes())
        self.assertEqual(credentials.credential_sources(config)['ca.crt'], current/'ca.crt')
        credentials.install(config)
        self.assertFalse((current/'issued').exists())
        self.assertEqual((config.private/'server.key').read_bytes(), (current/'server.key').read_bytes())
        for name in ('ca.crt', 'server.crt', 'server.key'):
            (current/name).unlink()
        self.assertEqual(credentials.credential_sources(config)['ca.crt'], old/'ca.crt')

    def test_generated_tls_authenticates_endpoint_and_is_idempotent(self):
        config = self.config()
        plan = planning.build(config)
        credentials.install(config)
        planning.validate(plan, config)
        issued = self.input/'credentials/tls/issued'
        self.assertEqual({p.name for p in issued.iterdir()}, {'ca.key', 'ca.crt', 'server.key', 'server.crt'})
        for path in issued.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertFalse((config.private/'ca.key').exists())
        self.assertEqual((self.input/'credentials/hosts/3090a').read_bytes(), (config.private/'host-token').read_bytes())
        original = {name: (config.private/name).read_bytes() for name in ('server.key', 'ca.crt', 'host-token')}
        credentials.install(config)
        for name, content in original.items():
            self.assertEqual((config.private/name).read_bytes(), content)
        self.document['controller']['url'] = 'https://different.example.invalid:18444'
        self.write_inputs()
        with self.assertRaisesRegex(ValueError, 'certificate'):
            credentials.validate_tls(self.config(), config.private)


class Health(unittest.TestCase):
    def test_failures_are_visible_and_recovery_clears_backoff(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor = health.ConnectionHealth(Path(directory)/'health.json', clock=lambda: 42.)
            with self.assertLogs('ci.parallel', level='WARNING'):
                monitor.record(ConnectionError('must not expose a credential'), pending_reports=3)
            report = files.read_json(monitor.path)
            self.assertFalse(report['connected'])
            self.assertEqual(report['pending_reports'], 3)
            self.assertNotIn('credential', json.dumps(report))
            self.assertGreater(monitor.retry_delay(), 5)
            monitor.record(pending_reports=0)
            self.assertEqual(monitor.retry_delay(), 5)
            self.assertEqual(files.read_json(monitor.path)['last_success'], 42.)

    def test_missing_github_permission_is_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory)/'token'
            files.atomic_write(token, 'test-only')
            rows, error = health.github_runners('example', token, call=mock.Mock(side_effect=ValueError('forbidden')))
            self.assertEqual(rows, {})
            self.assertEqual(error, 'ValueError')
