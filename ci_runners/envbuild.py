"""``ci-run env build <project>``: build a project's conda env on this host (design
decision 11.4, consumers §3a).

The env spec is ``.github/ci-runners.toml`` as the repository's **default branch**
holds it: the head of that branch is resolved once, and the spec and every requirement
file are read at that commit through the contents API, never from a pull request.

.. code-block:: toml

    schema = 1
    python = "3.12"                          # the env's python
    requirements = ["requirements-dev.txt"]  # pip -r, in order; their -r/-c files follow
    install = ["pytest-xdist"]               # more requirement specifiers, no options
    [[upstream]]                             # editable sibling checkouts
    repository = "chaobrain/brainstate"
    ref = "origin/main"                      # a branch of origin
    [variables]                              # for the registry entry (printed, not applied)
    XLA_PYTHON_CLIENT_PREALLOCATE = "false"

The project itself is not installed: a part runs from the job's checkout.

The env is built into a new directory ``<data>/ci-runners/envs/<project>-<stamp>``. Only
when every step has passed does the link ``<data>/ci-runners/envs/<project>`` move to it
(one rename), so a running part never sees a half-built env. The two previous builds are
kept for parts that still use them; older ones are removed. ``--dry-run`` reads the spec
and prints the steps without running them.

The token is the maintainer's ``GH_TOKEN``, set for the one run and never stored on the
host; it needs Contents: read on the repository and its private upstreams. It reaches git through its environment,
never through an argv.
"""
from __future__ import annotations

import argparse
import base64
import dataclasses
import datetime
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.parse
from collections.abc import Callable, Mapping

from ci_runners import github, inventory, registry

SPEC = '.github/ci-runners.toml'
MAX_FILE, MAX_FILES = 1 << 20, 16
KEEP = 3  # builds kept: the new one and the two before it
PYTHON = re.compile(r'3\.[0-9]{1,2}')
PATH = re.compile(r'[A-Za-z0-9_][A-Za-z0-9._/-]{0,199}')
#: a requirement specifier: name, extras, version clauses; no option, URL or path
SPECIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9._,-]+\])?([<>=!~]=?[A-Za-z0-9.*+!_-]+(,[<>=!~]=?[A-Za-z0-9.*+!_-]+)*)?')
BRANCH = re.compile(r'origin/([A-Za-z0-9._-][A-Za-z0-9._/-]{0,99})')
STAMP = re.compile(r'[0-9]{8}T[0-9]{6}Z')
#: a requirement file's lines that name another file: ``-r f``, ``--requirement f``, ``-c f``
NESTED = re.compile(r'\s*(?:-r|--requirement|-c|--constraint)[\s=]+(\S+)')


class SpecError(ValueError):
    """The env spec, or a file it names, is malformed: nothing is built."""


@dataclasses.dataclass(frozen=True)
class Upstream:
    repository: str
    branch: str

    @property
    def name(self) -> str:
        return self.repository.split('/')[1]


@dataclasses.dataclass(frozen=True)
class Spec:
    python: str
    requirements: tuple[str, ...] = ()
    install: tuple[str, ...] = ()
    upstream: tuple[Upstream, ...] = ()
    variables: Mapping[str, str] = dataclasses.field(default_factory=dict)


def _path(where: str, p: object) -> str:
    """A repository-relative file path that stays inside the repository."""
    if not isinstance(p, str) or not PATH.fullmatch(p) or posixpath.normpath(p) != p or p.startswith('..'):
        raise SpecError(f'{where}: {p!r} is not a path inside the repository')
    return p


def parse_spec(text: str) -> Spec:
    """``.github/ci-runners.toml``, schema 1. Raises SpecError. Pure."""
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise SpecError(f'{SPEC}: not TOML: {e}') from None
    if doc.get('schema') != 1:
        raise SpecError(f'{SPEC}: schema must be 1')
    unknown = set(doc) - {'schema', 'python', 'requirements', 'install', 'upstream', 'variables'}
    if unknown:
        raise SpecError(f'{SPEC}: unknown keys {sorted(unknown)}')
    py = doc.get('python')
    if not isinstance(py, str) or not PYTHON.fullmatch(py):
        raise SpecError(f'{SPEC}: python must be a version such as "3.12"')
    reqs = doc.get('requirements', [])
    if not isinstance(reqs, list):
        raise SpecError(f'{SPEC}: requirements must be a list')
    reqs = [_path('requirements', r) for r in reqs]
    install = doc.get('install', [])
    if not isinstance(install, list) or not all(isinstance(i, str) and SPECIFIER.fullmatch(i) for i in install):
        raise SpecError(f'{SPEC}: install must list requirement specifiers (no options, URLs or paths)')
    ups = doc.get('upstream', [])
    if not isinstance(ups, list):
        raise SpecError(f'{SPEC}: upstream must be an array of tables')
    upstream = []
    for u in ups:
        if not isinstance(u, dict) or set(u) != {'repository', 'ref'}:
            raise SpecError(f'{SPEC}: an upstream has exactly repository and ref')
        repo, ref = u['repository'], u['ref']
        if not isinstance(repo, str) or not registry.REPO.fullmatch(repo) or '..' in repo:
            raise SpecError(f'{SPEC}: upstream repository must be owner/name')
        m = BRANCH.fullmatch(ref) if isinstance(ref, str) else None
        if not m or '..' in ref:
            raise SpecError(f'{SPEC}: upstream ref must be origin/<branch>')
        upstream.append(Upstream(repo.lower(), m.group(1)))
    if len({u.name for u in upstream}) != len(upstream):
        raise SpecError(f'{SPEC}: two upstreams share a name')
    variables = doc.get('variables', {})
    if not isinstance(variables, dict):
        raise SpecError(f'{SPEC}: variables must be a table')
    for k, v in variables.items():
        if not registry.VAR.fullmatch(k) or registry.RESERVED.fullmatch(k):
            raise SpecError(f'{SPEC}: variables: {k!r} is not a name a project may set')
        if not isinstance(v, str) or '\0' in v or '\n' in v:
            raise SpecError(f'{SPEC}: variables.{k} must be a one-line string')
    return Spec(py, tuple(reqs), tuple(install), tuple(upstream), dict(variables))


