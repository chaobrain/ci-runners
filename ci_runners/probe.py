"""``place``'s host probes and the ``place`` step (design §4.2).

``ci-run probe`` prints this host's probe as JSON: its active runner pools, whether the
CI proxy answers, its CI slots and how many are held or waited for, and per card the
free memory and temperature. ``ci-run place`` probes every local-driven host of the
inventory (this one directly, the others over ssh, 60 s each, 5 min in all) and prints
the plan of ``place.place``. h100 joins with RUN-2.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence

from ci_runners import inventory, place, proxy, slots

PROBE_BOUND, PLACE_BOUND = 60, 300
_RUNNER = re.compile(r'ci-runner@([A-Za-z0-9_.-]+)\.service')


def runner_pools(units: str, role: str) -> frozenset[str]:
    """Pools with an active runner, from ``systemctl --user list-units 'ci-runner@*'``
    (``<role>-<pool>-<i>``). Pure."""
    out = set()
    for line in units.splitlines():
        m = _RUNNER.search(line)
        if m and m.group(1).startswith(role + '-'):
            pool = m.group(1)[len(role) + 1:].rsplit('-', 1)[0]
            if pool:
                out.add(pool)
    return frozenset(out)


def cards(csv: str, want: Sequence[int]) -> tuple[place.Card, ...]:
    """Cards from ``nvidia-smi --query-gpu=index,memory.total,memory.used,temperature.gpu
    --format=csv,noheader,nounits``, those of ``want`` only. Pure."""
    out = []
    for line in csv.splitlines():
        p = [x.strip() for x in line.split(',')]
        if len(p) == 4 and all(x.isdigit() for x in p) and int(p[0]) in want:
            out.append(place.Card(int(p[0]), int(p[1]) - int(p[2]), float(p[3])))
    return tuple(out)


def to_json(p: place.Probe) -> dict:
    d = dataclasses.asdict(p)
    d['runners'] = sorted(p.runners)
    return d


def from_json(d: object) -> place.Probe:
    """A probe from its JSON; anything malformed gives the failing probe, and a field
    it does not state counts against the host (a card without a temperature fails the
    probe; one without ``healthy`` or ``others`` is unhealthy, in use by others). Pure."""
    try:
        if not isinstance(d, dict):
            raise TypeError
        cs = tuple(place.Card(int(c['index']), int(c['free_mib']), float(c['temp_c']),
                              c.get('others') is not False, c.get('healthy') is True) for c in d.get('cards', []))
        return place.Probe(reachable=d.get('reachable') is True, runners=frozenset(str(r) for r in d.get('runners', [])),
                           proxy=d.get('proxy') is True, reserved=d.get('reserved') is not False,
                           quarantined=d.get('quarantined') is not False, slots=int(d.get('slots', 0)),
                           busy=int(d.get('busy', 0)), cards=cs, slowdown=float(d.get('slowdown', 1.0)),
                           remote=d.get('remote') is True, max_temp_c=float(d.get('max_temp_c', 80.0)))
    except (TypeError, ValueError, KeyError):
        return place.Probe()


def local(inv: inventory.Inventory, run: Callable = subprocess.run, http_ok: Callable = proxy.probe) -> place.Probe:
    """This host's probe, within ``PROBE_BOUND`` (10 + 20 + 20 s at most). It does not
    yet read a host reservation or the cards' use by others (§4.2): 3090b has neither;
    both come with RUN-3, before 3090a joins."""
    h = inv.host
    r = run(['systemctl', '--user', 'list-units', '--type=service', '--state=active', '--plain', '--no-legend',
             'ci-runner@*'], capture_output=True, text=True, timeout=10)
    pools = runner_pools(r.stdout or '', inv.this)
    ok = http_ok(h['proxy']['http_port'], timeout=20) if 'proxy' in h else True
    q = slots.SlotQueue(h['files']['slot_lock'], h['slots'], h['files']['slot_queue'])
    try:
        per, _, _ = q.held()
        busy = sum(per.values()) + len(q.waiting())
    except OSError:
        busy = h['slots']
    cs: tuple[place.Card, ...] = ()
    if h['gpus']:
        try:
            g = run(['nvidia-smi', '--query-gpu=index,memory.total,memory.used,temperature.gpu',
                     '--format=csv,noheader,nounits'], capture_output=True, text=True, timeout=20)
            cs = cards(g.stdout or '', list(h['gpus'])) if g.returncode == 0 else ()
        except (OSError, subprocess.SubprocessError):
            cs = ()
    return place.Probe(reachable=True, runners=pools, proxy=bool(ok), reserved=False, quarantined=False,
                       slots=int(h['slots']), busy=busy, cards=cs, slowdown=float(h['cpu_slowdown']),
                       max_temp_c=float(h['max_temp_c']))


def remote(inv: inventory.Inventory, role: str, run: Callable = subprocess.run) -> place.Probe:
    """Another host's probe over ssh: its own ``ci-run probe``. Unreachable on any error."""
    h = inv.hosts[role]
    try:
        r = run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=20', '-o', 'ServerAliveInterval=30',
                 '-o', 'ServerAliveCountMax=4', '--', str(h['ssh']), 'data/ci-runners/releases/hook/bin/ci-run', 'probe'],
                capture_output=True, text=True, timeout=PROBE_BOUND)
        return from_json(json.loads(r.stdout)) if r.returncode == 0 else place.Probe()
    except (OSError, subprocess.SubprocessError, ValueError):
        return place.Probe()


