"""The pre-job hook's decision: may this job run on this runner (design §6.2, §6.3)?

The policy is schema 2 (design §6.5; schema 1 is retired, decision 11.12). It may take
its actors and private repositories from the org snapshot (``etc/org.json``):
``with_snapshot`` merges them in, or refuses a stale one. Only private repositories run
(design §6.2): a repository listed as public is refused.

``decide`` is a pure function of the policy, the job's environment and its event
payload. It reads only what it is given, prints nothing and returns a fixed reason
code, so that no payload text and no inventory reaches a public job log (T9, T10).

Every check fails closed: a missing, mistyped or non-ASCII field refuses the job.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import tomllib
from collections.abc import Mapping

#: this repository: its jobs are refused on every runner (AGENTS.md, design §6.2)
SELF = 'chaobrain/ci-runners'

#: runner classes (``CI_RUNNER_CLASS``, design §5.1, decision 12.5)
CLASSES = frozenset({'light', 'part', 'h100', 'gpu', 'multi', 'compile', 'nightly'})
#: the classes that hold a host exclusively: accepted only for a repository whose entry
#: lists them in ``classes`` (decision 12.14); the others are open to every repository
OPT_IN = frozenset({'compile', 'nightly'})

#: events accepted per repository class (design §6.2)
PRIVATE_EVENTS = frozenset({'pull_request', 'push', 'workflow_dispatch', 'schedule', 'workflow_run', 'status',
                            'repository_dispatch'})
#: events the workflow bot may trigger
BOT_EVENTS = frozenset({'workflow_dispatch', 'repository_dispatch'})
#: the events a private repository's ``extra_events`` may add: those whose payload
#: carries the pull request the fork check reads, and the merge queue's
EXTRA_EVENTS = frozenset({'pull_request_target', 'pull_request_review', 'pull_request_review_comment',
                          'merge_group'})

#: the reason codes ``decide`` returns; the hook prints one of them and nothing else
REASONS = ('ok', 'runner-class', 'self', 'repository', 'repository-id', 'payload', 'paused', 'event', 'fork',
           'actor', 'bot', 'stale', 'policy', 'timeout', 'error')  # from 'stale' on: the hook could not decide

#: the org snapshot's schema, its age bound's range (minutes) and how far ahead of the
#: host's clock its time may be (seconds)
SNAPSHOT_SCHEMA = 1
MAX_AGE_RANGE = (10, 1440)
CLOCK_SKEW = 300

_REPO = re.compile(r'[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}\Z')
_LOGIN = re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})(?:\[bot\])?\Z')
_EVENT = re.compile(r'[a-z_]{1,50}\Z')
_DIGITS = re.compile(r'[0-9]{1,20}\Z')
_ORG = re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})\Z')


class PolicyError(ValueError):
    """The policy file is malformed; the hook then refuses every job."""


class StaleSnapshot(PolicyError):
    """The org snapshot is older than the policy allows; the hook refuses every job."""


@dataclasses.dataclass(frozen=True)
class Repo:
    name: str  # ``owner/name``, lower case
    id: int
    visibility: str  # ``private`` or ``public``
    paused: bool = False
    extra_events: frozenset[str] = frozenset()  # private only: events beyond PRIVATE_EVENTS
    bot_dispatch: bool = False  # private only: the bot may start BOT_EVENTS (lead, 2026-11-05)
    org_members: bool = False  # set by ``with_snapshot`` only: the org's members may run here too
    classes: frozenset[str] = frozenset()  # private only: the OPT_IN classes it may use (decision 12.14)


@dataclasses.dataclass(frozen=True)
class Org:
    """A schema 2 policy's ``[org]`` table (design §6.5)."""
    name: str  # the org, lower case: the snapshot's repositories must be its own
    members: bool  # the snapshot's members join the actors
    private_repositories: bool  # the snapshot's repositories join the repositories, as private
    max_age_minutes: int


@dataclasses.dataclass(frozen=True)
class Snapshot:
    """``etc/org.json``, written by ``ci-orgsync`` (design §6.5)."""
    org: str
    fetched_at: float  # Unix time of the fetch
    members: frozenset[str]  # lower-case human logins
    repos: Mapping[str, int]  # lower-case ``owner/name`` -> id, private repositories only
    digest: str = ''


@dataclasses.dataclass(frozen=True)
class Policy:
    actors: frozenset[str]  # allowlisted logins, lower case
    repos: Mapping[str, Repo]  # by lower-case ``owner/name``
    bot: str = 'github-actions[bot]'
    digest: str = ''  # sha256 of the policy file, printed (shortened) with every decision
    org: Org | None = None  # schema 2: merge the org snapshot (``with_snapshot``)
    members: frozenset[str] = frozenset()  # the snapshot's members: actors of ``org_members`` repositories only
    snapshot_digest: str = ''  # set by ``with_snapshot``; printed beside the policy's


