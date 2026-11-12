"""``ci-run sweep``: a consumer's runs whose job has queued at GitHub past a bound (design
§4.10, decision 19 item 9; issue #213). The step of the reusable workflow ``sweep.yml``,
run on a hosted runner with the job's token (``actions: write``, ``statuses: write``).

    ci-run sweep --workflow W --context C [--minutes N] [--max-requests R] [--max-cancels K]

``queue_minutes`` (the plan's) bounds the wait on a host once a runner took a job; this bounds
the wait before that, in GitHub's queue, which GitHub ends only after 24 hours and never for a
single job. A sweep:

1. lists the workflow ``W``'s runs that are ``queued`` and ``in_progress``, those created before
   the bound (``created=<T``, one page of 100 each; more is reported, not read) and those after
   it (to see a newer run on a head);
2. reads each old run's latest jobs (``Link``-paged, at most ``JOB_PAGES`` pages): a job is
   stuck when its status is ``queued`` and it was created more than N minutes ago. Only
   ``queued``: ``waiting``, ``pending`` and ``requested`` are an environment's gate or a
   concurrency hold, which a runner does not end (whether a matrix job that ``max-parallel``
   holds shows ``pending`` or ``queued`` is not verified: checked at the release);
3. trusts a stuck run's ``head_sha`` as the head it tests only for a ``push`` or
   ``pull_request`` run (``ci.yml``'s default sha, where the caller passes no ``sha`` for those
   events) or where a status of this run (its ``target_url`` ends with ``/actions/runs/<id>``)
   is on that sha already. Any other run (a dispatch with ``ci.yml``'s ``sha``) is left alone
   and reported: a cancel would leave the head it tests pending for ever;
4. reads the head's statuses (the first page of 100, newest first; ``GET .../status``, the
   combined one, carries no creator) and counts only those of ``BOT``, the job token's
   identity; a status of this run created before its ``run_started_at`` is an earlier
   attempt's and counts as none. A newer run of the workflow on the head, or a newest status
   of ``C`` that is another run's: cancel only (``verdict`` skips a cancelled run; the other
   run owns the context). None, or this run's ``pending``: post ``error``, then cancel. This
   run's ``failure`` (a red part) or other final state: cancel only (it stays the final
   status). This run's own earlier sweep ``error`` (a cancel that did not take):
   ``force-cancel``;
5. posts before it cancels, so that a sweep stopped between the two leaves the ``error`` for
   the next one to find; a cancel answered ``409`` is followed by a ``force-cancel``. The
   ``error``'s ``target_url`` is the swept run, its description built from validated pieces
   alone (``describe``).

At most ``--max-requests`` calls (tries count), ``--max-cancels`` cancelled runs and
``DEADLINE`` seconds a sweep, and no call once ``X-RateLimit-Remaining`` is below
``RATE_FLOOR`` (the job token's hourly calls are shared with the runs' own); a run is acted on
only with the calls for its whole action left. Each call 30 s and ``github.TRIES`` tries (5xx
and 429 only); a ``Link`` off the API host is refused. One line per run acted on or left, and
a table in ``GITHUB_STEP_SUMMARY``.

Status: 0 the sweep ran; 1 GitHub could not be read, a post failed or a bound was reached
(after what was done); 2 bad arguments or no token. Stdlib only.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence

from ci_runners import github, stats

API, SERVER = 'https://api.github.com', 'https://github.com'
PAGE, JOB_PAGES = 100, 11
MAX_REQUESTS, MAX_CANCELS = 50, 10
#: the sweep's wall-clock bound, under the job's timeout-minutes of 10
DEADLINE = 8 * 60
#: no call below this many of the token's calls left this hour
RATE_FLOOR = 100
#: the job token's identity: only its statuses count (as flow's claim reads the runners' own)
BOT = 'github-actions[bot]'
MINUTES, MIN_MINUTES, MAX_MINUTES = 60, 5, 1440
MAX_BODY = 32 << 20
WORKFLOW = re.compile(r'[A-Za-z0-9._-]{1,100}\.ya?ml\Z')
CONTEXT = re.compile(r'[A-Za-z0-9._/-]{1,100}\Z')  # as flow.post's
REPO = re.compile(r'[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}\Z')
PART_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z')
LABEL = re.compile(r'[a-z0-9][a-z0-9-]{0,63}\Z')
NEXT = re.compile(r'<([^>]+)>\s*;\s*rel="next"')
#: the events whose run's head_sha is the head ci.yml tests by default
TRUSTED_EVENTS = ('push', 'pull_request')
PREFIX = 'queued past '
#: one action's calls: the post, the cancel and a force-cancel after a 409, each with its tries
ACTION_CALLS = 3 * github.TRIES


class SweepError(RuntimeError):
    """GitHub could not be read."""


class Bound(SweepError):
    """A bound of the sweep was reached."""


# ------------------------------------------------------------------ GitHub

def transport(method: str, url: str, headers: Mapping[str, str], body: bytes | None) -> tuple[int, Mapping, bytes]:
    """One call, redirects not followed (the token goes to the API host only)."""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    req = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=github.CALL_BOUND) as r:
            return r.status, dict(r.headers), r.read(MAX_BODY + 1)
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), b''


class Api:
    """The job token's calls: ``github.TRIES`` tries each, at most ``limit`` in all, none past
    ``deadline`` seconds or below ``RATE_FLOOR`` calls left this hour."""

    def __init__(self, token: str, base: str = API, send: Callable = transport, limit: int = MAX_REQUESTS,
                 pause: float = 5.0, deadline: float = DEADLINE, clock: Callable[[], float] = time.monotonic) -> None:
        self.token, self.base, self.send, self.limit, self.pause = token, base.rstrip('/'), send, limit, pause
        self.clock, self.until = clock, clock() + deadline
        self.requests = 0
        self.remaining: int | None = None  # X-RateLimit-Remaining of the last answer

    def check(self) -> None:
        """Raise Bound where no further call may go."""
        if self.requests >= self.limit:
            raise Bound(f'more than {self.limit} requests (--max-requests)')
        if self.clock() > self.until:
            raise Bound('past the sweep\'s deadline')
        if self.remaining is not None and self.remaining < RATE_FLOOR:
            raise Bound(f'{self.remaining} API calls left this hour, under {RATE_FLOOR}')

    def left(self) -> int:
        return self.limit - self.requests

    def call(self, method: str, url: str, body: dict | None = None, ok: Sequence[int] = ()) -> tuple[int, Mapping, object]:
        """``(status, headers, JSON)`` of a 2xx answer or one in ``ok``."""
        data = json.dumps(body).encode() if body is not None else None
        last = ''
        for i in range(github.TRIES):
            self.check()
            self.requests += 1
            headers = {'Authorization': f'Bearer {self.token}', 'Accept': 'application/vnd.github+json',
                       'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'ci-runners'}
            try:
                code, h, raw = self.send(method, url, headers, data)
            except (OSError, ValueError) as e:
                code, h, raw, last = 0, {}, b'', type(e).__name__
            left = {k.lower(): v for k, v in h.items()}.get('x-ratelimit-remaining', '')
            if re.fullmatch(r'[0-9]{1,9}', str(left)):
                self.remaining = int(left)
            if 200 <= code < 300 or code in ok:
                try:
                    return code, h, json.loads(raw or b'null')
                except ValueError:
                    raise SweepError(f'{method} {url.split("?")[0]}: not JSON') from None
            if code:
                last = f'HTTP {code}'
                if code < 500 and code != 429:
                    break  # a refusal does not improve with retries
            if i + 1 < github.TRIES:
                time.sleep(self.pause)
        raise SweepError(f'{method} {url.split("?")[0]}: {last}')

    def pages(self, path: str, key: str | None, max_pages: int) -> list[dict]:
        """Every row of a listing, following ``Link: rel="next"`` on the API host alone."""
        url, out = f'{self.base}{path}', []
        for _ in range(max_pages):
            _, h, doc = self.call('GET', url)
            rows = doc.get(key) if key and isinstance(doc, dict) else doc
            if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
                raise SweepError(f'GET {path.split("?")[0]}: not a list')
            out += rows
            m = NEXT.search({k.lower(): v for k, v in h.items()}.get('link', ''))
            if not m or not rows:
                return out
            url = m.group(1)
            if not url.startswith(self.base + '/'):
                raise SweepError(f'GET {path.split("?")[0]}: a next page off {self.base}')
        raise SweepError(f'GET {path.split("?")[0]}: more than {max_pages} pages')


def ts(v: object) -> float | None:
    """A GitHub time (``2026-11-12T09:41:25Z``) as epoch seconds; None for none."""
    if not isinstance(v, str) or not v:
        return None
    try:
        return datetime.datetime.fromisoformat(v.replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


# ------------------------------------------------------------------ the rules (pure)

def stuck(jobs: Sequence[Mapping], now: float, bound: float) -> list[Mapping]:
    """The jobs queued at GitHub for longer than ``bound`` seconds, oldest first. Pure."""
    got = [j for j in jobs if j.get('status') == 'queued' and (ts(j.get('created_at')) or now) < now - bound]
    return sorted(got, key=lambda j: ts(j.get('created_at')) or now)


def of_run(st: Mapping, run_id: int) -> bool:
    """A status this run posted: its ``target_url`` is the run's page."""
    return str(st.get('target_url') or '').endswith(f'/actions/runs/{run_id}')


