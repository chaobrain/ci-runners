"""The reusable workflow's own commands (decision 16.6, design §4.10; RUN-10 R6).

``ci.yml`` and ``part.yml`` keep their logic here, in tested commands, and out of YAML
expressions. They run through ``actions/flow``, from the action's own checkout (the
release the workflow names, §9), not from the host's installed release: they read the
host's files (the capacity, the registry) but change nothing there.

``ci-run flow contract``  the contract check: the default branch's ``.github/ci-runners.toml``
                          ``contract`` against the head's; a lower or missing one posts
                          ``failure`` ("merge origin/<default>") and exits 1.
``ci-run flow plan``      the plan JSON (§4.10) checked and routed (16.5): the matrix of
                          ``part.yml``, the part ids, ``max_parallel`` and the summary; a plan
                          that does not load, a red or lost plan command and a route error
                          post their state and exit 1 (route) or 2 (plan).
``ci-run flow retry``     the retry of a part's first attempt by the five rows (16.5): prints
                          the retry's labels, or ``none``; ``retry`` in the step's outputs.
``ci-run flow locate``    the host of a part's first attempt, from its job's ``runner_name``
                          (§2): the salvage leg's labels, ``[self-hosted, <role>-light]``.
``ci-run flow status``    post a state on the context (a red part's fail-fast status).
``ci-run flow admit``     a public caller (decision 18.7, ``public.yml``): a repository of this
                          organisation and its events; prints the refusal and exits 1.
``ci-run flow verdict``   a public caller's verdict on a hosted runner: ``verdict`` without
                          the salvage (a public part's tree goes with its job).

A public caller's commands run with ``CIRUNNERS_PUBLIC=true`` (``public.yml`` sets it on its
steps): they read nothing of a host (no capacity, registry or policy), its plan is routed by
``route_public`` (hosted CPU; its own pool's ``gpu``, for a repository opted in to the public
GPU pool only) and its retry by ``retry_public``.

Every command reads ``GITHUB_*`` from its environment and writes ``GITHUB_OUTPUT`` and
``GITHUB_STEP_SUMMARY`` where they are set. Stdlib only.
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures
import http.client
import io
import json
import math
import os
import re
import shutil
import stat
import sys
import tomllib
import urllib.parse
import zipfile
import zlib
from collections.abc import Callable, Mapping, Sequence

from ci_runners import capacity, claim, github, policy, registry
from ci_runners import route as rt

SPEC = '.github/ci-runners.toml'
#: a part id the plan job's own part takes (its artifact is ``ci-part-plan-1``)
RESERVED = frozenset({'plan'})
#: the plan without a ``plan`` command (§4.10)
DEFAULT_PLAN = {'schema': 1, 'parts': [{'id': 'pytest', 'command': 'python -m pytest', 'needs': 'cpu', 'minutes': 60}]}
MAX_PLAN = 1 << 20
#: the part action's id bound (``part.ID``)
MAX_ID = 64
#: a part's command, so that it fits one argument and one environment value with room
MAX_COMMAND = 32 << 10
MAX_MEMORY_GIB = 4096
#: the matrix as one job output, under GitHub's 1 MB per job
MAX_MATRIX = 500_000  # well under GitHub's 1 MB per output and per evaluated expression
#: a part's own artifacts (``part.yml`` has one upload step per index)
MAX_ARTIFACTS = 4
#: an attempt's wait on its host for a slot or a lease where the plan sets no
#: ``queue_minutes``; the job's bound takes it on top of the route's
QUEUE_MINUTES = 15
ARTIFACT_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.]{0,31}')  # no '-': <name>-<id>-<attempt> stays unique
SAFE_PATH = re.compile(r'[A-Za-z0-9._/-]+')
#: the plan's ``files``: what the plan job uploads once for every part
MAX_FILES, MAX_FILES_BYTES = 1000, 64 << 20
#: what actions/upload-artifact refuses in a file's path (its path check), and so the plan
UNCARRIED = frozenset('":<>|*?\r\n')
#: the run's jobs read for the salvage leg's host: 2 + 256 parts x 4 jobs fit
MAX_JOB_PAGES = 20
#: parts of one run at once where the registry sets no ``max_parallel`` (§7 rule 1)
MAX_PARALLEL = registry.MAX_PARALLEL
#: the salvage leg's bound past the part's own job bound
SALVAGE_GRACE = 10
#: the states ``flow status`` posts: a red attempt's, and the plan job's end states (the verdict job)
STATES = ('failure', 'error', 'success')
GPUS = {'gpu': 1, 'gpu2': 2}
EXIT_RED, EXIT_PLAN = 1, 2


class FlowError(ValueError):
    """A command's input is malformed: the workflow's fault, never the consumer's."""


# ------------------------------------------------------------------ GitHub

def _env(env: Mapping[str, str]) -> tuple[str, str, str]:
    repo, token = env.get('GITHUB_REPOSITORY', ''), env.get('GITHUB_TOKEN', '')
    if not registry.REPO.fullmatch(repo) or not token:
        raise FlowError('GITHUB_REPOSITORY or GITHUB_TOKEN is missing')
    return env.get('GITHUB_API_URL', 'https://api.github.com').rstrip('/'), repo, token


def post(env: Mapping[str, str], call: Callable, *, context: str, sha: str, state: str, description: str) -> None:
    """Post ``state`` on ``context`` of ``sha``, linked to this run."""
    if state not in (*STATES, 'pending'):
        raise FlowError(f'state {state!r}')
    if not re.fullmatch(r'[A-Za-z0-9._/-]{1,100}', context) or not github.SHA.fullmatch(sha):
        raise FlowError('context or sha is malformed')
    base, repo, token = _env(env)
    target = f'{env.get("GITHUB_SERVER_URL", "https://github.com")}/{repo}/actions/runs/{env.get("GITHUB_RUN_ID", "")}'
    text = _text(description)
    call('POST', f'{base}/repos/{repo}/statuses/{sha}', token,
         {'state': state, 'context': context, 'description': text, 'target_url': target})


def _text(description: str) -> str:
    """A status description: one line, at most 140 characters."""
    one = ' '.join(description.split())
    return one if len(one) <= 140 else one[:137] + '...'


