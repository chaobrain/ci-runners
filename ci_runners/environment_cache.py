"""Pin a trusted YAML recipe on the controller and prepare its immutable local prefix."""
from __future__ import annotations

from contextlib import ExitStack
import base64
import binascii
import dataclasses
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import posixpath
import re
import shutil
import stat
import subprocess
import tempfile
import time
import urllib.parse

from ci_runners import envbuild, github, locks, registry

MANIFEST = '.github/ci-environment.yml'
# Leave room for the rest of the source, task and transport envelope.
MAX_RECIPE = 256 * 1024
MAX_MANIFEST = 64 * 1024
FORMAT = 1
BUILD_PATH = '/usr/local/bin:/usr/bin:/bin'
ERROR = envbuild.SpecError
_INCLUDE = re.compile(r'\s*(?:(-r|-c)\s*|(--requirement|--constraint)(?:\s+|=))([^\s#]+)\s*')
_BUILD_ENV = ('PATH', 'LANG', 'LC_ALL', 'TMPDIR', 'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS',
              'SSL_CERT_FILE', 'REQUESTS_CA_BUNDLE', 'CONDA_PKGS_DIRS', 'CONDA_OFFLINE', *envbuild.PROXY)


def _dump(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _repository(name):
    return isinstance(name, str) and registry.REPO.fullmatch(name) and '..' not in name and name.split('/')[1] != '.'


def _sha(value):
    return isinstance(value, str) and github.SHA.fullmatch(value)


def _yaml(text):
    # Only the controller needs a YAML parser. Execution hosts validate the JSON recipe.
    try:
        import yaml
    except ImportError as exc:
        raise ERROR('PyYAML is required on the controller to read ' + MANIFEST) from exc

    class Loader(yaml.SafeLoader):
        count = 0
        depth = 0

        def compose_node(self, parent, index):
            self.count += 1
            if self.count > 4096 or self.depth >= 24 or self.check_event(yaml.AliasEvent):
                raise ERROR(f'{MANIFEST}: YAML aliases, excessive nesting or excessive node count')
            self.depth += 1
            try:
                return super().compose_node(parent, index)
            finally:
                self.depth -= 1

        def construct_mapping(self, node, deep=False):
            keys = [self.construct_object(k, deep=True) for k, _ in node.value]
            if any(not isinstance(k, str) for k in keys) or len(keys) != len(set(keys)):
                raise ERROR(f'{MANIFEST}: expected distinct string keys')
            return super().construct_mapping(node, deep)

    try:
        return yaml.load(text, Loader=Loader)
    except (yaml.YAMLError, RecursionError) as exc:
        raise ERROR(f'{MANIFEST}: invalid YAML: {exc}') from None


def _spec(document):
    if not isinstance(document, dict) or type(document.get('schema')) is not int:
        raise ERROR(f'{MANIFEST}: expected a mapping with schema 1')
    # envbuild's TOML schema uses origin/<branch>. In YAML an upstream may also
    # declare an immutable commit directly, in which case no branch lookup is needed.
    ups = document.get('upstream')
    parsed = document
    if isinstance(ups, list):
        refs = [{**u, 'ref': 'origin/' + u['ref']} if isinstance(u, dict) and _sha(u.get('ref')) else u
                for u in ups]
        parsed = dict(document, upstream=refs)
    try:
        spec = envbuild.parse_document(parsed)
    except (envbuild.SpecError, TypeError, AttributeError) as exc:
        raise ERROR(str(exc).replace(envbuild.SPEC, MANIFEST)) from None
    if not all(_repository(u.repository) for u in spec.upstream):
        raise ERROR(f'{MANIFEST}: invalid upstream repository')
    if any(k.startswith(('GIT_', 'GH_', 'PIP_', 'CONDA_', 'SSH_')) for k in spec.variables):
        raise ERROR(f'{MANIFEST}: variables cannot override credential or installer settings')
    return spec


def _references(path, text):
    """pip include and constraint forms, including -rfile and continued lines."""
    for line in re.sub(r'\\\r?\n', '', text).splitlines():
        clean = re.sub(r'\s+#.*$', '', line).strip()
        if clean.startswith(('-r', '-c', '--requirement', '--constraint')):
            match = _INCLUDE.fullmatch(clean)
            if not match:
                raise ERROR(f'{path}: malformed requirement or constraint include')
            option = match.group(1) or match.group(2)
            target = posixpath.normpath(posixpath.join(posixpath.dirname(path), match.group(3)))
            yield envbuild._path(path, target), option in ('-c', '--constraint')


def _files(spec, read):
    files, active, constraints = {}, set(), []

    def visit(path):
        if path in active:
            raise ERROR(f'{path}: cyclic requirements include')
        if path in files:
            return
        if len(files) >= envbuild.MAX_FILES:
            raise ERROR(f'more than {envbuild.MAX_FILES} requirement files')
        text = read(path)
        if not isinstance(text, str) or '\0' in text or len(text.encode()) > MAX_RECIPE:
            raise ERROR(f'{path}: invalid requirement file')
        files[path] = text
        if sum(len(v.encode()) for v in files.values()) > MAX_RECIPE:
            raise ERROR('environment requirements too large')
        active.add(path)
        for child, constraint in _references(path, text):
            if constraint and child not in constraints:
                constraints.append(child)
            visit(child)
        active.remove(path)

    for name in spec.requirements:
        visit(name)
    return files, constraints


def _read(api, repository, revision, path, limit):
    data = api('GET', f'/repos/{repository}/contents/{urllib.parse.quote(path)}?ref={revision}')
    encoded = data.get('content') if isinstance(data, dict) else None
    if (not isinstance(encoded, str) or data.get('type') != 'file' or data.get('encoding') != 'base64'
            or len(encoded) > 2 * limit):
        raise ERROR(f'{path}: expected a bounded file at {revision[:12]}')
    try:
        raw = base64.b64decode(''.join(encoded.splitlines()), validate=True)
        if len(raw) > limit:
            raise ERROR(f'{path}: file too large')
        return raw.decode('utf-8')
    except (binascii.Error, UnicodeError) as exc:
        raise ERROR(f'{path}: invalid base64 or UTF-8') from exc


def resolve(api, repository, revision):
    """Read all repository files at one verified, trusted commit. No credentials are serialized."""
    if not _repository(repository) or not _sha(revision):
        raise ERROR('invalid trusted environment repository or revision')
    repository = repository.lower()
    document = _yaml(_read(api, repository, revision, MANIFEST, MAX_MANIFEST))
    spec = _spec(document)
    files, _ = _files(spec, lambda name: _read(api, repository, revision, name, MAX_RECIPE))
    commits = {}
    for upstream, raw in zip(spec.upstream, document.get('upstream', [])):
        commit = raw['ref']
        if not _sha(commit):
            ref = urllib.parse.quote(upstream.branch, safe='')
            reply = api('GET', f'/repos/{upstream.repository}/commits/{ref}')
            commit = reply.get('sha') if isinstance(reply, dict) else None
        if not _sha(commit):
            raise ERROR(f'{upstream.repository}: cannot resolve an upstream commit')
        commits[upstream.name] = commit
    recipe = dict(format=FORMAT, repository=repository, revision=revision, spec=document, files=files, commits=commits)
    validate(recipe)
    return recipe


def validate(recipe):
    """Check an untrusted wire representation, including its complete file closure."""
    if (not isinstance(recipe, dict) or set(recipe) != {'format', 'repository', 'revision', 'spec', 'files', 'commits'}
            or type(recipe['format']) is not int or recipe['format'] != FORMAT
            or not _repository(recipe['repository']) or not _sha(recipe['revision'])
            or not isinstance(recipe['files'], dict) or not isinstance(recipe['commits'], dict)):
        raise ERROR('invalid pinned environment recipe')
    try:
        if len(_dump(recipe)) > MAX_RECIPE:
            raise ERROR('pinned environment recipe exceeds the transport limit')
    except (TypeError, RecursionError) as exc:
        raise ERROR('environment recipe must contain only JSON data') from exc
    spec = _spec(recipe['spec'])
    files, _ = _files(spec, recipe['files'].get)
    if set(files) != set(recipe['files']):
        raise ERROR('pinned files do not match the recipe requirements')
    if (set(recipe['commits']) != {u.name for u in spec.upstream}
            or not all(_sha(s) for s in recipe['commits'].values())):
        raise ERROR('missing or invalid pinned upstream commits')
    for upstream, raw in zip(spec.upstream, recipe['spec'].get('upstream', [])):
        if _sha(raw['ref']) and raw['ref'] != recipe['commits'][upstream.name]:
            raise ERROR('explicit upstream commit differs from the recipe')
    return spec


def key(recipe, variant=None):
    spec = validate(recipe)
    if variant is not None and (not isinstance(variant, str) or not envbuild.VARIANT.fullmatch(variant)):
        raise ERROR('invalid environment cache variant')
    effective = dataclasses.asdict(spec.variant(variant))
    del effective['text'], effective['variants']
    material = [FORMAT, recipe['repository'].lower(), variant, platform.system(), platform.machine(),
                platform.libc_ver(), effective, recipe['files'], recipe['commits']]
    return hashlib.sha256(_dump(material).encode('ascii')).hexdigest()


def _lock(file, deadline):
    while True:
        try:
            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ERROR('timed out waiting for environment build lock')
            time.sleep(min(.1, remaining))


def _has_python(prefix):
    python = prefix / 'bin/python'
    return python.is_file() and os.access(python, os.X_OK) and (prefix / 'bin').resolve().is_relative_to(prefix)


def _legacy_python(path, python):
    # V1 records describe a recipe, not the host ABI. Check the installed
    # interpreter's Conda metadata before trusting a legacy prefix on shared storage.
    arch = {'x86_64': '64', 'aarch64': 'aarch64', 'ppc64le': 'ppc64le'}.get(platform.machine())
    if platform.system() != 'Linux' or not arch:
        return False
    records = (path / 'conda-meta').glob(f'python-{python}.*.json')
    for record in records:
        data = json.loads(record.read_text(encoding='utf-8'))
        if (data.get('name') == 'python' and data.get('subdir') == f'linux-{arch}'
                and data.get('version', '').startswith(python + '.')):
            return True
    return False


def _legacy(prefix, recipe, spec, variant, repository):
    if not prefix or variant:
        return None
    try:
        link = Path(prefix)
        path = link.resolve(strict=True)
        parent = link.parent.resolve(strict=True)
        if not path.is_relative_to(parent) or path == parent:
            return None
        if not _has_python(path) or not _legacy_python(path, spec.python):
            return None
        owner = recipe['repository'].lower().split('/')[0]
        repository = repository or recipe['repository']
        if not _repository(repository) or repository.lower().split('/')[0] != owner:
            return None
        _, old, files, commits = envbuild.kept(str(path), repository.lower())
        if old == spec and files == recipe['files'] and commits == recipe['commits']:
            return str(path)
    except (OSError, ValueError, RuntimeError, KeyError):
        pass
    return None


def _build(prefix, spec, recipe, *, root, tmp, conda, scope, build_env, run, deadline):
    inputs = tmp / 'requirements'
    inputs.mkdir()
    for name, content in recipe['files'].items():
        path = inputs / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
    home = tmp / 'home'
    home.mkdir()
    # A workflow's tokens, git configuration, .netrc, and Python path are never build inputs.
    env = {'PATH': BUILD_PATH, 'LANG': 'C.UTF-8', 'TMPDIR': str(tmp)}
    if build_env is not None:
        env.update({k: build_env[k] for k in _BUILD_ENV if k in build_env})
    env.update(HOME=str(home), GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1', GIT_TERMINAL_PROMPT='0',
               PYTHONNOUSERSITE='1', PIP_CONFIG_FILE=os.devnull)
    _, constraints = _files(spec, recipe['files'].get)
    steps = envbuild.prefix_steps(spec, str(prefix), reqdir=str(inputs), conda=conda, constraints=constraints)

    def execute(argv, label):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ERROR('environment preparation exceeded its time limit')
        try:
            result = run([*scope, *argv], cwd=str(tmp), env=env, timeout=remaining, check=False,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ERROR(f'environment build {label}: {exc.__class__.__name__}') from exc
        if result.returncode:
            raise ERROR(f'environment build {label} failed (exit {result.returncode})')

    for step in steps:
        if step[:2] == ['git', 'clone']:
            name = Path(step[-1]).name
            upstream = next(u for u in spec.upstream if u.name == name)
            commit = recipe['commits'][name]
            mirror = root / 'upstreams' / (upstream.repository + '.git')
            source_url = str(mirror) if mirror.is_dir() else f'https://github.com/{upstream.repository}.git'
            dest = step[-1]
            execute(['git', 'init', '--quiet', dest], f'initialize {name}')
            execute(['git', '-C', dest, 'fetch', '--quiet', '--depth=1', source_url, commit],
                    f'fetch pinned {upstream.repository} (private sources require a configured mirror)')
            execute(['git', '-C', dest, 'checkout', '--quiet', '--detach', commit], f'checkout {name}')
        else:
            execute(step, 'conda create' if step is steps[0] else 'pip install')
    if not _has_python(prefix):
        raise ERROR('environment build finished without an executable bin/python')


def _record(path):
    # Cache metadata must be a short, ordinary file, even after a failed migration.
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ERROR('invalid environment cache record')
        data = stream.read(4097)
        if len(data) > 4096:
            raise ERROR('oversized environment cache record')
        return data.decode('utf-8')


def _published(entry):
    name = _record(entry / 'ready')
    if name is None:
        return None
    if not re.fullmatch(r'build-[a-zA-Z0-9_-]{1,64}', name):
        raise ERROR('invalid published environment pointer')
    prefix = entry / name / 'prefix'
    if prefix.resolve() != prefix:
        raise ERROR('invalid published environment prefix')
    if not _has_python(prefix):
        # Recovery publishes a fresh attempt and never mutates the old build
        # that an already-running job might still reference.
        return None
    return str(prefix)


def lookup(root, recipe, *, variant=None):
    """Only check a completed cache entry. No directories are created on a miss."""
    return _published(Path(root).resolve() / 'entries' / key(recipe, variant))


def ensure(root, recipe, *, variant=None, conda='conda', scope=(), host_lock=None,
           build_env=None, legacy_prefix=None, legacy_repository=None, timeout=1800, run=subprocess.run):
    """Return a complete, physical prefix. Write the final pointer only after building at its real path."""
    spec = validate(recipe)
    digest = key(recipe, variant)
    root = Path(root).resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    entries = root / 'entries'
    entry = entries / digest
    for directory in (entries, entry):
        if directory.is_symlink():
            raise ERROR('unexpected symlink in environment cache')
        directory.mkdir(mode=0o700, exist_ok=True)
    deadline = time.monotonic() + timeout
    previous_failure = _record(entry / 'last-failure') or ''
    with (entry / 'build.lock').open('a') as lock:
        _lock(lock, deadline)
        published = _published(entry)
        if published:
            return published
        failure = _record(entry / 'last-failure') or ''
        if failure != previous_failure:
            reason = failure.partition('\n')[2]
            raise ERROR(f'environment build failed while waiting: {reason}')
        legacy = _legacy(legacy_prefix, recipe, spec, variant, legacy_repository)
        if legacy:
            return legacy
        # Never relocate a Conda prefix, even on publication. Unique attempt paths also
        # prevent an orphan subprocess from corrupting a retry following a killed builder.
        attempt = Path(tempfile.mkdtemp(prefix='build-', dir=entry))
        prefix = attempt / 'prefix'
        try:
            with tempfile.TemporaryDirectory(prefix='inputs-', dir=attempt) as tmp:
                with ExitStack() as stack:
                    if host_lock:
                        host = locks.HostLock(host_lock, timeout=max(0., deadline - time.monotonic()))
                        host.acquire('x')
                        stack.callback(host.release)
                    _build(prefix, spec.variant(variant), recipe, root=root, tmp=Path(tmp), conda=conda,
                           scope=scope, build_env=build_env, run=run, deadline=deadline)
            temp = attempt / 'ready'
            temp.write_text(attempt.name, encoding='ascii')
            os.replace(temp, entry / 'ready')
        except BaseException as exc:
            # Waiters already in this generation see the same failure. A new
            # request starts a fresh generation and may retry immediately.
            try:
                report = attempt / 'failure'
                report.write_text(attempt.name + '\n' + str(exc)[:512], encoding='utf-8')
                os.replace(report, entry / 'last-failure')
            finally:
                shutil.rmtree(attempt, ignore_errors=True)
            raise
        return str(prefix)
