"""The leak check of a test run (tests/leaks.py): a process of the run that outlives
it, also in a new session, is found and killed."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest

from tests import leaks

LEAVE = ('import subprocess, sys; '
         'p = subprocess.Popen(["sleep", "60"], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); open(sys.argv[1], "w").write(str(p.pid))')


def gone(pid: int) -> bool:
    try:
        with open(f'/proc/{pid}/stat') as f:
            return f.read().rsplit(')', 1)[1].split()[0] in ('Z', 'X')
    except OSError:
        return True


@unittest.skipUnless(os.path.isdir('/proc/self'), 'Linux only')
class Leaks(unittest.TestCase):
    def test_a_process_left_in_a_new_session_is_found_and_killed(self):
        with tempfile.NamedTemporaryFile() as f:
            r = subprocess.run([sys.executable, '-I', leaks.__file__, '--', sys.executable, '-I', '-c', LEAVE, f.name],
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 1, r.stderr)
            self.assertIn('outlived the run', r.stderr)
            self.assertTrue(gone(int(open(f.name).read())), 'the leftover still runs')

    def test_a_clean_run_passes_its_status_through(self):
        for code in (0, 3):
            r = subprocess.run([sys.executable, '-I', leaks.__file__, '--', sys.executable, '-c',
                                f'import sys; sys.exit({code})'], capture_output=True, timeout=60)
            self.assertEqual(r.returncode, code)

    def test_a_hang_kills_the_whole_tree(self):
        with tempfile.NamedTemporaryFile() as f:
            t0 = time.monotonic()
            code, _ = leaks.run([sys.executable, '-I', '-c', LEAVE + '; import time; time.sleep(60)', f.name],
                                timeout=2, quiet=True)
            self.assertEqual(code, 124)
            self.assertLess(time.monotonic() - t0, 20)
            self.assertTrue(gone(int(open(f.name).read())), 'the hung run left its child')


if __name__ == '__main__':
    unittest.main()
