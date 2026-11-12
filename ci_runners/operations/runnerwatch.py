"""``ci-run runner-watch``: an issue when a host has no runner online (design decision 15.6).

``ci-run runner-watch --org ORG --state FILE [--report-only] -- HOST...`` reads every
self-hosted runner of ORG from GitHub with ``GH_TOKEN`` (the maintainer's own login, read
by ``setup/maintenance/runnerwatch/runner-watch.sh`` for this run only), through the host's proxy
(``https_proxy``, set by the script's wrapper). A runner belongs to the HOST that its name
or one of its labels is, or starts with ``HOST-`` (the longer where two match); it is
online when GitHub says so, busy or idle.

FILE keeps, per host, the number of consecutive checks that found no runner online and the
time of the last check; a check more than ``GAP`` after it starts the count again, and one
less than ``SOON`` after it (a start by hand) does not count again. At the
second consecutive check with none online, the watch opens ``No runner of <host> online``
in ``<ORG>/ci-runners``, or adopts an open issue with that title: at most one per outage.
The first check that finds a runner of the host online comments on every open issue with
the title and closes it. ``--report-only`` prints what it would do and writes nothing.

Status: 0 done; 1 the runners could not be read (nothing written), an issue call failed
(retried at the next tick) or FILE could not be written; 2 bad arguments or no login.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
import time
from collections.abc import Callable, Mapping

from ci_runners.workflow import github

API = 'https://api.github.com'
PAGE, PAGES = 100, 10
GAP = 40 * 60  # seconds: one missed quarter-hour tick still counts as consecutive, two do not
SOON = 5 * 60  # seconds: a check sooner after the last (a start by hand) does not count again
REPO = 'ci-runners'
HOST = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*\Z')
ORG = re.compile(r'[A-Za-z0-9][A-Za-z0-9-]*\Z')
MAX_TOKEN = 255


class WatchError(RuntimeError):
    pass


def title(host: str) -> str:
    return f'No runner of {host} online'


def stamp(t: float) -> str:
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')


def host_of(runner: Mapping, hosts: list[str]) -> str | None:
    """The host a runner belongs to: its name or a label is the host or starts with
    ``<host>-``; the longer host where two match; None for a runner of no listed host."""
    words = [runner.get('name') or '']
    words += [lab.get('name') or '' for lab in runner.get('labels') or [] if isinstance(lab, dict)]
    best = None
    for h in hosts:
        if any(w == h or w.startswith(h + '-') for w in words) and (best is None or len(h) > len(best)):
            best = h
    return best


def runners(call: Callable, org: str, token: str, base: str = API) -> list:
    """Every self-hosted runner of the org; an error rather than a partial list."""
    out: list = []
    for page in range(1, PAGES + 1):
        got = call('GET', f'{base}/orgs/{org}/actions/runners?per_page={PAGE}&page={page}', token)
        rows = got.get('runners') if isinstance(got, dict) else None
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            raise WatchError('the runners: not a list of runners')
        out += rows
        if len(rows) < PAGE:
            return out
    raise WatchError(f'the runners: more than {PAGES * PAGE}')


def open_issues(call: Callable, repo: str, token: str, base: str = API) -> list:
    """The repository's open issues, without pull requests; an error rather than a partial list."""
    out: list = []
    for page in range(1, PAGES + 1):
        got = call('GET', f'{base}/repos/{repo}/issues?state=open&per_page={PAGE}&page={page}', token)
        if not isinstance(got, list) or not all(isinstance(i, dict) for i in got):
            raise WatchError('the open issues: not a list')
        out += [i for i in got if 'pull_request' not in i]
        if len(got) < PAGE:
            return out
    raise WatchError(f'the open issues: more than {PAGES * PAGE}')


def read_state(path: str) -> dict:
    """Per host ``{'offline': n, 'last': t, 'alerted': bool}``; a missing or unreadable
    file is an empty one (the counts start again; the title still prevents a duplicate)."""
    try:
        with open(path, encoding='utf-8') as f:
            doc = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        print(f'runner-watch: the state is unreadable ({type(e).__name__}); starting again', file=sys.stderr)
        return {}
    hosts = doc.get('hosts') if isinstance(doc, dict) else None
    if not isinstance(hosts, dict):
        return {}
    out = {}
    for h, s in hosts.items():
        if isinstance(s, dict) and isinstance(s.get('offline'), int) and isinstance(s.get('last'), (int, float)):
            out[h] = {'offline': s['offline'], 'last': float(s['last']), 'alerted': s.get('alerted') is True}
    return out


def write_state(path: str, state: dict) -> None:
    tmp = f'{path}.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump({'version': 1, 'hosts': state}, f, indent=1, sort_keys=True)
        f.write('\n')
    os.replace(tmp, path)


def opening_body(host: str, rows: list, since: float, now: float) -> str:
    lines = [f'No runner of `{host}` was online at two consecutive checks of the runner watch '
             f'(design decision 15.6): {stamp(since)} and {stamp(now)}.', '',
             f'Runners of the host registered with GitHub: {len(rows)}.']
    lines += [f'- `{r.get("name")}`: {r.get("status")}' for r in sorted(rows, key=lambda r: str(r.get('name')))]
    lines += ['', "Where to look: the host's CI account (`systemctl --user status 'ci-runner@*' ci-hooks "
              "ci-tunnel` and its journal), `ci-run pool status`, and design §8.", '',
              'The watch closes this issue with a comment at the first check that finds a runner of the host online.']
    return '\n'.join(lines) + '\n'