def _bot(etc: str, env: Mapping[str, str]) -> str:
    """The runners' identity (the policy's ``bot``, decision 15.9): only its statuses count."""
    if is_public(env):  # a hosted runner has no policy: the job token's identity (18.7)
        return policy.Policy(frozenset(), {}).bot
    with open(os.path.join(etc, 'policy.toml'), 'rb') as f:
        return policy.load(f.read(1 << 20)).bot


def _statuses(env: Mapping[str, str], call: Callable, context: str, sha: str, bot: str) -> list[claim.Status]:
    """The runners' own statuses of ``context`` on ``sha``, newest first."""
    if not github.SHA.fullmatch(sha):
        raise FlowError('sha is malformed')
    base, repo, token = _env(env)
    got = github.statuses(call, f'{base}/repos/{repo}/commits/{sha}/statuses?per_page={github.PAGE}', token)
    return [st for st in claim.parse_statuses(got, context) if st.creator.lower() == bot.lower()]


def run_claim(a: argparse.Namespace, env: Mapping[str, str], call: Callable, etc: str) -> int:
    """The claim's decision (§4.1) with a read-only token: ``run``, ``reason``, and ``seen``,
    the time of the newest status of the runners it read (0: none), for ``pending``."""
    bot = _bot(etc, env)
    mine = _statuses(env, call, a.context, a.sha, bot)
    c = claim.decide(mine, bot=bot)
    seen = max((st.time for st in mine), default=0.0)
    print(f'ci-run flow claim: {"run" if c.run else "skip"} ({c.reason})', file=sys.stderr)
    _out(env, run='true' if c.run else 'false', reason=c.reason, seen=repr(seen))
    return 0


def run_pending(a: argparse.Namespace, env: Mapping[str, str], call: Callable, etc: str) -> int:
    """The claim's ``pending`` post, from a job of its own beside the parts: only where the
    runners posted nothing on the context since the claim read it (``--seen``). A red
    attempt posted meanwhile, or a green, is never covered by a pending."""
    try:
        seen = float(a.seen)
    except ValueError:
        raise FlowError('--seen is not a time') from None
    newer = [st for st in _statuses(env, call, a.context, a.sha, _bot(etc, env)) if st.time > seen]
    if newer:
        print(f'ci-run flow pending: not posted: {newer[0].state} posted since the claim', file=sys.stderr)
        _out(env, posted='false')
        return 0
    post(env, call, context=a.context, sha=a.sha, state='pending', description=a.description)
    # a red attempt posted between the read and the post: put it back on top
    final = [st for st in _statuses(env, call, a.context, a.sha, _bot(etc, env)) if st.time > seen and st.state != 'pending']
    if final:
        top = max(final, key=lambda st: st.time)
        print(f'ci-run flow pending: {top.state} posted meanwhile: posted again over the pending', file=sys.stderr)
        post(env, call, context=a.context, sha=a.sha, state=top.state, description=top.description)
    _out(env, posted='true')
    return 0


def contents(env: Mapping[str, str], call: Callable, path: str, ref: str) -> str | None:
    """The text of ``path`` at ``ref`` through the contents API, or None where it does
    not exist there (HTTP 404). Raises ``github.ApiError`` on any other failure."""
    base, repo, token = _env(env)
    url = f'{base}/repos/{repo}/contents/{urllib.parse.quote(path)}?ref={urllib.parse.quote(ref, safe="")}'
    try:
        got = call('GET', url, token)
    except github.ApiError as e:
        if str(e).endswith('HTTP 404'):
            return None
        raise
    if not isinstance(got, dict) or got.get('encoding') != 'base64' or not isinstance(got.get('content'), str):
        raise github.ApiError(f'GET {path}@{ref}: not a file')
    return base64.b64decode(got['content']).decode('utf-8', 'replace')


#: the contract's end for a public repository on the private legs (ci.yml sends a public
#: caller to public.yml; an event without its repository in the payload still lands here)
PUBLIC = 'a public repository: its run is public.yml\'s, on hosted runners (decision 18.7)'
PRIVATE_ON_PUBLIC = 'a private repository: its run is ci.yml\'s own, not public.yml\'s'

# ------------------------------------------------------------------ a public caller (18.7)

#: the organisation whose public repositories the hosted legs serve (decided 2026-11-13: they
#: touch nothing of ours, so they need no opt-in; ``admit`` refuses every other owner)
ORG = policy.SELF.split('/')[0]
#: the public repositories the lead opted in to the public GPU pool (decision 16.7, 18.7), as
#: their policy entries' ``public_runners = true`` name them on the hosts: a plan with a
#: ``gpu`` part from any other is red at plan time; a CPU-only plan needs no entry. The gate
#: for the cards is the hosts' policy (the hook) and the issuer (C3); this list only makes
#: the refusal early and clear. It changes with the lead's entry, by a release; never this
#: repository (policy.SELF).
PUBLIC_CALLERS: frozenset[str] = frozenset()
#: the events a public caller's run takes (16.7, 18.7): a same-repository pull request, a push
#: to a protected ref; every other one is refused in ``admit``, as the hook refuses it
PUBLIC_EVENTS = ('pull_request', 'push')
HOSTED = 'ubuntu-latest'
#: a public plan's needs and their labels (18.7): CPU work on GitHub's hosted runners, its
#: ``gpu`` parts on its own confined single-job runners; ``gpu2``, ``timing`` and the cluster
#: (an overflow) never
PUBLIC_LABELS = {'cpu': [HOSTED], 'light': [HOSTED], 'gpu': [rt.BASE, 'ci-pub-gpu']}


def is_public(env: Mapping[str, str]) -> bool:
    """Whether this step is a public caller's (``public.yml`` sets ``CIRUNNERS_PUBLIC``)."""
    return env.get('CIRUNNERS_PUBLIC') == 'true'


def _login_of(obj: object, key: str = 'login') -> object:
    return obj.get(key) if isinstance(obj, dict) else None


