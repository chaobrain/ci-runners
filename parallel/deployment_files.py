"""Private atomic files and content-addressed copies of the public release tree."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path

SOURCE_DIRECTORIES = {'ci_runners', 'parallel', 'actions', 'bin', 'hooks', 'setup', 'tests', 'docs', '.github', 'experiments'}
SOURCE_FILES = {'VERSION', 'pyproject.toml', 'README.md', 'AGENTS.md', '.gitignore', 'MANIFEST.in'}


def private_directory(path, *, preserve_mode=False):
    path = Path(path)
    for parent in path.parents:
        if parent.is_symlink():
            raise ValueError(f'parent directory is a symlink: {parent}')
    if path.is_symlink():
        raise ValueError(f'directory is a symlink: {path}')
    created = not path.exists()
    if created:
        path.mkdir(parents=True, mode=0o700)
    if not path.is_dir() or path.stat().st_uid != os.getuid():
        raise ValueError(f'directory is not owned by this account: {path}')
    if created or not preserve_mode:
        path.chmod(0o700)


def file_state(path):
    path = Path(path)
    if not os.path.lexists(path):
        return None
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError(f'not a regular file owned by this account: {path}')
    return {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'mode': stat.S_IMODE(info.st_mode)}


def atomic_write(path, data, mode=0o600):
    path = Path(path)
    file_state(path)
    private_directory(path.parent, preserve_mode=True)
    data = data.encode() if isinstance(data, str) else data
    descriptor, temporary = tempfile.mkstemp(prefix='.'+path.name+'-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def check_private_file(path, info):
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
        raise ValueError(f'private file must be owned by this account and mode 600: {path}')


def read_private(path, limit=1 << 20):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(descriptor, 'rb') as stream:
        check_private_file(path, os.fstat(stream.fileno()))
        data = stream.read(limit+1)
    if not data or len(data) > limit:
        raise ValueError(f'private file empty or oversized: {path}')
    return data


def read_json(path, default=None):
    if not os.path.lexists(path):
        return {} if default is None else default
    return json.loads(read_private(path))


def write_json(path, value):
    atomic_write(path, json.dumps(value, indent=2, sort_keys=True)+'\n')


def release_files(root):
    root = Path(root)
    result = []
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root)
        if relative.parts[0] not in SOURCE_DIRECTORIES | SOURCE_FILES:
            continue
        if any(part in ('__pycache__', '.pytest_cache') for part in relative.parts) or path.suffix in ('.pyc', '.pyo'):
            continue
        if path.is_symlink():
            raise ValueError(f'release source contains a symlink: {relative}')
        if path.is_file():
            result.append(relative)
    return result


def release_digest(root):
    digest = hashlib.sha256()
    for relative in release_files(root):
        path = Path(root)/relative
        digest.update(f'{relative}\0{bool(path.stat().st_mode & 0o111)}\0'.encode())
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def install_release(source, destination, expected):
    source, destination = Path(source), Path(destination)
    if os.path.lexists(destination):
        if destination.is_symlink() or not destination.is_dir() or release_digest(destination) != expected:
            raise ValueError('existing release version has different contents; use a separate runtime')
        return
    private_directory(destination.parent)
    temporary = Path(tempfile.mkdtemp(prefix='.release-', dir=destination.parent))
    try:
        for relative in release_files(source):
            target = temporary/relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source/relative, target)
            target.chmod(0o555 if (source/relative).stat().st_mode & 0o111 else 0o444)
        if release_digest(temporary) != expected:
            raise ValueError('release source changed while copying')
        os.rename(temporary, destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def activate_release(destination):
    destination = Path(destination)
    link = destination.parent/'hook'
    if os.path.lexists(link) and not link.is_symlink():
        raise ValueError('releases/hook is not a symlink')
    if link.is_symlink() and link.resolve() == destination:
        return
    temporary = destination.parent/f'.hook-{os.getpid()}'
    os.symlink(destination.name, temporary)
    try:
        os.replace(temporary, link)
    finally:
        if temporary.is_symlink():
            temporary.unlink()
