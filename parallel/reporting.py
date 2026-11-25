"""Read-side publication gates over immutable native and compatibility manifests."""
from __future__ import annotations

import fnmatch
import json
from pathlib import Path
import re
import tempfile

from ci_runners import storage
from parallel import deployment_files, protocol


SOURCE_FIELDS = ('repository', 'repository_id', 'sha', 'run_id', 'run_attempt', 'context')


def same(a, b):
    return protocol.dumps(a) == protocol.dumps(b)


def requirements(rules):
    if not isinstance(rules, list) or len(rules) > 32:
        raise protocol.Error('required_files must be a bounded list')
    for rule in rules:
        if (not isinstance(rule, dict) or set(rule) != {'pattern', 'min_bytes'}
                or not isinstance(rule['pattern'], str) or not 0 < len(rule['pattern']) <= 200
                or not re.fullmatch(r'[A-Za-z0-9_./*?\[\]-]+', rule['pattern'])
                or rule['pattern'].startswith('/') or any(p in ('', '.', '..') for p in rule['pattern'].split('/'))
                or not protocol.positive(rule['min_bytes']) or rule['min_bytes'] > 2**31):
            raise protocol.Error('invalid required report file rule')
    return rules


def read_policy(path):
    if path is None:
        return {}
    try:
        doc = json.loads(deployment_files.read_private(path))
    except (OSError, ValueError) as error:
        raise protocol.Error('cannot read report-gate policy') from error
    if (not isinstance(doc, dict) or set(doc) != {'schema', 'repositories'}
            or type(doc['schema']) is not int or doc['schema'] != 1
            or not isinstance(doc['repositories'], list) or len(doc['repositories']) > 10000):
        raise protocol.Error('invalid report-gate policy')
    records, names = {}, set()
    for p in doc['repositories']:
        if (not isinstance(p, dict) or set(p) != {'id', 'repository', 'contexts', 'namespace', 'plan_name'}
                or not protocol.positive(p['id']) or not isinstance(p['repository'], str)
                or not protocol.REPO.fullmatch(p['repository'])
                or not isinstance(p['contexts'], list) or not p['contexts'] or len(p['contexts']) > 32
                or any(not isinstance(c, str) or not re.fullmatch(r'[A-Za-z0-9._/-]{1,100}', c) for c in p['contexts'])
                or not isinstance(p['namespace'], str) or not re.fullmatch(r'[A-Za-z0-9._/-]{1,100}', p['namespace'])
                or not isinstance(p['plan_name'], str) or not storage.ID.fullmatch(p['plan_name'])
                or p['id'] in records or p['repository'].lower() in names):
            raise protocol.Error('invalid report-gate repository/context binding')
        records[p['id']] = {**p, 'repository': p['repository'].lower()}
        names.add(p['repository'].lower())
    return records


def bind(source, policy):
    requested = source.get('report_gate', False)
    if type(requested) is not bool:
        raise protocol.Error('report_gate must be a boolean')
    source = {k: v for k, v in source.items() if k not in ('report_namespace', 'report_plan_name')}
    p = policy.get(source.get('repository_id'), {})
    context = source['context']
    if context not in p.get('contexts', ()) and context.endswith('/plan-only'):
        context = context[:-len('/plan-only')]
    matching = (p.get('repository') == source['repository'].lower()
                and context in p.get('contexts', ()))
    if matching and not requested:
        raise protocol.Error('this context requires report-gated publication')
    if requested and not matching:
        raise protocol.Error('report gate is not enabled for this repository/context')
    if requested:
        source.update(report_namespace=p['namespace'], report_plan_name=p['plan_name'])
    return source


def job_name(batch_id):
    return 'verify-reports/' + batch_id


def project_identity(source, name):
    return storage.identity(dict(repository=source['repository'], repository_id=source['repository_id'],
                                 sha=source.get('run_head_sha', source['sha']), run_id=source['run_id'], run_attempt=source['run_attempt'],
                                 context=source['report_namespace'], kind='project', part_id='files', part_attempt=1,
                                 bundle_name=name))


def receipt_identity(batch):
    return project_identity(batch['source'], 'central-report-' + batch['id'])


def manifest(client, ref, expected):
    storage.reference(ref, expected, client.store)
    got = client.describe(expected)
    doc = got.get('manifest')
    if (not same(got.get('reference'), ref) or not isinstance(doc, dict)
            or type(doc.get('schema')) is not int or doc['schema'] != 1
            or storage.digest(storage.dumps(doc)) != ref['manifest_sha256']
            or not same(doc.get('identity'), expected) or doc.get('state') != 'complete'
            or not isinstance(doc.get('members'), list) or len(doc['members']) > storage.DEFAULTS['entries']):
        raise storage.Error('identity')
    members = {}
    for entry in doc['members']:
        if (not isinstance(entry, dict) or set(entry) != {'name', 'size', 'sha256'}
                or type(entry['size']) is not int or entry['size'] < 0
                or not isinstance(entry['sha256'], str) or not storage.HEX.fullmatch(entry['sha256'])):
            raise storage.Error('corrupt')
        name = storage.member_name(entry['name'])
        if name in members:
            raise storage.Error('corrupt')
        members[name] = (entry['sha256'], entry['size'])
    return members


def encoded(value):
    data = json.dumps(value, sort_keys=True).encode()
    return storage.digest(data), len(data)