def nested(path: str, text: str) -> list[str]:
    """The files a requirement file names with ``-r`` or ``-c``, relative to the
    repository root, each checked to stay inside it. Pure."""
    out = []
    base = posixpath.dirname(path)
    for line in text.splitlines():
        m = NESTED.match(line)
        if m:
            out.append(_path(f'{path}: -r', posixpath.normpath(posixpath.join(base, m.group(1)))))
    return out


def build_dir(envs: str, project: str, stamp: str) -> str:
    if not registry.NAME.fullmatch(project) or not STAMP.fullmatch(stamp):
        raise SpecError('project or stamp is malformed')
    return os.path.join(envs, f'{project}-{stamp}')


def steps(spec: Spec, *, project: str, envs: str, stamp: str, reqdir: str, conda: str,
          scope: tuple[str, ...] = ()) -> list[list[str]]:
    """The commands that build the env, in order. ``reqdir`` holds the requirement files
    at their repository paths; ``scope`` prefixes the heavy commands (a memory scope).
    Pure."""
    d = build_dir(envs, project, stamp)
    py = os.path.join(d, 'bin', 'python')
    pip = [*scope, py, '-m', 'pip', 'install', '--no-input', '--disable-pip-version-check']
    out = [[*scope, conda, 'create', '--yes', '--quiet', '--prefix', d, '--override-channels', '-c', 'conda-forge',
            f'python={spec.python}', 'pip']]
    for u in spec.upstream:
        out.append(['git', 'clone', '--quiet', '--depth', '1', '--branch', u.branch,
                    f'https://github.com/{u.repository}.git', os.path.join(d, 'src', u.name)])
    if spec.requirements or spec.install:
        args = [a for r in spec.requirements for a in ('-r', os.path.join(reqdir, r))]
        out.append([*pip, *args, *spec.install])
    for u in spec.upstream:
        out.append([*pip, '-e', os.path.join(d, 'src', u.name)])
    return out


def prune(names: list[str], project: str, keep: int = KEEP) -> list[str]:
    """The builds of ``project`` to remove: all but the ``keep`` newest. Pure."""
    pat = re.compile(re.escape(project) + r'-(' + STAMP.pattern + r')\Z')
    builds = sorted(n for n in names if pat.fullmatch(n))
    return builds[:-keep] if len(builds) > keep else []


def registry_snippet(role: str, link: str, spec: Spec) -> str:
    lines = [f'[hosts.{role}]', f'env = "{link}"']
    if spec.variables:
        lines.append('variables = { ' + ', '.join(f'{k} = {json.dumps(v)}' for k, v in sorted(spec.variables.items()))
                     + ' }')
    return '\n'.join(lines) + '\n'


# ------------------------------------------------------------------ I/O


def fetch(call: Callable, token: str, repo: str, base: str = 'https://api.github.com') -> tuple[str, Spec, dict[str, str]]:
    """The default branch's head, its spec and its requirement files (``-r`` and ``-c``
    followed, at most ``MAX_FILES``). Every read names that one commit."""
    info = call('GET', f'{base}/repos/{repo}', token)
    branch = info.get('default_branch') if isinstance(info, dict) else None
    if not isinstance(branch, str) or not branch:
        raise SpecError(f'{repo}: no default branch')
    head = call('GET', f'{base}/repos/{repo}/commits/{urllib.parse.quote(branch, safe="")}', token)
    sha = head.get('sha') if isinstance(head, dict) else None
    if not isinstance(sha, str) or not github.SHA.fullmatch(sha):
        raise SpecError(f'{repo}: the default branch has no head')

    def read(path: str) -> str:
        got = call('GET', f'{base}/repos/{repo}/contents/{urllib.parse.quote(path)}?ref={sha}', token)
        if not isinstance(got, dict) or got.get('type') != 'file' or got.get('encoding') != 'base64':
            raise SpecError(f'{path}: not a file at {sha[:12]}')
        data = base64.b64decode(got.get('content', ''))
        if len(data) > MAX_FILE:
            raise SpecError(f'{path}: larger than {MAX_FILE} bytes')
        try:
            return data.decode('utf-8')
        except UnicodeDecodeError:
            raise SpecError(f'{path}: not UTF-8') from None

    spec = parse_spec(read(SPEC))
    files: dict[str, str] = {}
    todo = list(spec.requirements)
    while todo:
        p = todo.pop(0)
        if p in files:
            continue
        if len(files) >= MAX_FILES:
            raise SpecError(f'more than {MAX_FILES} requirement files')
        files[p] = read(p)
        todo += nested(p, files[p])
    return sha, spec, files


