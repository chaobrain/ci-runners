"""The CI proxy's check (design §5.3, §8): every 2 min a request to api.github.com through
the CI proxy; after 3 failures in a row, switch to the next allowed node and restart
the proxy. State: ``state/proxy.json``."""
from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
from collections.abc import Sequence

THRESHOLD = 3
URL = 'https://api.github.com/zen'


def step(state: dict, ok: bool, nodes: Sequence[str], threshold: int = THRESHOLD) -> tuple[dict, str | None]:
    """The next state after a check, and the node to switch to (None: stay). Pure."""
    node = state.get('node') if state.get('node') in nodes else nodes[0]
    if ok:
        return {'node': node, 'failures': 0}, None
    failures = int(state.get('failures', 0)) + 1
    if failures < threshold:
        return {'node': node, 'failures': failures}, None
    nxt = nodes[(list(nodes).index(node) + 1) % len(nodes)]
    return {'node': nxt, 'failures': 0}, nxt


def probe(http_port: int, timeout: float = 20, url: str = URL) -> bool:
    """One request through the CI proxy's HTTP port. Any HTTP answer from the far end
    means the proxy works: a 403 or 429 (the unauthenticated rate limit, shared by
    everyone behind the node's egress) is GitHub's, not the node's."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(
        {'https': f'http://127.0.0.1:{http_port}', 'http': f'http://127.0.0.1:{http_port}'}))
    try:
        with opener.open(url, timeout=timeout):
            return True
    except urllib.error.HTTPError:
        return True
    except OSError:  # no tunnel, refused, reset, timed out
        return False


def check(data: str, http_port: int, nodes: Sequence[str]) -> str:
    """Run one check and act on it; returns a log line."""
    etc_proxy = os.path.join(data, 'ci-runners', 'etc', 'proxy')
    path = os.path.join(data, 'ci-runners', 'state', 'proxy.json')
    try:
        with open(path) as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    ok = probe(http_port)
    state, switch = step(state, ok, nodes)
    if switch:
        active = os.path.join(etc_proxy, 'active.json')
        tmp = active + '.new'
        if os.path.lexists(tmp):
            os.unlink(tmp)
        os.symlink(os.path.join('nodes', f'{switch}.json'), tmp)
        os.replace(tmp, active)
        subprocess.run(['systemctl', '--user', 'restart', 'ci-proxy.service'], timeout=60)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + '.new', 'w') as f:
        json.dump(state, f)
    os.replace(path + '.new', path)
    return f'proxy check: {"ok" if ok else "failed"}, node {state["node"]}' + (f', switched to {switch}' if switch else '')