def project_members(client, source, ref, name):
    members = manifest(client, ref, project_identity(source, name))
    expected = dict(workflow_sha=source.get('run_head_sha', source['sha']), producer_checkout_sha=source['sha'],
                    run_id=source['run_id'], run_attempt=source['run_attempt'], repository_id=source['repository_id'],
                    repository=source['repository'])
    if members.pop('.ci-storage-source.json', None) != encoded(expected):
        raise storage.Error('identity')
    return members


def require_members(members, rules):
    for rule in requirements(rules):
        if not any(fnmatch.fnmatchcase(name, rule['pattern']) and value[1] >= rule['min_bytes']
                   for name, value in members.items()):
            raise storage.Error('missing')


def verify_receipt(etc, batch, reference, *, client=None):
    """Verify payload once, then compare pinned manifests without re-copying large reports."""
    from parallel import bundles
    source = batch['source']
    bound = bundles.backend(source)
    if source.get('report_gate') is not True or bound['backend'] != 'ssh' or batch['state'] not in protocol.TERMINAL:
        raise storage.Error('identity')
    client = client or storage.client(etc, bound)
    ident = receipt_identity(batch)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)/'receipt'
        client.read_bundle(reference, ident, root)
        path = root/'receipt.json'
        if not path.is_file() or path.stat().st_size > (2 << 20):
            raise storage.Error('limit')
        doc = json.loads(path.read_bytes())
    fields = {'schema', 'batch_id', 'source', 'plan_reference', 'plan_projection', 'allocations', 'selection_verified'}
    if (not isinstance(doc, dict) or set(doc) != fields or type(doc['schema']) is not int or doc['schema'] != 1
            or doc['batch_id'] != batch['id'] or doc['selection_verified'] is not True
            or not same(doc['source'], {key: source[key] for key in SOURCE_FIELDS})
            or not same(doc['plan_reference'], source.get('files_bundle'))
            or not isinstance(doc['allocations'], dict) or len(doc['allocations']) > 512
            or set(doc['allocations']) != {a['id'] for a in batch['allocations']}):
        raise storage.Error('identity')
    original = manifest(client, source['files_bundle'], bundles.plan_identity(source))
    projected = project_members(client, source, doc['plan_projection'], source['report_plan_name'])
    if {k: v for k, v in original.items() if k != '.ci-storage-source.json'} != projected:
        raise storage.Error('corrupt')
    tasks = {t['id']: t for t in batch['tasks']}
    latest = {task: max((a['task_attempt'] for a in batch['allocations'] if a['task'] == task), default=0) for task in tasks}
    if any(latest[t['id']] != t['attempts'] or batch['state'] == 'success' and t['state'] != 'success'
           for t in tasks.values()):
        raise storage.Error('identity')
    for a in batch['allocations']:
        p = doc['allocations'][a['id']]
        if (not isinstance(p, dict) or set(p) != {'native_reference', 'outcome', 'artifacts', 'seal'}
                or not same(p['native_reference'], a['storage_reference']) or not isinstance(p['artifacts'], dict)
                or a['released'] != 1 or a['runner_done'] != 1 or a['state'] != 'released'):
            raise storage.Error('identity')
        part = tasks[a['task']]['part']
        final = a['task_attempt'] == latest[a['task']]
        outcome = a['result'].get('outcome', {})
        red = outcome.get('outcome') == 'red' if isinstance(outcome, dict) else outcome == 'red'
        if (red or a['result'].get('state') == 'failure') and batch['state'] == 'success':
            raise storage.Error('identity')
        if a['storage_reference'] is None:
            if final or red or p['outcome'] is not None or p['seal'] is not None or p['artifacts']:
                raise storage.Error('missing')
            continue
        allocation = {**a, 'source': source, 'part': part, 'batch_id': batch['id']}
        native = manifest(client, a['storage_reference'], bundles.result_identity(allocation, a['job_id']))
        pid, attempt = part['id'], a['task_attempt']
        if any(name not in native for name in ('outcome.json', 'summary.md')):
            raise storage.Error('missing')
        copied = project_members(client, source, p['outcome'], f'ci-part-{pid}-{attempt}')
        if copied != {name: native[name] for name in ('outcome.json', 'summary.md')}:
            raise storage.Error('corrupt')
        if final:
            require_members(native, part.get('required_files', []))
        declared = part.get('artifacts', {})
        if set(p['artifacts']) - set(declared):
            raise storage.Error('identity')
        expected_refs, missing = {}, []
        for name in sorted(declared):
            prefix = f'artifacts/{name}/'
            subtree = {k[len(prefix):]: v for k, v in native.items() if k.startswith(prefix)}
            ref = p['artifacts'].get(name)
            if ref is not None:
                artifact_name = f'{name}-{pid}-{attempt}'
                if project_members(client, source, ref, artifact_name) != subtree:
                    raise storage.Error('corrupt')
                if name != 'impact':
                    expected_refs[artifact_name] = ref
            elif name != 'impact':
                missing.append(f'{name}-{pid}-{attempt}')
            if final and name != 'impact' and (ref is None or not subtree or
                    name == 'rows' and subtree.get('local_ci.md', ('', 0))[1] <= 0):
                raise storage.Error('missing')
        state = 'success' if not missing and a['result'].get('state') == 'success' and a['conclusion'] == 'success' else 'failure'
        seal = project_members(client, source, p['seal'], f'seal-{pid}-{attempt}')
        if seal != {'receipt.json': encoded(dict(part=pid, attempt=attempt, job_state=state,
                                                 missing=missing, references=expected_refs))}:
            raise storage.Error('corrupt')
    return reference
