"""The executor's ``slurm`` path, on the driver host (design decision 14.3, 14.4).

A part of the ``slurm`` class runs as one batch job on a Slurm cluster. The executor
(``part.Executor``) checks the class row against the part's targets (``row``) and
starts ``drive`` in the part's scope, so that a stopped runner leaves it running, as a
supervisor would be. ``drive``:

1. takes one of the site's ``max_parts`` slots of this host;
2. for each target in the part's order, tries its eligible accounts, least spent this
   month relative to their weight first, round-robin on ties (14.8), each priced
   against the site's and the account's caps from the accounts' ledgers (14.7);
3. stages the workspace and ``RUNNER_TEMP`` through the account's gate, without the
   checkout's credentials (``git_config``, ``TEMP_EXCLUDES``), and submits;
4. while the job pends past the target's bound, cancels it and tries the next account,
   then the next target, within the part's ``queue-timeout-minutes``;
5. watches the job, relays its log, cancels it on TERM (``stopped``);
6. at its end fetches ``RUNNER_TEMP`` and the part's directory back and writes
   ``exit.json`` by 14.4's table (``end_evidence``) and ``slurm.json``, the record's
   ``slurm`` block, with the job's cost.

A login host unreachable for ``LOST_AFTER`` s mid-part leaves no ``exit.json``: the part
is ``lost``. Every job the driver did not see end (that one, a cancel the login host did
not take, a submit cut short) stays in the block's ``open``, and the reaper (``reap``)
cancels it once the driver is gone. A pending job's worst case counts in the budget from
its submission: the account's cached ledger is then read again. Every call to the cluster is one ssh call to the account's gate
(``ci_runners.slurm.slurmgate``), over one master connection; the account's key is
``etc/slurm/keys/<alias>``.
"""
from __future__ import annotations

import codecs
import datetime
import fcntl
import glob
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import traceback
from collections.abc import Callable, Mapping, Sequence

from ci_runners.resources import slots
from ci_runners.slurm import slurmsite

CLASS = 'slurm'
POLL = 10.0  # s between status calls (14.3)
LOST_AFTER = 300  # s the login host may be unreachable mid-part (14.4)
CANCEL_WAIT = 90  # s a cancelled job has to end
LEDGER_TTL = 600  # s a ledger is cached (14.7)
NODES_TTL = 3600  # s the cluster's node lists are cached (the redactor's)
DRIVER_MEMORY = '1G'  # the driver's scope
LOG_CHUNK = 1 << 20  # slurmgate.LOG_CHUNK
#: a job log line longer than this goes out redacted up to ``LOG_KEEP`` before its end,
#: never through a value; ``LOG_KEEP`` is longer than any value the redactor replaces
LOG_LINE_MAX = 1 << 16
LOG_KEEP = 512
#: the job's environment from the runner's: what a command may read of its job
JOB_VARS = ('GITHUB_REPOSITORY', 'GITHUB_SHA', 'GITHUB_REF', 'GITHUB_REF_NAME', 'GITHUB_HEAD_REF', 'GITHUB_BASE_REF',
            'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT', 'GITHUB_RUN_NUMBER', 'GITHUB_JOB', 'GITHUB_EVENT_NAME',
            'GITHUB_ACTOR', 'GITHUB_SERVER_URL', 'GITHUB_WORKFLOW', 'GITHUB_ACTIONS', 'CI', 'RUNNER_OS',
            'RUNNER_ARCH', 'RUNNER_NAME')
#: what is not staged from, nor fetched into, RUNNER_TEMP: the runner's own files and our artifact
TEMP_EXCLUDES = ('/_runner_file_commands/', '/ci-runners/', '/_github_workflow/', '/_github_home/',
                 '/git-credentials-*')
#: the checkout's git config: staged without its credentials (``git_config``)
GIT_CONFIG = '/.git/config'
LIVE = frozenset({'PENDING', 'CONFIGURING', 'RUNNING', 'COMPLETING', 'SUSPENDED', 'REQUEUED', 'RESIZING',
                  'SIGNALING', 'STAGE_OUT', 'STOPPED', 'REQUEUE_HOLD', 'REQUEUE_FED', 'RESV_DEL_HOLD', 'UNKNOWN'})
NODE_FAIL = frozenset({'NODE_FAIL', 'BOOT_FAIL'})


class Unreachable(Exception):
    """ssh to the login host failed."""


class Refused(Exception):
    """The gate refused a call; its reason."""


# ------------------------------------------------------------------ the site and its targets


def site_path(etc: str, target: str) -> tuple[str, str]:
    """The site config and the target a ``target`` input names: ``<site>:<target>``, or a
    bare target where ``etc/slurm`` holds one site. ``('', why)`` if none. Pure but for
    the listing."""
    files = slurmsite.site_files(etc)
    site, _, name = target.rpartition(':')
    if site:
        return (files[site], name) if site in files else ('', f'no site {site}')
    if len(files) != 1:
        return '', f'{len(files)} site configs here: name the target <site>:{name}'
    return next(iter(files.values())), name


def state_dir(data: str, site: str) -> str:
    return os.path.join(data, 'ci-runners', 'state', 'slurm', site)


def max_cards(data: str, site: slurmsite.Site, target: str) -> int:
    """A target's ``max_cards``, or 1 while a failed P2P self-test holds it there (14.4)
    until a canary passes (``targets.json``)."""
    held = _read_json(os.path.join(state_dir(data, site.name), 'targets.json')) or {}
    t = held.get(target)
    n = site.targets[target].max_cards
    return min(n, int(t['max_cards'])) if isinstance(t, dict) and type(t.get('max_cards')) is int else n


