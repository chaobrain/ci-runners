"""Synthetic-only P1 framing and signed-scope validation; no CI backend activation."""
from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import os
import re
import select
import struct
import time

from ci_runners import bundles

HEADER_MAX = 65536
MANIFEST_MAX = 1 << 20
PAYLOAD_MAX = 66 << 20
GRANT_MAX = 16384
PREFIX = struct.Struct('>4sIIQ')
MAGIC = b'CISP'
AUDIENCE = 'ci-storage-pilot-v1'
NAMESPACE = 'ci-storage-capability-v1'
SIGNER = 'ci-storage-p1-issuer'
KEY_ID = 'pilot-issuer-v1'
RUN_FIELDS = {'repository_id', 'repository', 'run_id', 'run_attempt', 'sha'}
FULL_FIELDS = {f.name for f in dataclasses.fields(bundles.Identity)}
REPOSITORIES = {(910001, 'pilot-a/probe'), (910002, 'pilot-b/probe')}


class Error(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def require(condition, code='format'):
    if not condition:
        raise Error(code)


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def canonical(doc) -> bytes:
    try:
        return json.dumps(doc, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                          allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, RecursionError):
        raise Error('format') from None


def parse(raw: bytes) -> dict:
    try:
        return bundles._json(raw)
    except bundles.BundleError:
        raise Error('format') from None


def integer(value, lower, upper):
    return type(value) is int and lower <= value <= upper


def is_digest(value):
    return isinstance(value, str) and bool(bundles.DIGEST.fullmatch(value))


def scope(value, *, listing=False):
    require(type(value) is dict and set(value) == (RUN_FIELDS if listing else FULL_FIELDS), 'identity')
    full = {**value, **({'part_id': 'probe', 'part_attempt': 1} if listing else {})}
    try:
        bundles.Identity(**full)
    except (TypeError, bundles.BundleError):
        raise Error('identity') from None
    require((full['repository_id'], full['repository']) in REPOSITORIES
            and full['run_id'] in (920001, 920002) and full['run_attempt'] in (1, 2)
            and full['part_id'] == 'probe' and full['part_attempt'] in (1, 2)
            and full['sha'] == '0' * 40, 'identity')
    return dict(value)


def grant(raw: bytes, *, client: str, now: int | None = None):
    require(0 < len(raw) <= GRANT_MAX)
    doc = parse(raw)
    require(canonical(doc) == raw)
    fields = {'schema', 'audience', 'key_id', 'jti', 'nbf', 'exp', 'operation', 'client',
              'identity', 'manifest_sha256', 'payload_sha256', 'size'}
    require(set(doc) == fields and type(doc['schema']) is int and doc['schema'] == 1)
    require(doc['audience'] == AUDIENCE and doc['key_id'] == KEY_ID, 'authorization')
    require(isinstance(doc['jti'], str) and re.fullmatch('[0-9a-f]{32}', doc['jti']), 'authorization')
    require(isinstance(client, str) and client.startswith('SHA256:') and doc['client'] == client, 'authorization')
    require(integer(doc['nbf'], 1, 2**63 - 1) and integer(doc['exp'], 1, 2**63 - 1)
            and 0 < doc['exp'] - doc['nbf'] <= 600, 'authorization')
    now = int(time.time()) if now is None else now
    require(doc['nbf'] <= now + 30 and now < doc['exp'], 'expired')
    require(doc['operation'] in ('publish', 'read', 'list'), 'authorization')
    listing = doc['operation'] == 'list'
    scope(doc['identity'], listing=listing)
    if listing:
        require(doc['manifest_sha256'] == '' and doc['payload_sha256'] == ''
                and type(doc['size']) is int and doc['size'] == 0)
    else:
        require(is_digest(doc['manifest_sha256']) and is_digest(doc['payload_sha256'])
                and integer(doc['size'], 1, PAYLOAD_MAX))
    return doc


def b64(raw):
    return base64.b64encode(raw).decode('ascii')


def unb64(text, limit=GRANT_MAX):
    require(isinstance(text, str) and len(text) <= ((limit + 2) // 3) * 4)
    try:
        raw = base64.b64decode(text, validate=True)
    except (ValueError, UnicodeError):
        raise Error('format') from None
    require(len(raw) <= limit and b64(raw) == text)
    return raw


def request(header):
    require(set(header) == {'schema', 'operation', 'grant', 'signature'}
            and type(header['schema']) is int and header['schema'] == 1)
    raw, signature = unb64(header['grant']), unb64(header['signature'])
    require(bool(signature), 'signature')
    return raw, signature


def validate_manifest(raw, payload, authorization):
    require(0 < len(raw) <= MANIFEST_MAX and digest(raw) == authorization['manifest_sha256'], 'digest')
    doc = parse(raw)
    require(set(doc) == {'schema', 'kind', 'status', 'identity', 'size', 'sha256'}
            and type(doc['schema']) is int and doc['schema'] == 1)
    require(doc['kind'] == 'probe' and doc['status'] == 'complete', 'incomplete')
    scope(doc['identity'])
    require(doc['identity'] == authorization['identity'], 'identity')
    require(integer(doc['size'], 1, PAYLOAD_MAX) and doc['size'] == authorization['size']
            and len(payload) == doc['size'], 'size')
    require(doc['sha256'] == authorization['payload_sha256'] == digest(payload), 'digest')
    return doc


def make_manifest(identity, payload):
    scope(identity)
    require(integer(len(payload), 1, PAYLOAD_MAX), 'size')
    return canonical({'schema': 1, 'kind': 'probe', 'status': 'complete', 'identity': identity,
                      'size': len(payload), 'sha256': digest(payload)})


class Deadline:
    def __init__(self, total=300, idle=45):
        self.end = time.monotonic() + total
        self.idle = idle

    def remaining(self):
        left = self.end - time.monotonic()
        require(left > 0, 'timeout')
        return left

    def ready(self, fd, *, writing=False):
        timeout = min(self.remaining(), self.idle)
        r, w, _ = select.select([] if writing else [fd], [fd] if writing else [], [], timeout)
        require(bool(w if writing else r), 'timeout')

    def read(self, fd, size):
        chunks = []
        while size:
            self.ready(fd)
            raw = os.read(fd, min(size, 65536))
            require(bool(raw), 'truncated')
            chunks.append(raw)
            size -= len(raw)
        return b''.join(chunks)

    def write(self, fd, raw):
        view = memoryview(raw)
        while view:
            self.ready(fd, writing=True)
            try:
                count = os.write(fd, view[:65536])
            except BlockingIOError:
                continue
            require(count > 0, 'transport')
            view = view[count:]

    def eof(self, fd):
        self.ready(fd)
        require(os.read(fd, 1) == b'', 'trailing')


def read_head(fd, deadline):
    magic, head, manifest, payload = PREFIX.unpack(deadline.read(fd, PREFIX.size))
    require(magic == MAGIC and 0 < head <= HEADER_MAX and manifest <= MANIFEST_MAX
            and payload <= PAYLOAD_MAX, 'framing')
    return parse(deadline.read(fd, head)), manifest, payload


def read_body(fd, deadline, manifest_size, payload_size):
    manifest = deadline.read(fd, manifest_size)
    payload = deadline.read(fd, payload_size)
    deadline.eof(fd)
    return manifest, payload


def write_frame(fd, deadline, header, manifest=b'', payload=b''):
    raw = canonical(header)
    require(0 < len(raw) <= HEADER_MAX and len(manifest) <= MANIFEST_MAX
            and len(payload) <= PAYLOAD_MAX, 'size')
    deadline.write(fd, PREFIX.pack(MAGIC, len(raw), len(manifest), len(payload)))
    for part in (raw, manifest, payload):
        deadline.write(fd, part)
