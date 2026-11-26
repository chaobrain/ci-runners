"""The verdict of a run (design §4.8 rule 5).

``ci-run verdict --context C --sha S --parts "cpu gpu"`` reads the parts'
``ci-part-<id>-<attempt>`` artifacts (``outcome.json``, ``summary.md``) itself, through the
API with the job's token (``actions: read``), over one kept connection per host
(``client``; issue #242: no ``actions/download-artifact``). It lists the run's artifacts
and, of those that share a name (one per run attempt that made it), reads the newest: each
part counts its latest run attempt only, so a re-run of failed jobs can turn a run green
(issue #169); an attempt an earlier run attempt left behind is dropped (``current``). A
part's last attempt decides. Where a part's artifacts stop, the attempts after its last
artifact are read from their records on this host (``salvage``), as many as there are: a
retry, and a consumer's own fallback after a retry that did not start (attempt 3, issue
#161), whatever the attempt before it said in ``next``. A part with neither artifact nor
record is ``lost``, as is one whose zip cannot be fetched or read. A listing that fails
posts ``error`` with its cause first and no URL (``listing_error``, issue #250). The
verdict is then posted as the ``context`` status of ``S``, and the parts' summaries go to
the job summary.

``decide`` is pure. Only an infrastructure end the retry did not cure is ``error``; a
``red`` part fails the head whatever the others did (D6).

``ci-run salvage --parts "cpu gpu" --artifacts DIR --out DIR`` is the ``salvage`` action
(§4.5, decision 12.13), run on the light pool of each host that ran parts of a run whose
verdict runs elsewhere. For each part, the attempt after its last artifact (its first,
when it has none) and each attempt after that one, while this host has a record of this
run of it, gives ``<out>/ci-part-<id>-<attempt>/outcome.json`` with ``salvaged: true``, which the
action uploads. A part whose next attempt this host did not record is left alone; other
runs' records are never read.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import sys
import time
from collections.abc import Callable, Iterable, Mapping

from ci_runners import client, github, inventory, part
from ci_runners.artifacts import (KINDS, MAX_ATTEMPT, MAX_ZIP, _read, collect, current, members,
                                  newest, read, run_artifacts, run_attempt)

#: the outcomes that fail the head: the code is wrong, not the host
FAILING = frozenset({'red'})
#: the most parts one salvage job uploads: the action has this many upload steps
MAX_SALVAGED = 16
#: the zips fetched at once, each thread over its own kept connections (issue #242)
WORKERS = 8


@dataclasses.dataclass(frozen=True)
class End:
    """How one part ended: its last attempt's outcome, and where that came from."""
    id: str
    outcome: str
    reason: str = ''
    attempt: int = 1
    summary: str = ''


@dataclasses.dataclass(frozen=True)
class Verdict:
    state: str  # success, failure or error: the status the context gets
    description: str


#: GitHub refuses (HTTP 422) a commit status whose description is longer than this
STATUS_MAX = github.STATUS_MAX


def decide(ends: Iterable[End]) -> Verdict:
    """The head's status from its parts' ends. Pure.

    - no part: error (a workflow with neither command set tests nothing);
    - any part ``red``: failure, naming those parts;
    - every part ``pass``: success;
    - else error, naming the parts whose infrastructure end stands.
    """
    ends = sorted(ends, key=lambda e: e.id)
    if not ends:
        return Verdict('error', 'no part ran')
    bad = [e for e in ends if e.outcome in FAILING]
    if bad:
        return Verdict('failure', _describe('red', bad))
    infra = [e for e in ends if e.outcome != 'pass']
    if infra:
        return Verdict('error', _describe('infrastructure', infra))
    text = f'passed: {", ".join(e.id for e in ends)}'
    return Verdict('success', text if len(text) <= STATUS_MAX else f'passed: all {len(ends)} parts')


def _describe(head: str, ends: list[End]) -> str:
    return _cut(f'{head}: ' + ', '.join(f'{e.id} {e.outcome}' for e in ends))


def _cut(text: str) -> str:
    return github.status_text(text)


def next_attempt(attempts: Mapping[int, tuple[dict, str]]) -> int:
    """The attempt after a part's last artifact, read from the host's records when there
    is one: 1 when there is no artifact, else one more than the last. Not only a retry its
    last artifact asked for: a consumer's fallback job after a retry that did not start is
    attempt 3 of a part whose attempt 2 said ``next none`` (issue #161). Pure."""
    return max(attempts, default=0) + 1