@dataclasses.dataclass(frozen=True)
class Decision:
    accepted: bool
    reason: str  # one of REASONS


def _ascii_lower(s: object, pattern: re.Pattern[str]) -> str | None:
    """``s`` lower-cased if it is a str that matches ``pattern`` in full, else None.
    Matching first keeps non-ASCII look-alikes out (``'\\u212a'.lower() == 'k'``)."""
    if isinstance(s, str) and pattern.match(s):
        return s.lower()
    return None


def load(data: bytes | str) -> Policy:
    """Parse ``policy.toml`` (schema 2). Raises PolicyError on anything unexpected."""
    raw = data.encode() if isinstance(data, str) else data
    try:
        doc = tomllib.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise PolicyError(f'policy: not TOML: {e}') from None
    schema = doc.get('schema')
    if type(schema) is not int or schema != 2:
        raise PolicyError('policy: schema must be 2')
    unknown = set(doc) - {'schema', 'bot', 'actors', 'repositories', 'org'}
    if unknown:
        raise PolicyError(f'policy: unknown keys {sorted(unknown)}')
    org = _load_org(doc['org']) if 'org' in doc else None
    bot = _ascii_lower(doc.get('bot', 'github-actions[bot]'), _LOGIN)
    if bot is None or not bot.endswith('[bot]'):
        raise PolicyError('policy: bot must be a bot login')
    actors_raw = doc.get('actors', [])
    if not isinstance(actors_raw, list):
        raise PolicyError('policy: actors must be a list')
    actors = set()
    for a in actors_raw:
        login = _ascii_lower(a, _LOGIN)
        if login is None or login.endswith('[bot]'):
            raise PolicyError('policy: an actor is not a human login')
        actors.add(login)
    repos_raw = doc.get('repositories', {})
    if not isinstance(repos_raw, dict):
        raise PolicyError('policy: repositories must be a table')
    repos = {}
    for key, r in repos_raw.items():
        name = _ascii_lower(key, _REPO)
        if name is None or not isinstance(r, dict):
            raise PolicyError('policy: a repository key is not owner/name')
        if name == SELF:
            raise PolicyError(f'policy: {SELF} may not be listed')
        unknown = set(r) - {'id', 'visibility', 'paused', 'extra_events', 'bot_dispatch', 'classes'}
        if unknown:
            raise PolicyError(f'policy: {name}: unknown keys {sorted(unknown)}')
        rid, vis, paused = r.get('id'), r.get('visibility'), r.get('paused', False)
        if type(rid) is not int or rid <= 0:
            raise PolicyError(f'policy: {name}: id must be a positive integer')
        if vis not in ('private', 'public'):
            raise PolicyError(f'policy: {name}: visibility must be private or public')
        if type(paused) is not bool:
            raise PolicyError(f'policy: {name}: paused must be a boolean')
        extra = r.get('extra_events', [])
        if not isinstance(extra, list) or not all(isinstance(e, str) and e in EXTRA_EVENTS for e in extra):
            raise PolicyError(f'policy: {name}: extra_events must name events of {sorted(EXTRA_EVENTS)}')
        if extra and vis == 'public':
            raise PolicyError(f'policy: {name}: a public repository takes no extra events')
        bot_dispatch = r.get('bot_dispatch', False)
        if type(bot_dispatch) is not bool:
            raise PolicyError(f'policy: {name}: bot_dispatch must be a boolean')
        if bot_dispatch and vis == 'public':
            raise PolicyError(f'policy: {name}: a public repository takes no bot dispatch')
        classes = r.get('classes', [])
        if not isinstance(classes, list) or not all(isinstance(c, str) and c in OPT_IN for c in classes):
            raise PolicyError(f'policy: {name}: classes must name classes of {sorted(OPT_IN)}')
        if classes and vis == 'public':
            raise PolicyError(f'policy: {name}: a public repository takes no classes')
        if name in repos:
            raise PolicyError(f'policy: {name} is listed twice')
        repos[name] = Repo(name, rid, vis, paused, frozenset(extra), bot_dispatch, classes=frozenset(classes))
    return Policy(frozenset(actors), repos, bot, hashlib.sha256(raw).hexdigest(), org)


def _load_org(t: object) -> Org:
    if not isinstance(t, dict):
        raise PolicyError('policy: org must be a table')
    unknown = set(t) - {'name', 'members', 'private_repositories', 'max_age_minutes'}
    if unknown:
        raise PolicyError(f'policy: org: unknown keys {sorted(unknown)}')
    name = _ascii_lower(t.get('name'), _ORG)
    if name is None:
        raise PolicyError('policy: org: name must be an org login')
    members, repos = t.get('members', False), t.get('private_repositories', False)
    if type(members) is not bool or type(repos) is not bool:
        raise PolicyError('policy: org: members and private_repositories must be booleans')
    age = t.get('max_age_minutes', 60)
    lo, hi = MAX_AGE_RANGE
    if type(age) is not int or not lo <= age <= hi:
        raise PolicyError(f'policy: org: max_age_minutes must be an integer from {lo} to {hi}')
    return Org(name, members, repos, age)


