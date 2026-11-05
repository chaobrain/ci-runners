"""The runner hooks (design §6.3): ``job-started`` decides, ``job-completed`` cleans up.

Run by ``hooks/job-started.sh`` and ``hooks/job-completed.sh`` with the system Python,
isolated (``-I``). ``job-started`` reads only local files (the policy, the inventory,
``GITHUB_EVENT_PATH``), does no network I/O, bounds itself at 60 s and prints one line,
``ci-runners: accepted|refused <reason> policy=<hash>``; details go to the host's
``logs/hook.log`` only. Any error refuses the job. ``job-completed`` stops the job's
part scopes and always exits 0, within 120 s.
"""
from __future__ import annotations

import datetime
import json
import os
import signal
import subprocess
import sys

from ci_runners import policy

MAX_POLICY = 1 << 20
MAX_EVENT = 64 << 20
START_BOUND, COMPLETE_BOUND = 60, 120


class _Timeout(Exception):
    pass


def _read(path: str, limit: int) -> bytes:
    with open(path, 'rb') as f:
        data = f.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f'{path}: larger than {limit} bytes')
    return data


def _log(etc: str, record: dict) -> None:
    """Append a JSON line to ``<data>/ci-runners/logs/hook.log`` (beside ``etc``)."""
    try:
        logs = os.path.join(os.path.dirname(os.path.abspath(etc)), 'logs')
        os.makedirs(logs, mode=0o700, exist_ok=True)
        record['time'] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
        with open(os.path.join(logs, 'hook.log'), 'a') as f:
            f.write(json.dumps(record, sort_keys=True) + '\n')
    except OSError:
        pass  # the decision stands without its log line


def started(env: dict[str, str]) -> tuple[int, str]:
    """The decision as ``(exit status, printed line)``; never raises."""
    etc = env.get('CI_RUNNERS_ETC', '')
    digest = ''
    detail: dict = {'runner': env.get('RUNNER_NAME', ''), 'repository': env.get('GITHUB_REPOSITORY', ''),
                    'event': env.get('GITHUB_EVENT_NAME', ''), 'actor': env.get('GITHUB_ACTOR', ''),
                    'triggering_actor': env.get('GITHUB_TRIGGERING_ACTOR', ''), 'run': env.get('GITHUB_RUN_ID', ''),
                    'class': env.get('CI_RUNNER_CLASS', '')}
    try:
        if not etc:
            raise policy.PolicyError('CI_RUNNERS_ETC is not set')
        try:
            pol = policy.load(_read(os.path.join(etc, 'policy.toml'), MAX_POLICY))
        except (OSError, ValueError) as e:
            detail['error'] = f'policy: {e}'
            d = policy.Decision(False, 'policy')
        else:
            digest = pol.digest
            try:
                event = json.loads(_read(env.get('GITHUB_EVENT_PATH', ''), MAX_EVENT))
            except (OSError, ValueError, RecursionError) as e:
                detail['error'] = f'event: {type(e).__name__}'
                event = None
            d = policy.decide(pol, env, event)
    except _Timeout:
        d = policy.Decision(False, 'timeout')
    except Exception as e:  # noqa: BLE001 - any failure refuses
        detail['error'] = f'{type(e).__name__}: {e}'
        d = policy.Decision(False, 'error')
    detail.update(accepted=d.accepted, reason=d.reason, policy=digest[:12])
    _log(etc or '.', detail)
    return (0 if d.accepted else 1), policy.line(d, digest)


def _job_scopes(env: dict[str, str]) -> list[str]:
    """The part scopes the executor started for this runner's current job."""
    runner = env.get('RUNNER_NAME', '')
    if not runner:
        return []
    r = subprocess.run(['systemctl', '--user', 'list-units', '--type=scope', '--plain', '--no-legend',
                        f'ci-part-{runner}-*'], capture_output=True, text=True, timeout=30)
    return [line.split()[0] for line in r.stdout.splitlines() if line.strip()]


def completed(env: dict[str, str]) -> int:
    """Stop the job's part scopes (their processes die with the cgroup, so their slots
    and leases go too). Always 0: a failed cleanup is the reaper's to finish."""
    stopped = []
    try:
        scopes = _job_scopes(env)
        if scopes:
            subprocess.run(['systemctl', '--user', 'stop', *scopes], capture_output=True, timeout=90)
            stopped = scopes
    except (OSError, subprocess.SubprocessError, _Timeout) as e:
        _log(env.get('CI_RUNNERS_ETC', '.'), {'hook': 'job-completed', 'error': type(e).__name__})
        return 0
    _log(env.get('CI_RUNNERS_ETC', '.'), {'hook': 'job-completed', 'runner': env.get('RUNNER_NAME', ''),
                                         'run': env.get('GITHUB_RUN_ID', ''), 'stopped': stopped})
    return 0


def main(argv: list[str]) -> int:
    def alarm(_sig, _frame):
        raise _Timeout()
    signal.signal(signal.SIGALRM, alarm)
    env = dict(os.environ)
    if argv == ['job-started']:
        signal.alarm(START_BOUND)
        code, out = started(env)
        signal.alarm(0)
        print(out, flush=True)
        return code
    if argv == ['job-completed']:
        signal.alarm(COMPLETE_BOUND)
        try:
            return completed(env)
        finally:
            signal.alarm(0)
    print('usage: hook job-started|job-completed', file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
