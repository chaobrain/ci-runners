"""The pre-job hook's decision: may this job run on this runner (design §6.2, §6.3)?

``decide`` is a pure function of the policy, the job's environment and its event
payload. It reads only what it is given, prints nothing and returns a fixed reason
code, so that no payload text and no inventory reaches a public job log (T9, T10).

Every check fails closed: a missing, mistyped or non-ASCII field refuses the job.
"""
from __future__ import annotations

import dataclasses
import hashlib
import re
import tomllib
from collections.abc import Mapping

#: this repository: its jobs are refused on every runner (AGENTS.md, design §6.2)
SELF = 'chaobrain/ci-runners'

#: runner classes (``CI_RUNNER_CLASS``, design §5.1)
CLASSES = frozenset({'light', 'part', 'compile', 'nightly', 'gpu', 'h100'})
#: the classes a public repository may use (lead decision 2)
PUBLIC_CLASSES = frozenset({'gpu', 'h100'})

#: events accepted per repository class (design §6.2)
PRIVATE_EVENTS = frozenset({'pull_request', 'push', 'workflow_dispatch', 'schedule', 'workflow_run', 'status',
                            'repository_dispatch'})
PUBLIC_EVENTS = frozenset({'pull_request', 'push', 'workflow_dispatch'})
#: events the workflow bot may trigger, in a private repository only
BOT_EVENTS = frozenset({'workflow_dispatch', 'repository_dispatch'})

#: the reason codes ``decide`` returns; the hook prints one of them and nothing else
REASONS = ('ok', 'runner-class', 'self', 'repository', 'repository-id', 'payload', 'paused', 'event', 'fork',
           'actor', 'bot')

_REPO = re.compile(r'[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}\Z')
_LOGIN = re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})(?:\[bot\])?\Z')
_EVENT = re.compile(r'[a-z_]{1,50}\Z')
_DIGITS = re.compile(r'[0-9]{1,20}\Z')


class PolicyError(ValueError):
    """The policy file is malformed; the hook then refuses every job."""


@dataclasses.dataclass(frozen=True)
class Repo:
    name: str  # ``owner/name``, lower case
    id: int
    visibility: str  # ``private`` or ``public``
    paused: bool = False
    extra_events: frozenset[str] = frozenset()  # private only: events beyond PRIVATE_EVENTS


@dataclasses.dataclass(frozen=True)
class Policy:
    actors: frozenset[str]  # allowlisted logins, lower case
    repos: Mapping[str, Repo]  # by lower-case ``owner/name``
    bot: str = 'github-actions[bot]'
    digest: str = ''  # sha256 of the policy file, printed (shortened) with every decision


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
    """Parse ``policy.toml`` (schema 1). Raises PolicyError on anything unexpected."""
    raw = data.encode() if isinstance(data, str) else data
    try:
        doc = tomllib.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise PolicyError(f'policy: not TOML: {e}') from None
    if doc.get('schema') != 1:
        raise PolicyError('policy: schema must be 1')
    unknown = set(doc) - {'schema', 'bot', 'actors', 'repositories'}
    if unknown:
        raise PolicyError(f'policy: unknown keys {sorted(unknown)}')
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
        unknown = set(r) - {'id', 'visibility', 'paused', 'extra_events'}
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
        if not isinstance(extra, list) or not all(isinstance(e, str) and _EVENT.match(e) for e in extra):
            raise PolicyError(f'policy: {name}: extra_events must be a list of event names')
        if extra and vis == 'public':
            raise PolicyError(f'policy: {name}: a public repository takes no extra events')
        if name in repos:
            raise PolicyError(f'policy: {name} is listed twice')
        repos[name] = Repo(name, rid, vis, paused, frozenset(extra))
    return Policy(frozenset(actors), repos, bot, hashlib.sha256(raw).hexdigest())


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
    public = repo.visibility == 'public'
    if public and cls not in PUBLIC_CLASSES:
        return no('runner-class')
    allowed = PUBLIC_EVENTS if public else PRIVATE_EVENTS | repo.extra_events
    if event_name not in allowed:
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
        if login == policy.bot:
            if public or event_name not in BOT_EVENTS:
                return no('bot')
        elif login not in policy.actors:
            return no('actor')
    return Decision(True, 'ok')


def line(decision: Decision, policy_digest: str) -> str:
    """The one line the hook prints: ``ci-runners: accepted|refused <reason> policy=<hash>``."""
    verdict = 'accepted' if decision.accepted else 'refused'
    reason = decision.reason if decision.reason in REASONS else 'unknown'
    return f'ci-runners: {verdict} {reason} policy={policy_digest[:12] or "none"}'