def load_snapshot(data: bytes | str) -> Snapshot:
    """Parse ``org.json`` (schema 1). Raises PolicyError on anything unexpected: a
    malformed snapshot refuses every job rather than widen or narrow the lists."""
    raw = data.encode() if isinstance(data, str) else data
    try:
        doc = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise PolicyError('org snapshot: not JSON') from None
    if not isinstance(doc, dict) or set(doc) != {'schema', 'org', 'fetched_at', 'members', 'repositories'}:
        raise PolicyError('org snapshot: not a schema 1 snapshot')
    if type(doc['schema']) is not int or doc['schema'] != SNAPSHOT_SCHEMA:
        raise PolicyError('org snapshot: not a schema 1 snapshot')
    org = _ascii_lower(doc['org'], _ORG)
    t = doc['fetched_at']
    if org is None or type(t) not in (int, float) or not 0 < t < 1e11:
        raise PolicyError('org snapshot: org or fetched_at malformed')
    if not isinstance(doc['members'], list) or not isinstance(doc['repositories'], dict):
        raise PolicyError('org snapshot: members must be a list and repositories a table')
    members = set()
    for m in doc['members']:
        login = _ascii_lower(m, _LOGIN)
        if login is None or login.endswith('[bot]'):
            raise PolicyError('org snapshot: a member is not a human login')
        members.add(login)
    repos = {}
    for key, rid in doc['repositories'].items():
        name = _ascii_lower(key, _REPO)
        if name is None or not name.startswith(org + '/') or type(rid) is not int or rid <= 0:
            raise PolicyError("org snapshot: a repository is not one of the org's, with a positive id")
        repos[name] = rid
    return Snapshot(org, float(t), frozenset(members), repos, hashlib.sha256(raw).hexdigest())


def with_snapshot(policy: Policy, snap: Snapshot, now: float) -> Policy:
    """``policy`` with the org snapshot merged in, by its ``[org]`` table (design §6.5):

    - each snapshot repository that the policy does not list joins ``repos`` as private
      if ``private_repositories``. A listed one keeps the policy's entry, so the lead
      can pause it, and a public one is never made private;
    - if ``members``, the snapshot's members may act in the org's private repositories
      of the snapshot (``Repo.org_members``), and only there: a repository listed by hand
      is open to them only if it is private and the snapshot holds it with the same id.
      A public repository, or any other listed one, keeps the hand-kept actors alone.

    Raises StaleSnapshot when the snapshot is older than ``max_age_minutes`` or more
    than ``CLOCK_SKEW`` seconds in the future, and PolicyError when it is another
    org's. A policy without ``[org]`` is returned as it is. Pure.
    """
    org = policy.org
    if org is None:
        return policy
    if snap.org != org.name:
        raise PolicyError('org snapshot: another org')
    if not now - org.max_age_minutes * 60 <= snap.fetched_at <= now + CLOCK_SKEW:
        raise StaleSnapshot('org snapshot: stale')
    repos = dict(policy.repos)
    if org.private_repositories:
        for name, rid in snap.repos.items():
            if name != SELF and name not in repos:
                repos[name] = Repo(name, rid, 'private')
    members = snap.members if org.members else frozenset()
    if members:
        for name, repo in repos.items():
            if repo.visibility == 'private' and snap.repos.get(name) == repo.id and name != SELF:
                repos[name] = dataclasses.replace(repo, org_members=True)
    return dataclasses.replace(policy, repos=repos, members=members, snapshot_digest=snap.digest)


def _repo_matches(r: object, repo: Repo) -> bool:
    """Whether a payload repository object is ``repo``: same id and same name."""
    return (isinstance(r, dict) and type(r.get('id')) is int and r['id'] == repo.id
            and _ascii_lower(r.get('full_name'), _REPO) == repo.name)


def _login(obj: object, key: str) -> str | None:
    """``obj[key]`` as a lower-case login, or None."""
    return _ascii_lower(obj.get(key), _LOGIN) if isinstance(obj, dict) else None


