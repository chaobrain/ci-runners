"""Protected configuration and OpenSSH capability signatures for the P1 pilot."""
from __future__ import annotations

import base64
import os
from pathlib import Path
import shlex
import stat
import subprocess
import tempfile

from ci_runners import storage_protocol as p


def protected(path, *, owner=0, private=False, directory=False):
    path = Path(path)
    p.require(path.is_absolute(), 'configuration')
    for parent in reversed(path.parents):
        info = parent.lstat()
        sticky_root = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
        p.require(stat.S_ISDIR(info.st_mode) and info.st_uid in (0, owner)
                  and (not info.st_mode & 0o022 or sticky_root), 'permissions')
    info = path.lstat()
    p.require(info.st_uid == owner and not info.st_mode & (0o077 if private else 0o022), 'permissions')
    p.require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
              and info.st_nlink == 1, 'permissions')
    return path


def read_file(path, *, owner=0, private=False, limit=p.MANIFEST_MAX):
    protected(path, owner=owner, private=private)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as file:
        info = os.fstat(file.fileno())
        p.require(stat.S_ISREG(info.st_mode) and info.st_uid == owner and info.st_nlink == 1
                  and not info.st_mode & (0o077 if private else 0o022), 'permissions')
        raw = file.read(limit + 1)
    p.require(len(raw) <= limit, 'size')
    return raw


def config(path, fields):
    doc = p.parse(read_file(path))
    p.require(set(doc) == fields | {'schema', 'uid'} and type(doc['schema']) is int and doc['schema'] == 1,
              'configuration')
    p.require(p.integer(doc['uid'], 1, 2**31 - 1) and doc['uid'] == os.getuid(), 'permissions')
    return doc


def client_fingerprint(authorized_keys):
    try:
        lines = [line for line in read_file(authorized_keys, limit=16384).decode('ascii').splitlines()
                 if line.strip() and not line.lstrip().startswith('#')]
        p.require(len(lines) == 1, 'configuration')
        words = shlex.split(lines[0])
        p.require(len(words) in (3, 4) and words[0].startswith('restrict,')
                  and 'command=' in words[0] and words[1] == 'ssh-ed25519', 'configuration')
        options = words[0].split(',')
        p.require(len(options) == 3 and options[0] == 'restrict'
                  and {option.split('=', 1)[0] for option in options[1:]} == {'from', 'command'}, 'configuration')
        raw = base64.b64decode(words[2], validate=True)
    except (ValueError, UnicodeError):
        raise p.Error('configuration') from None
    # SSH Ed25519 blob: string algorithm, string 32-byte key.
    p.require(len(raw) == 51 and raw[:19] == b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20', 'configuration')
    return 'SHA256:' + base64.b64encode(bytes.fromhex(p.digest(raw))).decode().rstrip('=')


def _run(args, raw, deadline):
    try:
        result = subprocess.run(args, input=raw, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=min(45, deadline.remaining()), env={'PATH': '/usr/bin:/bin'})
    except subprocess.TimeoutExpired:
        raise p.Error('timeout') from None
    p.require(result.returncode == 0, 'signature')
    p.require(0 < len(result.stdout) <= p.GRANT_MAX, 'signature')
    return result.stdout


def sign(raw, key, *, client, deadline=None):
    deadline = deadline or p.Deadline()
    p.grant(raw, client=client)
    protected(key, owner=os.getuid(), private=True)
    return _run(['/usr/bin/ssh-keygen', '-Y', 'sign', '-n', p.NAMESPACE, '-f', str(key)], raw, deadline)


def verify(raw, signature, allowed_signers, *, deadline):
    p.require(0 < len(signature) <= p.GRANT_MAX and 0 < len(allowed_signers) <= 16384, 'signature')
    # Never trust client-inherited TMPDIR/TEMP or tempfile's environment-derived cache.
    info = Path('/tmp').lstat()
    p.require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0
              and (not info.st_mode & 0o022 or info.st_mode & stat.S_ISVTX), 'permissions')
    with tempfile.TemporaryDirectory(prefix='ci-storage-verify-', dir='/tmp') as temp:
        path = Path(temp) / 'signature'
        path.write_bytes(signature)
        trust = Path(temp) / 'allowed_signers'
        trust.write_bytes(allowed_signers)
        _run(['/usr/bin/ssh-keygen', '-Y', 'verify', '-n', p.NAMESPACE, '-I', p.SIGNER,
              '-f', str(trust), '-s', str(path)], raw, deadline)


def authorize(header, *, client, allowed_signers, deadline):
    raw, signature = p.request(header)
    verify(raw, signature, allowed_signers, deadline=deadline)
    doc = p.grant(raw, client=client)
    p.require(header['operation'] == doc['operation'], 'authorization')
    return doc
