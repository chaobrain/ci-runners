"""``ci-run pool add|check|ready``: adding a machine to the pool (decision 15.1; brainspike's
D-0111 amended, 2026-11-10: "Adding a machine is one script: runners with capability labels,
the env, its cache warmed, a canary, the numerics check; only then usable").

**Hosts only.** A Slurm cluster's entry (its gate, its targets and their prices) stays
hand-written; its targets count after the cluster's own acceptance gates (14.10).

``pool add KEY`` probes the machine, through its ssh alias or here (``--local``): its
``hostname``, cores, ``MemTotal``, and ``nvidia-smi``'s cards (one model, its MiB) and the
driver's CUDA major (``cudaNN``). It also says what is not there yet (the env's Python, the
work directory, the CI slot files), which the checks then need. It renders a
``[machine.KEY]`` entry with ``ready = false``, ``numerics_ok = false`` and ``p2p = false``,
appends it to ``$CI_RUNNERS_ETC/pool.toml``, validates the whole new file with
``pool.load`` and replaces the file atomically (mode 600). An existing key, or a host whose
``hostname`` another entry has, is refused. ``pool run`` and ``pool status``'s plan skip a
machine that is not ready, unless ``--on KEY`` names it.

Then the checks run, in order, each through ``pool run --on KEY`` in the caller's checkout
(``--dir``), so each holds one of the machine's CI slots:

1. ``env``: the env check (``pool.fingerprint``, ``pool.env_diff``) of the machine's env
   against the caller's (``--caller-python``); a difference fails it.
2. ``canary``: jax imports in the machine's env and sees as many cards as the entry has.
3. ``p2p`` (2+ cards): the P2P self-test (``p2p.py``, decision 12.6). Its result becomes the
   entry's ``p2p``; a failure does not block (the machine runs one-card work).
4. ``warm``: the registry's top-level ``warm`` command, the project's compile-cache warm-up.
5. ``numerics``: the registry's top-level ``numerics`` command, the project's numerics check
   (exit 0: bit-identical to the reference).

The checks see every card of the machine (``CUDA_VISIBLE_DEVICES``) and take no part lease:
a machine being added has no parts yet. The machine's ``cache`` is passed as
``CI_POOL_CACHE``. The first failure stops them. Each result is recorded in
``pool-add.json`` beside ``pool.toml`` (mode 600) with a digest of the entry and of the
registry's commands, so an edit of either makes the record stale. ``pool check KEY``
runs them again (``--steps`` some of them).

``pool ready KEY`` re-runs nothing: it reads the record and, only when every step passed
for the entry as it is now, sets ``ready = true``, ``numerics_ok = true`` and ``p2p`` (the
self-test's result) in the entry, validated and replaced as above.

What the lead does is printed as the next steps, never done here (AGENTS.md): the machine's
setup plan (its runners with their capability labels, ``setup/host/plan.sh``, applied by the
lead), the runners' registration, the CI account's env build (``ci-run env build``) and the
new names' hashes for ``tests/deny.sha256``.

CLI::

    ci-run pool add KEY (--ssh ALIAS | --local) --python P --work DIR --slots-lock L --slots-count N
                    [--slots-queue Q] [--labels GROUP=LABEL,...] [--cache DIR] [--dir CHECKOUT]
                    [--caller-python PY] [--timeout MIN] [--dry-run]
    ci-run pool check KEY [--steps env,canary,p2p,warm,numerics] [--dir CHECKOUT] [--caller-python PY]
                      [--timeout MIN] [--dry-run]
    ci-run pool ready KEY

Stdlib only.
"""
from __future__ import annotations

import dataclasses
import fcntl
import hashlib
import inspect
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence

from ci_runners.pools import pool

STATE = 'pool-add.json'
STEPS = ('env', 'canary', 'p2p', 'warm', 'numerics')
#: the steps whose failure stops the checks and keeps the machine out (p2p only sets ``p2p``)
BLOCKING = ('env', 'canary', 'warm', 'numerics')
DEFAULT_TIMEOUT_MIN = 120  # a warm-up or numerics check
EXIT_FAILED = 1
#: the vendor words a card's name drops before its model
_VENDOR = {'nvidia', 'geforce', 'rtx', 'tesla', 'quadro', 'titan'}
_BARE = re.compile(r'[A-Za-z0-9_-]+\Z')


