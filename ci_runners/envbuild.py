"""``ci-run env build <project>`` and ``env refresh``: build a project's conda env on this
host, and rebuild it when an upstream moved (design decisions 11.4, 12.10, 12.11, §4.9,
consumers §3a).

The env spec is ``.github/ci-runners.toml`` as the repository's **default branch**
holds it: the head of that branch is resolved once, and the spec and every requirement
file are read at that commit through the contents API, never from a pull request.

.. code-block:: toml

    schema = 1
    python = "3.12"                          # the env's python
    requirements = ["requirements-dev.txt"]  # pip -r, in order; their -r/-c files follow
    install = ["pytest-xdist"]               # more requirement specifiers, no options
    [conda]                                  # optional: packages that pip cannot give
    channels = ["conda-forge"]               # in order; the build passes --override-channels
    packages = ["nest-simulator=3.10"]       # match specs: no options, URLs or paths
    [[upstream]]                             # editable sibling checkouts
    repository = "chaobrain/brainstate"
    ref = "origin/main"                      # a branch of origin
    [variables]                              # for the registry entry (printed, not applied)
    XLA_PYTHON_CLIENT_PREALLOCATE = "false"
    [variant.cuda12]                         # a cluster target's env variant (14.9): its
    install = ["jax[cuda12]"]                # install replaces the base's
    conda = { packages = ["cuda-nvcc=12.2"] }  # optional: joins the base's conda table

The project itself is not installed: a part runs from the job's checkout. The ``conda``
packages are solved together with ``python`` in the one ``conda create``, before any pip
step; an unknown key or a malformed entry in the table builds nothing. A ``variant`` table
is read only by ``ci-run slurm env`` (``Spec.variant``); a build here uses the base. A
variant's ``conda`` channels follow the base's and its packages join them, in the same
solve; it names no channel or package the base names.

The constraints files the requirement files name with ``-c`` (fetched with them) bind the
upstreams' editable installs too, each passed with ``-c``: an upstream's dependency floor
cannot move a pinned version, its install fails instead. A spec without one builds as
before. The cluster's builds (``slurmenv``) take the same steps.

The env is built into a new directory ``<data>/ci-runners/envs/<project>-<stamp>``. Only
when every step has passed does the link ``<data>/ci-runners/envs/<project>`` move to it
(one rename), so a running part never sees a half-built env. The two previous builds are
kept for parts that still use them; older ones are removed. ``--dry-run`` reads the spec
and prints the steps without running them.

The token is the maintainer's ``GH_TOKEN``, set for the one run and never stored on the
host; it needs Contents: read on the repository and its private upstreams. It reaches git through its environment,
never through an argv.

``env build <project> --kept FILE`` (decision 15.8; ``-`` reads standard input) builds
from the record another host's env of the project kept (its ``ci-env.json``: the spec, the
requirement files and the repository's commit that host read with a token), checked as a
refresh checks its own record, and holds no token: its upstreams are cloned without
credentials. It is for a host whose account others share (h100), where a token in the
build's environment would be readable by them.

``env refresh <project>`` (or ``--all``: each project whose registry entry sets
``refresh = true``) holds no token. The env keeps the spec and requirement files it was
built from (``ci-env.json``) and the commit of each upstream. When an upstream's branch
has moved (``git ls-remote``, through the host's egress), the env is rebuilt from the
kept spec and linked in one rename, as a build is. An upstream git cannot read without
credentials (a private one) refuses the refresh; a failure of any step leaves the old env
as it was. One build or refresh of a project runs at a time. A build that a live part
holds (``envuse``) is never pruned.
"""
from __future__ import annotations

import argparse
import base64
import dataclasses
import contextlib
import datetime
import fcntl
import hashlib
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
from collections.abc import Callable, Mapping, Sequence

from ci_runners import envuse, github, inventory, registry

