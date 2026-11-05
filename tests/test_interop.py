"""Interop with brainspike's host lock and slots (design §7, D5; RUN-1 acceptance 2).

``ci-lock`` and ``host_lock.sh`` exclude each other in every mode pair, in both
directions, keep the turnstile's writer preference and the light entry across
implementations, and honour each other's nesting cover. ``ci-slot`` and ``ci_slot.sh``
together never hold more than the slot count.

The originals are copies of brainspike's ``scripts/host_lock.sh`` and
``scripts/ci_slot.sh`` (``tests/fixtures/brainspike/``, see its README); set
``CI_RUNNERS_INTEROP_SRC`` to a brainspike ``scripts/`` directory to test a live
checkout instead. Linux with bash and flock(1) only.
"""
from __future__ import annotations

import itertools
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = pathlib.Path(os.environ.get('CI_RUNNERS_INTEROP_SRC') or ROOT / 'tests' / 'fixtures' / 'brainspike')
HOST_LOCK_SH = SRC / 'host_lock.sh'
CI_SLOT_SH = SRC / 'ci_slot.sh'
CI_LOCK = ROOT / 'bin' / 'ci-lock'
CI_SLOT = ROOT / 'bin' / 'ci-slot'
PY = sys.executable

RUNNABLE = sys.platform.startswith('linux') and shutil.which('flock') and shutil.which('bash')