def _git_env(token: str) -> dict[str, str]:
    """git's environment: the token as an http header, from the environment only."""
    auth = base64.b64encode(f'x-access-token:{token}'.encode()).decode()
    return {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': os.environ.get('HOME', '/'),
            'GIT_TERMINAL_PROMPT': '0', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_COUNT': '1',
            'GIT_CONFIG_KEY_0': 'http.https://github.com/.extraheader',
            'GIT_CONFIG_VALUE_0': f'Authorization: Basic {auth}'}


def _token(etc: str) -> str:
    """The maintainer's ``GH_TOKEN``, for this run only. No token is ever read from a file
    on the host: one that can read private repositories' contents must not rest on h100's
    shared account (design §3.3, T14). ``etc`` is unused, kept for the call's shape."""
    t = os.environ.get('GH_TOKEN', '').strip()
    if not t:
        raise SpecError('no token: run with GH_TOKEN set in your own shell, for this build only')
    return t


def main(argv: list[str], inv: inventory.Inventory, etc: str, call: Callable | None = None,
         run: Callable = subprocess.run) -> int:
    p = argparse.ArgumentParser(prog='ci-run env build')
    p.add_argument('verb', choices=('build',))
    p.add_argument('project')
    p.add_argument('--conda', default='conda', help='the conda executable (default: conda on PATH)')
    p.add_argument('--memory', default='32G', help='MemoryMax of the build\'s scope')
    p.add_argument('--dry-run', action='store_true')
    a = p.parse_args(argv)
    try:
        proj = registry.load_file(etc, a.project, inv.this)
        registry.memory_bytes(a.memory)
        token = _token(etc)
        sha, spec, files = fetch(call or github.api, token, proj.repository)
        envs = os.path.join(inv.host['data'], 'ci-runners', 'envs')
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        d, link = build_dir(envs, proj.name, stamp), os.path.join(envs, proj.name)
        scope = ('systemd-run', '--user', '--scope', '--quiet', '-p', f'MemoryMax={a.memory}', '-p',
                 'MemorySwapMax=0', 'nice', '-n', '10')
        with tempfile.TemporaryDirectory(prefix='ci-envbuild-') as reqdir:
            cmds = steps(spec, project=proj.name, envs=envs, stamp=stamp, reqdir=reqdir, conda=a.conda, scope=scope)
            print(f'ci-run env build: {proj.repository} at {sha[:12]} into {d}', file=sys.stderr)
            if a.dry_run:
                for c in cmds:
                    print(' '.join(c))
                print(registry_snippet(inv.this, link, spec), end='')
                return 0
            for rel, text in files.items():
                path = os.path.join(reqdir, rel)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(text)
            if os.path.lexists(link) and not os.path.islink(link):
                raise SpecError(f'{link} exists and is not a link: move it away first')
            os.makedirs(envs, mode=0o700, exist_ok=True)
            genv = _git_env(token)
            for c in cmds:
                r = run(c, env=genv if c[0] == 'git' else None)
                if r.returncode != 0:
                    shutil.rmtree(d, ignore_errors=True)  # a failed build is never linked, so nothing uses it
                    raise SpecError(f'step failed ({r.returncode}): {" ".join(c[:12])}')
        with open(os.path.join(d, 'ci-env.json'), 'w', encoding='utf-8') as f:
            json.dump({'repository': proj.repository, 'sha': sha, 'stamp': stamp,
                       'upstream': [dataclasses.asdict(u) for u in spec.upstream]}, f, indent=1)
        tmp = f'{link}.new-{stamp}'
        os.symlink(os.path.basename(d), tmp)
        os.replace(tmp, link)  # the one switch
        for old in prune(os.listdir(envs), proj.name):
            shutil.rmtree(os.path.join(envs, old), ignore_errors=True)
    except (OSError, ValueError, github.ApiError, registry.RegistryError) as e:
        print(f'ci-run env build: {e}', file=sys.stderr)
        return 2
    print(f'ci-run env build: {link} -> {os.path.basename(d)}; the registry entry needs:', file=sys.stderr)
    print(registry_snippet(inv.this, link, spec), end='')
    return 0