def hold_one_card(data: str, site: str, target: str, why: str) -> None:
    path = os.path.join(state_dir(data, site), 'targets.json')
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    held = _read_json(path) or {}
    held[target] = {'max_cards': 1, 'why': why, 'since': time.time()}
    _write_json(path, held)


def row(data: str, site: slurmsite.Site, targets: Sequence[str], *, project: str, gpus: int, minutes: float,
        lock: str) -> tuple[list[str], str]:
    """The ``slurm`` class row (design §4.8, 14.2, 14.10) per target: known, enabled for
    the project, ``gpus`` within its ``max_cards`` (none on a CPU target), the timeout
    within its ``max_minutes``, ``lock`` ``none``. Returns the targets that pass, in
    order, and why the others do not."""
    if lock != 'none':
        return [], f'lock {lock}: the slurm runners take lock none (design §4.8)'
    ok, why = [], []
    for t in targets:
        tg = site.targets.get(t)
        if tg is None:
            why.append(f'{t}: no target of site {site.name}')
        elif not tg.enabled_for(project):
            why.append(f'{t}: not enabled for {project} (14.10)')
        elif tg.gpu and not 1 <= gpus <= max_cards(data, site, t):
            why.append(f'{t}: gpus {gpus}, it takes 1 to {max_cards(data, site, t)}')
        elif not tg.gpu and gpus:
            why.append(f'{t}: gpus {gpus}, a CPU target')
        elif minutes > tg.max_minutes:
            why.append(f'{t}: timeout-minutes {minutes:g} above its {tg.max_minutes}')
        else:
            ok.append(t)
    return ok, '; '.join(why)


# ------------------------------------------------------------------ the ledger and the choice


def _tres(alloc: str, key: str) -> int:
    n = 0
    for m in re.finditer(rf'(?:^|,){key}(?::[^=,]*)?=([0-9]+)', alloc):
        n = max(n, int(m.group(1)))
    return n


def row_cost(site: slurmsite.Site, job: Mapping[str, str]) -> tuple[float, bool]:
    """A ``sacct`` row's cost by the site's prices (14.7), and whether it still runs or
    pends: a job that may still run is priced at its whole time limit, a pending one by
    what it asks for (``ReqTRES``). A card of a partition no GPU target names is priced
    at the dearest card. Pure."""
    state = job.get('State', '').split(' ')[0]
    live = state in LIVE
    tres = job.get('AllocTRES') or job.get('ReqTRES', '')  # a pending job has no AllocTRES yet
    cards = _tres(tres, 'gres/gpu')
    cpus = _tres(tres, 'cpu')
    limit = int(job['TimelimitRaw']) * 60 if job.get('TimelimitRaw', '').isdigit() else 0
    secs = max(limit, int(job.get('ElapsedRaw') or 0) if str(job.get('ElapsedRaw', '')).isdigit() else 0) if live \
        else (int(job['ElapsedRaw']) if str(job.get('ElapsedRaw', '')).isdigit() else 0)
    p = site.prices
    models = [t.t['gpu_model'] for t in site.targets.values() if t.gpu and t.t['partition'] == job.get('Partition')]
    card = float(p['card_hour'][models[0]]) if models else max(map(float, p['card_hour'].values()), default=0.0)
    cost = cards * card if cards else 0.0
    if not cards or p['cpu_on_gpu_jobs']:
        cost += cpus * float(p['cpu_core_hour'])
    return cost * secs / 3600, live


def spend(site: slurmsite.Site, jobs: Sequence[Mapping[str, str]], today: str) -> tuple[float, float]:
    """``(this month, today)`` of an account's ledger rows; today's are the jobs that
    started today (``YYYY-MM-DD``) or may still run. Pure."""
    month = day = 0.0
    for j in jobs:
        c, live = row_cost(site, j)
        month += c
        if live or str(j.get('Start', '')).startswith(today):
            day += c
    return month, day


def row_target(site: slurmsite.Site, job: Mapping[str, str]) -> str:
    """The site's target a ``sacct`` row ran on: a target of its partition, a GPU one when
    it holds cards, else a CPU one (the first by name where several fit); ``?`` for none,
    so that no partition name leaves the site config. Pure."""
    cards = _tres(job.get('AllocTRES') or job.get('ReqTRES', ''), 'gres/gpu') > 0
    fit = sorted(n for n, t in site.targets.items() if t.t['partition'] == job.get('Partition'))
    return next((n for n in fit if site.targets[n].gpu == cards), fit[0] if fit else '?')


def daily(site: slurmsite.Site, rows: Mapping[str, Sequence[Mapping[str, str]]]) -> list[dict]:
    """The cost per day (``ci-run slurm cost``; a part's own is in its summary): the accounts'
    ledger rows (``rows``: account -> rows) summed per day, per target and per kind, priced
    as :func:`row_cost` prices them (a job that may still run at its whole time limit). The
    day is the job's start, else (still pending) its submission; the kind ``ci`` for the
    CI's jobs (named ``ci-``), ``other`` for the account's others. Sorted by day, kind,
    target; each ``{'day', 'kind', 'target', 'jobs', 'live', 'seconds', 'cost'}``. Pure."""
    out: dict[tuple[str, str, str], dict] = {}
    for jobs in rows.values():
        for j in jobs:
            cost, live = row_cost(site, j)
            day = next((str(j.get(k, ''))[:10] for k in ('Start', 'Submit')
                        if re.fullmatch(r'20[0-9]{2}-[0-9]{2}-[0-9]{2}', str(j.get(k, ''))[:10])), '?')
            kind = 'ci' if str(j.get('JobName', '')).startswith('ci-') else 'other'
            secs, target = j.get('ElapsedRaw', ''), row_target(site, j)
            r = out.setdefault((day, kind, target), {
                'day': day, 'kind': kind, 'target': target, 'jobs': 0, 'live': 0, 'seconds': 0,
                'cost': 0.0})
            r['jobs'] += 1
            r['live'] += live
            r['seconds'] += int(secs) if str(secs).isdigit() else 0
            r['cost'] += cost
    return [dict(v, cost=round(v['cost'], 4)) for _, v in sorted(out.items())]


