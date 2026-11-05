"""The reaper (design §5.3): dated hook logs and whole runs' part records, by their
newest change."""
from __future__ import annotations

import os
import pathlib
import tempfile
import time
import unittest

from ci_runners import reaper

DAY = 86400


class Reap(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = pathlib.Path(self.tmp.name)
        self.base = self.data / 'ci-runners'
        self.now = time.time()

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, rel, age_days):
        p = self.base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('x')
        t = self.now - age_days * DAY
        os.utime(p, (t, t))
        return p

    def age(self, rel, age_days):
        t = self.now - age_days * DAY
        os.utime(self.base / rel, (t, t))

    def test_a_run_counts_by_its_newest_file(self):
        self.put('state/parts/chaobrain/brainspike/777-1/test/status.json', 1)   # still being written
        self.put('state/parts/chaobrain/brainspike/700-1/test/status.json', 20)
        self.put('state/parts/chaobrain/brainspike/700-1/test/log', 20)
        self.put('state/parts/chaobrain/other/5-1/a/status.json', 20)
        for d in ('state/parts/chaobrain/brainspike/777-1/test', 'state/parts/chaobrain/brainspike/777-1',
                  'state/parts/chaobrain/brainspike/700-1/test', 'state/parts/chaobrain/brainspike/700-1',
                  'state/parts/chaobrain/other/5-1/a', 'state/parts/chaobrain/other/5-1',
                  'state/parts/chaobrain/other', 'state/parts/chaobrain/brainspike', 'state/parts/chaobrain'):
            self.age(d, 30)  # old directories: their files decide
        out = reaper.reap(str(self.data), now=self.now)
        self.assertEqual(out, 'reaper: removed 2 old entries')
        self.assertTrue((self.base / 'state/parts/chaobrain/brainspike/777-1/test/status.json').exists())
        self.assertFalse((self.base / 'state/parts/chaobrain/brainspike/700-1').exists())
        self.assertFalse((self.base / 'state/parts/chaobrain/other').exists())  # left empty, removed

    def test_dated_logs_go_after_14_days_and_nothing_else_there(self):
        self.put('logs/hook-2026-10-01.log', 20)
        self.put('logs/hook-2026-11-04.log', 1)
        self.put('logs/keep-me', 40)
        reaper.reap(str(self.data), now=self.now)
        self.assertEqual(sorted(p.name for p in (self.base / 'logs').iterdir()), ['hook-2026-11-04.log', 'keep-me'])

    def test_a_link_is_removed_not_followed(self):
        outside = self.data / 'outside'
        outside.mkdir()
        (outside / 'f').write_text('x')
        (self.base / 'state/parts/o/r').mkdir(parents=True)
        os.symlink(outside, self.base / 'state/parts/o/r/9-1')
        t = self.now - 30 * DAY
        os.utime(self.base / 'state/parts/o/r/9-1', (t, t), follow_symlinks=False)
        reaper.reap(str(self.data), now=self.now)
        self.assertTrue((outside / 'f').exists())
        self.assertFalse(os.path.lexists(self.base / 'state/parts/o/r/9-1'))

    def test_a_linked_owner_or_repository_is_not_walked(self):
        outside = self.data / 'outside'
        (outside / 'repo/1-1').mkdir(parents=True)
        (outside / 'repo/1-1/f').write_text('x')
        t = self.now - 30 * DAY
        for q in ('repo/1-1/f', 'repo/1-1', 'repo'):
            os.utime(outside / q, (t, t))
        (self.base / 'state/parts').mkdir(parents=True)
        os.symlink(outside, self.base / 'state/parts/o')
        reaper.reap(str(self.data), now=self.now)
        self.assertTrue((outside / 'repo/1-1/f').exists())

    def test_nothing_there_is_fine(self):
        self.assertEqual(reaper.reap(str(self.data), now=self.now), 'reaper: removed 0 old entries')


if __name__ == '__main__':
    unittest.main()
