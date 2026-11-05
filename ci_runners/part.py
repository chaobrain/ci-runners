"""The executor (design §4.3) and ``salvage`` (§4.5): run one part on this host.

``ci-run part`` checks the inputs against the project registry, takes a CI slot and
GPU leases, and runs the command in its own systemd scope
(``ci-part-<runner>-<id>-<n>`` under ``ci-<project>.slice``, with ``MemoryMax``,
``MemorySwapMax=0``, nice 10, ionice best-effort 7 and OOM score +500). In the scope a
small supervisor runs the command, enforces the inner timeout and records the exit in
the part's record, so the record finishes even when the runner itself is stopped
mid-part (the scope is not in the runner's cgroup): ``salvage`` then reads it.

A part's record is ``state/parts/<owner>/<repo>/<run>-<run attempt>/<id>-<attempt>/``:
``record.json`` (inputs, start), ``exit.json`` (the supervisor's) and ``outcome.json``
(the outputs). The step exits 1 only for ``red``; 2 for an input the registry refuses.
"""
from __future__ import annotations

import argparse
import dataclasses
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence

from ci_runners import gpus as gpus_mod
from ci_runners import inventory, outcome, registry, slots
from ci_runners import sampler as sampler_mod

ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}')
SLOT_CLASSES = ('part', 'compile', 'nightly')  # slot=full by default on these (design §4.3)
KILL_GRACE = 30  # s between TERM and KILL at the inner timeout
RELAY_DRAIN = 5  # s the relay may still copy once the command has ended
WATCHDOG_TAIL = 4 << 20  # bytes of the GPU watchdog's log read after a part
#: the pool that marks an h100-style host: one runner per card, labelled h100-gpu<N> (design §5.1)
H100_POOL = 'h100'
#: the step's own files: the command must not write the part's outputs or the job's env
STEP_FILES = frozenset({'GITHUB_OUTPUT', 'GITHUB_STEP_SUMMARY', 'GITHUB_ENV', 'GITHUB_PATH', 'GITHUB_STATE'})
OUTPUTS = ('outcome', 'reason', 'next', 'retry-on', 'host', 'gpus', 'queue-seconds', 'run-seconds', 'verify',
           'record', 'summary', 'attempt', 'resume')


class Refused(Exception):
    """An input the registry or the host refuses: exit 2, no outcome."""


# ------------------------------------------------------------------ small facts


def boot_id() -> str:
    try:
        with open('/proc/sys/kernel/random/boot_id') as f:
            return f.read().strip()
    except OSError:
        return ''


def _write_json(path: str, obj: object) -> None:
    tmp = f'{path}.{os.getpid()}.new'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(obj, f, indent=1, sort_keys=True)
        f.write('\n')
    os.replace(tmp, path)


def _read_json(path: str) -> dict | None:
    try:
        with open(path, encoding='utf-8') as f:
            v = json.load(f)
        return v if isinstance(v, dict) else None
    except (OSError, ValueError):
        return None


def unit_name(runner: str, part_id: str, run: str) -> str:
    """The scope's name, unique per run and attempt; ``job-completed`` stops
    ``ci-part-<runner>-*``. Pure."""
    safe = re.sub(r'[^A-Za-z0-9_.-]', '_', runner) or 'runner'
    return f'ci-part-{safe}-{part_id}-{run}.scope'


def scope_argv(unit: str, project: str, memory: str, inner: Sequence[str]) -> list[str]:
    """``systemd-run`` for the part's scope (design §4.3). Pure."""
    return ['systemd-run', '--user', '--scope', '--quiet', f'--unit={unit}',
            f'--slice=ci-{project}.slice', '-p', f'MemoryMax={memory}', '-p', 'MemorySwapMax=0',
            '-p', 'OOMPolicy=continue', *inner]


def command_argv(command: str, *, compile_part: bool, lock: str, lock_bin: str, host_lock: str,
                 cpus: str = '') -> list[str]:
    """What the supervisor runs: ``taskset`` to the inventory's ``cpus`` where it gives
    them (h100: NUMA node 0, design §5.4), nice and ionice, the host lock if the part
    declares one, then ``bash -c``. Pure."""
    argv = ['taskset', '-c', cpus] if cpus else []
    argv += ['nice', '-n', '0' if compile_part else '10', 'ionice', '-c', '2', '-n', '7']
    if lock != 'none':
        argv += [lock_bin, '--lock', host_lock, f'-{lock}']
    return argv + ['bash', '-c', command]


