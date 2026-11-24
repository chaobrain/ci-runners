"""Storage-host helper. The configured root is the only filesystem authority."""
from __future__ import annotations

import argparse
import contextlib
import errno
import json
import os
import re
import stat
import sys
import uuid
from pathlib import Path

from ci_runners import storage


def regular(path, maximum):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise storage.Error('limit')
        return stream.read(maximum + 1)


def flush_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Store:
    def __init__(self, root, store='pilot', limits=None):
        self.root = Path(root)
        self.store = store
        self.limits = {**storage.DEFAULTS, **(limits or {})}
        if not self.root.is_absolute():
            raise storage.Error('configuration')
        for path in (*reversed(self.root.parents), self.root):
            if path.is_symlink():
                raise storage.Error('configuration')
        if not self.root.is_dir():
            raise storage.Error('configuration')
        for name in ('.staging', '.locks', 'objects'):
            self.directory(self.root/name)

    def directory(self, path):
        if path != self.root and not path.is_relative_to(self.root):
            raise storage.Error('identity')
        current = self.root
        for name in path.relative_to(self.root).parts:
            current = current / name
            current.mkdir(exist_ok=True, mode=0o700)
            if current.is_symlink() or not current.is_dir():
                raise storage.Error('permission')
        return path

    def location(self, ident):
        path = self.root / storage.object_path(ident)
        self.directory(path.parent)
        if path.is_symlink():
            raise storage.Error('permission')
        return path

    @contextlib.contextmanager
    def locked(self, ident):
        import fcntl
        name = storage.object_path(ident).name
        fd = os.open(self.root/'.locks'/name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def begin(self):
        name = uuid.uuid4().hex
        self.directory(self.root/'.staging'/name)
        return {'stage': name}

    def describe(self, ident, manifest_sha256=None):
        ident = storage.identity(ident)
        path = self.location(ident)
        try:
            manifest = regular(path/'manifest.json', self.limits['manifest_bytes'])
            if (path/'bundle.zip').is_symlink():
                raise storage.Error('corrupt')
            doc = storage.verify(manifest, path/'bundle.zip', ident, self.limits, manifest_sha256)
        except FileNotFoundError as exc:
            raise storage.Error('missing') from exc
        return {'reference': {'schema': 1, 'store': self.store, 'identity': ident,
                              'manifest_sha256': storage.digest(manifest)}, 'manifest': doc}

    def commit(self, stage, ident):
        if not isinstance(stage, str) or not re.fullmatch('[0-9a-f]{32}', stage):
            raise storage.Error('identity')
        ident = storage.identity(ident)
        path = self.root/'.staging'/stage
        if path.is_symlink() or not path.is_dir():
            raise storage.Error('missing')
        manifest = regular(path/'manifest.json', self.limits['manifest_bytes'])
        if (path/'bundle.zip').is_symlink():
            raise storage.Error('corrupt')
        doc = storage.verify(manifest, path/'bundle.zip', ident, self.limits)
        with self.locked(ident):
            final = self.location(ident)
            if final.exists():
                old = self.describe(ident)
                if any(old['manifest'][key] != doc[key] for key in ('identity', 'size', 'sha256', 'members', 'retention')):
                    raise storage.Error('conflict')
                return old
            if set(p.name for p in path.iterdir()) != {'manifest.json', 'bundle.zip'}:
                raise storage.Error('corrupt')
            for name in ('manifest.json', 'bundle.zip'):
                fd = os.open(path/name, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            flush_directory(path)
            os.rename(path, final)
            flush_directory(final.parent)
            flush_directory(self.root/'.staging')
            return self.describe(ident)

    def list_completed(self, run, after=''):
        if (not isinstance(run, dict) or set(run) != {'repository_id', 'repository', 'run_id', 'run_attempt'}
                or any(type(run[k]) is not int or run[k] <= 0 for k in ('repository_id', 'run_id', 'run_attempt'))
                or not isinstance(run['repository'], str) or not storage.REPO.fullmatch(run['repository'])
                or after and not storage.HEX.fullmatch(after)):
            raise storage.Error('identity')
        parent = self.root/'objects'/str(run['repository_id'])/str(run['run_id'])/str(run['run_attempt'])
        if not parent.exists():
            return {'references': [], 'next': ''}
        self.directory(parent)
        names = sorted(p.name for p in parent.iterdir())
        if len(names) > self.limits['manifests_per_run']:
            raise storage.Error('limit')
        wanted = [name for name in names if name > after]
        references = []
        for name in wanted[:self.limits['manifests_per_page']]:
            path = parent/name
            if not storage.HEX.fullmatch(name) or path.is_symlink():
                raise storage.Error('corrupt')
            try:
                doc = json.loads(regular(path/'manifest.json', self.limits['manifest_bytes']))
                ident = storage.identity(doc['identity'])
            except (OSError, ValueError, KeyError) as exc:
                raise storage.Error('corrupt') from exc
            if any(ident[k] != v for k, v in run.items()) or storage.object_path(ident).name != name:
                raise storage.Error('identity')
            references.append(self.describe(ident)['reference'])
        return {'references': references, 'next': wanted[self.limits['manifests_per_page']-1]
                if len(wanted) > self.limits['manifests_per_page'] else ''}

    def cleanup(self, run, now, dry_run=True):
        if dry_run is not True:
            # No deletion until live-state/map reachability and operator approval exist.
            raise storage.Error('configuration')
        items, after = [], ''
        while True:
            page = self.list_completed(run, after)
            items.extend({'reference': ref, 'delete': False, 'reason': 'terminal-state-and-dependencies-unverified'}
                         for ref in page['references'])
            after = page['next']
            if not after:
                return {'dry_run': True, 'objects': items}

    def call(self, request):
        if not isinstance(request, dict):
            raise storage.Error('identity')
        op = request.get('operation')
        if op == 'begin':
            return self.begin()
        if op == 'commit':
            return self.commit(request['stage'], request['identity'])
        if op == 'describe':
            return self.describe(request['identity'], request.get('manifest_sha256'))
        if op == 'list':
            return self.list_completed(request['run'], request.get('after', ''))
        if op == 'cleanup':
            return self.cleanup(request['run'], request['now'], request.get('dry_run', True))
        raise storage.Error('configuration')


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--store', required=True)
    args = parser.parse_args(argv)
    try:
        raw = sys.stdin.buffer.read(storage.DEFAULTS['manifest_bytes'] + 1)
        if len(raw) > storage.DEFAULTS['manifest_bytes']:
            raise storage.Error('limit')
        request = json.loads(raw)
        limits = request.pop('limits', {})
        if (not isinstance(limits, dict) or set(limits) - set(storage.DEFAULTS)
                or any(type(v) is not int or not 0 < v <= storage.DEFAULTS[k] for k, v in limits.items())):
            raise storage.Error('configuration')
        result = Store(args.root, args.store, limits).call(request)
        print(storage.dumps({'result': result}).decode())
        return 0
    except (storage.Error, OSError, ValueError, KeyError, TypeError) as exc:
        code = exc.code if isinstance(exc, storage.Error) else 'transport'
        if isinstance(exc, OSError):
            code = {errno.EACCES: 'permission', errno.EPERM: 'permission', errno.ENOSPC: 'capacity',
                    errno.ENOENT: 'missing'}.get(exc.errno, 'transport')
        print(storage.dumps({'error': code}).decode())
        return 1


if __name__ == '__main__':
    sys.exit(main())
