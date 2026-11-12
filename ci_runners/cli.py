"""``ci-run``: the host layer's commands (design §3.1).

    ci-run reap            the reaper's pass (ci-reaper.timer), which also rewrites a hook copy
    ci-run egress --port P --allow FILE   the egress proxy (ci-egress.service on the egress host; 11.10)
    ci-run org-sync [--fetch --org NAME | --receive]   the org snapshot (§6.5, decision 11.9)
    ci-run runner-watch --org O --state F [--report-only] HOST...   an issue when a host has no
                           runner online (the maintainer's timer; decision 15.6)
    ci-run plan|apply ...  host setup (setup/plan.sh, setup/apply.sh)
    ci-run runners         this host's runners: name, labels, class
    ci-run role            this host's role
    ci-run claim ...       claim a head (actions/claim; design §4.1)
    ci-run part ...        run one part (actions/part; design §4.3)
    ci-run verdict ...     post a run's verdict (actions/verdict; §4.8)
    ci-run salvage ...     this host's parts of a run that stop short (actions/salvage; §4.5)
    ci-run env build P     build project P's env from its default branch's spec (decision 11.4)
    ci-run drain on|off [--pool P] | status   the drain switch (decision 13.1)
    ci-run hostload sample|status   the load record and the busy rule (decision 13.2)
    ci-run presence [--dry-run] | check RUNNER   stop and start idle runners by their resources
                           (ci-presence.timer, the runner unit's ExecCondition; decision 16.4)
    ci-run hooks copy      the hooks' copies in the private /tmp (ci-hooks.service; decision 13.5)
    ci-run slurm check|plan|apply|env|canary|status|cost|uninstall ...   a Slurm site's accounts (decision 14.9)
    ci-run pool status|run|add|check|ready ...   the machine registry's live capacity, a command on a free
                           machine, and adding a host (decision 15.1)
    ci-run route [--rows FILE]   a plan's parts to capability labels, from the capacity (decision 16.5)
    ci-run flow contract|plan|retry|locate|status ...   the reusable workflow's own steps (actions/flow;
                           decision 16.6), run from the action's checkout

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
    if cmd == 'egress':  # on the egress host: no inventory there
        from ci_runners import bridge
        return bridge.main(args)
    if cmd == 'reap':
        from ci_runners import hookcopy, reaper
        h = load_inventory().host
        print(reaper.reap(h['data']))
        from ci_runners import slurmpart
        for line in slurmpart.reap(h['data'], etc_dir()):  # a lost slurm part's job (decision 14.4)
            print(line)
        line = hookcopy.refresh(h)  # a copy /tmp's cleaning took, or one that changed (decision 13.5)
        if line:
            print(line)
        return 0
    if cmd == 'pool':  # the registry is its own file: no inventory needed (an agent's account has none)
        from ci_runners import pool
        return pool.main(args, etc_dir())
    if cmd == 'route':  # the plan job's, on a light runner: the capacity, no inventory (decision 16.5)
        from ci_runners import route
        return route.main(args, etc_dir())
    if cmd == 'flow':  # the reusable workflow's steps: the capacity and the registry, read only (16.6)
        from ci_runners import flow
        return flow.main(args, etc_dir())
    if cmd == 'runner-watch':  # the maintainer's account: no inventory there (decision 15.6)
        from ci_runners import runnerwatch
        return runnerwatch.main(args)
    if cmd == 'org-sync':
        from ci_runners import orgsync
        return orgsync.cli(args, etc_dir())
    if cmd == 'claim':
        from ci_runners import github
        return github.main(args, etc_dir())
    if cmd == 'part':
        from ci_runners import part
        return part.main(args, load_inventory(), etc_dir())
    if cmd == 'verdict':
        from ci_runners import verdict
        return verdict.main(args, load_inventory())
    if cmd == 'salvage':
        from ci_runners import verdict
        return verdict.salvage_main(args, load_inventory())
    if cmd == 'env':
        from ci_runners import envbuild
        return envbuild.main(args, load_inventory(), etc_dir())
    if cmd == 'hostload':
        from ci_runners import hostload
        return hostload.main(args, load_inventory())
    if cmd == 'presence':
        from ci_runners import presence
        return presence.main(args, load_inventory)
    if cmd == 'hooks':
        from ci_runners import hookcopy
        return hookcopy.main(args, load_inventory().host)
    if cmd == 'drain':
        from ci_runners import drain
        return drain.main(args, load_inventory())
    if cmd == 'slurm':
        from ci_runners import slurmsetup
        return slurmsetup.main(args, load_inventory(), etc_dir())
    if cmd in ('plan', 'apply', 'runners'):
        from ci_runners import setup
        return setup.main([cmd, *args])
    print(f'ci-run: unknown command {cmd!r}', file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