def admit(env: Mapping[str, str], event: object) -> str:
    """Why a public caller's run is refused, or '' (decision 16.7, 18.7). ``event`` is the
    parsed ``GITHUB_EVENT_PATH``. Pure."""
    repo = str(env.get('GITHUB_REPOSITORY') or '').lower()
    if repo.split('/')[0] != ORG or str(env.get('GITHUB_REPOSITORY_OWNER') or '').lower() != ORG:
        return (f'{repo or "this repository"}: ci-runners serves the public repositories of the {ORG} '
                'organisation only (decision 18.7)')
    name = env.get('GITHUB_EVENT_NAME', '')
    if name not in PUBLIC_EVENTS:
        return f'{name or "an event"}: a public repository runs on a same-repository pull_request or a protected push only'
    if not isinstance(event, dict):
        return 'the event payload does not load'
    actors = [env.get('GITHUB_ACTOR'), env.get('GITHUB_TRIGGERING_ACTOR'), _login_of(event.get('sender'))]
    users = [event.get('sender')]  # GitHub's account type: a bot whose login has no [bot] too
    if name == 'pull_request':
        pr = event.get('pull_request') if isinstance(event.get('pull_request'), dict) else {}
        names = [_login_of(_login_of(pr.get(side), 'repo'), 'full_name') for side in ('head', 'base')]
        if not all(isinstance(n, str) and n.lower() == repo for n in names):
            return "a fork's pull request: a public repository runs its own branches' only"
        actors.append(_login_of(pr.get('user')))
        users.append(pr.get('user'))
    elif env.get('GITHUB_REF_PROTECTED') != 'true':
        return 'a push to a ref that is not protected: a public repository runs on a protected push only'
    else:
        actors.append(_login_of(event.get('pusher'), 'name'))
    if not all(isinstance(x, str) and x and not x.lower().endswith('[bot]') for x in actors) \
            or any(_login_of(u, 'type') == 'Bot' for u in users):
        return "a bot, or an actor not named: a public repository runs its members' pushes and pull requests only"
    return ''


def run_admit(env: Mapping[str, str]) -> int:
    try:
        with open(env.get('GITHUB_EVENT_PATH', ''), 'rb') as f:
            event = json.loads(f.read(64 << 20).decode('utf-8'))
    except (OSError, ValueError, RecursionError):
        event = None
    why = admit(env, event)
    if why:
        print(f'::error::ci-runners: {why}')
        _summary(env, f'## ci-runners: refused\n\n{why}\n')
        return EXIT_RED
    print(f'ci-run flow admit: {env.get("GITHUB_REPOSITORY", "")} on {env.get("GITHUB_EVENT_NAME", "")}', file=sys.stderr)
    return 0


def route_public(parts: Sequence[Mapping], repo: str) -> list[dict]:
    """A public caller's route (decision 18.7): ``cpu`` and ``light`` parts on GitHub's hosted
    runners, ``gpu`` parts on its own pool's runners where ``repo`` is opted in to the public
    GPU pool, nothing else and never an overflow to the cluster. Raises ``route.PlanError``
    naming each part it cannot run. Pure."""
    bad = [f'{p.get("id")}: needs {p.get("needs")!r}: a public repository runs cpu and light parts on '
           'hosted runners and gpu parts on its own pool only (decision 18.7)'
           for p in parts if p.get('needs') not in PUBLIC_LABELS]
    repo = repo.lower()
    if repo not in PUBLIC_CALLERS or repo == policy.SELF:
        bad += [f'{p.get("id")}: needs \'gpu\': {repo or "this repository"} is not opted in to the public GPU pool '
                '(decision 18.7)' for p in parts if p.get('needs') == 'gpu']
    if bad:
        raise rt.PlanError('; '.join(bad))
    return [{**p, 'labels': list(PUBLIC_LABELS[p['needs']]), 'where': 'host', 'target': '',
             'timeout_minutes': rt._timeout(p['minutes'], 1.0),
             'why': 'hosted' if PUBLIC_LABELS[p['needs']] == [HOSTED] else 'its own pool'} for p in parts]


def retry_public(part: Mapping, first: Mapping) -> dict:
    """``route.retry`` for a public part (decision 18.7): there is no salvage leg, and every
    row (``cool-card`` and ``same-host``, which name a host, ``more-memory``, with its resume,
    ``any`` and ``fallback``) is the part's own labels again. Pure."""
    row = rt.row_of(str(first.get('outcome', '')), str(first.get('next', '')))
    out = {'retry': True, 'row': row, 'labels': list(PUBLIC_LABELS[part['needs']]), 'where': 'host', 'target': '',
           'timeout_minutes': part['timeout_minutes'], 'resume': row == 'more-memory'}
    if int(first.get('attempt') or 1) > 1 or first.get('next') not in ('retry', 'fallback') or row not in rt.ROWS:
        return {**out, 'retry': False, 'labels': [], 'resume': False, 'why': 'no retry: final, or retried once already'}
    return {**out, 'why': 'a public part: its own labels again'}


def run_verdict(a: argparse.Namespace) -> int:
    """A public caller's verdict on a hosted runner (``verdict.main`` without the salvage)."""
    from ci_runners import verdict
    if not is_public(os.environ):
        raise FlowError('verdict is a public caller\'s (ci.yml\'s own is actions/verdict)')
    return verdict.main(['--context', a.context, '--sha', a.sha, '--parts', a.parts], None)


def repository(env: Mapping[str, str], call: Callable) -> tuple[str, bool]:
    """The repository's default branch and whether it is private (an event without a
    ``repository`` in its payload, such as ``schedule``, names neither)."""
    base, repo, token = _env(env)
    got = call('GET', f'{base}/repos/{repo}', token)
    name = got.get('default_branch') if isinstance(got, dict) else None
    if not isinstance(name, str) or not name:
        raise github.ApiError(f'GET {repo}: no default branch')
    return name, got.get('private') is not False


# ------------------------------------------------------------------ the contract (16.6)

def _contract(text: str | None) -> tuple[int | None, str]:
    """A spec's ``contract``: (N, '') or (None, why not)."""
    if text is None:
        return None, f'no {SPEC}'
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None, f'{SPEC} does not load'
    v = doc.get('contract')
    if v is None:
        return None, 'no contract'
    if type(v) is not int or v < 0:
        return None, 'contract is not a whole number'
    return v, ''


def check_contract(default: str | None, head: str | None, branch: str) -> tuple[str, str]:
    """``(state, description)`` of a head's contract against its default branch's (16.6,
    §4.10). ``state`` is ``ok``, ``failure`` (the head is behind: "merge origin/<branch>")
    or ``error`` (the default branch's own spec does not say a contract). Pure."""
    want, why = _contract(default)
    if want is None:
        if why in (f'no {SPEC}', 'no contract'):
            return 'ok', f'{branch} sets no contract'
        return 'error', f'{branch}: {why}'
    have, why = _contract(head)
    if have is None:
        return 'failure', f'{why} here, contract {want} on {branch}: merge origin/{branch}'
    if have < want:
        return 'failure', f'contract {have} here, {want} on {branch}: merge origin/{branch}'
    return 'ok', f'contract {have}, {branch} {want}'


