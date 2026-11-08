"""``ci-run slurm canary <site> <account> --project P``: one short job per target of an
account (design decision 14.9, 14.10), through the CI key's gate as a part's job runs.

Each job runs for at most ``CANARY_MINUTES`` on the target's ``max_cards`` (a CPU
target: its CPUs), in the project's env for the target's variant, under the cluster
supervisor, which runs the P2P self-test first where the cards are 2 or more (12.6). Its
command is this file's ``check`` (stdlib only, in the env's Python): the modules named by
``--import`` load, the node's driver is at least the variant's floor (``FLOOR``; a GPU
variant without one fails), the job sees its cards (``CUDA_VISIBLE_DEVICES``, ``nvidia-smi``
and jax where it is imported), and a marker staged with the workspace comes back in the
part's results (the file round trip). The driver then records, per target, in
``state/slurm/<site>/targets.json``: the outcome, the start latency (from submission to
the job's start, both by ``sacct``), the elapsed time, ``sacct``'s state and exit code, the node and the cost
by the site's prices. A failed P2P self-test holds the target at one card; a pass lifts a
hold (14.4). A target is enabled in the site config only after its canary and real parts
pass (14.10): that edit is the lead's.
"""
from __future__ import annotations

import datetime
import json
import os
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence

CANARY_MINUTES = 3
#: minutes past ``--wait`` and the job's own limit after which the canary gives up on a job
#: whose end it cannot see (the gate unreachable), cancels it and records why
DEADLINE_MARGIN = 15
#: the oldest driver a variant runs on, by its major version
FLOOR = {'cuda13': 580, 'cuda12': 525}
MARKER = 'canary-marker'
RUN_URL = 'https://github.com/chaobrain/ci-runners/actions/runs/0'


# ------------------------------------------------------------------ in the job, on the node


def check(argv: Sequence[str], env=os.environ, run: Callable = subprocess.run) -> int:
    """``check --cards N --floor F [--import M]...``: write ``canary.json`` and the marker
    to ``$CI_RESULTS_DIR``; exit 1 if any check failed."""
    import argparse
    import importlib
    p = argparse.ArgumentParser(prog='slurmcanary check')
    p.add_argument('--cards', type=int, default=0)
    p.add_argument('--floor', type=int, default=0)
    p.add_argument('--import', dest='imports', action='append', default=[])
    a = p.parse_args(argv)
    why: list[str] = []
    out: dict[str, object] = {'python': '.'.join(map(str, sys.version_info[:3])), 'imports': {}, 'node': os.uname()[1]}
    for m in a.imports:
        try:
            importlib.import_module(m)
            out['imports'][m] = 'ok'  # type: ignore[index]
        except Exception as e:  # noqa: BLE001 - any failure to load is the finding
            out['imports'][m] = f'{type(e).__name__}: {e}'[:300]  # type: ignore[index]
            why.append(f'import {m}: {type(e).__name__}')
    if a.cards:
        try:
            r = run(['nvidia-smi', '--query-gpu=driver_version', '--format=csv,noheader'], capture_output=True,
                    text=True, timeout=60)
            versions = [v.strip() for v in r.stdout.splitlines() if v.strip()] if r.returncode == 0 else []
        except (OSError, subprocess.SubprocessError):
            versions = []
        out['driver'] = versions[0] if versions else None
        major = int(versions[0].split('.')[0]) if versions and versions[0].split('.')[0].isdigit() else 0
        if not versions:
            why.append('nvidia-smi: no driver version')
        elif major < a.floor:
            why.append(f'driver {versions[0]} is below the variant\'s floor {a.floor}')
        cvd = env.get('CUDA_VISIBLE_DEVICES')
        seen = len([x for x in cvd.split(',') if x.strip()]) if cvd is not None else len(versions)
        out['cards_seen'] = seen
        if seen != a.cards:
            why.append(f'the job sees {seen} cards, not {a.cards}')
        elif versions and len(versions) < a.cards:
            why.append(f'nvidia-smi lists {len(versions)} cards, not {a.cards}')
        if out['imports'].get('jax') == 'ok':  # type: ignore[union-attr]
            try:
                import jax
                n = len([d for d in jax.devices() if d.platform == 'gpu'])
            except Exception as e:  # noqa: BLE001 - a backend that fails to load is the finding
                n = 0
                why.append(f'jax: {type(e).__name__}: {e}'[:300])
            out['jax_cards'] = n
            if n != a.cards:
                why.append(f'jax sees {n} cards, not {a.cards}')
    results = env.get('CI_RESULTS_DIR', '')
    try:
        shutil.copyfile(os.path.join(env.get('GITHUB_WORKSPACE', ''), MARKER), os.path.join(results, MARKER))
    except OSError as e:
        why.append(f'the staged marker: {type(e).__name__}')
    out.update(ok=not why, why=why)
    with open(os.path.join(results, 'canary.json'), 'w', encoding='utf-8') as f:
        json.dump(out, f, indent=1)
    print(json.dumps(out))
    return 0 if not why else 1


