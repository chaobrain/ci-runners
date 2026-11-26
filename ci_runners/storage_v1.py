"""Paired schema 1 file transport; execution and status policy stay with v1.

Every internal object belongs to one verified workflow source and run attempt.
Only explicitly pre-cutover runs use GitHub history. Storage failures never
select another backend. This module has no scheduler or environment builder.
"""
from __future__ import annotations

import argparse
import fnmatch
import glob
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import zipfile

from ci_runners import storage

META = '.ci-storage-source.json'
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')
SHA = re.compile(r'[0-9a-f]{40}\Z')


def source_run(run):
    try:
        out = {key: run[key] for key in ('repository_id', 'repository', 'id', 'run_attempt', 'head_sha')}
        if (any(type(out[k]) is not int or out[k] <= 0 for k in ('repository_id', 'id', 'run_attempt'))
                or out['run_attempt'] > 99 or not isinstance(out['head_sha'], str)
                or not SHA.fullmatch(out['head_sha']) or not storage.REPO.fullmatch(out['repository'])):
            raise storage.Error('identity')
        return out
    except (KeyError, TypeError) as exc:
        raise storage.Error('identity') from exc


def current_run(env):
    """Verify the private event before opening credentials or the internal store."""
    try:
        repo = env['GITHUB_REPOSITORY'].lower()
        event_path = Path(env['GITHUB_EVENT_PATH'])
        if event_path.stat().st_size > 16 << 20:
            raise storage.Error('limit')
        event = json.loads(event_path.read_text(encoding='utf-8'))
        info = event['repository']
        pr = event.get('pull_request', {})
        source_repo = pr.get('head', {}).get('repo', {}).get('full_name', repo).lower()
        followed = event.get('workflow_run', {}).get('head_repository', {}).get('full_name', repo).lower()
        if (not storage.REPO.fullmatch(repo) or info.get('private') is not True
                or info.get('full_name', '').lower() != repo
                or str(info.get('id')) != env['GITHUB_REPOSITORY_ID']
                or source_repo != repo or followed != repo
                or env.get('GITHUB_EVENT_NAME') == 'pull_request_target'):
            raise storage.Error('identity')
        sha = pr.get('head', {}).get('sha') or env['GITHUB_SHA']
        run = dict(repository_id=int(env['GITHUB_REPOSITORY_ID']), repository=repo,
                   id=int(env['GITHUB_RUN_ID']), run_attempt=int(env.get('GITHUB_RUN_ATTEMPT', '1')),
                   head_sha=sha)
        if (not SHA.fullmatch(sha) or min(run['repository_id'], run['id'], run['run_attempt']) <= 0
                or run['run_attempt'] > 99):
            raise storage.Error('identity')
        return run
    except (KeyError, TypeError, ValueError, OSError, AttributeError) as exc:
        raise storage.Error('identity') from exc


def open_current(etc, env=None):
    """None means the explicitly configured GitHub route, never a failed SSH route."""
    env = os.environ if env is None else env
    cfg = storage.read_config(etc)
    repo = env.get('GITHUB_REPOSITORY', '').lower()
    cutover = cfg.get('schema1_cutover', {}).get(repo)
    required = env.get('CI_STORAGE_STORE', '')
    if cutover is None:
        if required:
            raise storage.Error('configuration')
        return None
    run = current_run(env)
    if run['id'] < cutover:
        if required:
            raise storage.Error('configuration')
        return None
    bound = storage.select(cfg, repo)
    if bound['backend'] != 'ssh' or required and bound['store'] != required:
        raise storage.Error('configuration')
    return Bundles(storage.client(etc, bound), bound['store'], run, cutover)


def no_links(path):
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise storage.Error('corrupt')
    return path


