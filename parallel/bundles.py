"""Bind internal files to the existing authorized source and allocation records."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from ci_runners import storage
from parallel import protocol

GITHUB = {'schema': 1, 'backend': 'github'}


def backend(source):
    return storage.binding(source.get('storage', GITHUB))


def repository_id(source):
    verified = source.get('admission', {}).get('event', {}).get('repository', {}).get('id')
    value = verified if verified is not None else source.get('repository_id')
    if type(value) is not int or value <= 0:
        raise storage.Error('identity')
    if 'repository_id' in source and source['repository_id'] != value:
        raise storage.Error('identity')
    return value


def plan_identity(source):
    return storage.identity(dict(repository_id=repository_id(source), repository=source['repository'].lower(),
                                 run_id=source['run_id'], run_attempt=source['run_attempt'], sha=source['sha'],
                                 context=source['context'], kind='plan', part_id='plan', part_attempt=1,
                                 bundle_name='plan-' + protocol.source_key(source)))


def result_identity(allocation, job_id=None):
    source = allocation['source']
    return storage.identity(dict(repository_id=repository_id(source), repository=source['repository'].lower(),
                                 run_id=source['run_id'], run_attempt=source['run_attempt'], sha=source['sha'],
                                 context=source['context'], kind='allocation', part_id=allocation['part']['id'],
                                 part_attempt=allocation['task_attempt'], bundle_name='allocation-' + allocation['id'],
                                 batch_id=allocation.get('batch_id') or protocol.source_key(source),
                                 task_id=allocation['grant']['task_id'], task_attempt=allocation['task_attempt'],
                                 allocation_id=allocation['id'], execution_job_id=job_id or allocation['job_id']))


def plan_limits(client):
    from ci_runners import flow
    client.limits = {**client.limits, 'entries': min(client.limits['entries'], flow.MAX_FILES),
                     'unpacked_bytes': min(client.limits['unpacked_bytes'], flow.MAX_FILES_BYTES),
                     'compressed_bytes': min(client.limits['compressed_bytes'], flow.MAX_FILES_ZIP)}
    return client


def verify_plan(etc, source, ref):
    ident = plan_identity(source)
    bound = backend(source)
    storage.reference(ref, ident, bound['store'])
    client = plan_limits(storage.client(etc, bound))
    with tempfile.TemporaryDirectory() as directory:
        destination = Path(directory)/'plan'
        client.read_bundle(ref, ident, destination)
        from ci_runners import flow
        why = flow.check_files(directory, 'plan')
        if why:
            raise storage.Error('corrupt')


def verify_result(etc, allocation, job_id):
    ident = result_identity(allocation, job_id)
    bound = backend(allocation['source'])
    client = storage.client(etc, bound)
    ref = client.describe(ident)['reference']
    storage.reference(ref, ident, bound['store'])
    with tempfile.TemporaryDirectory() as directory:
        destination = Path(directory)/'result'
        client.read_bundle(ref, ident, destination)
        for name in ('outcome.json', 'summary.md', 'allocation-result.json'):
            if not (destination/name).is_file():
                raise storage.Error('missing')
        metadata = destination/'allocation-result.json'
        if metadata.stat().st_size > 1 << 20:
            raise storage.Error('limit')
        try:
            doc = json.loads(metadata.read_bytes())
        except (ValueError, OSError) as exc:
            raise storage.Error('corrupt') from exc
        if not isinstance(doc, dict):
            raise storage.Error('corrupt')
        if (doc.get('allocation') != allocation['grant'] or doc.get('result') != allocation['result']
                or doc.get('source_repository') != ident['repository'] or doc.get('source_sha') != ident['sha']
                or doc.get('source_run_id') != ident['run_id'] or doc.get('source_run_attempt') != ident['run_attempt']
                or doc.get('execution_job_id') != job_id or doc.get('task_attempt') != ident['task_attempt']):
            raise storage.Error('identity')
        provenance = {k: allocation['source'][k] for k in ('run_head_sha', 'source_selection')
                      if k in allocation['source']}
        if provenance and protocol.dumps(doc.get('source_provenance')) != protocol.dumps(provenance):
            raise storage.Error('identity')
        if allocation['result'].get('state') == 'success':
            from parallel import reporting
            reporting.require_members({p.relative_to(destination).as_posix(): ('', p.stat().st_size)
                                       for p in destination.rglob('*') if p.is_file()}, allocation['part'].get('required_files', []))
    return ref
