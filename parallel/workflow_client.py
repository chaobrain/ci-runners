"""The schema 2 workflow bridge. Jobs never receive scheduler write credentials."""
from __future__ import annotations

import argparse
import json
import functools
import os
import shutil
import signal
import sys
import time
import threading
from pathlib import Path
from parallel import layout

from ci_runners import client, flow, part, registry
from parallel import executor, organization, protocol, transport


def output(values, env):
    output_path = env.get('GITHUB_OUTPUT')
    if not output_path:
        return
    lines = []
    for name, value in values.items():
        text = str(value)
        if not name.isidentifier() or '\n' in text or '\r' in text:
            raise protocol.Error('workflow outputs must be named single-line values')
        lines.append(f'{name}={text}\n')
    with open(output_path, 'a', encoding='utf-8') as output_file:
        output_file.writelines(lines)


def state_file(env, name):
    return Path(env['RUNNER_TEMP']) / ('ci-central-'+name+'.json')


def source(args, env, *, organization_mode=False):
    event = json.loads(Path(env['GITHUB_EVENT_PATH']).read_text()) if env.get('GITHUB_EVENT_PATH') else {}
    sha = args.sha or event.get('pull_request', {}).get('head', {}).get('sha') or env.get('GITHUB_SHA', '')
    context = args.context + ('/plan-only' if args.plan_only else '')
    project = 'organization' if organization_mode else args.project or env['GITHUB_REPOSITORY'].split('/')[1]
    data = dict(repository=env['GITHUB_REPOSITORY'], sha=sha, run_id=int(env['GITHUB_RUN_ID']),
                run_attempt=int(env['GITHUB_RUN_ATTEMPT']), project=project,
                context=context, concurrency_key='verified-by-service', cancel_in_progress=args.cancel_in_progress)
    protocol.source_key(data)
    return data


def build(args, inv, etc, env):
    if not args.plan_command.strip():
        raise protocol.Error('schema 2 requires a plan command with explicit workers')
    src = json.loads(state_file(env, 'source').read_text())
    settings = transport.settings(etc) if layout.settings_path(etc).is_file() else {}
    if src.get('repository_environment') is True and 'env_root' not in settings.get('organization', {}):
        raise protocol.Error('repository environment mode changed on the plan runner')
    organization_project = organization.project(settings, inv, src) if 'organization' in settings else None
    cache_mode = 'env_cache' in settings.get('organization', {})
    if cache_mode != ('environment_digest' in src):
        raise protocol.Error('environment cache mode changed on the plan runner')
    if organization_project is not None and not cache_mode and not organization.environment_ready(settings, src['repository']):
        raise protocol.Error(f'organization environment for {src["repository"]} is not ready on the plan runner')
    dest = state_file(env, 'plan')
    command_env = {**env, 'CIRUNNERS_PLAN': args.plan_command, 'CIRUNNERS_PLAN_OUTPUT': str(dest),
                   'CI_RUNNER_CLASS': 'plan'}
    for key in ('GH_TOKEN', 'GITHUB_TOKEN'):
        command_env.pop(key, None)
    engine = part.Executor(inv, etc, command_env, release=str(Path(__file__).resolve().parents[1]))
    options = {}
    if cache_mode:
        from parallel import job_environment
        control = transport.Client(path=env.get('CI_CONTROL_SOCKET') or str(layout.control_root(inv)/'run/plan.sock'))
        recipe = control.call('plan_environment', source=src, digest=src['environment_digest'])
        if protocol.fingerprint(recipe) != src['environment_digest']:
            raise protocol.Error('plan environment does not match its approved digest')
        options.update(organization_project=organization_project,
                       environment_request=job_environment.request(settings['organization'], inv,
                                                                   {**src, 'environment_recipe': recipe}))
    elif organization_project is not None:
        options.update(organization_project=organization_project,
                       validate_env_build=functools.partial(organization.build_ready, settings, src['repository']))
    args_for_plan = part.Inputs(project=src['project'], id='plan-v2', timeout_minutes=30,
                               memory='4G', queue_timeout_minutes=1,
                               command='exec /bin/bash -e -o pipefail -c "$CIRUNNERS_PLAN" > "$CIRUNNERS_PLAN_OUTPUT"')
    _, outcome = engine.execute(args_for_plan, **options)
    if outcome['outcome'] != 'pass':
        if organization_project is not None and outcome['outcome'] == 'unavailable' and outcome.get('reason'):
            raise protocol.Error(outcome['reason'])
        raise protocol.Error('plan command failed')
    if dest.stat().st_size > transport.LIMIT:
        raise protocol.Error('plan JSON exceeds the submission limit')
    proj = organization_project or registry.load_file(etc, src['project'], inv.this)
    doc = protocol.load_plan(json.loads(dest.read_text()), max_minutes=proj.max_minutes)
    if doc['files']:
        why = flow.check_files(env['GITHUB_WORKSPACE'], doc['files'])
        if why:
            raise protocol.Error(why)
    part._write_json(str(dest), doc)
    output({'files': doc['files'], 'parts': len(doc['parts'])}, env)
    return 0


