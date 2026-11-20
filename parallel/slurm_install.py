"""Maintenance-login installer used by the generated Slurm bundle."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ci_runners import __version__, slurmlogin, slurmsetup, slurmsite
from parallel import deployment_files as files


def install(bundle, *, apply=False):
    bundle = Path(bundle)
    manifest = files.read_json(bundle/'installation.json')
    site = slurmsite.load((bundle/'site.toml').read_text())
    alias = manifest['account']
    base = Path(slurmsetup.base_of(site, alias))
    entry = slurmsite.account_entry(site, alias)
    facts = slurmlogin.facts(str(base.parent), entry)
    problems = slurmsetup.findings(site, alias, facts)
    if problems:
        raise ValueError('; '.join(problems))
    if slurmlogin.ci_jobs(site.accounts[alias].a['slurm_account'])['jobs']:
        raise ValueError('cluster CI jobs must be drained before installation')
    if not apply:
        return {'ready': True, 'destination': str(base), 'version': __version__}
    slurmlogin.prepare(str(base))
    destination = base/'releases'/('v'+__version__)
    files.install_release(bundle/'release', destination, manifest['source_digest'])
    slurmlogin.install(str(base), destination.name, {'account.json': json.dumps(entry), 'python': site.site['login_python']+'\n'})
    return {'installed': str(destination), 'authorized_key_line': (bundle/'authorized-key.txt').read_text().strip(),
            'next': 'Install the supplied forced-command public key line if absent; build project environments with ci-run slurm env.'}


def main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument('--bundle', required=True)
    parser.add_argument('--apply', action='store_true')
    arguments = parser.parse_args(argv)
    try:
        print(json.dumps(install(arguments.bundle, apply=arguments.apply), indent=2))
        return 0
    except (OSError, ValueError, slurmlogin.LoginError) as error:
        print(f'slurm install: {error}', file=sys.stderr)
        return 2
