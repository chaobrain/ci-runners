"""The load record and the busy rule (design decision 13.2).

``ci-hostload.timer`` runs ``ci-run hostload sample`` every 60 s, which appends one line
to ``state/hostload/<date>.jsonl``: the raw counters and the four values other accounts
use (every account but the CI account, the local maintainer's included):

- ``cpu``: cores, the busy time of ``/proc/stat`` minus the CI account's (``cpu.stat``
  of its ``user@<uid>.service``), per second since the previous line;
- ``mem``: GiB, ``MemTotal`` minus ``MemAvailable``, minus the CI account's ``anon``;
- ``gpu``: MiB per card of the compute processes whose ``/proc/<pid>`` the account cannot
  see (under ``ProtectProc=invisible`` it sees only its own: fails closed);
- ``lock``: the PID of an exclusive ``flock`` on the host lock that the account cannot
  see, or null.

``busy`` reads the lines of a window: a part's first attempt ends ``unavailable`` when a
rule held in every sample of its window. With fewer than half the window's samples the
host is not busy (fail open). Every reason names the rule, the value and the threshold.

CLI::

    ci-run hostload sample      one line of the record (the timer)
    ci-run hostload status      the rules over the last window, as a part would read them
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence

from ci_runners import gpus as gpus_mod
from ci_runners import inventory, outcome

#: the inventory's ``busy`` table: key -> default (None: from the host, see ``thresholds``)
DEFAULTS: dict[str, float | None] = {'window_minutes': 10, 'cpu_cores': None, 'memory_gib': None, 'gpu_mib': 2048,
                                     'lock_minutes': 20}
KEEP_DAYS = 2  # the record's days kept by the reaper
MARK_DAYS = 14  # the refused markers' days kept by the reaper
PREV_MAX_S = 300  # a previous line older than this gives no CPU rate
_BUSY_FIELDS = ('user', 'nice', 'system', 'idle', 'iowait', 'irq', 'softirq', 'steal')


# ------------------------------------------------------------------ readings (pure parsers)


def cpu_busy_seconds(stat: str, hz: int) -> float | None:
    """The host's busy CPU seconds since boot from ``/proc/stat``'s ``cpu`` line: user,
    nice, system, irq, softirq and steal (idle and iowait are idle). Pure."""
    for line in stat.splitlines():
        p = line.split()
        if p and p[0] == 'cpu' and len(p) >= 9 and all(x.isdigit() for x in p[1:9]):
            v = dict(zip(_BUSY_FIELDS, map(int, p[1:9])))
            return (v['user'] + v['nice'] + v['system'] + v['irq'] + v['softirq'] + v['steal']) / hz
    return None


def keyed(text: str, key: str) -> int | None:
    """The integer after ``key`` in a ``key value`` or ``key: value kB`` file. Pure."""
    for line in text.splitlines():
        p = line.replace(':', ' ').split()
        if len(p) >= 2 and p[0] == key and p[1].isdigit():
            return int(p[1])
    return None


def manager_cgroup(proc_cgroup: str) -> str:
    """The CI account's ``user@<uid>.service`` cgroup from ``/proc/self/cgroup`` (cgroup
    v2), or empty. Pure."""
    lines = proc_cgroup.strip().splitlines()
    if len(lines) != 1 or not lines[0].startswith('0::/'):
        return ''
    parts = lines[0][3:].strip('/').split('/')
    for i, x in enumerate(parts):
        if re.fullmatch(r'user@\d+\.service', x):
            return '/' + '/'.join(parts[:i + 1])
    return ''


def write_flocks(locks: str, dev: tuple[int, int], ino: int) -> list[int]:
    """The PIDs holding an exclusive ``FLOCK`` on the file ``(major, minor), ino`` in
    ``/proc/locks``. Pure."""
    out = []
    for line in locks.splitlines():
        p = line.split()
        if len(p) >= 6 and p[1] == 'FLOCK' and p[3] == 'WRITE' and p[4].isdigit():
            try:
                ma, mi, i = p[5].split(':')
                if (int(ma, 16), int(mi, 16), int(i)) == (dev[0], dev[1], ino):
                    out.append(int(p[4]))
            except ValueError:
                continue
    return out


def compute_mib(csv: str) -> list[tuple[int, str, int]]:
    """``(pid, bus, MiB)`` of ``nvidia-smi --query-compute-apps=pid,gpu_bus_id,used_memory
    --format=csv,noheader,nounits``. Pure."""
    out = []
    for line in csv.splitlines():
        p = [x.strip() for x in line.split(',')]
        if len(p) == 3 and p[0].isdigit() and p[2].isdigit():
            out.append((int(p[0]), p[1], int(p[2])))
    return out


# ------------------------------------------------------------------ sampling


def _read(path: str) -> str:
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            return f.read(1 << 20)
    except OSError:
        return ''


def _smi(argv: Sequence[str], run: Callable) -> str:
    try:
        r = run(['nvidia-smi', *argv], capture_output=True, text=True, timeout=20)
        return r.stdout if r.returncode == 0 else ''
    except (OSError, subprocess.SubprocessError):
        return ''


def raw(host_lock: str = '', *, read: Callable[[str], str] = _read, run: Callable = subprocess.run,
        visible: Callable[[int], bool] = lambda pid: os.path.exists(f'/proc/{pid}'),
        hz: int | None = None, now: Callable[[], float] = time.time) -> dict:
    """One sample's raw readings: the host's and the CI account's CPU seconds, memory, the
    other accounts' compute memory per card, and another account's exclusive hold of the
    host lock."""
    hz = hz or os.sysconf('SC_CLK_TCK')
    cg = manager_cgroup(read('/proc/self/cgroup'))
    ci_usec = keyed(read(f'/sys/fs/cgroup{cg}/cpu.stat'), 'usage_usec') if cg else None
    ci_anon = keyed(read(f'/sys/fs/cgroup{cg}/memory.stat'), 'anon') if cg else None
    mem = read('/proc/meminfo')
    total, avail = keyed(mem, 'MemTotal'), keyed(mem, 'MemAvailable')
    buses = gpus_mod.gpu_buses(_smi(['--query-gpu=index,pci.bus_id', '--format=csv,noheader'], run))
    by_bus = {outcome.bus_key(b): c for c, b in buses.items()}
    gpu: dict[str, int] = {str(c): 0 for c in buses}
    apps = compute_mib(_smi(['--query-compute-apps=pid,gpu_bus_id,used_memory', '--format=csv,noheader,nounits'], run))
    for pid, bus, mib in apps:
        card = by_bus.get(outcome.bus_key(bus))
        if card is not None and not visible(pid):
            gpu[str(card)] = gpu.get(str(card), 0) + mib
    lock = None
    if host_lock:
        try:
            st = os.stat(host_lock)
            foreign = [p for p in write_flocks(read('/proc/locks'), (os.major(st.st_dev), os.minor(st.st_dev)),
                                               st.st_ino) if not visible(p)]
            lock = foreign[0] if foreign else None
        except OSError:
            lock = None
    busy = cpu_busy_seconds(read('/proc/stat'), hz)
    gib = 1 << 20  # kB per GiB
    return {'t': now(), 'cpu_s': busy, 'ci_cpu_s': None if ci_usec is None else ci_usec / 1e6,
            'mem_total': None if total is None else total / gib,
            'mem': None if total is None or avail is None else max(0.0, (total - avail) / gib - (ci_anon or 0) / (1 << 30)),
            'gpu': gpu, 'lock': lock}


def derive(line: dict, prev: dict | None) -> dict:
    """The line with ``cpu``: other accounts' cores since ``prev``, or None without a
    usable previous line. Pure."""
    out = dict(line)
    out['cpu'] = None
    if (prev and None not in (line.get('cpu_s'), line.get('ci_cpu_s'), prev.get('cpu_s'), prev.get('ci_cpu_s'))
            and 0 < line['t'] - prev['t'] <= PREV_MAX_S):
        dt = line['t'] - prev['t']
        out['cpu'] = round(max(0.0, (line['cpu_s'] - prev['cpu_s']) - (line['ci_cpu_s'] - prev['ci_cpu_s'])) / dt, 2)
    return out


def record_dir(data: str) -> str:
    return os.path.join(data, 'ci-runners', 'state', 'hostload')


def day_file(data: str, t: float) -> str:
    return os.path.join(record_dir(data), time.strftime('%Y-%m-%d', time.localtime(t)) + '.jsonl')


def lines(data: str, since: float, now: float) -> list[dict]:
    """The record's lines from ``since`` to ``now``, oldest first (today's and yesterday's
    files); unreadable lines are skipped."""
    out = []
    for t in sorted({since, now}):
        path = day_file(data, t)
        try:
            with open(path, encoding='utf-8') as f:
                text = f.read(16 << 20)
        except OSError:
            continue
        for line in text.splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if isinstance(d, dict) and isinstance(d.get('t'), (int, float)) and since <= d['t'] <= now:
                out.append(d)
    out.sort(key=lambda d: d['t'])
    dedup = []
    for d in out:
        if not dedup or dedup[-1]['t'] != d['t']:
            dedup.append(d)
    return dedup


def sample(data: str, host_lock: str = '', **kw) -> dict:
    """Append one line to the record and return it."""
    line = raw(host_lock, **kw)
    prev = lines(data, line['t'] - PREV_MAX_S, line['t'])
    line = derive(line, prev[-1] if prev else None)
    os.makedirs(record_dir(data), mode=0o700, exist_ok=True)
    fd = os.open(day_file(data, line['t']), os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        os.write(fd, (json.dumps(line, sort_keys=True) + '\n').encode())
    finally:
        os.close(fd)
    return line


# ------------------------------------------------------------------ the rule


def thresholds(busy: Mapping[str, object] | None, *, cpus: int, mem_total_gib: float | None) -> dict[str, float]:
    """The thresholds: the inventory's ``busy`` table over the defaults of decision 13.2
    (``cpu_cores`` half the online CPUs, ``memory_gib`` half ``MemTotal`` rounded down).
    Pure."""
    t = {k: v for k, v in DEFAULTS.items()}
    t['cpu_cores'] = max(1, cpus // 2)
    t['memory_gib'] = float(int(mem_total_gib / 2)) if mem_total_gib else None
    for k, v in (busy or {}).items():
        t[k] = v
    return t


def _held(samples: list[dict], key: Callable[[dict], float | None], th: float, least: float) -> float | None:
    """The smallest value of the samples that have one, if there are at least ``least``
    of them and every one is at or above ``th``; else None. Pure."""
    vals = [v for v in map(key, samples) if v is not None]
    if len(vals) < least or any(v < th for v in vals):
        return None
    return min(vals)


def busy(record: list[dict], now: float, th: Mapping[str, float], cards: Sequence[int] = (),
         lock: bool = False) -> str:
    """Why the host is busy for a part, by decision 13.2, or empty. ``record`` holds the
    lines of at least the longer window; ``cards`` are the cards the part may lease;
    ``lock`` whether the host has a host lock. A rule needs a value in at least half its
    window's minutes (one sample a minute), each at or above its threshold. Pure."""
    def window(minutes: float) -> list[dict]:
        return [d for d in record if now - minutes * 60 < d['t'] <= now]

    mins = th['window_minutes']
    w, least = window(mins), max(1.0, mins / 2)
    v = _held(w, lambda d: d.get('cpu'), th['cpu_cores'], least)
    if v is not None:
        return f'busy: cpu {v:g} >= {th["cpu_cores"]:g} cores for {mins:g} min'
    if th.get('memory_gib'):
        v = _held(w, lambda d: d.get('mem'), th['memory_gib'], least)
        if v is not None:
            return f'busy: memory {v:.1f} >= {th["memory_gib"]:g} GiB for {mins:g} min'
    for c in cards:
        v = _held(w, lambda d: (d.get('gpu') or {}).get(str(c)), th['gpu_mib'], least)
        if v is not None:
            return f'busy: card {c} {v:g} >= {th["gpu_mib"]:g} MiB for {mins:g} min'
    if lock:
        lw = window(th['lock_minutes'])
        pids = [d.get('lock') for d in lw]
        if len(pids) >= max(1.0, th['lock_minutes'] / 2) and None not in pids and len(set(pids)) == 1:
            return f'busy: host lock held exclusively by another account for >= {th["lock_minutes"]:g} min'
    return ''


