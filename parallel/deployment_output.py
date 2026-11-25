"""Publish local inspection copies of deployment and runtime state."""
from __future__ import annotations

import os
import sqlite3
import stat
import tempfile
import time
from contextlib import closing
from pathlib import Path

from parallel import deployment_files as files


def _file_exists(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError(f'inspection file must be a regular file owned by this account: {path}')
    return True


def _copy_database(source: Path, target: Path) -> None:
    _file_exists(target)
    descriptor, name = tempfile.mkstemp(prefix=f'.{target.name}-', dir=target.parent)
    os.close(descriptor)
    temporary = Path(name)
    deadline = time.monotonic() + 15

    def progress(status, remaining, total):
        if status != sqlite3.SQLITE_DONE and time.monotonic() >= deadline:
            raise ValueError(f'timed out backing up {source.name}')

    try:
        try:
            with (closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)) as live,
                  closing(sqlite3.connect(temporary)) as snapshot):
                live.backup(snapshot, pages=128, progress=progress, sleep=0.05)
                snapshot.execute('PRAGMA journal_mode=DELETE').fetchone()
        except sqlite3.Error as error:
            raise ValueError(f'cannot back up {source.name}: {error}') from error
        with temporary.open('rb') as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        directory = os.open(target.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def refresh(config):
    """Refresh the inspection directory using the original runtime files."""
    output = config.runtime / 'output'
    files.private_directory(output)
    state = config.control / 'state'
    sources = {'inventory.toml': config.etc / 'inventory.toml',
               'policy.toml': config.etc / 'policy.toml',
               'deployment.json': state / 'deployment.json'}
    for role, enabled in (('scheduler', config.is_controller), ('executor', 'executor' in config.host['roles'])):
        for name in (f'{role}.sqlite', f'{role}-health.json'):
            if enabled:
                sources[name] = state / name
            elif _file_exists(output / name):
                (output / name).unlink()

    projects = config.etc / 'projects'
    output_projects = output / 'projects'
    project_paths = [] if config.organization_mode else sorted(projects.glob('*.toml'))
    if project_paths or os.path.lexists(output_projects):
        files.private_directory(output_projects)
        names = {path.name for path in project_paths}
        for old in output_projects.glob('*.toml'):
            if old.name not in names and _file_exists(old):
                old.unlink()
    sources.update({f'projects/{path.name}': path for path in project_paths})

    written, missing = [], []
    for name, source in sources.items():
        target = output / name
        if not _file_exists(source):
            if _file_exists(target):
                target.unlink()
            missing.append(name)
            continue
        if name.endswith('.sqlite'):
            _copy_database(source, target)
        else:
            files.atomic_write(target, source.read_bytes())
        written.append(name)
    devices = output / 'devices.json'
    if _file_exists(devices):
        if config.is_controller:
            written.append(devices.name)
        else:
            devices.unlink()
    if config.is_controller and config.organization_mode:
        repositories = output / 'repositories.json'
        (written if _file_exists(repositories) else missing).append(repositories.name)
    return {'output': str(output), 'files': sorted(written), 'missing': sorted(missing)}
