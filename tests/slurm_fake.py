"""A fake Slurm and a fake ssh for the tests of the ``slurm`` class (RUN-9c to 9e).

``install(bin_dir)`` links ``sbatch``, ``squeue``, ``sacct``, ``scancel``, ``sinfo`` and
``ssh`` to this file; each acts by its name on a state directory, ``$FAKE_SLURM``:

- ``jobs/<id>.json``: a job (its ``#SBATCH`` values, its state, its runner's pid);
- ``plan.json``: a list of behaviours, the n-th for the n-th submission, else ``{}``:
  ``pend`` (s pending before it starts; ``-1`` for ever), ``reason`` (the pending
  reason), ``end`` (a final state it takes instead of running: ``NODE_FAIL``,
  ``TIMEOUT``, ...), ``other_cancel`` (s after its start another uid cancels it);
- ``ledger.json``: more ``sacct`` rows, for the ledger;
- ``down.json``: partitions ``sinfo`` reports down.

``squeue -j`` lists a finished job ``MIN_JOB_AGE`` s in its final state, then fails with
"Invalid job id", as a real one does.

A job that runs runs its batch script with ``bash`` under a runner (``--run``), in a
session of its own, with ``SLURM_JOB_ID`` and, for cards, ``CUDA_VISIBLE_DEVICES`` (of cards no host has); the
script's output goes to its ``--output``. ``scancel`` TERMs the runner, which TERMs the
batch shell and KILLs it ``KILL_WAIT`` s later; the job ends ``CANCELLED by <uid>``.
States move when a command looks at them, as a scheduler's would between polls.

``ssh`` maps a host to a base directory by ``$FAKE_SSH/hosts.json``
(``{"<alias>": {"base": ..., "release": ..., "down": false}}``) and runs the gate there
with ``SSH_ORIGINAL_COMMAND``, as a forced command would; a host with ``"login": true``
and a ``home`` is an owner's login, which runs the command with ``sh -c`` in ``home``; ``-O`` control calls pass; an
unknown or down host fails with 255. ``$FAKE_SSH/calls.log`` records each call.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import signal
import subprocess
import sys
import time

KILL_WAIT = 3.0
MIN_JOB_AGE = 1.0  # s squeue still lists a finished job, then 'Invalid job id' (exit 1), as Slurm's MinJobAge
OTHER_UID = 4243
NAMES = ('sbatch', 'squeue', 'sacct', 'scancel', 'sinfo', 'ssh')
FINAL = ('COMPLETED', 'FAILED', 'CANCELLED', 'TIMEOUT', 'NODE_FAIL', 'BOOT_FAIL', 'OUT_OF_MEMORY')


def install(bin_dir: str) -> str:
    os.makedirs(bin_dir, exist_ok=True)
    for n in NAMES:
        p = os.path.join(bin_dir, n)
        with open(p, 'w') as f:
            f.write(f'#!/bin/sh\nexec {sys.executable} -I {os.path.abspath(__file__)} {n} "$@"\n')
        os.chmod(p, 0o755)
    return bin_dir


def _state_dir() -> str:
    return os.environ['FAKE_SLURM']


def _load(path: str, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save(path: str, obj) -> None:
    tmp = f'{path}.{os.getpid()}.tmp'
    with open(tmp, 'w') as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def _jobs_dir() -> str:
    d = os.path.join(_state_dir(), 'jobs')
    os.makedirs(d, exist_ok=True)
    return d


def _job_path(jid: int) -> str:
    return os.path.join(_jobs_dir(), f'{jid}.json')


def _iso(t: float | None) -> str:
    return datetime.datetime.fromtimestamp(t).strftime('%Y-%m-%dT%H:%M:%S') if t else 'Unknown'


def _alive(pid: int) -> bool:
    try:
        with open(f'/proc/{pid}/stat') as f:
            return f.read().rsplit(')', 1)[1].split()[0] not in ('Z', 'X')
    except OSError:
        return False


def _directives(script: str) -> dict[str, str]:
    out = {}
    with open(script) as f:
        for line in f:
            m = re.match(r'#SBATCH (--[a-z-]+)(?:=(.*))?$', line.rstrip('\n'))
            if m:
                out[m.group(1)] = m.group(2) or ''
    return out


def _start(j: dict) -> None:
    log = j['sb'].get('--output') or os.path.join(os.path.dirname(j['script']), f'slurm-{j["id"]}.out')
    env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'SLURM_JOB_ID': str(j['id']),
           'HOME': os.environ.get('HOME', '/'), 'FAKE_SLURM': _state_dir()}
    if j['cards']:  # cards no host has: a test never touches a real one
        env['CUDA_VISIBLE_DEVICES'] = ','.join(str(90 + i) for i in range(j['cards']))
    with open(log, 'ab') as out:
        p = subprocess.Popen([sys.executable, '-I', os.path.abspath(__file__), '--run', str(j['id']), j['script']],
                             start_new_session=True, stdin=subprocess.DEVNULL, stdout=out, stderr=out, env=env,
                             cwd=j['sb'].get('--chdir') or os.path.dirname(j['script']))
    j.update(state='RUNNING', start=time.time(), runner=p.pid)


def refresh(j: dict) -> dict:
    """Move a job on: start it after its pending time, end it as its runner ended."""
    if j['state'] in FINAL:
        return j
    now = time.time()
    b = j['plan']
    if j['state'] == 'PENDING':
        pend = b.get('pend', 0)
        if pend < 0 or now < j['submit'] + pend:
            return j
        if b.get('end'):
            j.update(state=b['end'], start=now, end=now, rc=1)
        else:
            _start(j)
        _save(_job_path(j['id']), j)
        return j
    rc_file = os.path.join(_jobs_dir(), f'{j["id"]}.rc')
    if not _alive(j['runner']) and os.path.exists(rc_file):
        with open(rc_file) as f:
            rc = int(f.read().strip() or 1)
        st = 'CANCELLED' if j.get('cancelled_by') is not None else ('COMPLETED' if rc == 0 else 'FAILED')
        j.update(state=st, end=now, rc=rc)
        _save(_job_path(j['id']), j)
    return j


def _all() -> list[dict]:
    out = []
    for n in sorted(os.listdir(_jobs_dir())):
        if n.endswith('.json'):
            j = _load(os.path.join(_jobs_dir(), n), None)
            if j:
                out.append(refresh(j))
    return out


def _get(jid: str) -> dict | None:
    j = _load(_job_path(int(jid)), None) if jid.isdigit() else None
    return refresh(j) if j else None


# ------------------------------------------------------------------ the commands


def sbatch(args: list[str]) -> int:
    if args == ['--version']:
        print('slurm 24.05.3')
        return 0
    test_only = '--test-only' in args
    script = [a for a in args if not a.startswith('-')][-1]
    sb = _directives(script)
    if any(k == '--gres' for k in sb):  # this fake site takes --gpus only
        print('sbatch: error: Batch job submission failed: Unspecified error', file=sys.stderr)
        return 1
    if test_only:
        print(f'sbatch: Job 1 to start at {_iso(time.time() + 3600)} using 1 processors on nodes fake01',
              file=sys.stderr)
        return 0
    seq_path = os.path.join(_state_dir(), 'seq')
    n = int(_load(seq_path, 0)) + 1
    _save(seq_path, n)
    plan = _load(os.path.join(_state_dir(), 'plan.json'), [])
    b = plan[n - 1] if n - 1 < len(plan) else {}
    gpus = re.search(r'([0-9]+)$', sb.get('--gpus', '')) if '--gpus' in sb else None
    j = {'id': 1000 + n, 'script': os.path.abspath(script), 'sb': sb, 'state': 'PENDING', 'submit': time.time(),
         'plan': b, 'cards': int(gpus.group(1)) if gpus else 0, 'uid': os.getuid(),
         'sbatch_env': sorted(k for k in os.environ if k.startswith(('SBATCH_', 'SLURM_')))}
    _save(_job_path(j['id']), j)
    refresh(j)
    print(j['id'])
    return 0


def squeue(args: list[str]) -> int:
    fmt = args[args.index('-o') + 1] if '-o' in args else '%i %T'
    ids = args[args.index('-j') + 1].split(',') if '-j' in args else None
    account = args[args.index('-A') + 1] if '-A' in args else None
    shown = 0
    for j in _all():
        if (j['state'] in FINAL and time.time() - j.get('end', 0) > MIN_JOB_AGE) or (ids is not None and str(j['id']) not in ids):
            continue
        if account is not None and j['sb'].get('--account') != account:
            continue
        shown += 1
        b = j['plan']
        vals = {'i': str(j['id']), 'T': j['state'], 'j': j['sb'].get('--job-name', ''),
                'r': b.get('reason', 'Resources') if j['state'] == 'PENDING' else 'None',
                'N': j['sb'].get('--nodelist', 'fake01') if j['state'] != 'PENDING' else '',
                'M': str(int(time.time() - j['start'])) if j.get('start') else '0'}
        print(re.sub(r'%([a-zA-Z])', lambda m: vals.get(m.group(1), ''), fmt))
    if ids is not None and not shown:
        print('slurm_load_jobs error: Invalid job id specified', file=sys.stderr)
        return 1
    return 0


def _row(j: dict) -> dict[str, str]:
    state = j['state'] + (f' by {j["cancelled_by"]}' if j['state'] == 'CANCELLED' else '')
    end = j.get('end')
    elapsed = int((end or time.time()) - j['start']) if j.get('start') else 0
    cpus = int(j['sb'].get('--cpus-per-task', '1'))
    tres = f'billing={cpus},cpu={cpus}' + (f',gres/gpu={j["cards"]}' if j['cards'] else '') + ',node=1'
    return {'JobID': str(j['id']), 'JobName': j['sb'].get('--job-name', ''), 'Partition': j['sb'].get('--partition', ''),
            'State': state, 'ExitCode': f'{j.get("rc", 0)}:0', 'ElapsedRaw': str(elapsed),
            'NodeList': j['sb'].get('--nodelist', 'fake01') if j.get('start') else 'None assigned',
            'AllocTRES': tres if j.get('start') else '', 'ReqTRES': tres, 'Start': _iso(j.get('start')), 'End': _iso(end),
            'TimelimitRaw': j['sb'].get('--time', '0')}


def sacct(args: list[str]) -> int:
    fields = args[args.index('-o') + 1].split(',') if '-o' in args else ['JobID', 'State']
    ids = args[args.index('-j') + 1].split(',') if '-j' in args else None
    rows = [_row(j) for j in _all() if ids is None or str(j['id']) in ids]
    if ids is None:
        rows += _load(os.path.join(_state_dir(), 'ledger.json'), [])
    for r in rows:
        print('|'.join(str(r.get(f, '')) for f in fields))
    return 0


def scancel(args: list[str], uid: int | None = None) -> int:
    for jid in args:
        j = _get(jid)
        if j is None or j['state'] in FINAL:
            continue
        j['cancelled_by'] = os.getuid() if uid is None else uid
        if j['state'] == 'PENDING':
            j.update(state='CANCELLED', end=time.time())
        else:
            try:
                os.kill(j['runner'], signal.SIGTERM)
            except OSError:
                pass
        _save(_job_path(j['id']), j)
    return 0


def sinfo(args: list[str]) -> int:
    p = args[args.index('-p') + 1] if '-p' in args else ''
    print('down' if p in _load(os.path.join(_state_dir(), 'down.json'), []) else 'up')
    return 0


def runner(jid: str, script: str) -> int:
    """The job's runner: the batch shell, TERM passed on and KILL after KILL_WAIT."""
    child = subprocess.Popen(['bash', script])
    termed = []

    def on_term(_s, _f):
        if not termed:
            termed.append(time.monotonic())
            try:
                child.send_signal(signal.SIGTERM)
            except OSError:
                pass
    signal.signal(signal.SIGTERM, on_term)
    other = _load(_job_path(int(jid)), {}).get('plan', {}).get('other_cancel')
    t0 = time.monotonic()
    while child.poll() is None:
        if other is not None and not termed and time.monotonic() - t0 >= other:
            j = _load(_job_path(int(jid)), {})
            j['cancelled_by'] = OTHER_UID
            _save(_job_path(int(jid)), j)
            on_term(None, None)
        if termed and time.monotonic() - termed[0] > KILL_WAIT:
            child.kill()
        time.sleep(0.05)
    with open(os.path.join(_jobs_dir(), f'{jid}.rc.tmp'), 'w') as f:
        f.write(str(child.returncode if child.returncode >= 0 else 128 - child.returncode))
    os.replace(os.path.join(_jobs_dir(), f'{jid}.rc.tmp'), os.path.join(_jobs_dir(), f'{jid}.rc'))
    return 0


