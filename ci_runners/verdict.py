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
posts ``error`` and names it. The verdict is then posted as the ``context`` status of
``S``, and the parts' summaries go to the job summary.

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
import concurrent.futures
import dataclasses
import http.client
import io
import json
import os
import re
import sys
import time
import zipfile
from collections.abc import Callable, Iterable, Mapping

from ci_runners import client, github, inventory, part

#: the outcomes a part's ``outcome.json`` may name (design §4.4)
KINDS = frozenset({'pass', 'red', 'oom', 'thermal', 'reboot', 'unavailable', 'lost', 'stopped'})
#: the outcomes that fail the head: the code is wrong, not the host
FAILING = frozenset({'red'})
#: the most parts one salvage job uploads: the action has this many upload steps
MAX_SALVAGED = 16
#: the highest attempt read: an artifact's ``ci-part-<id>-<n>`` has two digits. A part has
#: its first attempt and one retry (design §4.4), and a consumer may add its own fallback
#: after a retry that did not start (attempt 3, issue #161); every one is read.
MAX_ATTEMPT = 99
ARTIFACT = re.compile(r'ci-part-([A-Za-z0-9][A-Za-z0-9_.-]{0,63})-([0-9]{1,2})\Z')
#: a record's run attempt: ``…/<run id>-<run attempt>/<id>-<n>`` at the end of ``record``
RECORD_RUN = re.compile(r'/[0-9]+-([0-9]+)/[^/]+-[0-9]{1,2}\Z')
MAX_FILE = 1 << 20
#: the largest part artifact (zipped) the verdict fetches itself
MAX_ZIP = 8 << 20
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
STATUS_MAX = 140


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
    return text if len(text) <= STATUS_MAX else text[:STATUS_MAX - 3] + '...'


def collect(root: str) -> dict[str, dict[int, tuple[dict, str]]]:
    """The salvage action's downloaded artifacts below ``root``, by part id and attempt:
    ``(outcome.json, summary.md)``. A directory whose name is not an artifact's, or whose
    ``outcome.json`` is not an object with a known outcome, is skipped."""
    out: dict[str, dict[int, tuple[dict, str]]] = {}
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return out
    for name in names:
        m = ARTIFACT.fullmatch(name)
        d = os.path.join(root, name)
        if not m or os.path.islink(d) or not os.path.isdir(d):
            continue
        doc = _read(os.path.join(d, 'outcome.json'))
        try:
            o = json.loads(doc) if doc is not None else None
        except ValueError:
            o = None
        if not isinstance(o, dict) or o.get('outcome') not in KINDS:
            continue
        out.setdefault(m.group(1), {})[int(m.group(2))] = (o, _read(os.path.join(d, 'summary.md')) or '')
    return {pid: current(attempts) for pid, attempts in out.items()}


def run_attempt(outcome: Mapping) -> int:
    """The run attempt (``GITHUB_RUN_ATTEMPT``) an outcome was made in: its
    ``run-attempt`` (every outcome from ``v1.5.2``, also one without a record), else its
    ``record`` (``<host>:<repo>/<run id>-<run attempt>/<id>-<n>``); 0 when neither names
    one. Pure."""
    v = outcome.get('run-attempt')
    if isinstance(v, int) and not isinstance(v, bool) and v > 0:
        return v
    m = RECORD_RUN.search(str(outcome.get('record', '')))
    return int(m.group(1)) if m else 0


def current(attempts: Mapping[int, tuple[dict, str]]) -> dict[int, tuple[dict, str]]:
    """A part's attempts without those an earlier run attempt left behind. A run's
    artifacts outlive its run attempts, and their names carry only the part's attempt:
    after a GitHub re-run of a part's first attempt, the earlier run attempt's retry
    would otherwise outrank it (issue #64). So the chain starts at the first attempt,
    and a later attempt counts only if it was made in that run attempt or a later one.
    Pure."""
    first = run_attempt(attempts[1][0]) if 1 in attempts else 0
    return {n: a for n, a in attempts.items() if n == 1 or run_attempt(a[0]) >= first}


def newest(listing: Iterable[Mapping], ids: Iterable[str]) -> dict[str, int]:
    """``{name: id}``: of the run's artifacts named ``ci-part-<id>-<attempt>`` for a part of
    ``ids``, the newest of each name (by ``created_at``, then id). A name is made once per
    run attempt, so its newest artifact is its latest run attempt's: each part counts its
    latest run attempt only, and a re-run of failed jobs can turn a run green (issue #169:
    run attempt 2's ``ci-part-gpu-3-1`` had the lower id). Expired artifacts, other names
    and malformed entries are left out. Pure."""
    want = set(ids)
    best: dict[str, Mapping] = {}
    for a in listing:
        if not (isinstance(a, Mapping) and isinstance(a.get('name'), str) and type(a.get('id')) is int
                and isinstance(a.get('created_at'), str) and not a.get('expired')):
            continue
        m = ARTIFACT.fullmatch(a['name'])
        if not m or m.group(1) not in want or not 1 <= int(m.group(2)) <= MAX_ATTEMPT:
            continue
        old = best.get(a['name'])
        if old is None or (a['created_at'], a['id']) > (old['created_at'], old['id']):
            best[a['name']] = a
    return {name: a['id'] for name, a in best.items()}


