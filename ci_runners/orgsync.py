"""``ci-run org-sync``: the org snapshot (design §6.5, decision 11.2).

Run every 10 min by ``ci-orgsync.timer`` on a host whose policy has an ``[org]`` table
(h100, RUN-5). It reads the org's members and its private repositories through the
GitHub API with ``etc/orgsync.token`` (fine-grained, read-only: org Members and
repository Metadata) and writes ``etc/org.json``, which the hook merges into the policy
(``policy.with_snapshot``). Every page must arrive; a failed, partial or empty fetch
leaves the old snapshot in place, and the hook refuses once it is stale.
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


def main(etc: str, call: Callable = github.api, now: Callable = time.time) -> int:
    try:
        with open(os.path.join(etc, 'policy.toml'), 'rb') as f:
            pol = policy.load(f.read(1 << 20))
        if pol.org is None:
            print('ci-run org-sync: the policy has no [org] table: nothing to sync', file=sys.stderr)
            return 0
        with open(os.path.join(etc, 'orgsync.token'), 'rb') as f:
            raw = f.read(MAX_TOKEN + 2)
        token = raw.strip().decode('ascii') if raw.isascii() else ''  # no byte of it reaches the journal
        if not token or len(token) > MAX_TOKEN or any(c.isspace() for c in token):
            raise SyncError('orgsync.token is empty or malformed')
        doc = fetch(pol.org.name, token, call, os.environ.get('GITHUB_API_URL', 'https://api.github.com'), now)
        write(etc, doc)
    except (OSError, ValueError, SyncError, github.ApiError) as e:
        print(f'ci-run org-sync: {e}; the old snapshot stays', file=sys.stderr)
        return 1
    print(f'ci-run org-sync: {len(doc["members"])} members, {len(doc["repositories"])} private repositories')
    return 0