def part_env(base: Mapping[str, str], *, role: str, project: registry.Project, part_dir: str, results: str,
             cards: Sequence[int], cpus: str, files: Mapping[str, str], slot: str, resume: bool) -> dict[str, str]:
    """The command's environment (design §4.3): the job's without our ``CI_*`` names,
    then the project's variables, then ours, which nothing overrides. Pure."""
    env = {k: v for k, v in base.items() if not k.startswith('CI_') and k not in STEP_FILES}
    env.update(project.variables)
    if project.env:
        env['PATH'] = f'{project.env}/bin:' + env.get('PATH', '/usr/bin:/bin')
        env['CONDA_PREFIX'] = project.env
    env.update({
        'CI_RUNNERS_HOST': role, 'CI_PROJECT': project.name, 'CI_PART_DIR': part_dir, 'CI_RESULTS_DIR': results,
        'CUDA_VISIBLE_DEVICES': ','.join(str(c) for c in cards), 'CUDA_DEVICE_ORDER': 'PCI_BUS_ID',
        'CI_CPUS': cpus, 'CI_HOST_LOCK': files['host_lock'], 'CI_SLOT_LOCK': files['slot_lock'],
        'CI_SLOT_QUEUE': files['slot_queue'], 'CI_SLOT_HELD': slot, 'CI_RESUME': '1' if resume else '0',
    })
    return env


# ------------------------------------------------------------------ the supervisor (in the scope)


def _relay(src: int, dst: int) -> None:
    """Copy ``src`` to ``dst`` until EOF; once ``dst`` fails, keep draining ``src``."""
    alive = True
    while True:
        try:
            chunk = os.read(src, 65536)
        except OSError:
            return
        if not chunk:
            return
        while alive and chunk:
            try:
                chunk = chunk[os.write(dst, chunk):]
            except OSError:  # the job's end closed: EPIPE (the supervisor ignores SIGPIPE)
                alive = False


def supervise(record: str, timeout: float, argv: Sequence[str]) -> int:
    """Run ``argv`` in its own session with OOM score +500, end it at ``timeout``, and
    write ``exit.json``. Runs inside the part's scope, in a session of its own, so the
    job's signals (a cancel, a stopped runner) do not reach it: only a stop of the scope
    does (job-completed, the reaper). It keeps the slot and lease fds it inherited for
    the part's life; the command does not get them.

    The command's output goes through a pipe this supervisor relays to its own: when the
    job's end of it closes (a stopped runner), the relay drops the rest and the command
    runs on, instead of dying of SIGPIPE at its next write."""
    try:
        os.setsid()
    except OSError:
        pass  # already a session leader
    child: subprocess.Popen | None = None
    signalled = []

    def stop(signum, _frame):
        signalled.append(signum)
        if child is not None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except OSError:
                pass
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, stop)

    def adj():
        with open('/proc/self/oom_score_adj', 'w') as f:
            f.write('500')
    timed_out = False
    child = subprocess.Popen(list(argv), start_new_session=True, preexec_fn=adj, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT)
    relay = threading.Thread(target=_relay, args=(child.stdout.fileno(), sys.stdout.fileno()), daemon=True)
    relay.start()
    _write_json(os.path.join(record, 'scope.json'), {'supervisor': os.getpid(), 'child': child.pid})
    deadline = time.monotonic() + timeout
    while True:
        try:
            rc = child.wait(timeout=max(0.0, deadline - time.monotonic()))
            break
        except subprocess.TimeoutExpired:
            if timed_out:  # TERM did not end it within the grace: KILL
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except OSError:
                    pass
                deadline = time.monotonic() + 3600
                continue
            timed_out = True
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except OSError:
                pass
            deadline = time.monotonic() + KILL_GRACE
    relay.join(timeout=RELAY_DRAIN)  # a background process the command left may hold the pipe
    oom_kills = 0
    try:
        with open('/proc/self/cgroup') as f:
            cg = f.read().strip().split('::', 1)[-1]
        with open(f'/sys/fs/cgroup{cg}/memory.events') as f:
            oom_kills = outcome.memory_oom_kills(f.read())
    except (OSError, ValueError):
        pass
    _write_json(os.path.join(record, 'exit.json'), {'rc': rc, 'timed_out': timed_out, 'oom_kills': oom_kills,
                                                     'signalled': bool(signalled), 'end_epoch': time.time(),
                                                     'boot_end': boot_id()})
    return rc if rc >= 0 else 128 - rc


# ------------------------------------------------------------------ leases


