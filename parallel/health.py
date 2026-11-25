"""Observable connection health, separate from listener/process liveness."""
from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path

from ci_runners import __version__, github, pool
from parallel import deployment_files, transport

LOGGER = logging.getLogger('ci.parallel')


class ConnectionHealth:
    def __init__(self, path, *, clock=time.time):
        self.path, self.clock = Path(path), clock
        self.last_success = None
        self.failures = 0

    def record(self, error=None, **details):
        if error is None:
            self.last_success, self.failures = self.clock(), 0
        else:
            self.failures += 1
            if self.failures == 1 or self.failures % 12 == 0:
                LOGGER.warning('control synchronization failed (%s), consecutive failures=%d', type(error).__name__, self.failures)
        report = dict(version=__version__, observed_at=self.clock(), last_success=self.last_success,
                      pid=os.getpid(), boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                      connected=error is None, consecutive_failures=self.failures,
                      last_error=type(error).__name__ if error else None, **details)
        deployment_files.write_json(self.path, report)

    def retry_delay(self):
        return min(30, 5 * 2 ** min(self.failures, 3))


def github_runners(organization, token_file, *, call=github.api):
    """Missing read permission is unknown, never fabricated online status."""
    try:
        token = transport.secret(str(token_file))
        result, page = {}, 1
        while True:
            reply = call('GET', f'https://api.github.com/orgs/{organization}/actions/runners?per_page=100&page={page}', token)
            rows = reply['runners']
            for row in rows:
                result[row['name']] = {'status': row['status'], 'busy': row['busy']}
            if len(rows) < 100:
                return result, None
            page += 1
    except (OSError, ValueError, KeyError, github.ApiError) as error:
        return {}, type(error).__name__


def status(config, *, query_github=True, run=subprocess.run):
    observed = pool.runner_procs(str(config.runtime/'actions-runner'))
    alive, busy = observed if observed is not None else (set(), set())
    online, github_error = ({}, 'not queried')
    if query_github and config.is_controller:
        online, github_error = github_runners(config.document['github']['organization'], config.private/'github-pat')
    runners = [{'name': name, 'listener_alive': name in alive if observed is not None else None,
                'worker_busy': name in busy if observed is not None else None,
                'github_status': online.get(name, {}).get('status', 'unknown')} for name, _, _ in config.runner_specs()]
    services = {}
    roles = (['scheduler'] if config.is_controller else []) + (['executor'] if 'executor' in config.host['roles'] else [])
    for role in roles:
        path = config.control/'state'/f'{role}-health.json'
        report = deployment_files.read_json(path, {'connected': False, 'observed_at': 0, 'last_error': 'no heartbeat'})
        report['fresh'] = 0 <= time.time()-report['observed_at'] <= 120
        try:
            report['service_active'] = run(['systemctl', '--user', 'is-active', f'ci-parallel-{role}.service'],
                                           capture_output=True, timeout=15).returncode == 0
        except (OSError, subprocess.SubprocessError):
            report['service_active'] = None
        report['ready'] = report['service_active'] is True and report['fresh'] and report['connected']
        services[role] = report
    receipt = deployment_files.read_json(config.control/'state/deployment.json')
    deployment = {key: receipt.get(key) for key in ('version', 'status', 'source_digest', 'updated_at', 'last_error')}
    return dict(version=__version__, host=config.host_name, services=services, runners=runners,
                github_query_error=github_error, deployment=deployment)
