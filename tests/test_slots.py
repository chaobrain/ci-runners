"""The fair slot queue (design §7, rules 1, 2 and 4)."""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import tempfile
import time
import unittest

from ci_runners import slots
from ci_runners.slots import Ticket as T
from ci_runners.slots import grants, queue_full

CAPS = {'brainspike': 2, 'brainevent': 1, 'other': 1}


def g(waiting, held=None, free=1, caps=CAPS):
    return grants(waiting, held or {}, caps, free, 'brainspike')


class Grants(unittest.TestCase):
    def test_first_come_first_served(self):
        self.assertEqual(g([T(3, 'other'), T(2, 'brainevent')]), [2])
        self.assertEqual(g([T(3, 'other'), T(2, 'brainevent')], free=2), [2, 3])
        self.assertEqual(g([], free=2), [])
        self.assertEqual(g([T(1, 'other')], free=0), [])

    def test_a_project_at_its_cap_is_skipped(self):
        self.assertEqual(g([T(1, 'brainevent'), T(2, 'other')], held={'brainevent': 1}), [2])
        self.assertEqual(g([T(1, 'brainevent'), T(2, 'brainevent')], free=2), [1])  # cap 1
        self.assertEqual(g([T(1, 'unknown')]), [])  # an unregistered project has no slots

    def test_the_project_holding_fewest_goes_first(self):
        self.assertEqual(g([T(1, 'brainspike'), T(2, 'other')], held={'brainspike': 1}), [2])
        self.assertEqual(g([T(1, 'brainspike'), T(2, 'brainspike'), T(3, 'other')], free=2), [1, 3])
        self.assertEqual(g([T(5, 'other'), T(4, 'brainevent')], held={'other': 0, 'brainevent': 0}), [4])  # a tie

    def test_tickets_without_a_project_are_the_default_projects(self):
        self.assertEqual(g([T(1), T(2)], held={'brainspike': 1}, free=2), [1])  # brainspike cap 2
        self.assertEqual(g([T(1), T(2, 'other')], held={'brainspike': 1}), [2])
        self.assertEqual(g([T(1), T(2)], free=2), [1, 2])

    def test_queue_full(self):
        self.assertTrue(queue_full(1, 1))
        self.assertFalse(queue_full(0, 1))
        self.assertTrue(queue_full(0, 0))



@unittest.skipUnless(shutil.which('flock') and os.path.exists('/proc/locks'), 'Linux with flock(1) only')
class Queue(unittest.TestCase):
    def test_flocked_reads_holders(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = pathlib.Path(d) / 'a.lock', pathlib.Path(d) / 'b.lock'
            a.touch()
            b.touch()
            self.assertFalse(slots._flocked(str(a)))
            hold = subprocess.Popen(['flock', '-x', str(a), 'sleep', '30'], start_new_session=True)
            try:
                for _ in range(200):
                    if slots._flocked(str(a)):
                        break
                    time.sleep(0.01)
                self.assertTrue(slots._flocked(str(a)))
                self.assertFalse(slots._flocked(str(b)))
            finally:
                os.killpg(hold.pid, 9)
                hold.wait()

    def test_a_taken_slot_is_held_exclusively(self):
        with tempfile.TemporaryDirectory() as d:
            q = slots.SlotQueue(os.path.join(d, 's.lock'), 1, poll=0.05)
            q.take_ticket('p', 'cmd')
            fd = q.try_take('p', {'p': 1}, 'p')
            self.assertIsNotNone(fd)
            try:
                r = subprocess.run(['flock', '-n', '-s', q.slots[0], 'true'], timeout=10)
                self.assertEqual(r.returncode, 1)  # ci_slot.sh's shared or exclusive try fails
                self.assertFalse(os.path.exists(q.ticket))
                self.assertEqual(q.held()[:2], ({'p': 1}, 0))
            finally:
                os.close(fd)

    def test_tickets_share_the_counter_and_dead_ones_go(self):
        with tempfile.TemporaryDirectory() as d:
            q = slots.SlotQueue(os.path.join(d, 's.lock'), 2, poll=0.05)
            dead = os.path.join(q.queue, '000000000007.ticket')
            os.makedirs(q.queue)
            with open(dead, 'w') as f:
                f.write('7 999999999 1 /x cmd\n')  # no such pid
            q.take_ticket('p', 'cmd')
            self.assertEqual(q.seq, 8)
            self.assertTrue(q.ticket.endswith('000000000008.wait'))
            self.assertEqual(q.waiting(), [T(8, 'p')])
            self.assertFalse(os.path.exists(dead))


if __name__ == '__main__':
    unittest.main()
