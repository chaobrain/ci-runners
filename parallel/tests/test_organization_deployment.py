"""Organization deployments and their optional controller-side Slurm CPU drivers."""
from __future__ import annotations

import fnmatch
import subprocess
import tomllib
from unittest import mock

from ci_runners import drain, hook, part, registry
from parallel import deploy, deployment_config, deployment_output, deployment_plan, deployment_steps, organization, protocol
from parallel import deployment_files as files
from parallel.tests.test_deployment import DeploymentFixture


DEFAULTS = dict(max_parallel=registry.MAX_PARALLEL, max_minutes=1440, retries=1, gpu_mem_mib=0)
BINDING = dict(repository_id=42, repository='example/project', slurm_project='project-a', slurm_target='cluster1/cpu')


class OrganizationDeployment(DeploymentFixture):
    def organization_host(self, *, executor=False):
        self.document.update(schema=2, mode='organization')
        host = self.document['hosts']['3090a']
        if executor:
            self.document['controller'].pop('bind')
            self.document['hosts'] = {'3090b': host}
            host.update(roles=['executor'], runner_count=1, priority=20)
            files.atomic_write(self.input/'credentials/tls/ca.crt', 'test-ca\n')
            files.atomic_write(self.input/'credentials/hosts/3090b', 'executor-key\n')
            self.host_name = '3090b'
        else:
            host['roles'] = ['controller']
            files.atomic_write(self.input/'device-admission.toml', '[devices]\n')
            self.host_name = '3090a'
        self.write_inputs()
        return host

    def read_plan(self, config):
        plan = deployment_plan.build(config)
        self.assertEqual(plan['repository_ids'], {})
        control = tomllib.loads(plan['files'][str(config.control/'etc/central.toml')]['content'])
        return plan, control

    def test_legacy_preflight_and_busy_host(self):
        self.organization_host(executor=True)
        config = self.config(self.host_name)
        def units(command, **kwargs):
            if 'list-units' in command:
                return subprocess.CompletedProcess(command, 0, 'ci-runner@legacy.service loaded active running')
            return self.fake_run(command, **kwargs)
        def blocked():
            return any('legacy/trial' in m for m in deployment_plan.check_host(config, run=units, network=False))
        self.assertFalse(blocked())
        config.host['roles'].append('controller')
        self.assertTrue(blocked())

    def test_cutover_checks_only_matching_resources(self):
        self.organization_host(executor=True)
        config = self.config(self.host_name)
        active = {'ci-runner@old.service', 'ci-part-old-1.scope', 'actions.runner.other.service',
                  'actions.runner.same.service', 'civ2-part-new-1.scope', 'ci-parallel-executor.service'}
        def units(command, **kwargs):
            if 'list-units' in command:
                rows = [u+' loaded active running' for u in active if any(fnmatch.fnmatchcase(u, p) for p in command)]
                return subprocess.CompletedProcess(command, 0, '\n'.join(rows))
            directory = config.runtime/'actions-runner/other' if 'actions.runner.same.service' in command else self.root/'legacy'
            return subprocess.CompletedProcess(command, 0, str(directory))
        self.assertEqual(set(deployment_steps.conflicting_units(config, run=units)),
                         {'actions.runner.same.service', 'civ2-part-new-1.scope', 'ci-parallel-executor.service'})

    def test_full_registration_with_another_runner_still_active(self):
        self.organization_host(executor=True)
        cfg = self.config(self.host_name)
        plan = deployment_plan.build(cfg)
        fake = self.fake_run
        def coexist(command, **kwargs):
            if 'list-units' in command:
                return subprocess.CompletedProcess(command, 0, 'actions.runner.other.service active')
            if 'WorkingDirectory' in command:
                return subprocess.CompletedProcess(command, 0, str(self.root/'legacy/runner'))
            if 'FragmentPath' in command:
                return subprocess.CompletedProcess(command, 0, 'LoadState=not-found')
            return fake(command, **kwargs)
        self.fake_run = coexist
        self.assertEqual(self.apply(plan, cfg)['status'], 'installed')
        self.assertEqual(self.registrations, ['3090b-0'])
        self.assertFalse(any('stop' in c for c in self.calls))

    def test_foreign_unit_is_not_adopted(self):
        self.organization_host(executor=True)
        cfg = self.config(self.host_name)
        foreign = subprocess.CompletedProcess([], 0, 'LoadState=loaded\nFragmentPath=/opt/another/executor.service')
        with self.assertRaisesRegex(ValueError, 'unit conflicts'):
            deployment_steps.inspect_executor_units(cfg, {}, run=mock.Mock(return_value=foreign))

    def test_executor_resource_verification_defaults_on_and_can_be_disabled(self):
        host = self.organization_host(executor=True)
        for option in (None, False):
            with self.subTest(option=option):
                if option is not None:
                    host['verify_resource_limits'] = option
                    self.write_inputs()
                config = self.config(self.host_name)
                _, control = self.read_plan(config)
                self.assertIs(control['executor']['verify_resource_limits'], option is not False)
                self.calls.clear()
                deployment_plan.check_host(config, run=self.fake_run, network=False)
                self.assertEqual(any(command[0] == 'systemd-run' for command in self.calls), option is not False)
        host['verify_resource_limits'] = 'false'
        self.write_inputs()
        with self.assertRaisesRegex(ValueError, 'verify_resource_limits'):
            self.config(self.host_name)

    def test_task_scopes_and_completion_use_the_same_isolated_namespace(self):
        self.organization_host(executor=True)
        cfg = self.config(self.host_name)
        plan = deployment_plan.build(cfg)
        text = plan['files'][str(cfg.runtime/'ci-runners/hooks/job-completed.sh')]['content']
        self.assertIn('"$root" job-completed --organization', text)
        for org, prefix in ((True, 'civ2-part'), (False, 'ci-part')):
            unit = part.unit_name('node-0', 'tests', '123', organization=org)
            self.assertTrue(part.SCOPE.fullmatch(unit))
            self.assertTrue(unit.startswith(prefix+'-node-0-'))
            with mock.patch.object(hook.subprocess, 'run', return_value=mock.Mock(stdout='')) as run:
                hook._job_scopes({'RUNNER_NAME': 'node-0'}, organization=org)
                self.assertEqual(run.call_args.args[0][-1], prefix+'-node-0-*')
                drain.running_scopes(run, organization=org)
                self.assertEqual(run.call_args.args[0][-1], prefix+'-*')

    def test_controller_plan_binds_shared_environment_and_profile_without_projects(self):
        self.organization_host()
        # Existing manual inputs need not be removed before changing modes.
        files.atomic_write(self.input/'projects/example.toml', 'this is not TOML\n')
        files.atomic_write(self.input/'slurm/stale.toml', 'neither is this\n')
        files.atomic_write(self.input/'organization.toml', 'max_parallel = 8\n')
        config = self.config()
        self.assertEqual(config.projects, {})
        self.assertEqual(config.sites, {})
        old_fingerprint = deployment_plan.input_fingerprint(config)
        plan, control = self.read_plan(config)
        self.assertEqual(control['organization'], {
            'name': 'example', 'env': str(config.runtime/'ci-runners/envs/organization'),
            'profile': {**DEFAULTS, 'max_parallel': 8},
        })
        self.assertTrue(control['repository_members'])
        self.assertTrue(control['plan']['device_admission_file'].endswith('/device-admission.toml'))
        policy = tomllib.loads(plan['files'][str(config.etc/'policy.toml')]['content'])
        self.assertEqual(policy, {'schema': 2, 'actors': [], 'repositories': {}})
        self.assertFalse(any('/projects/' in name or '/slurm/' in name for name in plan['files']))
        self.assertNotIn(str(config.unit_dir/'civ2.slice'), plan['files'])
        self.assertIn(str(config.unit_dir/'ci.slice'), plan['files'])
        self.assertIn('Slice=ci.slice', plan['files'][str(config.unit_dir/'ci-parallel-runner@.service')]['content'])
        self.assertFalse(any('ci-example.slice' in name for name in plan['files']))
        files.atomic_write(self.input/'organization.toml', 'max_parallel = 9\n')
        self.assertNotEqual(deployment_plan.input_fingerprint(self.config()), old_fingerprint)

    def test_executor_plan_uses_its_own_shared_environment_without_controller_profile(self):
        host = self.organization_host(executor=True)
        host['env'] = str(self.root/'shared-env')
        self.write_inputs()
        (self.input/'credentials/pat/github-pat').unlink()
        files.atomic_write(self.input/'organization.toml', 'This file is ignored on the executor.\n')
        config = self.config(self.host_name)
        plan, control = self.read_plan(config)
        self.assertEqual(control['organization'], {'name': 'example', 'env': host['env']})
        self.assertEqual(control['executor']['registration']['device'], self.host_name)
        self.assertNotIn('plan', control)
        self.assertNotIn(str(config.unit_dir/'ci.slice'), plan['files'])
        self.assertIn(str(config.unit_dir/'civ2.slice'), plan['files'])
        self.assertIn('Slice=civ2.slice', plan['files'][str(config.unit_dir/'ci-parallel-runner@.service')]['content'])
        self.assertEqual(set(deployment_plan.secret_fingerprints(config)), {'host-token', 'ca.crt'})

    def test_controller_renders_repository_environments_and_resolves_verified_source(self):
        host = self.organization_host()
        root = self.root/'prefixes'
        host.update(env_root=str(root), env_aliases={'Example/Project-Staging': 'project'})
        self.write_inputs()
        files.atomic_write(self.input/'organization.toml', '\n')
        config = self.config()
        _, control = self.read_plan(config)
        self.assertEqual(control['organization'], {
            'name': 'example', 'profile': DEFAULTS, 'env_root': str(root),
            'env_aliases': {'example/project-staging': 'project'},
        })
        organization.validate_settings(control['organization'], controller=True)
        source = {'repository_id': 42, 'project': 'repo-42', 'context': 'ci', 'organization_profile': DEFAULTS,
                  'env': '/wrong', 'env_root': '/wrong', 'env_aliases': {'example/project-staging': 'wrong'}}
        inv = config.inventory()
        for repository, expected in (('ExAmPle/PROJECT', str(root/'project')),
                                     ('EXAMPLE/Project-Staging', str(root/'project')),
                                     ('example/missing', str(root/'missing')),
                                     ('example/.github', ''), ('example/organization', ''),
                                     ('example/job-20230101T000000Z', '')):
            with self.subTest(repository=repository):
                project = organization.project(control, inv, {**source, 'repository': repository})
                self.assertEqual((project.name, project.repository, project.env), ('repo-42', repository.lower(), expected))
                self.assertFalse(organization.environment_ready(control, repository))
        for override in ({'repository_id': True}, {'project': 'project'}, {'repository': 'other/project'},
                         {'repository': 'example/..'}, {'repository': 'example/../project'}):
            with self.subTest(override=override), self.assertRaises(protocol.Error):
                organization.project(control, inv, {**source, 'repository': 'example/project', **override})

    def test_executor_rejects_invalid_root_or_aliases_in_input_and_central_settings(self):
        host = self.organization_host(executor=True)
        for options in (
                {'env_root': '/envs', 'env': '/shared'}, {'env_aliases': {}},
                {'env_root': 'relative'}, {'env_root': '/envs/../other'}, {'env_root': '/'},
                {'env_root': '/envs', 'env_aliases': ['bad']},
                *({'env_root': '/envs', 'env_aliases': {'example/staging': value}} for value in
                  ('/other', '.', '..', '.hidden', 'name/other', 'thing-20230101T000000Z', 'OrgAnIzAtIoN', 5)),
                {'env_root': '/envs', 'env_aliases': {'foreign/staging': 'project'}},
                {'env_root': '/envs', 'env_aliases': {'example/../a': 'project'}},
                {'env_root': '/envs', 'env_aliases': {'Example/Build': 'one', 'example/build': 'two'}}):
            with self.subTest(options=options):
                host.update(options)
                self.write_inputs()
                with self.assertRaises(ValueError):
                    self.config(self.host_name)
                with self.assertRaises(protocol.Error):
                    organization.validate_settings({'name': 'example', **options}, controller=False)
                for key in options:
                    del host[key]
        host['env_root'] = '/executor/prefixes'
        self.write_inputs()
        _, control = self.read_plan(self.config(self.host_name))
        self.assertEqual(control['organization'], {'name': 'example', 'env_root': '/executor/prefixes'})
        organization.validate_settings(control['organization'], controller=False)

    def test_discovery_and_pinned_build_require_usable_prefix_inside_root(self):
        root = self.root/'prefixes'
        settings = {'organization': {'name': 'Example', 'env_root': str(root), 'env_aliases': {
            'example/staging': 'project', 'example/broken': 'missing', 'example/.github': 'project'}}}
        old = root/'project-20240101T000000Z'
        new = root/'project-20240201T000000Z-gpu'
        for prefix in (old, new, root/'native', root/'broken', root/'.hidden', root/'organization', root/'UPPER',
                       self.root/'external'):
            files.atomic_write(prefix/'bin/python', '#!/bin/sh\n', 0o700)
        (root/'project').symlink_to(old.name)
        (root/'external').symlink_to(self.root/'external')
        (root/'dangling').symlink_to('absent')
        (root/'shared').symlink_to('organization')
        (root/'hidden').symlink_to('.hidden')
        (root/'loop').symlink_to('loop')
        files.atomic_write(root/'noexec/bin/python', '#!/bin/sh\n', 0o600)
        (root/'venv/bin').mkdir(parents=True)
        (root/'venv/bin/python').symlink_to('/bin/sh')
        (root/'escaped-bin').mkdir()
        (root/'escaped-bin/bin').symlink_to(self.root/'external/bin')
        self.assertEqual(organization.ready_repositories(settings),
                         ['example/.github', 'example/native', 'example/project', 'example/staging', 'example/venv'])
        for repository in (None, 'foreign/project', 'example/..', 'example/.', 'example/broken', 'example/organization',
                           'example/.hidden', 'example/project-20240101T000000Z', 'example/escaped-bin'):
            with self.subTest(repository=repository):
                self.assertFalse(organization.environment_ready(settings, repository))
        self.assertTrue(organization.environment_ready(settings, 'exAmPle/StAgInG'))
        (root/'project').unlink()
        (root/'project').symlink_to(new.name)
        self.assertTrue(organization.build_ready(settings, 'example/staging', str(old)))
        self.assertFalse(organization.build_ready(settings, 'example/staging', str(root/'project')))
        self.assertFalse(organization.build_ready(settings, 'example/staging', str(self.root/'external')))
        self.assertFalse(organization.build_ready(settings, 'example/organization', str(old)))
        (old/'bin/python').unlink()
        self.assertFalse(organization.build_ready(settings, 'example/staging', str(old)))
        self.assertTrue(organization.environment_ready(settings, 'example/staging'))

    def test_cache_config_renders_host_options_and_accepts_empty_cache(self):
        host = self.organization_host()
        cache = self.root/'new'/'cache'
        host.update(env_cache=str(cache), env_legacy_root=str(self.root/'v1'),
                    env_cache_variant='cuda12.2', env_aliases={'Example/Staging': 'project'},
                    conda='/bin/sh')
        self.write_inputs()
        files.atomic_write(self.input/'organization.toml', '\n')
        config = self.config()
        _, control = self.read_plan(config)
        settings = control['organization']
        organization.validate_settings(settings, controller=True)
        self.assertEqual(settings, {'name': 'example', 'profile': DEFAULTS, 'env_cache': str(cache),
                                    'env_legacy_root': str(self.root/'v1'), 'env_cache_variant': 'cuda12.2',
                                    'env_aliases': {'example/staging': 'project'}, 'conda': '/bin/sh'})
        problems = deployment_plan.check_host(config, run=self.fake_run, network=False)
        self.assertFalse(any('environment' in p or 'PyYAML' in p for p in problems), problems)
        self.assertFalse(cache.exists())
        files.atomic_write(self.root/'legacy/bin/python', '#!/bin/sh\n', 0o700)
        self.assertFalse(organization.build_ready(control, 'example/staging', str(self.root/'legacy')))
        self.assertTrue(any(cmd[1:] == ['-I', '-c', 'import yaml'] for cmd in self.calls))
        cache.parent.write_text('obstructed')
        problems = deployment_plan.check_host(config, run=self.fake_run, network=False)
        self.assertTrue(any('writable directory' in p for p in problems))
        for override in ({'env': '/shared'}, {'env_root': '/envs'}, {'env_cache': '/'},
                         {'env_legacy_root': '../v1'}, {'env_cache_variant': 'invalid/path'}):
            with self.subTest(override=override), self.assertRaises(ValueError):
                organization.validate_settings({**settings, **override}, controller=True)

    def test_strict_preflight_checks_root_and_every_explicit_alias(self):
        host = self.organization_host(executor=True)
        root = self.root/'prefixes'
        host.update(env_root=str(root), env_aliases={'example/staging': 'project'})
        self.write_inputs()
        config = self.config(self.host_name)
        def problems():
            return [p for p in deployment_plan.check_host(config, run=self.fake_run, network=False)
                    if 'organization env' in p]
        self.assertTrue(any('missing' in p for p in problems()))
        files.atomic_write(root/'organization/bin/python', '#!/bin/sh\n', 0o700)
        empty = problems()
        self.assertTrue(any('no repository' in p for p in empty), empty)
        self.assertTrue(any('example/staging' in p for p in empty), empty)
        files.atomic_write(root/'unrelated/bin/python', '#!/bin/sh\n', 0o700)
        self.assertEqual(len(problems()), 1)
        files.atomic_write(root/'project/bin/python', '#!/bin/sh\n', 0o700)
        self.assertEqual(problems(), [])

    def test_controller_requires_profile_file_and_rejects_unknown_limits(self):
        self.organization_host()
        with self.assertRaises(FileNotFoundError):
            self.config()
        deployment_config.load(self.input, self.host_name, inventory_only=True)
        files.atomic_write(self.input/'organization.toml', '\n')
        self.assertEqual(self.config().organization_profile, DEFAULTS)
        for values in ('max_minutes = 0\n', 'retries = true\n', 'repository = "example/demo"\n'):
            with self.subTest(values=values):
                files.atomic_write(self.input/'organization.toml', values)
                with self.assertRaises(ValueError):
                    self.config()

    def test_mode_requires_schema_two_and_slurm_requires_driver_roles(self):
        host = self.organization_host()
        self.document['schema'] = 1
        self.write_inputs()
        with self.assertRaisesRegex(ValueError, 'requires deployment schema 2'):
            self.config()
        self.document['schema'] = 2
        host.update(slurm_runners=1)
        self.write_inputs()
        with self.assertRaisesRegex(ValueError, 'controller/executor'):
            self.config()

    def prepare_driver(self):
        host = self.organization_host()
        host.update(roles=['controller', 'executor'], runner_count=1, priority=5, slurm_runners=2)
        self.write_inputs()
        deploy.initialize(self.input)
        path = self.input/'slurm-allowlist.json'
        self.assertEqual(files.read_json(path), {'schema': 1, 'repositories': []})
        files.write_json(path, {'schema': 1, 'repositories': [BINDING]})
        deploy.initialize(self.input)
        files.atomic_write(self.input/'device-admission.toml', '[devices.3090a]\ntoken_file = "credentials/hosts/3090a"\n')
        files.atomic_write(self.input/'credentials/hosts/3090a', 'test-device')
        files.atomic_write(self.input/'credentials/slurm/acct1', 'test-key')
        files.atomic_write(self.input/'slurm/ssh_config', 'test-config')
        files.atomic_write(self.input/'slurm/known_hosts', 'test-hosts')
        site = self.input/'slurm/cluster1.toml'
        files.atomic_write(site, (deployment_plan.SOURCE/'setup/slurm/site.example.toml').read_bytes())
        return site, path

    def test_enabled_controller_renders_only_site_runners_and_live_allowlist(self):
        site, path = self.prepare_driver()
        files.atomic_write(self.input/'projects/example.toml', 'invalid stale project')
        config = self.config()
        plan, control = self.read_plan(config)
        organization.validate_settings(control['organization'], controller=True)
        self.assertEqual(control['organization']['slurm_allowlist_file'], str(path))
        self.assertFalse(any('/projects/' in name for name in plan['files']))
        self.assertIn(str(config.etc/'slurm'/site.name), plan['files'])
        self.assertEqual([r['name'] for r in plan['runners'] if r['name'].startswith('slurm')], ['slurm0', 'slurm1'])
        fake_keygen = mock.Mock(return_value=mock.Mock(returncode=0, stdout=b'ssh-ed25519 EXAMPLE'))
        self.assertFalse(deploy.slurm_package(config, 'cluster1', 'acct1', self.root/'bundle',
                                             run=fake_keygen)['private_key_included'])
        self.assertTrue((self.root/'bundle/site.toml').is_file())
        self.assertFalse((self.root/'bundle/slurm-allowlist.json').exists())

    def test_driver_requires_site_and_valid_project_enabled_cpu_target(self):
        site, path = self.prepare_driver()
        for target in ('missing/cpu', 'cluster1/missing', 'cluster1/a40'):
            with self.subTest(target=target):
                files.write_json(path, {'schema': 1, 'repositories': [{**BINDING, 'slurm_target': target}]})
                with self.assertRaisesRegex(ValueError, 'enabled Slurm CPU target'):
                    self.config()
        files.write_json(path, {'schema': 1, 'repositories': [BINDING]})
        document = deployment_config.read_toml(site)
        document['targets']['cpu']['enabled'] = ['other-project']
        files.atomic_write(site, deployment_config.toml_document(document))
        with self.assertRaisesRegex(ValueError, 'enabled Slurm CPU target'):
            self.config()
        self.assertIsNone(organization.read_slurm_target(self.input, BINDING))
        document['targets']['cpu']['enabled'] = [['invalid']]
        files.atomic_write(site, deployment_config.toml_document(document))
        self.assertIsNone(organization.read_slurm_target(self.input, BINDING))
        document['targets']['cpu']['enabled'] = ['project-a']
        files.atomic_write(site, deployment_config.toml_document(document))
        self.assertEqual(organization.read_slurm_target(self.input, BINDING).name, 'cpu')
        document['accounts']['acct1']['retired'] = True
        files.atomic_write(site, deployment_config.toml_document(document))
        self.assertIsNone(organization.read_slurm_target(self.input, BINDING))
        path.unlink()
        with self.assertRaisesRegex(ValueError, 'allowlist'):
            self.config()
        site.unlink()
        self.assertIsNone(organization.read_slurm_target(self.input, BINDING))
        with self.assertRaisesRegex(ValueError, 'site configuration'):
            self.config()
        deployment_config.load(self.input, self.host_name, inventory_only=True)

    def test_allowlist_binds_verified_id_and_name_and_refuses_bad_files(self):
        path = self.root/'slurm-allowlist.json'
        files.write_json(path, {'schema': 1, 'repositories': [{**BINDING, 'repository': 'Example/Project'}]})
        bound = organization.read_slurm_allowlist(path, 'Example')
        self.assertEqual(organization.slurm_binding(bound, BINDING), BINDING)
        for override in (dict(repository='example/another'), dict(repository_id=99), dict(repository_id=True)):
            self.assertIsNone(organization.slurm_binding(bound, {**BINDING, **override}))
        for entries in ([{**BINDING, 'repository': 'outside/project'}], [{**BINDING, 'slurm_target': '../cpu'}],
                        [{**BINDING, 'slurm_project': '../project'}], [{**BINDING, 'repository_id': True}],
                        [BINDING, dict(BINDING, repository='example/another')], [BINDING, dict(BINDING, repository_id=99)]):
            with self.subTest(entries=entries):
                files.write_json(path, {'schema': 1, 'repositories': entries})
                with self.assertRaises(protocol.Error):
                    organization.read_slurm_allowlist(path, 'Example')
        for raw in ('{}', '{"schema":1,"schema":1,"repositories":[]}', 'null', 'not JSON'):
            files.atomic_write(path, raw)
            with self.assertRaises(protocol.Error):
                organization.read_slurm_allowlist(path, 'Example')
        files.write_json(path, {'schema': 1, 'repositories': []})
        self.assertEqual(organization.read_slurm_allowlist(path, 'example'), {})
        path.chmod(0o644)
        with self.assertRaises(protocol.Error):
            organization.read_slurm_allowlist(path, 'example')

    def test_socket_path_checked_in_offline_preflight_and_plan(self):
        self.prepare_driver()
        self.document['hosts']['3090a']['runtime'] = str(self.root/('r' * 96))
        self.write_inputs()
        config = self.config()
        problems = deployment_plan.check_host(config, run=self.fake_run, network=False)
        self.assertTrue(any('socket' in problem.lower() for problem in problems), problems)
        with self.assertRaisesRegex(ValueError, 'socket'):
            deployment_plan.build(config)

    def test_preflight_requires_executable_python_in_shared_environment(self):
        self.organization_host(executor=True)
        config = self.config(self.host_name)
        prefix = config.runtime/'ci-runners/envs/organization'
        files.atomic_write(prefix/'bin/python', '#!/bin/sh\nexit 0\n', 0o600)
        problems = deployment_plan.check_host(config, run=self.fake_run, network=False)
        self.assertTrue(any('organization environment' in item for item in problems))
        (prefix/'bin/python').chmod(0o700)
        problems = deployment_plan.check_host(config, run=self.fake_run, network=False)
        self.assertFalse(any('organization environment' in item for item in problems))

    def test_controller_preflight_checks_organization_endpoint_without_repo_inputs(self):
        self.organization_host()
        files.atomic_write(self.input/'organization.toml', '\n')
        config = self.config()
        with mock.patch.object(deployment_plan.github, 'api', return_value={}) as api:
            problems = deployment_plan.check_host(config, run=self.fake_run)
        self.assertEqual(api.call_count, 1)
        self.assertEqual(api.call_args.args[:2], ('GET', 'https://api.github.com/orgs/example/repos?type=all&per_page=1'))
        self.assertTrue(any('control/GitHub connection check failed' in item for item in problems))

    def test_init_writes_only_organization_inputs_for_selected_role(self):
        for sample, controller in (('host-organization', True), ('host-executor-organization', False)):
            with self.subTest(sample=sample):
                directory = self.root/sample
                template = deployment_plan.SOURCE/'parallel/examples'/f'{sample}.example.toml'
                files.atomic_write(directory/'host.toml', template.read_bytes())
                deploy.initialize(directory)
                self.assertEqual((directory/'organization.toml').exists(), controller)
                self.assertEqual((directory/'device-admission.toml').exists(), controller)
                self.assertEqual((directory/'credentials/pat/github-pat').exists(), controller)
                self.assertEqual(list((directory/'projects').iterdir()), [])
                self.assertEqual((directory/'host.toml').read_bytes(), template.read_bytes())

    def test_output_excludes_stale_project_copies_without_deleting_runtime_inputs(self):
        self.organization_host()
        files.atomic_write(self.input/'organization.toml', '\n')
        config = self.config()
        old_project = config.etc/'projects/example.toml'
        old_copy = config.runtime/'output/projects/example.toml'
        files.atomic_write(old_project, 'previous managed project\n')
        files.atomic_write(old_copy, 'previous inspection copy\n')
        snapshot = config.runtime/'output/repositories.json'
        self.assertIn(snapshot.name, deployment_output.refresh(config)['missing'])
        files.write_json(snapshot, {'repositories': []})
        self.assertIn(snapshot.name, deployment_output.refresh(config)['files'])
        self.assertFalse(old_copy.exists())
        self.assertEqual(old_project.read_text(), 'previous managed project\n')
