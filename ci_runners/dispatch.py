"""GitHub adapter: original event admission, durable dispatch and reconciliation."""
from __future__ import annotations

import json
import time
import urllib.parse
from pathlib import Path

from ci_runners import central, client, control, github, policy


def load_policy(etc):
    pol = policy.load(Path(etc, 'policy.toml').read_bytes())
    if pol.org is not None:
        pol = policy.with_snapshot(pol, policy.load_snapshot(Path(etc, 'org.json').read_bytes()), time.time())
    return pol


class GitHub:
    def __init__(self, token_file: str, actor: str):
        if not isinstance(actor, str) or not policy._LOGIN.fullmatch(actor):
            raise central.Error('configure the dispatch service actor')
        self.token_file, self.actor = token_file, actor.lower()

    def api(self, method, path, body=None):
        # Tokens may be renewed in place while a nightly batch is running.
        with client.Client(tries=1) as http:
            return http.api(method, 'https://api.github.com'+path, control.secret(self.token_file), body)

    def source(self, requested: dict, pol: policy.Policy) -> dict:
        central.source_key(requested)
        repo = requested['repository'].lower()
        known = pol.repos.get(repo)
        if repo == policy.SELF or known is None or known.visibility != 'private':
            raise central.Error('source repository is not admitted to private CPU scheduling')
        run = self.api('GET', f'/repos/{repo}/actions/runs/{requested["run_id"]}/attempts/{requested["run_attempt"]}')
        if not isinstance(run, dict) or run.get('run_attempt') != requested['run_attempt']:
            raise central.Error('source run attempt does not exist')
        for field in ('repository', 'head_repository'):
            if not policy._repo_matches(run.get(field), known):
                raise central.Error('source run is not from this repository')
        event_name = run.get('event')
        if event_name not in ('pull_request', 'push', 'workflow_dispatch', 'schedule'):
            raise central.Error('source event is not supported by schema 2')
        actor = (run.get('actor') or {}).get('login')
        trigger = (run.get('triggering_actor') or {}).get('login')
        event = {'repository': run['repository'], 'sender': {'login': actor}}
        if event_name == 'pull_request':
            matches = [p for p in run.get('pull_requests', []) if p.get('head', {}).get('sha') == requested['sha']]
            if not matches:
                raise central.Error('test SHA is not the source pull request head')
            pr = self.api('GET', f'/repos/{repo}/pulls/{matches[0]["number"]}')
            event['pull_request'] = pr
            concurrency_key = f'pr:{pr["number"]}'
        else:
            if run.get('head_sha') != requested['sha']:
                raise central.Error('test SHA is not the source run head')
            concurrency_key = f'branch:{run.get("head_branch", "")}'
            if event_name == 'push':
                event['pusher'] = {'name': actor}
        env = {'GITHUB_REPOSITORY': repo, 'GITHUB_REPOSITORY_ID': str(known.id), 'GITHUB_EVENT_NAME': event_name,
               'GITHUB_ACTOR': actor, 'GITHUB_TRIGGERING_ACTOR': trigger,
               'CI_RUNNER_CLASS': 'plan', 'CI_RUNNER_AUDIENCE': 'private'}
        decision = policy.decide(pol, env, event)
        if not decision.accepted:
            raise central.Error(f'source admission refused: {decision.reason}')
        if run.get('conclusion') == 'cancelled':
            raise central.Error('source run was cancelled')
        # Keep the original policy facts for the execution hook, not the service actor.
        return {**requested, 'repository': repo, 'concurrency_key': concurrency_key,
                'admission': {'env': env, 'event': event}}

    def runs(self, repo, workflow):
        out = []
        for page in range(1, 11):
            obj = self.api('GET', f'/repos/{repo}/actions/workflows/{urllib.parse.quote(workflow, safe="")}/runs?event=workflow_dispatch&per_page=100&page={page}')
            rows = obj.get('workflow_runs') if isinstance(obj, dict) else None
            if not isinstance(rows, list):
                raise central.Error('GitHub returned no workflow runs')
            out.extend(rows)
            if len(rows) < 100:
                break
        return out

    def execution(self, allocation, run):
        s = allocation['source']
        if (not isinstance(run, dict) or run.get('event') != 'workflow_dispatch'
                or run.get('display_title') != f'ci-execute/{allocation["id"]}'
                or str((run.get('actor') or {}).get('login', '')).lower() != self.actor
                or str((run.get('repository') or {}).get('full_name', '')).lower() != s['repository'].lower()
                or run.get('path', '').split('@')[0] != '.github/workflows/'+s['execution_workflow']):
            raise central.Error('execution run does not match the trusted dispatch')
        return run

    def verify_claim(self, allocation, run_id, attempt):
        repo = allocation['source']['repository']
        run = self.api('GET', f'/repos/{repo}/actions/runs/{run_id}/attempts/{attempt}')
        self.execution(allocation, run)
        if run.get('run_attempt') != attempt or run.get('status') == 'completed':
            raise central.Error('execution attempt is no longer active')


