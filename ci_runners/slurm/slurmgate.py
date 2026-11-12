"""The gate on a Slurm cluster (design decision 14.5) and the batch job's launcher (14.3).

The gate is the only command a cluster key runs: the key's ``authorized_keys`` line is
``restrict`` and forces ``<root>/ci-runners/releases/hook/bin/ci-slurm-gate``, which runs
``main`` with the release and ``<base>`` = ``<root>/ci-runners``. The verb comes from
``SSH_ORIGINAL_COMMAND`` (``ci-slurm-gate <verb> [args]``); a request comes on stdin. The
gate validates it against the account's cluster-side copy (``<base>/etc/account.json``,
``slurmsite.account_entry``), does one thing and exits: it keeps nothing running.

Verbs (``KEY`` is a part's ``<owner>/<repo>/<run>-<run attempt>/<id>-<attempt>``, its
directory ``<base>/work/KEY``):

- ``stage --server ... . KEY/ws/`` (or ``KEY/tmp/``): ``rsync --server`` into ``work/``;
- ``fetch --server --sender ... . KEY/tmp/`` (or ``KEY/part/``): ``rsync`` out of ``work/``;
- ``submit KEY``: a JSON request (``REQUEST``) on stdin; renders the batch script
  (``render``) and ``sbatch``-es it; prints ``{"job": N}``. A key may submit again once
  its job is gone (the next target of a part);
- ``status KEY``: the job's state, its node, its end and the cluster supervisor's exit;
- ``log KEY OFFSET``: the job's log from ``OFFSET``, at most ``LOG_CHUNK`` bytes;
- ``cancel KEY``: ``scancel`` the part's job;
- ``ledger [YYYY-MM]``: the account's jobs of the month, from ``sacct``;
- ``reap``: cancel a part's job past its deadline; remove part directories after
  ``WORK_DAYS`` days;
- ``check``: the site's facts this account sees (RUN-9e reads them);
- ``capacity``: the three read-only queries of ``pool status`` (decision 15.1, item 8) for
  the partitions of the account's targets: ``sinfo``'s nodes, the account's associations
  (``sacctmgr``) and its running jobs (``squeue``), their output one ``===`` line apart.

The batch script (``render``) is the release's only one: the account, the target's
partition and nodes, the site's GPU request syntax, ``--time`` = the part's timeout +
``TIME_MARGIN`` min, a warning ``TERM`` ``WARN_SECONDS`` s before it, a job name
``ci-<repo>-<id>-<attempt>``, the run's URL as its comment, ``--export=NONE``; every value
comes from the account's copy or is checked against a narrow pattern, and none is a flag
of the request. Its body runs ``job``: the cluster supervisor (``part.supervise``) with
the inner timeout, the memory cap by ``RssAnon`` (the nodes enforce none, 11.11) and the
P2P self-test before a command of two or more cards (12.6), in an explicit environment.

Python 3.11 or later (``slurmsite.MIN_PYTHON``); stdlib only.
"""
from __future__ import annotations

import datetime
import json
import math
import os
import pwd
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence

#: the end of a command's error text the gate passes on: far more than the driver keeps
#: (``slurmpart.Gate``: 300), so that the driver redacts a name whole before it cuts
ERR_KEEP = 4000
VERBS = ('stage', 'fetch', 'submit', 'status', 'log', 'cancel', 'ledger', 'nodes', 'reap', 'check', 'capacity')
TIME_MARGIN = 5  # min of a job's --time over the part's timeout (14.3; slurmsite.TIME_MARGIN)
WARN_SECONDS = 120  # the warning TERM before the time limit
LOG_CHUNK = 1 << 20
WORK_DAYS = 3  # a part directory's life on the cluster
MAX_REQUEST = 256 << 10
MAX_QUEUE_MINUTES = 24 * 60
#: a part's key below ``work/``
KEY = re.compile(r'(?P<owner>[a-z0-9][a-z0-9_.-]{0,38})/(?P<repo>[a-z0-9][a-z0-9_.-]{0,99})/[0-9]{1,20}-[0-9]{1,4}/'
                 r'(?P<id>[A-Za-z0-9][A-Za-z0-9_.-]{0,63})-(?P<n>[1-9][0-9]{0,2})')