# ------------------------------------------------------------------ on the driver


def _epoch(iso: str) -> float | None:
    try:
        return datetime.datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return None


def _cancel(g, key: str, poll: float) -> bool:
    """Cancel the canary's job; whether the gate confirmed it is gone."""
    from ci_runners import slurmpart
    for _ in range(10):
        try:
            if slurmpart.cancel_job(g, key) is not None:
                return True
        except (slurmpart.Unreachable, slurmpart.Refused):
            pass
        time.sleep(poll)
    return False


def one(ctx, site, alias: str, target: str, project: str, imports: Sequence[str], *, wait: float,
        poll: float = 5.0) -> dict:
    """The canary job of one target: its record (``ok`` and why not)."""
    from ci_runners import slurmpart, slurmsetup
    g = ctx.gate(site, alias)
    t = site.targets[target]
    cards = t.max_cards if t.gpu else 0
    variant = str(t.t['variant'])
    if cards and variant not in FLOOR:
        return {'at': time.time(), 'cards': cards, 'ok': False, 'why': [f'no driver floor for the variant {variant}']}
    base = slurmsetup.base_of(site, alias)
    key = f'ci-runners/canary/{time.time_ns() // 1000}-1/{target}-1'  # a run of its own each time
    token = secrets.token_hex(8)
    cmd = ['python', '-I', f'{base}/releases/hook/ci_runners/slurmcanary.py', 'check', '--cards', str(cards),
           '--floor', str(FLOOR.get(variant, 0)), *(x for m in imports for x in ('--import', m))]
    req = {'project': project, 'target': target, 'cards': cards, 'minutes': CANARY_MINUTES, 'queue_minutes': wait,
           'memory': '4G', 'command': shlex.join(cmd), 'variables': {}, 'run_url': RUN_URL, 'host': ctx.driver,
           'resume': False}
    rec: dict[str, object] = {'at': time.time(), 'cards': cards, 'ok': False}
    with tempfile.TemporaryDirectory(prefix='ci-canary-') as d:
        ws, back = os.path.join(d, 'ws'), os.path.join(d, 'part')
        os.makedirs(ws)
        os.makedirs(back)
        with open(os.path.join(ws, MARKER), 'w') as f:
            f.write(token)
        try:
            g.sync('stage', ws + '/', f'{g.host}:{key}/ws/')
            submitted = time.time()
            rec['job'] = g.json('submit', key, data=json.dumps(req).encode())['job']
        except (slurmpart.Unreachable, slurmpart.Refused) as e:
            return {**rec, 'why': [f'submit: {e}']}
        deadline = submitted + 60 * (wait + CANARY_MINUTES + DEADLINE_MARGIN)
        while True:
            try:
                st = g.json('status', key, timeout=60)
            except (slurmpart.Unreachable, slurmpart.Refused):
                st = {'state': 'UNKNOWN'}
            if str(st.get('state')) not in slurmpart.LIVE:
                break
            pending = st.get('state') == 'PENDING' and time.time() - submitted > wait * 60
            if pending or time.time() > deadline:
                why = (f'pending past {wait:g} min: {st.get("reason") or "?"}' if pending else
                       f'no end seen in {deadline - submitted:.0f} s (last state {st.get("state")})')
                gone = _cancel(g, key, poll)
                return {**rec, 'why': [why] + ([] if gone else [f'the cancel of job {rec["job"]} is unconfirmed: '
                                                                'check it with `ci-run slurm status`'])}
            time.sleep(poll)
        try:
            g.sync('fetch', f'{g.host}:{key}/part/', back + '/')
        except (slurmpart.Unreachable, slurmpart.Refused):
            pass
        res = os.path.join(back, 'results')
        try:
            with open(os.path.join(res, 'canary.json'), encoding='utf-8') as f:
                found = json.load(f)
        except (OSError, ValueError):
            found = {'why': ['no canary.json came back']}
        try:
            with open(os.path.join(res, MARKER)) as f:
                round_trip = f.read() == token
        except OSError:
            round_trip = False
    start, queued = _epoch(str(st.get('start', ''))), _epoch(str(st.get('submit', '')))
    elapsed = int(st.get('elapsed') or 0)
    ex = st.get('exit') if isinstance(st.get('exit'), dict) else {}
    why = [str(w) for w in found.get('why', [])]
    if ex.get('p2p'):
        why.insert(0, f'p2p: {ex["p2p"]}')
    if not round_trip and not ex.get('p2p'):
        why.append('the file round trip failed')
    if st.get('state') != 'COMPLETED' and not why:
        why.append(f'{st.get("state")} {st.get("exit_code", "")}'.strip())
    rec.update(ok=not why and st.get('state') == 'COMPLETED', why=why, state=st.get('state'), exit_code=st.get('exit_code'),
               node=st.get('node'), elapsed=elapsed, latency=round(start - queued, 1) if start and queued else None,
               cost=round(site.hourly(target, cards, t.cpus(cards)) * elapsed / 3600, 4), p2p=bool(ex.get('p2p')),
               found={k: found.get(k) for k in ('python', 'imports', 'driver', 'jax_cards')})
    return rec