def newest(statuses: Sequence[Mapping], context: str) -> Mapping | None:
    """The newest status of ``context`` (the API lists newest first)."""
    return next((s for s in statuses if s.get('context') == context), None)


def by_bot(statuses: Sequence[Mapping]) -> list[Mapping]:
    """The statuses the job token's identity posted: no other poster steers a sweep. Pure."""
    def login(s: Mapping) -> str:
        c = s.get('creator')
        return str(c.get('login') or '') if isinstance(c, dict) else ''
    return [s for s in statuses if login(s).lower() == BOT]


def decide(run: Mapping, statuses: Sequence[Mapping], context: str, newer: bool = False) -> tuple[str, str]:
    """``(action, why)`` for a run with a stuck job, from the bot's ``statuses`` of its head:
    ``post`` (post, then cancel), ``cancel`` (cancel only), ``force`` (force-cancel only) or
    ``leave``; ``newer``: a later run of the workflow has the same head. Pure."""
    rid, event = run['id'], str(run.get('event') or '')
    if event not in TRUSTED_EVENTS and not any(of_run(s, rid) for s in statuses):
        return 'leave', 'its head is not known (no status of this run on its head_sha)'
    if newer:
        return 'cancel', 'a newer run on its head owns the context'
    st = newest(statuses, context)
    started = ts(run.get('run_started_at'))
    if st is not None and of_run(st, rid) and started is not None and (ts(st.get('created_at')) or 0) < started:
        st = None  # an earlier attempt's: none of this one
    if st is None:
        return 'post', 'no status of this attempt'
    if not of_run(st, rid):
        return 'cancel', "another run's status is the newest"
    state = st.get('state')
    if state == 'pending':
        return 'post', 'its own pending'
    if state == 'error' and str(st.get('description') or '').startswith(PREFIX):
        return 'force', "an earlier sweep's cancel did not stop it"
    return 'cancel', f'its own {state if state in ("failure", "error", "success") else "status"} stays'


