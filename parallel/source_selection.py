"""Explicit same-repository dispatch selection, separate from ordinary run heads."""
from __future__ import annotations

import json

from ci_runners import policy
from parallel import protocol


KINDS = frozenset({'pull-request', 'commit-replay'})


def selector(source):
    value = source.get('test_source', {'kind': 'run-head'})
    if not isinstance(value, dict) or not isinstance(value.get('kind'), str):
        raise protocol.Error('test_source must be a typed selector')
    kind = value['kind']
    if kind not in KINDS | {'run-head'} or set(value) != ({'kind', 'number'} if kind == 'pull-request' else {'kind'}):
        raise protocol.Error('unknown test_source kind or fields')
    if kind == 'pull-request' and not protocol.positive(value['number']):
        raise protocol.Error('test_source PR number must be a positive integer')
    if 'range_base' in source and (kind != 'commit-replay' or not isinstance(source['range_base'], str)
                                    or not protocol.SHA.fullmatch(source['range_base'])):
        raise protocol.Error('range_base requires commit-replay and a full SHA')
    return dict(value)


def read_policy(path):
    """Feature opt-in is private, repository-ID-bound, and grants no new admission."""
    if path is None:
        return {}
    from parallel import deployment_files
    try:
        doc = json.loads(deployment_files.read_private(path))
    except (OSError, ValueError) as error:
        raise protocol.Error('cannot read source-selector policy') from error
    if (not isinstance(doc, dict) or set(doc) != {'schema', 'repositories'}
            or type(doc['schema']) is not int or doc['schema'] != 1
            or not isinstance(doc['repositories'], list) or len(doc['repositories']) > 10000):
        raise protocol.Error('invalid source-selector policy')
    records, names = {}, set()
    for item in doc['repositories']:
        if (not isinstance(item, dict) or set(item) != {'id', 'repository', 'kinds'}
                or not protocol.positive(item['id']) or not isinstance(item['repository'], str)
                or not protocol.REPO.fullmatch(item['repository'])
                or not isinstance(item['kinds'], list) or not item['kinds']
                or any(not isinstance(k, str) or k not in KINDS for k in item['kinds'])
                or len(set(item['kinds'])) != len(item['kinds'])):
            raise protocol.Error('invalid source-selector repository binding')
        name = item['repository'].lower()
        if item['id'] in records or name in names:
            raise protocol.Error('duplicate source-selector repository binding')
        records[item['id']] = {**item, 'repository': name}
        names.add(name)
    return records


def resolve(api, known, run, requested, enabled):
    """Return an authorized override, or None to retain the exact old path."""
    choice = selector(requested)
    if choice['kind'] == 'run-head':
        return None
    allowed = enabled.get(known.id, {})
    if (allowed.get('repository') != known.name.lower() or choice['kind'] not in allowed.get('kinds', ())):
        raise protocol.Error('source selector is not enabled for this repository')
    if run.get('event') != 'workflow_dispatch':
        raise protocol.Error('source override requires workflow_dispatch')
    head = run.get('head_sha')
    if not isinstance(head, str) or not protocol.SHA.fullmatch(head):
        raise protocol.Error('source run has no authoritative full head SHA')
    actors = {}
    for field in ('actor', 'triggering_actor'):
        actor = policy._login(run.get(field), 'login')
        if not actor or actor.lower().endswith('[bot]'):
            raise protocol.Error('source override requires human write-authorized actors')
        actors[actor.lower()] = actor
    permissions = []
    for login in sorted(actors):
        access = api('GET', f'/repos/{known.name}/collaborators/{actors[login]}/permission')
        user = access.get('user') if isinstance(access, dict) else None
        verified = policy._login(user, 'login')
        if (not isinstance(user, dict) or user.get('type') != 'User' or not verified
                or verified.lower() != login or access.get('permission') not in ('write', 'admin')):
            raise protocol.Error('source override actor lacks human write/admin permission')
        permissions.append({'login': verified, 'permission': access['permission']})
    sha = requested['sha']
    facts = dict(kind=choice['kind'], run_head_sha=head, actors=permissions)
    if choice['kind'] == 'pull-request':
        pr = api('GET', f'/repos/{known.name}/pulls/{choice["number"]}')
        if (not isinstance(pr, dict) or type(pr.get('number')) is not int or pr['number'] != choice['number']
                or any(not policy._repo_matches(pr.get(side, {}).get('repo'), known) for side in ('base', 'head'))
                or pr.get('head', {}).get('sha') != sha):
            raise protocol.Error('selected PR is not the exact same-repository head')
        facts.update(number=pr['number'], sha=sha)
        return facts, f'pr:{pr["number"]}'
    commit = api('GET', f'/repos/{known.name}/git/commits/{sha}')
    if not isinstance(commit, dict) or commit.get('sha') != sha:
        raise protocol.Error('replay commit is not resolved in the source repository')
    if 'range_base' in requested:
        base = requested['range_base']
        compared = api('GET', f'/repos/{known.name}/compare/{base}...{sha}')
        if (not isinstance(compared, dict) or compared.get('status') not in ('ahead', 'identical')
                or compared.get('merge_base_commit', {}).get('sha') != base):
            raise protocol.Error('replay base is not an ancestor of the tested commit')
        facts['range_base'] = base
    facts['sha'] = sha
    return facts, f'replay:{sha}:{requested.get("range_base", "")}'
