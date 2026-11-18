"""The schema 2 workflow bridge. Jobs never receive scheduler write credentials."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sys
import time
from pathlib import Path

from ci_runners import central, client, control, executor_service, flow, part, registry


def output(values, env):
    part.write_outputs({k: str(v) for k, v in values.items()}, env.get('GITHUB_OUTPUT', ''))


def state_file(env, name):
    return Path(env['RUNNER_TEMP']) / ('ci-central-'+name+'.json')


def source(args, env):
    event = json.loads(Path(env['GITHUB_EVENT_PATH']).read_text()) if env.get('GITHUB_EVENT_PATH') else {}
    sha = args.sha or event.get('pull_request', {}).get('head', {}).get('sha') or env.get('GITHUB_SHA', '')
    context = args.context + ('/plan-only' if args.plan_only else '')
    data = dict(repository=env['GITHUB_REPOSITORY'], sha=sha, run_id=int(env['GITHUB_RUN_ID']),
                run_attempt=int(env['GITHUB_RUN_ATTEMPT']), project=args.project or env['GITHUB_REPOSITORY'].split('/')[1],
                context=context, concurrency_key='verified-by-service', cancel_in_progress=args.cancel_in_progress)
    central.source_key(data)
    return data


def build(args, inv, etc, env):
    if not args.plan_command.strip():
        raise central.Error('schema 2 requires a plan command with explicit workers')
    src = json.loads(state_file(env, 'source').read_text())
    dest = state_file(env, 'plan')
    command_env = {**env, 'CIRUNNERS_PLAN': args.plan_command, 'CIRUNNERS_PLAN_OUTPUT': str(dest),
                   'CI_RUNNER_CLASS': 'plan'}
    for key in ('GH_TOKEN', 'GITHUB_TOKEN'):
        command_env.pop(key, None)
    engine = part.Executor(inv, etc, command_env, release=str(Path(__file__).resolve().parents[1]))
    _, outcome = engine.execute(part.Inputs(project=src['project'], id='plan-v2', timeout_minutes=30,
                                            memory='4G', queue_timeout_minutes=1,
                                            command='exec /bin/bash -e -o pipefail -c "$CIRUNNERS_PLAN" > "$CIRUNNERS_PLAN_OUTPUT"'))
    if outcome['outcome'] != 'pass':
        raise central.Error('plan command failed')
    if dest.stat().st_size > control.LIMIT:
        raise central.Error('plan JSON exceeds the submission limit')
    proj = registry.load_file(etc, src['project'], inv.this)
    doc = central.load_plan(json.loads(dest.read_text()), max_minutes=proj.max_minutes)
    if doc['files']:
        why = flow.check_files(env['GITHUB_WORKSPACE'], doc['files'])
        if why:
            raise central.Error(why)
    part._write_json(str(dest), doc)
    output({'files': doc['files'], 'parts': len(doc['parts'])}, env)
    return 0


def job_env(env):
    return {k: env[k] for k in ('RUNNER_NAME', 'GITHUB_REPOSITORY', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT',
                                'GITHUB_WORKSPACE', 'RUNNER_TEMP') if k in env}


def inspect(args, inv, env):
    grant = central.validate_allocation(json.loads(args.allocation_json))
    connection = control.Client(path=str(executor_service.paths(inv)[1]))
    allocation = connection.call('inspect', grant=grant, env=job_env(env))
    part._write_json(str(state_file(env, 'allocation')), allocation)
    output({'sha': allocation['source']['sha'], 'project': allocation['source']['project'],
            'part_id': allocation['part']['id'], 'fetch_depth': '0' if allocation['part'].get('history') else '1',
            'allocation_id': grant['allocation_id']}, env)
    return 0


def fetch(env):
    a = json.loads(state_file(env, 'allocation').read_text())
    if not flow.check_checkout(env['GITHUB_WORKSPACE'], env):
        raise central.Error('checkout retained credentials')
    import subprocess
    actual = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false', 'rev-parse', 'HEAD'],
                            cwd=env['GITHUB_WORKSPACE'], capture_output=True, text=True, check=True).stdout.strip()
    if actual != a['source']['sha']:
        raise central.Error('checkout is not the authorized test SHA')
    aid = a['source'].get('files_artifact_id')
    if aid:
        with client.Client() as http:
            data = http.artifact(f'https://api.github.com/repos/{a["source"]["repository"]}/actions/artifacts/{aid}/zip',
                                 env.get('GITHUB_TOKEN', ''), flow.MAX_FILES_ZIP)
        dest = Path(env['RUNNER_TEMP']) / 'ci-plan-files'
        if dest.exists():
            if dest.is_symlink() or not dest.is_dir():
                raise central.Error('plan files destination is not a directory')
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


def collect(inv, a, env, result):
    aid, pid = a['id'], a['part']['id']
    destination = Path(env['RUNNER_TEMP']) / 'ci-allocation' / aid
    destination.mkdir(parents=True, exist_ok=True)
    record = executor_service.record_path(inv, {'env': env, 'part': a['part']})
    for name in ('outcome.json', 'summary.md'):
        copy_tree(Path(record)/name, destination/name)
    copy_tree(Path(record)/'work'/'results', destination/'results')
    for name, relative in a['part'].get('artifacts', {}).items():
        origin = Path(env['RUNNER_TEMP']) / relative
        if Path(env['RUNNER_TEMP']).resolve() not in origin.resolve().parents:
            raise central.Error('artifact path escapes RUNNER_TEMP')
        copy_tree(origin, destination/'artifacts'/name)
    part._write_json(str(destination/'allocation-result.json'), {
        'allocation': a['grant'], 'source_repository': a['source']['repository'], 'source_sha': a['source']['sha'],
        'source_run_id': a['source']['run_id'], 'execution_run_id': int(env['GITHUB_RUN_ID']), 'result': result})
    output({'allocation_id': aid, 'part_id': pid, 'state': result['state']}, env)


def execute(inv, env):
    a = json.loads(state_file(env, 'allocation').read_text())
    connection = control.Client(path=str(executor_service.paths(inv)[1]))
    who = job_env(env)
    stop = False
    def cancel(*_args):
        nonlocal stop
        stop = True
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, cancel)
    connection.call('start', grant=a['grant'], env=who)
    offset = 0
    cancelled = False
    while True:
        if stop and not cancelled:
            connection.call('cancel', allocation_id=a['id'], env=who)
            cancelled = True
        row = connection.call('poll', allocation_id=a['id'], env=who, offset=offset)
        print(row['log'], end='', flush=True)
        previous, offset = offset, row['offset']
        if row['state'] == 'released' and previous == offset:
            collect(inv, a, env, row['result'])
            return 0 if row['result']['state'] == 'success' else 1
        time.sleep(1)


def main(argv, inv, etc, env=None):
    env = dict(os.environ if env is None else env)
    parser = argparse.ArgumentParser(prog='ci-run central')
    parser.add_argument('operation', choices=('prepare', 'build', 'submit', 'failure', 'inspect', 'fetch', 'execute', 'capacity', 'batch'))
    parser.add_argument('--project', default='')
    parser.add_argument('--context', default='')
    parser.add_argument('--sha', default='')
    parser.add_argument('--plan-command', default='')
    parser.add_argument('--plan-only', action='store_true')
    parser.add_argument('--cancel-in-progress', action='store_true')
    parser.add_argument('--files-artifact-id', default='')
    parser.add_argument('--allocation-json', default='')
    parser.add_argument('--batch-id', default='')
    args = parser.parse_args(argv)
    try:
        connection = control.Client(path=env.get('CI_CONTROL_SOCKET') or str(Path(inv.host['data'])/'ci-runners/state/plan.sock'))
        if args.operation == 'prepare':
            src = source(args, env)
            approved = connection.call('prepare', source=src)
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
                        f.write(f'CPU plan {result["submission_state"]}; batch `{result["batch_id"]}`. '
                                f'Final test result: commit status `{src["context"]}`.\n')
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
