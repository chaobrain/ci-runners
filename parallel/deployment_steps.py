"""Apply a reviewed deployment with durable per-step receipts."""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path

from ci_runners import __version__, setup
from parallel import deployment_config, deployment_credentials, deployment_files as files, deployment_plan, transport


def run_checked(command, *, run=subprocess.run, **options):
    result = run(command, capture_output=True, timeout=options.pop('timeout', 120), **options)
    if result.returncode:
        raise ValueError(f'{Path(command[0]).name} failed (exit {result.returncode}); no credentials were logged')
    return result


def ensure_idle(config):
    for name, query in [('executor.sqlite', "SELECT COUNT(*) FROM executions WHERE state != 'released'"),
                        ('executor.sqlite', 'SELECT COUNT(*) FROM reports WHERE delivered=0'),
                        ('scheduler.sqlite', 'SELECT COUNT(*) FROM allocations WHERE released=0 OR runner_done=0'),
                        ('scheduler.sqlite', "SELECT COUNT(*) FROM batches WHERE state='pending'")]:
        path = config.control/'state'/name
        if path.exists():
            database = sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True)
            try:
                if database.execute(query).fetchone()[0]:
                    raise ValueError('deployment requires drained allocations; existing work remains running')
            finally:
                database.close()


def save_receipt(config, receipt):
    receipt['updated_at'] = time.time()
    files.write_json(config.control/'state/deployment.json', receipt)


def registration_identity(config, name):
    return {'name': name, 'url': 'https://github.com/'+config.document['github']['organization'],
            'group': config.document['github']['runner_group']}


def verify_runner_identity(path, expected):
    files.file_state(path)
    identity = json.loads(path.read_text())
    if (identity.get('agentName') != expected['name']
            or identity.get('gitHubUrl', '').rstrip('/').lower() != expected['url'].lower()
            or identity.get('poolName', expected['group']) != expected['group']):
        raise ValueError(f'{expected["name"]}: registered runner identity does not match the reviewed plan')


