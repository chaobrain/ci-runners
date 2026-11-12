"""The verdict of a run (design §4.8 rule 5).

``ci-run verdict --context C --sha S --parts "cpu gpu" --artifacts DIR`` reads the
parts' ``ci-part-<id>-<attempt>`` artifacts (``outcome.json``, ``summary.md``) that the
action downloaded into ``DIR``; an attempt an earlier run attempt left behind is dropped
(``current``). A part's last attempt decides. Where a part's artifacts stop, the
attempts after its last artifact are read from their records on this host
(``salvage``), as many as there are: a retry, and a consumer's own fallback after a
retry that did not start (attempt 3, issue #161), whatever the attempt before it said
in ``next``. A part with neither artifact nor record is ``lost``. The verdict is then
posted as the ``context`` status of ``S``, and the parts' summaries go to the job summary.

A run's artifacts outlive its run attempts, and ``actions/download-artifact`` keeps, of
the artifacts that share a name, the one of the highest id, which need not be the newest:
after a "Re-run failed jobs", a part's first attempt left one ``ci-part-<id>-1`` per run
attempt, and the earlier run attempt's red one won (issue #169). So before it reads them,
the verdict lists the run's artifacts with its token (``actions: read``) and, for each name
whose newest artifact is not the one downloaded, fetches the newest (``latest``): each part
counts its latest run attempt only. Without ``actions: read`` it says so and reads what was
downloaded, as before.

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
import http.client
import io
import json
import os
import re
import sys
import time
import urllib.request
import zipfile
from collections.abc import Callable, Iterable, Mapping

from ci_runners import github, inventory, part

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
    text = f'{head}: ' + ', '.join(f'{e.id} {e.outcome}' for e in ends)
    return text if len(text) <= STATUS_MAX else text[:STATUS_MAX - 3] + '...'


def collect(root: str) -> dict[str, dict[int, tuple[dict, str]]]:
    """The downloaded artifacts below ``root``, by part id and attempt:
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


def stale(listing: Iterable[Mapping]) -> dict[str, int]:
    """``{name: id}`` of the part artifacts whose newest artifact of that name in the run
    is not the one ``actions/download-artifact`` took. A name is made once per run attempt,
    so its newest artifact (by ``created_at``) is its latest run attempt's; download-artifact
    takes the highest id, and ids do not follow creation (issue #169: run attempt 2's
    ``ci-part-gpu-3-1`` had the lower id, and run attempt 1's red one was read). Expired
    artifacts and other names are left out. Pure."""
    by_name: dict[str, list[Mapping]] = {}
    for a in listing:
        if (isinstance(a, Mapping) and isinstance(a.get('name'), str) and ARTIFACT.fullmatch(a['name'])
                and type(a.get('id')) is int and isinstance(a.get('created_at'), str) and not a.get('expired')):
            by_name.setdefault(a['name'], []).append(a)
    out = {}
    for name, arts in by_name.items():
        newest = max(arts, key=lambda a: (a['created_at'], a['id']))
        if newest['id'] != max(a['id'] for a in arts):
            out[name] = newest['id']
    return out


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


def fetch_zip(url: str, token: str, opener: Callable | None = None) -> bytes:
    """An artifact's zip: the API answers with a redirect to the storage's signed URL,
    which gets no token (an unredirected header). At most MAX_ZIP bytes."""
    req = urllib.request.Request(url, headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'ci-runners',
                                               'X-GitHub-Api-Version': '2022-11-28'})
    req.add_unredirected_header('Authorization', f'Bearer {token}')
    with (opener or urllib.request.urlopen)(req, timeout=github.CALL_BOUND) as r:
        data = r.read(MAX_ZIP + 1)
    if len(data) > MAX_ZIP:
        raise ValueError(f'{url.split("?")[0]}: larger than {MAX_ZIP} bytes')
    return data