def wait_for(path: pathlib.Path, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if path.exists():
            return True
        time.sleep(0.01)
    return path.exists()


@unittest.skipUnless(RUNNABLE, 'Linux, bash and flock(1) only')
class LockInterop(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix='ci-lock-'))
        self.lock = str(self.dir / 'host.lock')
        self.env = dict(os.environ, LOCAL_CI_HOST_LOCK=self.lock, CI_RUNNERS_PYTHON=PY)
        self.env.pop('BRAINSPIKE_HOST_LOCK', None)
        self.env.pop('CI_HOST_LOCK', None)
        self.procs = []

    def tearDown(self):
        for p in self.procs:  # the holder and its command: kill the whole session
            try:
                os.killpg(p.pid, 9)
            except ProcessLookupError:
                pass
            p.wait()
        shutil.rmtree(self.dir, ignore_errors=True)

    def start(self, impl: str, mode: str, tag: str, body: str = '') -> subprocess.Popen:
        """A holder that marks ``<tag>.in``, runs ``body``, waits for ``<tag>.go`` and
        marks ``<tag>.out``."""
        d = self.dir
        script = (f'touch {d}/{tag}.in; {body} while [ ! -e {d}/{tag}.go ] && [ -d {d} ]; do sleep 0.01; done; '
                  f'touch {d}/{tag}.out')  # the loop ends with the test's directory
        argv = ([str(HOST_LOCK_SH)] if impl == 'sh' else [str(CI_LOCK)]) + [f'-{mode}', 'bash', '-c', script]
        p = subprocess.Popen(argv, env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        self.procs.append(p)
        return p

    def inside(self, tag: str, timeout: float = 5.0) -> bool:
        return wait_for(self.dir / f'{tag}.in', timeout)

    def go(self, tag: str) -> None:
        (self.dir / f'{tag}.go').touch()

    def test_every_mode_pair_both_ways(self):
        for (ia, ib), (ma, mb) in itertools.product([('sh', 'py'), ('py', 'sh'), ('py', 'py')],
                                                     itertools.product('sxl', repeat=2)):
            with self.subTest(a=f'{ia}-{ma}', b=f'{ib}-{mb}'):
                a, b = f'a{ia}{ma}{ib}{mb}', f'b{ia}{ma}{ib}{mb}'
                self.start(ia, ma, a)
                self.assertTrue(self.inside(a))
                pb = self.start(ib, mb, b)
                if 'x' in (ma, mb):
                    self.assertFalse(self.inside(b, 0.4), 'entered beside an exclusive holder')
                    self.go(a)
                    self.assertTrue(self.inside(b))
                else:
                    self.assertTrue(self.inside(b), 'two shared holders exclude each other')
                    self.go(a)
                self.go(b)
                self.assertEqual(pb.wait(10), 0)

    def test_a_waiting_exclusive_request_keeps_new_shared_ones_out(self):
        for first, writer, late in (('sh', 'py', 'sh'), ('py', 'sh', 'py'), ('sh', 'sh', 'py')):
            with self.subTest(first=first, writer=writer, late=late):
                t = first + writer + late
                self.start(first, 's', f's1{t}')
                self.assertTrue(self.inside(f's1{t}'))
                self.start(writer, 'x', f'x{t}')
                time.sleep(0.3)  # the exclusive request holds the turnstile now
                self.start(late, 's', f's2{t}')
                self.assertFalse(self.inside(f's2{t}', 0.4))
                self.go(f's1{t}')
                self.assertTrue(self.inside(f'x{t}'))
                self.assertFalse(self.inside(f's2{t}', 0.3))
                self.go(f'x{t}')
                self.assertTrue(self.inside(f's2{t}'))
                self.go(f's2{t}')

    def test_light_enters_beside_a_normal_shared_holder_only(self):
        for holder, writer, light in (('sh', 'sh', 'py'), ('py', 'py', 'sh'), ('py', 'sh', 'py')):
            with self.subTest(holder=holder, writer=writer, light=light):
                t = holder + writer + light
                self.start(holder, 's', f's{t}')
                self.assertTrue(self.inside(f's{t}'))
                self.start(writer, 'x', f'x{t}')
                time.sleep(0.3)
                self.start(light, 'l', f'l{t}')
                self.assertTrue(self.inside(f'l{t}'), 'a light request waited behind the exclusive one')
                self.go(f's{t}')
                self.go(f'l{t}')
                self.assertTrue(self.inside(f'x{t}'))
                self.go(f'x{t}')
        # with only a light holder in, a light request queues behind the waiting writer
        self.start('sh', 'l', 'l1')
        self.assertTrue(self.inside('l1'))
        self.start('py', 'x', 'xw')
        time.sleep(0.3)
        self.start('py', 'l', 'l2')
        self.assertFalse(self.inside('l2', 0.4))
        self.go('l1')
        self.assertTrue(self.inside('xw'))
        self.go('xw')
        self.assertTrue(self.inside('l2'))
        self.go('l2')

    def test_nesting_covers_cross_implementations(self):
        out = self.dir / 'nested'
        cases = [
            ([str(HOST_LOCK_SH), '-x', str(CI_LOCK), '-s', 'touch', str(out)], 0),
            ([str(CI_LOCK), '-x', str(HOST_LOCK_SH), '-s', 'touch', str(out)], 0),
            ([str(CI_LOCK), '-s', str(HOST_LOCK_SH), '-s', 'touch', str(out)], 0),
            ([str(HOST_LOCK_SH), '-x', str(CI_LOCK), '-x', 'touch', str(out)], 0),
            ([str(CI_LOCK), '-x', str(HOST_LOCK_SH), '-x', 'touch', str(out)], 0),
            ([str(HOST_LOCK_SH), '-s', str(CI_LOCK), '-x', 'touch', str(out)], 3),
            ([str(CI_LOCK), '-s', str(HOST_LOCK_SH), '-x', 'touch', str(out)], 3),
        ]
        for argv, code in cases:
            with self.subTest(argv=' '.join(a.rsplit('/', 1)[-1] for a in argv[:4])):
                if out.exists():
                    out.unlink()
                r = subprocess.run(argv, env=self.env, capture_output=True, timeout=20)
                self.assertEqual(r.returncode, code, r.stderr.decode())
                self.assertEqual(out.exists(), code == 0)

    def test_a_stale_cover_takes_the_lock(self):
        env = dict(self.env, BRAINSPIKE_HOST_LOCK='x')  # nobody holds it
        r = subprocess.run([str(CI_LOCK), '-s', 'true'], env=env, capture_output=True, timeout=20)
        self.assertEqual(r.returncode, 0)
        self.assertIn(b'stale', r.stderr)
        r = subprocess.run([str(CI_LOCK), '-s', 'true'], env=dict(self.env, BRAINSPIKE_HOST_LOCK='y'),
                           capture_output=True, timeout=20)
        self.assertEqual(r.returncode, 2)

    def test_the_command_does_not_inherit_the_lock(self):
        # the command leaves a child behind; the lock is free once ci-lock exits
        r = subprocess.run([str(CI_LOCK), '-x', 'bash', '-c', 'sleep 30 >/dev/null 2>&1 & echo $!'], env=self.env,
                           capture_output=True, timeout=20)
        child = int(r.stdout)
        try:
            r2 = subprocess.run(['flock', '-n', '-x', self.lock, 'true'], timeout=10)
            self.assertEqual(r2.returncode, 0)
        finally:
            os.kill(child, 9)

    def test_a_timeout(self):
        self.start('sh', 'x', 'hold')
        self.assertTrue(self.inside('hold'))
        t0 = time.monotonic()
        r = subprocess.run([str(CI_LOCK), '--timeout', '0.5', '-s', 'true'], env=self.env, capture_output=True,
                           timeout=20)
        self.assertEqual(r.returncode, 4)
        self.assertLess(time.monotonic() - t0, 5)
        self.go('hold')


@unittest.skipUnless(RUNNABLE, 'Linux, bash and flock(1) only')
class SlotInterop(unittest.TestCase):
    def test_together_never_more_than_the_slot_count(self):
        with tempfile.TemporaryDirectory(prefix='ci-slot-') as d:
            d = pathlib.Path(d)
            lock, log = d / 'slot.lock', d / 'log'
            env = dict(os.environ, LOCAL_CI_SLOT_LOCK=str(lock), LOCAL_CI_SLOTS='2', LOCAL_CI_SLOT_POLL='0.1',
                       CI_RUNNERS_PYTHON=PY)
            body = (f'flock {d}/log.lock -c "echo in >> {log}"; sleep 0.3; '
                    f'flock {d}/log.lock -c "echo out >> {log}"')
            procs = []
            for i in range(10):
                if i % 2:
                    argv = [str(CI_SLOT_SH), 'bash', '-c', body]
                else:
                    project = 'p' if i % 4 else 'q'
                    argv = [str(CI_SLOT), '--project', project, '--default', 'p', '--cap', 'q=1', '--poll', '0.1',
                            'bash', '-c', body]
                procs.append(subprocess.Popen(argv, env=env, stderr=subprocess.DEVNULL))
            for p in procs:
                self.assertEqual(p.wait(60), 0)
            events = log.read_text().split()
            depth = peak = 0
            for e in events:
                depth += 1 if e == 'in' else -1
                peak = max(peak, depth)
            self.assertEqual(events.count('in'), 10)
            self.assertEqual(events.count('out'), 10)
            self.assertEqual(peak, 2)
            self.assertEqual([n for n in os.listdir(d / 'slot.queue') if n.endswith(('.ticket', '.wait'))], [])


if __name__ == '__main__':
    unittest.main()
