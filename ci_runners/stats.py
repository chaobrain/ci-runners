"""``ci-run stats``: how long work waits and runs on the runners (design decision 16.8, RUN-10 R7).

``ci-run stats [--repo R ...] [--since T | --run ID ...] [--records DIR ...] [--no-artifacts] [--json]``
reads, with the caller's own login (``GH_TOKEN`` in its environment, as the runner watch, 15.6;
the tool reads, stores and refreshes no token itself), the workflow runs of each repository
created since T (``24h`` by default; ``90m``, ``7d`` or an ISO time; ``--run``: those runs of
one repository) and reports per label and per repository. The repositories default to those of
the project registry (``<etc>/projects/*.toml``). The numbers, each from these timestamps:

- the *queue delay* of each job a runner took: its ``started_at`` − its ``created_at``;
- its *run time*: its ``completed_at`` − its ``started_at`` (completed jobs);
- per repository, a run's *wall time*: the latest ``completed_at`` of its jobs that were not
  skipped − the run's ``created_at`` (completed runs), and its *verdict delay*: the
  ``completed_at`` of the run's ``verdict`` job (the job that posts the final status, its last
  step; of the latest run attempt that has one) − the head's push, which is the repository's
  activity entry for the head sha (``timestamp``) at most ``PUSH_WINDOW`` before the run's
  ``created_at`` (or up to a minute after it) on a ``push``, ``pull_request`` or ``merge_group``
  run, else the run's ``created_at`` (a dispatch, a schedule, a push not found or the activity
  not readable: ``verdict_from`` counts each);

each as a count, p50, p90 and max in seconds (``quantile``); a difference below 0 (clock skew)
is left out and counted as ``negative``. A job a runner never took (cancelled in the queue, or
still queued: no ``started_at`` or no runner) is counted as *unstarted* and is in no delay; a
skipped job is in nothing. Jobs of every run attempt count (``filter=all``). A job's label is
its ``labels`` but ``self-hosted``, joined by ``+`` (``ci-cpu+mem-big``, ``hosta-gpu1``). The
*part attempts* (``first``, ``retry`` and ``fallback`` jobs) have rows of their own.

Two run shapes are read. A job's name is split at `` / ``; its last segment names its kind: the
reusable workflow's (16.6) ``<caller> / part <id> / first|locate|salvage|retry|red`` and
``<caller> / plan|pending|verdict``, a composing workflow's ``part <id> / first|where|salvage|
retry|fallback``, ``verdict``, or a flat ``<part>`` with ``<part>-retry`` or ``<part>-fallback``.

*Retries per row* (16.5): each retry or fallback attempt a runner took, by the row the first
attempt's outcome gives (``row``): that outcome comes from a part record under ``--records``, else
from the run's artifact ``ci-part-<id>-1`` (its ``outcome.json``; ``--no-artifacts`` skips them),
else a first attempt whose host had to be found (a ``locate``/``where`` or ``salvage`` job ran),
or that was cancelled, counts as ``any``, and a ``fallback`` job as ``fallback``; any other is
``unknown``. *Refusals after a start*: the outcomes read that are ``unavailable`` (the executor
refused a job its runner had started), by the reason's head: from the API the retried parts'
first attempts, with ``--records`` every attempt recorded on that host. *Presence* (16.4, 19): with
``--records DIR`` (a host's data directory, ``<DIR>/ci-runners``), its log lines since T: stops by
cause (the heads of its causes, card numbers dropped, joined by ``+``), starts by the cause of the
runner's previous stop in the window (else as logged: ``no cause``, ``presence is off``), the
stops a starting job prevented (``kept``), the holds set (19.8), infrastructure ends and
self-drains; the records of parts whose ``outcome.json`` was written in the window.

API use, each call 30 s and 3 tries: per repository one page of 100 runs per 100 runs, one page of
jobs per run (per 100 jobs), one page of activity per 100 pushes since T, and per run with a
retried part one artifact list plus one download per retried part (two requests: GitHub's
redirect, then the signed URL, which gets no login). A day of one repository with R runs, P
pushes and N retried parts in K runs: ceil(R/100) + R + ceil(P/100) + K + 2N requests (jobs
pages of 100: a run of more than 100 jobs takes more). At most ``--max-requests``
(``MAX_REQUESTS``) calls: beyond them the report is refused, not shortened. GitHub's run search
returns at most 1000 runs: more is refused too.

Status: 0 the report; 1 GitHub could not be read or a bound was reached; 2 bad arguments or no login.
"""
from __future__ import annotations