def run_contract(a: argparse.Namespace, env: Mapping[str, str], call: Callable) -> int:
    try:
        # the three reads at once (#239: about 2 s one after another through the egress); the
        # default branch's spec waits for the repository only where the event names no branch
        with concurrent.futures.ThreadPoolExecutor(3) as pool:
            repo = pool.submit(repository, env, call)
            head = pool.submit(contents, env, call, SPEC, a.sha)
            named = pool.submit(contents, env, call, SPEC, a.default_branch) if a.default_branch else None
            branch, private = repo.result()
            branch = a.default_branch or branch
            default = named.result() if named else contents(env, call, SPEC, branch)
            state, text = check_contract(default, head.result(), branch)
        if private == is_public(env):  # each caller its own legs (18.7): the workflow's guard misses some events
            state, text = 'error', PRIVATE_ON_PUBLIC if private else PUBLIC
    except github.ApiError as e:
        state, text = 'error', f'contract: {SPEC} not read ({e})'
    print(f'ci-run flow contract: {state}: {text}', file=sys.stderr)
    if state == 'ok':
        _out(env, ok='true')
        return 0
    _summary(env, f'## {a.context}: {state}\n\n{text}\n')
    _out(env, ok='false', state=state, description=_text(text))  # the verdict job posts it
    return EXIT_RED


# ------------------------------------------------------------------ the plan (§4.10)

def _relpath(v: object) -> bool:
    """A relative path that stays below its root: no absolute path, no ``..``, plain names."""
    return (isinstance(v, str) and 0 < len(v) <= 200 and bool(SAFE_PATH.fullmatch(v))
            and not v.startswith('/') and all(x not in ('', '.', '..') for x in v.rstrip('/').split('/')))


def load(doc: object) -> tuple[list[dict], dict]:
    """The parts of a plan and its options (``files``, ``max_parallel``): ``route.load_plan``'s
    checks, then this workflow's own (§4.10). Raises ``route.PlanError`` naming each fault."""
    parts = rt.load_plan(doc)
    errors = []
    opts = {'files': doc.get('files', ''), 'max_parallel': doc.get('max_parallel')}
    if 'files' in doc and not _relpath(doc['files']):
        errors.append('files is a relative path below the checkout, without ".."')
    mp = opts['max_parallel']
    if mp is not None and (type(mp) is not int or not 1 <= mp <= registry.MAX_PARALLEL_BOUND):
        errors.append(f'max_parallel is an integer in [1, {registry.MAX_PARALLEL_BOUND}]')
    for p in parts:
        pid = p['id'] if isinstance(p['id'], str) and len(p['id']) <= MAX_ID else f'{str(p["id"])[:16]}...'
        if p['id'] in RESERVED:
            errors.append(f'{pid}: the id is reserved for the plan job')
        if len(p['id']) > MAX_ID:
            errors.append(f'{pid}: an id is at most {MAX_ID} characters (its artifact\'s name)')
        if len(p['command'].encode()) > MAX_COMMAND:
            errors.append(f'{pid}: command is at most {MAX_COMMAND} bytes')
        if 'memory_gib' in p and p['memory_gib'] > MAX_MEMORY_GIB:
            errors.append(f'{pid}: memory_gib is at most {MAX_MEMORY_GIB}')
        if 'results' in p and not isinstance(p['results'], str):
            errors.append(f'{pid}: results is a string (a glob)')
        q = p.get('queue_minutes')
        if q is not None and (type(q) not in (int, float) or not 0 < q <= rt.MAX_MINUTES):
            errors.append(f'{pid}: queue_minutes is a number in (0, {rt.MAX_MINUTES}]')
        arts = p.get('artifacts')
        if arts is not None and (not isinstance(arts, dict) or len(arts) > MAX_ARTIFACTS or not all(
                isinstance(k, str) and ARTIFACT_NAME.fullmatch(k) and _relpath(v) for k, v in arts.items())):
            errors.append(f'{pid}: artifacts maps at most {MAX_ARTIFACTS} names ([A-Za-z0-9][A-Za-z0-9_.]*, '
                          'no "-") to relative paths below RUNNER_TEMP')
        elif arts and 'ci' in arts:
            errors.append(f'{pid}: the artifact name ci is reserved (ci-part-*, ci-plan-files)')
    if errors:
        raise rt.PlanError('; '.join(errors))
    return parts, opts


def check_files(workspace: str, rel: str) -> str:
    """Why the plan's ``files`` directory ``rel`` below ``workspace`` cannot be handed to
    the parts, or ''. The plan job uploads it hidden entries and all (``.name``, a hidden
    directory too), so it refuses here what that upload cannot carry: a link on its path or
    inside it, an entry that is neither a file nor a directory, a name with a character the
    upload refuses (``UNCARRIED``) or not in UTF-8, a ``.git`` (a clone's config may hold the
    plan command's token, which no part may get), an empty directory (the upload keeps files alone; the
    whole directory empty fails it), and more than the bounds."""
    root = os.path.join(workspace, rel)
    if os.path.realpath(root) != os.path.join(os.path.realpath(workspace), rel.rstrip('/')):
        return f'files: {rel} goes through a link'
    if not os.path.isdir(root):
        return 'files: not a directory'
    total = count = 0
    for d, dirs, names in os.walk(root):
        if not dirs and not names:
            return f'files: {os.path.relpath(d, root) if d != root else rel} is an empty directory (the upload carries files)'
        for n in dirs + names:
            f = os.path.join(d, n)
            count += 1
            if os.path.islink(f):
                return f'files: {os.path.relpath(f, root)} is a link'
            if n == '.git':  # a clone's config may hold the plan command's token (plan-token)
                return f'files: {os.path.relpath(f, root)} is a git directory (it may hold a credential)'
            try:
                n.encode('utf-8')
            except UnicodeEncodeError:
                return f'files: {os.path.relpath(f, root)!a} is not a UTF-8 name (the upload cannot name it)'
            if UNCARRIED & set(n):
                return f'files: {os.path.relpath(f, root)!r} has a character the upload refuses (" : < > | * ? or a line break)'
            if n in names:
                if not os.path.isfile(f):
                    return f'files: {os.path.relpath(f, root)} is not a file (a pipe, a socket or a device)'
                total += os.path.getsize(f)
        if count > MAX_FILES or total > MAX_FILES_BYTES:
            return f'files: more than {MAX_FILES} entries or {MAX_FILES_BYTES >> 20} MiB'
    return ''