def what(job: Mapping) -> str:
    """``part <id>``, ``plan`` or ``a job``, from the job's name as ``stats`` reads both run
    shapes; an id that is not a plain part id is ``a job``. Pure."""
    name = str(job.get('name') or '')
    part, k = stats.kind(name)
    if k == 'other' and name.split(' / ')[-1].strip() == 'plan':
        return 'plan'
    pid = stats.part_id(part) if k in stats.PART_KINDS else ''
    return f'part {pid}' if PART_ID.fullmatch(pid) else 'a job'


def labels(job: Mapping) -> str:
    """The job's labels but ``self-hosted``, joined by ``+``; empty where one is not a plain
    label name (it comes from the head's workflow file). Pure."""
    got = [x for x in job.get('labels') or [] if x != 'self-hosted']
    if got and all(isinstance(x, str) and LABEL.fullmatch(x) for x in got):
        return '+'.join(got)
    return ''


def describe(minutes: int, jobs: Sequence[Mapping]) -> str:
    """The error's description, at most 140 characters, from validated pieces only:
    ``queued past N min: part <id> on <labels>`` (``plan on``; ``<n> jobs, first part ...``). Pure."""
    first = jobs[0]
    head = f'{PREFIX}{minutes} min: ' + ('' if len(jobs) == 1 else f'{len(jobs)} jobs, first ')
    text, lab = head + what(first), labels(first)
    if lab and len(text) + 4 + len(lab) <= 140:
        text += f' on {lab}'
    return text[:140]


# ------------------------------------------------------------------ the sweep