import argparse
import datetime
import io
import json
import os
import re
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable, Iterable, Mapping, Sequence

from ci_runners import github

API = 'https://api.github.com'
PAGE = 100
MAX_REQUESTS = 2000
MAX_RUNS = 1000  # GitHub's run search returns no more
MAX_ARTIFACT = 1 << 20  # bytes of one ci-part artifact's zip (an outcome is a few hundred)
MAX_BODY = 32 << 20  # bytes of one answer
PUSH_WINDOW = 10 * 60  # s: a push longer before the run is not its trigger (a PR opened later)
PUSH_EVENTS = ('push', 'pull_request', 'merge_group')
REPO = re.compile(r'[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}\Z')
SINCE = re.compile(r'([1-9][0-9]{0,4})([mhd])\Z')
NEXT = re.compile(r'<([^>]+)>\s*;\s*rel="next"')
#: a job's kind by the last segment of its name
ATTEMPTS = ('first', 'retry', 'fallback')
HOST_FINDERS = ('locate', 'where', 'salvage')
PART_KINDS = ATTEMPTS + HOST_FINDERS + ('red',)
ROWS = ('cool-card', 'same-host', 'more-memory', 'any', 'fallback', 'unknown')
GPU_LABEL = re.compile(r'.+-gpu[0-9]+\Z')
MAX_LOG = 64 << 20  # bytes read of one presence log


class StatsError(RuntimeError):
    pass


# ------------------------------------------------------------------ GitHub, paginated and bounded

def transport(url: str, headers: Mapping[str, str]) -> tuple[int, Mapping[str, str], bytes]:
    """One GET, redirects not followed (a download's redirect leaves GitHub: its URL is
    signed and gets no Authorization): ``(status, headers, body)``."""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    opener = urllib.request.build_opener(NoRedirect)
    req = urllib.request.Request(url, headers=dict(headers), method='GET')
    try:
        with opener.open(req, timeout=github.CALL_BOUND) as r:
            return r.status, dict(r.headers), r.read(MAX_BODY + 1)
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), b''


class Api:
    """GET with the caller's login, ``github.TRIES`` tries a call, at most ``limit`` calls."""

    def __init__(self, token: str, base: str = API, send: Callable = transport, limit: int = MAX_REQUESTS,
                 pause: float = 5.0) -> None:
        self.token, self.base, self.send, self.limit, self.pause = token, base.rstrip('/'), send, limit, pause
        self.requests = 0

    def _get(self, url: str, auth: bool = True) -> tuple[int, Mapping[str, str], bytes]:
        last = ''
        for i in range(github.TRIES):
            if self.requests >= self.limit:
                raise StatsError(f'more than {self.limit} requests (--max-requests; or a shorter --since)')
            self.requests += 1
            headers = {'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28',
                       'User-Agent': 'ci-runners'}
            if auth:
                headers['Authorization'] = f'Bearer {self.token}'
            try:
                code, h, body = self.send(url, headers)
            except (OSError, ValueError) as e:
                code, h, body, last = 0, {}, b'', type(e).__name__
            if 200 <= code < 400:
                return code, h, body
            if code:
                last = f'HTTP {code}'
                if code < 500 and code != 429:
                    break
            if i + 1 < github.TRIES:
                time.sleep(self.pause)
        raise StatsError(f'GET {url.split("?")[0]}: {last}')

    def pages(self, path: str, key: str | None, max_pages: int) -> Iterable[list]:
        """Each page's list (``key`` of the page's object, or the page itself), following
        the ``Link: rel="next"`` header GitHub gives (page numbers and cursors alike)."""
        url = f'{self.base}{path}'
        for _ in range(max_pages):
            _, h, body = self._get(url)
            try:
                doc = json.loads(body or b'null')
            except ValueError:
                raise StatsError(f'GET {path.split("?")[0]}: not JSON') from None
            rows = doc.get(key) if key and isinstance(doc, dict) else doc
            if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
                raise StatsError(f'GET {path.split("?")[0]}: not a list')
            yield rows
            m = NEXT.search({k.lower(): v for k, v in h.items()}.get('link', ''))
            if not m or not rows:
                return
            url = m.group(1)
        raise StatsError(f'GET {path.split("?")[0]}: more than {max_pages} pages')

    def one(self, path: str) -> dict:
        """One object."""
        _, _, body = self._get(f'{self.base}{path}')
        try:
            doc = json.loads(body or b'null')
        except ValueError:
            doc = None
        if not isinstance(doc, dict):
            raise StatsError(f'GET {path.split("?")[0]}: not an object')
        return doc

    def blob(self, path: str) -> bytes:
        """A download: GitHub's redirect, then the signed URL without the login."""
        code, h, body = self._get(f'{self.base}{path}')
        if 300 <= code < 400:
            loc = {k.lower(): v for k, v in h.items()}.get('location', '')
            if not loc.startswith('https://'):
                raise StatsError(f'GET {path}: a redirect without an https location')
            code, h, body = self._get(loc, auth=False)
        if len(body) > MAX_ARTIFACT:
            raise StatsError(f'GET {path}: larger than {MAX_ARTIFACT} bytes')
        return body


