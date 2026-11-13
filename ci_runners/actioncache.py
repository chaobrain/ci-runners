"""The action archive cache of a host's CI account (design decision 20, issue #249).

The runner app (2.338.2) reads ``ACTIONS_RUNNER_ACTION_ARCHIVE_CACHE`` from its ``.env``:
where that directory exists, "Set up job" copies the archive of an action ``owner/repo``
that it resolved to the commit ``sha`` from ``<dir>/<owner>_<repo>/<sha>.tar.gz`` instead
of downloading it through the egress; a missing entry is downloaded as before. A cached
file that copies but does not unpack, or that holds other than one top directory, fails
the job at setup, with no fallback. So an entry here is checked before it appears,
appears by a rename in its own directory, and never changes in place.

``fill`` reads the actions a release's reusable workflows and actions name (``uses:
owner/repo[/path]@ref``; this repository's are one archive of it at its stamped tag),
resolves each ref to the commit the runner asks for (``git ls-remote``: a 40-hex ref is
itself, else the peeled tag, the tag, the branch), downloads its archive by that commit
(``api.github.com/repos/<o>/<r>/tarball/<sha>``, the runner's own URL, through the
environment's ``https_proxy`` and without a token: the repositories are public), checks
it (``check``) and stores it mode 444. An entry the manifest (``manifest.json``) holds
with the same sha256 is kept, not downloaded again; any other file under an entry's name
is deleted before the download, so a failed one leaves the runners a miss, never a bad
file. Every entry the fill stores or keeps gets the atime 0: the runner's first copy then
moves its atime past its mtime, even under ``relatime``, which ``status`` reports as used
since the fill (not where the file system is mounted ``noatime``). Our own reads open
with ``O_NOATIME``. ``status --diag`` also counts the runners' ``Found action archive``
lines. ``status`` names entries ``owner/repo@sha7`` and prints no path, so its output can
go into a release record.

``remove --keep RELEASE...`` deletes the entries that no kept release names; ``--all``
every entry and the manifest, and keeps the directory (the runners then download). A
runner copying an entry that is unlinked keeps its descriptor. ``fill`` and ``remove``
hold ``<dir>/.lock``.

CLI (as the host's CI account; stdlib only)::

    ci-run actions-cache fill [--release TAG | --tree DIR]   the actions of an installed
                           release (default releases/hook) or a tree; exit 1 if one failed
    ci-run actions-cache status [--diag RUNNERS_DIR] [--days N]   every entry checked, and
                           whether a job used it since the fill; exit 1 on a problem
    ci-run actions-cache remove --keep RELEASE... | --all
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import fcntl
import glob
import gzip
import hashlib
import json
import os
import posixpath
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import urllib.request
import zlib
from collections.abc import Callable, Iterator, Mapping

from ci_runners import stamp

SCHEMA = 1
MANIFEST = 'manifest.json'
LOCK = '.lock'
#: an archive's size bound; the largest we name, actions/upload-artifact, is about 2.2 MB
MAX_BYTES = 64 << 20
#: its tar stream's bound once inflated: a gzip bomb is refused before it is stored
MAX_UNPACKED = 512 << 20
OWNER = re.compile(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}\Z')
REPO = re.compile(r'(?!\.\.?\Z)[A-Za-z0-9._-]{1,100}\Z')
SHA = re.compile(r'[0-9a-f]{40}\Z')
#: a release as ``actions/RELEASE`` names it, or an installed release's directory (install.sh)
RELEASE = re.compile(r'(?:hook|v\d+\.\d+\.\d+|main-[0-9a-f]{12})\Z')
USES = re.compile(r'''^[ \t]*(?:-[ \t]+)?uses:[ \t]*['"]?([^\s'"#]+)''', re.M)
REMOTE = 'https://github.com/{repo}'
TARBALL = 'https://api.github.com/repos/{repo}/tarball/{sha}'
FOUND = re.compile(r"Found action archive '([^']*)' in cache directory")


class CacheError(Exception):
    """A refusal: of an archive, a ref, the manifest or a cache directory. Its text names
    no path."""


def cache_dir(h: Mapping[str, object]) -> str:
    """The cache of the host ``h`` (design §3.2), which a runner's ``.env`` names. Pure."""
    return os.path.join(str(h['data']), 'ci-runners', 'cache', 'actions')


