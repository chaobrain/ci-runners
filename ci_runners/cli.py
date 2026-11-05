"""``ci-run``: the host layer's commands (design §3.1).

    ci-run proxy-check     one check of the CI proxy (ci-proxy-check.timer)
    ci-run reap            the reaper's pass (ci-reaper.timer)
    ci-run plan|apply ...  host setup (setup/plan.sh, setup/apply.sh)
    ci-run runners         this host's runners: name, labels, class
    ci-run role            this host's role

The inventory is ``$CI_RUNNERS_ETC/inventory.toml``, else ``~/data/ci-runners/etc``.
"""
from __future__ import annotations

import os
import sys

from ci_runners import inventory


def etc_dir() -> str:
    return os.environ.get('CI_RUNNERS_ETC') or os.path.expanduser('~/data/ci-runners/etc')


def load_inventory() -> inventory.Inventory:
    with open(os.path.join(etc_dir(), 'inventory.toml'), encoding='utf-8') as f:
        return inventory.load(f.read())


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    cmd, args = argv[0], argv[1:]
    if cmd == 'role':
        print(load_inventory().this)
        return 0
    if cmd == 'proxy-check':
        from ci_runners import proxy
        h = load_inventory().host
        p = h['proxy']
        print(proxy.check(h['data'], p['http_port'], p['nodes']))
        return 0
    if cmd == 'reap':
        from ci_runners import reaper
        print(reaper.reap(load_inventory().host['data']))
        return 0
    if cmd in ('plan', 'apply', 'runners'):
        from ci_runners import setup
        return setup.main([cmd, *args])
    print(f'ci-run: unknown command {cmd!r}', file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
