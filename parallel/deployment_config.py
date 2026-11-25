"""Read deployment inputs and render the existing runtime contracts."""
from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import pwd
import re
import tomllib
import urllib.parse
from pathlib import Path

from ci_runners import inventory, policy, registry, setup, slurmsite
from parallel import organization, protocol


def toml_value(value):
    if isinstance(value, dict):
        return '{ ' + ', '.join(f'{json.dumps(k)} = {toml_value(v)}' for k, v in value.items()) + ' }'
    if isinstance(value, list):
        return '[' + ', '.join(toml_value(v) for v in value) + ']'
    return json.dumps(value, ensure_ascii=True, allow_nan=False)


def toml_document(document):
    lines = []
    for key, value in document.items():
        if not isinstance(value, dict):
            lines.append(f'{json.dumps(key)} = {toml_value(value)}')
    for key, value in document.items():
        if isinstance(value, dict):
            lines.extend(['', f'[{json.dumps(key)}]'])
            lines.extend(f'{json.dumps(name)} = {toml_value(item)}' for name, item in value.items())
    return '\n'.join(lines) + '\n'


def read_toml(path):
    with Path(path).open('rb') as stream:
        return tomllib.load(stream)


def absolute_path(value, field):
    if not isinstance(value, str) or not re.fullmatch(r'/[A-Za-z0-9._/-]+', value) or '..' in Path(value).parts:
        raise ValueError(f'{field} must be an absolute path without whitespace or parent traversal')
    return value


def checked_keys(document, allowed, required, field):
    if not isinstance(document, dict) or set(document) - set(allowed) or set(required) - set(document):
        raise ValueError(f'{field}: required keys {sorted(required)}, allowed keys {sorted(allowed)}')