def order(site: slurmsite.Site, target: str, spent: Mapping[str, float], turn: int) -> list[str]:
    """The accounts eligible for ``target`` (not retired, the target among theirs, under
    their monthly cap), least spent this month relative to their weight first; ``turn``
    rotates the ones tied (14.8). Pure."""
    ok = [a for a, x in sorted(site.accounts.items()) if not x.retired and target in x.a['targets']
          and spent.get(a, 0.0) < float(x.a['monthly_cap'])]
    key = {a: round(spent.get(a, 0.0) / site.accounts[a].weight, 6) for a in ok}
    out: list[str] = []
    for k in sorted(set(key.values())):
        tied = [a for a in ok if key[a] == k]
        r = turn % len(tied)
        out += tied[r:] + tied[:r]
    return out


def budget_refusal(site: slurmsite.Site, account: str, target: str, cards: int, minutes: float,
                   spent: Mapping[str, tuple[float, float]]) -> str:
    """Why a part's worst case would cross a cap (14.7): the site's daily or monthly, or
    the account's monthly; ``spent`` maps every account to ``(month, today)``. Pure."""
    worst = site.worst_cost(target, cards, minutes)
    month = sum(m for m, _ in spent.values())
    day = sum(d for _, d in spent.values())
    if day + worst > float(site.site['daily_cap']):
        return f'budget: the site\'s daily cap {site.site["daily_cap"]:g} (spent {day:.2f}, this part up to {worst:.2f})'
    if month + worst > float(site.site['monthly_cap']):
        return f'budget: the site\'s monthly cap {site.site["monthly_cap"]:g} (spent {month:.2f}, this part up to {worst:.2f})'
    own = spent.get(account, (0.0, 0.0))[0]
    cap = float(site.accounts[account].a['monthly_cap'])
    if own + worst > cap:
        return f'budget: account {account}\'s monthly cap {cap:g} (spent {own:.2f}, this part up to {worst:.2f})'
    return ''


# ------------------------------------------------------------------ the gate, over ssh


#: a Unix socket path's limit in bytes (``sun_path``, its NUL included), and what ssh adds
#: to the ControlPath while it makes the master (``.`` and 16 random characters)
SUN_PATH, SSH_TMP = 108, 17
#: a transfer a dropped connection ended: one of rsync's exits for it (socket I/O, protocol
#: stream, partial transfer, ssh's own) with a dropped connection's words in its error text,
#: tried again after these waits; a host that does not answer at all is not
SYNC_TRANSIENT, SYNC_DROPPED, SYNC_WAITS = (frozenset({10, 12, 23, 255}),
                                            (b'broken pipe', b'connection reset', b'connection closed'), (2.0, 8.0))


