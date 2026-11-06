"""The verdict of a run of ``h100-test.yml`` (design §4.7, job 5).

``ci-run verdict --context C --sha S --parts "cpu gpu" --artifacts DIR`` reads the
parts' ``ci-part-<id>-<attempt>`` artifacts (``outcome.json``, ``summary.md``) that the
action downloaded into ``DIR``. A part's last attempt decides: an attempt whose ``next``
asked for a retry is followed by the retry's record. A part whose last attempt left no
artifact (its runner stopped) is read from its record on this host (``salvage``), else
it is ``lost``. The verdict is then posted as the ``context`` status of ``S``, and the
parts' summaries go to the job summary.

``decide`` is pure. Only an infrastructure end the retry did not cure is ``error``; a
``red`` part fails the head whatever the others did (D6).
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import sys
from collections.abc import Callable, Iterable, Mapping

from ci_runners import github, inventory, part

#: the outcomes a part's ``outcome.json`` may name (design §4.4)
KINDS = frozenset({'pass', 'red', 'oom', 'thermal', 'reboot', 'unavailable', 'lost', 'stopped'})
#: the outcomes that fail the head: the code is wrong, not the host
FAILING = frozenset({'red'})
#: attempts per part: the first and one retry (design §4.4)
MAX_ATTEMPT = 2
ARTIFACT = re.compile(r'ci-part-([A-Za-z0-9][A-Za-z0-9_.-]{0,63})-([0-9]{1,2})\Z')
MAX_FILE = 1 << 20


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
    return Verdict('success', f'passed: {", ".join(e.id for e in ends)}')


def _describe(head: str, ends: list[End]) -> str:
    text = f'{head}: ' + ', '.join(f'{e.id} {e.outcome}' for e in ends)
    return text if len(text) <= 140 else text[:137] + '...'


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
    return out


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
    """The attempt to read from the host's records when the artifacts stop short: 1 when
    there is none; the retry when the last artifact asked for one and no retry artifact
    came; else 0 (the artifacts are complete). Pure."""
    if not attempts:
        return 1
    last = max(attempts)
    if attempts[last][0].get('next') in ('retry', 'fallback') and last < MAX_ATTEMPT:
        return last + 1
    return 0


def ends(ids: Iterable[str], found: Mapping[str, Mapping[int, tuple[dict, str]]],
         salvage: Callable[[str, int], dict | None]) -> list[End]:
    """Each expected part's end. ``salvage(id, attempt)`` reads an attempt's record, or
    gives None when there is none. Pure but for ``salvage``."""
    out = []
    for pid in ids:
        attempts = dict(found.get(pid, {}))
        n = next_attempt(attempts)
        if n:
            got = salvage(pid, n)
            if got is not None:
                attempts[n] = (got, '')
            elif not attempts:
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


def main(argv: list[str], inv: inventory.Inventory, call: Callable | None = None) -> int:
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

        items = ends(ids, collect(a.artifacts), salvage)
        v = decide(items)
        base = env.get('GITHUB_API_URL', 'https://api.github.com').rstrip('/')
        target = f'{env.get("GITHUB_SERVER_URL", "https://github.com")}/{repo}/actions/runs/{env.get("GITHUB_RUN_ID", "")}'
        token = env.get('GITHUB_TOKEN', '')
        if not token:
            raise ValueError('GITHUB_TOKEN is not set')
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