class Bundles:
    def __init__(self, client, store, run, cutover):
        self.client, self.store, self.run, self.cutover = client, store, source_run(run), cutover
        self.limits = client.limits

    def identity(self, name, run=None, attempt=None):
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise storage.Error('identity')
        run = self.run if run is None else source_run(run)
        return storage.identity(dict(repository_id=run['repository_id'], repository=run['repository'],
            run_id=run['id'], run_attempt=attempt or run['run_attempt'], sha=run['head_sha'],
            context='ci/files', kind='project', part_id='files', part_attempt=1, bundle_name=name))

    @storage.safe_errors
    def bind(self):
        if self.run['id'] < self.cutover:
            raise storage.Error('identity')
        ident = self.identity('run-backend')
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source'
            source.mkdir()
            (source / 'binding.json').write_bytes(storage.dumps({
                'schema': 1, 'backend': 'ssh', 'store': self.store, 'identity': ident}))
            return self.client.publish_bundle(ident, source, 'run')

    @storage.safe_errors
    def references(self, run=None, history=True, allow_empty=False):
        run = self.run if run is None else source_run(run)
        if (run['repository_id'] != self.run['repository_id'] or run['repository'] != self.run['repository']):
            raise storage.Error('identity')
        if run['id'] < self.cutover:
            return None
        if type(run['run_attempt']) is not int or not 1 <= run['run_attempt'] <= 99:
            raise storage.Error('identity')
        found, total = {}, 0
        attempts = range(1, run['run_attempt'] + 1) if history else [run['run_attempt']]
        current_binding = False
        for attempt in attempts:
            cursor, seen, refs, bound = '', set(), {}, None
            while True:
                page = self.client.list_completed({
                    'repository_id': run['repository_id'], 'repository': run['repository'],
                    'run_id': run['id'], 'run_attempt': attempt}, cursor)
                if not isinstance(page, dict) or not isinstance(page.get('references'), list):
                    raise storage.Error('corrupt')
                total += len(page['references'])
                if total > self.limits['manifests_per_run']:
                    raise storage.Error('limit')
                for ref in page['references']:
                    storage.reference(ref, store=self.store)
                    ident = ref['identity']
                    storage.reference(ref, self.identity(ident['bundle_name'], run, attempt), self.store)
                    if ident['bundle_name'] == 'run-backend':
                        bound = ref
                    elif ident['bundle_name'] in refs:
                        raise storage.Error('conflict')
                    else:
                        refs[ident['bundle_name']] = ref
                nxt = page.get('next')
                if not isinstance(nxt, str):
                    raise storage.Error('corrupt')
                if not nxt:
                    break
                if nxt in seen:
                    raise storage.Error('limit')
                seen.add(nxt)
                cursor = nxt
            if refs and bound is None:
                raise storage.Error('missing')
            if bound is not None:
                ident = self.identity('run-backend', run, attempt)
                with tempfile.TemporaryDirectory() as directory:
                    dest = Path(directory) / 'binding'
                    self.client.read_bundle(bound, ident, dest)
                    expected = {'schema': 1, 'backend': 'ssh', 'store': self.store, 'identity': ident}
                    if json.loads((dest / 'binding.json').read_bytes()) != expected:
                        raise storage.Error('identity')
                if attempt == run['run_attempt']:
                    current_binding = True
            # An executed optional upload that found no files must shadow its old
            # attempt. A skipped job, in contrast, may reuse its earlier evidence.
            for name in list(refs):
                if not name.startswith('ci-absent-'):
                    continue
                ref = refs.pop(name)
                with tempfile.TemporaryDirectory() as directory:
                    dest = Path(directory) / 'absence'
                    self.read(ref, dest, run)
                    doc = json.loads((dest / 'absence.json').read_bytes())
                    missing = doc.get('name') if isinstance(doc, dict) else None
                    if not isinstance(missing, str) or not NAME.fullmatch(missing) or name != self.absence_name(missing):
                        raise storage.Error('identity')
                    if missing in refs:
                        raise storage.Error('conflict')
                    found.pop(missing, None)
            found.update(refs)
        if not current_binding and not (allow_empty and total == 0):
            raise storage.Error('missing')
        return found

    def prepare_download(self):
        """Bind a fresh current attempt before it reads files kept from a rerun.

        Existing current-attempt objects must already have their valid binding;
        a read must not repair missing identity evidence around those objects.
        """
        if not self.references(history=False, allow_empty=True):
            self.bind()

    @staticmethod
    def absence_name(name):
        return 'ci-absent-' + storage.digest(name.encode())

    def publish_absence(self, name):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'absence'
            source.mkdir()
            (source / 'absence.json').write_bytes(storage.dumps({'name': name}))
            (source / META).write_bytes(storage.dumps({'source': self.run, 'producer_checkout_sha': None}))
            self.client.publish_bundle(self.identity(self.absence_name(name)), source, 'run')

    @storage.safe_errors
    def publish(self, name, patterns, missing='error', hidden=False, retention='diagnostic'):
        deadline = time.monotonic() + self.limits['operation_seconds']
        if missing not in ('error', 'warn', 'ignore') or name == 'run-backend' or name.startswith('ci-absent-'):
            raise storage.Error('configuration')
        self.identity(name)
        self.bind()
        selected, exclude = [], []
        for pattern in patterns.splitlines():
            pattern = os.path.expandvars(os.path.expanduser(pattern.strip()))
            if not pattern:
                continue
            if pattern.startswith('!'):
                exclude.append(str(Path(pattern[1:]).absolute()))
                continue
            for match in glob.iglob(pattern, recursive=True, include_hidden=hidden):
                storage.check_deadline(deadline)
                selected.append(no_links(match))
                if len(selected) > self.limits['entries']:
                    raise storage.Error('limit')
        if not selected:
            if missing in ('warn', 'ignore'):
                self.publish_absence(name)
                return None
            raise storage.Error('missing')
        common = Path(os.path.commonpath([str(p if p.is_dir() else p.parent) for p in selected]))
        members, total, examined = {}, 0, 0
        for entry in selected:
            for path in ([entry] if entry.is_file() else entry.rglob('*')):
                storage.check_deadline(deadline)
                examined += 1
                if examined > self.limits['entries']:
                    raise storage.Error('limit')
                no_links(path)
                mode = path.lstat().st_mode
                if not stat.S_ISREG(mode):
                    if stat.S_ISDIR(mode):
                        continue
                    raise storage.Error('corrupt')
                rel = path.relative_to(common)
                if (not hidden and any(p.startswith('.') for p in rel.parts)
                        or any(fnmatch.fnmatchcase(str(path), p) for p in exclude)):
                    continue
                if rel.as_posix() == META:
                    raise storage.Error('conflict')
                if rel not in members:
                    members[rel] = path
                    total += path.stat().st_size
                if len(members) >= self.limits['entries'] or total > self.limits['unpacked_bytes']:
                    raise storage.Error('limit')
        if not members:
            if missing in ('warn', 'ignore'):
                self.publish_absence(name)
                return None
            raise storage.Error('missing')
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source'
            source.mkdir()
            for rel, path in members.items():
                dest = source / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                # Copy only bounded, regular files. The codec verifies the staged copy again.
                fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
                with os.fdopen(fd, 'rb') as stream:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                        raise storage.Error('corrupt')
                    with dest.open('xb') as out:
                        left = self.limits['unpacked_bytes'] - sum(p.stat().st_size for p in source.rglob('*') if p.is_file())
                        while block := stream.read(min(1 << 20, left + 1)):
                            storage.check_deadline(deadline)
                            left -= len(block)
                            if left < 0:
                                raise storage.Error('limit')
                            out.write(block)
            checkout = subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True, text=True)
            producer = checkout.stdout.strip() if checkout.returncode == 0 else None
            if producer is not None and not SHA.fullmatch(producer):
                raise storage.Error('identity')
            (source / META).write_bytes(storage.dumps({'source': self.run, 'producer_checkout_sha': producer}))
            return self.client.publish_bundle(self.identity(name), source, retention)

    @storage.safe_errors
    def read(self, ref, destination, run=None):
        run = self.run if run is None else source_run(run)
        ident = self.identity(ref['identity']['bundle_name'], run, ref['identity']['run_attempt'])
        storage.reference(ref, ident, self.store)
        self.client.read_bundle(ref, ident, destination)
        meta = json.loads((Path(destination) / META).read_bytes())
        if not isinstance(meta, dict):
            raise storage.Error('identity')
        expected = {**run, 'run_attempt': ident['run_attempt']}
        producer = meta.get('producer_checkout_sha')
        if meta.get('source') != expected or producer is not None and (
                not isinstance(producer, str) or not SHA.fullmatch(producer)):
            raise storage.Error('identity')

    @storage.safe_errors
    def download(self, destination, name='', pattern='', merge=False, run=None):
        run = self.run if run is None else source_run(run)
        refs = self.references(run)
        if refs is None:
            raise storage.Error('configuration')
        patterns = pattern.strip('{}').split(',') if pattern else []
        wanted = {n: r for n, r in refs.items()
                  if n == name or not name and any(fnmatch.fnmatchcase(n, p) for p in patterns)}
        if not wanted:
            raise storage.Error('missing')
        destination = no_links(destination)
        # Verify the complete selection and every destination before writing any file.
        with tempfile.TemporaryDirectory() as directory:
            staged = {}
            for index, (n, ref) in enumerate(sorted(wanted.items())):
                source = Path(directory) / str(index)
                self.read(ref, source, run)
                target = destination if name or merge else destination / n
                for path in source.rglob('*'):
                    if not path.is_file() or path.relative_to(source).as_posix() == META:
                        continue
                    dest = no_links(target / path.relative_to(source))
                    if dest.exists() and (not dest.is_file() or storage.file_digest(dest) != storage.file_digest(path)):
                        raise storage.Error('conflict')
                    if dest in staged and storage.file_digest(staged[dest]) != storage.file_digest(path):
                        raise storage.Error('conflict')
                    staged[dest] = path
            for dest, path in staged.items():
                dest.parent.mkdir(parents=True, exist_ok=True)
                no_links(dest)
                if not dest.exists():
                    with path.open('rb') as source, dest.open('xb') as out:
                        shutil.copyfileobj(source, out)
        return len(wanted)

    @storage.safe_errors
    def seal(self, pid, attempt, state, expected):
        if not NAME.fullmatch(pid) or type(attempt) is not int or not 1 <= attempt <= 99:
            raise storage.Error('identity')
        refs = self.references()
        if not isinstance(expected, list) or any(not isinstance(n, str) or not NAME.fullmatch(n) for n in expected):
            raise storage.Error('identity')
        missing = [n for n in expected if n not in refs or refs[n]['identity']['run_attempt'] != self.run['run_attempt']]
        if missing or state != 'success':
            raise storage.Error('missing')
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'receipt'
            source.mkdir()
            (source / 'receipt.json').write_bytes(storage.dumps({'part': pid, 'attempt': attempt, 'state': state,
                'references': {n: refs[n] for n in expected}}))
            return self.publish(f'ci-seal-{pid}-{attempt}', str(source))

    def read_outcomes(self, ids):
        from ci_runners import artifacts
        with tempfile.TemporaryDirectory() as directory:
            self.download(directory, pattern='ci-part-*')
            return {pid: attempts for pid, attempts in artifacts.collect(directory).items() if pid in ids}

    def old_passes(self, found):
        return [pid for pid, attempts in found.items() if attempts and
                (last := max(attempts.items())[1][0]).get('outcome') == 'pass' and
                last.get('run-attempt') != self.run['run_attempt']]

    def report_gap(self, found, jobs=None):
        # A rerun may retain evidence only for jobs GitHub confirms were skipped.
        # A failed current upload must never uncover the previous attempt's pass.
        for pid in self.old_passes(found):
            matching = [job for job in jobs or [] if isinstance(job, dict) and
                        str(job.get('name', '')).split(' / ')[-2:] in
                        (['part ' + pid, 'first'], ['part ' + pid, 'retry'])]
            if not matching or any(job.get('conclusion') != 'skipped' for job in matching):
                return 'current part publication missing'
        refs = self.references()
        for pid, attempts in found.items():
            n, (outcome, _) = max(attempts.items())
            if outcome.get('outcome') != 'pass':
                continue
            ref = refs.get(f'ci-seal-{pid}-{n}')
            if ref is None or ref['identity']['run_attempt'] != outcome.get('run-attempt'):
                return 'required project report receipt missing'
            with tempfile.TemporaryDirectory() as directory:
                dest = Path(directory) / 'receipt'
                self.read(ref, dest)
                doc = json.loads((dest / 'receipt.json').read_bytes())
                if not isinstance(doc, dict) or not isinstance(doc.get('references'), dict):
                    raise storage.Error('corrupt')
                if doc.get('part') != pid or doc.get('attempt') != n or doc.get('state') != 'success':
                    return 'project file publication incomplete'
                for name, expected in doc.get('references', {}).items():
                    storage.reference(expected, store=self.store)
                    if refs.get(name) != expected:
                        return 'project report evidence changed'
        return ''