#: the plan's files artifact (ci.yml's plan job uploads it, each part reads it)
FILES_ARTIFACT = 'ci-plan-files'
#: its zip: the files' bound, and room for a header per entry
MAX_FILES_ZIP = MAX_FILES_BYTES + (2 << 20)


def unpack_files(data: bytes, dest: str) -> int:
    """The plan's files from their artifact's zip ``data`` into ``dest`` (which must not
    exist yet); how many files. Refused (ValueError), with nothing left behind, for what
    ``check_files`` refuses on the upload side (#235): a name outside ``dest`` (absolute,
    ``..``, a backslash, an empty or ``.`` part), a link or an entry that is neither a
    file nor a directory, a ``.git``, a character the upload refuses (``UNCARRIED``), a name
    given twice, and more than ``MAX_FILES`` entries or ``MAX_FILES_BYTES`` (the sizes the
    zip declares, which zipfile reads no more than, checking each file's CRC)."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        infos = z.infolist()
    except Exception as e:  # a bad zip: zipfile, zlib, ...
        raise ValueError(f'files: the zip cannot be read ({type(e).__name__})') from None
    seen: set[str] = set()
    names: set[str] = set()
    files, total = [], 0
    for info in infos:
        name = info.filename
        parts = name.rstrip('/').split('/') if name.endswith('/') else name.split('/')
        if (not name or name.startswith('/') or '\\' in name or any(p in ('', '.', '..') for p in parts)
                or re.match(r'[A-Za-z]:', name)):
            raise ValueError(f'files: {name!r} is not a path below the files')
        for p in parts:
            if p == '.git':
                raise ValueError(f'files: {name!r} is a git directory (it may hold a credential)')
            if UNCARRIED & set(p):
                raise ValueError(f'files: {name!r} has a character the upload refuses')
        kind = stat.S_IFMT(info.external_attr >> 16)
        if kind == stat.S_IFLNK:
            raise ValueError(f'files: {name!r} is a link')
        if kind not in (0, stat.S_IFREG, stat.S_IFDIR) or (kind == stat.S_IFDIR) != info.is_dir():
            raise ValueError(f'files: {name!r} is not a file or a directory')
        for i in range(1, len(parts) + 1):  # the directories it implies count as entries, as on the upload side
            seen.add('/'.join(parts[:i]) + ('/' if i < len(parts) or info.is_dir() else ''))
        if not info.is_dir():
            if name in names:
                raise ValueError(f'files: {name!r} is named twice')
            names.add(name)
            files.append(info)
            total += info.file_size
        if len(seen) > MAX_FILES or total > MAX_FILES_BYTES:
            raise ValueError(f'files: more than {MAX_FILES} entries or {MAX_FILES_BYTES >> 20} MiB')
    if {s.rstrip('/') for s in seen if s.endswith('/')} & names:
        raise ValueError('files: a name is both a file and a directory')
    # a new directory, and only the directories and files made below: with no link among the
    # entries, none is on a path; O_NOFOLLOW and O_EXCL hold that for each file too
    os.mkdir(dest, 0o700)  # FileExistsError where it is there: nothing is merged into another's
    try:
        for info in files:
            path = os.path.join(dest, *info.filename.split('/'))
            os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
            with os.fdopen(fd, 'wb') as out, z.open(info) as src:
                shutil.copyfileobj(src, out)
    except BaseException as e:
        shutil.rmtree(dest, ignore_errors=True)
        if isinstance(e, (OSError, zipfile.BadZipFile, EOFError, zlib.error, NotImplementedError, RuntimeError)):
            raise ValueError(f'files: the zip cannot be unpacked ({type(e).__name__})') from None
        raise
    return len(files)


def run_fetch(a: argparse.Namespace, env: Mapping[str, str], call: Callable, fetch: Callable) -> int:
    """The plan's files through the API (#245): the run's newest ``ci-plan-files``, its zip
    read and unpacked into ``--dest`` (``unpack_files``). Where the API cannot be read (no
    ``actions: read``, an outage, no such artifact) it says so in ``fallback=true`` and
    exits 0, and the workflow's next step downloads them with ``download-artifact``; what
    the zip holds is never a reason to fall back: a refusal fails the step."""
    from ci_runners import verdict
    base, repo, token = _env(env)
    run = env.get('GITHUB_RUN_ID', '')
    if not run.isdigit():
        raise FlowError('GITHUB_RUN_ID is malformed')
    try:
        arts = [x for x in verdict.run_artifacts(call, base, repo, run, token)
                if isinstance(x, dict) and x.get('name') == FILES_ARTIFACT and type(x.get('id')) is int
                and isinstance(x.get('created_at'), str) and not x.get('expired')]
        if not arts:
            raise github.ApiError(f'no {FILES_ARTIFACT} artifact listed')
        aid = max(arts, key=lambda x: (x['created_at'], x['id']))['id']
        data = fetch(f'{base}/repos/{repo}/actions/artifacts/{aid}/zip', token, MAX_FILES_ZIP)
    except (github.ApiError, OSError, http.client.HTTPException) as e:
        hint = '; give the job actions: read' if 'HTTP 403' in str(e) or 'HTTP 404' in str(e) else ''
        print(f'::notice::ci-run flow fetch: the plan\'s files are not read through the API ({e}){hint}: '
              'download-artifact downloads them', file=sys.stderr)
        _out(env, fallback='true')
        return 0
    n = unpack_files(data, a.dest)
    _out(env, fallback='false')
    print(f'ci-run flow fetch: {n} file(s) of the plan into {a.dest}', file=sys.stderr)
    return 0


def entry(p: Mapping, files: bool = False) -> dict:
    """A routed part as ``part.yml`` reads it (its ``part`` input): the plan's keys and
    the route's, the part action's ``gpus``, ``memory`` and ``queue``, the artifacts as a
    list, whether the plan handed files, and the salvage leg's bound."""
    out = dict(p)
    out['gpus'] = GPUS.get(p['needs'], 0)
    out['memory'] = f'{math.ceil(p["memory_gib"] * 1024)}M' if p.get('memory_gib') is not None else ''
    out['results'] = p.get('results', '')
    q = p['queue_minutes'] if p.get('queue_minutes') is not None else QUEUE_MINUTES
    out['queue'] = f'{q:g}'
    out['timeout_minutes'] = int(p['timeout_minutes']) + math.ceil(q)  # the wait for a slot, then the run
    out['artifacts'] = [{'name': k, 'path': v} for k, v in sorted((p.get('artifacts') or {}).items())]
    out['files'] = files
    out['salvage_minutes'] = out['timeout_minutes'] + SALVAGE_GRACE
    out['salvage_wait'] = out['timeout_minutes'] * 60
    return out


