"""The executor's ``slurm`` path, on the driver host (design decision 14.3, 14.4).

A part of the ``slurm`` class runs as one batch job on a Slurm cluster. The executor
(``part.Executor``) checks the class row against the part's targets (``row``) and
starts ``drive`` in the part's scope, so that a stopped runner leaves it running, as a
supervisor would be. ``drive``:

1. takes one of the site's ``max_parts`` slots of this host;
2. for each target in the part's order, tries its eligible accounts, least spent this
   month relative to their weight first, round-robin on ties (14.8), each priced
   against the site's and the account's caps from the accounts' ledgers (14.7);
3. stages the workspace and ``RUNNER_TEMP`` through the account's gate, and submits;
4. while the job pends past the target's bound, cancels it and tries the next account,
   then the next target, within the part's ``queue-timeout-minutes``;
5. watches the job, relays its log, cancels it on TERM (``stopped``);
6. at its end fetches ``RUNNER_TEMP`` and the part's directory back and writes
   ``exit.json`` by 14.4's table (``end_evidence``) and ``slurm.json``, the record's
   ``slurm`` block, with the job's cost.

A login host unreachable for ``LOST_AFTER`` s mid-part leaves no ``exit.json``: the part
is ``lost``, its record keeps the job, and the reaper (``reap``) cancels the job once its
driver is gone. Every call to the cluster is one ssh call to the account's gate
(``ci_runners.slurmgate``), over one master connection; the account's key is
``etc/slurm/keys/<alias>``.
"""
from __future__ import annotations

import datetime
import fcntl
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence

from ci_runners import slots, slurmsite

CLASS = 'slurm'
POLL = 10.0  # s between status calls (14.3)
LOST_AFTER = 300  # s the login host may be unreachable mid-part (14.4)
CANCEL_WAIT = 90  # s a cancelled job has to end
LEDGER_TTL = 600  # s a ledger is cached (14.7)
DRIVER_MEMORY = '1G'  # the driver's scope
LOG_CHUNK = 1 << 20  # slurmgate.LOG_CHUNK
#: the job's environment from the runner's: what a command may read of its job
JOB_VARS = ('GITHUB_REPOSITORY', 'GITHUB_SHA', 'GITHUB_REF', 'GITHUB_REF_NAME', 'GITHUB_HEAD_REF', 'GITHUB_BASE_REF',
            'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT', 'GITHUB_RUN_NUMBER', 'GITHUB_JOB', 'GITHUB_EVENT_NAME',
            'GITHUB_ACTOR', 'GITHUB_SERVER_URL', 'GITHUB_WORKFLOW', 'GITHUB_ACTIONS', 'CI', 'RUNNER_OS',
            'RUNNER_ARCH', 'RUNNER_NAME')
