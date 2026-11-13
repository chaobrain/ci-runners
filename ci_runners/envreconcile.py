"""Reconcile private repositories on a host enabled by its maintainer."""
from __future__ import annotations

import os
import sys
import tempfile
import time
import tomllib
from pathlib import Path

from ci_runners import envbuild, github, locks, policy, registry


def _write_new(path: Path, text: str) -> None:
    # A hard link publishes the complete entry without replacing a manual entry.
    with tempfile.NamedTemporaryFile(dir=path.parent, mode='w', encoding='utf-8') as f:
        f.write(text)
        f.flush()
        os.link(f.name, path)


def _reconcile(repo, rid, *, projects, manual, memory_max, inv, envs, token, args, call, run):
    name = manual.get(repo, repo.split('/')[1])
    if not registry.NAME.fullmatch(name):
        raise envbuild.SpecError('repository name is not a project key')
    path = projects / f'{name}.toml'
    link = os.path.join(envs, name)
    entry = None
    existing = os.path.lexists(path)
    if existing:
        project = registry.load_file(str(projects.parent), name, inv.this)
        if project.repository != repo or project.repository_id not in (None, rid):
            raise envbuild.SpecError('project key belongs to another repository identity')
        if project.repository_id is None and repo not in manual:
            return
        if project.env != link:
            raise envbuild.SpecError('registry environment does not use the managed path')
    else:
        if repo in manual or os.path.lexists(link):
            raise envbuild.SpecError('manual project is missing or the environment path is already in use')
        entry = (f'name = "{name}"\nrepository = "{repo}"\nrepository_id = {rid}\n'
                 f'context = "{name}/ci"\n[hosts."{inv.this}"]\nmemory_max = "{memory_max}"\nenv = "{link}"\n')
        project = registry.parse(tomllib.loads(entry), inv.this, name)

    if not args.dry_run:
        os.makedirs(envs, mode=0o700, exist_ok=True)
    # Read the branch and compare the active spec while holding the same lock as build.
    if args.dry_run:
        return _apply(project, rid, existing, path, entry, inv, envs, token, args, call, run)
    with envbuild._lock(envs, name):
        if not existing and os.path.lexists(path):
            raise envbuild.Busy(name)
        return _apply(project, rid, existing, path, entry, inv, envs, token, args, call, run)


def _apply(project, rid, existing, path, entry, inv, envs, token, args, call, run):
    try:
        sha, spec, files = envbuild.fetch(call, token, project.repository, repository_id=rid)
    except envbuild.MissingSpec:
        if existing:
            raise
        return
    if os.path.islink(project.env):
        try:
            _, applied, _, _ = envbuild.kept(project.env, project.repository)
        except envbuild.SpecError:
            applied = None
        if applied is not None and applied.text == spec.text and envbuild.applies_variables(project.env):
            return

    if not existing and not args.dry_run:
        _write_new(path, entry)
    hold = None
    try:
        host_lock = inv.host['files'].get('host_lock')
        if host_lock and not args.dry_run:
            hold = locks.HostLock(host_lock, held='', timeout=60)
            hold.acquire('x')
        envbuild._build(project, spec, files, sha, role=inv.this, token=token, envs=envs,
                        conda=args.conda, memory=args.memory, run=run, dry_run=args.dry_run, locked=True, apply_variables=True)
    finally:
        if hold is not None:
            hold.close()


def main(inv, etc, envs, args, call, run):
    root = Path(etc)
    projects = root / 'projects'
    try:
        config = tomllib.loads((root / 'envreconcile.toml').read_text(encoding='utf-8'))
        if set(config) - {'memory_max', 'manual'}:
            raise ValueError('unknown envreconcile.toml keys')
        memory_max = config.get('memory_max')
        registry.memory_bytes(memory_max)
        registry.memory_bytes(args.memory)
        manual = config.get('manual', {})
        if not isinstance(manual, dict) or any(
                not registry.REPO.fullmatch(repo) or repo.lower() != repo or not isinstance(name, str)
                or not registry.NAME.fullmatch(name) for repo, name in manual.items()):
            raise ValueError('manual must map lowercase owner/repo to a project key')
        token = envbuild._token(etc)
        current = policy.load((root / 'policy.toml').read_bytes())
        if current.org is None or not current.org.private_repositories:
            raise ValueError('org policy does not admit private repositories')
        snapshot = policy.load_snapshot((root / 'org.json').read_bytes())
        effective = policy.with_snapshot(current, snapshot, time.time())
        if not args.dry_run:
            projects.mkdir(mode=0o700, exist_ok=True)
    except (OSError, ValueError) as e:
        print(f'ci-run env reconcile: {e}', file=sys.stderr)
        return 2

    result = 0
    unmanaged = set()
    for path in sorted(projects.glob('*.toml')):
        try:
            project = registry.load_file(etc, path.stem, inv.this)
        except registry.RegistryError:
            continue
        if project.repository_id is None and project.repository not in manual:
            unmanaged.add(project.repository)
        if project.repository_id is not None and snapshot.repos.get(project.repository) != project.repository_id:
            print(f'ci-run env reconcile: {project.name}: repository missing or ID changed; review required',
                  file=sys.stderr)
            result = 2

    for repo, rid in sorted(snapshot.repos.items()):
        allowed = effective.repos.get(repo)
        if (repo in unmanaged or allowed is None or allowed.paused or allowed.visibility != 'private'
                or allowed.id != rid):
            continue
        try:
            _reconcile(repo, rid, projects=projects, manual=manual, memory_max=memory_max, inv=inv, envs=envs,
                       token=token, args=args, call=call, run=run)
        except envbuild.Busy:
            print(f'ci-run env reconcile: {repo}: project busy; will retry', file=sys.stderr)
        except (OSError, ValueError, github.ApiError) as e:
            print(f'ci-run env reconcile: {repo}: {e}', file=sys.stderr)
            result = 2
    return result