def _actors(env: Mapping[str, str], event: dict, name: str) -> list[str | None]:
    """Every login that this job acts for; None where one is required but absent or
    malformed (design §6.2: the actor, the triggering actor, a pull request's author,
    a push's pusher; also the payload's sender)."""
    out = [_ascii_lower(env.get('GITHUB_ACTOR'), _LOGIN), _ascii_lower(env.get('GITHUB_TRIGGERING_ACTOR'), _LOGIN)]
    if 'sender' in event:
        out.append(_login(event['sender'], 'login'))
    if 'pull_request' in event or name.startswith('pull_request'):
        pr = event.get('pull_request')
        out.append(_login(pr.get('user') if isinstance(pr, dict) else None, 'login'))
    if name == 'push':
        out.append(_login(event.get('pusher'), 'name'))
    if name == 'workflow_run':  # the run that triggered this one
        wr = event.get('workflow_run')
        for key in ('actor', 'triggering_actor'):
            out.append(_login(wr.get(key) if isinstance(wr, dict) else None, 'login'))
    return out


def decide(policy: Policy, env: Mapping[str, str], event: object) -> Decision:
    """Accept or refuse a job, by design §6.2, for the runner class ``CI_RUNNER_CLASS``.

    ``env`` is the job's environment as the runner sets it for the hook
    (``GITHUB_REPOSITORY``, ``GITHUB_REPOSITORY_ID``, ``GITHUB_EVENT_NAME``,
    ``GITHUB_ACTOR``, ``GITHUB_TRIGGERING_ACTOR``) plus the runner's ``CI_RUNNER_CLASS``;
    ``event`` is the parsed ``GITHUB_EVENT_PATH``. Pure.
    """
    def no(reason: str) -> Decision:
        return Decision(False, reason)

    cls = env.get('CI_RUNNER_CLASS')
    if cls not in CLASSES:
        return no('runner-class')
    name = _ascii_lower(env.get('GITHUB_REPOSITORY'), _REPO)
    if name == SELF:
        return no('self')
    repo = policy.repos.get(name) if name else None
    if repo is None:
        return no('repository')
    rid = env.get('GITHUB_REPOSITORY_ID')
    if not isinstance(rid, str) or not _DIGITS.match(rid) or int(rid) != repo.id:
        return no('repository-id')
    if not isinstance(event, dict):
        return no('payload')
    event_name = env.get('GITHUB_EVENT_NAME')
    if not isinstance(event_name, str) or not _EVENT.match(event_name):
        return no('event')
    if 'repository' in event:
        if not _repo_matches(event['repository'], repo):
            return no('repository-id')
    elif event_name != 'schedule':  # GitHub's schedule payload names no repository
        return no('payload')
    if repo.paused:
        return no('paused')
    if repo.visibility != 'private':  # public repositories stay on hosted runners (design §6.2)
        return no('repository')
    if cls in OPT_IN and cls not in repo.classes:  # an exclusive class, not listed for it (12.14)
        return no('runner-class')
    if event_name not in PRIVATE_EVENTS | repo.extra_events:
        return no('event')
    # not a fork: a pull request's head and base, and a workflow_run's head, are this repository
    if 'pull_request' in event or event_name.startswith('pull_request'):
        pr = event.get('pull_request')
        if not isinstance(pr, dict):
            return no('fork')
        for side in ('head', 'base'):
            s = pr.get(side)
            if not isinstance(s, dict) or not _repo_matches(s.get('repo'), repo):
                return no('fork')
    if event_name == 'workflow_run':
        wr = event.get('workflow_run')
        if not isinstance(wr, dict) or not _repo_matches(wr.get('head_repository'), repo):
            return no('fork')
    for login in _actors(env, event, event_name):  # None (absent or malformed) is on no list
        if login == policy.bot:  # on no allowlist: passes only where the repository's switch is on
            if not repo.bot_dispatch or event_name not in BOT_EVENTS:
                return no('bot')
        elif login not in policy.actors and not (repo.org_members and login in policy.members):
            return no('actor')
    return Decision(True, 'ok')


def actors_for(policy: Policy, repository: str) -> frozenset[str]:
    """The logins that may act in ``repository`` (as ``decide`` counts them, the bot
    aside): the hand-kept actors, plus the org's members where the snapshot opened the
    repository to them. ``claim`` counts these logins' statuses. Pure."""
    repo = policy.repos.get(_ascii_lower(repository, _REPO) or '')
    return policy.actors | policy.members if repo is not None and repo.org_members else policy.actors


def line(decision: Decision, policy_digest: str, snapshot_digest: str = '') -> str:
    """The one line the hook prints: ``ci-runners: accepted|refused <reason> policy=<hash>``,
    and `` org=<hash>`` when the org snapshot was merged."""
    verdict = 'accepted' if decision.accepted else 'refused'
    reason = decision.reason if decision.reason in REASONS else 'unknown'
    org = f' org={snapshot_digest[:12]}' if snapshot_digest else ''
    return f'ci-runners: {verdict} {reason} policy={policy_digest[:12] or "none"}{org}'