SPEC = '.github/ci-runners.toml'
MAX_FILE, MAX_FILES = 1 << 20, 16
KEEP = 3  # builds kept: the new one and the two before it
PYTHON = re.compile(r'3\.[0-9]{1,2}')
PATH = re.compile(r'[A-Za-z0-9_][A-Za-z0-9._/-]{0,199}')
#: a requirement specifier: name, extras, version clauses; no option, URL or path
SPECIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9._,-]+\])?([<>=!~]=?[A-Za-z0-9.*+!_-]+(,[<>=!~]=?[A-Za-z0-9.*+!_-]+)*)?')
#: a conda channel by name: no URL and no path
CHANNEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}')
#: a conda match spec: a name, then an optional version and build part that starts at an
#: operator; no option, channel prefix, URL or path
CONDA_SPEC = re.compile(r'([A-Za-z0-9][A-Za-z0-9._-]*)([<>=!~][A-Za-z0-9.*+!_,<>=~|-]*)?')
CONDA_KEYS = {'channels', 'packages'}
BRANCH = re.compile(r'origin/([A-Za-z0-9._-][A-Za-z0-9._/-]{0,99})')
STAMP = re.compile(r'[0-9]{8}T[0-9]{6}Z')
#: an env variant's name, as a site config's target names it (``slurmsite.VARIANT``)
VARIANT = re.compile(r'[a-z0-9][a-z0-9_.-]{0,30}')
VARIANT_KEYS = {'install', 'conda'}
#: a requirement file's lines that name another file: ``-r f``, ``--requirement f``, ``-c f``
NESTED = re.compile(r'\s*(?:-r|--requirement|-c|--constraint)[\s=]+(\S+)')
#: the lines of :data:`NESTED` that name a constraints file
CONSTRAINT = re.compile(r'\s*(?:-c|--constraint)[\s=]+(\S+)')


class SpecError(ValueError):
    """The env spec, or a file it names, is malformed: nothing is built."""


class MissingSpec(SpecError):
    pass


@dataclasses.dataclass(frozen=True)
class Upstream:
    repository: str
    branch: str

    @property
    def name(self) -> str:
        return self.repository.split('/')[1]


@dataclasses.dataclass(frozen=True)
class Conda:
    """The spec's ``conda`` table: the channels in order, and the match specs solved with python."""
    channels: tuple[str, ...] = ('conda-forge',)
    packages: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class Variant:
    """A ``[variant.<name>]`` table: its ``install``, which replaces the base's, and its
    ``conda`` channels and packages, which join the base's."""
    install: tuple[str, ...] = ()
    conda: Conda = Conda((), ())


@dataclasses.dataclass(frozen=True)
class Spec:
    python: str
    requirements: tuple[str, ...] = ()
    install: tuple[str, ...] = ()
    upstream: tuple[Upstream, ...] = ()
    variables: Mapping[str, str] = dataclasses.field(default_factory=dict)
    conda: Conda = Conda()
    #: ``[variant.<name>]``: each variant's ``install`` and ``conda`` additions
    variants: Mapping[str, Variant] = dataclasses.field(default_factory=dict)
    text: str = dataclasses.field(default='', compare=False)  # the spec as read, kept in the env

    def variant(self, name: str) -> 'Spec':
        """The spec a target of env variant ``name`` builds: the base with the variant's
        ``install`` and with its ``conda`` channels and packages after the base's, or the
        base itself where the spec does not name it."""
        if name not in self.variants:
            return self
        v = self.variants[name]
        conda = Conda(self.conda.channels + v.conda.channels, self.conda.packages + v.conda.packages)
        return dataclasses.replace(self, install=v.install, conda=conda)


def _path(where: str, p: object) -> str:
    """A repository-relative file path that stays inside the repository."""
    if not isinstance(p, str) or not PATH.fullmatch(p) or posixpath.normpath(p) != p or p.startswith('..'):
        raise SpecError(f'{where}: {p!r} is not a path inside the repository')
    return p


