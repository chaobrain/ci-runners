"""Dynamic CPU selection within fixed private runner slots."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from tests import test_part_slots as fixture
from ci_runners import inventory, part, presence


class Executor(fixture.SlotHost):
    def test_dynamic_core_count_controls_affinity_but_omission_does_not(self):
        pools = ('pools = { light = 1, part = { count = 1, labels = ["ci-cpu", "ci-cpu-dynamic"], '
                 'slot = { cpus = ["0-1"], workers = 3, memory = "1500M", speed = 1.0 } } }')
        self.inv = inventory.load(self.base.replace(fixture.UNSLOTTED, pools))
        env = self.runner(slot='0')
        cmd = 'printf "%s:%s" "$CI_CPUS" "$CI_WORKERS" > "$CI_PART_DIR/value"; grep Cpus_allowed_list /proc/self/status > "$CI_PART_DIR/affinity"'
        for request, seen, actual in (({}, '0-1:3', '0-1'), ({'cores': 1}, '0:1', '0'),
                                      ({'cores': 2, 'workers': 1}, '0-1:1', '0-1')):
            with self.subTest(request=request):
                _, out = self.go(env, id=f'c{len(request)}', command=cmd, **request)
                self.assertEqual(out['outcome'], 'pass')
                self.assertEqual(self.work(out, 'value'), seen)
                self.assertEqual(self.work(out, 'affinity').split()[-1], actual)
        for n, override, reason, kind in ((3, None, 'only 2 slot CPUs', 'cores'),
                                           (1, 2, 'allocated CPUs 1', 'workers')):
            _, out = self.go(env, id=f'over{n}', cores=n, workers=override)
            self.assertEqual(out['outcome'], 'unavailable')
            self.assertIn(reason, out['reason'])
            self.assertFalse(out[kind]['command_started'])
            self.assertFalse((Path(self.path(out)) / 'work' / 'ran').exists())
            self.assertEqual(presence.part_end(out, {'class': 'part'}, ''), '')

    def test_ordinary_runner_rejects_explicit_cores(self):
        _, out = self.go(self.runner(slot='0'), cores=1)
        self.assertEqual(out['outcome'], 'unavailable')
        self.assertFalse(out['cores']['command_started'])
        self.assertFalse((Path(self.path(out)) / 'work' / 'ran').exists())

    def test_concurrent_parts_meet_in_disjoint_slots_and_refuse_too_narrow_scope(self):
        # Each child must start and record its affinity before the other may exit.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            argv = [sys.executable, '-c', """import os, sys
from ci_runners import part
cpu = int(sys.argv[2])
os.sched_setaffinity(0, {cpu})
spec = (int(sys.argv[3]), 3, [cpu], None)
raise SystemExit(part.supervise(sys.argv[1], 6, sys.argv[4:], core_spec=spec))
"""]
            command = """import os, sys, time
from pathlib import Path
own, peer = map(Path, sys.argv[1:])
own.write_text(str(sorted(os.sched_getaffinity(0))))
until = time.monotonic() + 5
while not peer.exists() and time.monotonic() < until:
    time.sleep(.01)
assert peer.exists()
"""
            procs = []
            try:
                for i in (0, 1):
                    run = root / str(i)
                    run.mkdir()
                    procs.append(subprocess.Popen([*argv, str(run), str(i), '1', sys.executable, '-c', command,
                                                  str(root / f'cpu{i}'), str(root / f'cpu{1-i}')]))
                for proc in procs:
                    self.assertEqual(proc.wait(timeout=10), 0)
                self.assertEqual([(root / f'cpu{i}').read_text() for i in (0, 1)], ['[0]', '[1]'])
                run = root / 'narrow'
                run.mkdir()
                marker = root / 'must-not-start'
                subprocess.run([*argv, str(run), '0', '2', sys.executable, '-c',
                                'from pathlib import Path; import sys; Path(sys.argv[1]).touch()', str(marker)],
                               check=True, timeout=10)
                ex = json.loads((run / 'exit.json').read_text())
                self.assertFalse(ex['cores']['command_started'])
                self.assertFalse(marker.exists())
            finally:
                for proc in procs:
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait()


class Wiring(unittest.TestCase):
    def test_both_attempts_forward_the_original_core_count(self):
        workflow = (fixture.ROOT / '.github/workflows/part.yml').read_text(encoding='utf-8')
        action = (fixture.ROOT / 'actions/part/action.yml').read_text(encoding='utf-8')
        expr = '$' + '{{ fromJSON(inputs.part).cores }}'
        for segment in workflow.split('command: $' + '{{ fromJSON(inputs.part).command }}')[1:]:
            self.assertIn(f'cores: {expr}', segment)
        self.assertEqual(workflow.count(f'cores: {expr}'), 2)
        self.assertIn('IN_CORES: $' + '{{ inputs.cores }}', action)
        self.assertIn('set -- "$@" --cores "$IN_CORES"', action)
        argv = ['--project', 'p', '--id', 'f', '--command', 'true', '--timeout-minutes', '1',
                '--cores', '1000000000000000001', '--workers', '4']
        parsed = part.parse_args(argv)
        self.assertEqual((parsed.cores, parsed.workers), (10**18 + 1, 4))

    def test_scope_must_read_back_the_exact_selected_mask(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(part.os, 'setsid'), \
             mock.patch.object(part.signal, 'signal'), \
             mock.patch.object(part.os, 'sched_getaffinity', side_effect=[{0, 1, 2}, {2}]), \
             mock.patch.object(part.os, 'sched_setaffinity') as pin, \
             mock.patch.object(part.subprocess, 'Popen') as popen:
            part.supervise(d, 2, ['must-not-start'], core_spec=(1, 2, [1, 2], None))
            pin.assert_called_once_with(0, {1})
            popen.assert_not_called()
            ex = json.loads((Path(d) / 'exit.json').read_text())
            self.assertFalse(ex['cores']['command_started'])
            self.assertIn('does not match', ex['unavailable'])
