"""Transport-independent validation of internal standard part bundles.

No backend is enabled by importing this module. References must come from a
trusted run descriptor: hashes check integrity, not producer authorization or
atomic publication. Only standard outcome/summary bundles are supported here;
plan files continue to use flow's existing safe reader when integrated later.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import io
import json
import re
import stat
import zipfile
import zlib
from collections.abc import Mapping

from ci_runners import artifacts

MAX_MANIFEST = 1 << 20
PART_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z')
REPOSITORY = re.compile(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}\Z')
SHA = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')
DIGEST = re.compile(r'[0-9a-f]{64}\Z')
FILES = frozenset({'outcome.json', 'summary.md'})


class BundleError(ValueError):
    """A terminal validation error, with a stable diagnostic class."""

    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(f'{code}: {detail}')


def _require(ok: bool, code: str, detail: str) -> None:
    if not ok:
        raise BundleError(code, detail)


def _positive(value: object) -> bool:
    return type(value) is int and value > 0


@dataclasses.dataclass(frozen=True)
class Identity:
    repository_id: int
    repository: str
    run_id: int
    run_attempt: int
    part_id: str
    part_attempt: int
    sha: str

    def __post_init__(self) -> None:
        for field in ('repository_id', 'run_id', 'run_attempt', 'part_attempt'):
            _require(_positive(getattr(self, field)), 'identity', f'{field} must be a positive integer')
        _require(self.part_attempt <= artifacts.MAX_ATTEMPT, 'identity', 'part attempt exceeds bound')
        _require(isinstance(self.repository, str) and bool(REPOSITORY.fullmatch(self.repository))
                 and self.repository.split('/')[-1] not in ('.', '..'), 'identity', 'invalid repository')
        _require(isinstance(self.part_id, str) and bool(PART_ID.fullmatch(self.part_id))
                 and self.part_id != 'plan', 'identity', 'invalid or reserved part id')
        _require(isinstance(self.sha, str) and bool(SHA.fullmatch(self.sha)), 'identity', 'invalid commit SHA')


@dataclasses.dataclass(frozen=True)
class Reference:
    identity: Identity
    manifest_sha256: str

    def __post_init__(self) -> None:
        _require(isinstance(self.identity, Identity), 'identity', 'reference needs an identity')
        _require(_digest(self.manifest_sha256), 'format', 'invalid manifest digest')


def _digest(value: object) -> bool:
    return isinstance(value, str) and bool(DIGEST.fullmatch(value))


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json(data: bytes) -> dict:
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError('duplicate key')
            out[key] = value
        return out

    def constant(_value):
        raise ValueError('non-finite number')

    try:
        value = json.loads(data.decode('utf-8'), object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeError, ValueError, RecursionError):
        raise BundleError('format', 'invalid JSON object') from None
    _require(type(value) is dict, 'format', 'expected a JSON object')
    return value


def _utc(value: object) -> dt.datetime:
    _require(isinstance(value, str), 'format', 'expected UTC timestamp')
    try:
        parsed = dt.datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ')
    except ValueError:
        raise BundleError('format', 'invalid UTC timestamp') from None
    _require(parsed.strftime('%Y-%m-%dT%H:%M:%SZ') == value, 'format', 'noncanonical UTC timestamp')
    return parsed.replace(tzinfo=dt.timezone.utc)


def _manifest(reference: Reference, raw: bytes) -> dict:
    _require(len(raw) <= MAX_MANIFEST, 'size', 'manifest exceeds bound')
    _require(_hash(raw) == reference.manifest_sha256, 'digest', 'manifest digest mismatch')
    doc = _json(raw)
    fields = {'schema', 'backend', 'identity', 'kind', 'producer', 'created_at',
              'expires_at', 'status', 'size', 'sha256', 'files'}
    _require(set(doc) == fields and type(doc.get('schema')) is int and doc['schema'] == 1,
             'format', 'unsupported manifest schema or fields')
    _require(doc['backend'] == 'internal' and doc['kind'] == 'part', 'format', 'unsupported backend or kind')
    _require(doc['status'] == 'complete', 'incomplete', 'bundle is not complete')
    identity = doc['identity']
    _require(type(identity) is dict and set(identity) == {f.name for f in dataclasses.fields(Identity)},
             'identity', 'invalid identity fields')
    actual = Identity(**identity)
    _require(actual == reference.identity, 'identity', 'bundle identity mismatch')
    _require(isinstance(doc['producer'], str) and bool(PART_ID.fullmatch(doc['producer'])),
             'format', 'invalid producer')
    _require(_utc(doc['expires_at']) > _utc(doc['created_at']), 'format', 'expiry must follow creation')
    _require(_positive(doc['size']) and doc['size'] <= artifacts.MAX_ZIP,
             'size', 'bundle size exceeds bound')
    _require(_digest(doc['sha256']), 'format', 'invalid bundle digest')
    files = doc['files']
    _require(type(files) is list and 1 <= len(files) <= len(FILES), 'format', 'invalid file list')
    names = set()
    for file in files:
        _require(type(file) is dict and set(file) == {'name', 'size', 'sha256'},
                 'format', 'invalid file entry')
        name = file['name']
        _require(isinstance(name, str) and name in FILES and name not in names,
                 'archive', 'unexpected or duplicate filename')
        names.add(name)
        _require(type(file['size']) is int and 0 <= file['size'] <= artifacts.MAX_FILE,
                 'size', 'file size exceeds bound')
        _require(_digest(file['sha256']), 'format', 'invalid file digest')
    _require('outcome.json' in names, 'format', 'outcome.json is required')
    return doc


def read_part(reference: Reference, manifest: bytes, payload: bytes) -> tuple[dict, str]:
    """Validate the exact committed reference and reuse artifacts.members.

    No filesystem extraction and no search for an older object. Callers must
    bound network reads before buffering and enforce authorization, completion
    publication, run-descriptor selection, deadlines and retries separately.
    Expiry is a cleanup horizon, not a refusal of a still-pinned exact reference.
    """
    doc = _manifest(reference, manifest)
    _require(len(payload) <= artifacts.MAX_ZIP and len(payload) == doc['size'],
             'size', 'bundle byte count mismatch')
    _require(_hash(payload) == doc['sha256'], 'digest', 'bundle digest mismatch')
    expected = {f['name']: f for f in doc['files']}
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            infos = archive.infolist()
            _require(len(infos) == len(expected) and {i.filename for i in infos} == set(expected),
                     'archive', 'archive file set mismatch')
            for info in infos:
                kind = stat.S_IFMT(info.external_attr >> 16)
                _require(info.orig_filename == info.filename and not info.is_dir()
                         and kind in (0, stat.S_IFREG) and not info.flag_bits & 1,
                         'archive', 'only unencrypted regular files are allowed')
                file = expected[info.filename]
                _require(info.file_size == file['size'], 'size', 'archive declared size mismatch')
                # Read one extra byte to detect a larger stream; ZipFile checks CRC.
                with archive.open(info) as stream:
                    raw = stream.read(artifacts.MAX_FILE + 1)
                _require(len(raw) == file['size'], 'size', 'file byte count mismatch')
                _require(_hash(raw) == file['sha256'], 'digest', 'file digest mismatch')
                try:
                    raw.decode('utf-8')
                except UnicodeError:
                    raise BundleError('format', 'file is not UTF-8') from None
                if info.filename == 'outcome.json':
                    outcome = _json(raw)
                    _require(type(outcome.get('run-attempt')) is int
                             and outcome['run-attempt'] == reference.identity.run_attempt,
                             'identity', 'outcome run attempt mismatch')
        got = artifacts.members(f'ci-part-{reference.identity.part_id}-{reference.identity.part_attempt}', payload)
    except BundleError:
        raise
    except (OSError, ValueError, EOFError, NotImplementedError, RuntimeError, zipfile.BadZipFile, zlib.error):
        raise BundleError('archive', 'bundle archive cannot be read') from None
    _require(got is not None, 'format', 'outcome.json has no valid outcome')
    return got


def build_part(identity: Identity, files: Mapping[str, bytes], *, producer: str,
               created_at: str, expires_at: str) -> tuple[Reference, bytes, bytes]:
    """Prepare deterministic bytes for a future publisher; publish nothing.

    Repeated calls with identical input produce the same reference and bytes.
    Input files are byte snapshots; the later publisher must not reread changed
    paths after computing these hashes. Unsupported report/plan files fail closed.
    """
    snapshots = dict(files)
    _require(1 <= len(snapshots) <= len(FILES) and 'outcome.json' in snapshots
             and all(isinstance(name, str) and name in FILES for name in snapshots),
             'archive', 'unexpected file set')
    _require(all(isinstance(value, bytes) and len(value) <= artifacts.MAX_FILE
                 for value in snapshots.values()), 'size', 'invalid file bytes or size')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, raw in sorted(snapshots.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, raw)
    payload = buf.getvalue()
    doc = {'schema': 1, 'backend': 'internal', 'identity': dataclasses.asdict(identity),
           'kind': 'part', 'producer': producer, 'created_at': created_at,
           'expires_at': expires_at, 'status': 'complete', 'size': len(payload),
           'sha256': _hash(payload), 'files': [
               {'name': name, 'size': len(raw), 'sha256': _hash(raw)}
               for name, raw in sorted(snapshots.items())]}
    manifest = json.dumps(doc, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    reference = Reference(identity, _hash(manifest))
    read_part(reference, manifest, payload)
    return reference, manifest, payload