class Leases:
    """GPU leases: per card ``max_parts`` lock files under ``state/leases``, flock-held by
    the executor for the part's life. Choosing and taking happen under ``.lock``."""

    def __init__(self, directory: str, max_parts: int = 1) -> None:
        self.dir, self.max_parts = directory, max_parts
        os.makedirs(directory, mode=0o700, exist_ok=True)
        self.fds: list[int] = []

    def _path(self, card: int, k: int) -> str:
        return os.path.join(self.dir, f'card{card}.{k}.lock')

    def leased(self, cards: Sequence[int]) -> dict[int, int]:
        return {c: sum(slots._flocked(self._path(c, k)) for k in range(self.max_parts)) for c in cards}

    def try_take(self, n: int, cards: Sequence[int], free: Mapping[int, int], need_mib: int) -> list[int] | None:
        mutex = os.open(os.path.join(self.dir, '.lock'), os.O_WRONLY | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(mutex, fcntl.LOCK_EX)
            chosen = gpus_mod.choose_lease(n, cards=cards, free=free, need_mib=need_mib,
                                           leased=self.leased(cards), max_parts=self.max_parts)
            if chosen is None:
                return None
            for c in chosen:
                for k in range(self.max_parts):
                    fd = os.open(self._path(c, k), os.O_WRONLY | os.O_CREAT | os.O_CLOEXEC, 0o600)
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except OSError:
                        os.close(fd)
                        continue
                    self.fds.append(fd)
                    break
            return chosen
        finally:
            os.close(mutex)

    def release(self) -> None:
        for fd in self.fds:
            os.close(fd)
        self.fds = []


def smi_free(run: Callable = subprocess.run) -> dict[int, int]:
    try:
        r = run(['nvidia-smi', '--query-gpu=index,memory.total,memory.used', '--format=csv,noheader,nounits'],
                capture_output=True, text=True, timeout=30)
        return gpus_mod.gpu_free_mib(r.stdout) if r.returncode == 0 else {}
    except (OSError, subprocess.SubprocessError):
        return {}


def _smi(argv: Sequence[str], run: Callable) -> str:
    try:
        r = run(['nvidia-smi', *argv], capture_output=True, text=True, timeout=30)
        return r.stdout if r.returncode == 0 else ''
    except (OSError, subprocess.SubprocessError):
        return ''


def smi_status(run: Callable = subprocess.run) -> dict[int, tuple[str, float, float]]:
    return gpus_mod.gpu_status(_smi(['--query-gpu=index,pci.bus_id,temperature.gpu,utilization.gpu',
                                     '--format=csv,noheader,nounits'], run))


def smi_apps(run: Callable = subprocess.run) -> list[tuple[int, str]]:
    return gpus_mod.compute_apps(_smi(['--query-compute-apps=pid,gpu_bus_id', '--format=csv,noheader'], run))


def h100_style(host: Mapping[str, object]) -> bool:
    """Whether a host has a runner per card (an ``h100`` pool): the health gate refuses
    there instead of waiting, a part takes one card (D7), a thermal retry names a cool
    card's ``h100-gpu<M>``, a reboot counts as thermal and the early stop is on. Pure."""
    pools = host.get('pools')
    return isinstance(pools, Mapping) and H100_POOL in pools


def _local(epoch: object) -> str:
    """Host-local ``YYYY-MM-DD HH:MM:SS``, as the GPU watchdog writes times; empty if unknown."""
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(float(epoch))) if epoch else ''


def read_tail(path: str, limit: int = WATCHDOG_TAIL) -> str:
    """The last ``limit`` bytes of a log, or empty."""
    try:
        with open(path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - limit))
            return f.read().decode('utf-8', 'replace')
    except OSError:
        return ''


# ------------------------------------------------------------------ outputs


def write_outputs(out: Mapping[str, object], github_output: str) -> None:
    if not github_output:
        return
    with open(github_output, 'a', encoding='utf-8') as f:
        for k in OUTPUTS:
            v = out.get(k, '')
            v = json.dumps(v) if isinstance(v, (list, dict)) else ('true' if v is True else 'false' if v is False
                                                                     else str(v))
            if '\n' in v:
                raise ValueError(f'output {k} spans lines')
            f.write(f'{k}={v}\n')


def summary_md(out: Mapping[str, object], part_id: str) -> str:
    rows = [('outcome', f'{out["outcome"]} ({out["reason"]})'), ('next', out['next']), ('host', out['host']),
            ('cards', out['gpus'] or 'none'), ('queue', f'{out["queue-seconds"]} s'), ('run', f'{out["run-seconds"]} s'),
            ('attempt', out['attempt'])]
    if out.get('peak'):
        rows.append(('peak temperature', out['peak']))
    return f'### part `{part_id}`\n\n| | |\n|---|---|\n' + ''.join(f'| {k} | {v} |\n' for k, v in rows)


