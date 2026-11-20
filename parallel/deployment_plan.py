"""Read-only host checks and a reviewable, reproducible installation plan."""
from __future__ import annotations

import difflib
import hashlib
import os
import platform
import pwd
import shutil
import subprocess
from pathlib import Path

from ci_runners import __version__, github, setup
from parallel import gpu_resources
from parallel import deployment_config, deployment_credentials, deployment_files as files, protocol, transport

SOURCE = Path(__file__).resolve().parents[1]


def release_path(config):
    return config.runtime/'ci-runners/releases'/('v'+__version__)


def service_names(config):
    names = []
    if config.is_controller:
        names.append('ci-parallel-scheduler.service')
    if 'executor' in config.host['roles']:
        names.append('ci-parallel-executor.service')
    return names


def unit_files(config, source=SOURCE):
    replacements = {'RELEASE': str(release_path(config)), 'ETC': str(config.etc),
                    'PYTHON': config.host.get('python', '/usr/bin/python3'), 'RUNTIME': str(config.runtime),
                    'CONTROL_UNITS': ' '.join(service_names(config))}
    rendered = {}
    for name in [*service_names(config), 'ci-parallel-runner@.service']:
        text = (source/'parallel/units'/name).read_text()
        for key, value in replacements.items():
            text = text.replace('@'+key+'@', value)
        rendered[str(config.unit_dir/name)] = {'content': text, 'mode': 0o644}
    rendered[str(config.unit_dir/'ci.slice')] = {'content': '[Slice]\nMemoryMax='+config.host['memory_max']+'\nMemorySwapMax=0\n', 'mode': 0o644}
    for name in config.projects:
        memory = config.project_document(name)['hosts'][config.host_name]['memory_max']
        rendered[str(config.unit_dir/f'ci-{name}.slice')] = {'content': '[Slice]\nMemoryMax='+memory+'\nMemorySwapMax=0\n', 'mode': 0o644}
    return rendered


def resolve_repository_ids(config, call=github.api):
    identities = {}
    for name, project in config.projects.items():
        if project.get('repository_id'):
            identities[name] = project['repository_id']
            continue
        if not config.is_controller:
            raise ValueError('executor input needs repository IDs from controller export-host')
        token = transport.secret(str(deployment_credentials.pat_path(config.input_dir)))
        metadata = call('GET', 'https://api.github.com/repos/'+project['repository'], token)
        if not protocol.positive(metadata.get('id')) or metadata.get('private') is not True:
            raise ValueError(f'{name}: v2 currently admits private repositories only')
        identities[name] = metadata['id']
    return identities


def secret_fingerprints(config):
    return {name: hashlib.sha256(files.read_private(path)).hexdigest()
            for name, path in deployment_credentials.credential_sources(config).items()}


def input_fingerprint(config):
    inputs = config.public_inputs()
    records = {}
    for name, project in config.projects.items():
        environment = project.get('environment', {})
        if environment.get('mode') == 'kept':
            records[name] = hashlib.sha256(config.kept_record(project).read_bytes()).hexdigest()
    return protocol.fingerprint({'inputs': inputs, 'credentials': secret_fingerprints(config), 'environment_records': records})


def check_gpu_inventory(config, *, run=subprocess.run):
    host = config.inventory().host
    cards = host.get('gpu_uuids', [])
    ready = gpu_resources.available(cards, host['max_temp_c'], run=run)
    return ['configured GPU UUIDs must be present, idle and cool before cutover'] if set(cards) - ready.keys() else []