#: what is not staged from, nor fetched into, RUNNER_TEMP: the runner's own files and our artifact
TEMP_EXCLUDES = ('/_runner_file_commands/', '/ci-runners/', '/_github_workflow/', '/_github_home/')
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
    pends: a job that may still run is priced at its whole time limit. A card of a
    partition no GPU target names is priced at the dearest card. Pure."""
    state = job.get('State', '').split(' ')[0]
    live = state in LIVE
    cards = _tres(job.get('AllocTRES', ''), 'gres/gpu')
    cpus = _tres(job.get('AllocTRES', ''), 'cpu')
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


class Gate:
    """An account's gate over ssh: one master connection per driver, batch mode, the
    account's key only. ``ssh`` and ``rsync`` are the tools' paths."""

    def __init__(self, etc: str, data: str, site: slurmsite.Site, alias: str, *, ssh: str = '/usr/bin/ssh',
                 rsync: str = '/usr/bin/rsync', run: Callable = subprocess.run) -> None:
        self.site, self.alias, self.ssh, self.rsync, self.run = site, alias, ssh, rsync, run
        self.key = os.path.join(etc, 'slurm', 'keys', alias)
        self.cm = os.path.join(state_dir(data, site.name), 'cm')

    def ssh_argv(self) -> list[str]:
        os.makedirs(self.cm, mode=0o700, exist_ok=True)
        return [self.ssh, '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', '-i', self.key, '-o', 'ControlMaster=auto',
                '-o', f'ControlPath={self.cm}/%C', '-o', 'ControlPersist=60', '-o', 'ConnectTimeout=20',
                '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=4']

    @property
    def host(self) -> str:
        return str(self.site.accounts[self.alias].a['ssh'])

    def call(self, verb: str, *args: str, data: bytes = b'', timeout: float = 120) -> bytes:
        try:
            r = self.run([*self.ssh_argv(), self.host, 'ci-slurm-gate', verb, *args], input=data, capture_output=True,
                         timeout=timeout)
        except subprocess.TimeoutExpired:
            raise Unreachable(f'{self.alias}: the gate did not answer in {timeout:g} s') from None
        if r.returncode == 255:
            raise Unreachable(f'{self.alias}: {(r.stderr or b"").decode(errors="replace").strip()[-200:]}')
        if r.returncode != 0:
            why = (r.stderr or b'').decode(errors='replace').strip().removeprefix('ci-slurm-gate: refused: ')
            raise Refused(why[-300:] or f'exit {r.returncode}')
        return r.stdout or b''

    def json(self, verb: str, *args: str, data: bytes = b'', timeout: float = 120) -> dict:
        try:
            v = json.loads(self.call(verb, *args, data=data, timeout=timeout))
        except ValueError:
            raise Refused(f'{verb}: not JSON') from None
        if not isinstance(v, dict):
            raise Refused(f'{verb}: not an object')
        return v

    def sync(self, verb: str, src: str, dst: str, excludes: Sequence[str] = (), timeout: float = 1800) -> None:
        """``rsync -a --delete`` through the gate's ``stage`` (to the cluster) or ``fetch``
        (back)."""
        argv = [self.rsync, '-a', '--delete', '-e', shlex.join(self.ssh_argv()), f'--rsync-path=ci-slurm-gate {verb}',
                *(f'--exclude={x}' for x in excludes), src, dst]
        try:
            r = self.run(argv, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise Unreachable(f'{self.alias}: {verb} ran past {timeout:g} s') from None
        if r.returncode == 255 or (r.returncode in (12, 35) and b'connection' in (r.stderr or b'').lower()):
            raise Unreachable(f'{self.alias}: {verb}: {(r.stderr or b"").decode(errors="replace").strip()[-200:]}')
        if r.returncode != 0:
            raise Refused(f'{verb}: rsync exit {r.returncode}: {(r.stderr or b"").decode(errors="replace").strip()[-200:]}')


def ledgers(gates: Mapping[str, Gate], cache: str, now: float, month: str) -> dict[str, list[dict]]:
    """Each account's ledger rows of ``month``, cached ``LEDGER_TTL`` s in ``cache``; an
    account whose gate does not answer is left out."""
    os.makedirs(cache, mode=0o700, exist_ok=True)
    out = {}
    for alias, g in gates.items():
        path = os.path.join(cache, f'ledger-{alias}.json')
        c = _read_json(path)
        if c and c.get('month') == month and now - float(c.get('at', 0)) < LEDGER_TTL and isinstance(c.get('jobs'), list):
            out[alias] = c['jobs']
            continue
        try:
            led = g.json('ledger', month)
        except (Unreachable, Refused):
            continue
        jobs = [j for j in led.get('jobs', []) if isinstance(j, dict)]
        _write_json(path, {'month': month, 'at': now, 'jobs': jobs})
        out[alias] = jobs
    return out


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

    def say(self, text: str) -> None:
        self.out.write(f'ci-runners: {text}\n')
        self.out.flush()

    def save(self) -> None:
        _write_json(os.path.join(self.record, 'slurm.json'), self.block)

    def finish(self, ex: dict[str, object]) -> int:
        ex = {'timed_out': False, 'oom_kills': 0, 'signalled': False, **ex, 'end_epoch': self.clock(),
              'boot_end': boot_id()}
        self.save()
        _write_json(os.path.join(self.record, 'exit.json'), ex)
        return 0

    def gate(self, alias: str) -> Gate:
        return Gate(self.plan['etc'], self.plan['data'], self.site, alias, ssh=self.plan['ssh'],
                    rsync=self.plan['rsync'], run=self.run)

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
        cache = state_dir(p['data'], self.site.name)
        rows = ledgers(gates, cache, now, month)
        spent = {a: spend(self.site, jobs, today) for a, jobs in rows.items()}
        why: list[str] = []
        turns = _read_json(os.path.join(cache, 'turns.json')) or {}
        for target in p['targets']:
            for alias in order(self.site, target, {a: m for a, (m, _) in spent.items()}, int(turns.get(target, 0))):
                if alias not in rows:
                    why.append(f'{target}@{alias}: the login host did not answer')
                    continue
                refusal = budget_refusal(self.site, alias, target, p['cards'], p['minutes'], spent)
                if refusal:
                    why.append(refusal)
                    continue
                turns[target] = int(turns.get(target, 0)) + 1
                _write_json(os.path.join(cache, 'turns.json'), turns)
                end = self.attempt(gates[alias], target, deadline)
                if end is not None:
                    return end
                if self.stopping:
                    return self.finish({'rc': None, 'signalled': True})
                why.append(str(self.block['tried'][-1]['why']) if self.block['tried'] else f'{target}@{alias}')
                if self.clock() >= deadline:
                    return self.finish({'rc': None, 'unavailable': f'pending: {"; ".join(why[-3:])}'})
        budget = [w for w in why if w.startswith('budget:')]
        if budget and len(budget) == len(why):
            return self.finish({'rc': None, 'unavailable': budget[0]})
        return self.finish({'rc': None, 'unavailable': ('; '.join(why) or 'no eligible account')[:400]})

    def attempt(self, g: Gate, target: str, deadline: float) -> int | None:
        """One account and target: stage, submit, wait out the pending bound, watch. The
        drive's end, or None to try the next."""
        p = self.plan
        tried: dict[str, object] = {'account': g.alias, 'target': target}
        self.block['tried'].append(tried)  # type: ignore[union-attr]
        self.block.update(account=g.alias, target=target, partition=self.site.targets[target].t['partition'],
                          cards=p['cards'], cpus=self.site.targets[target].cpus(p['cards']))
        try:
            g.sync('stage', p['workspace'].rstrip('/') + '/', f'{g.host}:{self.key}/ws/')
            if p.get('temp'):
                g.sync('stage', p['temp'].rstrip('/') + '/', f'{g.host}:{self.key}/tmp/', TEMP_EXCLUDES)
            req = dict(p['request'], target=target)
            job = g.json('submit', self.key, data=json.dumps(req).encode())['job']
        except (Unreachable, Refused) as e:
            tried['why'] = f'{target}@{g.alias}: {e}'
            self.say(str(tried['why']))
            return None
        tried['job'] = job
        self.block.update(job=job, state='PENDING')
        self.save()
        self.say(f'{self.site.name}:{target} job {job} ({g.alias})')
        submitted, bound = self.clock(), self.site.pending_minutes(target) * 60
        offset, lost_since, st = 0, None, {}
        while True:
            if self.stopping:
                return self.cancel(g, job)
            try:
                st = g.json('status', self.key, timeout=60)
                lost_since = None
            except Unreachable as e:
                lost_since = lost_since or self.clock()
                if self.clock() - lost_since >= LOST_AFTER:
                    self.block['lost'] = f'the login host did not answer for {LOST_AFTER} s: {e}'
                    self.save()
                    return 0  # no exit.json: lost; the reaper cancels the job once this driver is gone
                self.sleep(p['poll'])
                continue
            except Refused as e:
                st = {'state': 'UNKNOWN', 'why': str(e)}
            state = str(st.get('state', 'UNKNOWN'))
            if state != self.block.get('state'):
                self.block.update(state=state, node=st.get('node', ''))
                if state == 'RUNNING' and 'run_epoch' not in self.block:
                    self.block['run_epoch'] = self.clock()
                self.save()
            if state == 'PENDING':
                now = self.clock()
                if now - submitted >= bound or now >= deadline:
                    reason = st.get('reason') or 'pending'
                    tried['why'] = f'{target}@{g.alias} {reason}'
                    self.say(f'job {job} pends past its bound ({reason}): cancelled')
                    self.cancel(g, job, end=False)
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
            self.out.write(chunk.decode('utf-8', 'replace'))
            self.out.flush()
            with open(os.path.join(self.record, 'job.log'), 'ab') as f:
                f.write(chunk)
            offset += len(chunk)
            if len(chunk) < LOG_CHUNK:
                return offset

    def cancel(self, g: Gate, job: int, end: bool = True) -> int | None:
        """``scancel`` the job and wait for it to go (``CANCEL_WAIT`` s); ``end``: end the
        drive ``stopped``."""
        t0 = self.clock()
        while self.clock() - t0 < CANCEL_WAIT:
            try:
                g.json('cancel', self.key, timeout=60)
                st = g.json('status', self.key, timeout=60)
                if st.get('state') not in LIVE:
                    self.block['state'] = st.get('state')
                    break
            except (Unreachable, Refused):
                pass
            self.sleep(min(self.plan['poll'], 2))
        if end:
            return self.finish({'rc': None, 'signalled': True})
        return None

    def ended(self, g: Gate, st: Mapping[str, object], target: str) -> int:
        p = self.plan
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
        ex = end_evidence(st, stopping=self.stopping)
        if ex.get('p2p'):
            hold_one_card(p['data'], self.site.name, target, str(ex['p2p']))
        self.say(f'{self.site.name}:{target} job {st.get("job")} {st.get("state")} on {st.get("node")}, '
                 f'{elapsed} s, cost {cost:.3f}')
        return self.finish(ex)


