"""Private storage configuration and immutable, bounded CI bundle format."""
from __future__ import annotations

import hashlib
import errno
import functools
import json
import math
import os
import re
import shutil
import stat
import tempfile
import time
import tomllib
import zipfile
from pathlib import Path, PurePosixPath

DEFAULTS = dict(connect_seconds=10, operation_seconds=120, attempts=3,
                manifest_bytes=1 << 20, compressed_bytes=1 << 30, unpacked_bytes=2 << 30,
                entries=10000, manifests_per_page=1024, manifests_per_run=10000)
ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')
REPO = re.compile(r'[a-z0-9-]+/[a-z0-9_.-]+\Z')
HEX = re.compile(r'[0-9a-f]{64}\Z')
KEY = ('repository_id', 'repository', 'run_id', 'run_attempt', 'kind', 'part_id', 'part_attempt', 'bundle_name')


class Error(ValueError):
    """A public-safe storage failure; raw transport diagnostics are never included."""
    def __init__(self, code):
        self.code = code
        super().__init__('CI storage: ' + code)


def safe_errors(operation):
    """Keep local filenames out of public storage errors as well as SSH failures."""
    @functools.wraps(operation)
    def call(*args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except Error:
            raise
        except OSError as exc:
            code = {errno.EACCES: 'permission', errno.EPERM: 'permission', errno.ENOENT: 'missing',
                    errno.ENOSPC: 'capacity'}.get(exc.errno, 'transport')
            raise Error(code) from None
        except (ValueError, TypeError, KeyError, RuntimeError, zipfile.BadZipFile) as exc:
            raise Error('corrupt') from None
    return call


def dumps(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def check_deadline(deadline):
    if deadline is not None and time.monotonic() >= deadline:
        raise Error('timeout')


def stream_digest(stream, deadline=None):
    value = hashlib.sha256()
    while True:
        check_deadline(deadline)
        block = stream.read(1 << 20)
        if not block:
            return value.hexdigest()
        value.update(block)


def file_digest(path, deadline=None):
    with open(path, 'rb') as stream:
        return stream_digest(stream, deadline)


def identity(value):
    extra = {'sha', 'context', 'batch_id', 'task_id', 'task_attempt', 'allocation_id', 'execution_job_id'}
    if not isinstance(value, dict) or set(value) - set(KEY) - extra or not set(KEY) <= set(value):
        raise Error('identity')
    if any(type(value[k]) is not int or value[k] <= 0 for k in ('repository_id', 'run_id', 'run_attempt', 'part_attempt')):
        raise Error('identity')
    if (value['part_attempt'] > 99 or not isinstance(value['repository'], str)
            or not REPO.fullmatch(value['repository'])
            or any(p in ('.', '..') for p in value['repository'].split('/'))):
        raise Error('identity')
    if value['kind'] not in ('plan', 'allocation', 'outcome', 'project', 'mapping'):
        raise Error('identity')
    for key in ('part_id', 'bundle_name', 'batch_id', 'task_id', 'allocation_id'):
        if key in value and (not isinstance(value[key], str) or not ID.fullmatch(value[key])):
            raise Error('identity')
    if not isinstance(value.get('sha'), str) or not re.fullmatch('[0-9a-f]{40}', value['sha']):
        raise Error('identity')
    if not isinstance(value.get('context'), str) or not re.fullmatch(r'[A-Za-z0-9._/-]{1,100}', value['context']):
        raise Error('identity')
    if value['kind'] == 'allocation':
        if not {'batch_id', 'task_id', 'task_attempt', 'allocation_id', 'execution_job_id'} <= set(value):
            raise Error('identity')
        if (type(value['execution_job_id']) is not int or value['execution_job_id'] <= 0
                or type(value['task_attempt']) is not int or value['task_attempt'] != value['part_attempt']):
            raise Error('identity')
    return dict(value)


def object_path(value):
    value = identity(value)
    return PurePosixPath('objects', str(value['repository_id']), str(value['run_id']),
                         str(value['run_attempt']), digest(dumps({k: value[k] for k in KEY})))


def binding(value):
    if not isinstance(value, dict) or value.get('schema') != 1 or type(value.get('schema')) is not int:
        raise Error('configuration')
    if value == {'schema': 1, 'backend': 'github'}:
        return dict(value)
    if (set(value) != {'schema', 'backend', 'store'} or value['backend'] != 'ssh'
            or not isinstance(value['store'], str) or not ID.fullmatch(value['store'])):
        raise Error('configuration')
    return dict(value)


@safe_errors
def read_config(etc):
    path = Path(etc) / 'storage.toml'
    if not path.exists() and not path.is_symlink():
        return {'schema': 1, 'default_backend': 'github', 'repositories': {}, 'stores': {}, 'limits': dict(DEFAULTS)}
    try:
        if path.is_symlink() or path.stat().st_mode & 0o022:
            raise Error('configuration')
        data = path.read_bytes()
        if len(data) > DEFAULTS['manifest_bytes']:
            raise Error('limit')
        cfg = tomllib.loads(data.decode('utf-8'))
    except (OSError, ValueError) as exc:
        raise Error('configuration') from exc
    if (not isinstance(cfg, dict) or type(cfg.get('schema')) is not int or cfg['schema'] != 1
            or set(cfg) - {'schema', 'default_backend', 'repositories', 'stores', 'limits', 'retention', 'schema1_cutover'}
            or cfg.get('default_backend', 'github') != 'github'):
        raise Error('configuration')
    if any(not isinstance(cfg.get(k, {}), dict) for k in ('limits', 'repositories', 'stores', 'retention')):
        raise Error('configuration')
    retention = cfg.get('retention', {})
    if (set(retention) - {'run_days', 'diagnostic_days', 'mapping_days', 'cleanup_dry_run'}
            or type(retention.get('cleanup_dry_run', True)) is not bool
            or any(type(v) is not int or v <= 0 for k, v in retention.items() if k != 'cleanup_dry_run')):
        raise Error('configuration')
    limits = {**DEFAULTS, **cfg.get('limits', {})}
    if set(limits) != set(DEFAULTS) or any(type(v) is not int or not 0 < v <= DEFAULTS[k] for k, v in limits.items()):
        raise Error('configuration')
    cfg.update(limits=limits, repositories=cfg.get('repositories', {}), stores=cfg.get('stores', {}))
    for repo, item in cfg['repositories'].items():
        if not REPO.fullmatch(repo) or not isinstance(item, dict):
            raise Error('configuration')
        b = binding({'schema': 1, **item})
        if b['backend'] == 'ssh' and b['store'] not in cfg['stores']:
            raise Error('configuration')
    for name, item in cfg['stores'].items():
        if not ID.fullmatch(name) or not isinstance(item, dict) or set(item) != {'ssh_alias', 'identity_file', 'known_hosts_file', 'root', 'helper'}:
            raise Error('configuration')
        if not isinstance(item['ssh_alias'], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@-]{0,255}', item['ssh_alias']):
            raise Error('configuration')
        for key in ('identity_file', 'known_hosts_file', 'root', 'helper'):
            if not isinstance(item[key], str) or not re.fullmatch(r'/[A-Za-z0-9_./-]+', item[key]) or '..' in item[key].split('/'):
                raise Error('configuration')
    cutovers = cfg.get('schema1_cutover', {})
    if not isinstance(cutovers, dict):
        raise Error('configuration')
    for repo, run in cutovers.items():
        if (not isinstance(repo, str) or not REPO.fullmatch(repo) or type(run) is not int or run <= 0
                or cfg['repositories'].get(repo, {}).get('backend') != 'ssh'):
            raise Error('configuration')
    cfg['schema1_cutover'] = cutovers
    return cfg


def select(cfg, repository):
    item = cfg['repositories'].get(repository.lower(), {'backend': 'github'})
    return binding({'schema': 1, **item})


def reference(value, expected=None, store=None):
    if (not isinstance(value, dict) or set(value) != {'schema', 'store', 'identity', 'manifest_sha256'}
            or type(value['schema']) is not int or value['schema'] != 1
            or not isinstance(value['store'], str) or not ID.fullmatch(value['store'])
            or not isinstance(value['manifest_sha256'], str) or not HEX.fullmatch(value['manifest_sha256'])):
        raise Error('identity')
    ident = identity(value['identity'])
    if expected is not None and ident != identity(expected) or store is not None and value['store'] != store:
        raise Error('identity')
    return value


def member_name(name):
    if (not isinstance(name, str) or not name or name.startswith('/') or '\\' in name or ':' in name
            or any(p in ('', '.', '..', '.git') for p in name.split('/'))
            or any(ord(c) < 32 or c in '"<>|*?' for c in name)):
        raise Error('corrupt')
    return name


@safe_errors
def pack(source, destination, ident, limits, retention='diagnostic', deadline=None):
    ident = identity(ident)
    source = Path(source)
    if source.is_symlink() or not source.is_dir():
        raise Error('missing')
    members, total, entries = [], 0, set()
    paths = []
    for path in source.rglob('*'):
        check_deadline(deadline)
        paths.append(path)
        if len(paths) > limits['entries']:
            raise Error('limit')
    with zipfile.ZipFile(destination, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(paths):
            check_deadline(deadline)
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise Error('corrupt')
            if path.is_dir():
                continue
            name = member_name(path.relative_to(source).as_posix())
            entries.update('/'.join(name.split('/')[:i]) for i in range(1, len(name.split('/')) + 1))
            expected_size = path.stat().st_size
            total += expected_size
            if len(entries) > limits['entries'] or total > limits['unpacked_bytes']:
                raise Error('limit')
            info = zipfile.ZipInfo(name)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            value, size = hashlib.sha256(), 0
            descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            with os.fdopen(descriptor, 'rb') as src, archive.open(info, 'w', force_zip64=True) as out:
                if not stat.S_ISREG(os.fstat(src.fileno()).st_mode):
                    raise Error('corrupt')
                while True:
                    check_deadline(deadline)
                    block = src.read(1 << 20)
                    if not block:
                        break
                    size += len(block)
                    if size > expected_size:
                        raise Error('limit')
                    value.update(block)
                    out.write(block)
            if size != expected_size:
                raise Error('corrupt')
            members.append({'name': name, 'size': size, 'sha256': value.hexdigest()})
    size = Path(destination).stat().st_size
    if not members:
        raise Error('missing')
    if size > limits['compressed_bytes']:
        raise Error('limit')
    doc = {'schema': 1, 'identity': ident, 'state': 'complete', 'size': size,
           'sha256': file_digest(destination, deadline), 'members': members, 'created_at': time.time(), 'retention': retention}
    if len(dumps(doc)) > limits['manifest_bytes']:
        raise Error('limit')
    return doc


def verify(manifest, payload, expected, limits, manifest_sha256=None, deadline=None):
    check_deadline(deadline)
    if (len(manifest) > limits['manifest_bytes'] or
            manifest_sha256 is not None and digest(manifest) != manifest_sha256):
        raise Error('corrupt')
    try:
        doc = json.loads(manifest)
        if (not isinstance(doc, dict) or type(doc.get('schema')) is not int or doc['schema'] != 1 or doc.get('state') != 'complete'
                or doc.get('identity') != identity(expected)):
            raise Error('identity')
        if (doc.get('retention') not in ('run', 'diagnostic', 'mapping')
                or type(doc.get('created_at')) not in (int, float) or not math.isfinite(doc['created_at']) or doc['created_at'] < 0):
            raise Error('corrupt')
        items = doc['members']
        if not isinstance(items, list) or not items or len(items) > limits['entries']:
            raise Error('limit')
        if (type(doc['size']) is not int or not 0 < doc['size'] <= limits['compressed_bytes']
                or Path(payload).stat().st_size != doc['size'] or file_digest(payload, deadline) != doc['sha256']):
            raise Error('corrupt')
        with zipfile.ZipFile(payload) as archive:
            infos = archive.infolist()
            if len(infos) != len(items):
                raise Error('corrupt')
            seen, entries, total = set(), set(), 0
            for info, item in zip(infos, items):
                name = member_name(info.filename)
                mode = stat.S_IFMT(info.external_attr >> 16)
                if (name in seen or mode not in (0, stat.S_IFREG) or info.is_dir()
                        or item['name'] != name or type(item['size']) is not int or item['size'] < 0
                        or item['size'] != info.file_size):
                    raise Error('corrupt')
                seen.add(name)
                entries.update('/'.join(name.split('/')[:i]) for i in range(1, len(name.split('/')) + 1))
                total += info.file_size
                if total > limits['unpacked_bytes'] or len(entries) > limits['entries']:
                    raise Error('limit')
                with archive.open(info) as stream:
                    if stream_digest(stream, deadline) != item['sha256']:
                        raise Error('corrupt')
            if any('/'.join(name.split('/')[:i]) in seen for name in seen for i in range(1, len(name.split('/')))):
                raise Error('corrupt')
        return doc
    except Error:
        raise
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, EOFError, zipfile.BadZipFile) as exc:
        raise Error('corrupt') from exc


@safe_errors
def unpack(manifest, payload, expected, destination, limits, manifest_sha256=None, deadline=None):
    doc = verify(manifest, payload, expected, limits, manifest_sha256, deadline)
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise Error('conflict')
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.ci-bundle-', dir=destination.parent))
    try:
        with zipfile.ZipFile(payload) as archive:
            for item in doc['members']:
                out = staging / item['name']
                out.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(item['name']) as src, out.open('xb') as target:
                    while True:
                        check_deadline(deadline)
                        block = src.read(1 << 20)
                        if not block:
                            break
                        target.write(block)
        check_deadline(deadline)
        # Caller owns the local private parent; never merge an existing directory.
        os.rename(staging, destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return doc


@safe_errors
def client(etc, pinned):
    pinned = binding(pinned)
    if pinned['backend'] != 'ssh':
        raise Error('configuration')
    cfg = read_config(etc)
    if pinned['store'] not in cfg['stores']:
        raise Error('configuration')
    from ci_runners.storage_ssh import SSHStore
    return SSHStore(cfg['stores'][pinned['store']], pinned['store'], cfg['limits'])