def check(call: Callable, org: str, token: str, hosts: list[str], state: dict, now: float, report_only: bool,
          out=sys.stdout, base: str = API) -> tuple[dict, int]:
    """One check: the new state and the status. Raises on a failed read of the runners or
    of the open issues, before anything is written."""
    rows = runners(call, org, token, base)
    by_host: dict[str, list] = {h: [] for h in hosts}
    other = 0
    for r in rows:
        h = host_of(r, hosts)
        if h is None:
            other += 1
        else:
            by_host[h].append(r)
    repo = f'{org}/{REPO}'
    issues = open_issues(call, repo, token, base)
    new, status = {}, 0
    for h in hosts:
        prev = state.get(h, {'offline': 0, 'last': 0.0, 'alerted': False})
        online = sum(1 for r in by_host[h] if r.get('status') == 'online')
        mine = [i for i in issues if i.get('title') == title(h)]
        head = f'runner-watch: {h}: {online} of {len(by_host[h])} online'
        if online:
            new[h] = {'offline': 0, 'last': now, 'alerted': False}
            for i in mine:
                n = i.get('number')
                if report_only:
                    print(f'{head}; would close #{n}', file=out)
                    continue
                try:
                    call('POST', f'{base}/repos/{repo}/issues/{n}/comments', token,
                         {'body': f'{online} of {len(by_host[h])} runners of `{h}` online at the check of '
                                  f'{stamp(now)} (runner watch, design decision 15.6). Closing.'}, tries=1)
                    call('PATCH', f'{base}/repos/{repo}/issues/{n}', token,
                         {'state': 'closed', 'state_reason': 'completed'}, tries=1)
                    print(f'{head}; closed #{n}', file=out)
                except github.ApiError as e:
                    print(f'{head}; closing #{n} failed: {e}', file=sys.stderr)
                    status = 1
            if not mine:
                print(head, file=out)
            continue
        last = now
        if prev['offline'] > 0 and 0 <= now - prev['last'] < SOON:
            count, last = prev['offline'], prev['last']  # the same check again: neither counts nor moves
        elif prev['offline'] > 0 and now - prev['last'] <= GAP:
            count = prev['offline'] + 1
        else:
            count = 1
        alerted = prev['alerted']
        head += f'; none at {count} consecutive check{"s" if count > 1 else ""}'
        if count < 2 or alerted:
            print(head + ('; alerted' if alerted else ''), file=out)
        elif mine:
            alerted = True
            print(f'{head}; {"would adopt" if report_only else "adopted"} #{mine[0].get("number")}', file=out)
        elif report_only:
            print(f'{head}; would open "{title(h)}" in {repo}', file=out)
        else:
            try:
                got = call('POST', f'{base}/repos/{repo}/issues', token,
                           {'title': title(h), 'body': opening_body(h, by_host[h], prev['last'], now)}, tries=1)
                alerted = True
                print(f'{head}; opened #{got.get("number") if isinstance(got, dict) else "?"}', file=out)
            except github.ApiError as e:
                print(f'{head}; opening the issue failed: {e}', file=sys.stderr)
                status = 1
        new[h] = {'offline': count, 'last': last, 'alerted': alerted}
    if other:
        print(f'runner-watch: {other} runner{"s" if other > 1 else ""} of no listed host', file=out)
    return new, status


def main(args: list[str], environ: Mapping[str, str] = os.environ, call: Callable = github.api,
         now: Callable = time.time, out=sys.stdout, base: str = API) -> int:
    p = argparse.ArgumentParser(prog='ci-run runner-watch')
    p.add_argument('--org', required=True)
    p.add_argument('--state', required=True)
    p.add_argument('--report-only', action='store_true')
    p.add_argument('hosts', nargs='+')
    try:
        a = p.parse_args(args)
    except SystemExit:
        return 2
    bad = [h for h in a.hosts if not HOST.match(h)]
    if not ORG.match(a.org) or bad or len(set(a.hosts)) != len(a.hosts) or not os.path.isabs(a.state):
        print('runner-watch: ORG, a HOST or STATE is malformed (or a HOST is listed twice)', file=sys.stderr)
        return 2
    raw = environ.get('GH_TOKEN', '')
    if not raw or len(raw) > MAX_TOKEN or not raw.isascii() or any(c.isspace() for c in raw):
        print('runner-watch: no login (GH_TOKEN is empty or malformed)', file=sys.stderr)
        return 2
    state = read_state(a.state)
    t = now()
    try:
        new, status = check(call, a.org, raw, a.hosts, state, t, a.report_only, out, base)
    except (github.ApiError, WatchError) as e:
        print(f'runner-watch: {e}; nothing changed', file=sys.stderr)
        return 1
    if a.report_only:
        print('runner-watch: report only: no issue and no state written', file=out)
        return status
    try:
        write_state(a.state, new)
    except OSError as e:
        print(f'runner-watch: the state could not be written ({type(e).__name__})', file=sys.stderr)
        return 1
    return status