def run_artifacts(call: Callable, base: str, repo: str, run_id: str, token: str) -> list:
    """The run's artifacts as the API lists them, up to ``github.PAGES`` pages."""
    out: list = []
    for page in range(1, github.PAGES + 1):
        got = call('GET', f'{base}/repos/{repo}/actions/runs/{run_id}/artifacts?per_page={github.PAGE}&page={page}',
                   token)
        arts = got.get('artifacts') if isinstance(got, dict) else None
        if not isinstance(arts, list):
            raise github.ApiError('GET artifacts: not a list')
        out += arts
        if len(arts) < github.PAGE:
            break
    return out


def members(name: str, data: bytes) -> tuple[dict, str] | None:
    """``(outcome.json, summary.md)`` of an artifact's zip ``data``, read in memory; nothing
    else of the zip is read and nothing is written. None where its ``outcome.json`` is
    missing, larger than MAX_FILE, not UTF-8 or not an object with a known outcome (as a
    downloaded one was skipped); a summary that is larger, or not UTF-8, is left out.
    Raises ValueError for a zip that cannot be read."""
    got: dict[str, str] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for member in ('outcome.json', 'summary.md'):
                try:
                    info = z.getinfo(member)
                except KeyError:
                    continue
                if info.file_size > MAX_FILE:
                    continue
                raw = z.read(info)  # its declared size at most: zipfile reads no more, and checks the CRC
                try:
                    got[member] = raw.decode('utf-8')
                except UnicodeDecodeError:
                    pass
    except Exception as e:  # a bad zip, CRC, compression method, encryption: zipfile, zlib, ...
        raise ValueError(f'{name}: the zip cannot be read ({type(e).__name__})') from None
    try:
        o = json.loads(got['outcome.json']) if 'outcome.json' in got else None
    except ValueError:
        o = None
    if not isinstance(o, dict) or o.get('outcome') not in KINDS:
        return None
    return o, got.get('summary.md', '')


def read(ids: Iterable[str], call: Callable, fetch: Callable, base: str, repo: str, run_id: str, token: str,
         log: Callable[[str], None], workers: int = 1) -> dict[str, dict[int, tuple[dict, str]]]:
    """The parts' attempts, by part id and attempt, read through the API (issue #242): the
    run's listing, then each part artifact's newest zip (``newest``), as ``collect`` reads
    a download. An artifact whose zip cannot be fetched or read, or whose outcome is not
    valid, is left out, and is then read from this host's records, else ``lost``; an older
    artifact of its name is never read in its place. ``workers`` zips are fetched at once
    (``fetch`` is then called from as many threads). Raises github.ApiError where the
    listing fails."""
    todo = sorted(newest(run_artifacts(call, base, repo, run_id, token), ids).items())

    def one(item: tuple[str, int]) -> tuple[dict, str] | str:
        name, aid = item
        try:
            got = members(name, fetch(f'{base}/repos/{repo}/actions/artifacts/{aid}/zip', token))
        except (OSError, ValueError, http.client.HTTPException, github.ApiError) as e:
            return f'{name}: not read ({e})'
        return f'{name}: its outcome.json is missing or not valid' if got is None else got
    with concurrent.futures.ThreadPoolExecutor(max(1, min(workers, len(todo)))) as pool:
        results = list(pool.map(one, todo))
    out: dict[str, dict[int, tuple[dict, str]]] = {}
    for (name, _), got in zip(todo, results):
        if isinstance(got, str):
            log(got)
            continue
        m = ARTIFACT.fullmatch(name)
        out.setdefault(m.group(1), {})[int(m.group(2))] = got
    return {pid: current(attempts) for pid, attempts in out.items()}


def _read(path: str) -> str | None:
    try:
        if os.path.islink(path):
            return None
        with open(path, encoding='utf-8') as f:
            text = f.read(MAX_FILE + 1)
    except (OSError, UnicodeDecodeError):
        return None
    return text if len(text) <= MAX_FILE else None


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


def summary_md(v: Verdict, context: str, items: list[End]) -> str:
    head = f'## {context}: {v.state}\n\n{v.description}\n\n'
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
        try:  # each part's latest run attempt, not the artifact of the highest id (issue #169)
            found = read(ids, call, fetch, base, repo, env['GITHUB_RUN_ID'], token,
                         lambda text: print(f'ci-run verdict: {text}', file=sys.stderr), WORKERS)
        except github.ApiError as e:  # nothing is known of any part: not a part's loss
            hint = '; the job needs actions: read' if 'HTTP 403' in str(e) or 'HTTP 404' in str(e) else ''
            items, v = [], Verdict('error', _cut(f'verdict: the run\'s artifacts could not be listed ({e}){hint}'))
        else:
            items = ends(ids, found, salvage)
            v = decide(items)
        call('POST', f'{base}/repos/{repo}/statuses/{a.sha}', token,
             {'state': v.state, 'context': a.context, 'description': v.description, 'target_url': target})
    except (OSError, ValueError, github.ApiError) as e:
        print(f'ci-run verdict: {e}', file=sys.stderr)
        return 2
    if env.get('GITHUB_STEP_SUMMARY'):
        with open(env['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as f:
            f.write(summary_md(v, a.context, items))
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