def _conda(doc: object, where: str = 'conda', base: Conda | None = None) -> Conda:
    """The ``conda`` table, or with ``base`` a variant's, whose channels and packages join
    the base's: its channels default to none, and it names no channel or package the base
    names. Raises SpecError."""
    if not isinstance(doc, dict):
        raise SpecError(f'{SPEC}: {where} must be a table')
    unknown = set(doc) - CONDA_KEYS
    if unknown:
        raise SpecError(f'{SPEC}: {where}: unknown keys {sorted(unknown)}')
    have = list(base.channels) if base is not None else []
    channels = doc.get('channels', ['conda-forge'] if base is None else [])
    if (not isinstance(channels, list) or (base is None and not channels)
            or not all(isinstance(c, str) and CHANNEL.fullmatch(c) for c in channels)
            or len(set(have + channels)) != len(have) + len(channels)):
        raise SpecError(f'{SPEC}: {where}.channels must list distinct channel names (no URLs or paths)'
                        + ('' if base is None else ", none of the base's"))
    packages = doc.get('packages', [])
    if not isinstance(packages, list):
        raise SpecError(f'{SPEC}: {where}.packages must be a list')
    names = [CONDA_SPEC.fullmatch(p).group(1).lower() for p in base.packages] if base is not None else []
    for pkg in packages:
        m = CONDA_SPEC.fullmatch(pkg) if isinstance(pkg, str) else None
        if not m:
            raise SpecError(f'{SPEC}: {where}.packages: {pkg!r} is not a match spec (no options, URLs or paths)')
        names.append(m.group(1).lower())
    if 'python' in names:
        raise SpecError(f'{SPEC}: {where}.packages cannot name python: the python key sets it')
    if len(set(names)) != len(names):
        raise SpecError(f'{SPEC}: {where}.packages names a package twice' + ('' if base is None else ' (with the base)'))
    return Conda(tuple(channels), tuple(packages))


def parse_spec(text: str) -> Spec:
    """``.github/ci-runners.toml``, schema 1. Raises SpecError. Pure."""
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise SpecError(f'{SPEC}: not TOML: {e}') from None
    if doc.get('schema') != 1:
        raise SpecError(f'{SPEC}: schema must be 1')
    unknown = set(doc) - {'schema', 'python', 'requirements', 'install', 'upstream', 'variables', 'conda', 'variant',
                          'contract'}
    if unknown:
        raise SpecError(f'{SPEC}: unknown keys {sorted(unknown)}')
    # the head's contract with the default branch's (decision 16.6): accepted and ignored
    # here from R1 (16.3), so that a consumer may write it before the reusable workflow reads it
    if 'contract' in doc and not (type(doc['contract']) is int and doc['contract'] >= 0):
        raise SpecError(f'{SPEC}: contract must be a whole number')
    py = doc.get('python')
    if not isinstance(py, str) or not PYTHON.fullmatch(py):
        raise SpecError(f'{SPEC}: python must be a version such as "3.12"')
    reqs = doc.get('requirements', [])
    if not isinstance(reqs, list):
        raise SpecError(f'{SPEC}: requirements must be a list')
    reqs = [_path('requirements', r) for r in reqs]
    install = _install('install', doc.get('install', []))
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
    conda = _conda(doc['conda']) if 'conda' in doc else Conda()
    return Spec(py, tuple(reqs), install, tuple(upstream), dict(variables), conda,
                _variants(doc.get('variant', {}), conda), text)


def _install(where: str, v: object) -> tuple[str, ...]:
    if not isinstance(v, list) or not all(isinstance(i, str) and SPECIFIER.fullmatch(i) for i in v):
        raise SpecError(f'{SPEC}: {where} must list requirement specifiers (no options, URLs or paths)')
    return tuple(v)


def _variants(doc: object, base: Conda) -> dict[str, Variant]:
    """The ``variant`` table: ``[variant.<name>]`` with ``install`` and an optional ``conda``
    table that joins ``base``. Raises SpecError."""
    if not isinstance(doc, dict):
        raise SpecError(f'{SPEC}: variant must be a table of tables')
    out = {}
    for name, v in doc.items():
        if not VARIANT.fullmatch(name):
            raise SpecError(f'{SPEC}: variant {name!r} is not a variant name')
        if not isinstance(v, dict):
            raise SpecError(f'{SPEC}: variant.{name} must be a table')
        if set(v) - VARIANT_KEYS:
            raise SpecError(f'{SPEC}: variant.{name}: unknown keys {sorted(set(v) - VARIANT_KEYS)} (only install and conda)')
        if 'install' not in v:
            raise SpecError(f'{SPEC}: variant.{name} needs install')
        conda = _conda(v['conda'], f'variant.{name}.conda', base) if 'conda' in v else Conda((), ())
        out[name] = Variant(_install(f'variant.{name}.install', v['install']), conda)
    return out


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