class Dispatcher:
    def __init__(self, store, api: GitHub, *, clock=time.time):
        self.store, self.api, self.clock = store, api, clock
        self.last_write = 0.

    def send_one(self):
        # All API mutations pass this single writer, at most one per second.
        if self.clock() - self.last_write < 1:
            return False
        msg = self.store.next_message()
        if msg is None:
            return False
        self.last_write = self.clock()
        try:
            payload = msg['payload']
            if msg['kind'] == 'source_status':
                s = payload['source']
                existing = next((b for b in self.store.batches() if b['id'] == central.source_key(s)), None)
                current = next(n for n in self.store.notices() if n['id'] == central.source_key(s))
                if existing is None and self.store.is_latest_status({'source': s}):
                    self.api.api('POST', f'/repos/{s["repository"]}/statuses/{s["sha"]}', {
                        'context': s['context'], 'state': current['state'], 'description': github.status_text(current['reason']),
                        'target_url': f'https://github.com/{s["repository"]}/actions/runs/{s["run_id"]}'})
            elif msg['kind'] == 'status':
                b = self.store.batch(payload['batch_id'])
                if self.store.is_latest_status(b):
                    state = payload['state']
                    # A delayed pending message must not overwrite a terminal result.
                    if state == 'pending' and b['state'] != 'pending':
                        state = b['state']
                    self.api.api('POST', f'/repos/{b["source"]["repository"]}/statuses/{b["source"]["sha"]}', {
                        'context': b['source']['context'], 'state': 'error' if state == 'cancelled' else state,
                        'description': github.status_text(f'CPU queue: {state}; {len(b["tasks"])} tasks'),
                        'target_url': f'https://github.com/{b["source"]["repository"]}/actions/runs/{b["source"]["run_id"]}'})
            elif msg['kind'] == 'dispatch':
                a = self.store.allocation(payload['allocation_id'])
                if a['run_id'] is not None or a['runner_done']:
                    pass
                elif (a['cancelled'] or self.clock() >= a['deadline']) and msg['attempts'] == 1:
                    self.store.dispatch_aborted(a['id'])
                else:
                    s = a['source']
                    matches = []
                    if msg['attempts'] > 1:
                        matches = [r for r in self.api.runs(s['repository'], s['execution_workflow'])
                                   if r.get('display_title') == f'ci-execute/{a["id"]}']
                    if matches:
                        run = self.api.execution(a, min(matches, key=lambda r: r['id']))
                        self.store.bind_run(a['id'], run['id'], run.get('run_attempt', 1))
                    elif msg['attempts'] > 1:
                        # The previous request may have reached GitHub. Keep its reservation.
                        raise central.Error('dispatch result unknown; awaiting GitHub reconciliation')
                    else:
                        reply = self.api.api('POST', f'/repos/{s["repository"]}/actions/workflows/{urllib.parse.quote(s["execution_workflow"], safe="")}/dispatches',
                                     {'ref': s['execution_ref'], 'return_run_details': True,
                                      'inputs': {'runner_tag': a['runner'], 'allocation_json': central.dumps(a['grant'])}})
                        if isinstance(reply, dict) and central.positive(reply.get('workflow_run_id')):
                            self.store.bind_run(a['id'], reply['workflow_run_id'], 1)
            elif msg['kind'] == 'cancel':
                a = self.store.allocation(payload['allocation_id'])
                if a['run_id'] is None and not a['runner_done']:
                    raise central.Error('awaiting execution run identity before cancellation')
                if a['run_id'] is not None and not a['runner_done']:
                    self.api.api('POST', f'/repos/{a["source"]["repository"]}/actions/runs/{a["run_id"]}/cancel')
            else:
                raise central.Error('unknown outbox message')
        except (OSError, ValueError, github.ApiError) as e:
            self.store.message_retry(msg['id'], type(e).__name__, min(300, 2 ** min(msg['attempts'], 8)))
        else:
            self.store.message_done(msg['id'])
        return True

    def reconcile(self):
        listings = {}
        for a in self.store.allocations():
            s = a['source']
            key = (s['repository'], s['execution_workflow'])
            if key not in listings:
                listings[key] = self.api.runs(*key)
            matches = [r for r in listings[key] if r.get('display_title') == f'ci-execute/{a["id"]}']
            if not matches and a['run_id'] is not None:
                matches = [self.api.api('GET', f'/repos/{s["repository"]}/actions/runs/{a["run_id"]}')]
            if not matches:
                continue
            run = self.api.execution(a, next((r for r in matches if r['id'] == a['run_id']), min(matches, key=lambda r: r['id'])))
            self.store.bind_run(a['id'], run['id'], run.get('run_attempt', 1))
            if run.get('status') == 'completed':
                conclusion = run.get('conclusion') or 'error'
                artifacts = []
                if conclusion == 'success':
                    obj = self.api.api('GET', f'/repos/{s["repository"]}/actions/runs/{run["id"]}/artifacts?per_page=100')
                    artifacts = [r for r in obj.get('artifacts', []) if r.get('name') == f'ci-allocation-{a["id"]}' and r.get('expired') is False]
                    if not artifacts:
                        conclusion = 'error'
                self.store.runner_finished(a['id'], run['id'], run.get('run_attempt', 1), conclusion=conclusion,
                                           artifacts=[{'id': r['id'], 'name': r['name']} for r in artifacts])
            elif a['cancelled'] or (a['state'] == 'reserved' and self.clock() >= a['deadline']):
                self.store.expire(a['id'])
        for b in self.store.batches(pending=True):
            s = b['source']
            run = self.api.api('GET', f'/repos/{s["repository"]}/actions/runs/{s["run_id"]}/attempts/{s["run_attempt"]}')
            if run.get('conclusion') == 'cancelled':
                self.store.cancel(b['id'])
        for notice in self.store.notices(pending=True):
            s = notice['source']
            run = self.api.api('GET', f'/repos/{s["repository"]}/actions/runs/{s["run_id"]}/attempts/{s["run_attempt"]}')
            if run.get('status') == 'completed':
                self.store.notice(s, 'error', 'Source workflow ended before submitting a CPU batch')