#: the subdirectories ``stage`` writes and ``fetch`` reads
STAGE_DIRS = ('ws', 'tmp')
FETCH_DIRS = ('tmp', 'part')
#: rsync's long options a stage or fetch may pass; its short ones are one ``-[A-Za-z.]`` word
RSYNC_LONG = frozenset({'--server', '--sender', '--delete', '--delete-excluded', '--numeric-ids', '--safe-links'})
#: rsync's short options, before the ``e`` of the capability string: none that follows a
#: link (``-L``, ``-k``, ``-K``), keeps a backup or takes an argument
_RSYNC_SHORT = re.compile(r'-[vlogDtprzciuxSHAXWCEOJmn]{1,30}(?:e[.A-Za-z]{0,30})?\Z')
PROJECT = re.compile(r'[a-z0-9][a-z0-9-]{0,39}\Z')
NAME = re.compile(r'[a-z][a-z0-9-]{0,30}\Z')
ROLE = re.compile(r'[a-z0-9][a-z0-9-]{0,30}\Z')
SIZE = re.compile(r'([1-9][0-9]{0,9})([KMGT]?)\Z')
VAR = re.compile(r'[A-Z_][A-Z0-9_]{0,63}\Z')
RUN_URL = re.compile(r'https://github\.com/[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}/actions/runs/[0-9]{1,20}'
                     r'(?:/attempts/[0-9]{1,4})?\Z')
#: the variables the job sets itself: a request's ``variables`` may not name them
RESERVED_VARS = frozenset({'PATH', 'HOME', 'USER', 'LOGNAME', 'SHELL', 'TMPDIR', 'LANG', 'CONDA_PREFIX', 'GITHUB_WORKSPACE',
                           'CUDA_VISIBLE_DEVICES', 'CUDA_DEVICE_ORDER', 'RUNNER_TEMP', 'PYTHONPATH', 'PYTHONHOME',
                           'BASH_ENV', 'ENV', 'IFS'})
RESERVED_PREFIXES = ('CI_', 'SLURM_', 'LD_', 'GITHUB_OUTPUT', 'GITHUB_ENV', 'GITHUB_PATH', 'GITHUB_STATE',
                     'GITHUB_STEP_SUMMARY')
#: the variables of the node's environment the command keeps: Slurm's and the cards'
KEEP_VARS = ('CUDA_VISIBLE_DEVICES', 'GPU_DEVICE_ORDINAL', 'ROCR_VISIBLE_DEVICES')
#: a request's keys: (check, required)
REQUEST = {
    'project': (lambda v: isinstance(v, str) and bool(PROJECT.fullmatch(v)), True),
    'target': (lambda v: isinstance(v, str) and bool(NAME.fullmatch(v)), True),
    'cards': (lambda v: type(v) is int and 0 <= v <= 64, True),
    'minutes': (lambda v: type(v) in (int, float) and 0 < v <= 7 * 24 * 60, True),
    'queue_minutes': (lambda v: type(v) in (int, float) and 0 <= v <= MAX_QUEUE_MINUTES, True),
    'memory': (lambda v: isinstance(v, str) and bool(SIZE.fullmatch(v)), True),
    'command': (lambda v: isinstance(v, str) and 0 < len(v) <= 64 << 10 and '\0' not in v, True),
    'variables': (lambda v: isinstance(v, dict) and len(v) <= 256 and all(
        isinstance(k, str) and VAR.fullmatch(k) and k not in RESERVED_VARS and not k.startswith(RESERVED_PREFIXES)
        and isinstance(x, str) and len(x) <= 8192 and '\0' not in x for k, x in v.items()), True),
    'run_url': (lambda v: isinstance(v, str) and bool(RUN_URL.fullmatch(v)), True),
    'host': (lambda v: isinstance(v, str) and bool(ROLE.fullmatch(v)), True),
    'resume': (lambda v: type(v) is bool, True),
}
#: the states in which a job may still run or start (``squeue``)
LIVE_STATES = frozenset({'PENDING', 'CONFIGURING', 'RUNNING', 'COMPLETING', 'SUSPENDED', 'REQUEUED', 'RESIZING',
                         'SIGNALING', 'STAGE_OUT', 'STOPPED', 'REQUEUE_HOLD', 'REQUEUE_FED', 'RESV_DEL_HOLD'})
SACCT_FIELDS = ('JobID', 'JobName', 'Partition', 'State', 'ExitCode', 'ElapsedRaw', 'NodeList', 'AllocTRES', 'Start',
                'End', 'TimelimitRaw', 'ReqTRES', 'Submit')


class GateError(Exception):
    """A refused call: the gate prints its reason and exits 2."""


# ------------------------------------------------------------------ small facts


def _size_bytes(m: str) -> int:
    n, unit = SIZE.fullmatch(m).groups()  # type: ignore[union-attr]
    return int(n) << {'': 0, 'K': 10, 'M': 20, 'G': 30, 'T': 40}[unit]


def _read_json(path: str) -> dict | None:
    try:
        with open(path, encoding='utf-8') as f:
            v = json.load(f)
        return v if isinstance(v, dict) else None
    except (OSError, ValueError):
        return None


def _write_json(path: str, obj: object) -> None:
    tmp = f'{path}.{os.getpid()}.new'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(obj, f, indent=1, sort_keys=True)
        f.write('\n')
    os.replace(tmp, path)


