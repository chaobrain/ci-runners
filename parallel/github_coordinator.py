"""Source admission, job reconciliation and durable commit-status publication."""
from __future__ import annotations

import dataclasses
import time
from pathlib import Path

from ci_runners import client, github, policy, storage
from parallel import bundles, protocol, transport


_SOURCE_EVENTS = frozenset({'pull_request', 'push', 'workflow_dispatch', 'schedule'})
_REPOSITORY_PERMISSIONS = frozenset({'read', 'write', 'admin'})


def _organization_record(organization, item):
    """Validate an authoritative repository identity and its visibility."""
    if not isinstance(item, dict):
        raise protocol.Error('GitHub returned an invalid organization repository')
    name = policy._ascii_lower(item.get('full_name'), policy._REPO)
    owner = item.get('owner')
    visibility = item.get('visibility')
    if (name is None or name.split('/')[0] != organization or not isinstance(owner, dict)
            or policy._ascii_lower(owner.get('login'), policy._ORG) != organization
            or owner.get('type') != 'Organization' or not protocol.positive(item.get('id'))
            or visibility not in ('private', 'public', 'internal')):
        raise protocol.Error('GitHub returned an invalid organization repository')
    return {'id': item['id'], 'full_name': item['full_name'], 'visibility': visibility}


def load_policy(etc):
    pol = policy.load(Path(etc, 'policy.toml').read_bytes())
    if pol.org is not None:
        pol = policy.with_snapshot(pol, policy.load_snapshot(Path(etc, 'org.json').read_bytes()), time.time())
    return pol


