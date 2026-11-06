"""``ci-run org-sync``: the org snapshot (design §6.5, decisions 11.2 and 11.9).

    ci-run org-sync                     fetch with etc/orgsync.token, write etc/org.json
    ci-run org-sync --fetch --org NAME  fetch with $GH_TOKEN, print the snapshot
    ci-run org-sync --receive           read a snapshot on stdin, check it, write etc/org.json

The snapshot holds the org's members and its private repositories, read through the
GitHub API, and the hook merges it into the policy (``policy.with_snapshot``). Every
page must arrive; a failed, partial or empty fetch leaves the old snapshot in place, and
the hook refuses once it is stale.

h100 holds no token (decision 11.9): a timer of the maintainer's account on the
workstation runs ``--fetch`` with the maintainer's own login and pipes it over ssh to
``--receive`` on h100. ``--receive`` writes only a snapshot that loads as the hook loads
it, of the policy's org, not from the future and not older than the one in place. A
host that holds its own token runs the first form from ``ci-orgsync.timer``.
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable

from ci_runners import github, policy

PAGE, PAGES = 100, 50  # up to 5,000 members or repositories
MAX_TOKEN = 4096
MAX_SNAPSHOT = 4 << 20  # bytes, as the hook reads it


class SyncError(RuntimeError):
    pass


def pages(call: Callable, url: str, token: str) -> list:
    """Every item of a paged list endpoint; raises SyncError past ``PAGES`` pages."""
    out: list = []
    for page in range(1, PAGES + 1):
        got = call('GET', f'{url}&page={page}', token)
        if not isinstance(got, list):
            raise SyncError(f'GET {url.split("?")[0]}: not a list')
        out += got
        if len(got) < PAGE:
            return out
    raise SyncError(f'GET {url.split("?")[0]}: more than {PAGES} pages')


def snapshot(org: str, members: list, repos: list, now: float) -> dict:
    """The ``org.json`` document from the API's member and repository lists. Pure.

    Members are users (``type == "User"``); a repository counts only if the API states
    it private and it belongs to ``org``. Raises SyncError on an item without an ASCII
    login, name or an id, and on an empty member list (an org always has one; an empty
    list would refuse everyone). Names are checked as ASCII before they are lower-cased.
    """
    org = org.lower()
    logins = set()
    for m in members:
        if not isinstance(m, dict) or not isinstance(m.get('login'), str) or not m['login'].isascii():
            raise SyncError('a member without an ASCII login')
        if m.get('type', 'User') == 'User':
            logins.add(m['login'].lower())
    if not logins:
        raise SyncError('no members')
    out = {}
    for r in repos:
        if (not isinstance(r, dict) or not isinstance(r.get('full_name'), str) or not r['full_name'].isascii()
                or type(r.get('id')) is not int):
            raise SyncError('a repository without an ASCII name or an id')
        name = r['full_name'].lower()
        if r.get('private') is True and name.startswith(org + '/'):
            out[name] = r['id']
    doc = {'schema': policy.SNAPSHOT_SCHEMA, 'org': org, 'fetched_at': round(now, 3), 'members': sorted(logins),
           'repositories': dict(sorted(out.items()))}
    policy.load_snapshot(json.dumps(doc))  # what the hook will read must load
    return doc


def fetch(org: str, token: str, call: Callable = github.api, base: str = 'https://api.github.com',
          now: Callable = time.time) -> dict:
    base = base.rstrip('/')
    started = now()  # the snapshot is as old as its first page: a removal during the fetch counts
    members = pages(call, f'{base}/orgs/{org}/members?per_page={PAGE}', token)
    repos = pages(call, f'{base}/orgs/{org}/repos?type=private&per_page={PAGE}', token)
    return snapshot(org, members, repos, started)


def write(etc: str, doc: dict) -> str:
    """Write ``etc/org.json`` atomically, mode 600: a temporary file, then a rename and
    an fsync of ``etc``. The temporary file is created exclusively and without following
    a link; those a killed run left behind are removed first (unlink follows no link)."""
    path = os.path.join(etc, 'org.json')
    tmp = f'{path}.{os.getpid()}.tmp'
    for name in os.listdir(etc):
        if name.startswith('org.json.') and name.endswith('.tmp'):
            try:
                os.unlink(os.path.join(etc, name))
            except OSError:
                pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(doc, f, indent=1, sort_keys=True)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        dfd = os.open(etc, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def _token(raw: bytes) -> str:
    """A token as read from a file or the environment, checked; no byte of a bad one is
    ever printed."""
    token = raw.strip().decode('ascii') if raw.isascii() else ''
    if not token or len(token) > MAX_TOKEN or any(c.isspace() for c in token):
        raise SyncError('the token is empty or malformed')
    return token


def accept(data: bytes, org: str, current: bytes | None, now: float) -> dict:
    """The document of a received snapshot, if ``etc/org.json`` may become it. Raises
    SyncError otherwise. Pure.

    It must load as the hook loads it (``policy.load_snapshot``), be ``org``'s, be no
    more than ``policy.CLOCK_SKEW`` seconds in the future, and be no older than
    ``current``, the snapshot in place (if that one loads): a replayed old snapshot
    cannot bring back a removed member."""
    if len(data) > MAX_SNAPSHOT:
        raise SyncError(f'the snapshot is larger than {MAX_SNAPSHOT} bytes')
    try:
        snap = policy.load_snapshot(data)
    except policy.PolicyError as e:
        raise SyncError(str(e)) from None
    if snap.org != org.lower():
        raise SyncError(f'the snapshot is of org {snap.org!r}, the policy\'s is {org.lower()!r}')
    if snap.fetched_at > now + policy.CLOCK_SKEW:
        raise SyncError('the snapshot is from the future')
    if current is not None:
        try:
            old = policy.load_snapshot(current)
        except policy.PolicyError:
            old = None  # a broken snapshot in place refuses every job: any good one replaces it
        if old is not None and snap.fetched_at < old.fetched_at:
            raise SyncError('the snapshot is older than the one in place')
    return json.loads(data)


def _policy_org(etc: str) -> str | None:
    with open(os.path.join(etc, 'policy.toml'), 'rb') as f:
        pol = policy.load(f.read(1 << 20))
    return None if pol.org is None else pol.org.name


def receive(etc: str, stdin, now: Callable = time.time) -> int:
    try:
        org = _policy_org(etc)
        if org is None:
            raise SyncError('the policy has no [org] table')
        data = stdin.read(MAX_SNAPSHOT + 1)
        try:
            with open(os.path.join(etc, 'org.json'), 'rb') as f:
                current = f.read(MAX_SNAPSHOT + 1)
        except FileNotFoundError:
            current = None
        doc = accept(data, org, current, now())
        write(etc, doc)
    except (OSError, ValueError, SyncError) as e:
        print(f'ci-run org-sync --receive: {e}; the old snapshot stays', file=sys.stderr)
        return 1
    print(f'ci-run org-sync --receive: {len(doc["members"])} members, {len(doc["repositories"])} private repositories')
    return 0


def fetch_out(org: str, environ, out, call: Callable = github.api, now: Callable = time.time) -> int:
    try:
        if not policy._ORG.match(org):
            raise SyncError('--org is not an org name')
        token = _token(environ.get('GH_TOKEN', '').encode())
        doc = fetch(org, token, call, environ.get('GITHUB_API_URL', 'https://api.github.com'), now)
    except (OSError, ValueError, SyncError, github.ApiError) as e:
        print(f'ci-run org-sync --fetch: {e}', file=sys.stderr)
        return 1
    json.dump(doc, out, indent=1, sort_keys=True)
    out.write('\n')
    out.flush()
    return 0


def cli(args: list[str], etc: str) -> int:
    if not args:
        return main(etc)
    if args == ['--receive']:
        return receive(etc, sys.stdin.buffer)
    if len(args) == 3 and args[:2] == ['--fetch', '--org']:
        return fetch_out(args[2], os.environ, sys.stdout)
    print('usage: ci-run org-sync [--fetch --org NAME | --receive]', file=sys.stderr)
    return 2


def main(etc: str, call: Callable = github.api, now: Callable = time.time) -> int:
    try:
        with open(os.path.join(etc, 'policy.toml'), 'rb') as f:
            pol = policy.load(f.read(1 << 20))
        if pol.org is None:
            print('ci-run org-sync: the policy has no [org] table: nothing to sync', file=sys.stderr)
            return 0
        with open(os.path.join(etc, 'orgsync.token'), 'rb') as f:
            raw = f.read(MAX_TOKEN + 2)
        try:
            token = _token(raw)
        except SyncError:
            raise SyncError('orgsync.token is empty or malformed') from None
        doc = fetch(pol.org.name, token, call, os.environ.get('GITHUB_API_URL', 'https://api.github.com'), now)
        write(etc, doc)
    except (OSError, ValueError, SyncError, github.ApiError) as e:
        print(f'ci-run org-sync: {e}; the old snapshot stays', file=sys.stderr)
        return 1
    print(f'ci-run org-sync: {len(doc["members"])} members, {len(doc["repositories"])} private repositories')
    return 0