def load_account(base: str) -> dict:
    a = _read_json(os.path.join(base, 'etc', 'account.json'))
    if a is None or a.get('schema') != 1 or not isinstance(a.get('targets'), dict):
        raise GateError('no account entry: run ci-run slurm apply')
    return a


def part_dir(base: str, key: str, *, create: bool = False) -> str:
    """``<base>/work/KEY``, which must be a key and lie under ``work/`` with no link on
    the way; ``create`` makes it and its parents, mode 700 (a parent another part of
    the run makes at the same time is taken, if it is a directory)."""
    if not KEY.fullmatch(key):
        raise GateError(f'{key!r} is no part key')
    work = os.path.join(base, 'work')
    if create:
        os.makedirs(work, mode=0o700, exist_ok=True)
    path = work
    for name in key.split('/'):
        path = os.path.join(path, name)
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            if not create:
                raise GateError(f'no part {key}') from None
            try:
                os.mkdir(path, 0o700)
            except FileExistsError:  # another part of the run made it meanwhile
                pass
            st = os.lstat(path)  # whatever is there now: a directory, or refused
        if not stat.S_ISDIR(st.st_mode):
            raise GateError(f'{key}: not a directory on the way')
    return path


# ------------------------------------------------------------------ stage and fetch


def rsync_argv(base: str, verb: str, args: Sequence[str]) -> list[str]:
    """The ``rsync --server`` a ``stage`` or ``fetch`` runs, in ``<base>/work``: its
    options from ``RSYNC_LONG`` and short words, ``--sender`` for a fetch only, then
    ``.`` and a part's ``ws/`` or ``tmp/`` (stage) or ``tmp/`` or ``part/`` (fetch)."""
    args = list(args)
    if len(args) < 3 or args[0] != '--server' or args[-2] != '.':
        raise GateError(f'{verb}: not an rsync --server call')
    opts = args[1:-2]
    for o in opts:
        if not (o in RSYNC_LONG or _RSYNC_SHORT.fullmatch(o)) or o == '--server':
            raise GateError(f'{verb}: rsync option {o!r} is not allowed')
    if ('--sender' in opts) != (verb == 'fetch'):
        raise GateError(f'{verb}: {"only" if verb == "fetch" else "no"} --sender')
    key, _, sub = args[-1].rstrip('/').rpartition('/')
    if sub not in (STAGE_DIRS if verb == 'stage' else FETCH_DIRS):
        raise GateError(f'{verb}: {args[-1]!r} is not a part\'s {" or ".join(STAGE_DIRS if verb == "stage" else FETCH_DIRS)}')
    d = part_dir(base, key, create=verb == 'stage')
    target = os.path.join(d, sub)
    if verb == 'stage':
        try:
            os.mkdir(target, 0o700)
        except FileExistsError:
            pass
    if os.path.islink(target) or not os.path.isdir(target):
        raise GateError(f'{verb}: {sub} is not a directory')
    return ['rsync', *args[:-1], f'{key}/{sub}/']


# ------------------------------------------------------------------ submit


def check_request(req: object, account: Mapping[str, object], base: str) -> dict:
    """A request (``REQUEST``) checked against the account's copy: its target one of the
    account's, its cards within the target's ``max_cards`` (none on a CPU target), its
    minutes within ``max_minutes``, its memory within ``mem_per_node``, and the env of its
    project and the target's variant built. Returns the request."""
    if not isinstance(req, dict):
        raise GateError('the request is not a JSON object')
    unknown = set(req) - set(REQUEST)
    if unknown:
        raise GateError(f'request: unknown keys {sorted(unknown)}')
    for k, (ok, required) in REQUEST.items():
        if k not in req:
            if required:
                raise GateError(f'request: {k} is missing')
        elif not ok(req[k]):
            raise GateError(f'request: {k} is malformed')
    targets = account['targets']
    t = targets.get(req['target']) if isinstance(targets, dict) else None
    if t is None:
        raise GateError(f'target {req["target"]} is not one of this account\'s')
    gpu = 'gpu_model' in t
    if gpu and not 1 <= req['cards'] <= int(t['max_cards']):
        raise GateError(f'cards {req["cards"]}: target {req["target"]} takes 1 to {t["max_cards"]}')
    if not gpu and req['cards']:
        raise GateError(f'cards {req["cards"]}: target {req["target"]} has no cards')
    if req['minutes'] > int(t['max_minutes']):
        raise GateError(f'minutes {req["minutes"]:g}: target {req["target"]} takes at most {t["max_minutes"]}')
    if _size_bytes(req['memory']) > _size_bytes(str(t['mem_per_node'])):
        raise GateError(f'memory {req["memory"]}: target {req["target"]} has {t["mem_per_node"]} a node')
    if not os.path.isdir(env_dir(base, req['project'], str(t['variant']))):
        raise GateError(f'no env {req["project"]}-{t["variant"]} here: run ci-run slurm env')
    return req