def check_host(config, *, run=subprocess.run, network=True):
    problems = []
    account = pwd.getpwnam(config.host['account'])
    if account.pw_uid != os.getuid() or Path(account.pw_dir).resolve() != Path(config.host['home']).resolve():
        problems.append('run as the configured account with its actual home directory')
    if platform.system() != 'Linux' or platform.machine() not in ('x86_64', 'AMD64'):
        problems.append('this release installs the Linux x64 runner application')
    for command in ('git', 'curl', 'tar', 'openssl', 'systemctl', 'systemd-run', 'ssh', 'rsync'):
        if not shutil.which(command):
            problems.append('missing command: '+command)
    try:
        setup.check_python(config.host.get('python', '/usr/bin/python3'), run=run)
        secret_fingerprints(config)
    except (OSError, ValueError) as error:
        problems.append(str(error))
    if not setup.linger_on(config.host['account']):
        problems.append('linger is disabled; unattended user services require it to be enabled by the host administrator')
    existing_parent = config.runtime
    while not existing_parent.exists():
        existing_parent = existing_parent.parent
    if shutil.disk_usage(existing_parent).free < config.host.get('disk_min_gib', 20) * (1 << 30):
        problems.append('runtime filesystem has less free space than disk_min_gib')
    if 'executor' in config.host['roles']:
        problems.extend(check_gpu_inventory(config, run=run))
        count = deployment_config.cpu_count(config.host)
        if count > len(os.sched_getaffinity(0)):
            problems.append('configured CPU count exceeds the CPUs available to this account')
        probe = ('import sys;sys.path.insert(0,sys.argv[1]);from parallel.task_execution import resource_limits,check_limits;'
                 'quota,memory=resource_limits();check_limits({"cores":1,"RAM":"64M"},quota,memory)')
        command = ['systemd-run', '--user', '--scope', '--quiet', '-p', 'CPUQuota=100%',
                   '-p', 'MemoryMax=64M', '-p', 'MemorySwapMax=0',
                   config.host.get('python', '/usr/bin/python3'), '-I', '-c', probe, str(SOURCE)]
        try:
            if run(command, capture_output=True, timeout=30).returncode:
                problems.append('effective CPU quota or memory limit is unavailable to the user systemd manager')
        except (OSError, subprocess.SubprocessError):
            problems.append('cannot probe user systemd resource limits')
    try:
        result = run(['systemctl', '--user', 'list-units', '--state=active', '--plain', '--no-legend',
                      'ci-staging-*', 'ci-runner@*', 'ci-executor.service', 'ci-plan.service'], capture_output=True, text=True, timeout=15)
        if result.returncode:
            problems.append('user systemd manager is unavailable')
        elif result.stdout.strip():
            problems.append('legacy/trial CI services are active; drain and retire them before activating this runtime')
    except (OSError, subprocess.SubprocessError):
        problems.append('cannot inspect conflicting CI services')
    for name, project in config.projects.items():
        environment = project.get('environment', {'mode': 'existing'})
        prefix = Path(config.project_document(name)['hosts'][config.host_name]['env'])
        if environment['mode'] == 'existing' and not (prefix/'bin/python').is_file():
            problems.append(f'{name}: existing project environment is missing: {prefix}')
        if environment['mode'] in ('kept', 'build') and not shutil.which(config.host.get('conda', 'conda')):
            problems.append(f'{name}: conda is required to build the environment')
        if environment['mode'] == 'build' and not config.is_controller:
            problems.append(f'{name}: export a kept environment record from the controller')
    if any(not (config.runtime/'actions-runner'/name/'.runner').exists() for name, _, _ in config.runner_specs()):
        path = deployment_credentials.registration_path(config.input_dir)
        try:
            info = path.lstat()
            files.check_private_file(path, info)
            if not info.st_size:
                raise ValueError('empty registration token file')
        except (OSError, ValueError):
            problems.append(f'supply a nonempty private organization runner registration token at {path}')
    if network:
        try:
            if config.is_controller:
                token = transport.secret(str(deployment_credentials.pat_path(config.input_dir)))
                for project in config.projects.values():
                    metadata = github.api('GET', 'https://api.github.com/repos/'+project['repository'], token)
                    if metadata.get('private') is not True or project.get('repository_id', metadata['id']) != metadata['id']:
                        raise ValueError('repository identity or private visibility differs from configuration')
                    github.api('GET', 'https://api.github.com/repos/'+project['repository']+'/actions/runs?per_page=1', token)
            else:
                sources = deployment_credentials.credential_sources(config)
                client = transport.Client(url=config.document['controller']['url'], token_file=str(sources['host-token']),
                                          ca_file=str(sources['ca.crt']))
                if not client.call('health').get('ready'):
                    problems.append('controller is reachable but not ready')
        except Exception as error:
            problems.append('control/GitHub connection check failed: '+type(error).__name__)
    return problems


def build(config, *, source=SOURCE, repository_ids=None):
    identities = resolve_repository_ids(config) if repository_ids is None else repository_ids
    rendered = deployment_config.runtime_files(config, identities, source)
    rendered.update(unit_files(config, source))
    receipt = files.read_json(config.control/'state/deployment.json')
    changes = {}
    for path, specification in rendered.items():
        before = files.file_state(path)
        if before and path not in receipt.get('files', {}) and path not in receipt.get('pending_files', {}):
            raise ValueError(f'generated file belongs to an unrecorded installation: {path}')
        previous = Path(path).read_text() if before else ''
        changes[path] = dict(specification, before=before,
                             diff=''.join(difflib.unified_diff(previous.splitlines(True), specification['content'].splitlines(True),
                                                            fromfile=path, tofile=path)))
    plan = dict(schema=1, version=__version__, host=config.host_name, runtime=str(config.runtime),
                source_digest=files.release_digest(source), input_digest=input_fingerprint(config),
                repository_ids=identities, files=changes,
                runners=[{'name': name, 'labels': labels, 'role': role} for name, labels, role in config.runner_specs()],
                organization=config.document['github']['organization'], runner_group=config.document['github']['runner_group'],
                services=service_names(config))
    plan['digest'] = protocol.fingerprint(plan)
    return plan


def validate(plan, config, *, source=SOURCE):
    payload = {key: value for key, value in plan.items() if key != 'digest'}
    if plan.get('digest') != protocol.fingerprint(payload):
        raise ValueError('deployment plan checksum is invalid')
    if (plan.get('host') != config.host_name or plan.get('runtime') != str(config.runtime)
            or plan.get('input_digest') != input_fingerprint(config) or plan.get('source_digest') != files.release_digest(source)):
        raise ValueError('deployment plan is stale; regenerate it for these inputs and this release')
    desired = deployment_config.runtime_files(config, plan['repository_ids'], source)
    desired.update(unit_files(config, source))
    if set(plan['files']) != set(desired) or any(
            plan['files'][path][key] != specification[key] for path, specification in desired.items() for key in ('content', 'mode')):
        raise ValueError('deployment plan files do not match generated configuration')
    receipt = files.read_json(config.control/'state/deployment.json')
    for path, specification in plan['files'].items():
        current = files.file_state(path)
        if (current != specification['before'] and current != receipt.get('files', {}).get(path)
                and current != receipt.get('pending_files', {}).get(path)):
            raise ValueError(f'planned file changed outside deployment: {path}')
    return receipt
