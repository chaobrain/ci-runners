"""The shared execution profile for private organization jobs."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from ci_runners import policy, registry, slurmsite
from parallel import deployment_files, protocol


_LIMITS = {
    'max_parallel': (1, registry.MAX_PARALLEL_BOUND, registry.MAX_PARALLEL),
    'max_minutes': (1, protocol.MAX_MINUTES, protocol.MAX_MINUTES),
    'retries': (0, 1, 1),
    'gpu_mem_mib': (0, 1 << 20, 0),
}
_ENV_NAME = re.compile(r'[A-Za-z0-9_-][A-Za-z0-9_.-]{0,99}\Z')
_BUILD_SUFFIX = re.compile(r'-[0-9]{8}T[0-9]{6}Z(?:-[a-z0-9_.-]+)?\Z', re.IGNORECASE | re.ASCII)


def profile(document):
    """Validate the deployment profile and fill in the usual project defaults."""
    if not isinstance(document, dict) or set(document) - _LIMITS.keys():
        raise protocol.Error('invalid organization profile keys')
    normalized = {}
    for name, (minimum, maximum, default) in _LIMITS.items():
        value = document.get(name, default)
        if type(value) is not int or not minimum <= value <= maximum:
            raise protocol.Error(f'organization.{name} must be an integer in [{minimum}, {maximum}]')
        normalized[name] = value
    return normalized


def _repository_name(organization_name, repository):
    if not isinstance(repository, str) or not protocol.REPO.fullmatch(repository):
        raise protocol.Error('invalid organization repository name')
    name = repository.lower()
    owner, slug = name.split('/')
    if owner != organization_name.lower() or name == policy.SELF or slug in ('.', '..'):
        raise protocol.Error('invalid organization repository binding')
    return name


def _stable_name(value):
    return (isinstance(value, str) and _ENV_NAME.fullmatch(value) and value.lower() != 'organization'
            and not _BUILD_SUFFIX.search(value))


def validate_environment(config, name, *, require_path):
    """Validate host-local environment options and canonicalize repository names."""
    choices = config.keys() & {'env', 'env_root', 'env_cache'}
    if len(choices) > 1:
        raise protocol.Error('env, env_root and env_cache are mutually exclusive')
    if 'env_aliases' in config and not choices.intersection({'env_root', 'env_cache'}):
        raise protocol.Error('env_aliases requires env_root or env_cache')
    if 'env_cache_variant' in config and ('env_cache' not in config or
            not isinstance(config['env_cache_variant'], str) or
            not slurmsite.VARIANT.fullmatch(config['env_cache_variant'])):
        raise protocol.Error('env_cache_variant requires env_cache and a valid variant name')
    if 'env_legacy_root' in config:
        path = config['env_legacy_root']
        if ('env_cache' not in config or not isinstance(path, str) or not re.fullmatch(r'/[A-Za-z0-9._/-]+', path)
                or '..' in Path(path).parts or len(Path(path).parts) < 2):
            raise protocol.Error('invalid organization env_legacy_root')
    field = next(iter(choices), 'env')
    if require_path or field in config:
        path = config.get(field)
        if (not isinstance(path, str) or not re.fullmatch(r'/[A-Za-z0-9._/-]+', path)
                or '..' in Path(path).parts or (field != 'env' and len(Path(path).parts) < 2)):
            raise protocol.Error(f'invalid organization environment path for {field}')
    if 'env_aliases' in config:
        if not isinstance(config['env_aliases'], dict):
            raise protocol.Error('env_aliases must map repository names to environment names')
        aliases = {}
        for repository, directory in config['env_aliases'].items():
            repository = _repository_name(name, repository)
            if repository in aliases or not _stable_name(directory):
                raise protocol.Error('duplicate repository or invalid directory in env_aliases')
            aliases[repository] = directory
        config['env_aliases'] = aliases


def standalone_executor(settings):
    return 'organization' in settings and 'plan' not in settings


def validate_settings(config, *, controller):
    """Validate the organization table rendered for this host's control service."""
    expected = {'name', 'profile'} if controller else {'name'}
    optional = {'env', 'env_root', 'env_aliases', 'env_cache', 'conda', 'env_legacy_root', 'env_cache_variant'}
    if controller:
        optional.add('slurm_allowlist_file')
    if not isinstance(config, dict) or not expected <= config.keys() <= expected | optional:
        raise protocol.Error('invalid organization service configuration')
    if not isinstance(config['name'], str) or not policy._ORG.fullmatch(config['name']):
        raise protocol.Error('invalid organization name')
    validate_environment(config, config['name'], require_path=True)
    if 'conda' in config and ('env_cache' not in config or not isinstance(config['conda'], str)
                              or not re.fullmatch(r'/[A-Za-z0-9._/-]+', config['conda'])
                              or '..' in Path(config['conda']).parts):
        raise protocol.Error('invalid organization cache conda executable')
    if controller and profile(config['profile']) != config['profile']:
        raise protocol.Error('organization service profile must include all limits')
    if 'slurm_allowlist_file' in config:
        path = config['slurm_allowlist_file']
        if not isinstance(path, str) or not Path(path).is_absolute() or '..' in Path(path).parts or '\0' in path:
            raise protocol.Error('invalid organization Slurm allowlist path')