def job_env(env):
    return {k: env[k] for k in ('RUNNER_NAME', 'GITHUB_REPOSITORY', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT',
                                'GITHUB_WORKSPACE', 'RUNNER_TEMP') if k in env}


def source_identity(env):
    return {'repository': env['GITHUB_REPOSITORY'], 'run_id': int(env['GITHUB_RUN_ID']),
            'run_attempt': int(env['GITHUB_RUN_ATTEMPT'])}


def task_request(args, env):
    return {'batch_id': args.batch_id, 'task_id': args.task_id, 'task_attempt': args.task_attempt,
            'source_identity': source_identity(env)}


def wait_for_task_allocation(args, connection, env):
    request = task_request(args, env)
    task = connection.call('request_allocation', **request)
    timeout_at = time.monotonic() + task['part']['queue_minutes'] * 60 + 120
    while True:
        allocation = task['allocation']
        if allocation is not None:
            # Even a cancelled grant must create its job; admission refuses execution,
            # and job reconciliation then confirms that its runner has finished.
            output({'runner_tag': allocation['runner'], 'allocation_json': protocol.dumps(allocation['grant'])}, env)
            return 0
        if task['state'] in protocol.TERMINAL:
            output({'runner_tag': '', 'allocation_json': ''}, env)
            return 0
        if time.monotonic() >= timeout_at:
            raise protocol.Error('allocation request exceeded its wait bound')
        time.sleep(2)
        task = connection.call('task', **request)


def wait_for_terminal_result(connection, operation, request, *, timeout_seconds=600):
    timeout_at = time.monotonic() + timeout_seconds
    while True:
        record = connection.call(operation, **request)
        if record['state'] in protocol.TERMINAL:
            return record
        if time.monotonic() >= timeout_at:
            raise protocol.Error('completion evidence is still pending; resources remain reserved')
        time.sleep(2)


def report_final_result(args, connection, env):
    if args.operation == 'task-result':
        record = wait_for_terminal_result(connection, 'task', task_request(args, env))
    elif args.batch_id:
        record = wait_for_terminal_result(connection, 'batch', {
            'batch_id': args.batch_id, 'source_identity': source_identity(env)})
    elif args.submission_state == 'validated':
        record = {'state': 'success'}
    else:
        record = {'state': 'error', 'result': {'reason': 'plan construction or submission failed'}}
    output({'state': record['state']}, env)
    if env.get('GITHUB_STEP_SUMMARY'):
        with open(env['GITHUB_STEP_SUMMARY'], 'a') as summary:
            summary.write(f'CI tests: **{record["state"]}**.\n\n')
            for task in record.get('tasks', []):
                summary.write(f'- `{task["part"]["id"]}`: {task["state"]}\n')
    return 0 if record['state'] == 'success' else 1