class AddError(Exception):
    """The machine cannot be added (or made ready) as asked."""


# ------------------------------------------------------------------ probing the machine


def probe_machine(python: str, work: str, lock: str, count: int, cache: str) -> dict:
    """What the entry needs, read on the machine: its hostname, cores, ``MemTotal``, the
    cards (``nvidia-smi``: name, MiB) and the driver's CUDA major, and what is missing there
    yet. Self-contained (its source runs on the machine). Stdlib."""
    import os
    import re
    import socket
    import subprocess
    out = {'hostname': socket.gethostname(), 'cpus': os.cpu_count() or 0, 'memory_gib': 0, 'cards': [],
           'cuda_major': 0, 'missing': []}
    with open('/proc/meminfo') as f:
        for line in f:
            if line.startswith('MemTotal:'):
                out['memory_gib'] = int(line.split()[1]) // (1 << 20)
    try:
        r = subprocess.run(['nvidia-smi', '--query-gpu=name,memory.total', '--format=csv,noheader,nounits'],
                           capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            out['cards'] = [[a.strip(), int(b)] for a, b in (x.rsplit(',', 1) for x in r.stdout.splitlines() if x.strip())]
            h = subprocess.run(['nvidia-smi'], capture_output=True, text=True, timeout=60)
            m = re.search(r'CUDA (?:UMD )?Version:\s*(\d+)', h.stdout)  # 'CUDA UMD Version' from driver 610
            out['cuda_major'] = int(m.group(1)) if m else 0
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    if not (os.path.isfile(python) and os.access(python, os.X_OK)):
        out['missing'].append(f'the env\'s python {python} (the CI account\'s env build)')
    if not (os.path.isdir(work) and os.access(work, os.W_OK)):
        out['missing'].append(f'the work directory {work} (writable)')
    stem = lock[:-len('.lock')] if lock.endswith('.lock') else lock
    gone = [p for p in [lock] + [f'{stem}.{i}.lock' for i in range(2, count + 1)] if not os.path.exists(p)]
    if gone:
        out['missing'].append(f'{len(gone)} CI slot file(s) (the setup plan\'s root step)')
    if cache and not os.path.isdir(cache):
        out['missing'].append(f'the compile cache {cache} (made by its first run)')
    return out


def probe(access: str, ssh: str, args: Mapping, run: pool.Runner = subprocess.run, host_python: str = '') -> dict:
    """``probe_machine`` on the machine, run by ``host_python`` (default ``pool.HOST_PYTHON``):
    through ssh, or here for ``local``."""
    src = (inspect.getsource(probe_machine) + '\nimport json\nprint(json.dumps(probe_machine('
           f'{args["python"]!r}, {args["work"]!r}, {args["lock"]!r}, {int(args["count"])}, {args["cache"]!r})))\n')
    m = pool.Machine('probe', access, ssh=ssh)
    try:
        r = run(pool._remote(m, [host_python or pool.HOST_PYTHON, '-I', '-']), input=src, capture_output=True, text=True,
                timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        raise AddError(f'probe failed: {type(e).__name__}') from None
    if r.returncode != 0:
        raise AddError(f'probe failed (exit {r.returncode}): {(r.stderr or "").strip()[-300:]}')
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise AddError('probe answer not JSON') from None


def gpu_model(name: str) -> str:
    """A card's model as the registry names it, from ``nvidia-smi``'s name: ``NVIDIA GeForce
    RTX 3090`` -> ``3090``, ``Tesla V100-SXM2-32GB`` -> ``v100``. Pure."""
    words = [w for w in re.split(r'[\s_]+', name.lower()) if w and w not in _VENDOR]
    model = re.sub(r'[^a-z0-9]', '', words[0].split('-')[0]) if words else ''
    if not model:
        raise AddError(f'card {name!r}: no model in its name')
    return model


def entry(access: str, ssh: str, p: Mapping, *, python: str, work: str, lock: str, count: int, queue: str = '',
          labels: Mapping[str, str] | None = None, cache: str = '', host_python: str = '') -> dict:
    """The ``[machine.KEY]`` table of a host from its probe ``p``, not ready. Pure."""
    e: dict = {'access': access}
    if access == 'ssh':
        e['ssh'] = ssh
    e.update(hostname=p['hostname'], cpus=int(p['cpus']), memory_gib=int(p['memory_gib']), gpus=len(p['cards']))
    if p['cards']:
        models = {gpu_model(n) for n, _ in p['cards']}
        if len(models) > 1:
            raise AddError(f'cards of more than one model {sorted(models)}: one entry has one model')
        e.update(gpu_model=models.pop(), gpu_mib=min(int(mib) for _, mib in p['cards']))
        if p.get('cuda_major'):
            e['cuda'] = f'cuda{int(p["cuda_major"])}'
    e.update(p2p=False, python=python, **({'host_python': host_python} if host_python else {}), work=work,
             slots={'lock': lock, 'count': int(count), **({'queue': queue} if queue else {})})
    if labels:
        e['labels'] = dict(labels)
    if cache:
        e['cache'] = cache
    e.update(numerics_ok=False, ready=False)
    return e


# ------------------------------------------------------------------ the registry's text


def _key(k: str) -> str:
    return k if _BARE.fullmatch(k) else json.dumps(k)


def _value(v: object) -> str:
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        return json.dumps(v)  # a TOML basic string: JSON's escapes are TOML's
    if isinstance(v, dict):
        return '{ ' + ', '.join(f'{_key(k)} = {_value(x)}' for k, x in v.items()) + ' }'
    raise TypeError(f'no TOML for {type(v).__name__}')


def render(key: str, e: Mapping) -> str:
    """The entry as TOML text. Pure."""
    return f'[machine.{key}]\n' + ''.join(f'{_key(k)} = {_value(v)}\n' for k, v in e.items())


def add_text(text: str, key: str, e: Mapping) -> str:
    """``text`` (the registry) with the entry appended, the whole validated: an existing
    key, or a host already there under another key, is refused. Pure."""
    reg = pool.load(text)
    if key in reg.machines:
        raise AddError(f'machine {key} is in the registry already')
    same = [k for k, m in reg.machines.items() if m.access != 'slurm' and m.hostname and m.hostname == e['hostname']]
    if same:
        raise AddError(f'{e["hostname"]} is in the registry already, as {same[0]}')
    new = text + ('' if text.endswith('\n') or not text else '\n') + '\n' + render(key, e)
    try:
        got = pool.load(new).machines[key]
    except pool.RegistryError as err:
        raise AddError(f'the new entry does not validate: {err}') from None
    if got.ready:
        raise AddError('the new entry would be ready')
    return new


def ready_text(text: str, key: str, p2p: bool) -> str:
    """``text`` with ``[machine.KEY]``'s ``ready`` and ``numerics_ok`` true and ``p2p`` set,
    every other line kept; the result must load to the same registry but those three keys.
    Pure."""
    lines = text.splitlines(keepends=True)
    head = re.compile(rf'\s*\[\s*machine\.{re.escape(key)}\s*\]\s*(#.*)?$')
    start = next((i for i, x in enumerate(lines) if head.match(x)), None)
    if start is None:
        raise AddError(f'no [machine.{key}] table in the registry (an entry pool add did not write?)')
    end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith('[')), len(lines))
    body = [x for x in lines[start + 1:end] if not re.match(r'\s*(ready|numerics_ok|p2p)\s*=', x)]
    while body and not body[-1].strip():
        body.pop()
    if body and not body[-1].endswith('\n'):
        body[-1] += '\n'
    tail = lines[end:]
    new = ''.join(lines[:start + 1] + body + [f'p2p = {_value(p2p)}\n', 'numerics_ok = true\n', 'ready = true\n']
                  + (['\n'] if tail else []) + tail)
    old_reg, new_reg = pool.load(text), pool.load(new)
    want = dict(old_reg.machines)
    want[key] = dataclasses.replace(want[key], ready=True, numerics_ok=True, p2p=p2p)
    if dict(new_reg.machines) != want or dataclasses.replace(new_reg, machines={}) != dataclasses.replace(old_reg, machines={}):
        raise AddError(f'[machine.{key}] could not be edited in place; set ready, numerics_ok and p2p by hand')
    return new