def iso(t: float) -> str:
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def ts(v: object) -> float | None:
    """A GitHub time (``2026-11-12T09:41:25Z``) as epoch seconds; None for none."""
    if not isinstance(v, str) or not v:
        return None
    try:
        return datetime.datetime.fromisoformat(v.replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def collect(api: Api, repo: str, since: float, *, artifacts: bool = True, outcomes: Mapping | None = None,
            run_ids: Sequence[int] = ()) -> dict:
    """A repository's runs created since ``since`` (or the runs ``run_ids``), each with every
    attempt's jobs, the repository's pushes in reach, and the first attempts' outcomes of its
    retried parts from their artifacts (where ``outcomes``, the records', has none)."""
    runs: list[dict] = []
    if run_ids:
        for rid in run_ids:
            got = api.one(f'/repos/{repo}/actions/runs/{rid}')
            if not isinstance(got.get('id'), int):
                raise StatsError(f'GET run {rid}: not a run')
            runs.append(got)
        since = min(ts(r.get('created_at')) or since for r in runs)
    else:
        q = urllib.parse.quote(f'>={iso(since)}')
        for page in api.pages(f'/repos/{repo}/actions/runs?per_page={PAGE}&created={q}', 'workflow_runs',
                              MAX_RUNS // PAGE + 1):
            runs += page
            if len(runs) > MAX_RUNS:
                raise StatsError(f'{repo}: more than {MAX_RUNS} runs since {iso(since)} (a shorter --since)')
    for r in runs:
        r['jobs'] = [j for page in api.pages(f'/repos/{repo}/actions/runs/{r["id"]}/jobs?per_page={PAGE}&filter=all',
                                             'jobs', 50) for j in page]
    pushes: list[dict] | None = []
    if any(r.get('event') in PUSH_EVENTS for r in runs):
        horizon = since - PUSH_WINDOW
        try:
            for page in api.pages(f'/repos/{repo}/activity?per_page={PAGE}&direction=desc', None, 100):
                pushes += page
                if any((ts(p.get('timestamp')) or 0) < horizon for p in page):
                    break
        except StatsError:
            if api.requests >= api.limit:
                raise
            pushes = None  # no activity to read (a login without it): every verdict delay from the run's creation
    found: dict = {}
    if artifacts:
        for r in runs:
            want = retried(r['jobs']) - {(att, pid) for (rid, att, pid) in (outcomes or {}) if rid == r['id']}
            if not want:
                continue
            arts = [a for page in api.pages(f'/repos/{repo}/actions/runs/{r["id"]}/artifacts?per_page={PAGE}',
                                            'artifacts', 10) for a in page]
            for a in arts:
                m = re.fullmatch(r'ci-part-(.+)-1', str(a.get('name', '')))
                if not m or m.group(1) not in {pid for _a, pid in want} or a.get('expired'):
                    continue
                doc = outcome_of_zip(api.blob(f'/repos/{repo}/actions/artifacts/{a["id"]}/zip'))
                if doc is not None:
                    att = int(doc.get('run-attempt') or 1) if str(doc.get('run-attempt') or '1').isdigit() else 0
                    found[(r['id'], att, m.group(1))] = doc
    return {'repo': repo, 'runs': runs, 'pushes': pushes, 'outcomes': found, 'since': since}


def outcome_of_zip(data: bytes) -> dict | None:
    """The ``outcome.json`` of a part's artifact, or None."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for n in z.namelist():
                if n.rsplit('/', 1)[-1] == 'outcome.json':
                    with z.open(n) as f:
                        doc = json.loads(f.read(MAX_ARTIFACT))
                    return doc if isinstance(doc, dict) else None
    except (zipfile.BadZipFile, ValueError, OSError, KeyError):
        return None
    return None


# ------------------------------------------------------------------ the jobs: shapes and kinds

def kind(name: str) -> tuple[str, str]:
    """``(part, kind)`` of a job's name: ``part`` the name without its kind (a part's jobs
    share it), ``kind`` one of PART_KINDS, ``verdict`` or ``other``. Pure."""
    segs = [s.strip() for s in name.split(' / ')]
    last = segs[-1]
    if len(segs) >= 2 and last in PART_KINDS:
        return ' / '.join(segs[:-1]), last
    for k in ('retry', 'fallback'):
        if last.endswith('-' + k) and len(last) > len(k) + 1:
            return ' / '.join(segs[:-1] + [last[:-len(k) - 1]]), k
    if last == 'verdict':
        return '', 'verdict'
    return name.strip(), 'other'


def part_id(part: str) -> str:
    """The plan's id of a part from its jobs' shared name: ``part <id>``, ``part (<id>)`` or
    the flat ``<id>``, after the caller's segments. Pure."""
    last = part.split(' / ')[-1].strip()
    if last.startswith('part ') or last.startswith('part('):
        last = last[4:].strip()
    return last[1:-1].strip() if last.startswith('(') and last.endswith(')') else last


def took(j: Mapping) -> bool:
    """A runner took the job: it has a ``started_at`` and a runner, and was not skipped."""
    return j.get('conclusion') != 'skipped' and ts(j.get('started_at')) is not None and bool(
        j.get('runner_name') or j.get('runner_id'))


def labels_of(j: Mapping) -> str:
    """The label key of a job: its ``runs-on`` labels but ``self-hosted``, joined by ``+``."""
    got = [str(x) for x in j.get('labels') or [] if x != 'self-hosted']
    return '+'.join(got) if got else ('self-hosted' if j.get('labels') else '-')


def jobs_by_part(jobs: Sequence[Mapping]) -> dict[tuple[int, str], dict[str, list]]:
    """``{(run attempt, part): {kind: [jobs]}}``; a flat ``<part>`` job is that part's
    ``first`` once a ``<part>-retry`` or ``-fallback`` names it. Pure."""
    out: dict[tuple[int, str], dict[str, list]] = {}
    for j in jobs:
        p, k = kind(str(j.get('name', '')))
        if k in PART_KINDS:
            out.setdefault((int(j.get('run_attempt') or 1), p), {}).setdefault(k, []).append(j)
    for j in jobs:
        p, k = kind(str(j.get('name', '')))
        key = (int(j.get('run_attempt') or 1), p)
        if k == 'other' and key in out and 'first' not in out[key]:
            out[key]['first'] = [j]
    return out


def retried(jobs: Sequence[Mapping]) -> set[tuple[int, str]]:
    """``(run attempt, part id)`` of every part with a retry or fallback a runner took. Pure."""
    return {(a, part_id(p)) for (a, p), ks in jobs_by_part(jobs).items()
            if any(took(j) for k in ('retry', 'fallback') for j in ks.get(k, []))}


def row(kind_: str, doc: Mapping | None, first: Sequence[Mapping], finders: Sequence[Mapping]) -> str:
    """The row of 16.5 a retry attempt took: from the first attempt's outcome ``doc``, else
    what its jobs show. Pure."""
    if kind_ == 'fallback':
        return 'fallback'
    if doc:
        o, on = doc.get('outcome'), [str(x) for x in doc.get('retry-on') or []]
        if o == 'thermal':
            return 'cool-card' if doc.get('cool-card') or any(GPU_LABEL.fullmatch(x) and not x.startswith('ci-')
                                                              for x in on) else 'fallback'
        if o == 'reboot':
            return 'same-host'
        if o == 'oom':
            return 'more-memory'
        if o in ('lost', 'stopped'):
            return 'any'
        if o == 'unavailable':
            return 'same-host' if doc.get('next') == 'retry' else 'fallback'
        return 'unknown'
    if any(took(j) for j in finders) or any(j.get('conclusion') == 'cancelled' for j in first):
        return 'any'
    return 'unknown'


# ------------------------------------------------------------------ the numbers

def quantile(values: Sequence[float], p: float) -> float | None:
    """The value at index ``round(p × (n − 1))`` of the sorted values (Python's ``round``,
    half to even); None for none. Pure."""
    if not values:
        return None
    v = sorted(values)
    return v[int(round(p * (len(v) - 1)))]


def summary(values: Sequence[float]) -> dict:
    return {'n': len(values), 'p50': quantile(values, 0.5), 'p90': quantile(values, 0.9),
            'max': max(values) if values else None}


class Acc:
    def __init__(self) -> None:
        self.queue: list[float] = []
        self.run: list[float] = []
        self.jobs = 0
        self.negative = 0

    def add(self, j: Mapping) -> None:
        self.jobs += 1
        c, s, e = ts(j.get('created_at')), ts(j.get('started_at')), ts(j.get('completed_at'))
        if c is not None and s is not None:
            if s - c >= 0:
                self.queue.append(s - c)
            else:
                self.negative += 1
        if s is not None and e is not None and j.get('status') == 'completed':
            if e - s >= 0:
                self.run.append(e - s)
            else:
                self.negative += 1

    def out(self) -> dict:
        d = {'jobs': self.jobs, 'queue': summary(self.queue), 'run': summary(self.run)}
        if self.negative:
            d['negative'] = self.negative
        return d


def push_time(run: Mapping, pushes: Sequence[Mapping]) -> float | None:
    """When the run's head was pushed: the earliest activity entry for its sha at most
    PUSH_WINDOW before the run's creation (and not after it by more than a minute). Pure."""
    c = ts(run.get('created_at'))
    if run.get('event') not in PUSH_EVENTS or c is None:
        return None
    got = [t for p in pushes if p.get('after') == run.get('head_sha') and p.get('activity_type') != 'branch_deletion'
           and (t := ts(p.get('timestamp'))) is not None and c - PUSH_WINDOW <= t <= c + 60]
    return min(got) if got else None


def reason_head(reason: object) -> str:
    """A reason's head: its text before the first ``:`` (or ``(``), digits dropped. Pure."""
    s = re.split(r'[:(]', str(reason or '?'), maxsplit=1)[0].strip()
    return re.sub(r'\s*[0-9]+\b', '', s).strip()[:40] or '?'


def report(data: Sequence[Mapping], records: Mapping | None = None, since: float = 0.0, until: float = 0.0,
           requests: int = 0) -> dict:
    """The report of ``collect``'s results and the records' (``read_records``). Pure."""
    rec = records or {'outcomes': {}, 'presence': None}
    labels: dict[str, Acc] = {}
    repos: dict[str, dict] = {}
    for d in data:
        repo = d['repo']
        all_, parts, by_label = Acc(), Acc(), {}
        wall, verdict, unstarted, no_verdict = [], [], 0, 0
        origin = {'push': 0, 'created': 0}
        retries = {r: 0 for r in ROWS}
        refusals: dict[str, int] = {}
        outcomes = dict(d.get('outcomes') or {})
        outcomes.update({k[1:]: v for k, v in rec['outcomes'].items() if k[0] == repo.lower()})
        for r in d['runs']:
            jobs = r.get('jobs') or []
            byp = jobs_by_part(jobs)
            attempts = {id(j) for ks in byp.values() for k in ATTEMPTS for j in ks.get(k, [])}
            ends = []
            for j in jobs:
                if j.get('conclusion') != 'skipped' and (e := ts(j.get('completed_at'))) is not None:
                    ends.append(e)
                if not took(j):
                    unstarted += j.get('conclusion') != 'skipped'
                    continue
                all_.add(j)
                labels.setdefault(labels_of(j), Acc()).add(j)
                by_label.setdefault(labels_of(j), Acc()).add(j)
                if id(j) in attempts:
                    parts.add(j)
            c = ts(r.get('created_at'))
            if r.get('status') == 'completed' and ends and c is not None:
                wall.append(max(ends) - c)
            vs = [j for j in jobs if kind(str(j.get('name', '')))[1] == 'verdict' and took(j)
                  and ts(j.get('completed_at')) is not None]
            if vs and c is not None:
                last = max(vs, key=lambda j: (int(j.get('run_attempt') or 1), ts(j.get('completed_at'))))
                p = push_time(r, d.get('pushes') or [])
                origin['push' if p is not None else 'created'] += 1
                verdict.append(ts(last.get('completed_at')) - (p if p is not None else c))
            elif r.get('status') == 'completed':
                no_verdict += 1
            for (att, p), ks in byp.items():
                doc = outcomes.get((r['id'], att, part_id(p)))
                for k in ('retry', 'fallback'):
                    for j in ks.get(k, []):
                        if took(j):
                            retries[row(k, doc, ks.get('first', []), [x for f in HOST_FINDERS for x in ks.get(f, [])])] += 1
        seen = set()
        for key, doc in outcomes.items():
            if doc.get('outcome') == 'unavailable' and key not in seen:
                seen.add(key)
                h = reason_head(doc.get('reason'))
                refusals[h] = refusals.get(h, 0) + 1
        repos[repo] = {'runs': len(d['runs']), 'completed': sum(1 for r in d['runs'] if r.get('status') == 'completed'),
                       **all_.out(), 'unstarted': unstarted, 'parts': parts.out(), 'wall': summary(wall),
                       'verdict': summary(verdict), 'verdict_from': origin, 'no_verdict': no_verdict,
                       'retries': {k: v for k, v in retries.items() if v}, 'refusals': refusals,
                       'pushes_read': d.get('pushes') is not None,
                       'labels': {k: v.out() for k, v in sorted(by_label.items())}}
    return {'since': iso(since), 'until': iso(until), 'requests': requests,
            'labels': {k: v.out() for k, v in sorted(labels.items())}, 'repos': repos,
            'presence': rec['presence']}


# ------------------------------------------------------------------ a host's files

RECORD = re.compile(r'(?P<run>[0-9]+)-(?P<att>[0-9]+)/(?P<id>[A-Za-z0-9][A-Za-z0-9_.-]{0,63})-(?P<n>[0-9]+)\Z')


def read_records(dirs: Sequence[str], since: float, until: float) -> dict:
    """The part records' first-attempt outcomes and every ``unavailable`` one, and the
    presence log lines, of each host data directory in ``dirs``, from ``since`` to ``until``.
    An unreadable file or line is skipped."""
    outcomes: dict = {}
    presence: dict = {}
    for d in dirs:
        root = os.path.join(d, 'ci-runners', 'state', 'parts')
        for dirpath, dirnames, files in os.walk(root):
            dirnames.sort()
            if 'outcome.json' not in files:
                continue
            rel = os.path.relpath(dirpath, root).split(os.sep)
            m = RECORD.fullmatch('/'.join(rel[2:])) if len(rel) == 4 else None
            if not m:
                continue
            try:
                if not since <= os.stat(os.path.join(dirpath, 'outcome.json')).st_mtime <= until + 60:
                    continue
                with open(os.path.join(dirpath, 'outcome.json'), encoding='utf-8') as f:
                    doc = json.load(f)
            except (OSError, ValueError):
                continue
            if isinstance(doc, dict) and (m.group('n') == '1' or doc.get('outcome') == 'unavailable'):
                key = (f'{rel[0]}/{rel[1]}'.lower(), int(m.group('run')), int(m.group('att')), m.group('id'))
                if m.group('n') == '1':
                    outcomes[key] = doc
                else:  # a refusal of a later attempt: counted, never read as a first attempt's outcome
                    outcomes[key[:3] + (f'{m.group("id")}#{m.group("n")}',)] = doc
        presence[d] = presence_counts(d, since, until)
    return {'outcomes': outcomes, 'presence': presence if dirs else None}


def presence_counts(d: str, since: float, until: float) -> dict:
    """Presence's log lines of one host from ``since`` to ``until`` (16.4, 19.8). Pure but
    for the reads."""
    out = {'stop': {}, 'start': {}, 'kept': {}, 'holds': 0, 'infra': 0, 'drains': 0}
    logs = os.path.join(d, 'ci-runners', 'logs')
    try:
        names = sorted(n for n in os.listdir(logs) if re.fullmatch(r'presence-[0-9]{4}-[0-9]{2}-[0-9]{2}\.log', n))
    except OSError:
        return out
    last_stop: dict[str, str] = {}
    for n in names:
        try:
            with open(os.path.join(logs, n), encoding='utf-8', errors='replace') as f:
                text = f.read(MAX_LOG)
        except OSError:
            continue
        for raw in text.splitlines():
            try:
                line = json.loads(raw)
            except ValueError:
                continue
            t = ts(line.get('time')) if isinstance(line, dict) else None
            if t is None or not since <= t <= until:
                continue
            action, name = line.get('action'), str(line.get('runner', ''))
            if action in ('stop', 'kept'):
                c = ' + '.join(sorted({reason_head(x) for x in str(line.get('cause', '')).split('; ') if x})) or '?'
                out[action][c] = out[action].get(c, 0) + 1
                if action == 'stop':
                    last_stop[name] = c
            elif action == 'start':
                c = last_stop.pop(name, None) or str(line.get('cause') or '?')
                out['start'][c] = out['start'].get(c, 0) + 1
            elif line.get('hold') == 'set':
                out['holds'] += 1
            elif 'infra' in line and 'error' not in line:
                out['infra'] += 1
            elif str(line.get('drain', '')).startswith('drained') and 'error' not in line:
                out['drains'] += 1
    return out


# ------------------------------------------------------------------ the text

def _s(v: float | None) -> str:
    if v is None:
        return '-'
    return f'{v:.0f}s' if v < 600 else f'{v / 60:.1f}m'


def _cols(s: Mapping) -> str:
    return f'{s["n"]:>5} {_s(s["p50"]):>8} {_s(s["p90"]):>8} {_s(s["max"]):>8}'


def text(rep: Mapping) -> str:
    head = f'{"":<40} {"n":>5} {"p50":>8} {"p90":>8} {"max":>8}'
    lines = [f'ci-run stats: runs created {rep["since"]} to {rep["until"]} ({rep["requests"]} requests)', '',
             'per label: queue (created -> started), run (started -> completed)', head]
    for k, v in rep['labels'].items():
        lines += [f'{k[:31] + " queue":<40} {_cols(v["queue"])}', f'{k[:31] + " run":<40} {_cols(v["run"])}']
    for repo, v in rep['repos'].items():
        lines += ['', f'{repo}: {v["runs"]} runs ({v["completed"]} completed), {v["jobs"]} jobs taken, '
                      f'{v["unstarted"]} not taken, {v["no_verdict"]} completed without a verdict', head]
        rows = [('queue, every job', v['queue']), ('run, every job', v['run']), ('queue, part attempts', v['parts']['queue']),
                ('run, part attempts', v['parts']['run']), ('wall (created -> last job end)', v['wall']),
                (f'verdict (push {v["verdict_from"]["push"]}, created {v["verdict_from"]["created"]})', v['verdict'])]
        rows += [(f'{k[:31]} queue', x['queue']) for k, x in v['labels'].items()]
        lines += [f'{n:<40} {_cols(s)}' for n, s in rows]
        lines.append('retries by row: ' + (', '.join(f'{k} {n}' for k, n in v['retries'].items()) or 'none'))
        lines.append('refusals after a start: ' + (', '.join(f'{k} {n}' for k, n in sorted(v['refusals'].items()))
                                                   or 'none read'))
    if rep['presence'] is None:
        lines += ['', 'presence: not read (no --records)']
    for d, p in (rep['presence'] or {}).items():
        def kv(m):
            return ', '.join(f'{k} {n}' for k, n in sorted(m.items())) or 'none'
        lines += ['', f'presence of {d}: stops {kv(p["stop"])}', f'  starts (by the stop\'s cause) {kv(p["start"])}',
                  f'  kept (a job started) {kv(p["kept"])}; holds {p["holds"]}, infrastructure ends {p["infra"]}, '
                  f'self-drains {p["drains"]}']
    return '\n'.join(lines) + '\n'


# ------------------------------------------------------------------ CLI

def parse_since(v: str, now: float) -> float:
    m = SINCE.fullmatch(v)
    if m:
        return now - int(m.group(1)) * {'m': 60, 'h': 3600, 'd': 86400}[m.group(2)]
    t = ts(v if ('T' in v or 'Z' in v) else v + 'T00:00:00Z')
    if t is None or t >= now:
        raise ValueError(f'--since {v!r}: 90m, 24h, 7d or an ISO time in the past')
    return t


def default_repos(etc: str) -> list[str]:
    """The repositories of the project registry (``<etc>/projects/*.toml``), sorted."""
    out = set()
    try:
        names = os.listdir(os.path.join(etc, 'projects'))
    except OSError:
        return []
    for n in names:
        if n.endswith('.toml'):
            try:
                with open(os.path.join(etc, 'projects', n), 'rb') as f:
                    r = tomllib.load(f).get('repository')
            except (OSError, tomllib.TOMLDecodeError):
                continue
            if isinstance(r, str) and REPO.match(r):
                out.add(r)
    return sorted(out)


def main(argv: Sequence[str], etc: str = '', environ: Mapping[str, str] = os.environ, send: Callable = transport,
         now: Callable[[], float] = time.time, out=sys.stdout, base: str = API) -> int:
    p = argparse.ArgumentParser(prog='ci-run stats')
    p.add_argument('--repo', action='append', default=[])
    p.add_argument('--since', default='24h')
    p.add_argument('--run', action='append', type=int, default=[], help='these runs only (one --repo)')
    p.add_argument('--records', action='append', default=[], help='a host\'s data directory (<DIR>/ci-runners)')
    p.add_argument('--no-artifacts', action='store_true')
    p.add_argument('--max-requests', type=int, default=MAX_REQUESTS)
    p.add_argument('--json', action='store_true')
    try:
        a = p.parse_args(list(argv))
    except SystemExit:
        return 2
    t = now()
    try:
        since = parse_since(a.since, t)
    except ValueError as e:
        print(f'stats: {e}', file=sys.stderr)
        return 2
    repos = a.repo or default_repos(etc)
    bad = [r for r in repos if not REPO.match(r)]
    if not repos or bad or a.max_requests < 1 or not all(os.path.isdir(d) for d in a.records) or (
            a.run and (len(repos) != 1 or any(r < 1 for r in a.run))):
        print('stats: name each repository as OWNER/NAME (--repo; one with --run), a positive --max-requests '
              'and existing --records directories', file=sys.stderr)
        return 2
    token = environ.get('GH_TOKEN', '')
    if not token or len(token) > 255 or not token.isascii() or any(c.isspace() for c in token):
        print('stats: no login: run it as GH_TOKEN=$(gh auth token) ci-run stats ...', file=sys.stderr)
        return 2
    api = Api(token, base, send, a.max_requests)
    rec = read_records(a.records, since, t)
    try:
        data = [collect(api, r, since, artifacts=not a.no_artifacts, run_ids=a.run,
                        outcomes={k[1:]: v for k, v in rec['outcomes'].items() if k[0] == r.lower()}) for r in repos]
    except StatsError as e:
        print(f'stats: {e}', file=sys.stderr)
        return 1
    rep = report(data, rec, min(d['since'] for d in data), t, api.requests)
    out.write(json.dumps(rep, indent=1, sort_keys=True) + '\n' if a.json else text(rep))
    return 0
