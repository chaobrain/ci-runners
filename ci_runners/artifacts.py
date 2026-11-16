"""Read standard CI part artifacts without the host inventory.

The verdict and salvage use the same readers as the consumer CLI. Repository-specific
artifacts and decisions about missing outcomes stay with their callers.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import http.client
import io
import json
import os
import re
import zipfile
from collections.abc import Callable, Iterable, Mapping

from ci_runners import github


#: the outcomes a part's ``outcome.json`` may name (design §4.4)
KINDS = frozenset({'pass', 'red', 'oom', 'thermal', 'reboot', 'unavailable', 'lost', 'stopped'})
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


def collect(root: str, *, history: bool = False) -> dict[str, dict[int, tuple[dict, str]]]:
    """The salvage action's downloaded artifacts below ``root``, by part id and attempt:
    ``(outcome.json, summary.md)``. A directory whose name is not an artifact's, or whose
    ``outcome.json`` is not an object with a known outcome, is skipped.

    With ``history``, keep older attempts and directories without a readable outcome.
    Reports use them to associate project-specific logs with the original attempt.
    A missing or unreadable outcome is represented by an empty dict.
    """
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
        if not history and (not isinstance(o, dict) or not isinstance(o.get('outcome'), str) or o['outcome'] not in KINDS):
            continue
        if not isinstance(o, dict):
            o = {}
        out.setdefault(m.group(1), {})[int(m.group(2))] = (o, _read(os.path.join(d, 'summary.md')) or '')
    if history:
        return out
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
    if not isinstance(o, dict) or not isinstance(o.get('outcome'), str) or o['outcome'] not in KINDS:
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


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog='ci-run artifacts', description='Read downloaded CI part artifacts.')
    parser.add_argument('directory', help='directory containing ci-part-<id>-<attempt> folders')
    args = parser.parse_args(argv)
    parts = {
        part_id: {attempt: {'outcome': outcome, 'summary': summary}
                  for attempt, (outcome, summary) in attempts.items()}
        for part_id, attempts in collect(args.directory).items()
    }
    print(json.dumps({'schema': 1, 'parts': parts}, sort_keys=True))
    return 0