class Gate:
    """An account's gate over ssh: one master connection per process, batch mode, the
    account's key only. ``ssh`` and ``rsync`` are the tools' paths. Each process has a master
    of its own: a master lives in the cgroup of the process that started it (a part's scope,
    the reaper's service) and dies with it, so a master shared across processes broke every
    other part's transfer when its starter ended (issue #103). Its socket is named by a
    short hash of the alias and the process, not ssh's 40-character ``%C``, so that a data
    root of a usual length fits a Unix socket path; one too long is refused (Unreachable).
    A transfer that a dropped connection ends is tried again, twice, after a wait."""

    def __init__(self, etc: str, data: str, site: slurmsite.Site, alias: str, *, ssh: str = '/usr/bin/ssh',
                 rsync: str = '/usr/bin/rsync', run: Callable = subprocess.run,
                 redact: Callable[[str], str] = str, sleep: Callable[[float], None] = time.sleep) -> None:
        self.site, self.alias, self.ssh, self.rsync, self.run, self.sleep = site, alias, ssh, rsync, run, sleep
        self.redact = redact  # the error text it raises, redacted before it is cut
        self.key = os.path.join(etc, 'slurm', 'keys', alias)
        self.cm = os.path.join(state_dir(data, site.name), 'cm')
        self.sock = os.path.join(self.cm, hashlib.sha256(f'{alias}\0{os.getpid()}'.encode()).hexdigest()[:12])

    def ssh_argv(self) -> list[str]:
        if len(os.fsencode(self.sock)) + SSH_TMP >= SUN_PATH:
            raise Unreachable(f'{self.alias}: the data root is too long for the ssh control socket '
                              f'({len(os.fsencode(self.sock))} bytes; at most {SUN_PATH - SSH_TMP - 1})')
        os.makedirs(self.cm, mode=0o700, exist_ok=True)
        return [self.ssh, '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', '-i', self.key, '-o', 'ControlMaster=auto',
                '-o', f'ControlPath={self.sock}', '-o', 'ControlPersist=60', '-o', 'ConnectTimeout=20',
                '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=4']

    @property
    def host(self) -> str:
        return str(self.site.accounts[self.alias].a['ssh'])

    def ssh_facts(self) -> dict[str, str] | None:
        """``ssh -G``'s ``hostname``, ``user`` and ``proxyjump`` of the account's alias (no
        connection), for the redactor; None if ssh cannot say the first two."""
        try:
            r = self.run([self.ssh, '-G', self.host], capture_output=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            return None
        if r.returncode != 0:
            return None
        out = {}
        for line in (r.stdout or b'').decode(errors='replace').splitlines():
            k, _, v = line.partition(' ')
            if k in ('hostname', 'user', 'proxyjump'):
                out[k] = v.strip()
        return out if out.get('hostname') and out.get('user') else None

    def call(self, verb: str, *args: str, data: bytes = b'', timeout: float = 120) -> bytes:
        try:
            r = self.run([*self.ssh_argv(), self.host, 'ci-slurm-gate', verb, *args], input=data, capture_output=True,
                         timeout=timeout)
        except subprocess.TimeoutExpired:
            raise Unreachable(f'{self.alias}: the gate did not answer in {timeout:g} s') from None
        if r.returncode == 255:
            raise Unreachable(f'{self.alias}: {self.redact((r.stderr or b"").decode(errors="replace").strip())[-200:]}')
        if r.returncode != 0:
            why = (r.stderr or b'').decode(errors='replace').strip().removeprefix('ci-slurm-gate: refused: ')
            raise Refused(self.redact(why)[-300:] or f'exit {r.returncode}')
        return r.stdout or b''

    def json(self, verb: str, *args: str, data: bytes = b'', timeout: float = 120) -> dict:
        try:
            v = json.loads(self.call(verb, *args, data=data, timeout=timeout))
        except ValueError:
            raise Refused(f'{verb}: not JSON') from None
        if not isinstance(v, dict):
            raise Refused(f'{verb}: not an object')
        return v

    def sync(self, verb: str, src: str, dst: str, excludes: Sequence[str] = (), timeout: float = 1800,
             delete: bool = True) -> None:
        """``rsync -a --delete`` through the gate's ``stage`` (to the cluster) or ``fetch``
        (back, without a link that points out of what it fetches)."""
        argv = [self.rsync, '-a', *(['--delete'] if delete else []), *(['--safe-links'] if verb == 'fetch' else []), '-e',
                shlex.join(self.ssh_argv()), f'--rsync-path=ci-slurm-gate {verb}', *(f'--exclude={x}' for x in excludes),
                src, dst]
        for wait in (*SYNC_WAITS, None):
            try:
                r = self.run(argv, capture_output=True, timeout=timeout)
            except subprocess.TimeoutExpired:
                raise Unreachable(f'{self.alias}: {verb} ran past {timeout:g} s') from None
            err = (r.stderr or b'').lower()
            dropped = r.returncode in SYNC_TRANSIENT and any(w in err for w in SYNC_DROPPED)
            if not dropped or wait is None:
                break
            self.sleep(wait)
        if r.returncode == 255 or (r.returncode in (12, 35) and b'connection' in (r.stderr or b'').lower()):
            raise Unreachable(f'{self.alias}: {verb}: {self.redact((r.stderr or b"").decode(errors="replace").strip())[-200:]}')
        if r.returncode != 0:
            raise Refused(f'{verb}: rsync exit {r.returncode}: '
                          f'{self.redact((r.stderr or b"").decode(errors="replace").strip())[-200:]}')


def ledgers(gates: Mapping[str, Gate], cache: str, now: float, month: str) -> tuple[dict[str, list[dict]], set[str]]:
    """Each account's ledger rows of ``month``, cached ``LEDGER_TTL`` s in ``cache``, and
    the accounts whose gate did not answer. Such an account's rows are its last cached
    ones of the month, however old, so that its spend still counts against the site's
    caps; one with none is left out of the rows (its spend is unknown)."""
    os.makedirs(cache, mode=0o700, exist_ok=True)
    out, silent = {}, set()
    for alias, g in gates.items():
        path = os.path.join(cache, f'ledger-{alias}.json')
        c = _read_json(path)
        cached = c['jobs'] if c and c.get('month') == month and isinstance(c.get('jobs'), list) else None
        if cached is not None and now - float(c.get('at', 0)) < LEDGER_TTL:  # type: ignore[union-attr]
            out[alias] = cached
            continue
        try:
            led = g.json('ledger', month)
        except (Unreachable, Refused):
            silent.add(alias)
            if cached is not None:
                out[alias] = cached
            continue
        jobs = [j for j in led.get('jobs', []) if isinstance(j, dict)]
        _write_json(path, {'month': month, 'at': now, 'jobs': jobs})
        out[alias] = jobs
    return out, silent


def site_nodes(gates: Mapping[str, Gate], cache: str, now: float) -> list[str] | None:
    """The cluster's node lists (``sinfo``'s ``%N``, through the first gate that answers),
    cached ``NODES_TTL`` s in ``cache``: every node a pending reason may name, for the
    redactor. The last cached ones, however old, if no gate answers; None if none are,
    or if the answer is not lists of nodes (empty, or a list that does not parse): then
    some node could not be redacted."""
    os.makedirs(cache, mode=0o700, exist_ok=True)
    path = os.path.join(cache, 'nodes.json')
    c = _read_json(path) or {}
    cached = _node_lists(c.get('nodes'))
    if cached is not None and now - float(c.get('at', 0)) < NODES_TTL:
        return cached
    for g in gates.values():
        try:
            got = g.json('nodes').get('nodes')
        except (Unreachable, Refused):
            continue
        lists = _node_lists(got)
        if lists is not None:
            _write_json(path, {'at': now, 'nodes': lists})
        return lists  # an answer that does not parse: unknown, not the cache
    return cached


def _node_lists(v: object) -> list[str] | None:
    """``v`` if it is a non-empty list of host lists that each expand, else None. Pure."""
    if not isinstance(v, list) or not v or not all(isinstance(x, str) and slurmsite.hostlist(x) for x in v):
        return None
    return list(v)


def retired_rows(site: slurmsite.Site, cache: str, month: str) -> dict[str, list[dict]]:
    """The retired accounts' last cached ledger rows of ``month``: their spend still
    counts against the site's caps, though their gate is gone (14.9)."""
    out = {}
    for alias, a in site.accounts.items():
        c = _read_json(os.path.join(cache, f'ledger-{alias}.json')) if a.retired else None
        if c and c.get('month') == month and isinstance(c.get('jobs'), list):
            out[alias] = c['jobs']
    return out


def stale_ledger(cache: str, alias: str) -> None:
    """Mark an account's cached ledger stale (a job was just submitted): the next part
    reads ``sacct`` again, and keeps the rows should the gate not answer then."""
    path = os.path.join(cache, f'ledger-{alias}.json')
    c = _read_json(path)
    if c is not None:
        c['at'] = 0
        _write_json(path, c)


def git_config(text: str) -> str:
    """A checkout's ``.git/config`` without credentials: no ``extraheader`` (where
    ``actions/checkout`` keeps the job's token) and no ``includeIf`` section (where newer
    versions point at a credentials file). Pure."""
    out, skip = [], False
    for line in text.splitlines(keepends=True):
        s = line.strip()
        if s.startswith('['):
            skip = s.lower().startswith('[includeif')
        if skip or s.split('=', 1)[0].strip().lower() == 'extraheader':
            continue
        out.append(line)
    return ''.join(out)


def cancel_job(g: Gate, key: str) -> dict | None:
    """The part's job on ``g``'s account, cancelled: its last status once it has ended
    (``{'state': 'GONE'}`` when the account holds no job of the key), else None after
    one ``scancel``. Raises Unreachable, and Refused for any other refusal."""
    try:
        st = g.json('status', key, timeout=60)
    except Refused as e:  # gone only if the account holds no job of the key; a failing squeue is no answer
        if str(e).endswith((f'{key} has no job', f'no part {key}')):
            return {'state': 'GONE'}
        raise
    if str(st.get('state')) not in LIVE:
        return st
    g.json('cancel', key, timeout=60)
    return None


# ------------------------------------------------------------------ the end (14.4)


def end_evidence(st: Mapping[str, object], *, stopping: bool) -> dict[str, object]:
    """The part's ``exit.json`` from the gate's last status of its job, by 14.4's table:
    our cancel is ``stopped``; another user's cancel and a kill by the node ``unavailable``
    (fallback); a node failure ``unavailable`` with a retry here (``retry_here``); the job's
    time limit ``red`` with its reason; otherwise the cluster supervisor's exit (its
    ``rc``, inner timeout, memory cap and P2P self-test). Pure."""
    state, by = str(st.get('state', '')), str(st.get('by', ''))
    ex = st.get('exit') if isinstance(st.get('exit'), dict) else None
    node = str(st.get('node') or '?')
    if stopping or (state == 'CANCELLED' and by == 'us'):
        return {'rc': None, 'signalled': True}
    if state == 'CANCELLED':
        return {'rc': None, 'unavailable': f'cancelled by another user of the site on {node}'}
    if state in NODE_FAIL:
        return {'rc': None, 'unavailable': f'node fail: {state} on {node}', 'retry_here': True}
    if state == 'TIMEOUT':
        return {'rc': None, 'red': 'the job\'s time limit ended it: the cluster supervisor did not end the command'}
    if ex is None:
        return {'rc': None, 'unavailable': f'killed by the node: {state} on {node}, without the supervisor\'s exit'}
    out = {k: ex[k] for k in ('rc', 'timed_out', 'oom_kills', 'p2p') if k in ex}
    if ex.get('signalled') and not ex.get('timed_out') and not ex.get('p2p'):
        return {'rc': None, 'red': 'the job\'s warning signal before its time limit stopped the command'}
    return out


# ------------------------------------------------------------------ the driver (in the part's scope)


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


def boot_id() -> str:
    try:
        with open('/proc/sys/kernel/random/boot_id') as f:
            return f.read().strip()
    except OSError:
        return ''


class Driver:
    """``drive``'s state: the plan the executor wrote (``slurm-plan.json``), the record and
    the ``slurm`` block it keeps up to date."""

    def __init__(self, record: str, *, run: Callable = subprocess.run, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, out=None) -> None:
        self.record, self.run, self.clock, self.sleep = record, run, clock, sleep
        self.out = out or sys.stdout
        self.plan = _read_json(os.path.join(record, 'slurm-plan.json')) or {}
        self.site = slurmsite.load_file(self.plan['site_file'])
        self.stopping = False
        self.block: dict[str, object] = {'site': self.site.name, 'tried': [],
                                         'driver': [os.getpid(), round(slots._start_of(os.getpid()))]}
        self.key = self.plan['key']
        # what the part shows on GitHub (its log, reason and summary) names no inventory
        self.redact = slurmsite.Redactor(self.site)
        self.log_tail = ''
        self.log_decoder = codecs.getincrementaldecoder('utf-8')('replace')

    def say(self, text: str) -> None:
        self.out.write(self.redact(f'ci-runners: {text}') + '\n')
        self.out.flush()

    def write_log(self, text: str, end: bool = False) -> None:
        """The job's log to the job log, redacted by whole lines, so that no name is
        split across two chunks; a line longer than ``LOG_LINE_MAX`` goes up to
        ``LOG_KEEP`` before its end, or before the value that would split there
        (``Redactor.safe_cut``). ``end``: the rest."""
        text = self.log_tail + text
        cut = len(text) if end else text.rfind('\n') + 1
        if not end and cut == 0 and len(text) > LOG_LINE_MAX:
            cut = self.redact.safe_cut(text, len(text) - LOG_KEEP)
        self.log_tail = text[cut:]
        if cut:
            self.out.write(self.redact(text[:cut]))
            self.out.flush()

    def node_seen(self, st: Mapping[str, object]) -> None:
        self.redact.add_nodes(str(st.get('node') or ''))

    def save(self) -> None:
        _write_json(os.path.join(self.record, 'slurm.json'), self.block)

    def finish(self, ex: dict[str, object]) -> int:
        self.write_log(self.log_decoder.decode(b'', final=True), end=True)
        ex = {'timed_out': False, 'oom_kills': 0, 'signalled': False, **ex, 'end_epoch': self.clock(),
              'boot_end': boot_id()}
        for k in ('unavailable', 'red', 'p2p'):  # one line each: they become the part's reason output
            if isinstance(ex.get(k), str):
                ex[k] = ' '.join(re.sub(r'[^\x20-\x7e]', '?', self.redact(ex[k])).split())[:400]
        self.save()
        _write_json(os.path.join(self.record, 'exit.json'), ex)
        return 0

    def gate(self, alias: str) -> Gate:
        return Gate(self.plan['etc'], self.plan['data'], self.site, alias, ssh=self.plan['ssh'],
                    rsync=self.plan['rsync'], run=self.run, redact=self.redact)

    def on_signal(self, _signum, _frame) -> None:
        self.stopping = True

    # -- the slot of this host

    def take_slot(self, deadline: float) -> int | None:
        """One of the site's ``max_parts`` slots of this host, flock-held for the drive."""
        d = os.path.join(state_dir(self.plan['data'], self.site.name), 'parts')
        os.makedirs(d, mode=0o700, exist_ok=True)
        while True:
            for k in range(int(self.site.site['max_parts'])):
                fd = os.open(os.path.join(d, f'{k}.lock'), os.O_WRONLY | os.O_CREAT | os.O_CLOEXEC, 0o600)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return fd
                except OSError:
                    os.close(fd)
            if self.stopping or self.clock() >= deadline:
                return None
            self.sleep(min(self.plan['poll'], 5))

    # -- one part

    def drive(self) -> int:
        deadline = float(self.plan['deadline'])
        slot = self.take_slot(deadline)
        if slot is None:
            return self.finish({'rc': None, 'signalled': True} if self.stopping else
                               {'rc': None, 'unavailable': f'queue timeout: {self.site.site["max_parts"]} cluster parts '
                                                            'run from this host'})
        try:
            return self._drive(deadline)
        finally:
            os.close(slot)

    def _drive(self, deadline: float) -> int:
        p = self.plan
        now = self.clock()
        month, today = datetime.date.fromtimestamp(now).strftime('%Y-%m'), datetime.date.fromtimestamp(now).isoformat()
        gates = {a: self.gate(a) for a, x in self.site.accounts.items() if not x.retired}
        for a, g in gates.items():
            facts = g.ssh_facts()
            if facts is None:  # its login host and user would show unredacted: fail closed
                return self.finish({'rc': None, 'unavailable': f'ssh -G {a}: no host and user to redact'})
            self.redact.add_ssh(facts)
        cache = state_dir(p['data'], self.site.name)
        nodes = site_nodes(gates, cache, now)
        if nodes is None:  # a pending reason may name any node: fail closed
            return self.finish({'rc': None, 'unavailable': 'the cluster\'s node list is unknown (no login host answers)'})
        for n in nodes:
            self.redact.add_nodes(n)
        rows, silent = ledgers(gates, cache, now, month)
        unknown = sorted(set(gates) - set(rows))
        rows.update(retired_rows(self.site, cache, month))
        if unknown:  # the site's caps count every account: fail closed
            return self.finish({'rc': None, 'unavailable': f'budget: the spend of {", ".join(unknown)} is unknown (its '
                                                           'login host does not answer, and no ledger is cached)'})
        spent = {a: spend(self.site, jobs, today) for a, jobs in rows.items()}
        why: list[str] = []
        turns = _read_json(os.path.join(cache, 'turns.json')) or {}
        for target in p['targets']:
            for alias in order(self.site, target, {a: m for a, (m, _) in spent.items()}, int(turns.get(target, 0))):
                if alias in silent:
                    why.append(f'{target}@{alias}: the login host did not answer')
                    continue
                refusal = budget_refusal(self.site, alias, target, p['cards'], p['minutes'], spent)
                if refusal:
                    why.append(refusal)
                    continue
                turns[target] = int(turns.get(target, 0)) + 1
                _write_json(os.path.join(cache, 'turns.json'), turns)
                end = self.attempt(gates[alias], target, deadline, cache)
                if end is not None:
                    return end
                if self.stopping:
                    return self.finish({'rc': None, 'signalled': True})
                why.append(str(self.block['tried'][-1]['why']) if self.block['tried'] else f'{target}@{alias}')
                if self.clock() >= deadline:
                    return self.finish({'rc': None, 'unavailable': '; '.join(why[-3:])})  # finish redacts, then cuts
        budget = [w for w in why if w.startswith('budget:')]
        if budget and len(budget) == len(why):
            return self.finish({'rc': None, 'unavailable': budget[0]})
        return self.finish({'rc': None, 'unavailable': '; '.join(why) or 'no eligible account'})

    def opened(self, alias: str, job: int | None) -> None:
        """A job of the part on ``alias`` that may run: kept in ``open`` until its end is
        seen, so that the reaper cancels it once this driver is gone (T21)."""
        self.block.setdefault('open', {})[alias] = job  # type: ignore[union-attr]
        self.save()

    def closed(self, alias: str) -> None:
        self.block.get('open', {}).pop(alias, None)  # type: ignore[union-attr]
        self.save()

    def stage(self, g: Gate) -> None:
        """The workspace, its git config without credentials (``git_config``), and
        ``RUNNER_TEMP`` but the runner's own files and the checkout's credentials."""
        p = self.plan
        ws = p['workspace'].rstrip('/')
        g.sync('stage', ws + '/', f'{g.host}:{self.key}/ws/', (GIT_CONFIG,))
        try:
            with open(ws + GIT_CONFIG, encoding='utf-8', errors='replace') as f:
                text = git_config(f.read())
        except OSError:
            text = None
        if text is not None:
            d = os.path.join(self.record, 'stage-git')
            os.makedirs(os.path.join(d, '.git'), mode=0o700, exist_ok=True)
            with open(os.path.join(d, '.git', 'config'), 'w', encoding='utf-8') as f:
                f.write(text)
            try:
                g.sync('stage', d + '/', f'{g.host}:{self.key}/ws/', delete=False)
            finally:
                shutil.rmtree(d, ignore_errors=True)
        if p.get('temp'):
            g.sync('stage', p['temp'].rstrip('/') + '/', f'{g.host}:{self.key}/tmp/', TEMP_EXCLUDES)

    def attempt(self, g: Gate, target: str, deadline: float, cache: str) -> int | None:
        """One account and target: stage, submit, wait out the pending bound, watch. The
        drive's end, or None to try the next."""
        p = self.plan
        tried: dict[str, object] = {'account': g.alias, 'target': target}
        self.block['tried'].append(tried)  # type: ignore[union-attr]
        self.block.update(account=g.alias, target=target, partition=self.site.targets[target].t['partition'],
                          cards=p['cards'], cpus=self.site.targets[target].cpus(p['cards']))
        try:
            self.stage(g)
        except (Unreachable, Refused) as e:
            tried['why'] = f'{target}@{g.alias}: {e}'
            self.say(str(tried['why']))
            return None
        held = g.alias in self.block.get('open', {})  # type: ignore[operator]  # an earlier job not seen to end
        if not held:
            self.opened(g.alias, None)  # a submit that fails may still have reached sbatch
        try:
            job = g.json('submit', self.key, data=json.dumps(dict(p['request'], target=target)).encode())['job']
        except (Unreachable, Refused) as e:
            tried['why'] = f'{target}@{g.alias}: {e}'
            self.say(str(tried['why']))
            if self.stopping:
                return self.cancel(g)
            if isinstance(e, Refused) and not held:  # the gate refused it: no job
                self.closed(g.alias)
            return None
        stale_ledger(cache, g.alias)
        tried['job'] = job
        self.block.update(job=job, state='PENDING')
        self.opened(g.alias, job)
        self.say(f'{self.site.name}:{target} job {job} ({g.alias})')
        submitted, bound = self.clock(), self.site.pending_minutes(target) * 60
        offset, lost_since, st = 0, None, {}
        while True:
            if self.stopping:
                return self.cancel(g)
            try:
                st = g.json('status', self.key, timeout=60)
                lost_since = None
            except Unreachable as e:
                lost_since = lost_since or self.clock()
                if self.clock() - lost_since >= float(p.get('lost_after', LOST_AFTER)):
                    self.block['lost'] = f'the login host did not answer for {p.get("lost_after", LOST_AFTER):g} s: {e}'
                    self.save()
                    return 0  # no exit.json: lost; the reaper cancels the job once this driver is gone
                self.sleep(p['poll'])
                continue
            except Refused as e:
                st = {'state': 'UNKNOWN', 'why': str(e)}
            state = str(st.get('state', 'UNKNOWN'))
            self.node_seen(st)
            if state != self.block.get('state'):
                self.block.update(state=state, node=st.get('node', ''))
                if state == 'RUNNING' and 'run_epoch' not in self.block:
                    self.block['run_epoch'] = self.clock()
                self.save()
            if state == 'PENDING':
                now = self.clock()
                if now - submitted >= bound or now >= deadline:
                    reason = st.get('reason') or 'pending'
                    tried['why'] = f'pending: {target}@{g.alias} {reason}'
                    self.say(f'job {job} pends past its bound ({reason}): cancelled')
                    self.cancel(g, end=False)
                    self.block.pop('run_epoch', None)
                    return None
            else:
                offset = self.relay(g, offset)
            if state not in LIVE:
                offset = self.relay(g, offset)
                return self.ended(g, st, target)
            self.sleep(p['poll'])

    def relay(self, g: Gate, offset: int) -> int:
        """The job's log from ``offset`` to the job log and the record's ``job.log``."""
        while True:
            try:
                chunk = g.call('log', self.key, str(offset), timeout=60)
            except (Unreachable, Refused):
                return offset
            if not chunk:
                return offset
            self.write_log(self.log_decoder.decode(chunk))
            with open(os.path.join(self.record, 'job.log'), 'ab') as f:
                f.write(chunk)
            offset += len(chunk)
            if len(chunk) < LOG_CHUNK:
                return offset

    def cancel(self, g: Gate, end: bool = True) -> int | None:
        """``scancel`` the part's job and wait for its end (``cancel_wait`` s); one not seen
        to end stays ``open`` for the reaper. ``end``: end the drive ``stopped``."""
        t0 = self.clock()
        while self.clock() - t0 < float(self.plan.get('cancel_wait', CANCEL_WAIT)):
            try:
                st = cancel_job(g, self.key)
            except (Unreachable, Refused):
                st = None
            if st is not None:
                if st.get('state') != 'GONE':
                    self.block['state'] = st.get('state')
                self.closed(g.alias)
                break
            self.sleep(min(self.plan['poll'], 2))
        if end:
            return self.finish({'rc': None, 'signalled': True})
        return None

    def ended(self, g: Gate, st: Mapping[str, object], target: str) -> int:
        p = self.plan
        self.node_seen(st)
        ran = isinstance(st.get('exit'), dict) or int(st.get('elapsed') or 0) > 0
        if ran:
            for sub, dst, excl in (('tmp', p.get('temp'), TEMP_EXCLUDES), ('part', p['part_dir'], ())):
                if dst:
                    try:
                        g.sync('fetch', f'{g.host}:{self.key}/{sub}/', dst.rstrip('/') + '/', excl)
                    except (Unreachable, Refused) as e:
                        self.say(f'results not fetched: {e}')
        elapsed = int(st.get('elapsed') or 0)
        cost = self.site.hourly(target, p['cards'], self.site.targets[target].cpus(p['cards'])) * elapsed / 3600
        self.block.update(state=st.get('state'), node=st.get('node', ''), elapsed=elapsed, cost=round(cost, 4),
                          exit_code=st.get('exit_code', ''))
        self.block.get('open', {}).pop(g.alias, None)  # type: ignore[union-attr]
        ex = end_evidence(st, stopping=self.stopping)
        if ex.get('p2p'):
            hold_one_card(p['data'], self.site.name, target, str(ex['p2p']))
        self.say(f'{self.site.name}:{target} job {st.get("job")} {st.get("state")} on {st.get("node")}, '
                 f'{elapsed} s, cost {cost:.3f}')
        return self.finish(ex)


def drive(record: str) -> int:
    """The driver of one part, run in the part's scope by the executor. Its stderr is the
    job's log: an error it did not expect ends the part ``unavailable`` with the
    error's type only, and its traceback goes to the record (``driver-error.txt``)."""
    d = None
    try:
        d = Driver(record)
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, d.on_signal)
        return d.drive()
    except slurmsite.SiteError as e:
        why = f'site config: {e}'
    except Exception as e:  # noqa: BLE001  (any error: the part still ends)
        why = f'driver: {type(e).__name__}'
        try:
            with open(os.path.join(record, 'driver-error.txt'), 'a', encoding='utf-8') as f:
                f.write(traceback.format_exc())
        except OSError:
            pass
    try:
        if d is not None:
            return d.finish({'rc': None, 'unavailable': why})
    except Exception:  # noqa: BLE001
        pass
    _write_json(os.path.join(record, 'exit.json'), {'rc': None, 'unavailable': why[:400],  # no driver to finish
                                                     'timed_out': False, 'oom_kills': 0, 'signalled': False,
                                                     'end_epoch': time.time(), 'boot_end': boot_id()})
    return 0