def register_runner(config, name, labels, receipt, *, run=subprocess.run):
    directory = config.runtime/'actions-runner'/name
    expected = registration_identity(config, name)
    recorded = receipt.setdefault('runners', {}).get(name)
    if recorded and recorded['identity'] != expected:
        raise ValueError(f'{name}: recorded runner belongs to a different organization or group')
    identity_path = directory/'.runner'
    if os.path.lexists(identity_path):
        if not recorded:
            raise ValueError(f'{name}: existing runner has no deployment receipt')
        verify_runner_identity(identity_path, expected)
        recorded['registered'] = True
        save_receipt(config, receipt)
        return
    receipt['runners'][name] = {'identity': expected, 'registered': False}
    save_receipt(config, receipt)
    token_path = deployment_credentials.registration_path(config.input_dir)
    try:
        token = transport.secret(str(token_path))
    except (OSError, ValueError) as error:
        raise ValueError(f'{name}: supply a private organization runner registration token at {token_path}') from error
    environment = {'HOME': config.host['home'], 'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'ACTIONS_RUNNER_INPUT_TOKEN': token}
    command = [str(directory/'config.sh'), '--unattended', '--url', expected['url'], '--name', name,
               '--runnergroup', expected['group'], '--labels', ','.join(labels), '--work', str(config.runtime/'ci-work'/name)]
    result = run(command, cwd=directory, env=environment, capture_output=True, timeout=180)
    if result.returncode or not identity_path.is_file():
        raise ValueError(f'{name}: registration failed; check organization/group and refresh {token_path}')
    verify_runner_identity(identity_path, expected)
    receipt['runners'][name]['registered'] = True
    save_receipt(config, receipt)


def prepare_environment(config, name, receipt, *, run=subprocess.run):
    project = config.projects[name]
    environment = project.get('environment', {'mode': 'existing'})
    prefix = Path(config.project_document(name)['hosts'][config.host_name]['env'])
    if environment['mode'] == 'existing':
        if not (prefix/'bin/python').is_file():
            raise ValueError(f'{name}: existing environment is missing')
        return
    expected_prefix = config.runtime/'ci-runners/envs'/name
    if prefix != expected_prefix:
        raise ValueError(f'{name}: built environments must use the generated runtime prefix')
    if receipt.setdefault('environments', {}).get(name) and (prefix/'bin/python').is_file():
        return
    command = [str(deployment_plan.release_path(config)/'bin/ci-run'), 'env', 'build', name,
               '--conda', config.host.get('conda', 'conda'), '--memory', config.host['memory_max']]
    environment_variables = {'HOME': config.host['home'], 'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
                             'CI_RUNNERS_ETC': str(config.etc), 'CI_RUNNERS_PYTHON': config.host.get('python', '/usr/bin/python3'),
                             'XDG_RUNTIME_DIR': os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')}
    if environment['mode'] == 'kept':
        command.extend(['--kept', str(config.kept_record(project))])
    elif config.is_controller:
        environment_variables['GH_TOKEN'] = transport.secret(str(config.private/'github-pat'))
    else:
        raise ValueError('executor environments require an exported kept record')
    run_checked(command, run=run, env=environment_variables, timeout=3600)
    if not (prefix/'bin/python').is_file():
        raise ValueError(f'{name}: environment build did not produce bin/python')
    receipt['environments'][name] = True
    save_receipt(config, receipt)


def wait_ready(config, *, timeout=90, sleep=time.sleep):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        ready = True
        if config.is_controller:
            try:
                ready = transport.Client(path=str(config.control/'run/plan.sock')).call('health')['ready']
            except (OSError, ValueError):
                ready = False
        if 'executor' in config.host['roles']:
            report = files.read_json(config.control/'state/executor-health.json')
            ready = ready and report.get('connected', False) and time.time()-report.get('observed_at', 0) <= 30
            try:
                transport.Client(path=str(config.control/'run/executor.sock')).call('health')
            except (OSError, ValueError):
                ready = False
        if ready:
            return
        sleep(1)
    raise ValueError('services did not become control-ready; inspect deploy status and the user journal')


def apply(config, plan, *, source=deployment_plan.SOURCE, run=subprocess.run, preflight=deployment_plan.check_host, readiness=wait_ready):
    receipt = deployment_plan.validate(plan, config, source=source)
    problems = preflight(config, run=run)
    if problems:
        raise ValueError('preflight failed: '+'; '.join(problems))
    files.private_directory(config.control/'run')
    descriptor = os.open(config.control/'run/deploy.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        receipt = deployment_plan.validate(plan, config, source=source)
        ensure_idle(config)
        if receipt.get('input_digest') != plan['input_digest'] or receipt.get('version') != __version__:
            units = run(['systemctl', '--user', 'list-units', '--state=active,activating,deactivating', '--plain',
                         '--no-legend', 'ci-parallel-*.service', 'ci-part-*.scope', 'actions.runner.*.service'],
                        capture_output=True, text=True, timeout=15)
            if units.returncode or units.stdout.strip():
                raise ValueError('stop old control services and runners and drain task scopes before cutover')
        receipt.update(schema=1, version=__version__, host=config.host_name, runtime=str(config.runtime),
                       plan_digest=plan['digest'], input_digest=plan['input_digest'], source_digest=plan['source_digest'],
                       repository_ids=plan['repository_ids'], status='applying')
        receipt.setdefault('files', {})
        save_receipt(config, receipt)
        files.install_release(source, deployment_plan.release_path(config), plan['source_digest'])
        deployment_credentials.install(config, run)
        changed_units = set(receipt.get('pending_units', []))
        for path, specification in plan['files'].items():
            desired = {'sha256': hashlib.sha256(specification['content'].encode()).hexdigest(), 'mode': specification['mode']}
            if files.file_state(path) != desired:
                receipt.setdefault('pending_files', {})[path] = desired
                if Path(path).parent == config.unit_dir:
                    changed_units.add(Path(path).name)
                receipt['pending_units'] = sorted(changed_units)
                save_receipt(config, receipt)
                files.atomic_write(path, specification['content'], specification['mode'])
            if receipt['files'].get(path) != desired or path in receipt.get('pending_files', {}):
                receipt['files'][path] = files.file_state(path)
                receipt.setdefault('pending_files', {}).pop(path, None)
                save_receipt(config, receipt)
        files.activate_release(deployment_plan.release_path(config))
        for name in config.projects:
            prepare_environment(config, name, receipt, run=run)
        for name, labels, _ in config.runner_specs():
            directory = config.runtime/'actions-runner'/name
            files.private_directory(directory)
            files.private_directory(config.runtime/'ci-work'/name)
            if not setup.runner_complete(str(directory)):
                setup._unpack(str(directory), str(config.runtime/'ci-runners/cache/runner.tar.gz'), 0)
            register_runner(config, name, labels, receipt, run=run)
        run_checked(['systemctl', '--user', 'daemon-reload'], run=run)
        for unit in deployment_plan.service_names(config):
            if unit in changed_units:
                run_checked(['systemctl', '--user', 'restart', unit], run=run)
            run_checked(['systemctl', '--user', 'enable', '--now', unit], run=run)
        readiness(config)
        for name, _, _ in config.runner_specs():
            run_checked(['systemctl', '--user', 'enable', '--now', f'ci-parallel-runner@{name}.service'], run=run)
        receipt['status'] = 'installed'
        receipt['pending_units'] = []
        receipt.pop('last_error', None)
        save_receipt(config, receipt)
        return receipt
    except Exception as error:
        if receipt.get('plan_digest') == plan['digest']:
            receipt.update(status='incomplete', last_error=type(error).__name__)
            save_receipt(config, receipt)
        raise
    finally:
        os.close(descriptor)


def export_host(config, host_name, output):
    if config.repository_members:
        raise ValueError('schema 2 executors use their own host.toml and device credentials')
    if not config.is_controller or host_name == config.controller_name or host_name not in config.document['hosts']:
        raise ValueError('export-host must run on the controller and name an executor-only host')
    receipt = files.read_json(config.control/'state/deployment.json')
    if receipt.get('status') != 'installed' or receipt.get('input_digest') != deployment_plan.input_fingerprint(config):
        raise ValueError('apply the current controller configuration before exporting node access')
    output = Path(output).absolute()
    if os.path.lexists(output) or deployment_plan.SOURCE in output.resolve().parents:
        raise ValueError('export destination must be a new directory outside the release repository')
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.host-export-', dir=output.parent))
    try:
        deployment_credentials.initialize_layout(temporary)
        files.atomic_write(temporary/'host.toml', deployment_config.toml_document(config.document))
        for name, project in config.projects.items():
            exported = copy.deepcopy(project)
            exported['repository_id'] = receipt['repository_ids'][name]
            environment = exported.get('environment', {})
            if environment.get('mode') in ('build', 'kept'):
                record = (config.runtime/'ci-runners/envs'/name/'ci-env.json'
                          if environment['mode'] == 'build' else config.kept_record(project))
                exported['environment'] = {'mode': 'kept', 'record': f'environments/{name}.json'}
                files.atomic_write(temporary/exported['environment']['record'], record.read_bytes())
            files.atomic_write(temporary/'projects'/f'{name}.toml', deployment_config.toml_document(exported))
        for name, site in config.sites.items():
            files.atomic_write(temporary/'slurm'/f'{name}.toml', deployment_config.toml_document(site))
        deployment_credentials.copy_private(config.private/'hosts'/host_name, temporary/deployment_credentials.HOST_TOKENS/host_name)
        deployment_credentials.copy_private(config.private/'ca.crt', temporary/deployment_credentials.TLS_DIR/'ca.crt')
        os.rename(temporary, output)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return {'host': host_name, 'output': str(output), 'registration_token_required': True,
            'runner_registration_token': str(deployment_credentials.REGISTRATION_FILE)}