@dataclasses.dataclass
class DeploymentConfig:
    input_dir: Path
    host_name: str
    document: dict
    projects: dict
    sites: dict
    organization_profile: dict | None = None

    @property
    def host(self):
        return self.document['hosts'][self.host_name]

    @property
    def controller_name(self):
        return next((name for name, host in self.document['hosts'].items() if 'controller' in host['roles']), None)

    @property
    def is_controller(self):
        return 'controller' in self.host['roles']

    @property
    def repository_members(self):
        return self.document['schema'] == 2

    @property
    def organization_mode(self):
        return self.document.get('mode') == 'organization'

    @property
    def organization_executor(self):
        return self.organization_mode and not self.is_controller

    @property
    def verify_resource_limits(self):
        return self.host.get('verify_resource_limits', True)

    @property
    def organization_settings(self):
        settings = {'name': self.document['github']['organization']}
        if 'env_cache' in self.host:
            settings['env_cache'] = self.host['env_cache']
            for key in ('conda', 'env_aliases', 'env_legacy_root', 'env_cache_variant'):
                if key in self.host:
                    settings[key] = self.host[key]
        elif 'env_root' in self.host:
            settings['env_root'] = self.host['env_root']
            if 'env_aliases' in self.host:
                settings['env_aliases'] = self.host['env_aliases']
        else:
            settings['env'] = self.host.get('env', str(self.runtime / 'ci-runners/envs/organization'))
        if self.organization_profile is not None:
            settings['profile'] = self.organization_profile
        if self.is_controller and self.host.get('slurm_runners', 0):
            settings['slurm_allowlist_file'] = str(self.input_dir / 'slurm-allowlist.json')
        from parallel import reporting, source_selection, timing
        for key, name, validate, controller_only in (
                ('source_selectors_file', 'source-selectors.json', source_selection.read_policy, True),
                ('report_gates_file', 'report-gates.json', reporting.read_policy, True),
                ('timing_profiles_file', 'timing-profiles.json', timing.read_profiles, False)):
            path = self.input_dir / name
            if (self.is_controller or not controller_only) and path.exists():
                validate(path)
                settings[key] = str(path)
        return settings

    @property
    def device_admission_file(self):
        return self.input_dir / 'device-admission.toml'

    @property
    def runtime(self):
        return Path(self.host['runtime'])

    @property
    def etc(self):
        return self.runtime / 'ci-runners/etc'

    @property
    def control(self):
        return self.runtime / 'parallel'

    @property
    def private(self):
        return self.control / 'etc/private'

    @property
    def unit_dir(self):
        return Path(self.host['home']) / '.config/systemd/user'

    def kept_record(self, project):
        path = (self.input_dir/project['environment']['record']).resolve()
        if (self.input_dir not in path.parents or
                path.relative_to(self.input_dir).parts[0] in ('credentials', 'tls')):
            raise ValueError('kept environment records must stay inside the input and outside private credential directories')
        return path

    def public_inputs(self):
        inputs = {'host': self.host_name, 'topology': self.document, 'projects': self.projects, 'sites': self.sites}
        if self.organization_profile is not None:
            inputs['organization_profile'] = self.organization_profile
        if self.organization_mode:
            fingerprints = {}
            for name in ('source-selectors.json', 'report-gates.json', 'timing-profiles.json'):
                path = self.input_dir / name
                if path.exists() and (self.is_controller or name == 'timing-profiles.json'):
                    from parallel import deployment_files
                    fingerprints[name] = hashlib.sha256(deployment_files.read_private(path)).hexdigest()
            if fingerprints:
                inputs['feature_policy_sha256'] = fingerprints
        return inputs

    def inventory_document(self):
        hosts = {}
        for name, specification in self.document['hosts'].items():
            pools = {}
            if 'controller' in specification['roles']:
                pools['plan'] = {'class': 'plan', 'count': 1}
                if self.repository_members or any(spec.get('gpu_uuids') for spec in self.document['hosts'].values()):
                    pools['plan'].update(count=1 + len(protocol.GPU_WAIT_LANES), gpu_waiters=True)
            if 'executor' in specification['roles']:
                count = specification['runner_count'] if self.repository_members else cpu_count(specification) // protocol.GROUP_CPUS
                pools['cpu'] = {'class': 'executor', 'device': name, 'count': count}
            if specification.get('slurm_runners', 0):
                pools['slurm'] = {'class': 'executor', 'device': 'slurm', 'count': specification['slurm_runners']}
            runtime = specification['runtime']
            hosts[name] = dict(account=specification['account'], home=specification['home'], data=runtime,
                               slots=1, memory_max=specification['memory_max'], disk_min_gib=specification.get('disk_min_gib', 20),
                               gpus=[], max_temp_c=80, pools=pools,
                               python=specification.get('python', '/usr/bin/python3'),
                               files={'slot_lock': runtime+'/ci-runners/state/slot.lock',
                                      'slot_queue': runtime+'/ci-runners/state/slot.queue'})
            profile_file = self.input_dir/'timing-profiles.json'
            if self.organization_mode and 'executor' in specification['roles'] and profile_file.exists():
                from parallel import timing
                profiles = [p for p in timing.read_profiles(profile_file).values() if p['device'] == name]
                if profiles:
                    if (any(p['cpu_count'] != cpu_count(specification) for p in profiles)
                            or len({p['host_lock'] for p in profiles}) != 1):
                        raise ValueError('timing profiles must bind the complete local CPU budget and one inventory lock')
                    hosts[name]['files']['host_lock'] = profiles[0]['host_lock']
            if specification.get('gpu_uuids'):
                hosts[name]['gpu_uuids'] = specification['gpu_uuids']
        return {'schema': 2, 'this': self.host_name, 'hosts': hosts}

    def inventory(self):
        return inventory.load(toml_document(self.inventory_document()))

    def runner_specs(self):
        return setup.runners(self.inventory())

    def project_document(self, name):
        document = copy.deepcopy(self.projects[name])
        for key in ('actors', 'repository_id', 'environment'):
            document.pop(key, None)
        overrides = document.get('hosts', {})
        hosts = {}
        for role, host in self.document['hosts'].items():
            hosts[role] = {'memory_max': host['memory_max'],
                           'env': str(Path(host['runtime']) / 'ci-runners/envs' / name), **overrides.get(role, {})}
        if self.controller_name is not None:
            hosts['plan'] = dict(hosts[self.controller_name], **overrides.get('plan', {}))
        document['hosts'] = hosts
        return document


def cpu_ids(value):
    ranges = inventory.cpu_ranges(value)
    if not ranges:
        raise ValueError('cpus must name a nonempty, nonoverlapping CPU set')
    cpus = [cpu for lower, upper in ranges for cpu in range(lower, upper+1)]
    if len(set(cpus)) != len(cpus):
        raise ValueError('cpus contains overlaps')
    return cpus


def cpu_count(host):
    # Legacy deployment inputs may name a CPU list; only its cardinality survives.
    if 'cpu_count' in host and 'cpus' in host:
        raise ValueError('use cpu_count or legacy cpus, not both')
    count = host.get('cpu_count') if 'cpu_count' in host else len(cpu_ids(host.get('cpus', '')))
    if not protocol.positive(count) or count % protocol.GROUP_CPUS:
        raise ValueError(f'cpu_count must be a positive multiple of {protocol.GROUP_CPUS}')
    return count