class GitHub:
    def __init__(self, token_file: str):
        self.token_file = token_file

    def api(self, method, path, body=None):
        # Tokens may be renewed in place while a nightly batch is running.
        with client.Client(tries=1) as http:
            return http.api(method, 'https://api.github.com'+path, transport.secret(self.token_file), body)

    def repositories(self, organization):
        """Read the complete visible repository catalog for inspection."""
        organization = policy._ascii_lower(organization, policy._ORG)
        if organization is None:
            raise protocol.Error('invalid GitHub organization')
        records, names, ids = [], set(), set()
        for page in range(1, 101):
            rows = self.api('GET', f'/orgs/{organization}/repos?type=all&per_page=100&page={page}')
            if not isinstance(rows, list) or len(rows) > 100:
                raise protocol.Error('GitHub returned an invalid organization repository page')
            for item in rows:
                record = _organization_record(organization, item)
                name, rid = record['full_name'].lower(), record['id']
                if name in names or rid in ids:
                    raise protocol.Error('GitHub returned a duplicate organization repository')
                names.add(name)
                ids.add(rid)
                records.append(record)
            if len(rows) < 100:
                return sorted(records, key=lambda record: record['full_name'].lower())
        raise protocol.Error('GitHub organization repository listing exceeds the limit')

    def _private_organization_repository(self, full_name, organization):
        name = policy._ascii_lower(full_name, policy._REPO)
        organization = policy._ascii_lower(organization, policy._ORG)
        if not name or name == policy.SELF or name.split('/')[0] != organization:
            raise protocol.Error('source repository is not in the configured organization')
        metadata = self.api('GET', f'/repos/{name}')
        repo = _organization_record(organization, metadata)
        if (repo['full_name'].lower() != name or repo['visibility'] != 'private'
                or metadata.get('private') is not True):
            raise protocol.Error('source repository is not admitted to private test scheduling')
        return policy.Repo(name, repo['id'], 'private')

    def admit(self, pol, env, event, *, repository_members=False, organization=None,
              organization_slurm_repository_id=None) -> policy.Decision:
        """Verify current repository ownership, or apply the deployment's actor policy."""
        if organization is not None:
            name = policy._ascii_lower(env.get('GITHUB_REPOSITORY'), policy._REPO)
            if name == policy.SELF:
                return policy.Decision(False, 'self')
            event_name = env.get('GITHUB_EVENT_NAME')
            if not isinstance(event_name, str) or event_name not in _SOURCE_EVENTS:
                return policy.Decision(False, 'event')
            try:
                repo = self._private_organization_repository(name, organization)
            except protocol.Error:
                return policy.Decision(False, 'repository')
            except (github.ApiError, OSError, ValueError):
                return policy.Decision(False, 'policy')
            if protocol.positive(organization_slurm_repository_id) and organization_slurm_repository_id == repo.id:
                repo = dataclasses.replace(repo, classes=frozenset({'slurm'}))
            return policy.check_context(policy.Policy(frozenset(), {repo.name: repo}), env, event)
        if not repository_members:
            return policy.decide(pol, env, event)
        decision = policy.check_context(pol, env, event)
        if not decision.accepted:
            return decision
        repo = pol.repos[env['GITHUB_REPOSITORY'].lower()]
        if repo.visibility != 'private':
            return policy.Decision(False, 'repository')
        event_name = env['GITHUB_EVENT_NAME']
        if event_name not in _SOURCE_EVENTS:
            return policy.Decision(False, 'event')
        actors = set(policy._actors(env, event, event_name))
        if None in actors:
            return policy.Decision(False, 'actor')
        if any(login.endswith('[bot]') for login in actors):
            return policy.Decision(False, 'bot')
        try:
            metadata = self.api('GET', f'/repos/{repo.name}')
            if not policy._repo_matches(metadata, repo) or metadata.get('private') is not True:
                return policy.Decision(False, 'repository')
            for login in sorted(actors):
                access = self.api('GET', f'/repos/{repo.name}/collaborators/{login}/permission')
                if not isinstance(access, dict):
                    return policy.Decision(False, 'actor')
                permission = access.get('permission')
                if (not isinstance(permission, str) or permission not in _REPOSITORY_PERMISSIONS
                        or policy._login(access.get('user'), 'login') != login):
                    return policy.Decision(False, 'actor')
        except github.ApiError as error:
            return policy.Decision(False, 'actor' if error.cause == 'HTTP 404' else 'policy')
        except (OSError, ValueError):
            return policy.Decision(False, 'policy')
        return decision

    def source(self, requested: dict, pol: policy.Policy, *, repository_members=False, organization=None) -> dict:
        protocol.source_key(requested)
        repo = requested['repository'].lower()
        if organization is not None:
            known = self._private_organization_repository(repo, organization)
        else:
            known = pol.repos.get(repo)
            if repo == policy.SELF or known is None or known.visibility != 'private':
                raise protocol.Error('source repository is not admitted to private test scheduling')
        run = self.api('GET', f'/repos/{repo}/actions/runs/{requested["run_id"]}/attempts/{requested["run_attempt"]}')
        if not isinstance(run, dict) or run.get('run_attempt') != requested['run_attempt']:
            raise protocol.Error('source run attempt does not exist')
        for field in ('repository', 'head_repository'):
            if not policy._repo_matches(run.get(field), known):
                raise protocol.Error('source run is not from this repository')
        event_name = run.get('event')
        if event_name not in _SOURCE_EVENTS:
            raise protocol.Error('source event is not supported by schema 2')
        event = {'repository': run['repository']}
        if event_name == 'pull_request':
            matches = [p for p in run.get('pull_requests', []) if p.get('head', {}).get('sha') == requested['sha']]
            if not matches:
                raise protocol.Error('test SHA is not the source pull request head')
            pr = self.api('GET', f'/repos/{repo}/pulls/{matches[0]["number"]}')
            event['pull_request'] = pr
            concurrency_key = f'pr:{pr["number"]}'
        else:
            if run.get('head_sha') != requested['sha']:
                raise protocol.Error('test SHA is not the source run head')
            concurrency_key = f'branch:{run.get("head_branch", "")}'
        env = {'GITHUB_REPOSITORY': repo, 'GITHUB_REPOSITORY_ID': str(known.id), 'GITHUB_EVENT_NAME': event_name,
               'CI_RUNNER_CLASS': 'plan', 'CI_RUNNER_AUDIENCE': 'private'}
        if organization is not None:
            decision = policy.check_context(policy.Policy(frozenset(), {repo: known}), env, event)
        else:
            actor = (run.get('actor') or {}).get('login')
            trigger = (run.get('triggering_actor') or {}).get('login')
            env.update(GITHUB_ACTOR=actor, GITHUB_TRIGGERING_ACTOR=trigger)
            event['sender'] = {'login': actor}
            if event_name == 'push':
                event['pusher'] = {'name': actor}
            decision = self.admit(pol, env, event, repository_members=repository_members)
        if not decision.accepted:
            raise protocol.Error(f'source admission refused: {decision.reason}')
        if run.get('conclusion') == 'cancelled':
            raise protocol.Error('source run was cancelled')
        # Keep the original policy facts for the execution hook, not the service actor.
        return {**requested, 'repository': repo, 'concurrency_key': concurrency_key,
                'admission': {'env': env, 'event': event}}

    def run_attempt(self, source):
        run = self.api('GET', f'/repos/{source["repository"]}/actions/runs/{source["run_id"]}/attempts/{source["run_attempt"]}')
        if not isinstance(run, dict) or run.get('run_attempt') != source['run_attempt']:
            raise protocol.Error('source run attempt does not match')
        return run

    def _list_records(self, path, field):
        records = []
        for page in range(1, 101):
            response = self.api('GET', f'{path}?per_page=100&page={page}')
            rows = response.get(field) if isinstance(response, dict) else None
            if not isinstance(rows, list):
                raise protocol.Error(f'GitHub returned no {field}')
            records.extend(rows)
            if len(rows) < 100:
                return records
        raise protocol.Error(f'GitHub {field} listing exceeds the reconciliation limit')

    def jobs(self, source):
        return self._list_records(
            f'/repos/{source["repository"]}/actions/runs/{source["run_id"]}/attempts/{source["run_attempt"]}/jobs', 'jobs')

    def artifacts(self, source):
        return self._list_records(f'/repos/{source["repository"]}/actions/runs/{source["run_id"]}/artifacts', 'artifacts')

    def verify_claim(self, allocation, run_id, run_attempt):
        source = allocation['source']
        if (run_id, run_attempt) != (source['run_id'], source['run_attempt']):
            raise protocol.Error('execution is from another source attempt')
        if self.run_attempt(source).get('status') == 'completed':
            raise protocol.Error('source attempt is no longer active')
        job_name = protocol.execution_job_name(allocation['task'], allocation['task_attempt'])
        job = find_job(self.jobs(source), job_name)
        if job is None or job.get('status') != 'in_progress':
            raise protocol.Error('authorized execution job is not running')
        validate_execution_job(allocation, job)
        return job['id']


