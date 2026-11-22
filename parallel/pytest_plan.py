"""Generate a schema 2 plan from the actual pytest node IDs in a checkout."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import shlex
import sys

from parallel import protocol


class Collected:
    def pytest_collection_finish(self, session):
        self.nodes = [item.nodeid for item in session.items]


def plan(nodes, args):
    if not nodes:
        raise ValueError('pytest collected no tests')
    if len(nodes) != len(set(nodes)):
        raise ValueError('duplicate pytest node IDs; use non-overlapping test paths')
    sorted_ids = sorted(nodes)
    count = min(args.shards, len(sorted_ids))
    parts = []
    command = 'python -m pytest --rootdir=.'
    if args.workers > 1:
        command += ' -n "$CI_WORKERS"'
    command += ' --junitxml="$CI_RESULTS_DIR/pytest.xml" -- '
    for index in range(count):
        parts.append({
            'id': f'suite-{index + 1:02d}', 'needs': 'cpu', 'gpu': args.gpu,
            'cores': args.cores, 'workers': args.workers, 'minutes': args.minutes,
            'queue_minutes': args.queue_minutes, 'command': command + shlex.join(sorted_ids[index::count]),
            'results': 'pytest.xml',
        })
    result = {'schema': 2, 'parts': parts, 'max_parallel': count}
    protocol.load_plan(result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description='Collect pytest cases and distribute them by count over CI tasks')
    parser.add_argument('--shards', type=int, default=1)
    parser.add_argument('--cores', type=int, default=4)
    parser.add_argument('--workers', type=int, default=1, help='use pytest-xdist when greater than one')
    parser.add_argument('--minutes', type=int, default=20)
    parser.add_argument('--queue-minutes', type=int, default=15)
    parser.add_argument('--gpu', action='store_true', help='request a GPU for every generated task')
    parser.add_argument('paths', nargs='*', help='pytest paths or node IDs, relative to the checkout')
    args = parser.parse_args(argv)
    if not 1 <= args.shards <= 256:
        parser.error('--shards must be in [1, 256]')

    # Match python -m pytest: allow the checkout itself to supply the project imports.
    sys.path.insert(0, os.getcwd())
    collected = Collected()
    # Keep the plan stream separate from plugin, subprocess and native library output.
    with os.fdopen(os.dup(1), 'w', encoding='utf-8') as output:
        sys.stdout.flush()
        os.dup2(2, 1)
        try:
            with contextlib.redirect_stdout(sys.stderr):
                import pytest
                status = pytest.main(['--collect-only', '-q', '--rootdir=.', '-p', 'no:cacheprovider',
                                      '--', *args.paths], plugins=[collected])
            if status:
                return int(status)
            try:
                result = plan(collected.nodes, args)
            except ValueError as error:
                parser.error(str(error))
            print(json.dumps(result), file=output)
        finally:
            sys.stdout.flush()
            os.dup2(output.fileno(), 1)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