def entry_dir(cache: str, repo: str) -> str:
    """The runner's directory of ``owner/repo``: its ``/`` replaced by ``_``. Pure."""
    return os.path.join(cache, repo.replace('/', '_'))


def _repo(repo: str) -> bool:
    owner, _, name = repo.partition('/')
    return bool(OWNER.match(owner) and REPO.match(name))


# ------------------------------------------------------------------ what a release names

def uses(text: str, where: str = '') -> list[tuple[str, str]]:
    """``(owner/repo, ref)`` of every action archive ``text`` names: a ``uses:`` of
    ``owner/repo[/path]@ref``. A local action (``./``), a container (``docker://``) and a
    reusable workflow (``owner/repo/.github/workflows/<file>@ref``, which no runner
    downloads) are not archives. ValueError for a reference of another form. Pure."""
    out = []
    for m in USES.finditer(text):
        spec = m.group(1)
        if spec.startswith(('./', 'docker://')):
            continue
        path, at, ref = spec.rpartition('@')
        parts = path.split('/')
        if len(parts) >= 3 and parts[2] == '.github':
            continue
        repo = '/'.join(parts[:2])
        if not at or len(parts) < 2 or not _repo(repo) or not (SHA.match(ref) or stamp.GIT_REF.match(ref)) \
                or '..' in ref:
            raise ValueError(f'{where}: uses {spec!r} is not owner/repo[/path]@ref')
        out.append((repo, ref))
    return out


def sources(tree: str) -> list[str]:
    """The files of ``tree`` whose actions a self-hosted job runs: every action's
    ``action.yml`` and the reusable workflows the stamp rewrites (``stamp.WORKFLOWS``;
    this repository's own CI, ``test.yml``, runs on hosted runners). Relative paths."""
    acts = sorted(os.path.relpath(p, tree) for p in glob.glob(os.path.join(glob.escape(tree), 'actions', '*', 'action.yml')))
    return acts + [w for w in stamp.WORKFLOWS if os.path.isfile(os.path.join(tree, w))]


def refs(tree: str) -> list[tuple[str, str]]:
    """``(owner/repo, ref)`` of every action archive ``tree``'s ``sources`` name, once each,
    sorted. CacheError where the tree has none."""
    found = set()
    for rel in sources(tree):
        with open(os.path.join(tree, rel), encoding='utf-8') as f:
            found.update(uses(f.read(), rel))
    if not found:
        raise CacheError('the tree names no action')
    return sorted(found)


def release_of(tree: str) -> str:
    """The release ``tree`` is stamped with (``actions/RELEASE``): ``vX.Y.Z``, or ``hook``."""
    try:
        with open(os.path.join(tree, 'actions', 'RELEASE'), encoding='utf-8') as f:
            release = f.read().strip()
    except OSError:
        raise CacheError('the tree has no actions/RELEASE') from None
    if not stamp.RELEASE.match(release):
        raise CacheError(f'actions/RELEASE reads {release[:40]!r}, not hook or vX.Y.Z')
    return release