def env_dir(base: str, project: str, variant: str) -> str:
    return os.path.join(base, 'envs', f'{project}-{variant}')


def render(account: Mapping[str, object], req: Mapping[str, object], key: str, d: str, release: str) -> str:
    """The batch script of a checked request (14.3). Every ``#SBATCH`` value comes from
    the account's copy, the key, or a request value checked against a narrow pattern
    (cards, minutes, memory, the run's URL); the command and the variables stay in
    ``request.json``, which ``job`` reads. Pure."""
    t = account['targets'][req['target']]  # type: ignore[index]
    m = KEY.fullmatch(key)
    cards = int(req['cards'])
    cpus = cards * int(t['cpus_per_card']) if 'gpu_model' in t else int(t['cpus'])
    lines = [f'--job-name=ci-{m["repo"]}-{m["id"]}-{m["n"]}', f'--account={account["slurm_account"]}',
             f'--partition={t["partition"]}']
    if t.get('nodes'):
        lines.append('--nodelist=' + ','.join(t['nodes']))
    if t.get('exclude'):
        lines.append('--exclude=' + ','.join(t['exclude']))
    lines += ['--nodes=1', '--ntasks=1', f'--cpus-per-task={cpus}']
    if cards:
        lines.append(str(account['gpu_request']).replace('{n}', str(cards)))
    lines += [f'--mem={max(1, _size_bytes(str(req["memory"])) >> 20)}M',
              f'--time={math.ceil(float(req["minutes"])) + TIME_MARGIN}',
              f'--signal=B:TERM@{WARN_SECONDS}', f'--comment={req["run_url"]}',
              f'--output={d}/log', f'--chdir={d}', '--export=NONE', *account.get('submit_args', [])]
    py = str(account['login_python'])
    launcher = ('import sys; sys.path.insert(0, sys.argv.pop(1)); '
                'from ci_runners.slurm.slurmgate import job; sys.exit(job(sys.argv[1]))')
    return ('#!/bin/bash\n' + ''.join(f'#SBATCH {x}\n' for x in lines)
            + '# the cluster supervisor (design 14.3): rendered by the gate; the command is in request.json\n'
            + f'exec {shlex.quote(py)} -I -c {shlex.quote(launcher)} {shlex.quote(release)} {shlex.quote(d)}\n')


def sbatch_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """``sbatch``'s environment: the gate's without the variables that override a batch
    script's ``#SBATCH`` lines (``SBATCH_*``, and ``SALLOC_*``, ``SRUN_*``, ``SLURM_*``),
    which a site's login profile may set. Pure."""
    env = os.environ if env is None else env
    return {k: v for k, v in env.items() if not k.startswith(('SBATCH_', 'SALLOC_', 'SRUN_', 'SLURM_'))}


def submit(base: str, release: str, key: str, data: bytes, run: Callable = subprocess.run) -> dict:
    account = load_account(base)
    if len(data) > MAX_REQUEST:
        raise GateError('the request is too long')
    try:
        req = json.loads(data.decode('utf-8'))
    except (UnicodeDecodeError, ValueError):
        raise GateError('the request is not JSON') from None
    check_request(req, account, base)
    d = part_dir(base, key, create=True)
    prev = _read_json(os.path.join(d, 'job.json'))
    if prev is not None:  # a part tries the next target with the same key: only once its job is gone
        r = run(['squeue', '-h', '-j', str(prev.get('job')), '-o', '%T'], capture_output=True, text=True, timeout=60)
        gone = ((r.stdout or '').strip() not in LIVE_STATES if r.returncode == 0
                else 'invalid job id' in (r.stderr or '').lower())
        if not gone or type(prev.get('job')) is not int:
            raise GateError(f'{key} has a job already')
        os.replace(os.path.join(d, 'job.json'), os.path.join(d, f'job-{prev["job"]}.json'))
    for sub in ('part', 'record', 'tmp', 'ws'):
        os.makedirs(os.path.join(d, sub, 'results') if sub == 'part' else os.path.join(d, sub), mode=0o700,
                    exist_ok=True)
    _write_json(os.path.join(d, 'request.json'), req)
    script = os.path.join(d, 'job.sh')
    with open(script, 'w', encoding='utf-8') as f:
        f.write(render(account, req, key, d, release))
    os.chmod(script, 0o700)
    r = run(['sbatch', '--parsable', script], capture_output=True, text=True, timeout=120, cwd=d, env=sbatch_env())
    out = (r.stdout or '').strip().split(';')[0]
    if r.returncode != 0 or not out.isdigit():
        raise GateError(f'sbatch refused it: {(r.stderr or r.stdout or "").strip()[-ERR_KEEP:]}')
    now = time.time()
    job = {'job': int(out), 'submit_epoch': now, 'target': req['target'], 'cards': req['cards'],
           'deadline': now + (float(req['queue_minutes']) + math.ceil(float(req['minutes'])) + TIME_MARGIN) * 60}
    _write_json(os.path.join(d, 'job.json'), job)
    return {'job': job['job']}


