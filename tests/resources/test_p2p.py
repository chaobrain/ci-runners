"""The P2P self-test of a ``multi`` part (design decision 12.6), on fixture drivers: one
that copies faithfully, one whose peer copy returns zeros (h100's fault, §5.4), one that
leaves the destination as it was, and one whose driver call fails."""
from __future__ import annotations

import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from ci_runners.resources import p2p
from ci_runners.execution import part
from tests.p2p_fake import Fake, zeros_release


SIZE = 4096


class SelfTest(unittest.TestCase):
    def test_a_faithful_copy_passes_every_ordered_pair(self):
        d = Fake(3)
        self.assertEqual(p2p.self_test(d, SIZE), '')
        self.assertEqual(sorted(d.copies), [(a, b) for a in range(3) for b in range(3) if a != b])
        self.assertTrue(d.peers and d.closed and not d.mem)

    def test_zeros_fail(self):
        """h100's fault: the copy reports success and the destination reads zeros."""
        d = Fake(mode='zeros')
        why = p2p.self_test(d, SIZE, names=['2', '5'])
        self.assertEqual(why, 'card 2 -> 5: byte 0 reads 0x00, not ' + f'{p2p.pattern(0, 1, SIZE)[0]:#04x} (zeros)')
        self.assertTrue(d.closed and not d.mem)  # freed after a failure too

    def test_a_copy_that_does_nothing_fails(self):
        self.assertIn('card 0 -> 1: byte 0', p2p.self_test(Fake(mode='none'), SIZE))

    def test_a_late_wrong_byte_fails(self):
        self.assertEqual(p2p.self_test(Fake(mode='tail'), SIZE)[:28], f'card 0 -> 1: byte {SIZE - 1} reads')

    def test_a_driver_error_fails(self):
        self.assertEqual(p2p.self_test(Fake(fail='peer'), SIZE), 'peer returned CUDA_ERROR_UNKNOWN')
        self.assertEqual(p2p.self_test(Fake(fail='count'), SIZE), 'count returned CUDA_ERROR_UNKNOWN')

    def test_one_card_fails(self):
        self.assertEqual(p2p.self_test(Fake(1), SIZE), '1 card(s) visible: the test needs two')

    def test_the_pattern(self):
        a, b = p2p.pattern(0, 1, 1000), p2p.pattern(1, 0, 1000)
        self.assertEqual(len(a), 1000)
        self.assertNotIn(0, a)  # zeros never pass
        self.assertNotEqual(a, b)  # each pair its own phase: a stale buffer does not pass
        self.assertEqual(len(p2p.pattern(0, 1, p2p.SIZE)), 64 << 20)
        self.assertEqual(p2p.first_difference(b'abcd', b'abXd', chunk=2), 2)
        self.assertEqual(p2p.first_difference(b'ab', b'abc'), 2)


class Bound(unittest.TestCase):
    """The supervisor's side (``part.run_p2p``): a pass, a failure's reason, the bound."""

    def test_pass_failure_and_bound(self):
        py = [sys.executable, '-I', '-c']
        self.assertEqual(part.run_p2p([*py, 'print("p2p: ok")'], 10), '')
        why = part.run_p2p([*py, 'import sys; print("p2p: card 0 -> 1: byte 0 reads 0x00"); sys.exit(1)'], 10)
        self.assertEqual(why, 'card 0 -> 1: byte 0 reads 0x00')
        self.assertEqual(part.run_p2p([*py, 'import sys; sys.exit(3)'], 10), 'the self-test exited 3')
        self.assertEqual(part.run_p2p([*py, 'import time; time.sleep(30)'], 0.5), 'the self-test ran over its 0.5 s bound')
        self.assertIn('did not start', part.run_p2p(['/nonexistent/p2p'], 10))


class Entry(unittest.TestCase):
    """The release's own entry point, the supervisor's default command (``part.p2p_argv``),
    over the zeros fixture in place of the CUDA driver: it must exit 1."""

    def test_the_default_command_fails_on_zeros(self):
        with tempfile.TemporaryDirectory() as d:
            r = subprocess.run(part.p2p_argv(zeros_release(d)), capture_output=True, text=True, timeout=60,
                               env={'PATH': '/usr/bin:/bin', 'CUDA_VISIBLE_DEVICES': '3,5'})
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertRegex(r.stdout, r'^p2p: card 3 -> 5: byte 0 reads 0x00, not 0x[0-9a-f]{2} \(zeros\)\n\Z')


class HardBound(unittest.TestCase):
    """The bound holds when the killed test does not go: here a process of its own session
    keeps the pipe open, as a call stuck in the driver keeps the test alive."""

    def test_a_test_that_does_not_go_is_given_up(self):
        with tempfile.TemporaryDirectory() as d:
            pidfile = pathlib.Path(d, 'pid')
            argv = [sys.executable, '-I', '-c', 'import subprocess, sys, time; '
                    'p = subprocess.Popen(["sleep", "30"], start_new_session=True); '
                    f'open({str(pidfile)!r}, "w").write(str(p.pid)); time.sleep(30)']
            t0 = time.monotonic()
            try:
                with mock.patch.object(part, 'P2P_KILL_WAIT', 0.5):
                    why = part.run_p2p(argv, 0.5)
                took = time.monotonic() - t0
            finally:
                for _ in range(100):
                    if pidfile.exists() and pidfile.read_text():
                        break
                    time.sleep(0.05)
                os.kill(int(pidfile.read_text()), signal.SIGKILL)
        self.assertEqual(why, 'the self-test ran over its 0.5 s bound')
        self.assertLess(took, 5)


@unittest.skipUnless(os.environ.get('CI_RUNNERS_P2P_LIVE') == '1', 'set CI_RUNNERS_P2P_LIVE=1 on a host with two cards')
class Live(unittest.TestCase):
    def test_the_driver(self):
        self.assertEqual(p2p.self_test(p2p.Driver()), '')


if __name__ == '__main__':
    unittest.main()