def inspect(args, inv, env):
    grant = protocol.validate_allocation(json.loads(args.allocation_json))
    connection = transport.Client(path=str(executor.paths(inv)[1]))
    allocation = connection.call('inspect', grant=grant, env=job_env(env))
    part._write_json(str(state_file(env, 'allocation')), allocation)
    output({'sha': allocation['source']['sha'], 'project': allocation['source']['project'],
            'part_id': allocation['part']['id'], 'fetch_depth': '0' if allocation['part'].get('history') else '1',
            'allocation_id': grant['allocation_id']}, env)
    return 0


def fetch(env):
    a = json.loads(state_file(env, 'allocation').read_text())
    if not flow.check_checkout(env['GITHUB_WORKSPACE'], env):
        raise protocol.Error('checkout retained credentials')
    import subprocess
    actual = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false', 'rev-parse', 'HEAD'],
                            cwd=env['GITHUB_WORKSPACE'], capture_output=True, text=True, check=True).stdout.strip()
    if actual != a['source']['sha']:
        raise protocol.Error('checkout is not the authorized test SHA')
    aid = a['source'].get('files_artifact_id')
    if aid:
        with client.Client() as http:
            data = http.artifact(f'https://api.github.com/repos/{a["source"]["repository"]}/actions/artifacts/{aid}/zip',
                                 env.get('GITHUB_TOKEN', ''), flow.MAX_FILES_ZIP)
        dest = Path(env['RUNNER_TEMP']) / 'ci-plan-files'
        if dest.exists():
            if dest.is_symlink() or not dest.is_dir():
                raise protocol.Error('plan files destination is not a directory')
            shutil.rmtree(dest)
        flow.unpack_files(data, str(dest))
    return 0


def copy_tree(source: Path, destination: Path):
    """Artifact copies include regular files only and cannot follow links outside their root."""
    root = source.resolve()
    if source.is_symlink():
        return
    if source.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        return
    if not source.is_dir():
        return
    for path in source.rglob('*'):
        if path.is_symlink() or not path.is_file() or root not in path.resolve().parents:
            continue
        target = destination / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)


def collect_allocation_results(inv, allocation, env, result):
    allocation_id = allocation['id']
    part_id = allocation['part']['id']
    destination = Path(env['RUNNER_TEMP']) / 'ci-allocation' / allocation_id
    destination.mkdir(parents=True, exist_ok=True)
    record = executor.record_path(inv, {
        'env': env, 'part': allocation['part'], 'task_attempt': allocation['task_attempt']})
    for name in ('outcome.json', 'summary.md'):
        copy_tree(Path(record)/name, destination/name)
    copy_tree(Path(record)/'work'/'results', destination/'results')
    for name, relative in allocation['part'].get('artifacts', {}).items():
        origin = Path(env['RUNNER_TEMP']) / relative
        if Path(env['RUNNER_TEMP']).resolve() not in origin.resolve().parents:
            raise protocol.Error('artifact path escapes RUNNER_TEMP')
        copy_tree(origin, destination/'artifacts'/name)
    part._write_json(str(destination/'allocation-result.json'), {
        'allocation': allocation['grant'], 'source_repository': allocation['source']['repository'],
        'source_sha': allocation['source']['sha'], 'source_run_id': allocation['source']['run_id'],
        'source_run_attempt': allocation['source']['run_attempt'], 'execution_job_id': allocation['job_id'],
        'task_attempt': allocation['task_attempt'], 'result': result})
    output({'allocation_id': allocation_id, 'part_id': part_id, 'state': result['state']}, env)


def request_execution_stop(stop_event, _signal, _frame):
    stop_event.set()


def execute(inv, env):
    a = json.loads(state_file(env, 'allocation').read_text())
    connection = transport.Client(path=str(executor.paths(inv)[1]))
    who = job_env(env)
    stop_requested = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, functools.partial(request_execution_stop, stop_requested))
    connection.call('start', grant=a['grant'], env=who)
    offset = 0
    cancelled = False
    while True:
        if stop_requested.is_set() and not cancelled:
            connection.call('cancel', allocation_id=a['id'], env=who)
            cancelled = True
        row = connection.call('poll', allocation_id=a['id'], env=who, offset=offset)
        print(row['log'], end='', flush=True)
        previous, offset = offset, row['offset']
        if row['state'] == 'released' and previous == offset:
            collect_allocation_results(inv, a, env, row['result'])
            return 0 if row['result']['state'] == 'success' else 1
        time.sleep(1)