# ------------------------------------------------------------------ status, log, cancel


def _job(base: str, key: str) -> tuple[str, int]:
    d = part_dir(base, key)
    j = _read_json(os.path.join(d, 'job.json'))
    if j is None or type(j.get('job')) is not int:
        raise GateError(f'{key} has no job')
    return d, j['job']


def parse_sacct(text: str, fields: Sequence[str] = SACCT_FIELDS) -> list[dict[str, str]]:
    """``sacct -n -P -o <fields>`` lines as dicts. Pure."""
    rows = []
    for line in text.splitlines():
        cols = line.split('|')
        if len(cols) == len(fields):
            rows.append(dict(zip(fields, cols)))
    return rows


def status(base: str, key: str, run: Callable = subprocess.run) -> dict:
    """The part's job: ``squeue`` while it is there, then ``sacct``. ``by`` is ``us`` or
    ``other`` for a cancelled job, by the uid ``sacct`` names; ``exit`` the cluster
    supervisor's ``exit.json``, once written."""
    d, job = _job(base, key)
    out: dict[str, object] = {'job': job, 'state': 'UNKNOWN', 'reason': '', 'node': '', 'elapsed': 0, 'exit_code': '',
                              'by': '', 'start': '', 'end': '', 'submit': ''}
    r = run(['squeue', '-h', '-j', str(job), '-o', '%T|%r|%N|%M'], capture_output=True, text=True, timeout=60)
    line = (r.stdout or '').strip().splitlines()
    if r.returncode == 0 and line and line[0].count('|') == 3:
        st, reason, node, _ = line[0].split('|')
        out.update(state=st, reason=reason if reason not in ('None', '(null)') else '', node=node)
    if out['state'] in ('UNKNOWN', 'COMPLETING') or out['state'] not in LIVE_STATES:
        r = run(['sacct', '-n', '-P', '-X', '-j', str(job), '-o', ','.join(SACCT_FIELDS)], capture_output=True,
                text=True, timeout=60)
        rows = [x for x in parse_sacct(r.stdout or '') if x['JobID'] == str(job)] if r.returncode == 0 else []
        if rows:
            x = rows[0]
            st, _, by = x['State'].partition(' by ')
            out.update(state=st or 'UNKNOWN', exit_code=x['ExitCode'], node=x['NodeList'] if x['NodeList'] not in
                       ('None assigned', '') else out['node'], start=x['Start'], end=x['End'], submit=x['Submit'],
                       elapsed=int(x['ElapsedRaw']) if x['ElapsedRaw'].isdigit() else 0)
            if by.strip():
                out['by'] = 'us' if by.strip() == str(os.getuid()) else 'other'
    out['exit'] = _read_json(os.path.join(d, 'record', 'exit.json'))
    try:
        out['log_size'] = os.path.getsize(os.path.join(d, 'log'))
    except OSError:
        out['log_size'] = 0
    return out


def log(base: str, key: str, offset: str) -> bytes:
    if not offset.isdigit():
        raise GateError('log: the offset is no number')
    d = part_dir(base, key)
    try:
        with open(os.path.join(d, 'log'), 'rb') as f:
            f.seek(int(offset))
            return f.read(LOG_CHUNK)
    except FileNotFoundError:
        return b''


def cancel(base: str, key: str, run: Callable = subprocess.run) -> dict:
    _, job = _job(base, key)
    r = run(['scancel', str(job)], capture_output=True, text=True, timeout=60)
    return {'job': job, 'cancelled': r.returncode == 0}


# ------------------------------------------------------------------ ledger and reap