# ------------------------------------------------------------------ ssh

_SSH_VALUE = set('BbcDEeFIiJLlmOoPpQRSWw')


def ssh(args: list[str]) -> int:
    d = os.environ['FAKE_SSH']
    i, control = 0, False
    while i < len(args) and args[i].startswith('-'):
        flag = args[i][1:2]
        if flag == 'O':
            control = True
        i += 2 if flag in _SSH_VALUE and len(args[i]) == 2 else 1
    host, rest = args[i], args[i + 1:]
    with open(os.path.join(d, 'calls.log'), 'a') as f:
        f.write(json.dumps([host, ' '.join(rest)]) + '\n')
    h = _load(os.path.join(d, 'hosts.json'), {}).get(host)
    if h is None or h.get('down'):
        print(f'ssh: connect to host {host} port 22: Connection refused', file=sys.stderr)
        return 255
    if control:
        return 0
    if h.get('login'):  # the account owner's own login: a shell, as sshd runs the command
        os.chdir(h['home'])
        os.execve('/bin/sh', ['sh', '-c', ' '.join(rest)], dict(os.environ, HOME=h['home']))
    env = dict(os.environ, SSH_ORIGINAL_COMMAND=' '.join(rest))
    gate = ('import sys; sys.path.insert(0, sys.argv.pop(1)); from ci_runners.slurmgate import main; '
            'sys.exit(main())')
    os.execve(sys.executable, [sys.executable, '-I', '-c', gate, h['release'], h['base']], env)
    return 255


def main(argv: list[str]) -> int:
    name, args = argv[0], argv[1:]
    if name == '--run':
        return runner(args[0], args[1])
    return {'sbatch': sbatch, 'squeue': squeue, 'sacct': sacct, 'scancel': scancel, 'sinfo': sinfo,
            'ssh': ssh}[name](args)


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
