"""A public caller's CPU part on a GitHub-hosted runner (decision 18.7): ``actions/hosted``,
called by ``public-part.yml`` only.

``ci-run hosted --id I --command C --timeout-minutes M [--results G] [--resume true]
[--attempt-of R]`` runs ``bash -c C`` in the job's checkout, in a session of its own, bounded
at ``M`` minutes, and writes what the part action's artifact holds
(``$RUNNER_TEMP/ci-runners/<id>/outcome.json`` and ``summary.md``) and its outputs. A hosted
runner is a VM of its own for one job: there is no slot, lease, scope or record to take, and
nothing to salvage. Its outcome is ``pass`` (exit 0) or ``red`` (any other end, the bound
included); a job that ends without its outputs is ``lost``, which the retry reads.

It refuses to run anywhere but on a GitHub-hosted runner (``RUNNER_ENVIRONMENT``) without a
host layer (``CI_RUNNERS_ETC``): on one of our runners a part is the executor's (§4.3).
No token reaches the command: the job's own variables that carry one are dropped.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping

ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z')
#: what the command's environment never holds (a token by any of its names)
SECRET = re.compile(r'(TOKEN|SECRET|PASSWORD|CREDENTIAL)', re.I)
KILL_GRACE = 10  # s: from SIGTERM to SIGKILL at the bound
MAX_MINUTES = 240


class Refused(ValueError):
    pass


def command_env(env: Mapping[str, str], results: str, resume: bool) -> dict[str, str]:
    """The command's environment: the job's, without any variable that names a token,
    with ``CI_RESULTS_DIR`` and, for a retry that resumes, ``CI_RESUME=1``. Pure."""
    out = {k: v for k, v in env.items() if not SECRET.search(k) and not k.startswith('ACTIONS_ID_TOKEN')}
    out['CI_RESULTS_DIR'] = results
    out.pop('CI_RESUME', None)
    if resume:
        out['CI_RESUME'] = '1'
    return out


def outcome_of(code: int | None, timed_out: bool, minutes: float) -> tuple[str, str]:
    """``(outcome, reason)`` of the command's end. Pure."""
    if timed_out:
        return 'red', f'timeout: the part ran past its {minutes:g} min'
    if code == 0:
        return 'pass', 'exit 0'
    if code is not None and code < 0:
        return 'red', f'signal {-code}'
    return 'red', f'exit {code}'


def run(argv: list[str], env: Mapping[str, str] | None = None, *, popen: Callable = subprocess.Popen,
        clock: Callable[[], float] = time.monotonic) -> int:
    ap = argparse.ArgumentParser(prog='ci-run hosted')
    ap.add_argument('--id', required=True)
    ap.add_argument('--command', required=True)
    ap.add_argument('--timeout-minutes', type=float, required=True)
    ap.add_argument('--results', default='')
    ap.add_argument('--resume', default='false')
    ap.add_argument('--attempt-of', default='')
    a = ap.parse_args(argv)
    env = dict(os.environ if env is None else env)
    try:
        if env.get('RUNNER_ENVIRONMENT') != 'github-hosted' or env.get('CI_RUNNERS_ETC'):
            raise Refused('a hosted part runs on a GitHub-hosted runner only; on ours a part is the executor\'s')
        if not ID.fullmatch(a.id) or not 0 < a.timeout_minutes <= MAX_MINUTES or not env.get('RUNNER_TEMP'):
            raise Refused('--id, --timeout-minutes or RUNNER_TEMP is malformed')
    except Refused as e:
        print(f'::error::ci-run hosted: {e}')
        return 2
    attempt = 2 if a.attempt_of else 1
    run_attempt = env.get('GITHUB_RUN_ATTEMPT', '0')
    run_attempt_n = int(run_attempt) if run_attempt.isdigit() and 0 < int(run_attempt) < 10000 else 0
    out_dir = os.path.join(env['RUNNER_TEMP'], 'ci-runners', a.id)
    results = os.path.join(out_dir, 'results')
    os.makedirs(results, exist_ok=True)
    t0 = clock()
    timed_out = False
    p = popen(['bash', '-c', a.command], cwd=env.get('GITHUB_WORKSPACE') or None,
              env=command_env(env, results, a.resume == 'true'), start_new_session=True)
    try:
        code = p.wait(timeout=a.timeout_minutes * 60)
    except subprocess.TimeoutExpired:
        timed_out = True
        for sig, wait in ((signal.SIGTERM, KILL_GRACE), (signal.SIGKILL, None)):
            try:
                os.killpg(p.pid, sig)
            except ProcessLookupError:
                break
            try:
                p.wait(timeout=wait)
                break
            except subprocess.TimeoutExpired:
                continue
        code = p.returncode
    secs = clock() - t0
    outcome, reason = outcome_of(code, timed_out, a.timeout_minutes)
    found = sorted(glob.glob(os.path.join(results, a.results))) if a.results else []
    doc = {'outcome': outcome, 'reason': reason, 'next': 'none', 'retry-on': [], 'run-attempt': run_attempt_n,
           'attempt': attempt, 'host': 'hosted', 'run-seconds': round(secs, 1)}
    with open(os.path.join(out_dir, 'outcome.json'), 'w', encoding='utf-8') as f:
        json.dump(doc, f, sort_keys=True)
        f.write('\n')
    summary = (f'### part `{a.id}`: {outcome}\n\n{reason}; attempt {attempt} on a GitHub-hosted runner, '
               f'{secs:.0f} s' + (f'; {len(found)} result file(s)' if a.results else '') + '\n')
    with open(os.path.join(out_dir, 'summary.md'), 'w', encoding='utf-8') as f:
        f.write(summary)
    if env.get('GITHUB_OUTPUT'):
        with open(env['GITHUB_OUTPUT'], 'a', encoding='utf-8') as f:
            f.write(f'outcome={outcome}\nreason={reason}\nnext=none\nattempt={attempt}\nhost=hosted\n')
    if env.get('GITHUB_STEP_SUMMARY'):
        with open(env['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as f:
            f.write(summary)
    print(f'ci-run hosted: {a.id}: {outcome} ({reason})', file=sys.stderr)
    return 0 if outcome == 'pass' else 1


def main(argv: list[str]) -> int:
    return run(argv)