def ledger(month: str, run: Callable = subprocess.run, now: datetime.datetime | None = None) -> dict:
    """The account's jobs of ``month`` (``YYYY-MM``, default this one) as ``sacct`` sees
    them: every job, ours named ``ci-`` (14.7). The driver prices them."""
    now = now or datetime.datetime.now()
    month = month or now.strftime('%Y-%m')
    if not re.fullmatch(r'20[0-9]{2}-(0[1-9]|1[0-2])', month):
        raise GateError('ledger: the month is not YYYY-MM')
    r = run(['sacct', '-n', '-P', '-X', '-S', f'{month}-01T00:00:00', '-E', 'now', '-o', ','.join(SACCT_FIELDS)],
            capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise GateError(f'sacct failed: {(r.stderr or "").strip()[-ERR_KEEP:]}')
    return {'month': month, 'jobs': parse_sacct(r.stdout or '')}


def nodes(run: Callable = subprocess.run) -> dict:
    """The cluster's node lists, as ``sinfo`` compresses them (``%N``): what the driver's
    redactor replaces, since a pending job's reason may name any of them."""
    r = run(['sinfo', '-h', '-o', '%N'], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise GateError(f'sinfo failed: {(r.stderr or "").strip()[-ERR_KEEP:]}')
    return {'nodes': sorted({x.strip() for x in (r.stdout or '').splitlines() if x.strip()})}


#: ``capacity``'s node query: the columns ``pool.parse_sinfo`` reads
CAPACITY_SINFO = 'NodeHost:|,Partition:|,StateCompact:|,CPUsState:|,Memory:|,AllocMem:|,Gres:|,GresUsed:|'
CAPACITY_TIMEOUT = 20  # s a query may take: the three and ssh's connect (20 s) end inside the caller's 90 s


def capacity(base: str, run: Callable = subprocess.run) -> bytes:
    """``pool status``'s three queries for the partitions of the account's targets (decision
    15.1, item 8): the nodes (``sinfo``), the account's associations there (``sacctmgr``)
    and its running jobs there (``squeue``), each output a ``===`` line apart. Reads only."""
    parts = sorted({str(t.get('partition', '')) for t in load_account(base)['targets'].values()} - {''})
    if not parts:
        raise GateError('capacity: the account has no target with a partition')
    user = pwd.getpwuid(os.getuid()).pw_name
    queries = (['sinfo', '-h', '-N', '-p', ','.join(parts), '-O', CAPACITY_SINFO],
               ['sacctmgr', '-nP', 'show', 'assoc', f'user={user}', 'format=Partition,GrpTRES'],
               ['squeue', '-h', '-u', user, '-t', 'RUNNING', '-p', ','.join(parts), '-o', '%P|%C|%b'])
    outs = []
    for argv in queries:
        r = run(argv, capture_output=True, text=True, timeout=CAPACITY_TIMEOUT)
        if r.returncode != 0:
            raise GateError(f'{argv[0]} failed: {(r.stderr or "").strip()[-ERR_KEEP:]}')
        out = r.stdout or ''
        if argv[0] == 'sacctmgr':  # the account's other partitions are not the caller's business
            keep = {p.lower() for p in parts}
            out = ''.join(x for x in out.splitlines(True) if x.partition('|')[0].strip().lower() in keep)
        outs.append(out if not out or out.endswith('\n') else out + '\n')
    return '===\n'.join(outs).encode()


def reap(base: str, now: float | None = None, run: Callable = subprocess.run) -> dict:
    """Cancel a part's job that is still there past its deadline (``job.json``), and
    remove a part directory nothing changed for ``WORK_DAYS`` days whose job is gone."""
    now = time.time() if now is None else now
    work = os.path.join(base, 'work')
    cancelled, removed = [], 0
    for owner in _dirs(work):
        for repo in _dirs(owner):
            for runs in _dirs(repo):
                for d in _dirs(runs):
                    j = _read_json(os.path.join(d, 'job.json'))
                    live = False
                    if j and type(j.get('job')) is int:
                        r = run(['squeue', '-h', '-j', str(j['job']), '-o', '%T'], capture_output=True, text=True,
                                timeout=60)
                        # gone: listed in a final state, or purged; any other failure counts as live
                        live = ((r.stdout or '').strip() in LIVE_STATES if r.returncode == 0
                                else 'invalid job id' not in (r.stderr or '').lower())
                        if live and now > float(j.get('deadline', 0)):
                            if run(['scancel', str(j['job'])], capture_output=True, timeout=60).returncode == 0:
                                cancelled.append(j['job'])
                    if not live and now - _newest(d) > WORK_DAYS * 86400:
                        shutil.rmtree(d, ignore_errors=True)
                        removed += 1
                for x in (runs, repo, owner):
                    try:
                        os.rmdir(x)
                    except OSError:
                        pass
    return {'cancelled': cancelled, 'removed': removed}


def _dirs(path: str) -> list[str]:
    try:
        return [os.path.join(path, n) for n in sorted(os.listdir(path))
                if os.path.isdir(os.path.join(path, n)) and not os.path.islink(os.path.join(path, n))]
    except OSError:
        return []


def _newest(path: str) -> float:
    t = os.lstat(path).st_mtime
    for top, dirs, files in os.walk(path):
        for n in dirs + files:
            try:
                t = max(t, os.lstat(os.path.join(top, n)).st_mtime)
            except OSError:
                pass
    return t


# ------------------------------------------------------------------ check


def unsafe_dirs(root: str, tree: str = '') -> dict[str, str]:
    """What another user could swap under ``root`` (and ``tree`` in it): ``root`` and
    ``tree`` must be this account's directories (not links: ``link``; ``owner``), that
    its group and others may not write (``writable``); ``tree`` may be ``missing``.
    Every directory above ``root`` up to ``/`` (as written and as resolved) must belong
    to this account or to root (``owner``) and be writable by its group or others only
    with the sticky bit (``writable``): whoever may rename an entry there may swap the
    root. Path -> why; ``{}`` if none."""
    out: dict[str, str] = {}
    me = os.getuid()
    for p in ([root, tree] if tree else [root]):
        try:
            st = os.lstat(p)
        except FileNotFoundError:
            out[p] = 'missing'
            continue
        if stat.S_ISLNK(st.st_mode):
            out[p] = 'link'
        elif not stat.S_ISDIR(st.st_mode) or st.st_uid != me:
            out[p] = 'owner'
        elif st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            out[p] = 'writable'
    above = set()
    for start in (os.path.abspath(root), os.path.realpath(root)):
        p = start
        while os.path.dirname(p) != p:
            p = os.path.dirname(p)
            above.add(p)
    for p in sorted(above):
        try:
            lst, st = os.lstat(p), os.stat(p)
        except OSError:
            out[p] = 'missing'
            continue
        if stat.S_ISLNK(lst.st_mode) and lst.st_uid not in (0, me):  # its owner may point it elsewhere
            out[p] = 'owner'
            continue
        if st.st_uid not in (0, me):
            out[p] = 'owner'
        elif st.st_mode & (stat.S_IWGRP | stat.S_IWOTH) and not st.st_mode & stat.S_ISVTX:
            out[p] = 'writable'
    return out


def check(base: str, release: str, run: Callable = subprocess.run) -> dict:
    """What this account sees of the site (RUN-9e's ``check`` reads it): the Python,
    the Slurm commands and version, what another user could swap under the root
    (``unsafe``, ``unsafe_dirs``), the root's room, and per target its partition's
    state and whether ``sbatch --test-only`` takes the largest request a part may make
    (its ``max_cards`` and ``max_minutes``: the partition access and the association's
    limits on cards, CPUs and time)."""
    def out_of(argv: Sequence[str], **kw) -> tuple[int, str]:
        try:
            r = run(list(argv), capture_output=True, text=True, timeout=60, **kw)
            return r.returncode, (r.stdout or '') + (r.stderr or '')
        except (OSError, subprocess.SubprocessError, GateError) as e:
            return 127, str(e) or type(e).__name__
    facts: dict[str, object] = {'python': list(sys.version_info[:3]),
                                'commands': {c: bool(shutil.which(c)) for c in
                                             ('sbatch', 'squeue', 'sacct', 'scancel', 'sinfo', 'rsync')}}
    rc, text = out_of(['sbatch', '--version'])
    m = re.search(r'(\d+)\.(\d+)\.(\d+)', text)
    facts['slurm'] = [int(x) for x in m.groups()] if rc == 0 and m else None
    # whoever else may write the root, the tree or a directory above may swap the gate, the env or a job's files
    facts['unsafe'] = unsafe_dirs(os.path.dirname(base), base)
    try:
        st = os.statvfs(base)
        facts['free_gib'] = st.f_bavail * st.f_frsize / (1 << 30)
        with tempfile.NamedTemporaryFile(dir=base):
            facts['writable'] = True
    except OSError:
        facts['writable'] = False
    try:
        account = load_account(base)
    except GateError as e:
        facts['account'] = str(e)
        return facts
    targets = {}
    for name, t in sorted(account['targets'].items()):
        rc, text = out_of(['sinfo', '-h', '-p', str(t['partition']), '-o', '%a'])
        up = rc == 0 and 'up' in text.split()
        # the largest request a part may make: --test-only then checks the association's limits too
        req = {'project': 'check', 'target': name, 'cards': int(t['max_cards']) if 'gpu_model' in t else 0,
               'minutes': int(t['max_minutes']),
               'queue_minutes': 0, 'memory': '1G', 'command': 'true', 'variables': {},
               'run_url': 'https://github.com/o/r/actions/runs/1', 'host': 'check', 'resume': False}
        with tempfile.TemporaryDirectory(dir=base) as d:
            script = os.path.join(d, 'job.sh')
            with open(script, 'w', encoding='utf-8') as f:
                f.write(render(account, req, 'o/r/1-1/check-1', d, release))
            rc, text = out_of(['sbatch', '--test-only', script], env=sbatch_env())
        targets[name] = {'partition_up': up, 'test_only': rc == 0, 'test_only_text': text.strip()[-300:]}
    facts['targets'] = targets
    facts['envs'] = [os.path.basename(x) for x in _dirs(os.path.join(base, 'envs'))]
    return facts


# ------------------------------------------------------------------ the batch job


def job(d: str) -> int:
    """The batch job's body (14.3): the part's command under the cluster supervisor
    (``part.supervise``), in the part's workspace, with an explicit environment: the
    node's Slurm and card variables, the project's env and variables, and ours. The
    account's own Python packages (``~/.local``) and pip config are not read; no conda is
    on its ``PATH``, so ``~/.condarc`` has no reader. The command's temporary files go to
    a 700 directory of its own on the node (T20)."""
    from ci_runners.execution import part  # the release's supervisor
    from ci_runners.layout import release_root
    req = _read_json(os.path.join(d, 'request.json'))
    base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(d)))))
    account = load_account(base)
    t = account['targets'][req['target']]
    envd = os.path.realpath(env_dir(base, req['project'], str(t['variant'])))  # a later build's relink: not in this job
    tmp = tempfile.mkdtemp(prefix='ci-')
    try:
        env = {k: v for k, v in os.environ.items() if k.startswith('SLURM_') or k in KEEP_VARS}
        env.update({'HOME': os.environ.get('HOME') or os.path.expanduser('~'), 'LANG': 'C.UTF-8', 'TMPDIR': tmp,
                    'PATH': f'{envd}/bin:/usr/local/bin:/usr/bin:/bin', 'CONDA_PREFIX': envd,
                    'PYTHONNOUSERSITE': '1', 'PIP_CONFIG_FILE': os.devnull})  # the account's ~/.local, pip.conf: not read
        env.update(req['variables'])
        cards = int(req['cards'])
        env.update({'CI_RUNNERS_HOST': req['host'], 'CI_PROJECT': req['project'], 'CI_PART_DIR': f'{d}/part',
                    'GITHUB_WORKSPACE': f'{d}/ws',
                    'CI_RESULTS_DIR': f'{d}/part/results', 'RUNNER_TEMP': f'{d}/tmp', 'CUDA_DEVICE_ORDER': 'PCI_BUS_ID',
                    'CI_CPUS': part.cpu_list(os.sched_getaffinity(0)),  # the job's own cpuset, on the node
                    'CI_RESUME': '1' if req['resume'] else '0', 'CI_SLURM_JOB': os.environ.get('SLURM_JOB_ID', '')})
        if not cards:
            env['CUDA_VISIBLE_DEVICES'] = ''
        os.environ.clear()
        os.environ.update(env)
        os.chdir(os.path.join(d, 'ws'))
        release = release_root()
        return part.supervise(os.path.join(d, 'record'), float(req['minutes']) * 60,
                              part.command_argv(req['command']), _size_bytes(req['memory']),
                              part.p2p_argv(release) if cards >= 2 else [])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------ the forced command