def constraints(files: dict[str, str]) -> list[str]:
    """The constraints files the requirement ``files`` (repository path -> text, as
    fetched) name with ``-c``, relative to the repository root, in order, once each. Pure."""
    out: list[str] = []
    for path, text in files.items():
        base = posixpath.dirname(path)
        for line in text.splitlines():
            m = CONSTRAINT.match(line)
            if m:
                p = _path(f'{path}: -c', posixpath.normpath(posixpath.join(base, m.group(1))))
                if p not in out:
                    out.append(p)
    return out


def build_dir(envs: str, project: str, stamp: str, variant: str = '') -> str:
    """``<project>-<stamp>``, or a variant's own build ``<project>-<stamp>-<variant>``."""
    if not registry.NAME.fullmatch(project) or not STAMP.fullmatch(stamp) or (variant and not VARIANT.fullmatch(variant)):
        raise SpecError('project, stamp or variant is malformed')
    return os.path.join(envs, f'{project}-{stamp}' + (f'-{variant}' if variant else ''))


def steps(spec: Spec, *, project: str, envs: str, stamp: str, reqdir: str, conda: str,
          scope: tuple[str, ...] = (), variant: str = '', constraints: Sequence[str] = ()) -> list[list[str]]:
    """The commands that build the env, in order, into ``build_dir`` (a ``variant``'s own
    where given: the caller passes ``spec.variant(variant)``). ``reqdir`` holds the
    requirement files at their repository paths; ``scope`` prefixes the heavy commands (a
    memory scope). ``constraints`` (:func:`constraints` of those files) also bind each
    upstream's editable install, so an upstream's dependency floor cannot move a pin. Pure."""
    d = build_dir(envs, project, stamp, variant)
    py = os.path.join(d, 'bin', 'python')
    pip = [*scope, py, '-m', 'pip', 'install', '--no-input', '--disable-pip-version-check']
    channels = [a for c in spec.conda.channels for a in ('-c', c)]
    out = [[*scope, conda, 'create', '--yes', '--quiet', '--prefix', d, '--override-channels', *channels,
            f'python={spec.python}', 'pip', *spec.conda.packages]]  # one solve, before every pip step
    for u in spec.upstream:
        out.append(['git', 'clone', '--quiet', '--depth', '1', '--branch', u.branch,
                    f'https://github.com/{u.repository}.git', os.path.join(d, 'src', u.name)])
    if spec.requirements or spec.install:
        args = [a for r in spec.requirements for a in ('-r', os.path.join(reqdir, r))]
        out.append([*pip, *args, *spec.install])
    cons = [a for c in constraints for a in ('-c', os.path.join(reqdir, c))]
    for u in spec.upstream:
        out.append([*pip, *cons, '-e', os.path.join(d, 'src', u.name)])
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


def fetch(call: Callable, token: str, repo: str, base: str = 'https://api.github.com', *,
          repository_id: int | None = None) -> tuple[str, Spec, dict[str, str]]:
    """The default branch's head, its spec and its requirement files (``-r`` and ``-c``
    followed, at most ``MAX_FILES``). Every read names that one commit."""
    info = call('GET', f'{base}/repos/{repo}', token)
    if repository_id is not None and (
            not isinstance(info, dict) or type(info.get('id')) is not int or info['id'] != repository_id
            or str(info.get('full_name', '')).lower() != repo or info.get('private') is not True):
        raise SpecError(f'{repo}: repository identity or visibility changed')
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

    try:
        spec = parse_spec(read(SPEC))
    except github.ApiError as e:
        if repository_id is not None and e.cause == 'HTTP 404':
            raise MissingSpec(f'{repo}: no {SPEC} on its default branch') from None
        raise
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