class Sweep:
    def __init__(self, api: Api, repo: str, server: str, *, workflow: str, context: str, minutes: int,
                 max_cancels: int = MAX_CANCELS, now: float | None = None) -> None:
        self.api, self.repo, self.server = api, repo, server.rstrip('/')
        self.workflow, self.context, self.minutes, self.max_cancels = workflow, context, minutes, max_cancels
        self.now = time.time() if now is None else now
        self.cancels = 0
        self.rows: list[tuple[str, str, str, str, str]] = []  # (run, event, head, done, why)
        self.listed: list[Mapping] = []
        self.notes: list[str] = []
        self.failed = False

    def runs(self) -> list[Mapping]:
        """The workflow's queued and in-progress runs created before the bound, oldest first;
        ``self.listed`` gets the younger ones too."""
        cutoff = self.now - self.minutes * 60
        when = datetime.datetime.fromtimestamp(cutoff, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        seen: dict[int, Mapping] = {}
        for status in ('queued', 'in_progress'):
            for op in ('<', '>='):  # the old runs get a page of their own: young ones never crowd them out
                created = urllib.parse.quote(op + when, safe='')
                _, _, doc = self.api.call('GET', f'{self.api.base}/repos/{self.repo}/actions/workflows/{self.workflow}'
                                                 f'/runs?status={status}&created={created}&per_page={PAGE}')
                rows = doc.get('workflow_runs') if isinstance(doc, dict) else None
                if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
                    raise SweepError(f'GET runs ({status}): not a list')
                total = doc.get('total_count')
                if isinstance(total, int) and total > len(rows):
                    self.notes.append(f'{total} {status} runs created {op} {when}: only the newest {len(rows)} read')
                for r in rows:
                    if type(r.get('id')) is int and r['id'] > 0:
                        seen.setdefault(r['id'], r)
        self.listed = list(seen.values())
        old = [r for r in self.listed if (ts(r.get('created_at')) or self.now) < cutoff]
        return sorted(old, key=lambda r: (ts(r.get('created_at')), r['id']))

    def newer(self, run: Mapping) -> bool:
        """A later run of the workflow, queued or in progress, has the same head."""
        key = (ts(run.get('created_at')) or 0, run['id'])
        return any(r['id'] != run['id'] and r.get('head_sha') == run.get('head_sha')
                   and (ts(r.get('created_at')) or 0, r['id']) > key for r in self.listed)

    def statuses(self, sha: str) -> list[Mapping]:
        """The bot's statuses on the first page of the head's (newest first)."""
        _, _, doc = self.api.call('GET', f'{self.api.base}/repos/{self.repo}/commits/{sha}/statuses?per_page={PAGE}')
        if not isinstance(doc, list) or not all(isinstance(s, dict) for s in doc):
            raise SweepError('GET statuses: not a list')
        return by_bot(doc)

    def note(self, run: Mapping, done: str, why: str) -> None:
        event, sha = str(run.get('event') or ''), str(run.get('head_sha') or '')
        self.rows.append((str(run['id']), event if re.fullmatch(r'[a-z_]{1,40}', event) else '-',
                          sha[:12] if github.SHA.fullmatch(sha) else '-', done, why))
        print(f'ci-run sweep: run {run["id"]}: {done} ({why})')

    def one(self, run: Mapping) -> None:
        rid, sha = run['id'], str(run.get('head_sha') or '')
        jobs = self.api.pages(f'/repos/{self.repo}/actions/runs/{rid}/jobs?filter=latest&per_page={PAGE}', 'jobs',
                              JOB_PAGES)
        late = stuck(jobs, self.now, self.minutes * 60)
        if not late:
            return
        text = describe(self.minutes, late)
        if not github.SHA.fullmatch(sha):
            self.note(run, 'left alone', f'{text}; its head_sha is malformed')
            return
        action, why = decide(run, self.statuses(sha), self.context, self.newer(run))
        if action == 'leave':
            self.note(run, 'left alone', f'{text}; {why}')
            return
        if self.cancels >= self.max_cancels:
            raise Bound(f'more than {self.max_cancels} runs to cancel (--max-cancels)')
        if self.api.left() < ACTION_CALLS:
            raise Bound(f'{self.api.left()} requests left, too few for a post and its cancel (--max-requests)')
        self.cancels += 1
        posted = ''
        if action == 'post':  # first: a sweep stopped before the cancel leaves it for the next one
            target = f'{self.server}/{self.repo}/actions/runs/{rid}'
            self.api.call('POST', f'{self.api.base}/repos/{self.repo}/statuses/{sha}',
                          {'state': 'error', 'context': self.context, 'target_url': target, 'description': text})
            posted = 'error posted, '
        verb = 'force-cancel' if action == 'force' else 'cancel'
        code, _, _ = self.api.call('POST', f'{self.api.base}/repos/{self.repo}/actions/runs/{rid}/{verb}', ok=(409,))
        if code == 409 and verb == 'cancel':  # completed, or a cancel that does not take
            verb = 'force-cancel'
            code, _, _ = self.api.call('POST', f'{self.api.base}/repos/{self.repo}/actions/runs/{rid}/{verb}', ok=(409,))
        done = 'not cancelled (it had completed)' if code == 409 else f'{verb}led'
        self.note(run, posted + done, text if action == 'post' else f'{text}; {why}')

    def sweep(self) -> int:
        try:
            todo = self.runs()
        except SweepError as e:
            print(f'ci-run sweep: {e}', file=sys.stderr)
            return 1
        for run in todo:
            try:
                self.one(run)
            except Bound as e:
                self.notes.append(f'stopped: {e}')
                self.failed = True
                break
            except SweepError as e:  # this run could not be read or posted: the others still are
                self.note(run, 'failed', str(e))
                self.failed = True
        for n in self.notes:
            print(f'ci-run sweep: {n}', file=sys.stderr)
        print(f'ci-run sweep: {len(todo)} runs older than {self.minutes} min read, {self.cancels} cancelled, '
              f'{self.api.requests} requests')
        return 1 if self.failed else 0

    def summary(self) -> str:
        out = [f'### ci-runners sweep: `{self.workflow}`, jobs queued past {self.minutes} min', '']
        if self.rows:
            out += ['| run | event | head | done | why |', '|---|---|---|---|---|']
            out += [f'| {r} | {e} | `{h}` | {d} | {w} |' for r, e, h, d, w in self.rows]
        else:
            out.append('No run has a job queued past the bound.')
        out += [f'- {n}' for n in self.notes]
        return '\n'.join(out) + '\n'


def minutes(v: str) -> int:
    n = int(v) if re.fullmatch(r'[0-9]{1,5}', v) else -1
    if not MIN_MINUTES <= n <= MAX_MINUTES:
        raise argparse.ArgumentTypeError(f'a whole number of minutes from {MIN_MINUTES} to {MAX_MINUTES}')
    return n


def count(v: str) -> int:
    if not re.fullmatch(r'[1-9][0-9]{0,4}', v):
        raise argparse.ArgumentTypeError('a whole number from 1')
    return int(v)


def main(argv: list[str], env: Mapping[str, str] | None = None, send: Callable = transport,
         now: float | None = None, pause: float = 5.0, clock: Callable[[], float] = time.monotonic) -> int:
    p = argparse.ArgumentParser(prog='ci-run sweep')
    p.add_argument('--workflow', required=True)
    p.add_argument('--context', required=True)
    p.add_argument('--minutes', type=minutes, default=MINUTES)
    p.add_argument('--max-requests', type=count, default=MAX_REQUESTS)
    p.add_argument('--max-cancels', type=count, default=MAX_CANCELS)
    a = p.parse_args(argv)
    env = os.environ if env is None else env
    repo, token = env.get('GITHUB_REPOSITORY', ''), env.get('GITHUB_TOKEN', '')
    if not WORKFLOW.fullmatch(a.workflow) or not CONTEXT.fullmatch(a.context):
        print('ci-run sweep: --workflow is not a workflow file name or --context not a status context', file=sys.stderr)
        return 2
    if not REPO.fullmatch(repo) or not token:
        print('ci-run sweep: GITHUB_REPOSITORY or GITHUB_TOKEN is missing', file=sys.stderr)
        return 2
    api = Api(token, env.get('GITHUB_API_URL') or API, send, a.max_requests, pause, clock=clock)
    s = Sweep(api, repo, env.get('GITHUB_SERVER_URL') or SERVER, workflow=a.workflow, context=a.context,
              minutes=a.minutes, max_cancels=a.max_cancels, now=now)
    code = s.sweep()
    if env.get('GITHUB_STEP_SUMMARY'):
        try:
            with open(env['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as f:
                f.write(s.summary())
        except OSError as e:
            print(f'ci-run sweep: the summary: {type(e).__name__}', file=sys.stderr)
    return code