def summary(parts: Sequence[Mapping], capacity_note: str, max_parallel: int) -> str:
    rows = ''.join(f'| `{p["id"]}` | {p["needs"]} | {" ".join(x for x in p["labels"] if x != rt.BASE)}{" " + p["target"] if p["target"] else ""}'
                   f' | {p["minutes"]:g} | {p["timeout_minutes"]} | {p.get("why", "")} |\n' for p in parts)
    return (f'## the plan: {len(parts)} part(s), at most {max_parallel} at once\n\ncapacity: {capacity_note}\n\n'
            '| part | needs | labels | minutes | job bound | why |\n|---|---|---|---|---|---|\n' + rows)


def _rows(rows_file: str, etc: str) -> tuple[list | None, str]:
    try:
        if rows_file:
            with open(rows_file, encoding='utf-8') as f:
                got = json.load(f)
            if not isinstance(got, list) or not all(isinstance(r, dict) and isinstance(r.get('key'), str) for r in got):
                raise capacity.CapacityError(f'{rows_file}: not a list of rows')
            return capacity.rows_from(got), ''
        return capacity.load(etc), ''
    except (capacity.CapacityError, OSError, ValueError) as e:
        return None, rt.redact(str(e) or type(e).__name__)


def _project(etc: str, name: str) -> registry.Project | None:
    """The registry entry of ``name`` for its top-level keys (``max_parallel``,
    ``retries``): as this host sees it, else as the first host of its ``[hosts]`` that
    loads (a light runner's host need not run the project); None where none loads."""
    try:
        from ci_runners import cli
        this = cli.load_inventory().this
    except (OSError, ValueError, KeyError):
        this = ''
    if not registry.NAME.fullmatch(name):
        return None
    try:
        with open(os.path.join(etc, 'projects', f'{name}.toml'), 'rb') as f:
            doc = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    hosts = doc.get('hosts') if isinstance(doc.get('hosts'), dict) else {}
    for role in ([this] if this else []) + sorted(hosts):
        try:
            return registry.parse(doc, role, name)
        except registry.RegistryError:
            continue
    return None


def run_plan(a: argparse.Namespace, env: Mapping[str, str], call: Callable, etc: str) -> int:
    def fail(state: str, text: str, code: int) -> int:
        print(f'ci-run flow plan: {state}: {text}', file=sys.stderr)
        _summary(env, f'## {a.context}: {state}\n\n{text}\n')
        _out(env, state=state, description=_text(text))  # the run's end: the verdict job posts it
        return code

    if a.plan_outcome and a.plan_outcome != 'pass':  # the plan command's own end (§4.4)
        return fail('failure' if a.plan_outcome == 'red' else 'error',
                    f'plan: {a.plan_outcome}' + (f' ({a.plan_reason})' if a.plan_reason else ''), EXIT_RED)
    try:
        if a.plan_file:
            with open(a.plan_file, 'rb') as f:
                raw = f.read(MAX_PLAN + 1)
            if len(raw) > MAX_PLAN:
                raise rt.PlanError(f'the plan is larger than {MAX_PLAN} bytes')
            try:
                doc = json.loads(raw.decode('utf-8'))
            except (UnicodeDecodeError, ValueError) as e:
                raise rt.PlanError(f'the plan command\'s standard output is not one JSON document ({type(e).__name__})')
        else:
            doc = DEFAULT_PLAN
        parts, opts = load(doc)
        if opts['files']:
            why = check_files(a.workspace or env.get('GITHUB_WORKSPACE', '.'), opts['files'])
            if why:
                raise rt.PlanError(why)
    except OSError as e:
        return fail('error', f'plan: no plan output ({type(e).__name__})', EXIT_PLAN)
    except rt.PlanError as e:
        return fail('failure', f'plan: {e}', EXIT_PLAN)
    public = is_public(env)
    rows, unread = (None, 'a public caller') if public else _rows(a.rows, etc)
    try:
        routed = route_public(parts, env.get('GITHUB_REPOSITORY', '')) if public else rt.route(parts, rows, unread)
    except rt.PlanError as e:
        return fail('failure', f'plan: {e}', EXIT_PLAN)
    except rt.RouteError as e:
        for line in e.errors:
            print(f'ci-run flow plan: route: {line}', file=sys.stderr)
        return fail('error', f'route: {e.errors[0]}', EXIT_RED)
    proj = None if public else _project(etc, a.project)
    most = proj.max_parallel if proj is not None else MAX_PARALLEL
    most = min(most, opts['max_parallel'] or most)  # a plan lowers the registry's cap, never raises it
    matrix = [entry(p, bool(opts['files'])) for p in routed]
    text = json.dumps(matrix, sort_keys=True, separators=(',', ':'))
    if len(text) > MAX_MATRIX:
        return fail('failure', f'plan: the routed parts take {len(text)} bytes, more than {MAX_MATRIX}', EXIT_PLAN)
    note = 'read' if rows else f'unreadable ({unread or "no rows"}): every part on its shared label, no overflow'
    if public:
        note = 'not read: a public caller, its CPU parts hosted and its gpu parts on its own pool (decision 18.7)'
    _summary(env, summary(matrix, note, most))
    print(f'ci-run flow plan: {len(matrix)} part(s), max_parallel {most}, capacity {note}', file=sys.stderr)
    if not matrix or a.plan_only == 'true':  # nothing to run, or a plan-only run: the plan job's state is the run's
        text_ = 'nothing to run' if not matrix else f'plan only: {len(matrix)} part(s) routed, none run'
        _out(env, parts='[]', ids='', max_parallel=str(most), count=str(len(matrix)), files=opts['files'],
             state='success', description=text_)
        return 0
    _out(env, parts=text, ids=' '.join(p['id'] for p in matrix), max_parallel=str(most), count=str(len(matrix)),
         files=opts['files'], routed='true')
    return 0


