"""The drain switch (design decision 13.1): a drained host or pool takes no new part.

The state is one file, ``<data>/ci-runners/state/drain.json``: ``{"host": <epoch or
null>, "pools": {<pool key>: <epoch>}}``, replaced whole (a rename) under
``state/drain.lock``, so it survives a restart. A missing file is "not drained"; an
unreadable or malformed one drains the host (fail closed). ``part`` reads it in its gate
and again just before the command (``refusal``); ``claim``, ``salvage`` and ``verdict``
never read it.

CLI (as the CI account)::

    ci-run drain on [--pool P]     drain the host, or one pool (its inventory key or class)
    ci-run drain off [--pool P]    undrain the host and every pool, or that pool only
    ci-run drain status            the state, and the part scopes still running
"""
from __future__ import annotations

import fcntl
import json
import os
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Mapping

from ci_runners import inventory

UNREADABLE = 'drain state unreadable'


def state_path(data: str) -> str:
    return os.path.join(data, 'ci-runners', 'state', 'drain.json')


def parse(text: str) -> dict | None:
    """The state from the file's text, or None if it is malformed. Pure."""
    try:
        doc = json.loads(text)
    except ValueError:
        return None
    if not isinstance(doc, dict) or set(doc) != {'host', 'pools'}:
        return None
    host, pools = doc['host'], doc['pools']
    num = (int, float)
    if not (host is None or (type(host) in num and not isinstance(host, bool))):
        return None
    if not isinstance(pools, dict) or not all(isinstance(k, str) and type(v) in num and not isinstance(v, bool)
                                              for k, v in pools.items()):
        return None
    return {'host': host, 'pools': dict(pools)}


def read(data: str) -> dict | None:
    """The drain state: ``{'host': None, 'pools': {}}`` when the file is missing, None
    when it cannot be read or is malformed (the caller fails closed)."""
    try:
        fd = os.open(state_path(data), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return {'host': None, 'pools': {}}
    except OSError:
        return None
    try:
        with os.fdopen(fd, 'rb') as f:
            st = os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_size > 1 << 16:
                return None
            return parse(f.read(1 << 16).decode('utf-8', 'replace'))
    except OSError:
        return None


def refusal(state: dict | None, host: Mapping[str, object], cls: str) -> str:
    """Why a part of runner class ``cls`` may not start here, or empty: the host or the
    class's pool is drained, or the state is unreadable. Pure."""
    if state is None:
        return UNREADABLE
    if state['host'] is not None:
        return 'drained: host'
    pool = inventory.pool_of(host, cls)
    if pool is not None and pool in state['pools']:
        return f'drained: pool {pool}'
    return ''


def pool_key(host: Mapping[str, object], name: str) -> str:
    """The inventory key of the pool ``name`` names, by key or by class; ValueError if
    none. Pure."""
    pools = host['pools']
    if name in pools:
        return name
    key = inventory.pool_of(host, name)
    if key is None:
        raise ValueError(f'no pool or class {name!r} on this host (pools: {", ".join(sorted(pools))})')
    return key


def change(data: str, on: bool, pool: str = '', now: Callable[[], float] = time.time) -> dict:
    """Set or clear the drain under the lock, and return the new state. ``off`` without a
    pool clears the host and every pool. An unreadable state is replaced: the operator's
    command is the fix for it."""
    d = os.path.dirname(state_path(data))
    os.makedirs(d, mode=0o700, exist_ok=True)
    lock = os.open(os.path.join(d, 'drain.lock'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        st = read(data) or {'host': None, 'pools': {}}
        if on and pool:
            st['pools'].setdefault(pool, now())
        elif on:
            st['host'] = st['host'] if st['host'] is not None else now()
        elif pool:
            st['pools'].pop(pool, None)
        else:
            st = {'host': None, 'pools': {}}
        tmp = f'{state_path(data)}.{os.getpid()}.new'
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(st, f, sort_keys=True)
            f.write('\n')
        os.replace(tmp, state_path(data))
        return st
    finally:
        os.close(lock)


def _when(t: float) -> str:
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(t))


def running_scopes(run: Callable = subprocess.run) -> list[str]:
    """The part scopes of the account still active (``ci-part-*.scope``)."""
    try:
        r = run(['systemctl', '--user', 'list-units', '--type=scope', '--state=active', '--no-legend', '--plain',
                 'ci-part-*'], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    return sorted(line.split()[0] for line in (r.stdout or '').splitlines() if line.split())


def status_text(state: dict | None, scopes: list[str]) -> str:
    """What ``drain status`` prints. Pure."""
    if state is None:
        lines = [f'host: drained ({UNREADABLE}; "drain off" rewrites it)']
    else:
        lines = [f'host: drained since {_when(state["host"])}' if state['host'] is not None else 'host: not drained']
        lines += [f'pool {p}: drained since {_when(t)}' for p, t in sorted(state['pools'].items())]
    lines.append(f'running part scopes: {len(scopes)}')
    lines += [f'  {s}' for s in scopes]
    return '\n'.join(lines) + '\n'


def main(argv: list[str], inv: inventory.Inventory, run: Callable = subprocess.run) -> int:
    usage = 'usage: ci-run drain on|off [--pool P] | ci-run drain status'
    if not argv or argv[0] not in ('on', 'off', 'status'):
        print(usage, file=sys.stderr)
        return 2
    data = str(inv.host['data'])
    if argv[0] == 'status':
        if len(argv) != 1:
            print(usage, file=sys.stderr)
            return 2
        sys.stdout.write(status_text(read(data), running_scopes(run)))
        return 0
    rest = argv[1:]
    if rest not in ([],) and not (len(rest) == 2 and rest[0] == '--pool'):
        print(usage, file=sys.stderr)
        return 2
    try:
        pool = pool_key(inv.host, rest[1]) if rest else ''
    except ValueError as e:
        print(f'ci-run drain: {e}', file=sys.stderr)
        return 2
    st = change(data, argv[0] == 'on', pool)
    sys.stdout.write(status_text(st, running_scopes(run)))
    return 0