def validate_host(name, host, *, schema=1, organization_mode=False):
    allowed = {'roles', 'account', 'home', 'runtime', 'cpus', 'cpu_count', 'memory_max',
               'slurm_runners', 'disk_min_gib', 'python', 'conda', 'gpu_uuids', 'verify_resource_limits'}
    if schema == 2:
        allowed.update(('runner_count', 'priority'))
    if organization_mode:
        allowed.update(('env', 'env_root', 'env_aliases', 'env_cache', 'env_legacy_root', 'env_cache_variant'))
    checked_keys(host, allowed, {'roles', 'account', 'home', 'runtime', 'memory_max'}, f'hosts.{name}')
    if name not in protocol.DEVICES - {'slurm'}:
        raise ValueError(f'host {name}: use a supported schema 2 device name')
    roles = host['roles']
    if (not isinstance(roles, list) or not roles or
            any(role not in ('controller', 'executor') for role in roles) or len(set(roles)) != len(roles)):
        raise ValueError(f'host {name}: roles must contain controller and/or executor')
    if 'verify_resource_limits' in host and (type(host['verify_resource_limits']) is not bool or 'executor' not in roles):
        raise ValueError(f'host {name}: verify_resource_limits must be a boolean executor setting')
    protocol.validate_gpu_list(name if 'executor' in roles else '', host.get('gpu_uuids', []))
    if not isinstance(host['account'], str) or not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', host['account']):
        raise ValueError(f'host {name}: invalid account')
    for key in ('home', 'runtime', 'python', 'conda', 'env'):
        if key in host:
            absolute_path(host[key], f'{name}.{key}')
    memory = registry.memory_bytes(host['memory_max'])
    if 'executor' in roles:
        count = cpu_count(host)
        groups = count // protocol.GROUP_CPUS
        protocol.runner_names(name, groups)
        if schema == 2:
            runner_count = host.get('runner_count')
            priority = host.get('priority')
            if type(runner_count) is not int or runner_count != groups:
                raise ValueError(f'{name}: runner_count must equal the number of CPU groups ({groups})')
            if type(priority) is not int or not -(1 << 31) <= priority < (1 << 31):
                raise ValueError(f'{name}: priority must be a signed 32-bit integer')
        if memory < count * (1 << 30):
            raise ValueError(f'{name}: memory_max cannot cover its configured CPU groups')
    if 'executor' not in roles and {'runner_count', 'priority'} & host.keys():
        raise ValueError(f'{name}: runner_count and priority are executor settings')
    slurm_count = host.get('slurm_runners', 0)
    if type(slurm_count) is not int or slurm_count < 0:
        raise ValueError('slurm_runners must be a nonnegative integer')
    if slurm_count:
        if not {'controller', 'executor'} <= set(roles):
            raise ValueError('Slurm drivers must be on the controller/executor host')
        protocol.runner_names('slurm', slurm_count)