# ------------------------------------------------------------------ the retry (16.5)

def _first(doc: Mapping | None, role: str) -> dict:
    """``route.retry``'s ``first`` from an attempt's ``outcome.json`` (the part action's
    outputs, or the salvage's); a missing one is ``lost`` on ``role``."""
    if not isinstance(doc, Mapping) or not isinstance(doc.get('outcome'), str):
        return {'outcome': 'lost', 'next': 'retry', 'attempt': 1, 'host': role, 'record': ''}
    card = str(doc.get('cool-card', ''))
    labels = doc.get('pool-labels')
    try:
        attempt = int(doc.get('attempt') or 1)
    except (TypeError, ValueError):
        attempt = 99  # unreadable: never retried again
    return {'outcome': doc['outcome'], 'next': str(doc.get('next', '')), 'attempt': attempt,
            'host': str(doc.get('host') or role), 'pool_labels': labels if isinstance(labels, list) else [],
            'cool_card': int(card) if card.isdigit() else None, 'record': str(doc.get('record') or '')}


def attempt_of(first: Mapping, part_id: str, env: Mapping[str, str]) -> str:
    """The retry's ``attempt-of``: the first attempt's record, else its name as the record
    would have it (``<role>:<repo>/<run>-<run attempt>/<id>-1``), which the part action
    takes by name (decision 13.3), so the retry is attempt 2 wherever it runs."""
    if first.get('record'):
        return str(first['record'])
    role = str(first.get('host') or '')
    role = role if re.fullmatch(r'[a-z0-9][a-z0-9-]{0,30}', role) else 'unknown'
    run = f'{env.get("GITHUB_RUN_ID", "0")}-{env.get("GITHUB_RUN_ATTEMPT", "1")}'
    return f'{role}:{env.get("GITHUB_REPOSITORY", "").lower()}/{run}/{part_id}-1'


def decide(part: Mapping, doc: Mapping | None, role: str, rows: Sequence | None, unread: str,
           retries: int, env: Mapping[str, str]) -> dict | None:
    """The retry of ``part`` after its first attempt's ``doc`` (None: no outputs, lost),
    or None for no retry. Pure but for ``env``'s run names."""
    first = _first(doc, role)
    if retries < 1:
        return None
    q = math.ceil(float(part.get('queue') or 0))  # the route's bound without the queue, and the queue on top
    bare = {**part, 'timeout_minutes': int(part['timeout_minutes']) - q}
    r = retry_public(bare, first) if is_public(env) else rt.retry(bare, first, rows, unread)
    if not r['retry']:
        return None
    return {'labels': r['labels'], 'timeout_minutes': int(r['timeout_minutes']) + q, 'target': r['target'],
            'resume': 'true' if r['resume'] else 'false', 'attempt_of': attempt_of(first, part['id'], env),
            # more memory: the bigger slot's own cap, not the plan's that the first attempt hit
            'memory': '' if r['row'] == 'more-memory' else str(part.get('memory') or ''),
            'row': r['row'], 'why': r['why'], 'first': first['outcome']}


def _read_json(path: str) -> dict | None:
    try:
        with open(path, encoding='utf-8') as f:
            got = json.loads(f.read(MAX_PLAN))
    except (OSError, ValueError):
        return None
    return got if isinstance(got, dict) else None


def run_retry(a: argparse.Namespace, env: Mapping[str, str], etc: str) -> int:
    try:
        part = json.loads(a.part)
        if not isinstance(part, dict) or not isinstance(part.get('labels'), list):
            raise ValueError
        if is_public(env) and part.get('needs') not in PUBLIC_LABELS:
            raise ValueError
    except ValueError:
        raise FlowError('--part is not a routed part') from None
    doc = None
    for f in a.outcome_file:  # the first that holds an outcome of this run attempt
        got = _read_json(f)
        if isinstance(got, dict) and isinstance(got.get('outcome'), str) and str(
                got.get('run-attempt') or env.get('GITHUB_RUN_ATTEMPT', '1')) == env.get('GITHUB_RUN_ATTEMPT', '1'):
            doc = got
            break
    proj = _project(etc, a.project) if a.project and not is_public(env) else None
    rows, unread = (None, 'a public caller') if is_public(env) else _rows(a.rows, etc)
    r = decide(part, doc, a.role, rows, unread, proj.retries if proj is not None else 1, env)
    _out(env, retry=json.dumps(r, sort_keys=True, separators=(',', ':')) if r else 'none')
    print(json.dumps(r['labels']) if r else 'none')
    first = (doc or {}).get('outcome', 'lost (no outputs)')
    print(f'ci-run flow retry: {part.get("id")}: first {first}: '
          + (f'{r["row"]}: {r["why"]}' if r else 'no retry'), file=sys.stderr)
    _summary(env, f'- part `{part.get("id")}`: first attempt {first}; '
             + (f'retry on `{" ".join(r["labels"][1:])}` ({r["row"]}: {r["why"]})\n' if r else 'no retry\n'))
    return 0


# ------------------------------------------------------------------ the salvage leg's host (§4.5)

def role_of(runner: str, roles: Sequence[str]) -> str:
    """The host role a runner's name starts with (``<role>-<pool>-<i>``, ``<role>-gpu<N>``,
    §5.1): the longest of ``roles`` followed by ``-``, or ''. Pure."""
    fits = [r for r in roles if r and runner.startswith(r + '-')]
    return max(fits, key=len, default='')


def light_roles(rows: Sequence) -> list[str]:
    """The roles of the hosts a salvage leg can reach now: up (16.5's rule: a job queued on
    a label no runner serves waits 24 h), and where the row lists its runners, one on
    ``<role>-light``. Pure."""
    out = set()
    for r in rows:
        if r.target or r.down:
            continue
        for x in {r.machine, str(r.row.get('role') or '')} - {''}:
            if not r.runners or (r.runners.get(f'{x}-light') and r.runners[f'{x}-light'].registered > 0):
                out.add(x)
    return sorted(out)


