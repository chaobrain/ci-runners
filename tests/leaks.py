"""No test run leaves a process behind.

A run is marked by ``CI_RUNNERS_TEST_RUN=<id>`` in its environment, which every process
it starts inherits, also one in a new session or re-parented to init. After the run
ends, or hangs and is stopped, every process with the mark is a leftover: it is listed
and killed. Linux only (``/proc/<pid>/environ``); elsewhere nothing is found.

CLI, for CI::

    python tests/leaks.py [--timeout S] -- CMD [ARG...]

runs CMD with a fresh mark and exits with its status, or 1 if a process of the run
outlived it, or 124 if it ran past the timeout (its whole tree is killed either way).
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import uuid

ENV = 'CI_RUNNERS_TEST_RUN'
GRACE = 2.0  # seconds a just-killed process may take to go before it counts as left behind


def marked(run: str) -> list[tuple[int, str]]:
    """``(pid, command line)`` of the live processes carrying the mark of ``run``."""
    tag = f'{ENV}={run}'.encode()
    out = []
    try:
        pids = [int(p) for p in os.listdir('/proc') if p.isdigit()]
    except OSError:
        return out
    for pid in pids:
        if pid == os.getpid():
            continue
        try:
            with open(f'/proc/{pid}/environ', 'rb') as f:
                env = f.read().split(b'\0')
            with open(f'/proc/{pid}/cmdline', 'rb') as f:
                cmd = f.read().replace(b'\0', b' ').decode(errors='replace').strip()
        except OSError:  # gone, or not ours
            continue
        if tag in env:
            out.append((pid, cmd))
    return out


def reap(run: str, grace: float = GRACE) -> list[tuple[int, str]]:
    """Wait up to ``grace`` for the run's processes to go, then kill the rest. Returns
    what was still there after the grace (the leftovers)."""
    end = time.monotonic() + grace
    left = marked(run)
    while left and time.monotonic() < end:
        time.sleep(0.05)
        left = marked(run)
    for _ in range(50):
        now = marked(run)
        if not now:
            break
        for pid, _ in now:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        time.sleep(0.05)
    return left


def run(cmd: list[str], *, timeout: float | None = None, quiet: bool = False,
        cwd: str | os.PathLike | None = None) -> tuple[int, list[tuple[int, str]]]:
    """Run ``cmd`` marked; ``(status, leftovers)``. A timeout gives status 124, and the
    whole marked tree is killed then too."""
    mark = uuid.uuid4().hex
    env = dict(os.environ, **{ENV: mark})
    out = subprocess.DEVNULL if quiet else None
    p = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=out)
    try:
        code = p.wait(timeout)
        return code, reap(mark)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()
        reap(mark, grace=0)
        return 124, []
    except BaseException:
        p.kill()
        p.wait()
        reap(mark, grace=0)
        raise


def main(argv: list[str]) -> int:
    timeout = None
    if argv[:1] == ['--timeout']:
        timeout, argv = float(argv[1]), argv[2:]
    if argv[:1] == ['--']:
        argv = argv[1:]
    if not argv:
        print('usage: leaks.py [--timeout S] -- CMD [ARG...]', file=sys.stderr)
        return 2
    code, left = run(argv, timeout=timeout)
    if code == 124:
        print(f'leaks: {argv[0]} ran past {timeout:g} s; its processes were killed', file=sys.stderr)
        return 124
    for pid, cmd in left:
        print(f'leaks: left behind: {pid} {cmd[:200]}', file=sys.stderr)
    if left:
        print(f'leaks: {len(left)} process(es) outlived the run (killed now)', file=sys.stderr)
        return 1
    return code


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