def unpack(root: str, name: str, data: bytes) -> None:
    """``outcome.json`` and ``summary.md`` of an artifact's zip ``data`` into ``root/name``,
    in place of what was there; nothing else of the zip is read. Both are read whole
    before anything is replaced, so a zip that cannot be read (raises ValueError) leaves
    the directory as it was."""
    got: dict[str, bytes] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for member in ('outcome.json', 'summary.md'):
                try:
                    info = z.getinfo(member)
                except KeyError:
                    continue
                if info.file_size <= MAX_FILE:
                    got[member] = z.read(info)
    except Exception as e:  # a bad zip, CRC, compression method, encryption: zipfile, zlib, ...
        raise ValueError(f'{name}: the zip cannot be read ({type(e).__name__})') from None
    d = os.path.join(root, name)
    if os.path.islink(d):
        os.unlink(d)
    os.makedirs(d, exist_ok=True)
    for member in ('outcome.json', 'summary.md'):
        path = os.path.join(d, member)
        if os.path.lexists(path):
            os.unlink(path)
        if member in got:
            with open(path, 'xb') as f:
                f.write(got[member])


def latest(root: str, call: Callable, fetch: Callable, base: str, repo: str, run_id: str, token: str) -> list[str]:
    """Replace each downloaded part artifact that is not its name's newest in the run by
    the newest (``stale``); the names replaced. Raises github.ApiError, OSError or
    ValueError, and then leaves the rest as downloaded."""
    done = []
    for name, aid in sorted(stale(run_artifacts(call, base, repo, run_id, token)).items()):
        try:
            data = fetch(f'{base}/repos/{repo}/actions/artifacts/{aid}/zip', token)
        except (OSError, http.client.HTTPException) as e:  # urllib's errors, a short read
            raise github.ApiError(f'GET artifact {aid}: {type(e).__name__}') from None
        unpack(root, name, data)
        done.append(name)
    return done


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


def main(argv: list[str], inv: inventory.Inventory, call: Callable | None = None,
         fetch: Callable | None = None) -> int:
    p = argparse.ArgumentParser(prog='ci-run verdict')
    p.add_argument('--context', required=True)
    p.add_argument('--sha', required=True)
    p.add_argument('--parts', default='')
    p.add_argument('--artifacts', required=True)
    p.add_argument('--wait-seconds', type=float, default=0)
    a = p.parse_args(argv)
    env = os.environ
    try:
        ids = parse_ids(a.parts)
        repo = env.get('GITHUB_REPOSITORY', '').lower()
        run = f'{env.get("GITHUB_RUN_ID", "")}-{env.get("GITHUB_RUN_ATTEMPT", "1")}'
        if not re.fullmatch(r'[0-9]+-[0-9]+', run) or not re.fullmatch(r'[a-z0-9-]+/[a-z0-9._-]+', repo):
            raise ValueError('GITHUB_RUN_ID, GITHUB_RUN_ATTEMPT or GITHUB_REPOSITORY is malformed')
        if not re.fullmatch(r'[A-Za-z0-9._/-]{1,100}', a.context) or not github.SHA.fullmatch(a.sha):
            raise ValueError('context or sha is malformed')

        def salvage(pid: str, attempt: int) -> dict | None:
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
            for name in latest(a.artifacts, call or github.api, fetch or fetch_zip, base, repo,
                               env['GITHUB_RUN_ID'], token):
                print(f'ci-run verdict: {name}: the latest run attempt\'s artifact, not the downloaded one',
                      file=sys.stderr)
        except (OSError, ValueError, github.ApiError) as e:
            hint = '; give the job actions: read' if 'HTTP 403' in str(e) or 'HTTP 404' in str(e) else ''
            print(f'ci-run verdict: the run\'s artifacts could not be listed or fetched ({e}): a part re-run in '
                  f'this run attempt may be read from an earlier one{hint}', file=sys.stderr)
        items = ends(ids, collect(a.artifacts), salvage)
        v = decide(items)
        (call or github.api)('POST', f'{base}/repos/{repo}/statuses/{a.sha}', token,
                             {'state': v.state, 'context': a.context, 'description': v.description,
                              'target_url': target})
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
               here: Callable[[str], Mapping[int, str]]) -> list[tuple[str, int, str]]:
    """``(id, attempt, ref)`` of the attempts this host salvages: the attempt after the
    part's last artifact, and each attempt after it, while this host has its record
    (``here(id)``). Pure but for ``here``."""
    out = []
    for pid in ids:
        refs = here(pid)
        n = next_attempt(found.get(pid, {}))
        while n in refs:
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
        todo = to_salvage(ids, collect(a.artifacts), lambda pid: records(root, repo, run, pid))
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