def load(input_dir, host_name, *, inventory_only=False):
    input_dir = Path(input_dir).resolve()
    document = read_toml(input_dir / 'host.toml')
    required = {'schema', 'github', 'controller', 'hosts'}
    checked_keys(document, required | {'mode'}, required, 'host.toml')
    schema = document['schema']
    if type(schema) is not int or schema not in (1, 2):
        raise ValueError('deployment input schema must be 1 or 2')
    if 'mode' in document and (schema != 2 or document['mode'] != 'organization'):
        raise ValueError('host.toml mode must be organization and requires deployment schema 2')
    organization_mode = 'mode' in document
    hosts = document['hosts']
    if not isinstance(hosts, dict) or host_name not in hosts:
        raise ValueError('select a configured host')
    if schema == 2 and set(hosts) != {host_name}:
        raise ValueError('deployment schema 2 describes only the local host')
    for name, host in hosts.items():
        validate_host(name, host, schema=schema, organization_mode=organization_mode)
    is_controller = 'controller' in hosts[host_name]['roles']
    if schema == 1 and sum('controller' in h['roles'] for h in hosts.values()) != 1:
        raise ValueError('select a configured host; exactly one host must have the controller role')
    github = document['github']
    checked_keys(github, {'organization', 'runner_group'}, {'organization', 'runner_group'}, 'github')
    if (not isinstance(github['organization'], str) or not policy._ORG.fullmatch(github['organization'])
            or not isinstance(github['runner_group'], str) or not re.fullmatch(r'[A-Za-z0-9_. -]{1,64}', github['runner_group'])):
        raise ValueError('invalid GitHub organization or runner group')
    if organization_mode:
        organization.validate_environment(hosts[host_name], github['organization'], require_path=False)
    controller = document['controller']
    endpoint_keys = {'url', 'bind'} if schema == 1 or is_controller else {'url'}
    checked_keys(controller, endpoint_keys, endpoint_keys, 'controller')
    if not isinstance(controller['url'], str):
        raise ValueError('controller.url must be an HTTPS origin with an explicit port')
    endpoint = urllib.parse.urlsplit(controller['url'])
    if (endpoint.scheme != 'https' or not endpoint.hostname or endpoint.username or endpoint.password
            or endpoint.query or endpoint.fragment or endpoint.path not in ('', '/') or not endpoint.port):
        raise ValueError('controller.url must be an HTTPS origin with an explicit port')
    if 'bind' in controller and (not isinstance(controller['bind'], str)
                                 or not re.fullmatch(r'[A-Za-z0-9.:-]+', controller['bind'])):
        raise ValueError('invalid controller.bind')
    runtime = Path(document['hosts'][host_name]['runtime']).resolve()
    document['hosts'][host_name]['runtime'] = str(runtime)
    document['hosts'][host_name]['home'] = str(Path(document['hosts'][host_name]['home']).resolve())
    source = Path(__file__).resolve().parents[1]
    if any(a == b or a in b.parents or b in a.parents for a, b in ((runtime, source), (runtime, input_dir), (source, input_dir))):
        raise ValueError('source, input and runtime directories must be separate, non-nested trees')
    if inventory_only or organization_mode:
        profile = (organization.profile(read_toml(input_dir / 'organization.toml'))
                   if organization_mode and is_controller and not inventory_only else None)
        config = DeploymentConfig(input_dir, host_name, document, {}, {}, profile)
        config.inventory()
        if not inventory_only and config.host.get('slurm_runners', 0):
            config.sites, parsed_sites = load_sites(input_dir)
            if not config.sites:
                raise ValueError('organization Slurm drivers need a site configuration')
            for binding in organization.read_slurm_allowlist(
                    config.organization_settings['slurm_allowlist_file'], github['organization']).values():
                site = parsed_sites.get(binding['slurm_target'].split('/')[0])
                if site is None or organization.slurm_cpu_target(site, binding) is None:
                    raise ValueError(f"{binding['repository']}: no enabled Slurm CPU target for {binding['slurm_project']}")
        return config
    projects = {path.stem: read_toml(path) for path in sorted((input_dir/'projects').glob('*.toml'))}
    if not projects:
        raise ValueError('projects must contain at least one project TOML')
    sites, _ = load_sites(input_dir)
    config = DeploymentConfig(input_dir, host_name, document, projects, sites)
    config.inventory()
    for name, project in projects.items():
        actors = project.get('actors', [])
        if schema == 1 and (not isinstance(actors, list) or not actors or
                            any(not isinstance(a, str) or not policy._LOGIN.fullmatch(a) for a in actors)):
            raise ValueError(f'{name}: actors must explicitly list trusted GitHub logins')
        if project.get('repository_id') is not None and not protocol.positive(project['repository_id']):
            raise ValueError(f'{name}: repository_id must be a positive integer')
        environment = project.get('environment', {'mode': 'existing'})
        checked_keys(environment, {'mode', 'record'}, {'mode'}, f'{name}.environment')
        if environment['mode'] not in ('existing', 'build', 'kept'):
            raise ValueError(f'{name}: environment.mode must be existing, build or kept')
        if schema == 2 and not is_controller and environment['mode'] == 'build':
            raise ValueError(f'{name}: executor environment.mode must be existing or kept')
        if environment['mode'] == 'kept' and not protocol.relative(environment.get('record', '')):
            raise ValueError(f'{name}: kept environment needs an input-relative record path')
        registered = config.project_document(name)
        for role in registered['hosts']:
            registry.parse(registered, role, name)
        if registered['repository'].split('/')[0].lower() != github['organization'].lower():
            raise ValueError(f'{name}: repository must belong to the configured organization')
        if registered['repository'].lower() == policy.SELF:
            raise ValueError('this repository cannot run on its own self-hosted runners')
    if schema == 1 and len({tuple(sorted(project['actors'])) for project in projects.values()}) != 1:
        raise ValueError('the existing host policy uses one trust set; projects must declare the same actors')
    needs_slurm_site = schema == 1 or is_controller
    if needs_slurm_site and any('slurm' in p.get('devices', []) for p in projects.values()) and not sites:
        raise ValueError('projects requesting Slurm need a site configuration')
    return config