def first_runner(jobs: Sequence[Mapping], part_id: str) -> str:
    """The runner of part ``part_id``'s first attempt: the one job named ``... / part <id> /
    first``; '' where there is none, or several, or it has no runner. Pure."""
    want = ['part ' + part_id, 'first']
    hits = [j for j in jobs if isinstance(j, Mapping) and str(j.get('name', '')).split(' / ')[-2:] == want]
    if len(hits) != 1:
        return ''
    return str(hits[0].get('runner_name') or '')


def run_jobs(env: Mapping[str, str], call: Callable) -> list:
    base, repo, token = _env(env)
    run, attempt = env.get('GITHUB_RUN_ID', ''), env.get('GITHUB_RUN_ATTEMPT', '1')
    if not run.isdigit() or not attempt.isdigit():
        raise FlowError('GITHUB_RUN_ID or GITHUB_RUN_ATTEMPT is malformed')
    out: list = []
    for page in range(1, MAX_JOB_PAGES + 1):
        got = call('GET', f'{base}/repos/{repo}/actions/runs/{run}/attempts/{attempt}/jobs?per_page=100&page={page}',
                   token)
        jobs = got.get('jobs') if isinstance(got, dict) else None
        if not isinstance(jobs, list):
            raise github.ApiError('GET jobs: no jobs')
        out += jobs
        if len(jobs) < 100:
            break
    return out


def run_locate(a: argparse.Namespace, env: Mapping[str, str], call: Callable, etc: str) -> int:
    try:
        runner = first_runner(run_jobs(env, call), a.part_id)
    except (github.ApiError, FlowError) as e:
        print(f'ci-run flow locate: the run\'s jobs could not be read ({e}): no salvage leg', file=sys.stderr)
        runner = ''
    rows, _ = _rows(a.rows, etc)
    role = role_of(runner, light_roles(rows or []))
    labels = json.dumps([rt.BASE, f'{role}-light']) if role else ''
    _out(env, labels=labels, role=role)
    print(f'ci-run flow locate: {a.part_id}: first attempt on runner {runner or "(none)"}: '
          + (f'salvage on {role}-light' if role else 'no host known, no salvage leg'), file=sys.stderr)
    return 0


# ------------------------------------------------------------------ outputs and the command

def _out(env: Mapping[str, str], **kv: str) -> None:
    if not env.get('GITHUB_OUTPUT'):
        return
    with open(env['GITHUB_OUTPUT'], 'a', encoding='utf-8') as f:
        for k, v in kv.items():
            if '\n' in v:
                raise FlowError(f'output {k} spans lines')
            f.write(f'{k}={v}\n')


def _summary(env: Mapping[str, str], text: str) -> None:
    if env.get('GITHUB_STEP_SUMMARY'):
        with open(env['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as f:
            f.write(text)


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog='ci-run flow')
    sub = ap.add_subparsers(dest='cmd', required=True)
    c = sub.add_parser('contract')
    c.add_argument('--context', required=True)
    c.add_argument('--sha', required=True)
    c.add_argument('--default-branch', default='')
    p = sub.add_parser('plan')
    p.add_argument('--context', required=True)
    p.add_argument('--sha', required=True)
    p.add_argument('--project', required=True)
    p.add_argument('--plan-file', default='')
    p.add_argument('--plan-outcome', default='')
    p.add_argument('--plan-reason', default='')
    p.add_argument('--plan-only', choices=('true', 'false', ''), default='')
    p.add_argument('--workspace', default='')
    p.add_argument('--rows', default='')
    r = sub.add_parser('retry')
    r.add_argument('--part', required=True)
    r.add_argument('--outcome-file', action='append', default=[])
    r.add_argument('--role', default='')
    r.add_argument('--project', default='')
    r.add_argument('--rows', default='')
    lo = sub.add_parser('locate')
    lo.add_argument('--part-id', required=True)
    lo.add_argument('--rows', default='')
    cl = sub.add_parser('claim')
    cl.add_argument('--context', required=True)
    cl.add_argument('--sha', required=True)
    pe = sub.add_parser('pending')
    pe.add_argument('--context', required=True)
    pe.add_argument('--sha', required=True)
    pe.add_argument('--seen', required=True)
    pe.add_argument('--description', default='queued on the runners')
    sub.add_parser('admit')
    fe = sub.add_parser('fetch')
    fe.add_argument('--dest', required=True)
    v = sub.add_parser('verdict')
    v.add_argument('--context', required=True)
    v.add_argument('--sha', required=True)
    v.add_argument('--parts', default='')
    s = sub.add_parser('status')
    s.add_argument('--context', required=True)
    s.add_argument('--sha', required=True)
    s.add_argument('--state', choices=STATES, required=True)
    s.add_argument('--description', required=True)
    return ap


def main(argv: Sequence[str], etc: str, call: Callable | None = None, env: Mapping[str, str] | None = None,
         fetch: Callable | None = None) -> int:
    a = parser().parse_args(list(argv))
    env = os.environ if env is None else env
    if a.cmd == 'fetch' and (call is None or fetch is None):  # kept connections (#242)
        from ci_runners import client
        own = client.Client(env)
        try:
            return main(argv, etc, call or own.api, env, fetch or own.artifact)
        finally:
            own.close()
    call = call or github.api
    try:
        if a.cmd == 'fetch':
            return run_fetch(a, env, call, fetch)
        if a.cmd == 'contract':
            return run_contract(a, env, call)
        if a.cmd == 'plan':
            return run_plan(a, env, call, etc)
        if a.cmd == 'retry':
            return run_retry(a, env, etc)
        if a.cmd == 'locate':
            return run_locate(a, env, call, etc)
        if a.cmd == 'claim':
            return run_claim(a, env, call, etc)
        if a.cmd == 'pending':
            return run_pending(a, env, call, etc)
        if a.cmd == 'admit':
            return run_admit(env)
        if a.cmd == 'verdict':
            return run_verdict(a)
        post(env, call, context=a.context, sha=a.sha, state=a.state, description=a.description)
        return 0
    except (ValueError, github.ApiError, OSError) as e:  # FlowError, a policy that does not load
        print(f'ci-run flow {a.cmd}: {e}', file=sys.stderr)
        return 2