def main(argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None,
         stdin=None, stdout=None, run: Callable = subprocess.run, execvp: Callable = os.execvp) -> int:
    """``main([base])``: the forced command, of the release this module is in. Exit 0
    with the verb's JSON (or bytes) on stdout, or 2 with the refusal on stderr."""
    argv = list(sys.argv[1:] if argv is None else argv)
    env = os.environ if env is None else env
    stdin = sys.stdin.buffer if stdin is None else stdin
    stdout = sys.stdout.buffer if stdout is None else stdout
    from ci_runners.layout import release_root
    release, base = release_root(), argv[0]
    real_run = run

    def run(*a, **kw):  # a Slurm command that hangs or is missing: a refusal
        try:
            return real_run(*a, **kw)
        except (OSError, subprocess.SubprocessError) as e:
            raise GateError(f'{a[0][0] if a and a[0] else "a command"}: {type(e).__name__}') from None
    try:
        words = shlex.split(env.get('SSH_ORIGINAL_COMMAND', ''))
        if len(words) < 2 or words[0] != 'ci-slurm-gate' or words[1] not in VERBS:
            raise GateError(f'usage: ci-slurm-gate {"|".join(VERBS)} ...')
        verb, args = words[1], words[2:]
        if verb in ('stage', 'fetch'):
            rsync = rsync_argv(base, verb, args)
            os.chdir(os.path.join(base, 'work'))
            execvp(rsync[0], rsync)
            return 0
        if verb == 'log':
            if len(args) != 2:
                raise GateError('usage: log KEY OFFSET')
            stdout.write(log(base, args[0], args[1]))
            return 0
        if verb == 'submit' and len(args) == 1:
            res: object = submit(base, release, args[0], stdin.read(MAX_REQUEST + 1), run)
        elif verb == 'status' and len(args) == 1:
            res = status(base, args[0], run)
        elif verb == 'cancel' and len(args) == 1:
            res = cancel(base, args[0], run)
        elif verb == 'ledger' and len(args) <= 1:
            res = ledger(args[0] if args else '', run)
        elif verb == 'nodes' and not args:
            res = nodes(run)
        elif verb == 'reap' and not args:
            res = reap(base, run=run)
        elif verb == 'check' and not args:
            res = check(base, release, run)
        elif verb == 'capacity' and not args:
            stdout.write(capacity(base, run))
            return 0
        else:
            raise GateError(f'{verb}: wrong arguments')
    except (GateError, ValueError) as e:
        sys.stderr.write(f'ci-slurm-gate: refused: {e}\n')
        return 2
    stdout.write((json.dumps(res, sort_keys=True) + '\n').encode())
    return 0


if __name__ == '__main__':
    sys.exit(main())
