"""The runner hooks (design §6.3): ``job-started`` decides, ``job-completed`` cleans up.

Run by ``hooks/job-started.sh`` and ``hooks/job-completed.sh`` with the system Python,
isolated (``-I``). ``job-started`` reads only local files (the policy, with a schema 2
policy the org snapshot ``org.json``, the inventory and ``GITHUB_EVENT_PATH``), does no network I/O, bounds itself at 60 s and prints one line,
``ci-runners: accepted|refused <reason> policy=<hash>``, by the runner's class and audience
(design §6.3). Both are the inventory's for the runner's name (``RUNNER_NAME``, which no
workflow can set; decision 18.7, C2c), never the environment's: ``job-started.sh`` starts the
decision under ``env -i`` with the ``GITHUB_*`` and ``RUNNER_*`` it reads (``ENV``), so no
``CI_RUNNER_*``, ``LD_*`` or ``PYTHON*`` of a job's arrives, and ``started`` ignores
``CI_RUNNER_CLASS`` and ``CI_RUNNER_AUDIENCE`` where a caller passes them. A runner the
inventory does not name is refused (``runner``, the host's fault: a stale runner directory)
and an inventory that does not load refuses (``inventory``). Details go to the host's
``logs/hook-<date>.log`` only. Any error refuses the job. ``job-completed`` marks the job's
parts whose executor outlived it as cancelled (``cancel_parts``, issue #257), stops the job's
part scopes and always exits 0, within 120 s.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import signal
import stat
import subprocess
import sys
import time

from ci_runners import policy

MAX_POLICY = 1 << 20
MAX_SNAPSHOT = 4 << 20
MAX_EVENT = 64 << 20
MAX_INVENTORY = 1 << 20
START_BOUND, COMPLETE_BOUND = 60, 120
#: the environment ``job-started`` reads, and all that ``job-started.sh`` passes it (C2c)
ENV = ('GITHUB_ACTOR', 'GITHUB_EVENT_NAME', 'GITHUB_EVENT_PATH', 'GITHUB_REF_PROTECTED', 'GITHUB_REPOSITORY',
       'GITHUB_REPOSITORY_ID', 'GITHUB_RUN_ID', 'GITHUB_TRIGGERING_ACTOR', 'RUNNER_NAME')


class _Timeout(Exception):
    pass


def _read(path: str, limit: int) -> bytes:
    with open(path, 'rb') as f:
        data = f.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f'{path}: larger than {limit} bytes')
    return data


def _log(etc: str, record: dict) -> None:
    """Append a JSON line to ``<data>/ci-runners/logs/hook-<UTC date>.log`` (beside ``etc``;
    the reaper drops a day's file after 14 days)."""
    try:
        logs = os.path.join(os.path.dirname(os.path.abspath(etc)), 'logs')
        os.makedirs(logs, mode=0o700, exist_ok=True)
        now = datetime.datetime.now(datetime.timezone.utc)
        record['time'] = now.isoformat(timespec='seconds')
        # never block or follow a link: an earlier job of this account may have planted a FIFO
        fd = os.open(os.path.join(logs, f'hook-{now:%Y-%m-%d}.log'),
                     os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return
            os.write(fd, (json.dumps(record, sort_keys=True) + '\n').encode())
        finally:
            os.close(fd)
    except OSError:
        pass  # the decision stands without its log line


def runner_facts(etc: str, name: str) -> tuple[str, str] | None:
    """The runner's ``(class, audience)`` as this host's inventory names it: a private pool's
    runner as setup names it, a public runner as ``<role>-pub-gpu<N>-<seq>`` on a card of the
    host where the inventory has a public pool (decision 18.7); None for any other name.
    Raises ValueError or OSError where the inventory does not load."""
    from ci_runners import inventory, setup
    inv = inventory.load(_read(os.path.join(etc, 'inventory.toml'), MAX_INVENTORY).decode('utf-8'))
    for n, _, cls in setup.runners(inv):
        if n == name:
            return cls, 'private'
    m = re.fullmatch(rf'{re.escape(inv.this)}{inventory.PUBLIC_HOST_SUFFIX}([0-9]{{1,4}})-[0-9]{{1,12}}', name)
    if m and inventory.public_pool(inv) is not None and int(m.group(1)) in inv.host['gpus']:
        return 'gpu', 'public'
    return None


def started(env: dict[str, str]) -> tuple[int, str]:
    """The decision as ``(exit status, printed line)``; never raises."""
    etc = env.get('CI_RUNNERS_ETC', '')
    digest = snap_digest = ''
    detail: dict = {'runner': env.get('RUNNER_NAME', ''), 'repository': env.get('GITHUB_REPOSITORY', ''),
                    'event': env.get('GITHUB_EVENT_NAME', ''), 'actor': env.get('GITHUB_ACTOR', ''),
                    'triggering_actor': env.get('GITHUB_TRIGGERING_ACTOR', ''), 'run': env.get('GITHUB_RUN_ID', ''),
                    'class': '', 'audience': '', 'ref_protected': env.get('GITHUB_REF_PROTECTED', '')}
    try:
        if not etc:
            raise policy.PolicyError('CI_RUNNERS_ETC is not set')
        try:
            pol = policy.load(_read(os.path.join(etc, 'policy.toml'), MAX_POLICY))
            digest = pol.digest
            if pol.org is not None:
                snap = policy.load_snapshot(_read(os.path.join(etc, 'org.json'), MAX_SNAPSHOT))
                pol = policy.with_snapshot(pol, snap, time.time())
                snap_digest = pol.snapshot_digest
        except policy.StaleSnapshot as e:
            detail['error'] = str(e)
            d = policy.Decision(False, 'stale')
        except (OSError, ValueError) as e:
            detail['error'] = f'policy: {e}'
            d = policy.Decision(False, 'policy')
        else:
            try:
                facts = runner_facts(etc, env.get('RUNNER_NAME', ''))
            except (OSError, ValueError) as e:
                detail['error'] = f'inventory: {e}'[:300]
                d = policy.Decision(False, 'inventory')
            else:
                if facts is None:  # a runner directory this host's inventory does not know
                    detail['error'] = 'runner: the inventory names no such runner'
                    d = policy.Decision(False, 'runner')
                else:
                    detail['class'], detail['audience'] = facts
                    env = {**env, 'CI_RUNNER_CLASS': facts[0], 'CI_RUNNER_AUDIENCE': facts[1]}  # never the caller's
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
    detail.update(accepted=d.accepted, reason=d.reason, policy=digest[:12], org=snap_digest[:12])
    _log(etc or '.', detail)
    return (0 if d.accepted else 1), policy.line(d, digest, snap_digest)


def _job_scopes(env: dict[str, str]) -> list[str]:
    """The part scopes the executor started for this runner's current job."""
    runner = env.get('RUNNER_NAME', '')
    if not runner:
        return []
    r = subprocess.run(['systemctl', '--user', 'list-units', '--type=scope', '--plain', '--no-legend',
                        f'ci-part-{runner}-*'], capture_output=True, text=True, timeout=30)
    return [line.split()[0] for line in r.stdout.splitlines() if line.strip()]


MAX_RECORD = 1 << 20


def cancel_parts(env: dict[str, str], alive=None) -> list[str]:
    """The job's parts whose executor outlived the job (issue #257): this runner's records of
    this run (``GITHUB_REPOSITORY``, ``GITHUB_RUN_ID``, any run attempt) with no
    ``outcome.json`` whose executor still runs. The job ended under them, so it was
    cancelled (a step the runner's signal did not reach, an orphan the runner kills next):
    each gets the cancel mark (``outcome.CANCELLED``), and one still waiting for a slot, a
    card or a lock (no ``run_epoch`` in its record yet) gets SIGTERM, so that it ends
    ``stopped`` and starts no command. An executor already gone is left alone: its job was not
    cancelled under it, and presence counts it. Returns the records marked."""
    from ci_runners import outcome, slots
    alive = alive or slots.alive
    runner, run, repo = env.get('RUNNER_NAME', ''), env.get('GITHUB_RUN_ID', ''), env.get('GITHUB_REPOSITORY', '')
    if not runner or not run.isdigit() or not re.fullmatch(r'[A-Za-z0-9-]+/[A-Za-z0-9._-]+', repo):
        return []
    root = os.path.join(os.path.dirname(os.path.abspath(env.get('CI_RUNNERS_ETC', '.'))), 'state', 'parts',
                        *repo.lower().split('/'))
    out = []
    try:
        runs = [e.path for e in os.scandir(root) if re.fullmatch(rf'{run}-[0-9]+', e.name)
                and e.is_dir(follow_symlinks=False)]
    except OSError:
        return []
    for d in sorted(runs):
        try:
            records = sorted(e.path for e in os.scandir(d) if e.is_dir(follow_symlinks=False))
        except OSError:
            continue
        for record in records:
            try:
                if os.path.lexists(os.path.join(record, 'outcome.json')):
                    continue
                rec = json.loads(_read(os.path.join(record, 'record.json'), MAX_RECORD))
                if not isinstance(rec, dict) or rec.get('runner') != runner:
                    continue
                pid, start = int(rec.get('executor', 0)), float(rec.get('start_epoch', 0))
                if pid <= 1 or not alive(pid, start):
                    continue
                outcome.mark_cancelled(record, 'job-completed')
                out.append('/'.join(record.split(os.sep)[-2:]))
                if 'run_epoch' not in rec:
                    os.kill(pid, signal.SIGTERM)
            except (OSError, ValueError, TypeError, OverflowError):
                continue  # one unreadable record stops no other
    return out


def completed(env: dict[str, str]) -> int:
    """Mark the job's parts its end cancelled (``cancel_parts``), then stop the job's part
    scopes (their processes die with the cgroup, so their slots and leases go too). Always
    0: a failed cleanup is the reaper's to finish."""
    stopped, cancelled = [], []
    try:
        cancelled = cancel_parts(env)
    except Exception as e:  # noqa: BLE001 - the scopes are stopped all the same
        _log(env.get('CI_RUNNERS_ETC', '.'), {'hook': 'job-completed', 'error': type(e).__name__})
    try:
        scopes = _job_scopes(env)
        if scopes:
            subprocess.run(['systemctl', '--user', 'stop', *scopes], capture_output=True, timeout=90)
            stopped = scopes
    except (OSError, subprocess.SubprocessError, _Timeout) as e:
        _log(env.get('CI_RUNNERS_ETC', '.'), {'hook': 'job-completed', 'error': type(e).__name__})
        return 0
    _log(env.get('CI_RUNNERS_ETC', '.'), {'hook': 'job-completed', 'runner': env.get('RUNNER_NAME', ''),
                                         'run': env.get('GITHUB_RUN_ID', ''), 'stopped': stopped,
                                         **({'cancelled': cancelled} if cancelled else {})})
    return 0


def main(argv: list[str]) -> int:
    def alarm(_sig, _frame):
        raise _Timeout()
    signal.signal(signal.SIGALRM, alarm)
    env = dict(os.environ)
    if argv[:1] == ['job-started'] and len(argv) <= 2:
        if len(argv) == 2 and argv[1]:  # the etc setup wrote into the hook: the environment's never wins
            env['CI_RUNNERS_ETC'] = argv[1]
        signal.alarm(START_BOUND)
        try:
            code, out = started(env)
        except _Timeout:  # the bound hit outside started()'s own handler (its log line, say)
            code, out = 1, policy.line(policy.Decision(False, 'timeout'), '')
        except BaseException:  # noqa: BLE001 - whatever happens, one fixed line and a refusal
            code, out = 1, policy.line(policy.Decision(False, 'error'), '')
        finally:
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
