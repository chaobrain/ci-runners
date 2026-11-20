"""Read deployment inputs and render the existing runtime contracts."""
from __future__ import annotations

import copy
import dataclasses
import json
import pwd
import re
import tomllib
import urllib.parse
from pathlib import Path

from ci_runners import inventory, policy, registry, setup, slurmsite
from parallel import protocol


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

    @property
    def host(self):
        return self.document['hosts'][self.host_name]

    @property
    def controller_name(self):
        return next(name for name, host in self.document['hosts'].items() if 'controller' in host['roles'])

    @property
    def is_controller(self):
        return self.host_name == self.controller_name

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
        return {'host': self.host_name, 'topology': self.document, 'projects': self.projects, 'sites': self.sites}

    def inventory_document(self):
        hosts = {}
        for name, specification in self.document['hosts'].items():
            pools = {}
            if 'controller' in specification['roles']:
                pools['plan'] = {'class': 'plan', 'count': 1}
                if any(spec.get('gpu_uuids') for spec in self.document['hosts'].values()):
                    pools['plan'].update(count=1 + len(protocol.GPU_WAIT_LANES), gpu_waiters=True)
            if 'executor' in specification['roles']:
                count = cpu_count(specification) // protocol.GROUP_CPUS
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


def validate_host(name, host):
    checked_keys(host, {'roles', 'account', 'home', 'runtime', 'cpus', 'cpu_count', 'memory_max', 'slurm_runners',
                        'disk_min_gib', 'python', 'conda', 'gpu_uuids'}, {'roles', 'account', 'home', 'runtime', 'memory_max'}, f'hosts.{name}')
    if name not in protocol.DEVICES - {'slurm'}:
        raise ValueError(f'host {name}: use a supported schema 2 device name')
    roles = host['roles']
    if not isinstance(roles, list) or not roles or len(set(roles)) != len(roles) or set(roles) - {'controller', 'executor'}:
        raise ValueError(f'host {name}: roles must contain controller and/or executor')
    protocol.validate_gpu_list(name if 'executor' in roles else '', host.get('gpu_uuids', []))
    if not isinstance(host['account'], str) or not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', host['account']):
        raise ValueError(f'host {name}: invalid account')
    for key in ('home', 'runtime', 'python', 'conda'):
        if key in host:
            absolute_path(host[key], f'{name}.{key}')
    memory = registry.memory_bytes(host['memory_max'])
    if 'executor' in roles:
        count = cpu_count(host)
        protocol.runner_names(name, count//protocol.GROUP_CPUS)
        if memory < count * (1 << 30):
            raise ValueError(f'{name}: memory_max cannot cover its configured CPU groups')
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
    checked_keys(document, {'schema', 'github', 'controller', 'hosts'}, {'schema', 'github', 'controller', 'hosts'}, 'host.toml')
    if document['schema'] != 1:
        raise ValueError('deployment input schema must be 1')
    github = document['github']
    checked_keys(github, {'organization', 'runner_group'}, {'organization', 'runner_group'}, 'github')
    if not policy._ORG.fullmatch(github['organization']) or not re.fullmatch(r'[A-Za-z0-9_. -]{1,64}', github['runner_group']):
        raise ValueError('invalid GitHub organization or runner group')
    controller = document['controller']
    checked_keys(controller, {'url', 'bind'}, {'url', 'bind'}, 'controller')
    endpoint = urllib.parse.urlsplit(controller['url'])
    if (endpoint.scheme != 'https' or not endpoint.hostname or endpoint.username or endpoint.password
            or endpoint.query or endpoint.fragment or endpoint.path not in ('', '/') or not endpoint.port):
        raise ValueError('controller.url must be an HTTPS origin with an explicit port')
    if not re.fullmatch(r'[A-Za-z0-9.:-]+', controller['bind']):
        raise ValueError('invalid controller.bind')
    for name, host in document['hosts'].items():
        validate_host(name, host)
    if host_name not in document['hosts'] or sum('controller' in h['roles'] for h in document['hosts'].values()) != 1:
        raise ValueError('select a configured host; exactly one host must have the controller role')
    runtime = Path(document['hosts'][host_name]['runtime']).resolve()
    document['hosts'][host_name]['runtime'] = str(runtime)
    document['hosts'][host_name]['home'] = str(Path(document['hosts'][host_name]['home']).resolve())
    source = Path(__file__).resolve().parents[1]
    if any(a == b or a in b.parents or b in a.parents for a, b in ((runtime, source), (runtime, input_dir), (source, input_dir))):
        raise ValueError('source, input and runtime directories must be separate, non-nested trees')
    if inventory_only:
        config = DeploymentConfig(input_dir, host_name, document, {}, {})
        config.inventory()
        return config
    projects = {path.stem: read_toml(path) for path in sorted((input_dir/'projects').glob('*.toml'))}
    if not projects:
        raise ValueError('projects must contain at least one project TOML')
    sites = {path.stem: read_toml(path) for path in sorted((input_dir/'slurm').glob('*.toml'))}
    config = DeploymentConfig(input_dir, host_name, document, projects, sites)
    config.inventory()
    for name, project in projects.items():
        actors = project.get('actors', [])
        if not isinstance(actors, list) or not actors or any(not isinstance(a, str) or not policy._LOGIN.fullmatch(a) for a in actors):
            raise ValueError(f'{name}: actors must explicitly list trusted GitHub logins')
        if project.get('repository_id') is not None and not protocol.positive(project['repository_id']):
            raise ValueError(f'{name}: repository_id must be a positive integer')
        environment = project.get('environment', {'mode': 'existing'})
        checked_keys(environment, {'mode', 'record'}, {'mode'}, f'{name}.environment')
        if environment['mode'] not in ('existing', 'build', 'kept'):
            raise ValueError(f'{name}: environment.mode must be existing, build or kept')
        if environment['mode'] == 'kept' and not protocol.relative(environment.get('record', '')):
            raise ValueError(f'{name}: kept environment needs an input-relative record path')
        registered = config.project_document(name)
        for role in registered['hosts']:
            registry.parse(registered, role, name)
        if registered['repository'].split('/')[0].lower() != github['organization'].lower():
            raise ValueError(f'{name}: repository must belong to the configured organization')
        if registered['repository'].lower() == policy.SELF:
            raise ValueError('this repository cannot run on its own self-hosted runners')
    if len({tuple(sorted(project['actors'])) for project in projects.values()}) != 1:
        raise ValueError('the existing host policy uses one trust set; projects must declare the same actors')
    for name, site_document in sites.items():
        site = slurmsite.load(toml_document(site_document))
        if name != site.name:
            raise ValueError('Slurm filename must match site.name')
    if any('slurm' in p.get('devices', []) for p in projects.values()) and not sites:
        raise ValueError('projects requesting Slurm need a site configuration')
    return config


def runtime_files(config, repository_ids, release):
    """All generated non-secret files, including their final modes."""
    inv = config.inventory()
    files = {}
    def add(path, text, mode=0o600):
        files[str(path)] = {'content': text, 'mode': mode}
    add(config.etc/'inventory.toml', toml_document(config.inventory_document()))
    actors = sorted({actor for project in config.projects.values() for actor in project['actors']})
    repositories = {}
    for name, project in config.projects.items():
        registered = config.project_document(name)
        add(config.etc/'projects'/f'{name}.toml', toml_document(registered))
        repository = project['repository'].lower()
        entry = repositories.setdefault(repository, {'id': repository_ids[name], 'visibility': 'private', 'classes': []})
        if entry['id'] != repository_ids[name]:
            raise ValueError(f'{repository}: projects specify conflicting repository IDs')
        if 'slurm' in project.get('devices', []):
            entry['classes'] = ['slurm']
    policy_document = {'schema': 2, 'actors': actors, 'repositories': repositories}
    policy.load(toml_document(policy_document))
    add(config.etc/'policy.toml', toml_document(policy_document))
    control = {'schema': 1}
    uid = pwd.getpwnam(config.host['account']).pw_uid
    if config.is_controller:
        endpoint = urllib.parse.urlsplit(config.document['controller']['url'])
        control['plan'] = dict(database=str(config.control/'state/scheduler.sqlite'), socket=str(config.control/'run/plan.sock'),
                               runner_uids=[uid], bind=config.document['controller']['bind'], port=endpoint.port,
                               tls_certificate=str(config.private/'server.crt'), tls_key=str(config.private/'server.key'),
                               github_token_file=str(config.private/'github-pat'))
        control['hosts'] = {name: {'token_file': str(config.private/'hosts'/name)} for name, host in config.document['hosts'].items()
                            if 'executor' in host['roles']}
    if 'executor' in config.host['roles']:
        control['executor'] = {'url': config.document['controller']['url'], 'token_file': str(config.private/'host-token'),
                               'ca_file': str(config.private/'ca.crt'), 'runner_uids': [uid]}
    add(config.control/'etc/central.toml', toml_document(control))
    for name in ('job-started.sh', 'job-completed.sh'):
        text = (release/'hooks'/name).read_text()
        add(config.runtime/'ci-runners/hooks'/name, setup.hook_text(text, setup.python_of(inv), str(config.etc.parent), str(config.etc)), 0o755)
    for name, labels, role in config.runner_specs():
        pool = 'plan' if role == 'plan' else 'slurm' if name.startswith('slurm') else 'cpu'
        add(config.runtime/'actions-runner'/name/'.env', setup.env_file(inv, role, str(config.etc), pool=pool))
    if config.host.get('slurm_runners', 0):
        for name, site in config.sites.items():
            add(config.etc/'slurm'/f'{name}.toml', toml_document(site))
    return files