def validate_slurm_route(binding):
    project = binding.get('slurm_project')
    target = binding.get('slurm_target')
    if not isinstance(project, str) or not slurmsite.PROJECT.fullmatch(project):
        raise protocol.Error('invalid Slurm project name')
    segments = target.split('/') if isinstance(target, str) else []
    if len(segments) != 2 or not all(slurmsite.NAME.fullmatch(segment) for segment in segments):
        raise protocol.Error('Slurm target must be site/target')


def _unique_keys(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise protocol.Error('duplicate Slurm allowlist key')
    return result


def read_slurm_allowlist(path, organization_name):
    """Read current controller-owned Slurm permissions, indexed by verified GitHub ID."""
    try:
        document = json.loads(deployment_files.read_private(path), object_pairs_hook=_unique_keys)
    except (OSError, ValueError, RecursionError) as error:
        raise protocol.Error('cannot read private Slurm allowlist') from error
    if (not isinstance(document, dict) or set(document) != {'schema', 'repositories'}
            or type(document['schema']) is not int or document['schema'] != 1
            or not isinstance(document['repositories'], list)):
        raise protocol.Error('invalid Slurm allowlist schema')
    bindings, names = {}, set()
    for entry in document['repositories']:
        required = {'repository_id', 'repository', 'slurm_project', 'slurm_target'}
        if not isinstance(entry, dict) or not required <= set(entry) or set(entry) - required - {'gpu_targets'}:
            raise protocol.Error('invalid Slurm allowlist entry')
        ident, name = entry['repository_id'], entry['repository']
        if not protocol.positive(ident) or not isinstance(name, str) or not protocol.REPO.fullmatch(name):
            raise protocol.Error('invalid Slurm allowlist repository identity')
        name = name.lower()
        if name.split('/')[0] != organization_name.lower() or name == policy.SELF or ident in bindings or name in names:
            raise protocol.Error('duplicate or out-of-scope Slurm allowlist repository')
        validate_slurm_route(entry)
        targets = entry.get('gpu_targets', {})
        if (not isinstance(targets, dict) or len(targets) > 16
                or any(not isinstance(key, str) or not protocol.ID.fullmatch(key) for key in targets)):
            raise protocol.Error('invalid logical GPU Slurm targets')
        for target in targets.values():
            validate_slurm_route(dict(entry, slurm_target=target))
        bindings[ident] = {**entry, 'repository': name}
        names.add(name)
    return bindings


def slurm_binding(allowlist, source):
    ident, name = source.get('repository_id'), source.get('repository')
    if not protocol.positive(ident) or not isinstance(name, str) or not protocol.REPO.fullmatch(name):
        return None
    binding = allowlist.get(ident)
    return binding if binding is not None and binding['repository'] == name.lower() else None


def slurm_cpu_target(site, binding, *, gpu=False):
    """Return an enabled target of the requested class, served by a live account."""
    site_name, target_name = binding['slurm_target'].split('/')
    target = site.targets.get(target_name)
    if site.name != site_name or target is None or target.gpu != gpu or not target.enabled_for(binding['slurm_project']):
        return None
    return target if any(not acct.retired and target_name in acct.a['targets'] for acct in site.accounts.values()) else None


def read_slurm_target(etc, binding, *, gpu=False, capacity=False):
    """Check the installed site on every authorization, including after administrator edits."""
    try:
        validate_slurm_route(binding)
        site_name = binding['slurm_target'].split('/')[0]
        path = Path(etc) / 'slurm' / f'{site_name}.toml'
        site = slurmsite.load(deployment_files.read_private(path).decode('utf-8'))
    except (OSError, ValueError, RecursionError):
        return None
    target = slurm_cpu_target(site, binding, gpu=gpu)
    return (target, min(site.site['max_parts'], registry.MAX_PARALLEL_BOUND)) if target and capacity else target


def gpu_target_profiles(etc, binding):
    """Pin only enabled, served profiles from the controller's private target table."""
    profiles = {}
    for logical, route in binding.get('gpu_targets', {}).items():
        current = read_slurm_target(etc, dict(binding, slurm_target=route), gpu=True, capacity=True)
        if current is not None:
            target, maximum = current
            profiles[logical] = dict(slurm_target=route, gpu_model=target.t['gpu_model'],
                                     variant=target.t['variant'], cluster_max_parallel=maximum)
    return profiles


def gpu_slurm_profile(source, task):
    """The caller's logical request selects a controller-prepared profile only."""
    profiles = source.get('slurm_gpu_targets', {})
    logical = task.get('cluster_target')
    profile = profiles.get(logical) if isinstance(profiles, dict) else None
    if (not task.get('gpu') or task.get('device') != 'slurm' or not isinstance(profile, dict)
            or set(profile) != {'slurm_target', 'gpu_model', 'variant', 'cluster_max_parallel'}
            or not protocol.positive(profile['cluster_max_parallel'])
            or profile['cluster_max_parallel'] > registry.MAX_PARALLEL_BOUND):
        raise protocol.Error('GPU Slurm target was not prepared by the controller')
    return dict(profile)


def _environment(config, repository):
    if 'env_cache' in config:
        # The authorized recipe materializes a prefix on the plan or execution host.
        return ''
    if 'env_root' not in config:
        return config['env']
    name = _repository_name(config['name'], repository)
    directory = config.get('env_aliases', {}).get(name, name.split('/')[1])
    if not _stable_name(directory):
        return ''
    return str(Path(config['env_root']) / directory)


def build_ready(settings, repository, build):
    """Check the physical build held for the job, even if its stable link has since moved."""
    config = settings['organization']
    try:
        if not build or 'env_cache' in config:
            return False
        path = Path(build)
        if path != path.resolve(strict=True):
            return False
        if 'env_root' in config:
            if not _environment(config, repository):
                return False
            relative = path.relative_to(Path(config['env_root']).resolve(strict=True))
            if not relative.parts or any(p.startswith('.') or p.lower() == 'organization' for p in relative.parts):
                return False
            if not (path / 'bin').resolve(strict=True).is_relative_to(path):
                return False
        python = path / 'bin/python'
        return python.is_file() and os.access(python, os.X_OK)
    except (OSError, ValueError, RuntimeError):
        return False


def environment_ready(settings, repository=None):
    try:
        prefix = _environment(settings['organization'], repository)
        if not prefix:
            return False
        selected = Path(prefix).resolve(strict=True)
    except (OSError, ValueError, RuntimeError):
        return False
    return build_ready(settings, repository, selected)


def ready_repositories(settings):
    """Advertise only repositories for which this device has a usable local prefix."""
    config = settings['organization']
    if 'env_root' not in config:
        return []
    candidates = set(config.get('env_aliases', {}))
    try:
        for entry in Path(config['env_root']).iterdir():
            if entry.name == entry.name.lower() and _stable_name(entry.name):
                candidates.add(f"{config['name'].lower()}/{entry.name}")
    except OSError:
        return []
    return sorted(name for name in candidates if environment_ready(settings, name))


def project(settings, inv, source):
    """Bind trusted source facts and limits to this host's environment."""
    config = settings['organization']
    repository = source.get('repository')
    repository_id = source.get('repository_id')
    name = source.get('project')
    _repository_name(config['name'], repository)
    if not protocol.positive(repository_id) or name != f'repo-{repository_id}':
        raise protocol.Error('invalid organization repository binding')
    limits = profile(source.get('organization_profile'))
    if limits != source['organization_profile']:
        raise protocol.Error('incomplete organization job profile')
    document = {'name': name, 'repository': repository, 'context': source.get('context'),
                'hosts': {inv.this: {'memory_max': inv.host['memory_max'], 'env': _environment(config, repository)}},
                **limits}
    return registry.parse(document, inv.this, name)