PROXY = ('https_proxy', 'http_proxy', 'no_proxy', 'HTTPS_PROXY', 'HTTP_PROXY', 'NO_PROXY')


def _git_env(token: str = '') -> dict[str, str]:
    """git's environment: the host's egress proxy variables, and the token as an http
    header, from the environment only. Without a token git has no credentials at all: no
    user or system configuration, no prompt."""
    env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': os.environ.get('HOME', '/'),
           'GIT_TERMINAL_PROMPT': '0', 'GIT_CONFIG_NOSYSTEM': '1', **{k: os.environ[k] for k in PROXY if k in os.environ}}
    if token:
        auth = base64.b64encode(f'x-access-token:{token}'.encode()).decode()
        env.update({'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'http.https://github.com/.extraheader',
                    'GIT_CONFIG_VALUE_0': f'Authorization: Basic {auth}'})
    else:
        env['GIT_CONFIG_GLOBAL'] = os.devnull
    return env


def _token(etc: str) -> str:
    """The maintainer's ``GH_TOKEN``, for this run only. No token is ever read from a file
    on the host: one that can read private repositories' contents must not rest on h100's
    shared account (design §3.3, T14). ``etc`` is unused, kept for the call's shape."""
    t = os.environ.get('GH_TOKEN', '').strip()
    if not t:
        raise SpecError('no token: run with GH_TOKEN set in your own shell, for this build only')
    return t


def _stamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')


def _scope(memory: str) -> tuple[str, ...]:
    return ('systemd-run', '--user', '--scope', '--quiet', '-p', f'MemoryMax={memory}', '-p', 'MemorySwapMax=0',
            'nice', '-n', '10')


def _commit(run: Callable, path: str, env: dict[str, str]) -> str:
    """The commit a clone is at."""
    r = run(['git', '-C', path, 'rev-parse', 'HEAD'], env=env, capture_output=True, text=True)
    out = r.stdout.strip() if r.returncode == 0 and isinstance(r.stdout, str) else ''
    if not github.SHA.fullmatch(out):
        raise SpecError(f'{path}: no commit')
    return out


class Busy(ValueError):
    """Another build or refresh of the project is running."""


def _lock(envs: str, project: str):
    """The project's build lock: one build or refresh at a time. The returned file holds
    it until closed."""
    f = open(os.path.join(envs, f'.{project}.lock'), 'a')
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        raise Busy(project) from None
    return f