def drive(record: str) -> int:
    """The driver of one part, run in the part's scope by the executor."""
    d = Driver(record)
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, d.on_signal)
    try:
        return d.drive()
    except slurmsite.SiteError as e:
        return d.finish({'rc': None, 'unavailable': f'site config: {e}'})


# ------------------------------------------------------------------ the reaper's orphan cancel


def reap(data: str, etc: str, *, run: Callable = subprocess.run, ssh: str = '/usr/bin/ssh',
         rsync: str = '/usr/bin/rsync') -> list[str]:
    """Cancel the job of a ``slurm`` part whose driver is gone without an end (a lost
    part, a stopped host; 14.4, T21): its record holds the job and no ``exit.json``.
    Then run each account's gate ``reap``. Returns what it did, a line each."""
    out = []
    root = os.path.join(data, 'ci-runners', 'state', 'parts')
    sites: dict[str, slurmsite.Site] = {}
    for top, dirs, files in os.walk(root):
        if 'slurm.json' not in files or 'exit.json' in files:
            continue
        dirs[:] = []
        block = _read_json(os.path.join(top, 'slurm.json')) or {}
        drv = block.get('driver')
        if block.get('reaped') or type(block.get('job')) is not int or not isinstance(block.get('account'), str):
            continue
        if isinstance(drv, list) and len(drv) == 2 and slots.alive(int(drv[0]), float(drv[1])):
            continue
        plan = _read_json(os.path.join(top, 'slurm-plan.json')) or {}
        try:
            site = sites.get(plan.get('site_file', '')) or slurmsite.load_file(plan['site_file'])
            sites[plan['site_file']] = site
            Gate(etc, data, site, block['account'], ssh=ssh, rsync=rsync, run=run).json('cancel', plan['key'])
        except (KeyError, slurmsite.SiteError, Unreachable, Refused) as e:
            out.append(f'slurm reap: {os.path.relpath(top, root)}: job {block["job"]} not cancelled: {e}')
            continue
        block['reaped'] = time.time()
        _write_json(os.path.join(top, 'slurm.json'), block)
        out.append(f'slurm reap: {os.path.relpath(top, root)}: job {block["job"]} cancelled, its driver gone')
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