def ends(ids: Iterable[str], found: Mapping[str, Mapping[int, tuple[dict, str]]],
         salvage: Callable[[str, int], dict | None]) -> list[End]:
    """Each expected part's end. ``salvage(id, attempt)`` reads an attempt's record, or
    gives None when there is none; the attempts after the last artifact are read while
    there are records of them. Pure but for ``salvage``."""
    out = []
    for pid in ids:
        attempts = dict(found.get(pid, {}))
        n = next_attempt(attempts)
        while n <= MAX_ATTEMPT:
            got = salvage(pid, n)
            if got is None:
                break
            attempts[n] = (got, '')
            n += 1
        if not attempts:
            attempts[1] = ({'outcome': 'lost', 'reason': 'no artifact and no record'}, '')
        last = max(attempts)
        o, summary = attempts[last]
        out.append(End(pid, str(o.get('outcome')), str(o.get('reason', '')), last, summary))
    return out


def parse_ids(text: str) -> list[str]:
    ids = text.split()
    for i in ids:
        if not part.ID.fullmatch(i):
            raise ValueError(f'part id {i!r} is malformed')
    if len(set(ids)) != len(ids):
        raise ValueError('a part id is given twice')
    return ids


def listing_error(e: github.ApiError) -> Verdict:
    """The verdict where the run's artifacts could not be listed (issue #250): ``error``,
    its cause first and no URL, ``verdict: artifacts not listed: HTTP 403 (the job needs
    actions: read)`` (the hint for a 403 or 404), ``...: HTTP 502``, ``...: TimeoutError``;
    the whole error, URL and all, goes to the job's log and summary. Pure."""
    hint = ' (the job needs actions: read)' if e.cause in ('HTTP 403', 'HTTP 404') else ''
    return Verdict('error', _cut(f'verdict: artifacts not listed: {e.cause}{hint}'))


def summary_md(v: Verdict, context: str, items: list[End], detail: str = '') -> str:
    """The job summary: the status's state and description, ``detail`` (the whole error
    the description shortens, where there is one) and each part's summary. Pure."""
    head = f'## {context}: {v.state}\n\n{v.description}\n\n' + (f'{detail}\n\n' if detail else '')
    parts = []
    for e in items:
        parts.append(e.summary or f'### part `{e.id}`\n\n{e.outcome} ({e.reason}), attempt {e.attempt}\n')
    return head + '\n'.join(parts)


def main(argv: list[str], inv: inventory.Inventory | None, call: Callable | None = None,
         fetch: Callable | None = None) -> int:
    p = argparse.ArgumentParser(prog='ci-run verdict')
    p.add_argument('--context', required=True)
    p.add_argument('--sha', required=True)
    p.add_argument('--parts', default='')
    p.add_argument('--wait-seconds', type=float, default=0)
    a = p.parse_args(argv)
    env = os.environ
    own = pool = None
    if call is None or fetch is None:  # kept connections, not one per request (issue #242)
        own, pool = client.Client(), client.Pool()
        call = call or own.api
        fetch = fetch or (lambda url, token: pool.artifact(url, token, MAX_ZIP))
    t0 = time.monotonic()
    try:
        return _verdict(a, env, inv, call, fetch)
    finally:
        if own is not None:
            spent: dict[str, float] = {}
            for c in (own, *pool.clients):
                for host, secs in c.seconds.items():
                    spent[host] = spent.get(host, 0.0) + secs
            print(f'ci-run verdict: {time.monotonic() - t0:.1f} s, {own.opened + sum(c.opened for c in pool.clients)} '
                  f'connection(s) over {1 + len(pool.clients)} client(s); requests by host (summed): '
                  + ', '.join(f'{h} {s:.1f} s' for h, s in sorted(spent.items())), file=sys.stderr)
            own.close()
            pool.close()