def finish(record: str, rec: Mapping[str, object], ex: Mapping[str, object] | None, *, stopped: bool = False,
           unavailable: str = '', unit_result: str = '', watchdog: str = '', cool_card: int | None = None,
           retry_on: Sequence[str] | None = None) -> dict[str, object]:
    """The outputs of a part from its record and exit (design §4.4). ``watchdog`` is the
    GPU watchdog's log: a kill line counts if it falls in the part's run and names one of
    our cards (``rec['buses']``) and a PID the sampler saw (``rec['pids']``).
    ``cool_card`` is a card of this h100-style host that passes the health gate, for a
    thermal retry; ``retry_on`` overrides the labels of a retry or fallback. Pure but for
    the clock when the exit has no end time."""
    end = float((ex or {}).get('end_epoch') or time.time())
    ev = outcome.Evidence(
        rc=None if ex is None else ex.get('rc'), timed_out=bool(ex and ex.get('timed_out')),
        stopped=stopped or bool(ex and ex.get('signalled') and not ex.get('timed_out')),
        unavailable=unavailable, boot_start=str(rec.get('boot_start', '')),
        boot_end=str((ex or {}).get('boot_end', '')),
        oom_kills=int((ex or {}).get('oom_kills', 0)) + (1 if unit_result == 'oom-kill' else 0),
        start_epoch=float(rec.get('start_epoch', 0)), watchdog=watchdog, start=_local(rec.get('run_epoch')),
        end=_local(end), pids=tuple(int(p) for p in rec.get('pids', ())), buses=tuple(map(str, rec.get('buses', ()))),
        early_stop=bool(rec.get('early_stop')))
    o = outcome.classify(ev)
    attempt = int(rec.get('attempt', 1))
    h = outcome.hint(o.kind, attempt=attempt, retries=int(rec.get('retries', 1)), remote=bool(rec.get('h100')),
                     cool_card=cool_card is not None)
    cls, role = str(rec.get('class', '')), str(rec.get('host', ''))
    own = [str(x) for x in rec.get('pool_labels') or ()]  # the pool's labels name its host already (RUN-5)
    if h.next != 'none' and retry_on is not None:
        retry_on = list(retry_on)
    elif h.next == 'retry' and h.where == 'cool-card':
        retry_on = ['self-hosted', f'{H100_POOL}-gpu{cool_card}']
    elif h.next == 'retry':
        retry_on = ['self-hosted', *own] if own else ['self-hosted', cls, role] if h.where == 'same-host' \
            else ['self-hosted', cls]
    elif h.next == 'fallback':
        retry_on = list(rec.get('fallback') or ['self-hosted', *(own or [cls])])
    else:
        retry_on = []
    peak = rec.get('peak_c') or {}
    run_seconds = int(max(0.0, end - float(rec['run_epoch']))) if rec.get('run_epoch') else 0
    return {'outcome': o.kind, 'reason': o.reason, 'next': h.next, 'retry-on': retry_on, 'host': role,
            'gpus': ','.join(str(c) for c in rec.get('cards', [])), 'queue-seconds': int(rec.get('queue_seconds', 0)),
            'run-seconds': run_seconds, 'verify': False,
            'record': f'{role}:{record_ref(record)}', 'summary': f'{record_ref(record)}/summary.md', 'attempt': attempt,
            'resume': h.resume, 'peak': ', '.join(f'card {c} {t:g} C' for c, t in sorted(peak.items()))
            if isinstance(peak, dict) else ''}