def _build(proj: registry.Project, spec: Spec, files: dict[str, str], sha: str, *, role: str, token: str, envs: str,
           conda: str, memory: str, run: Callable, dry_run: bool = False, locked: bool = False,
           previous: str | None = None, apply_variables: bool = False) -> int:
    """Build the env of ``proj`` from ``spec`` and its requirement ``files`` (at ``sha`` of
    the repository), then move the link. The old env stays unless every step passed."""
    stamp = _stamp()
    d, link = build_dir(envs, proj.name, stamp), os.path.join(envs, proj.name)
    with tempfile.TemporaryDirectory(prefix='ci-envbuild-') as reqdir:
        cmds = steps(spec, project=proj.name, envs=envs, stamp=stamp, reqdir=reqdir, conda=conda, scope=_scope(memory),
                     constraints=constraints(files))
        print(f'ci-run env build: {proj.repository} at {sha[:12]} into {d}', file=sys.stderr)
        if dry_run:
            for c in cmds:
                print(' '.join(c))
            print(registry_snippet(role, link, spec), end='')
            return 0
        for rel, text in files.items():
            path = os.path.join(reqdir, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                f.write(text)
        if os.path.lexists(link) and not os.path.islink(link):
            raise SpecError(f'{link} exists and is not a link: move it away first')
        os.makedirs(envs, mode=0o700, exist_ok=True)
        with contextlib.nullcontext() if locked else _lock(envs, proj.name):
            if previous is not None and os.path.realpath(link) != previous:
                raise Busy(proj.name)
            if os.path.lexists(d):  # a failed build removes its directory: never one that is not its own
                raise SpecError(f'{d} exists: another build of this second; try again')
            genv = _git_env(token)
            commits = {}
            build_env = {k: v for k, v in os.environ.items() if k != 'GH_TOKEN'} if 'GH_TOKEN' in os.environ else None
            try:
                for c in cmds:
                    r = run(c, env=genv if c[0] == 'git' else build_env)
                    if r.returncode != 0:
                        raise SpecError(f'step failed ({r.returncode}): {" ".join(c[:12])}')
                for u in spec.upstream:
                    commits[u.name] = _commit(run, os.path.join(d, 'src', u.name), genv)
                with open(os.path.join(d, 'ci-env.json'), 'w', encoding='utf-8') as f:
                    json.dump({'repository': proj.repository, 'sha': sha, 'stamp': stamp, 'spec': spec.text,
                               'files': files, 'spec_digest': hashlib.sha256(spec.text.encode()).hexdigest(),
                               'apply_variables': apply_variables,
                               'upstream': [{**dataclasses.asdict(u), 'commit': commits[u.name]} for u in spec.upstream]},
                              f, indent=1)
            except BaseException:
                shutil.rmtree(d, ignore_errors=True)  # a failed build is never linked, so nothing uses it
                raise
            tmp = f'{link}.new-{stamp}'
            os.symlink(os.path.basename(d), tmp)
            os.replace(tmp, link)  # the one switch
            for old in prune(os.listdir(envs), proj.name):
                envuse.remove_unused(os.path.join(envs, old))  # not one a live part holds
    print(f'ci-run env build: {link} -> {os.path.basename(d)}; the registry entry needs:', file=sys.stderr)
    print(registry_snippet(role, link, spec), end='')
    return 0


# ------------------------------------------------------------------ refresh


def applies_variables(build: str) -> bool:
    try:
        with open(os.path.join(build, 'ci-env.json'), encoding='utf-8') as f:
            record = json.load(f)
    except (OSError, ValueError):
        return False
    return isinstance(record, dict) and record.get('apply_variables') is True


def kept(link: str, repository: str) -> tuple[str, Spec, dict[str, str], dict[str, str]]:
    """The commit of the repository, the spec, the requirement files and the upstream
    commits (by name) the env at ``link`` was built from. Raises SpecError for an env that kept none (built before ``v1.4``) or a
    record that does not fit the spec."""
    try:
        with open(os.path.join(link, 'ci-env.json'), encoding='utf-8') as f:
            rec = json.load(f)
    except (OSError, ValueError):
        raise SpecError(f'{link}: no readable ci-env.json: run env build once') from None
    return kept_record(rec, repository, link)


def kept_record(rec: object, repository: str, where: str) -> tuple[str, Spec, dict[str, str], dict[str, str]]:
    """:func:`kept` of a parsed ``ci-env.json`` (``where`` names it in errors): the record
    must be of ``repository``, its spec must parse, its requirement files be text under
    the repository's paths and its upstreams those of the spec, each with a commit. Pure."""
    if not isinstance(rec, dict) or rec.get('repository') != repository or not isinstance(rec.get('spec'), str) \
            or not isinstance(rec.get('files'), dict) or not isinstance(rec.get('sha'), str) \
            or not github.SHA.fullmatch(rec['sha']) or not isinstance(rec.get('upstream'), list):
        raise SpecError(f'{where}: the env kept no spec: run env build once')
    spec = parse_spec(rec['spec'])
    files = {_path('files', k): v for k, v in rec['files'].items()}
    if not all(isinstance(v, str) for v in files.values()):
        raise SpecError(f'{where}: a kept requirement file is not text')
    if len(files) > MAX_FILES or any(len(v.encode('utf-8')) > MAX_FILE for v in files.values()):
        raise SpecError(f'{where}: more than {MAX_FILES} requirement files, or one larger than {MAX_FILE} bytes')
    todo, named = list(spec.requirements), []  # the files the spec names, as fetch follows them
    while todo:
        q = todo.pop(0)
        if q in named:
            continue
        if q not in files:
            raise SpecError(f'{where}: the kept spec names {q}, which the record does not hold')
        named.append(q)
        todo += nested(q, files[q])  # each -r and -c target inside the repository, or a SpecError
    if set(named) != set(files):
        raise SpecError(f'{where}: the record holds requirement files the kept spec does not name')
    commits = {}
    for u in rec['upstream']:
        if not isinstance(u, dict) or not isinstance(u.get('commit'), str) or not github.SHA.fullmatch(u['commit']) \
                or not isinstance(u.get('repository'), str) or not isinstance(u.get('branch'), str):
            raise SpecError(f'{where}: a kept upstream has no commit: run env build once')
        commits[(u.get('repository'), u.get('branch'))] = u['commit']
    if set(commits) != {(u.repository, u.branch) for u in spec.upstream}:
        raise SpecError(f'{where}: the kept upstreams do not match the kept spec')
    return rec['sha'], spec, files, {u.name: commits[(u.repository, u.branch)] for u in spec.upstream}


#: the most a kept record given to ``--kept`` may hold: its files and the spec, with room for JSON
MAX_KEPT = (MAX_FILES + 2) * MAX_FILE


def read_kept(path: str, repository: str, stdin=None) -> tuple[str, Spec, dict[str, str]]:
    """The commit, spec and requirement files of the kept record at ``path`` (``-``:
    ``stdin``), another host's ``ci-env.json`` (decision 15.8). Its upstream commits are
    not used: the build clones each upstream's branch as it is now."""
    try:
        if path == '-':
            text = (stdin or sys.stdin).read(MAX_KEPT + 1)
        else:
            with open(path, encoding='utf-8') as f:
                text = f.read(MAX_KEPT + 1)
    except (OSError, UnicodeDecodeError) as e:
        raise SpecError(f'--kept {path}: {e}') from None
    if len(text) > MAX_KEPT:
        raise SpecError(f'--kept {path}: larger than {MAX_KEPT} bytes')
    try:
        rec = json.loads(text)
    except (ValueError, RecursionError):
        raise SpecError(f'--kept {path}: not JSON, or nested too deep') from None
    sha, spec, files, _ = kept_record(rec, repository, f'--kept {path}')
    return sha, spec, files


def head_of(run: Callable, u: Upstream, env: dict[str, str]) -> str:
    """The commit ``u``'s branch is at, read without credentials (``git ls-remote``)."""
    ref = f'refs/heads/{u.branch}'
    try:
        r = run(['git', 'ls-remote', '--quiet', f'https://github.com/{u.repository}.git', ref], env=env,
                capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise SpecError(f'{u.repository}: git ls-remote timed out') from None
    out = r.stdout if r.returncode == 0 and isinstance(r.stdout, str) else ''
    for line in out.splitlines():
        sha, _, name = line.partition('\t')
        if name == ref and github.SHA.fullmatch(sha):
            return sha
    raise SpecError(f'{u.repository}: cannot read {u.branch} without a token (a private repository, or no route): '
                    'a refresh holds none, so it builds nothing; run env build')


def refresh(proj: registry.Project, *, role: str, envs: str, conda: str, memory: str, run: Callable) -> str:
    """Rebuild the env of ``proj`` if an upstream moved; returns what happened. Raises
    SpecError, and leaves the old env, when it cannot."""
    link = os.path.join(envs, proj.name)
    if not os.path.islink(link):
        raise SpecError(f'{link}: no env to refresh: run env build')
    previous = os.path.realpath(link)
    sha, spec, files, old = kept(previous, proj.repository)
    if not spec.upstream:
        return 'no upstream to follow'
    env = _git_env()
    moved = {u.name: new for u in spec.upstream if (new := head_of(run, u, env)) != old[u.name]}
    if not moved:
        return 'up to date'
    print(f'ci-run env refresh: {proj.name}: moved: {", ".join(sorted(moved))}', file=sys.stderr)
    _build(proj, spec, files, sha, role=role, token='', envs=envs, conda=conda, memory=memory, run=run,
           previous=previous, apply_variables=applies_variables(previous))
    return 'rebuilt'


def main(argv: list[str], inv: inventory.Inventory, etc: str, call: Callable | None = None,
         run: Callable = subprocess.run) -> int:
    p = argparse.ArgumentParser(prog='ci-run env')
    p.add_argument('verb', choices=('build', 'refresh', 'reconcile'))
    p.add_argument('project', nargs='?')
    p.add_argument('--all', action='store_true', help='refresh: every project whose registry entry sets refresh = true')
    p.add_argument('--conda', default=os.environ.get('CI_RUNNERS_CONDA') or 'conda',
                   help='the conda executable (default: $CI_RUNNERS_CONDA, else conda on PATH)')
    p.add_argument('--memory', default='32G', help='MemoryMax of the build\'s scope')
    p.add_argument('--dry-run', action='store_true', help='build: print the steps, run none')
    p.add_argument('--kept', metavar='FILE', help="build: from another host's kept record of the project's env "
                   '(its ci-env.json; - reads stdin), with no token (decision 15.8)')
    a = p.parse_args(argv)
    if a.verb == 'refresh' and (a.dry_run or a.kept is not None or bool(a.project) == a.all):
        p.error('refresh takes a project or --all, and no --dry-run or --kept')
    if a.verb == 'build' and (not a.project or a.all):
        p.error('build takes a project')
    envs = os.path.join(inv.host['data'], 'ci-runners', 'envs')
    if a.verb == 'reconcile':
        if a.project or a.all or a.kept is not None:
            p.error('reconcile takes no project, --all or --kept')
        from ci_runners import envreconcile
        return envreconcile.main(inv, etc, envs, a, call or github.api, run)
    if a.verb == 'refresh':
        return _refresh_cli(a, inv, etc, envs, run)
    try:
        proj = registry.load_file(etc, a.project, inv.this)
        registry.memory_bytes(a.memory)
        link = os.path.join(envs, proj.name)
        previous = os.path.realpath(link)
        if a.kept is not None:  # no token at all: the record was read with one on another host (decision 15.8)
            if os.environ.get('GH_TOKEN', '').strip():  # every build step would inherit it
                raise SpecError('--kept holds no token: unset GH_TOKEN for this build')
            token = ''
            sha, spec, files = read_kept(a.kept, proj.repository)
        else:
            token = _token(etc)
            sha, spec, files = fetch(call or github.api, token, proj.repository, repository_id=proj.repository_id)
        return _build(proj, spec, files, sha, role=inv.this, token=token, envs=envs, conda=a.conda, memory=a.memory,
                      run=run, dry_run=a.dry_run, previous=previous,
                      apply_variables=applies_variables(previous))
    except Busy as e:
        print(f'ci-run env build: another build or refresh of {e} is running', file=sys.stderr)
        return 2
    except (OSError, ValueError, github.ApiError, registry.RegistryError) as e:
        print(f'ci-run env build: {e}', file=sys.stderr)
        return 2


def _refresh_cli(a: argparse.Namespace, inv: inventory.Inventory, etc: str, envs: str, run: Callable) -> int:
    """Refresh one project, or each project of this host that asks for it. A failure of one
    leaves its env as it was and the others go on; the exit status is 2 if any failed."""
    names = [a.project] if a.project else registry.refresh_projects(etc, inv.this)
    worst = 0
    for name in names:
        try:
            registry.memory_bytes(a.memory)
            proj = registry.load_file(etc, name, inv.this)
            what = refresh(proj, role=inv.this, envs=envs, conda=a.conda, memory=a.memory, run=run)
            print(f'ci-run env refresh: {name}: {what}', file=sys.stderr)
        except Busy:
            print(f'ci-run env refresh: {name}: another build or refresh is running', file=sys.stderr)
        except (OSError, ValueError, registry.RegistryError) as e:
            print(f'ci-run env refresh: {name}: {e}', file=sys.stderr)
            worst = 2
    return worst