def _verdict(a: argparse.Namespace, env: Mapping[str, str], inv: inventory.Inventory | None, call: Callable,
             fetch: Callable) -> int:
    try:
        ids = parse_ids(a.parts)
        repo = env.get('GITHUB_REPOSITORY', '').lower()
        run = f'{env.get("GITHUB_RUN_ID", "")}-{env.get("GITHUB_RUN_ATTEMPT", "1")}'
        if not re.fullmatch(r'[0-9]+-[0-9]+', run) or not re.fullmatch(r'[a-z0-9-]+/[a-z0-9._-]+', repo):
            raise ValueError('GITHUB_RUN_ID, GITHUB_RUN_ATTEMPT or GITHUB_REPOSITORY is malformed')
        if not re.fullmatch(r'[A-Za-z0-9._/-]{1,100}', a.context) or not github.SHA.fullmatch(a.sha):
            raise ValueError('context or sha is malformed')

        def salvage(pid: str, attempt: int) -> dict | None:
            if inv is None:  # a public caller's verdict on a hosted runner: no record there (18.7)
                return None
            ref = f'{inv.this}:{repo}/{run}/{pid}-{attempt}'
            try:  # the record's own outputs only: nothing reaches this step's outputs
                return part.salvage(inv, ref, {'GITHUB_REPOSITORY': repo}, wait_seconds=a.wait_seconds)[1]
            except part.Refused:
                return None

        base = env.get('GITHUB_API_URL', 'https://api.github.com').rstrip('/')
        target = f'{env.get("GITHUB_SERVER_URL", "https://github.com")}/{repo}/actions/runs/{env.get("GITHUB_RUN_ID", "")}'
        token = env.get('GITHUB_TOKEN', '')
        if not token:
            raise ValueError('GITHUB_TOKEN is not set')
        from ci_runners import cli, storage, storage_v1
        internal = None
        try:  # each part's latest run attempt, not the artifact of the highest id (issue #169)
            internal = storage_v1.open_current(cli.etc_dir(), env)
            found = (internal.read_outcomes(ids) if internal is not None else
                     read(ids, call, fetch, base, repo, env['GITHUB_RUN_ID'], token,
                          lambda text: print(f'ci-run verdict: {text}', file=sys.stderr), WORKERS))
        except github.ApiError as e:  # nothing is known of any part: not a part's loss
            items, v, detail = [], listing_error(e), str(e)
            print(f'ci-run verdict: the run\'s artifacts could not be listed ({e})', file=sys.stderr)
        except storage.Error as e:
            items, v, detail = [], Verdict('error', 'internal files: ' + e.code), e.code
        else:
            items, detail = ends(ids, found, salvage), ''
            v = decide(items)
            if internal is not None and v.state == 'success':
                try:
                    gap = ('required standard outcome missing' if any(e.id not in found for e in items)
                           else internal.report_gap(found))
                except storage.Error as e:
                    gap = 'internal report evidence: ' + e.code
                if gap:
                    v = Verdict('error', gap)
        call('POST', f'{base}/repos/{repo}/statuses/{a.sha}', token,
             {'state': v.state, 'context': a.context, 'description': v.description, 'target_url': target})
    except (OSError, ValueError, github.ApiError) as e:
        print(f'ci-run verdict: {e}', file=sys.stderr)
        return 2
    if env.get('GITHUB_STEP_SUMMARY'):
        with open(env['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as f:
            f.write(summary_md(v, a.context, items, detail))
    if env.get('GITHUB_OUTPUT'):
        with open(env['GITHUB_OUTPUT'], 'a', encoding='utf-8') as f:
            f.write(f'state={v.state}\n')
    print(f'ci-run verdict: {v.state} ({v.description})', file=sys.stderr)
    return 1 if v.state != 'success' else 0


# ------------------------------------------------------------------ salvage (§4.5)


def records(parts_root: str, repo: str, run: str, pid: str) -> dict[int, str]:
    """``{attempt: ref}`` of part ``pid``'s attempts on this host in run ``run`` (``<run
    id>-<run attempt>``), every one of them; ``ref`` is ``<repo>/<run>/<id>-<attempt>``.
    Only this run's records are read: another run's part of the same id, older or newer,
    has its own."""
    d = os.path.join(parts_root, repo, run)
    try:
        names = os.listdir(d)
    except OSError:
        return {}
    name = re.compile(re.escape(pid) + r'-([1-9][0-9]?)\Z')
    out = {}
    for entry in names:
        m = name.fullmatch(entry)
        if not m or os.path.islink(os.path.join(d, entry)):
            continue
        doc = _read(os.path.join(d, entry, 'record.json'))
        try:
            rec = json.loads(doc) if doc is not None else None
        except ValueError:
            rec = None
        if isinstance(rec, dict) and rec.get('id') == pid:
            out[int(m.group(1))] = f'{repo}/{run}/{entry}'
    return out


def to_salvage(ids: Iterable[str], found: Mapping[str, Mapping[int, tuple[dict, str]]],
               here: Callable[[str], Mapping[int, str]], now: int = 0) -> list[tuple[str, int, str]]:
    """``(id, attempt, ref)`` of the attempts this host salvages: the attempt after the
    part's last artifact, and each attempt after it, while this host has its record
    (``here(id)``, this run attempt's records). Given ``now``, the run attempt, an
    artifact of an earlier run attempt (a re-run of failed jobs) does not hide this run
    attempt's record of the same attempt: a red first attempt whose runner died before
    its upload is salvaged, not shadowed by the earlier run's artifact of that name. Pure
    but for ``here``."""
    out = []
    for pid in ids:
        refs, have = here(pid), found.get(pid, {})
        done = {n for n, a in have.items() if not now or run_attempt(a[0]) in (now, 0)}
        last, n = max(have, default=0), 1
        while n in done or n in refs or n <= last:
            if n not in done and n in refs:
                out.append((pid, n, refs[n]))
            n += 1
    return out


def salvage_main(argv: list[str], inv: inventory.Inventory, *, systemctl: Callable | None = None,
                 poll: float = 10) -> int:
    p = argparse.ArgumentParser(prog='ci-run salvage')
    p.add_argument('--parts', required=True)
    p.add_argument('--artifacts', required=True)
    p.add_argument('--out', required=True)
    p.add_argument('--wait-seconds', type=float, default=0)
    a = p.parse_args(argv)
    env = os.environ
    try:
        ids = parse_ids(a.parts)
        repo = env.get('GITHUB_REPOSITORY', '').lower()
        run = f'{env.get("GITHUB_RUN_ID", "")}-{env.get("GITHUB_RUN_ATTEMPT", "1")}'
        if not re.fullmatch(r'[0-9]+-[0-9]+', run) or not re.fullmatch(r'[a-z0-9-]+/[a-z0-9._-]+', repo):
            raise ValueError('GITHUB_RUN_ID, GITHUB_RUN_ATTEMPT or GITHUB_REPOSITORY is malformed')
        if a.wait_seconds < 0:
            raise ValueError('wait-seconds is negative')
        root = os.path.join(inv.host['data'], 'ci-runners', 'state', 'parts')
        todo = to_salvage(ids, collect(a.artifacts), lambda pid: records(root, repo, run, pid), int(run.split('-')[1]))
        if len(todo) > MAX_SALVAGED:
            raise ValueError(f'{len(todo)} parts to salvage, more than the action uploads ({MAX_SALVAGED})')
        names = []
        deadline = time.monotonic() + a.wait_seconds  # one wait for the whole job, not one per part
        for pid, n, ref in todo:
            try:  # the record's own outputs only: nothing reaches this step's outputs
                got = part.salvage(inv, f'{inv.this}:{ref}', {'GITHUB_REPOSITORY': repo},
                                   wait_seconds=max(0.0, deadline - time.monotonic()), poll=poll,
                                   **({'run': systemctl} if systemctl else {}))[1]
            except part.Refused as e:
                print(f'ci-run salvage: {pid}: {e}', file=sys.stderr)
                continue
            name = f'ci-part-{pid}-{n}'
            d = os.path.join(a.out, name)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, 'outcome.json'), 'w', encoding='utf-8') as f:
                json.dump({**got, 'salvaged': True}, f, sort_keys=True)
            names.append(name)
            print(f'ci-run salvage: {pid} attempt {n}: {got.get("outcome")}', file=sys.stderr)
    except (OSError, ValueError) as e:
        print(f'ci-run salvage: {e}', file=sys.stderr)
        return 2
    if env.get('GITHUB_OUTPUT'):
        with open(env['GITHUB_OUTPUT'], 'a', encoding='utf-8') as f:
            f.write(f'count={len(names)}\n')
            for i, name in enumerate(names, 1):
                f.write(f'name-{i}={name}\n')
    print(f'ci-run salvage: {len(names)} part(s) salvaged on {inv.this}', file=sys.stderr)
    return 0