def canary(ctx, name: str, alias: str, project: str, *, targets: Sequence[str] = (), imports: Sequence[str] = (),
           wait: float = 30.0, poll: float = 5.0) -> int:
    from ci_runners import slurmpart
    from ci_runners.slurmsetup import SetupError
    site = ctx.account(name, alias)
    own = list(site.accounts[alias].a['targets'])
    if set(targets) - set(own):
        raise SetupError(f'--target {sorted(set(targets) - set(own))}: not a target of {alias} ({own})')
    path = os.path.join(slurmpart.state_dir(ctx.data, site.name), 'targets.json')
    bad = 0
    for target in targets or own:
        rec = one(ctx, site, alias, target, project, imports, wait=wait, poll=poll)
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        held = slurmpart._read_json(path) or {}
        entry = dict(held.get(target) or {})
        if rec['ok']:
            entry = {}  # a pass lifts a hold at one card (14.4)
        elif rec.get('p2p'):
            entry.update(max_cards=1, why='p2p: the canary\'s self-test failed', since=time.time())
        held[target] = {**entry, 'canary': rec}
        slurmpart._write_json(path, held)
        f = rec.get('found') or {}
        ctx.say(f'{target}: {"pass" if rec["ok"] else "FAIL: " + "; ".join(rec.get("why") or [])} '
                f'(job {rec.get("job")}, {rec.get("cards")} cards on {rec.get("node") or "?"}, '
                f'{rec.get("elapsed", 0)} s, start latency {rec.get("latency")} s, cost {float(rec.get("cost") or 0):.3f}; '
                f'python {f.get("python")}, driver {f.get("driver")}, imports {f.get("imports")})')
        bad += not rec['ok']
    return 1 if bad else 0


if __name__ == '__main__':
    if sys.argv[1:2] == ['check']:
        sys.exit(check(sys.argv[2:]))
    sys.exit(2)