def marker(data: str, repo: str, head: str, part_id: str) -> str:
    """The marker of a part refused as busy on this host: once per head and part."""
    h = hashlib.sha256(f'{repo.lower()}\0{head}\0{part_id}'.encode()).hexdigest()[:32]
    return os.path.join(record_dir(data), 'refused', h)


def check(inv: inventory.Inventory, *, cards: Sequence[int] = (), now: float | None = None,
          cpus: int | None = None) -> str:
    """The busy rule on this host's record now (decision 13.2)."""
    h = inv.host
    now = time.time() if now is None else now
    data = str(h['data'])
    raw_busy = h.get('busy') if isinstance(h.get('busy'), dict) else {}
    longest = max(float(raw_busy.get('window_minutes', DEFAULTS['window_minutes'])),
                  float(raw_busy.get('lock_minutes', DEFAULTS['lock_minutes'])))
    rec = lines(data, now - longest * 60, now)
    mem_total = next((d['mem_total'] for d in reversed(rec) if d.get('mem_total')), None)
    th = thresholds(raw_busy, cpus=cpus or os.cpu_count() or 1, mem_total_gib=mem_total)
    return busy(rec, now, th, cards, lock=bool(h['files'].get('host_lock')))


# ------------------------------------------------------------------ CLI


def main(argv: list[str], inv: inventory.Inventory) -> int:
    h = inv.host
    if argv == ['sample']:
        line = sample(str(h['data']), str(h['files'].get('host_lock') or ''))
        print(json.dumps({k: line[k] for k in ('cpu', 'mem', 'gpu', 'lock')}, sort_keys=True))
        return 0
    if argv == ['status']:
        why = check(inv, cards=list(h['gpus']))
        print(why or 'not busy')
        return 0
    print('usage: ci-run hostload sample|status', file=sys.stderr)
    return 2