def load_sites(input_dir):
    sites, parsed_sites = {}, {}
    for path in sorted((input_dir/'slurm').glob('*.toml')):
        document = read_toml(path)
        site = slurmsite.load(toml_document(document))
        if path.stem != site.name:
            raise ValueError('Slurm filename must match site.name')
        sites[site.name] = document
        parsed_sites[site.name] = site
    return sites, parsed_sites


def runtime_files(config, repository_ids, release):
    """All generated non-secret files, including their final modes."""
    inv = config.inventory()
    files = {}
    def add(path, text, mode=0o600):
        files[str(path)] = {'content': text, 'mode': mode}
    add(config.etc/'inventory.toml', toml_document(config.inventory_document()))
    actors = [] if config.repository_members else sorted(
        {actor for project in config.projects.values() for actor in project['actors']})
    repositories = {}
    for name, project in config.projects.items():
        registered = config.project_document(name)
        add(config.etc/'projects'/f'{name}.toml', toml_document(registered))
        repository = project['repository'].lower()
        repository_id = repository_ids.get(name)
        if repository_id is None and config.repository_members and not config.is_controller:
            continue  # The executor gets the repository identity from the controller claim.
        if not protocol.positive(repository_id):
            raise ValueError(f'{name}: missing or invalid trusted repository ID')
        entry = repositories.setdefault(repository, {'id': repository_id, 'visibility': 'private', 'classes': []})
        if entry['id'] != repository_id:
            raise ValueError(f'{repository}: projects specify conflicting repository IDs')
        if 'slurm' in project.get('devices', []):
            entry['classes'] = ['slurm']
    policy_document = {'schema': 2, 'actors': actors, 'repositories': repositories}
    policy.load(toml_document(policy_document))
    add(config.etc/'policy.toml', toml_document(policy_document))
    control = {'schema': 1}
    if config.repository_members:
        control['repository_members'] = True
    if config.organization_mode:
        control['organization'] = config.organization_settings
    uid = pwd.getpwnam(config.host['account']).pw_uid
    if config.is_controller:
        endpoint = urllib.parse.urlsplit(config.document['controller']['url'])
        control['plan'] = dict(database=str(config.control/'state/scheduler.sqlite'), socket=str(config.control/'run/plan.sock'),
                               runner_uids=[uid], bind=config.document['controller']['bind'], port=endpoint.port,
                               tls_certificate=str(config.private/'server.crt'), tls_key=str(config.private/'server.key'),
                               github_token_file=str(config.private/'github-pat'))
        if config.repository_members:
            control['plan']['device_admission_file'] = str(config.device_admission_file)
        else:
            control['hosts'] = {name: {'token_file': str(config.private/'hosts'/name)}
                                for name, host in config.document['hosts'].items() if 'executor' in host['roles']}
    if 'executor' in config.host['roles']:
        control['executor'] = {'url': config.document['controller']['url'], 'token_file': str(config.private/'host-token'),
                               'ca_file': str(config.private/'ca.crt'), 'runner_uids': [uid],
                               'verify_resource_limits': config.verify_resource_limits}
        if config.repository_members:
            host = config.host
            control['executor']['registration'] = dict(
                device=config.host_name, cpu_count=cpu_count(host), memory_max=host['memory_max'],
                gpu_uuids=host.get('gpu_uuids', []), runner_count=host['runner_count'], priority=host['priority'])
    add(config.control/'etc/central.toml', toml_document(control))
    for name in ('job-started.sh', 'job-completed.sh'):
        text = (release/'hooks'/name).read_text()
        if name == 'job-completed.sh' and config.organization_executor:
            text = text.replace('"$root" job-completed\n', '"$root" job-completed --organization\n')
        add(config.runtime/'ci-runners/hooks'/name, setup.hook_text(text, setup.python_of(inv), str(config.etc.parent), str(config.etc)), 0o755)
    for name, labels, role in config.runner_specs():
        pool = 'plan' if role == 'plan' else 'slurm' if name.startswith('slurm') else 'cpu'
        add(config.runtime/'actions-runner'/name/'.env', setup.env_file(inv, role, str(config.etc), pool=pool))
    if config.host.get('slurm_runners', 0):
        for name, site in config.sites.items():
            add(config.etc/'slurm'/f'{name}.toml', toml_document(site))
    return files
