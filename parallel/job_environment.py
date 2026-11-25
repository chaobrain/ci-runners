"""Prepare the pinned default environment inside the plan or execution resource scope."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

from ci_runners import environment_cache, envuse, part
from parallel import protocol


def request(config, inv, source):
    recipe = source.get('environment_recipe')
    if not isinstance(recipe, dict) or recipe.get('repository') != source['repository'].lower():
        raise protocol.Error('job lacks a pinned environment for the verified repository')
    repository = source['repository'].lower()
    legacy_name = config.get('env_aliases', {}).get(repository, repository.split('/')[1])
    legacy_root = Path(config.get('env_legacy_root', Path(inv.host['data']) / 'ci-runners/envs'))
    legacy_repository = (repository.split('/')[0] + '/' + legacy_name.lower()
                         if repository in config.get('env_aliases', {}) else repository)
    build_env = {}
    if tunnel := inv.host.get('tunnel'):
        proxy = f'http://127.0.0.1:{tunnel["http_port"]}'
        build_env.update(http_proxy=proxy, https_proxy=proxy, no_proxy=','.join(tunnel['direct']))
    return {'root': config['env_cache'], 'recipe': recipe, 'variant': config.get('env_cache_variant'),
            'build_env': build_env,
            'conda': config.get('conda', 'conda'), 'host_lock': inv.host.get('files', {}).get('host_lock'),
            'legacy_prefix': str(legacy_root / legacy_name), 'legacy_repository': legacy_repository}


def wrap(record, preparation, release, argv):
    # Use a private file: recipes may exceed the kernel's single-argument limit.
    part._write_json(os.path.join(record, 'environment-request.json'), preparation)
    entry = ('import sys; sys.path.insert(0, sys.argv.pop(1)); '
             'from parallel.job_environment import launch; sys.exit(launch(sys.argv[1], sys.argv[2:]))')
    return [sys.executable, '-I', '-c', entry, str(release), str(record), *argv]


def launch(record, argv):
    """Held by the existing supervisor for the full preparation and test lifetime."""
    path = Path(record)
    try:
        options = part._read_json(str(path / 'environment-request.json'))
        recipe = options['recipe']
        variables = environment_cache.validate(recipe).variables
        def hold_prefix():
            selected = environment_cache.ensure(**options)
            return envuse.hold(selected, validate=lambda p: os.access(Path(p, 'bin/python'), os.X_OK))
        prefix, fd = hold_prefix()
        if (not prefix or fd is None) and options.pop('legacy_prefix', None):
            # A v1 maintenance prune can retire a build between selection and locking.
            prefix, fd = hold_prefix()
        if not prefix or fd is None:
            raise ValueError('prepared environment disappeared before use')
        # The inherited shared lock protects an adopted v1 prefix during testing.
        os.set_inheritable(fd, True)
        part._write_json(str(path / 'environment-preparation.json'), {'prefix': prefix, 'revision': recipe['revision']})
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        reason = f'environment preparation failed: {str(error)[:512]}'
        part._write_json(str(path / 'environment-preparation.json'), {'error': reason})
        print(reason, file=sys.stderr, flush=True)
        return 2
    env = dict(os.environ, **variables)
    env.update(PATH=os.path.join(prefix, 'bin') + ':' + env.get('PATH', os.defpath), CONDA_PREFIX=prefix)
    os.execvpe(argv[0], argv, env)
