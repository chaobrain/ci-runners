"""Bounded, immutable synthetic probe store. Linux; administrator prepares roots."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import tempfile
import time

from ci_runners import storage_auth as auth, storage_protocol as p


def private_fd(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    info = os.fstat(fd)
    if not (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
            and not info.st_mode & 0o077 and info.st_nlink == 1):
        os.close(fd)
        raise p.Error('permissions')
    return fd


@contextmanager
def lock(path, deadline):
    fd = private_fd(path)
    try:
        while True:
            deadline.remaining()
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(min(.02, deadline.remaining()))
        yield
    finally:
        os.close(fd)


class Store:
    def __init__(self, root, *, capacity=1 << 30, free_floor=50 << 30):
        owner = Path(root).lstat().st_uid
        p.require(owner in (0, os.getuid()), 'permissions')
        self.root = auth.protected(root, owner=owner, directory=True)
        self.capacity, self.free_floor = capacity, free_floor
        p.require(p.integer(capacity, 1, 1 << 30) and p.integer(free_floor, 0, 2**63 - 1), 'configuration')
        for name in ('staging', 'objects', 'ledger'):
            auth.protected(self.root / name, owner=os.getuid(), private=True, directory=True)
        self.staging = self.root / 'staging'
        self.objects = self.root / 'objects'
        self.ledger = self.root / 'ledger'

    @contextmanager
    def slot(self):
        held = None
        try:
            for n in range(4):
                fd = private_fd(self.ledger / f'slot-{n}')
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    os.close(fd)
                    continue
                held = fd
                break
            p.require(held is not None, 'busy')
            yield
        finally:
            if held is not None:
                os.close(held)

    def _claim(self, grant):
        path = self.ledger / 'grants.sqlite'
        os.close(private_fd(path))
        db = sqlite3.connect(path, timeout=0)
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('CREATE TABLE IF NOT EXISTS grants (jti TEXT PRIMARY KEY, digest TEXT NOT NULL, uses INTEGER NOT NULL)')
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT digest, uses FROM grants WHERE jti=?', (grant['jti'],)).fetchone()
            fingerprint = p.digest(p.canonical(grant))
            if row is not None:
                p.require(row[0] == fingerprint and row[1] < 3, 'replay')
                db.execute('UPDATE grants SET uses=uses+1 WHERE jti=?', (grant['jti'],))
            else:
                count = db.execute('SELECT count(*) FROM grants').fetchone()[0]
                p.require(count < 4096, 'quota')
                db.execute('INSERT INTO grants VALUES(?,?,1)', (grant['jti'], fingerprint))
            db.commit()  # A crash after admission must not reset replay accounting.
        finally:
            db.close()

    def _usage(self):
        total, entries = 0, 0
        for base in (self.staging, self.objects):
            for directory, dirs, files in os.walk(base, followlinks=False):
                for name in dirs + files:
                    info = (Path(directory) / name).lstat()
                    p.require(info.st_uid == os.getuid() and not stat.S_ISLNK(info.st_mode)
                              and (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)
                                   and info.st_nlink == 1), 'corrupt')
                    total += max(info.st_size, info.st_blocks * 512)
                    entries += 1
                    p.require(entries <= 20000, 'quota')
        return total

    def _read(self, key):
        path = self.objects / key
        try:
            auth.protected(path, owner=os.getuid(), private=True, directory=True)
            manifest = auth.read_file(path / 'manifest.json', owner=os.getuid(), private=True)
            payload = auth.read_file(path / 'payload', owner=os.getuid(), private=True, limit=p.PAYLOAD_MAX)
        except FileNotFoundError:
            raise p.Error('missing') from None
        doc = p.parse(manifest)
        p.require(type(doc.get('identity')) is dict and p.digest(p.canonical(doc['identity'])) == key, 'corrupt')
        checked = {'identity': doc['identity'], 'manifest_sha256': p.digest(manifest),
                   'payload_sha256': doc.get('sha256'), 'size': doc.get('size')}
        p.validate_manifest(manifest, payload, checked)
        return manifest, payload

    @staticmethod
    def _persist(path, raw):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())

    @staticmethod
    def _sync_dir(path):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def admit(self, grant, *, deadline):
        with lock(self.ledger / 'store.lock', deadline):
            p.grant(p.canonical(grant), client=grant['client'])
            self._claim(grant)

    def handle(self, grant, manifest=b'', payload=b'', *, deadline):
        """Apply an already-admitted invocation; gateway admits before reading a body."""
        with lock(self.ledger / 'store.lock', deadline):
            p.grant(p.canonical(grant), client=grant['client'])
            if grant['operation'] == 'list':
                return self._list(grant, deadline), b'', b''
            key = p.digest(p.canonical(grant['identity']))
            target = self.objects / key
            if grant['operation'] == 'read':
                found_manifest, found_payload = self._read(key)
                p.validate_manifest(found_manifest, found_payload, grant)
                return {'identity': grant['identity'], 'manifest_sha256': p.digest(found_manifest)}, found_manifest, found_payload
            p.require(grant['operation'] == 'publish', 'authorization')
            p.validate_manifest(manifest, payload, grant)
            if os.path.lexists(target):
                found_manifest, found_payload = self._read(key)
                p.require(found_manifest == manifest and found_payload == payload, 'conflict')
            else:
                reservation = len(manifest) + len(payload) + 16384
                p.require(self._usage() + reservation <= self.capacity, 'quota')
                p.require(shutil.disk_usage(self.root).free >= self.free_floor + reservation, 'space')
                with tempfile.TemporaryDirectory(prefix='pending-', dir=self.staging) as temp:
                    staged = Path(temp)
                    self._persist(staged / 'payload', payload)
                    self._persist(staged / 'manifest.json', manifest)
                    self._sync_dir(staged)
                    deadline.remaining()
                    p.grant(p.canonical(grant), client=grant['client'])
                    p.require(self._usage() <= self.capacity, 'quota')
                    os.rename(staged, target)
                    self._sync_dir(self.objects)
            return {'identity': grant['identity'], 'manifest_sha256': p.digest(manifest)}, b'', b''

    def _list(self, grant, deadline):
        names = sorted(self.objects.iterdir())
        p.require(len(names) <= 16, 'corrupt')
        found = []
        for path in names:
            deadline.remaining()
            p.require(re.fullmatch('[0-9a-f]{64}', path.name), 'corrupt')
            manifest, _ = self._read(path.name)
            doc = p.parse(manifest)
            run = {k: doc['identity'][k] for k in p.RUN_FIELDS}
            if run == grant['identity']:
                found.append({'identity': doc['identity'], 'manifest_sha256': p.digest(manifest),
                              'payload_sha256': doc['sha256'], 'size': doc['size']})
        return {'objects': found}
