"""The v2 deployment CLI. Read inputs, review a plan, then apply it locally."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from ci_runners import __version__, github, slurmsetup, slurmsite
from parallel import deployment_config, deployment_credentials, deployment_files as files, deployment_plan, deployment_steps, health


def initialize(input_dir):
    input_dir = Path(input_dir).resolve()
    source = deployment_plan.SOURCE
    if input_dir == source or source in input_dir.parents:
        raise ValueError('ci-deploy-input must be outside the release repository')
    for name in ('', 'projects', 'slurm', 'environments'):
        files.private_directory(input_dir/name)
    deployment_credentials.initialize_layout(input_dir)
    examples = {'host.example.toml': 'host.toml'}
    if not any((input_dir/'projects').iterdir()):
        examples['project.example.toml'] = 'projects/example.toml'
    for example, destination in examples.items():
        path = input_dir/destination
        if not os.path.lexists(path):
            files.atomic_write(path, (source/'parallel/examples'/example).read_bytes())
    return {'input': str(input_dir), 'controller_pat': str(deployment_credentials.pat_path(input_dir).relative_to(input_dir)),
            'runner_registration_token': str(deployment_credentials.registration_path(input_dir).relative_to(input_dir)),
            'next': 'Edit host.toml and projects/*.toml. Fill the PAT on the controller and the token on each host registering runners.'}


def slurm_package(config, site_name, alias, output, *, run=subprocess.run):
    if site_name not in config.sites:
        raise ValueError('unknown Slurm site')
    site = slurmsite.load(deployment_config.toml_document(config.sites[site_name]))
    if alias not in site.accounts:
        raise ValueError('unknown Slurm account')
    output = Path(output).absolute()
    if output.exists() or output.is_symlink() or deployment_plan.SOURCE in output.parents:
        raise ValueError('package output must be a new directory outside the release repository')
    key = config.input_dir/'credentials/slurm'/alias
    files.read_private(key)
    public_key = deployment_steps.run_checked(['ssh-keygen', '-y', '-f', str(key)], run=run).stdout.decode().strip()
    if not public_key.startswith('ssh-ed25519 '):
        raise ValueError('Slurm gate requires an ed25519 CI key')
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.slurm-package-', dir=output.parent))
    try:
        digest = files.release_digest(deployment_plan.SOURCE)
        files.install_release(deployment_plan.SOURCE, temporary/'release', digest)
        files.atomic_write(temporary/'site.toml', deployment_config.toml_document(config.sites[site_name]))
        files.write_json(temporary/'installation.json', {'account': alias, 'source_digest': digest})
        files.atomic_write(temporary/'authorized-key.txt', slurmsetup.key_line(slurmsetup.base_of(site, alias), public_key, config.host_name)+'\n')
        launcher = '#!/bin/sh\nset -eu\nroot=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
        launcher += ('exec '+site.site['login_python']+' -I -c '
                     "'import sys;sys.path.insert(0,sys.argv.pop(1));from parallel.slurm_install import main;sys.exit(main(sys.argv[1:]))' "
                     '"$root/release" --bundle "$root" "$@"\n')
        files.atomic_write(temporary/'install.sh', launcher, 0o700)
        os.rename(temporary, output)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return {'package': str(output), 'check': './install.sh', 'install': './install.sh --apply', 'private_key_included': False}


def argument_parser():
    parser = argparse.ArgumentParser(prog='parallel/deploy.sh', description=__doc__)
    parser.add_argument('--version', action='version', version=__version__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('init', 'check', 'plan', 'apply', 'export-host', 'status', 'slurm-package'):
        command = commands.add_parser(name)
        command.add_argument('--input', type=Path, required=True)
        if name != 'init':
            command.add_argument('--host', required=True, help='this physical host, as named in host.toml')
        if name in ('check', 'status'):
            command.add_argument('--offline', action='store_true', help='skip GitHub and remote control queries')
        if name == 'check':
            command.add_argument('--inventory-only', action='store_true', help='validate host.toml and the local GPU inventory only')
        if name in ('plan', 'export-host', 'slurm-package'):
            command.add_argument('--output', type=Path, required=True)
        if name == 'apply':
            command.add_argument('--plan', type=Path, required=True)
        if name == 'export-host':
            command.add_argument('--target', required=True)
        if name == 'slurm-package':
            command.add_argument('--site', required=True)
            command.add_argument('--account', required=True)
    return parser


def main(argv=None):
    arguments = argument_parser().parse_args(argv)
    previous_mask = os.umask(0o077)
    try:
        if arguments.command == 'init':
            result = initialize(arguments.input)
        else:
            config = deployment_config.load(arguments.input, arguments.host, inventory_only=getattr(arguments, 'inventory_only', False))
            if arguments.command == 'check':
                if arguments.inventory_only:
                    problems = deployment_plan.check_gpu_inventory(config)
                    print(json.dumps({'host': config.host_name, 'configured_gpus': len(config.host.get('gpu_uuids', [])),
                                      'inventory_ready': not problems, 'problems': problems}, indent=2))
                    return int(bool(problems))
                problems = deployment_plan.check_host(config, network=not arguments.offline)
                print(json.dumps({'host': config.host_name, 'ready': not problems, 'problems': problems}, indent=2))
                return int(bool(problems))
            if arguments.command == 'plan':
                plan = deployment_plan.build(config)
                files.write_json(arguments.output, plan)
                result = {'plan': str(arguments.output), 'runners': len(plan['runners']),
                          'services': plan['services'], 'digest': plan['digest']}
            elif arguments.command == 'apply':
                result = deployment_steps.apply(config, files.read_json(arguments.plan))
                result = {key: result[key] for key in ('version', 'host', 'status', 'source_digest')}
            elif arguments.command == 'export-host':
                result = deployment_steps.export_host(config, arguments.target, arguments.output)
            elif arguments.command == 'slurm-package':
                result = slurm_package(config, arguments.site, arguments.account, arguments.output)
            elif arguments.command == 'status':
                result = health.status(config, query_github=not arguments.offline)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (OSError, ValueError, github.ApiError, subprocess.SubprocessError) as error:
        print(f'deploy: {error}', file=sys.stderr)
        return 2
    finally:
        os.umask(previous_mask)


if __name__ == '__main__':
    sys.exit(main())