class ReadError(RuntimeError):
    """Internal transport/integrity failure; never ordinary absent map history."""


class ArtifactApi:
    """Supply verified bundle bytes to existing workflow/ancestry selection code."""
    internal_files = True

    def __init__(self, api, bundles):
        if api.repo.lower() != bundles.run['repository']:
            raise storage.Error('identity')
        self.api, self.bundles, self.repo = api, bundles, api.repo
        self.objects, self.lock = {}, threading.RLock()

    def __getattr__(self, name):
        return getattr(self.api, name)

    def listing(self, run):
        run = {**run, 'repository_id': self.bundles.run['repository_id'], 'repository': self.repo.lower()}
        try:
            refs = self.bundles.references(run, allow_empty=run.get('status') == 'completed')
        except storage.Error as exc:
            raise ReadError('internal files: ' + exc.code) from None
        if refs is None:
            return self.api.pages(f'/repos/{self.repo}/actions/runs/{run["id"]}/artifacts', 'artifacts', limit=100)
        result = []
        for name, ref in refs.items():
            if name.startswith('ci-seal-'):
                continue
            aid = int(ref['manifest_sha256'][:15], 16) + (1 << 60)
            with self.lock:
                pair = (ref, source_run(run))
                old = self.objects.setdefault(aid, pair)
                if old != pair:
                    raise ReadError('internal files: identity collision')
            result.append(dict(id=aid, name=name, expired=False, created_at=run.get('created_at', ''),
                               workflow_run={k: run[k] for k in ('id', 'head_sha', 'head_branch') if k in run}))
        return result

    def json(self, method, path, body=None):
        parsed = urlparse(path)
        prefix = '/repos/' + re.escape(self.repo) + '/actions/'
        match = re.fullmatch(prefix + r'runs/([0-9]+)/artifacts', parsed.path)
        if method == 'GET' and match:
            rid = int(match[1])
            run = (self.bundles.run if rid == self.bundles.run['id'] else
                   self.api.json('GET', f'/repos/{self.repo}/actions/runs/{rid}'))
            items = self.listing(run)
            query = parse_qs(parsed.query)
            if 'name' in query:
                items = [a for a in items if a['name'] == query['name'][0]]
            size, page = int(query.get('per_page', ['100'])[0]), int(query.get('page', ['1'])[0])
            if not 1 <= size <= 100 or not 1 <= page <= 10000:
                raise ReadError('internal files: invalid page')
            return {'total_count': len(items), 'artifacts': items[(page - 1) * size:page * size]}
        if method == 'GET' and parsed.path == f'/repos/{self.repo}/actions/artifacts':
            # Existing named-list optimizations fall back to their authoritative workflow runs.
            raise ValueError('internal files require per-run history selection')
        return self.api.json(method, path, body)

    def pages(self, path, key=None, limit=3, per_page=100):
        out = []
        for page in range(1, limit + 1):
            doc = self.json('GET', path + ('&' if '?' in path else '?') + f'per_page={per_page}&page={page}')
            items = doc[key] if key else doc
            out.extend(items)
            if len(items) < per_page:
                break
        return out

    def download(self, path):
        match = re.fullmatch('/repos/' + re.escape(self.repo) + r'/actions/artifacts/([0-9]+)/zip', urlparse(path).path)
        aid = int(match[1]) if match else 0
        with self.lock:
            item = self.objects.get(aid)
        if item is None:
            if aid >= 1 << 60:
                raise ReadError('internal files: unlisted reference')
            return self.api.download(path)
        ref, run = item
        try:
            with tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / 'verified'
                self.bundles.read(ref, source, run)
                raw = io.BytesIO()
                with zipfile.ZipFile(raw, 'w', zipfile.ZIP_DEFLATED) as archive:
                    for file in source.rglob('*'):
                        if file.is_file() and file.relative_to(source).as_posix() != META:
                            archive.write(file, file.relative_to(source).as_posix())
                return raw.getvalue()
        except storage.Error as exc:
            raise ReadError('internal files: ' + exc.code) from None