def write_atomic(path: str, text: str) -> None:
    """Replace ``path`` with ``text``, mode 600, in one rename."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or '.', prefix='.' + os.path.basename(path) + '.')
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


class _Locked:
    """The registry's edits one at a time (``pool.toml.lock`` beside it)."""

    def __init__(self, etc: str) -> None:
        self.path = os.path.join(etc, 'pool.toml.lock')

    def __enter__(self) -> _Locked:
        self.fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_CLOEXEC, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc) -> None:
        os.close(self.fd)


# ------------------------------------------------------------------ the record of the checks


def digest(reg: pool.Registry, key: str) -> str:
    """What the checks' record holds for: the entry (but ``ready``, ``numerics_ok`` and
    ``p2p``, which the record sets) and the registry's ``check``, ``warm`` and ``numerics``.
    Pure."""
    m = dataclasses.asdict(dataclasses.replace(reg.machines[key], ready=False, numerics_ok=False, p2p=False))
    blob = json.dumps({'machine': m, 'check': list(reg.check), 'warm': reg.warm, 'numerics': reg.numerics},
                      sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def required(reg: pool.Registry, m: pool.Machine) -> list[str]:
    """The steps a machine's checks run. Pure."""
    return [s for s in STEPS if s != 'p2p' or m.gpus >= 2]


def not_passed(rec: Mapping | None, reg: pool.Registry, key: str) -> list[str]:
    """Why the record does not make the machine ready ('' each step passed for the entry
    as it is now). Pure."""
    m = reg.machines[key]
    out = [f'the registry names no {s} command (its top-level {s!r})' for s in ('warm', 'numerics')
           if not getattr(reg, s)]
    if not rec:
        return out + ['no check has run (pool check ' + key + ')']
    if rec.get('digest') != digest(reg, key):
        return out + ['the entry or the registry\'s commands changed since the checks ran (pool check ' + key + ')']
    steps = rec.get('steps', {})
    for s in required(reg, m):
        st = steps.get(s)
        if not st:
            out.append(f'{s}: not run')
        elif not st.get('ok') and s in BLOCKING:
            out.append(f'{s}: failed ({st.get("why") or "?"})')
    return out


def read_state(etc: str) -> dict:
    try:
        with open(os.path.join(etc, STATE), encoding='utf-8') as f:
            v = json.load(f)
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


# ------------------------------------------------------------------ the checks


def canary_command(cards: int) -> str:
    """jax imports and sees ``cards`` cards (none on a CPU machine). Pure."""
    code = ('import jax\n'
            'n = sum(d.platform == "gpu" for d in jax.devices())\n'
            f'print(f"canary: jax {{jax.__version__}} on {{jax.default_backend()}}: {{n}} card(s), {cards} expected")\n'
            f'raise SystemExit(0 if n == {cards} else 1)\n')
    return shlex.join(['python', '-I', '-c', code])


def p2p_command() -> str:
    """The P2P self-test (``p2p.py``, stdlib), its source passed whole: it does not depend on
    where a run stages this package. Pure."""
    from ci_runners.resources import p2p
    return shlex.join(['python', '-I', '-c', inspect.getsource(p2p)])


@dataclasses.dataclass
class Step:
    name: str
    command: str
    ask: pool.Ask
    timeout_min: float
    variables: Mapping[str, str]


def steps(reg: pool.Registry, m: pool.Machine, timeout_min: float = DEFAULT_TIMEOUT_MIN) -> list[Step]:
    """The pool runs of the checks after ``env``, in order. Pure."""
    cards = {'CUDA_VISIBLE_DEVICES': ','.join(str(i) for i in range(m.gpus))} if m.gpus else {}
    cache = {'CI_POOL_CACHE': m.cache} if m.cache else {}
    small = pool.Ask(cpus=1, memory_gib=min(4, m.memory_gib) or 1)
    big = pool.Ask(cpus=min(8, m.cpus) or 1, memory_gib=min(32, m.memory_gib) or 1)
    out = [Step('canary', canary_command(m.gpus), small, 10, {**cards, 'XLA_PYTHON_CLIENT_PREALLOCATE': 'false'})]
    if m.gpus >= 2:
        out.append(Step('p2p', p2p_command(), small, 10, cards))
    out += [Step(s, getattr(reg, s), big, timeout_min, {**cards, **cache}) for s in ('warm', 'numerics')]
    return out


def _say(msg: str) -> None:
    print(f'pool add: {msg}', file=sys.stderr, flush=True)


def env_check(reg: pool.Registry, m: pool.Machine, python: str, run: pool.Runner = subprocess.run) -> str:
    """The env check of the machine's env against ``python``'s: '' or why it failed."""
    prefix = [] if m.access == 'local' else pool._ssh(m.ssh)
    try:
        diff = pool.env_diff(pool.fingerprint([], python, reg.check, run),
                             pool.fingerprint(prefix, m.python, reg.check, run))
    except (RuntimeError, ValueError, IndexError, OSError, subprocess.SubprocessError) as e:
        return f'could not run: {e}'
    return '; '.join(diff)


def run_on(reg: pool.Registry, key: str, s: Step, root: str, python: str, run: pool.Runner = subprocess.run,
           pool_run: Callable = pool.pool_run) -> int:
    """``pool run --on KEY --here`` of step ``s`` from the checkout ``root``."""
    one = dataclasses.replace(reg, machines={key: reg.machines[key]})
    rows = pool.status(one, run)
    a = pool.RunArgs(s.command, root, s.ask, s.timeout_min, pool.DEFAULT_QUEUE_MIN, False, 'refuse', python,
                     dict(s.variables))
    return pool_run(reg, a, only=key, here_ok=True, run=run, rows=rows)


def check(reg: pool.Registry, key: str, etc: str, root: str, *, python: str = '', only: Sequence[str] = (),
          timeout_min: float = DEFAULT_TIMEOUT_MIN, dry_run: bool = False, run: pool.Runner = subprocess.run,
          pool_run: Callable = pool.pool_run, now: Callable[[], float] = time.time) -> int:
    """Run the checks of machine ``key`` in order, each recorded in ``pool-add.json``; the
    first failure of a blocking step stops them. 0 when every step run passed."""
    m = reg.machines[key]
    if m.access == 'slurm':
        raise AddError(f'{key} is a cluster: its targets pass the cluster\'s own gates (slurm canary)')
    python = python or sys.executable
    plan = {s.name: s for s in steps(reg, m, timeout_min)}
    names = [s for s in required(reg, m) if not only or s in only]
    if dry_run:
        for n in names:
            if n == 'env':
                print(f'env: the versions of {", ".join(reg.check)} in {m.python} against {python}\'s')
            elif not plan[n].command:
                print(f'{n}: the registry names no {n} command (its top-level {n!r})')
            else:
                print(f'{n}: pool run --on {key}: {plan[n].command[:300]}')
        return 0
    path = os.path.join(etc, STATE)
    d = digest(reg, key)
    with _Locked(etc):
        state = read_state(etc)
        rec = state.get(key) or {}
        if rec.get('digest') != d:  # the entry or the commands changed: an earlier record holds for nothing
            rec = {'digest': d, 'steps': {}}
        state[key] = rec
        write_atomic(path, json.dumps(state, indent=1, sort_keys=True) + '\n')

    def record(name: str, ok: bool, why: str = '') -> None:
        with _Locked(etc):
            st = read_state(etc)
            r = st.get(key) or {'digest': d, 'steps': {}}
            if r.get('digest') != d:
                r = {'digest': d, 'steps': {}}
            r['steps'][name] = {'ok': ok, 'at': int(now()), **({'why': why} if why else {})}
            st[key] = r
            write_atomic(path, json.dumps(st, indent=1, sort_keys=True) + '\n')
    for n in names:
        if n == 'env':
            why = env_check(reg, m, python, run)
        elif not plan[n].command:
            why = f'the registry names no {n} command (its top-level {n!r})'
        else:
            _say(f'{n}: pool run --on {key}')
            rc = run_on(reg, key, plan[n], root, python, run, pool_run)
            why = '' if rc == 0 else (f'no CI slot or the machine was skipped (exit {rc})' if rc == pool.EXIT_NO_ROOM
                                      else f'exit {rc}')
        record(n, not why, why)
        _say(f'{n}: ' + ('passed' if not why else f'failed: {why}'))
        if why and n in BLOCKING:
            return EXIT_FAILED
    return 0


def next_steps(reg: pool.Registry, key: str) -> str:
    """What the lead does, printed, never done here. Pure."""
    m = reg.machines[key]
    where = 'on the machine' + (f' ({m.ssh})' if m.access == 'ssh' else '')
    labels = ', '.join(f'{g}={x}' for g, x in m.labels.items()) or 'none given'
    return '\n'.join([
        f'next steps for {key} (the lead\'s; nothing here applies them):',
        f'  1. runners: {where}, as the CI account, give its runners their capability labels ({labels}) in its',
        '     inventory.toml, then `setup/host/plan.sh ROLE` (a dry run); the lead approves and applies it',
        '     (`setup/host/apply.sh ROLE --plan FILE`; its root step makes the CI slot files) and registers the',
        '     runners (`setup/host/register.sh RUNNER...`).',
        f'  2. the env: {where}, `ci-run env build {reg.project} --dry-run`; the lead runs the build.',
        '  3. the new names: `python -m ci_runners.pools.pool deny $CI_RUNNERS_ETC/pool.toml` into tests/deny.sha256.',
        f'  4. `ci-run pool check {key}` until every step passes, then `ci-run pool ready {key}`.'])


def make_ready(etc: str, key: str) -> int:
    """``pool ready KEY``: ready only when the record says every step passed."""
    path = os.path.join(etc, 'pool.toml')
    with _Locked(etc):
        reg = pool.load_file(path)
        if key not in reg.machines:
            raise AddError(f'no machine {key} in the registry')
        if reg.machines[key].ready:
            print(f'{key} is ready already')
            return 0
        rec = read_state(etc).get(key)
        why = not_passed(rec, reg, key)
        if why:
            _say(f'{key} is not ready:\n  ' + '\n  '.join(why))
            return EXIT_FAILED
        p2p = bool(rec['steps'].get('p2p', {}).get('ok'))
        with open(path, encoding='utf-8') as f:
            text = f.read()
        write_atomic(path, ready_text(text, key, p2p))
    print(f'{key} is ready: numerics_ok = true, p2p = {str(p2p).lower()}')
    return 0


# ------------------------------------------------------------------ CLI


def _labels(s: str) -> dict[str, str]:
    out = {}
    for kv in (x for x in s.split(',') if x):
        g, _, label = kv.partition('=')
        if not g or not label:
            raise ValueError(f'--labels {s!r}: GROUP=LABEL,...')
        out[g] = label
    return out


def _root(d: str) -> str:
    return subprocess.run(['git', '-C', d, 'rev-parse', '--show-toplevel'], capture_output=True, text=True).stdout.strip()


def add(etc: str, key: str, access: str, ssh: str, *, python: str, work: str, lock: str, count: int, queue: str = '',
        labels: Mapping[str, str] | None = None, cache: str = '', host_python: str = '', dry_run: bool = False,
        run: pool.Runner = subprocess.run) -> pool.Registry:
    """Probe the machine and write its entry (not ready); returns the new registry."""
    path = os.path.join(etc, 'pool.toml')
    if not pool._KEY.fullmatch(key):
        raise AddError(f'{key!r}: a key is [a-z0-9][a-z0-9_-]*')
    with _Locked(etc):
        try:
            pool.load_file(path)
            with open(path, encoding='utf-8') as f:
                text = f.read()
        except FileNotFoundError:
            raise AddError(f'{path}: no registry (write its top level, schema, project and repository, first)') from None
        if key in pool.load(text).machines:  # before the probe: a refusal costs no ssh
            raise AddError(f'machine {key} is in the registry already')
        p = probe(access, ssh, {'python': python, 'work': work, 'lock': lock, 'count': count, 'cache': cache}, run,
                  host_python)
        e = entry(access, ssh, p, python=python, work=work, lock=lock, count=count, queue=queue, labels=labels,
                  cache=cache, host_python=host_python)
        new = add_text(text, key, e)
        for x in p.get('missing', []):
            _say(f'not there yet: {x}')
        if dry_run:
            print(render(key, e), end='')
            _say('dry run: the registry is unchanged')
        else:
            write_atomic(path, new)
            _say(f'[machine.{key}] written to {path} (ready = false)')
    return pool.load(new)


def main(argv: Sequence[str], etc: str, run: pool.Runner = subprocess.run, pool_run: Callable = pool.pool_run) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog='ci-run pool')
    sub = ap.add_subparsers(dest='cmd', required=True)
    a_ = sub.add_parser('add', help='probe a host and write its entry, not ready; then its checks')
    a_.add_argument('key')
    acc = a_.add_mutually_exclusive_group(required=True)
    acc.add_argument('--ssh', default='', help='its ssh alias')
    acc.add_argument('--local', action='store_true', help='this host')
    a_.add_argument('--python', required=True, help='the env a command runs in there')
    a_.add_argument('--work', required=True, help='where a run stages its tree there')
    a_.add_argument('--host-python', default='', help=f'the host side\'s python there, 3.11 or later '
                    f'(default: {pool.HOST_PYTHON})')
    a_.add_argument('--slots-lock', required=True)
    a_.add_argument('--slots-count', type=int, required=True)
    a_.add_argument('--slots-queue', default='')
    a_.add_argument('--labels', default='', help='its runner labels, GROUP=LABEL,...')
    a_.add_argument('--cache', default='', help='its compile cache')
    c_ = sub.add_parser('check', help='run the checks of a machine again')
    c_.add_argument('key')
    c_.add_argument('--steps', default='', help=f'some of {",".join(STEPS)}')
    for p in (a_, c_):
        p.add_argument('--dir', default='.', help='the checkout the checks run in (default: .)')
        p.add_argument('--caller-python', default='', help='your env\'s python, the env check\'s reference '
                       '(default: the one running this)')
        p.add_argument('--timeout', type=float, default=DEFAULT_TIMEOUT_MIN, help='min, a warm-up or numerics check')
        p.add_argument('--dry-run', action='store_true', help='print, change nothing, run no check')
    r_ = sub.add_parser('ready', help='mark a machine ready once its checks passed')
    r_.add_argument('key')
    a = ap.parse_args(list(argv))
    try:
        if a.cmd == 'ready':
            return make_ready(etc, a.key)
        only = [x for x in getattr(a, 'steps', '').split(',') if x]
        if set(only) - set(STEPS):
            ap.error(f'--steps: some of {",".join(STEPS)}')
        root = _root(a.dir)
        if not root:
            ap.error(f'{a.dir}: not a git checkout')
        if a.cmd == 'add':
            reg = add(etc, a.key, 'local' if a.local else 'ssh', a.ssh, python=a.python, work=a.work, lock=a.slots_lock,
                      count=a.slots_count, queue=a.slots_queue, labels=_labels(a.labels), cache=a.cache,
                      host_python=a.host_python, dry_run=a.dry_run, run=run)
        else:
            reg = pool.load_file(os.path.join(etc, 'pool.toml'))
            if a.key not in reg.machines:
                raise AddError(f'no machine {a.key} in the registry')
        rc = check(reg, a.key, etc, root, python=a.caller_python, only=only, timeout_min=a.timeout, dry_run=a.dry_run, run=run,
                   pool_run=pool_run)
        print(next_steps(reg, a.key))
        return rc
    except (AddError, pool.RegistryError, OSError, ValueError) as e:
        _say(str(e))
        return pool.EXIT_USAGE