def find_job(jobs, expected_name):
    matches = [job for job in jobs if job.get('name') == expected_name
               or job.get('name', '').endswith(' / ' + expected_name)]
    if len(matches) > 1:
        raise protocol.Error('execution job name is ambiguous')
    return matches[0] if matches else None


def validate_execution_job(allocation, job):
    if not protocol.positive(job.get('id')) or job.get('run_id') != allocation['run_id']:
        raise protocol.Error('execution job is from another run')
    if allocation['job_id'] is not None and allocation['job_id'] != job['id']:
        raise protocol.Error('another job already owns this allocation')
    runner = job.get('runner_name')
    if runner != allocation['runner'] and (runner or job.get('status') == 'in_progress'):
        raise protocol.Error('execution job is on another runner')
    if job.get('status') != 'completed' and allocation['runner'] not in job.get('labels', []):
        raise protocol.Error('execution job does not target the authorized runner')


class WorkflowCoordinator:
    def __init__(self, store, api: GitHub, *, clock=time.time, storage_etc=None):
        self.store, self.api, self.clock = store, api, clock
        self.storage_etc = storage_etc
        self.last_write = 0.

    def publish_status(self, source, state, description):
        self.api.api('POST', f'/repos/{source["repository"]}/statuses/{source["sha"]}', {
            'context': source['context'], 'state': 'error' if state == 'cancelled' else state,
            'description': github.status_text(description),
            'target_url': f'https://github.com/{source["repository"]}/actions/runs/{source["run_id"]}'})

    def publish_notice(self, source):
        batch_id = protocol.source_key(source)
        if any(batch['id'] == batch_id for batch in self.store.batches()):
            return
        if not self.store.is_latest_status({'source': source}):
            return
        notice = next(record for record in self.store.notices() if record['id'] == batch_id)
        self.publish_status(source, notice['state'], notice['reason'])

    def publish_batch(self, batch_id):
        batch = self.store.batch(batch_id)
        if self.store.is_latest_status(batch):
            self.publish_status(batch['source'], batch['state'], f'CI tests: {batch["state"]}; {len(batch["tasks"])} tasks')

    def send_one(self):
        if self.clock() - self.last_write < 1:
            return False
        message = self.store.next_message()
        if message is None:
            return False
        self.last_write = self.clock()
        try:
            if message['kind'] == 'source_status':
                self.publish_notice(message['payload']['source'])
            elif message['kind'] == 'status':
                self.publish_batch(message['payload']['batch_id'])
            else:
                raise protocol.Error('unknown status outbox message')
        except (OSError, ValueError, github.ApiError) as error:
            self.store.message_retry(message['id'], type(error).__name__, min(300, 2 ** min(message['attempts'], 8)))
        else:
            self.store.message_done(message['id'])
        return True

    def reconcile_execution_jobs(self, batch, jobs, source_done):
        source = batch['source']
        artifacts = None
        for allocation in self.store.allocations():
            if allocation['batch_id'] != batch['id'] or allocation['runner_done']:
                continue
            if allocation['state'] == 'reserved' and self.clock() >= allocation['deadline']:
                self.store.expire(allocation['id'])
            job = find_job(jobs, protocol.execution_job_name(allocation['task'], allocation['task_attempt']))
            if job is None:
                if source_done:
                    self.store.runner_finished(allocation['id'], source['run_id'], source['run_attempt'],
                                               job_id=allocation['job_id'], conclusion='failure')
                continue
            validate_execution_job(allocation, job)
            if job.get('status') != 'completed':
                continue
            conclusion = job.get('conclusion') or 'error'
            references = []
            receipt = None
            if bundles.backend(source)['backend'] == 'ssh':
                if not allocation['released']:
                    continue
                try:
                    if self.storage_etc is None:
                        raise storage.Error('configuration')
                    receipt = bundles.verify_result(self.storage_etc, allocation, job['id'])
                except storage.Error:
                    if conclusion == 'success':
                        conclusion = 'error'
            elif conclusion == 'success':
                if artifacts is None:
                    artifacts = self.api.artifacts(source)
                references = [{'id': item['id'], 'name': item['name']} for item in artifacts
                              if item.get('name') == f'ci-allocation-{allocation["id"]}' and item.get('expired') is False]
                if not references:
                    conclusion = 'error'
            self.store.runner_finished(allocation['id'], source['run_id'], source['run_attempt'],
                                       job_id=job['id'], conclusion=conclusion, artifacts=references, storage_reference=receipt)

    def reconcile_task_requests(self, batch, jobs):
        for task in batch['tasks']:
            if task['state'] != 'queued':
                continue
            task_attempt = task['attempts'] + 1
            job = find_job(jobs, protocol.allocation_job_name(task['id'], task_attempt))
            if job and job.get('status') == 'completed' and job.get('conclusion') != 'success':
                self.store.task_request_failed(batch['id'], task['id'], task_attempt)

    def reconcile(self):
        active_batches = {allocation['batch_id'] for allocation in self.store.allocations()}
        for batch in self.store.batches():
            if batch['state'] != 'pending' and batch['id'] not in active_batches:
                continue
            run = self.api.run_attempt(batch['source'])
            source_done = run.get('status') == 'completed'
            if source_done:
                self.store.source_finished(batch['id'], cancelled=run.get('conclusion') == 'cancelled')
            jobs = self.api.jobs(batch['source'])
            self.reconcile_execution_jobs(batch, jobs, source_done)
            self.reconcile_task_requests(self.store.batch(batch['id']), jobs)
        for notice in self.store.notices(pending=True):
            run = self.api.run_attempt(notice['source'])
            if run.get('status') == 'completed':
                self.store.notice(notice['source'], 'error', 'Source workflow ended before submitting a test batch')