def adapt_api(api, etc=None, env=None):
    env = os.environ if env is None else env
    etc = etc or env.get('CI_RUNNERS_ETC')
    if not etc:
        if env.get('CI_STORAGE_STORE'):
            raise ReadError('internal files: host configuration missing')
        return api
    try:
        bundles = open_current(etc, env)
        return ArtifactApi(api, bundles) if bundles is not None else api
    except storage.Error as exc:
        raise ReadError('internal files: ' + exc.code) from None


def output(env, **values):
    if env.get('GITHUB_OUTPUT'):
        with open(env['GITHUB_OUTPUT'], 'a', encoding='utf-8') as stream:
            for name, value in values.items():
                stream.write(f'{name}={value}\n')


def main(argv, etc, env=None):
    env = os.environ if env is None else env
    parser = argparse.ArgumentParser(prog='ci-run storage')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('mode')
    up = commands.add_parser('upload')
    up.add_argument('--name', required=True)
    up.add_argument('--path', required=True)
    up.add_argument('--missing', choices=('error', 'warn', 'ignore'), default='warn')
    up.add_argument('--hidden', choices=('true', 'false'), default='false')
    up.add_argument('--retention', choices=('run', 'diagnostic', 'mapping'), default='diagnostic')
    down = commands.add_parser('download')
    down.add_argument('--name', default='')
    down.add_argument('--pattern', default='*')
    down.add_argument('--path', required=True)
    down.add_argument('--merge', choices=('true', 'false'), default='false')
    seal = commands.add_parser('seal')
    seal.add_argument('--id', required=True)
    seal.add_argument('--attempt', type=int, required=True)
    seal.add_argument('--state', required=True)
    seal.add_argument('--expected', default='[]')
    seal.add_argument('--part', default='')
    args = parser.parse_args(argv)
    try:
        bundles = open_current(etc, env)
        if args.command == 'mode':
            output(env, backend='ssh' if bundles else 'github')
            return 0
        if bundles is None:
            if args.command == 'seal':
                return 0
            raise storage.Error('configuration')
        if args.command == 'upload':
            ref = bundles.publish(args.name, args.path, args.missing, args.hidden == 'true', args.retention)
            output(env, stored='true' if ref else 'false', **{
                'artifact-id': 'internal-' + ref['manifest_sha256'] if ref else ''})
        elif args.command == 'download':
            bundles.prepare_download()
            bundles.download(args.path, args.name, args.pattern, args.merge == 'true')
        else:
            expected = json.loads(args.expected)
            if isinstance(expected, list):
                selected = []
                for item in expected:
                    if isinstance(item, dict):
                        if item.get('outcome') == 'skipped' or item.get('optional') and item.get('stored') == 'false':
                            continue
                        item = item.get('name')
                    selected.append(item)
                expected = selected
            if args.part:
                part = json.loads(args.part)
                expected = [f'{a["name"]}-{args.id}-{args.attempt}' for a in part.get('artifacts', [])]
            bundles.seal(args.id, args.attempt, args.state, expected)
        return 0
    except (storage.Error, OSError, ValueError, KeyError, TypeError) as exc:
        code = exc.code if isinstance(exc, storage.Error) else 'corrupt'
        print('ci-run storage: ' + code + '; no backend fallback', file=sys.stderr)
        return 2