def _publish(record: str, out: dict[str, object], part_id: str, env: Mapping[str, str]) -> None:
    with open(os.path.join(record, 'summary.md'), 'w', encoding='utf-8') as f:
        f.write(summary_md(out, part_id))
    _write_json(os.path.join(record, 'outcome.json'), out)
    write_outputs(out, env.get('GITHUB_OUTPUT', ''))
    if env.get('RUNNER_TEMP'):  # what the part action uploads as ci-part-<id>-<attempt>
        art = os.path.join(env['RUNNER_TEMP'], 'ci-runners', part_id)
        os.makedirs(art, exist_ok=True)
        for n in ('outcome.json', 'summary.md'):
            shutil.copyfile(os.path.join(record, n), os.path.join(art, n))
    if env.get('GITHUB_STEP_SUMMARY'):
        with open(env['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as f:
            f.write(summary_md(out, part_id))


# ------------------------------------------------------------------ the executor


@dataclasses.dataclass
class Inputs:
    project: str
    command: str
    id: str
    timeout_minutes: float
    gpus: int = 0
    gpu_mem_mib: int = 0
    memory: str = ''
    slot: str = ''  # full or none; empty: by the runner class
    lock: str = 'none'
    queue_timeout_minutes: float = 60
    results: str = ''
    resume: bool = False
    attempt_of: str = ''


def parse_args(argv: Sequence[str]) -> Inputs:
    p = argparse.ArgumentParser(prog='ci-run part')
    p.add_argument('--project', required=True)
    p.add_argument('--command', required=True)
    p.add_argument('--id', required=True)
    p.add_argument('--timeout-minutes', type=float, required=True)
    p.add_argument('--gpus', type=int, default=0)
    p.add_argument('--gpu-mem-mib', type=int, default=0)
    p.add_argument('--memory', default='')
    p.add_argument('--slot', choices=('', 'full', 'none'), default='')
    p.add_argument('--lock', choices=('none', 's', 'x', 'l'), default='none')
    p.add_argument('--queue-timeout-minutes', type=float, default=60)
    p.add_argument('--results', default='')
    p.add_argument('--resume', choices=('true', 'false', ''), default='false')
    p.add_argument('--attempt-of', default='')
    a = p.parse_args(list(argv))
    return Inputs(a.project, a.command, a.id, a.timeout_minutes, a.gpus, a.gpu_mem_mib, a.memory, a.slot, a.lock,
                  a.queue_timeout_minutes, a.results, a.resume == 'true', a.attempt_of)


class Executor:
    """One part. The I/O is injectable: ``run`` (subprocess.run), ``spawn``
    (subprocess.Popen) and ``scope`` (False runs the supervisor without systemd, for tests);
    for the GPU sampler, ``procs`` (the PIDs in the part's scope), ``stop_part`` (the early
    stop), ``clock`` and ``sample_s``."""

    def __init__(self, inv: inventory.Inventory, etc: str, env: Mapping[str, str], *, release: str,
                 run: Callable = subprocess.run, spawn: Callable = subprocess.Popen, scope: bool = True,
                 poll: float = 3.0, procs: Callable[[], set[int]] | None = None,
                 stop_part: Callable[[], None] | None = None, clock: Callable[[], float] = time.time,
                 sample_s: float = sampler_mod.SAMPLE_S) -> None:
        self.inv, self.etc, self.env, self.release = inv, etc, dict(env), release
        self.run, self.spawn, self.scope, self.poll = run, spawn, scope, poll
        self.procs, self.stop_part, self.clock, self.sample_s = procs, stop_part, clock, sample_s
        self.stopped = False
        self.proc: subprocess.Popen | None = None
        self.unit = ''
        self.record = ''

    # -- checks before anything is taken

    def check(self, a: Inputs) -> tuple[registry.Project, str]:
        if not ID.fullmatch(a.id):
            raise Refused(f'id must match {ID.pattern}')
        try:
            proj = registry.load_file(self.etc, a.project, self.inv.this)
        except registry.RegistryError as e:
            raise Refused(f'project {a.project}: {e}') from None
        if self.env.get('GITHUB_REPOSITORY', '').lower() != proj.repository:
            raise Refused(f'project {a.project} belongs to another repository')
        cls = self.env.get('CI_RUNNER_CLASS', '')
        if not cls:
            raise Refused('CI_RUNNER_CLASS is not set: not one of our runners')
        memory = a.memory or proj.memory or proj.memory_max
        try:
            if registry.memory_bytes(memory) > registry.memory_bytes(proj.memory_max):
                raise Refused(f'memory {memory} is above the project\'s {proj.memory_max} here')
        except registry.RegistryError as e:
            raise Refused(str(e)) from None
        if a.gpu_mem_mib < 0 or a.queue_timeout_minutes < 0:
            raise Refused('gpu-mem-mib and queue-timeout-minutes must not be negative')
        if not 0 <= a.gpus <= len(self.inv.host['gpus']):
            raise Refused(f'gpus must be in [0, {len(self.inv.host["gpus"])}] here')
        if a.lock not in ('none', 's', 'x', 'l'):
            raise Refused('lock must be none, s, x or l')
        if not 0 < a.timeout_minutes <= (600 if cls == 'nightly' else 240):
            raise Refused('timeout-minutes is out of range')
        card = self.env.get('CI_RUNNER_CARD', '')
        if card and (not card.isdigit() or int(card) not in self.inv.host['gpus']):
            raise Refused('CI_RUNNER_CARD names no card of this host')
        return proj, memory

    def candidates(self) -> list[int]:
        """The cards this runner may lease: its own card on an h100-style host, where the
        runner of card N sets ``CI_RUNNER_CARD=N`` (design §5.1); else every card."""
        card = self.env.get('CI_RUNNER_CARD', '')
        if card and h100_style(self.inv.host):
            return [int(card)]
        return list(self.inv.host['gpus'])

    def gate(self, a: Inputs, cls: str) -> str:
        """Why a GPU part cannot start on this host before anything is taken, or empty
        (design §5.4 phase 1, D7, §5.1 RUN-5)."""
        h = self.inv.host
        if not a.gpus:
            return ''
        if h100_style(h) and a.gpus > 1:
            return 'multi-GPU part on h100 (D7)'
        if h100_style(h) and cls != H100_POOL:
            return f'the {cls} runners of this host take no card: GPU parts go to h100-gpu<N>'
        notice = h.get('watchdog_notice')
        if notice and os.path.lexists(str(notice)):
            return "the GPU watchdog's notice is up"
        return ''

    def cool_elsewhere(self, exclude: Sequence[int]) -> int | None:
        """The coolest card of this h100-style host outside ``exclude`` that passes the
        health gate, or None (and None on any other host)."""
        h = self.inv.host
        if not h100_style(h):
            return None
        cool = gpus_mod.cool_cards([c for c in h['gpus'] if c not in exclude], smi_status(self.run), h['max_temp_c'])
        return cool[0] if cool else None

    # -- the sampler's defaults

    def _scope_procs(self) -> set[int]:
        """The PIDs in the part's scope (its cgroup's ``cgroup.procs``)."""
        if not self.unit:
            return set()
        r = self.run(['systemctl', '--user', 'show', '-p', 'ControlGroup', '--value', self.unit],
                     capture_output=True, text=True, timeout=30)
        cg = (r.stdout or '').strip()
        if not cg.startswith('/'):
            return set()
        with open(f'/sys/fs/cgroup{cg}/cgroup.procs') as f:
            return {int(x) for x in f.read().split() if x.isdigit()}

    def _stop_scope(self) -> None:
        """The early stop: stop the scope (its cgroup dies with it); without systemd, kill
        the command's process group, whose PID the supervisor recorded."""
        if self.unit:
            self.run(['systemctl', '--user', 'stop', self.unit], capture_output=True, timeout=90)
            return
        sc = _read_json(os.path.join(self.record, 'scope.json')) or {}
        if isinstance(sc.get('child'), int) and sc['child'] > 1:
            try:
                os.killpg(sc['child'], signal.SIGKILL)
            except OSError:
                pass

    def record_dir(self, a: Inputs, attempt: int) -> str:
        repo = self.env.get('GITHUB_REPOSITORY', 'unknown/unknown').lower()
        run = f'{self.env.get("GITHUB_RUN_ID", "0")}-{self.env.get("GITHUB_RUN_ATTEMPT", "1")}'
        if not re.fullmatch(r'[0-9]+-[0-9]+', run) or not registry.REPO.fullmatch(repo):
            raise Refused('GITHUB_RUN_ID, GITHUB_RUN_ATTEMPT or GITHUB_REPOSITORY is malformed')
        return os.path.join(self.inv.host['data'], 'ci-runners', 'state', 'parts', repo, run, f'{a.id}-{attempt}')

    def attempt(self, a: Inputs) -> int:
        if not a.attempt_of:
            return 1
        prev = read_record(self.inv, a.attempt_of)
        return int((prev or {}).get('attempt', 1)) + 1

    # -- the run

    def _on_signal(self, signum, _frame) -> None:
        """The job is cancelled or its runner stops. Before the command starts: end
        ``stopped``. Once it runs: leave it to its scope (job-completed stops it on a
        cancel; after a stopped runner it finishes and ``salvage`` reads it) and exit
        without outputs."""
        self.stopped = True
        if self.proc is not None:
            raise SystemExit(128 + signum)

    def execute(self, a: Inputs) -> tuple[int, dict[str, object]]:
        proj, memory = self.check(a)
        h = self.inv.host
        cls = self.env['CI_RUNNER_CLASS']
        attempt = self.attempt(a)
        record = self.record_dir(a, attempt)
        self.record = record
        os.makedirs(os.path.dirname(record), mode=0o700, exist_ok=True)
        try:
            os.mkdir(record, 0o700)  # atomic: two runs of one id cannot both pass
        except FileExistsError:
            raise Refused(f'part {a.id} already ran in this run (give each part its own id)') from None
        work = os.path.join(record, 'work')  # the command's CI_PART_DIR: it cannot rewrite the record
        results = os.path.join(work, 'results')
        os.makedirs(results, mode=0o700)
        fallback = list(proj.fallback.get(cls, ()))
        rec: dict[str, object] = {
            'project': proj.name, 'id': a.id, 'class': cls, 'host': self.inv.this, 'runner': self.env.get('RUNNER_NAME', ''),
            'attempt': attempt, 'retries': proj.retries, 'fallback': fallback, 'inputs': dataclasses.asdict(a),
            'start_epoch': time.time(), 'boot_start': boot_id(), 'executor': os.getpid(),
            'release': self.release, 'h100': h100_style(h)}
        if inventory.pool_labels(self.inv, cls):
            rec['pool_labels'] = inventory.pool_labels(self.inv, cls)
        _write_json(os.path.join(record, 'record.json'), rec)
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, self._on_signal)
        t0 = time.monotonic()
        deadline = t0 + a.queue_timeout_minutes * 60
        leases = Leases(os.path.join(h['data'], 'ci-runners', 'state', 'leases'))
        slot_fd, slot_path = None, ''
        try:
            free = shutil.disk_usage(h['data']).free / (1 << 30)
            if free < h['disk_min_gib']:
                return self._end(record, rec, a, unavailable=f'low disk: {free:.0f} GiB free under the data directory')
            why = self.gate(a, cls)
            if why:
                return self._end(record, rec, a, unavailable=why)
            take_slot = (a.slot or ('full' if cls in SLOT_CLASSES else 'none')) == 'full'
            if take_slot:
                q = slots.SlotQueue(h['files']['slot_lock'], h['slots'], h['files']['slot_queue'], poll=self.poll)
                mine = sum(1 for t in q.waiting() if t.project == proj.name)
                if slots.queue_full(mine, proj.max_waiting):
                    return self._end(record, rec, a, unavailable='project queue full')
                q.take_ticket(proj.name, f'part {a.id}')
                try:
                    try:
                        caps, default = registry.slot_caps(self.etc, self.inv.this, h['slots'])
                    except registry.RegistryError as e:
                        return self._end(record, rec, a, unavailable=f'registry: {e}')
                    caps.setdefault(proj.name, proj.max_slots)
                    while slot_fd is None:
                        slot_fd = q.try_take(proj.name, caps, default=default)
                        if slot_fd is None:
                            if self.stopped:
                                return self._end(record, rec, a, stopped=True)
                            if time.monotonic() >= deadline:
                                return self._end(record, rec, a, unavailable='queue timeout: no CI slot')
                            time.sleep(self.poll)
                finally:
                    if q.ticket and os.path.exists(q.ticket):
                        os.unlink(q.ticket)
                slot_path = q.slots[q._index(slot_fd)]
                os.set_inheritable(slot_fd, False)  # passed to the supervisor alone (pass_fds)
            cards: list[int] = []
            if a.gpus:
                need = a.gpu_mem_mib or proj.gpu_mem_mib
                mine = self.candidates()
                while True:
                    # the health gate (h100-style hosts; elsewhere place has filtered hot cards):
                    # a hot or unread card refuses, to a cool card's runner, else the fallback
                    cool = gpus_mod.cool_cards(mine, smi_status(self.run), h['max_temp_c']) if h100_style(h) else mine
                    if not cool:
                        other = self.cool_elsewhere(mine)
                        return self._end(record, rec, a, unavailable=f'health gate: card {",".join(map(str, mine))} '
                                         f'is at or above {h["max_temp_c"]} C or unread',
                                         retry_on=None if other is None else ['self-hosted', f'{H100_POOL}-gpu{other}'])
                    got = leases.try_take(a.gpus, cool, smi_free(self.run), need)
                    if got is not None:
                        cards = got
                        break
                    if self.stopped:
                        return self._end(record, rec, a, stopped=True)
                    if time.monotonic() >= deadline:
                        return self._end(record, rec, a, unavailable='queue timeout: no card fits')
                    time.sleep(self.poll)
            rec.update(queue_seconds=round(time.monotonic() - t0), cards=cards, slot=slot_path, run_epoch=time.time())
            cpus = str(h.get('cpus') or len(os.sched_getaffinity(0)))
            env = part_env(self.env, role=self.inv.this, project=proj, part_dir=work, results=results, cards=cards,
                           cpus=cpus, files=h['files'], slot=slot_path, resume=a.resume)
            inner = command_argv(a.command, compile_part=cls == 'compile', lock=a.lock,
                                 lock_bin=os.path.join(self.release, 'bin', 'ci-lock'), host_lock=h['files']['host_lock'],
                                 cpus=str(h.get('cpus') or ''))
            sup = [sys.executable, '-I', '-c', 'import sys; sys.path.insert(0, sys.argv.pop(1)); '
                   'from ci_runners.part import supervise; '
                   'sys.exit(supervise(sys.argv[1], float(sys.argv[2]), sys.argv[3:]))',
                   self.release, record, str(a.timeout_minutes * 60), *inner]
            run_id = f'{os.path.basename(os.path.dirname(record))}-{attempt}'
            self.unit = unit_name(self.env.get('RUNNER_NAME', ''), a.id, run_id) if self.scope else ''
            rec['unit'] = self.unit
            _write_json(os.path.join(record, 'record.json'), rec)
            argv = scope_argv(self.unit, proj.name, memory, sup) if self.scope else sup
            if self.stopped:
                return self._end(record, rec, a, stopped=True)
            held = tuple(fd for fd in [slot_fd, *leases.fds] if fd is not None)
            self.proc = self.spawn(argv, cwd=self.env.get('GITHUB_WORKSPACE') or os.getcwd(), env=env, pass_fds=held)
            smp, done, thread = None, threading.Event(), None
            if cards:
                smp = sampler_mod.Sampler(cards, csv_path=os.path.join(record, 'samples.csv'),
                                          query=lambda: (smi_status(self.run), smi_apps(self.run)),
                                          procs=self.procs or self._scope_procs, stop=self.stop_part or self._stop_scope,
                                          early_stop=h100_style(h), clock=self.clock)
                thread = threading.Thread(target=smp.loop, args=(done, self.sample_s), daemon=True)
                thread.start()
            self.proc.wait()
            done.set()
            if thread is not None:
                thread.join(timeout=60)
                rec.update(pids=sorted(smp.pids), buses=[smp.buses[c] for c in sorted(smp.buses)],
                           early_stop=smp.stopped_card is not None, peak_c={str(c): t for c, t in smp.peak.items()})
            unit_result = ''
            if self.scope:
                r = self.run(['systemctl', '--user', 'show', '-p', 'Result', '--value', self.unit],
                             capture_output=True, text=True, timeout=30)
                unit_result = (r.stdout or '').strip()
                self.run(['systemctl', '--user', 'reset-failed', self.unit], capture_output=True, timeout=30)
            log = str(h.get('watchdog_log') or '')
            return self._end(record, rec, a, ex=_read_json(os.path.join(record, 'exit.json')),
                             stopped=self.stopped, unit_result=unit_result,
                             watchdog=read_tail(log) if log and cards else '',
                             cool_card=self.cool_elsewhere(cards) if cards else None)
        finally:
            leases.release()
            if slot_fd is not None:
                os.close(slot_fd)

    def _end(self, record: str, rec: dict[str, object], a: Inputs, *, ex: Mapping[str, object] | None = None,
             stopped: bool = False, unavailable: str = '', unit_result: str = '', watchdog: str = '',
             cool_card: int | None = None, retry_on: Sequence[str] | None = None) -> tuple[int, dict[str, object]]:
        _write_json(os.path.join(record, 'record.json'), rec)
        out = finish(record, rec, ex, stopped=stopped, unavailable=unavailable, unit_result=unit_result,
                     watchdog=watchdog, cool_card=cool_card, retry_on=retry_on)
        _publish(record, out, a.id, self.env)
        return (1 if out['outcome'] == 'red' else 0), out


# ------------------------------------------------------------------ salvage


def record_ref(record: str) -> str:
    """A record's path below ``state/parts`` (``<owner>/<repo>/<run>/<id>-<attempt>``):
    what the ``record`` output names, so no home path reaches the job's outputs. Pure."""
    parts = record.split(os.sep)
    return '/'.join(parts[-4:])


def _record_path(inv: inventory.Inventory, ref: str) -> str:
    """The local path of a ``<role>:<owner>/<repo>/<run>/<id>-<attempt>`` record,
    which must lie under this host's part records."""
    role, _, rel = ref.partition(':')
    if role != inv.this:
        raise Refused(f'record {ref!r} is on another host (salvage over ssh comes with RUN-3)')
    root = os.path.realpath(os.path.join(inv.host['data'], 'ci-runners', 'state', 'parts'))
    real = os.path.realpath(os.path.join(root, rel))
    if not real.startswith(root + os.sep):
        raise Refused('the record is not a part record of this host')
    return real


def read_record(inv: inventory.Inventory, ref: str) -> dict | None:
    return _read_json(os.path.join(_record_path(inv, ref), 'record.json'))


def salvage(inv: inventory.Inventory, ref: str, env: Mapping[str, str], *, wait_seconds: float = 0,
            run: Callable = subprocess.run, poll: float = 10) -> tuple[int, dict[str, object]]:
    """The outputs of a part whose job ended without them (design §4.5): its
    ``outcome.json``, else what its record and exit say, else ``lost``. A part whose
    scope still runs is waited for up to ``wait_seconds``."""
    record = _record_path(inv, ref)
    rec = _read_json(os.path.join(record, 'record.json'))
    if rec is None:
        raise Refused('no part record there')
    if not record_ref(record).lower().startswith(env.get('GITHUB_REPOSITORY', '').lower() + '/'):
        raise Refused('the record is another repository\'s')
    done = _read_json(os.path.join(record, 'outcome.json'))
    if done is None:
        deadline = time.monotonic() + wait_seconds
        unit = str(rec.get('unit', ''))
        while unit and not os.path.exists(os.path.join(record, 'exit.json')) and time.monotonic() < deadline:
            r = run(['systemctl', '--user', 'is-active', unit], capture_output=True, text=True, timeout=30)
            if (r.stdout or '').strip() not in ('active', 'activating', 'deactivating'):
                break
            time.sleep(poll)
        ex = _read_json(os.path.join(record, 'exit.json'))
        done = finish(record, rec, ex)
        if ex is not None:  # final: kept for the next reader
            pid = str(rec.get('id', ''))
            _publish(record, done, pid if ID.fullmatch(pid) else 'part', env)
        else:  # lost for now: the record may still finish, so nothing is kept
            write_outputs(done, env.get('GITHUB_OUTPUT', ''))
    else:
        write_outputs(done, env.get('GITHUB_OUTPUT', ''))
    return (1 if done['outcome'] == 'red' else 0), done


# ------------------------------------------------------------------ CLI


def main(argv: list[str], inv: inventory.Inventory, etc: str) -> int:
    release = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        if argv and argv[0] == 'salvage':
            p = argparse.ArgumentParser(prog='ci-run salvage')
            p.add_argument('--record', required=True)
            p.add_argument('--wait-seconds', type=float, default=0)
            a = p.parse_args(argv[1:])
            code, out = salvage(inv, a.record, os.environ, wait_seconds=a.wait_seconds)
        else:
            code, out = Executor(inv, etc, os.environ, release=release).execute(parse_args(argv))
    except Refused as e:
        print(f'ci-run part: refused: {e}', file=sys.stderr)
        return 2
    print(f'ci-run part: {out["outcome"]} ({out["reason"]}), next {out["next"]}', file=sys.stderr)
    return code