# ------------------------------------------------------------------ the reaper's orphan cancel


def reap(data: str, etc: str, *, run: Callable = subprocess.run, ssh: str = '/usr/bin/ssh',
         rsync: str = '/usr/bin/rsync') -> list[str]:
    """Cancel the jobs of a ``slurm`` part whose driver is gone and did not see them end
    (a lost part, a cancel the login host did not take, a stopped host; 14.4, T21): its
    record's ``open``. A job counts as cancelled once its end is seen. Then run each
    account's gate ``reap``. Returns what it did, a line each."""
    out = []
    root = os.path.join(data, 'ci-runners', 'state', 'parts')
    sites: dict[str, slurmsite.Site] = {}
    for path in sorted(glob.glob(os.path.join(glob.escape(root), *'****', 'slurm.json'))):  # a record's own
        top = os.path.dirname(path)
        block = _read_json(path) or {}
        drv, todo = block.get('driver'), block.get('open')
        if not isinstance(todo, dict) or not todo:
            continue
        if isinstance(drv, list) and len(drv) == 2 and slots.alive(int(drv[0]), float(drv[1])):
            continue
        plan = _read_json(os.path.join(top, 'slurm-plan.json')) or {}
        for alias, job in sorted(todo.items()):
            name = f'slurm reap: {os.path.relpath(top, root)}: job {job if job is not None else "?"} ({alias})'
            try:
                site = sites.get(plan.get('site_file', '')) or slurmsite.load_file(plan['site_file'])
                sites[plan['site_file']] = site
                st = cancel_job(Gate(etc, data, site, alias, ssh=ssh, rsync=rsync, run=run), plan['key'])
            except (KeyError, slurmsite.SiteError, Unreachable, Refused) as e:
                out.append(f'{name} not cancelled: {e}')
                continue
            if st is None:
                out.append(f'{name} cancelled, its driver gone; its end not seen yet')
                continue
            del todo[alias]
            out.append(f'{name} ended ({st.get("state")}), its driver gone')
        _write_json(path, block)
    for name, path in slurmsite.site_files(etc).items():
        try:
            site = slurmsite.load_file(path)
        except slurmsite.SiteError:
            continue
        for alias, a in site.accounts.items():
            if a.retired:
                continue
            try:
                r = Gate(etc, data, site, alias, ssh=ssh, rsync=rsync, run=run).json('reap')
                out.append(f'slurm reap: {name}:{alias}: {len(r.get("cancelled", []))} cancelled past their deadline, '
                           f'{r.get("removed", 0)} part directories removed')
            except (Unreachable, Refused) as e:
                out.append(f'slurm reap: {name}:{alias}: {e}')
    return out