def main(argv, inv, etc, env=None):
    env = dict(os.environ if env is None else env)
    parser = argparse.ArgumentParser(prog='ci-run central')
    parser.add_argument('operation', choices=('prepare', 'build', 'submit', 'failure', 'inspect', 'fetch', 'execute', 'allocate', 'task-result', 'batch-result', 'capacity', 'batch'))
    parser.add_argument('--project', default='')
    parser.add_argument('--context', default='')
    parser.add_argument('--sha', default='')
    parser.add_argument('--plan-command', default='')
    parser.add_argument('--plan-only', action='store_true')
    parser.add_argument('--cancel-in-progress', action='store_true')
    parser.add_argument('--files-artifact-id', default='')
    parser.add_argument('--allocation-json', default='')
    parser.add_argument('--batch-id', default='')
    parser.add_argument('--task-id', default='')
    parser.add_argument('--task-attempt', type=int, default=1)
    parser.add_argument('--submission-state', default='')
    args = parser.parse_args(argv)
    try:
        connection = transport.Client(path=env.get('CI_CONTROL_SOCKET') or str(layout.control_root(inv)/'run/plan.sock'))
        if args.operation == 'prepare':
            settings = transport.settings(etc) if layout.settings_path(etc).is_file() else {}
            organization_mode = 'organization' in settings
            src = source(args, env, organization_mode=organization_mode)
            approved = connection.call('prepare', source=src)
            repository_environment = 'env_root' in settings.get('organization', {})
            if approved.get('repository_environment') is True and not repository_environment:
                raise protocol.Error('the plan runner needs env_root for this controller')
            if ('env_cache' in settings.get('organization', {})) != ('environment_digest' in approved):
                raise protocol.Error('the plan runner and controller need the same environment cache mode')
            if organization_mode:
                if 'environment_digest' in approved:
                    src['environment_digest'] = approved['environment_digest']
                binding = {field: approved[field] for field in
                           ('sha', 'project', 'repository_id', 'organization_profile')}
                src = {**src, **binding}
                if repository_environment:
                    src['repository_environment'] = True
                approved = {field: approved[field] for field in ('sha', 'project')}
            part._write_json(str(state_file(env, 'source')), src)
            output(approved, env)
        elif args.operation == 'build':
            return build(args, inv, etc, env)
        elif args.operation in ('submit', 'failure'):
            src = json.loads(state_file(env, 'source').read_text())
            if args.operation == 'failure':
                connection.call('failure', source=src)
            else:
                src['files_artifact_id'] = int(args.files_artifact_id) if args.files_artifact_id else None
                doc = json.loads(state_file(env, 'plan').read_text())
                result = connection.call('submit', source=src, plan=doc, plan_only=args.plan_only)
                output(result, env)
                if env.get('GITHUB_STEP_SUMMARY'):
                    with open(env['GITHUB_STEP_SUMMARY'], 'a') as f:
                        f.write(f'test plan {result["submission_state"]}; batch `{result["batch_id"]}`. '
                                f'Tests and the final verdict continue in this workflow run.\n')
        elif args.operation == 'allocate':
            return wait_for_task_allocation(args, connection, env)
        elif args.operation in ('task-result', 'batch-result'):
            return report_final_result(args, connection, env)
        elif args.operation == 'inspect':
            return inspect(args, inv, env)
        elif args.operation == 'fetch':
            return fetch(env)
        elif args.operation == 'execute':
            return execute(inv, env)
        elif args.operation == 'capacity':
            print(json.dumps(connection.call('capacity'), indent=2))
        elif args.operation == 'batch':
            print(json.dumps(connection.call('batch', batch_id=args.batch_id), indent=2))
        return 0
    except (OSError, ValueError, KeyError, part.Refused, registry.RegistryError) as e:
        print(f'ci-run central: {e}', file=sys.stderr)
        return 2