def git_ls_remote(url: str, patterns: list[str]) -> str:
    """``git ls-remote URL PATTERN...``'s output (the environment's ``https_proxy``)."""
    r = subprocess.run(['git', 'ls-remote', url, *patterns], capture_output=True, text=True, timeout=120,
                       env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'})
    if r.returncode:
        raise CacheError(f'git ls-remote {url}: exit {r.returncode}')
    return r.stdout


def resolve(repo: str, ref: str, ls_remote: Callable[[str, list[str]], str] = git_ls_remote) -> str:
    """The commit the runner gets for ``repo@ref``: a 40-hex ref is itself; else the peeled
    tag (an annotated tag's commit), the tag, then the branch."""
    if SHA.match(ref):
        return ref
    names = [f'refs/tags/{ref}^{{}}', f'refs/tags/{ref}', f'refs/heads/{ref}']  # in that order
    got = {}
    for line in ls_remote(REMOTE.format(repo=repo), [names[1], names[0], names[2]]).splitlines():
        sha, _, name = line.strip().partition('\t')
        if SHA.match(sha):
            got.setdefault(name, sha)
    for name in names:
        if name in got:
            return got[name]
    raise CacheError(f'{repo}@{ref}: no tag or branch of that name')


def download(url: str, out, limit: int) -> None:
    """Write the body of ``url`` (redirects followed) to ``out``: at most ``limit`` bytes."""
    req = urllib.request.Request(url, headers={'User-Agent': 'ci-runners', 'Accept': 'application/vnd.github+json'})
    n = 0
    with urllib.request.urlopen(req, timeout=300) as r:  # nosec: a fixed https URL; https_proxy from the environment
        while chunk := r.read(1 << 20):
            n += len(chunk)
            if n > limit:
                raise CacheError(f'larger than {limit} bytes')
            out.write(chunk)


# ------------------------------------------------------------------ an entry

def _open(path: str):
    """A file of the cache for reading, never through a link, never blocking on a FIFO, and
    without moving its atime (``O_NOATIME``, which only the owner may set; anyone else
    reads it plainly)."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    try:
        fd = os.open(path, flags | getattr(os, 'O_NOATIME', 0))
    except PermissionError:
        fd = os.open(path, flags)
    return os.fdopen(fd, 'rb')


def _inside(top: str, path: str) -> bool:
    p = posixpath.normpath(path)  # an absolute path stays absolute, so outside
    return p == top or p.startswith(top + '/')


def check(path: str, limit: int = MAX_BYTES, unpacked: int = MAX_UNPACKED) -> str:
    """The top directory of the archive at ``path``, else CacheError: what the runner can
    unpack and unwrap (``tar -xzf``, then exactly one directory), and nothing that writes
    outside that directory. A regular file of at most ``limit`` bytes; a gzip stream read
    to its end, at most ``unpacked`` bytes inflated; tar members all under one top-level
    entry, which is a directory; no absolute name, no ``..``, ``.`` or empty part; a
    symbolic link only to a path under the top directory, a hard link only to a member
    under it; no device, FIFO or other kind."""
    with _open(path) as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise CacheError('not a regular file')
        if st.st_size > limit:
            raise CacheError(f'{st.st_size} bytes, over the bound of {limit}')
        try:
            n = 0
            with gzip.GzipFile(fileobj=f, mode='rb') as g:
                while chunk := g.read(1 << 20):
                    n += len(chunk)
                    if n > unpacked:
                        raise CacheError(f'over {unpacked} bytes unpacked')
            f.seek(0)
            with tarfile.open(fileobj=f, mode='r:gz') as t:
                members = t.getmembers()
        except (OSError, EOFError, tarfile.TarError, zlib.error):
            raise CacheError('not a gzip tar archive') from None
    top = None
    for m in members:
        parts = m.name.split('/')
        if m.name.startswith('/'):
            raise CacheError('a member with an absolute name')
        if '..' in parts:
            raise CacheError("a member name with '..'")
        if '' in parts or '.' in parts:
            raise CacheError('a member name with an empty or "." part')
        top = parts[0] if top is None else top
        if parts[0] != top:
            raise CacheError('more than one top-level entry')
        if len(parts) == 1 and not m.isdir():
            raise CacheError('the top-level entry is not a directory')
        if not (m.isreg() or m.isdir() or m.issym() or m.islnk()):
            raise CacheError('a device, FIFO or other special member')
        if m.issym() and not _inside(top, posixpath.join(posixpath.dirname(m.name), m.linkname)):
            raise CacheError('a symbolic link that leaves the top directory')
        if m.islnk() and not _inside(top, m.linkname):
            raise CacheError('a hard link that leaves the top directory')
    if top is None:
        raise CacheError('an empty archive')
    return top


def _digest(path: str) -> tuple[str, int]:
    h, n = hashlib.sha256(), 0
    with _open(path) as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest(), n


def _intact(path: str, e: Mapping[str, object]) -> bool:
    """The entry at ``path`` is a regular file with the manifest record's size and sha256."""
    try:
        st = os.lstat(path)
        return stat.S_ISREG(st.st_mode) and st.st_size == e['bytes'] and _digest(path) == (e['sha256'], e['bytes'])
    except OSError:
        return False


def _fsync_dir(d: str) -> None:
    fd = os.open(d, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_new(d: str, name: str, write: Callable, mode: int) -> str:
    """Write a file in ``d`` under a fresh temporary name (``O_EXCL``, never through a link),
    fsynced, then mode ``mode``: its path. It is unlinked if ``write`` fails."""
    tmp = os.path.join(d, f'.{name}.{secrets.token_hex(4)}.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, 'wb') as f:
            write(f)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return tmp


def store(d: str, sha: str, url: str, fetch: Callable = download, limit: int = MAX_BYTES) -> tuple[str, int]:
    """Download ``url`` into a temporary file in ``d``, check it, and rename it to
    ``<sha>.tar.gz``: that name only ever holds a checked archive, whole. Its sha256 and
    size; on any failure the temporary file is gone and the name untouched."""
    tmp = _write_new(d, sha, lambda f: fetch(url, f, limit), 0o444)
    try:
        check(tmp, limit)
        got = _digest(tmp)
        os.replace(tmp, os.path.join(d, f'{sha}.tar.gz'))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    _fsync_dir(d)
    return got


def reset_atime(path: str) -> None:
    """The entry's atime to 0, its mtime kept: a later read by the runner (``File.Copy``)
    moves the atime past the mtime, under ``relatime`` too (an atime before the ctime)."""
    os.utime(path, ns=(0, os.lstat(path).st_mtime_ns), follow_symlinks=False)


def used(st: os.stat_result) -> bool:
    """Whether the entry was read since the fill reset its atime. Pure."""
    return st.st_atime_ns > st.st_mtime_ns


# ------------------------------------------------------------------ the manifest and the lock

def _ensure_dir(d: str) -> None:
    with contextlib.suppress(FileExistsError):
        os.mkdir(d, 0o700)
    st = os.lstat(d)
    if not stat.S_ISDIR(st.st_mode):
        raise CacheError('a cache directory is a link or not a directory')
    if st.st_uid != os.getuid():
        raise CacheError("a cache directory is not this account's")


def _empty() -> dict:
    return {'schema': SCHEMA, 'filled': None, 'entries': []}


def _entry_ok(e: object) -> bool:
    return (isinstance(e, dict) and isinstance(e.get('repo'), str) and _repo(e['repo'])
            and isinstance(e.get('sha'), str) and bool(SHA.match(e['sha']))
            and isinstance(e.get('sha256'), str) and bool(re.fullmatch(r'[0-9a-f]{64}', e['sha256']))
            and type(e.get('bytes')) is int and isinstance(e.get('filled'), str)
            and all(isinstance(e.get(k), list) and all(isinstance(x, str) for x in e[k]) for k in ('refs', 'releases')))


def read_manifest(cache: str) -> dict:
    """The manifest (schema 1): ``{"schema", "filled": <the last fill's UTC time>,
    "entries": [{"repo", "sha", "refs", "releases", "sha256", "bytes", "filled"}]}``; an
    empty one where there is none; CacheError where it does not read as that."""
    try:
        with _open(os.path.join(cache, MANIFEST)) as f:
            text = f.read(1 << 22)
    except FileNotFoundError:
        return _empty()
    except OSError:
        raise CacheError('the manifest is unreadable') from None
    try:
        doc = json.loads(text)
    except ValueError:
        raise CacheError('the manifest is not JSON') from None
    if not (isinstance(doc, dict) and doc.get('schema') == SCHEMA and isinstance(doc.get('entries'), list)
            and all(_entry_ok(e) for e in doc['entries'])):
        raise CacheError('the manifest is not of schema 1')
    return doc


def write_manifest(cache: str, doc: dict) -> None:
    data = (json.dumps(doc, indent=1, sort_keys=True) + '\n').encode()
    tmp = _write_new(cache, MANIFEST, lambda f: f.write(data), 0o600)
    os.replace(tmp, os.path.join(cache, MANIFEST))
    _fsync_dir(cache)


@contextlib.contextmanager
def locked(cache: str) -> Iterator[None]:
    """``fill`` and ``remove`` one at a time: CacheError if another holds the lock."""
    fd = os.open(os.path.join(cache, LOCK), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CacheError('another fill or remove holds the cache') from None
        yield
    finally:
        os.close(fd)


def _now(t: float) -> str:
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _name(repo: str, sha: str) -> str:
    return f'{repo}@{sha[:7]}'


# ------------------------------------------------------------------ the commands

def fill(cache: str, tree: str, *, ls_remote: Callable = git_ls_remote, fetch: Callable = download,
         limit: int = MAX_BYTES, now: Callable[[], float] = time.time, out: Callable = print) -> int:
    """Store the archive of every action ``tree`` names, each by the commit its ref resolves
    to now, where the cache does not hold it intact. 0, or 1 if one failed (the others are
    stored, and the failed one has no file under its name)."""
    release, wanted = release_of(tree), refs(tree)
    os.makedirs(os.path.dirname(cache), mode=0o700, exist_ok=True)
    _ensure_dir(cache)
    failed = 0
    with locked(cache):
        try:
            doc = read_manifest(cache)
        except CacheError as e:
            out(f'{e}: every entry is checked again')
            doc = _empty()
        entries = {(e['repo'], e['sha']): e for e in doc['entries']}
        found: dict[tuple[str, str], list[str]] = {}
        for repo, ref in wanted:
            try:
                found.setdefault((repo, resolve(repo, ref, ls_remote)), []).append(ref)
            except (CacheError, OSError, subprocess.SubprocessError) as e:
                failed += 1
                out(f'failed {repo}@{ref}: {e}')
        stored = kept = 0
        for (repo, sha), rs in sorted(found.items()):
            d, e = entry_dir(cache, repo), entries.get((repo, sha))
            path = os.path.join(d, f'{sha}.tar.gz')
            try:
                _ensure_dir(d)
                if e is not None and _intact(path, e):
                    kept += 1
                    verb = 'kept'
                else:
                    if os.path.lexists(path):  # not what the manifest holds: a miss until it is stored again
                        os.unlink(path)
                    digest, size = store(d, sha, TARBALL.format(repo=repo, sha=sha), fetch, limit)
                    e = entries[(repo, sha)] = {'repo': repo, 'sha': sha, 'refs': [], 'releases': [],
                                                'sha256': digest, 'bytes': size, 'filled': _now(now())}
                    stored += 1
                    verb = 'stored'
                e['refs'] = sorted({*e['refs'], *rs})
                e['releases'] = sorted({*e['releases'], release})
                reset_atime(path)
                out(f'{verb} {_name(repo, sha)} ({", ".join(rs)}), {e["bytes"]} bytes')
            except (CacheError, OSError) as x:
                failed += 1
                out(f'failed {_name(repo, sha)}: {x}')
        doc = {'schema': SCHEMA, 'filled': _now(now()),
               'entries': sorted(entries.values(), key=lambda e: (e['repo'], e['sha']))}
        write_manifest(cache, doc)
    out(f'fill for {release}: {stored} stored, {kept} kept, {failed} failed')
    return 1 if failed else 0


def mount_options(path: str, mountinfo: str) -> set[str] | None:
    """The options of the mount that holds ``path`` (a real path), from the text of
    ``/proc/self/mountinfo``: the last mount at the longest mount point above it. Pure."""
    best, opts = -1, None
    for line in mountinfo.splitlines():
        f = line.partition(' - ')[0].split()
        if len(f) < 6:
            continue
        mp = re.sub(r'\\([0-7]{3})', lambda m: chr(int(m.group(1), 8)), f[4])
        if (path == mp or path.startswith(mp.rstrip('/') + '/')) and len(mp) >= best:
            best, opts = len(mp), set(f[5].split(','))
    return opts


def _mountinfo() -> str:
    try:
        with open('/proc/self/mountinfo', encoding='utf-8', errors='replace') as f:
            return f.read(4 << 20)
    except OSError:
        return ''


def diag_counts(runners: str, days: float, now: float) -> dict[tuple[str, str], int]:
    """``Found action archive`` lines per ``(<owner>_<repo>, sha)`` in the runners'
    diagnostic logs (``<runners>/<name>/_diag/Worker_*.log``) changed in the last ``days``
    days: one line per hit."""
    out: dict[tuple[str, str], int] = {}
    for p in sorted(glob.glob(os.path.join(glob.escape(runners), '*', '_diag', 'Worker_*.log'))):
        try:
            if os.stat(p).st_mtime < now - days * 86400:
                continue
            with open(p, encoding='utf-8', errors='replace') as f:
                for line in f:
                    m = FOUND.search(line)
                    if m:
                        d, base = os.path.split(m.group(1))
                        key = (os.path.basename(d), base.removesuffix('.tar.gz'))
                        out[key] = out.get(key, 0) + 1
        except OSError:
            continue
    return out


def _archives(cache: str) -> set[tuple[str, str]]:
    """``(<owner>_<repo>, sha)`` of every archive name in the cache."""
    out = set()
    for p in glob.glob(os.path.join(glob.escape(cache), '*', '*.tar.gz')):
        d, base = os.path.split(p)
        out.add((os.path.basename(d), base.removesuffix('.tar.gz')))
    return out


def status(cache: str, *, diag: str | None = None, days: float = 7, mountinfo: Callable[[], str] = _mountinfo,
           now: Callable[[], float] = time.time, out: Callable = print) -> int:
    """Check every entry the manifest holds (there, intact, still one top directory) and
    say whether a job read it since the fill; 1 on a problem, an archive the manifest does
    not hold among them. Read only: names, states and counts, no path."""
    if not os.path.isdir(cache):
        out('action archive cache: none on this account')
        return 1
    doc = read_manifest(cache)
    opts = mount_options(os.path.realpath(cache), mountinfo())
    atime = opts is not None and 'noatime' not in opts
    hits = diag_counts(diag, days, now()) if diag else None
    out(f'action archive cache: {len(doc["entries"])} entries, last fill {doc["filled"] or "never"}')
    if not atime:
        out('atime: ' + ('the cache\'s file system is mounted noatime' if opts else 'its mount is unknown')
            + ': "used since the fill" is not available')
    problems = 0
    for e in doc['entries']:
        path = os.path.join(entry_dir(cache, e['repo']), f'{e["sha"]}.tar.gz')
        try:
            st = os.lstat(path)  # before any read: our reads leave the atime alone anyway (O_NOATIME)
        except FileNotFoundError:
            st = None
        if st is None:
            state = 'missing'
        elif not _intact(path, e):
            state = 'changed'
        else:
            try:
                check(path)
                state = 'ok'
            except CacheError as x:
                state = f'invalid ({x})'
        problems += state != 'ok'
        line = f'{_name(e["repo"], e["sha"])} ({", ".join(e["refs"])}; {", ".join(e["releases"])}): {state}'
        if st is not None and atime:
            line += '; used since the fill' if used(st) else '; not used since the fill'
        if hits is not None:
            line += f'; {hits.get((e["repo"].replace("/", "_"), e["sha"]), 0)} hits in the diagnostic logs ({days:g} d)'
        out(line)
    listed = {(e['repo'].replace('/', '_'), e['sha']) for e in doc['entries']}
    stray = _archives(cache) - listed
    if stray:
        problems += 1
        out(f'{len(stray)} archive(s) the manifest does not hold: remove --all, then fill')
    out('status: ' + ('ok' if not problems else f'{problems} problem(s)'))
    return 1 if problems else 0


def remove(cache: str, keep: list[str] | None = None, everything: bool = False, out: Callable = print) -> int:
    """Delete the entries no release in ``keep`` names, or with ``everything`` every entry,
    any other file and the manifest (the directory and its lock stay)."""
    if not os.path.isdir(cache):
        out('action archive cache: none on this account')
        return 0
    _ensure_dir(cache)
    with locked(cache):
        if everything:
            n = len(_archives(cache))
            for name in os.listdir(cache):
                p = os.path.join(cache, name)
                if name == LOCK:
                    continue
                if stat.S_ISDIR(os.lstat(p).st_mode):
                    shutil.rmtree(p)
                else:
                    os.unlink(p)
            _fsync_dir(cache)
            out(f'removed {n} archive(s) and the manifest')
            return 0
        doc = read_manifest(cache)
        left = []
        for e in doc['entries']:
            rel = [r for r in e['releases'] if r in (keep or [])]
            if rel:
                left.append({**e, 'releases': rel})
                continue
            d = entry_dir(cache, e['repo'])
            with contextlib.suppress(FileNotFoundError):
                os.unlink(os.path.join(d, f'{e["sha"]}.tar.gz'))
            with contextlib.suppress(OSError):
                os.rmdir(d)  # only once empty
            out(f'removed {_name(e["repo"], e["sha"])} ({", ".join(e["releases"])})')
        write_manifest(cache, {**doc, 'entries': left})
        out(f'kept {len(left)} entr{"y" if len(left) == 1 else "ies"}')
    return 0


def main(argv: list[str], h: Mapping[str, object]) -> int:
    ap = argparse.ArgumentParser(prog='ci-run actions-cache')
    sub = ap.add_subparsers(dest='cmd', required=True)
    f = sub.add_parser('fill', help="store the archives of a release's actions")
    g = f.add_mutually_exclusive_group()
    g.add_argument('--release', default='hook', help='an installed release (releases/<RELEASE>; default hook)')
    g.add_argument('--tree', help='a checkout of a release instead')
    s = sub.add_parser('status', help='check every entry; used since the fill')
    s.add_argument('--diag', metavar='RUNNERS_DIR', help="count the runners' cache hits in RUNNERS_DIR/*/_diag")
    s.add_argument('--days', type=float, default=7, help='the diagnostic logs of the last N days (7)')
    r = sub.add_parser('remove', help='prune by release, or empty the cache')
    g = r.add_mutually_exclusive_group(required=True)
    g.add_argument('--keep', nargs='+', metavar='RELEASE', help='keep the entries these releases name')
    g.add_argument('--all', action='store_true', help='every entry and the manifest')
    a = ap.parse_args(argv)
    cache = cache_dir(h)
    if a.cmd != 'remove' and not h.get('actions_cache'):
        print("ci-run actions-cache: this host's inventory does not set actions_cache: its runners do not "
              'read the cache (design 20)', file=sys.stderr)
    try:
        if a.cmd == 'fill':
            if a.tree:
                tree = a.tree
            elif RELEASE.match(a.release):
                tree = os.path.realpath(os.path.join(str(h['data']), 'ci-runners', 'releases', a.release))
            else:
                print(f'ci-run actions-cache: --release {a.release!r} is not hook, vX.Y.Z or main-<sha12>',
                      file=sys.stderr)
                return 2
            return fill(cache, tree)
        if a.cmd == 'status':
            return status(cache, diag=a.diag, days=a.days)
        if a.keep and not all(stamp.RELEASE.match(k) for k in a.keep):
            print('ci-run actions-cache: --keep takes releases: hook or vX.Y.Z', file=sys.stderr)
            return 2
        return remove(cache, a.keep, a.all)
    except (CacheError, OSError, ValueError) as e:
        print(f'ci-run actions-cache: {e}', file=sys.stderr)
        return 1