def probe_all(inv: inventory.Inventory, *, local_probe: Callable, remote_probe: Callable,
              bound: float = PLACE_BOUND) -> dict[str, place.Probe]:
    """Every local-driven host's probe, in parallel; a probe not back within ``bound`` fails."""
    roles = [r for r, h in inv.hosts.items() if not h.get('remote')]
    out = {r: place.Probe() for r in roles}
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(roles)))
    futs = {ex.submit(local_probe if r == inv.this else remote_probe, r): r for r in roles}
    try:
        for f in concurrent.futures.as_completed(futs, timeout=bound):
            try:
                out[futs[f]] = f.result()
            except Exception:  # noqa: BLE001 - a broken probe skips its host
                pass
    except concurrent.futures.TimeoutError:
        pass
    ex.shutdown(wait=False, cancel_futures=True)
    return out


def default_prefer(inv: inventory.Inventory) -> dict[str, list[str]]:
    """This host first, then the inventory's order, for every class."""
    roles = [inv.this] + [r for r, h in inv.hosts.items() if r != inv.this and not h.get('remote')]
    return {c: roles for c in place.CLASSES}


def main(argv: list[str], inv: inventory.Inventory) -> int:
    if argv[:1] == ['probe']:
        print(json.dumps(to_json(local(inv)), sort_keys=True))
        return 0
    p = argparse.ArgumentParser(prog='ci-run place')
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument('--parts')
    g.add_argument('--parts-file')
    p.add_argument('--prefer', default='')
    p.add_argument('--exclude', default='')
    a = p.parse_args(argv[1:])
    try:
        if a.parts_file:
            with open(a.parts_file, encoding='utf-8') as f:
                parts = place.parse_parts(json.load(f))
        else:
            parts = place.parse_parts(json.loads(a.parts))
        prefer = json.loads(a.prefer) if a.prefer else default_prefer(inv)
        if not isinstance(prefer, dict) or not all(isinstance(v, list) for v in prefer.values()):
            raise ValueError('prefer must map a class to a list of hosts')
    except (OSError, ValueError) as e:
        print(f'ci-run place: {e}', file=sys.stderr)
        return 2
    t0 = time.monotonic()
    probes = probe_all(inv, local_probe=lambda _r: local(inv), remote_probe=lambda r: remote(inv, r))
    plan = place.place(parts, probes, prefer, [x for x in a.exclude.split(',') if x])
    line = json.dumps(plan, separators=(',', ':'))
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as f:
            f.write(f'plan={line}\n')
    print(line)
    faults = {r: place.host_fault(pr) for r, pr in probes.items() if place.host_fault(pr)}
    print(f'ci-run place: {len(probes)} host(s) probed in {time.monotonic() - t0:.1f} s; skipped: {faults or "none"}',
          file=sys.stderr)
    return 0
