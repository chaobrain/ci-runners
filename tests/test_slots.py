"""The fair slot queue (design §7, rules 1 and 2)."""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from ci_runners import slots
from ci_runners.slots import Ticket as T
from ci_runners.slots import grants, queue_full

CAPS = {'myproj': 2, 'brainevent': 1, 'other': 1}


def g(waiting, held=None, free=1, caps=CAPS):
    return grants(waiting, held or {}, caps, free)


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
        self.assertEqual(g([T(1, 'myproj'), T(2, 'other')], held={'myproj': 1}), [2])
        self.assertEqual(g([T(1, 'myproj'), T(2, 'myproj'), T(3, 'other')], free=2), [1, 3])
        self.assertEqual(g([T(5, 'other'), T(4, 'brainevent')], held={'other': 0, 'brainevent': 0}), [4])  # a tie

    def test_a_ticket_without_a_project_gets_no_slot(self):
        self.assertEqual(g([T(1), T(2, 'other')]), [2])
        self.assertEqual(g([T(1), T(2)], free=2), [])

    def test_a_ticket_for_every_slot_holds_back_later_ones(self):
        """Decision 12.8: a nightly part's ticket is served after the older ones, takes every
        slot left whatever its cap, and no later ticket passes it."""
        every = T(2, 'myproj', every=True)
        self.assertEqual(g([T(1, 'other'), every, T(3, 'brainevent')], free=2), [1, 2])
        self.assertEqual(g([every, T(3, 'brainevent')], free=2), [2, 2])
        self.assertEqual(g([every, T(3, 'brainevent')], held={'myproj': 1}, free=1), [2])  # past its cap of 2
        self.assertEqual(g([every, T(3, 'brainevent')], free=0), [])
        self.assertEqual(g([T(1, 'other'), T(3, 'brainevent')], free=2), [1, 3])

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
            fd = q.try_take('p', {'p': 1})
            self.assertIsNotNone(fd)
            try:
                r = subprocess.run(['flock', '-n', '-s', q.slots[0], 'true'], timeout=10)
                self.assertEqual(r.returncode, 1)  # another's shared or exclusive try fails
                self.assertFalse(os.path.exists(q.ticket))
                self.assertEqual(q.held()[:2], ({'p': 1}, 0))
            finally:
                os.close(fd)

    def test_a_round_waits_for_another_round(self):
        """A round holds take.lock from its read of the holders to its holder record, so
        no other round sees a slot flocked and not yet recorded (a project would pass its
        cap)."""
        with tempfile.TemporaryDirectory() as d:
            q = slots.SlotQueue(os.path.join(d, 's.lock'), 1, poll=0.05)
            q.take_ticket('p', 'cmd')
            lock = os.path.join(q.queue, 'take.lock')
            hold = subprocess.Popen(['flock', '-x', lock, 'sleep', '1'], start_new_session=True)
            try:
                for _ in range(200):
                    if slots._flocked(lock):
                        break
                    time.sleep(0.01)
                t0 = time.monotonic()
                fd = q.try_take('p', {'p': 1})
                waited = time.monotonic() - t0
                self.assertIsNotNone(fd)
                os.close(fd)
                self.assertGreater(waited, 0.5)
            finally:
                hold.wait()

    def test_a_ticket_for_every_slot_takes_them_all(self):
        """Decision 12.8: ``try_take_every`` keeps what it gets until it holds every slot."""
        with tempfile.TemporaryDirectory() as d:
            q = slots.SlotQueue(os.path.join(d, 's.lock'), 2, poll=0.05)
            for p in q.slots:
                pathlib.Path(p).touch()
            hold = subprocess.Popen(['flock', '-x', q.slots[1], 'sleep', '30'], start_new_session=True)
            try:
                for _ in range(200):
                    if slots._flocked(q.slots[1]):
                        break
                    time.sleep(0.01)
                older = slots.SlotQueue(os.path.join(d, 's.lock'), 2, poll=0.05)
                older.take_ticket('q', 'cmd')  # an older ticket goes first, within its cap
                q.take_ticket('p', 'cmd', every=True)
                self.assertEqual(q.waiting(), [T(older.seq, 'q'), T(q.seq, 'p', every=True)])
                self.assertIsNone(q.try_take_every('p', {'q': 1}))
                self.assertEqual(q.every, [])  # the one free slot is the older ticket's
                fd = older.try_take('q', {'q': 1})
                os.close(fd)
                self.assertIsNone(q.try_take_every('p', {}))  # slot 2 is held: it keeps slot 1 and waits
                self.assertEqual((len(q.every), q.held()[1], os.path.exists(q.ticket)), (1, 0, True))
                later = slots.SlotQueue(os.path.join(d, 's.lock'), 2, poll=0.05)
                later.take_ticket('p', 'cmd')
                self.assertIsNone(later.try_take('p', {'p': 2}))
            finally:
                os.killpg(hold.pid, 9)
                hold.wait()
            fds = q.try_take_every('p', {})
            self.assertEqual((len(fds), q.held()[:2], os.path.exists(q.ticket)), (2, ({'p': 2}, 0), False))
            self.assertIsNone(later.try_take('p', {'p': 2}))
            for fd in fds:
                os.close(fd)

    def test_tickets_share_the_counter_and_dead_ones_go(self):
        with tempfile.TemporaryDirectory() as d:
            q = slots.SlotQueue(os.path.join(d, 's.lock'), 2, poll=0.05)
            dead = os.path.join(q.queue, '000000000007.wait')
            os.makedirs(q.queue)
            with open(dead, 'w') as f:
                f.write('7 999999999 1 /x ci-slot project=p cmd\n')  # no such pid
            q.take_ticket('p', 'cmd')
            self.assertEqual(q.seq, 8)
            self.assertTrue(q.ticket.endswith('000000000008.wait'))
            self.assertEqual(q.waiting(), [T(8, 'p')])
            self.assertFalse(os.path.exists(dead))


class Concurrent(unittest.TestCase):
    """Waiters in separate processes, as the parts of v1.2 and v1.3 share h100's queue
    in a release's window (design §7, §9): never more holders than slots, never more
    than a project's cap."""

    def test_never_more_than_the_slots_or_a_cap(self):
        root = str(pathlib.Path(__file__).resolve().parents[1])
        body = (
            'import os, sys, time, fcntl\n'
            'sys.path.insert(0, sys.argv[1])\n'
            'from ci_runners import slots\n'
            'd, project = sys.argv[2], sys.argv[3]\n'
            'q = slots.SlotQueue(os.path.join(d, "slot.lock"), 2, os.path.join(d, "slot.queue"))\n'
            'q.take_ticket(project, "cmd")\n'
            'fd = None\n'
            'while fd is None:\n'
            '    fd = q.try_take(project, {"p": 2, "q": 1})\n'
            '    time.sleep(0.02) if fd is None else None\n'
            'def log(e):\n'
            '    with open(os.path.join(d, "log"), "a") as f:\n'
            '        fcntl.flock(f, fcntl.LOCK_EX)\n'
            '        f.write(f"{e} {project}\\n")\n'
            'log("in"); time.sleep(0.3); log("out")\n'
            'os.close(fd)\n')
        with tempfile.TemporaryDirectory(prefix='ci-slot-') as d:
            procs = [subprocess.Popen([sys.executable, '-I', '-c', body, root, d, 'q' if i % 3 else 'p'])
                     for i in range(9)]
            for p in procs:
                self.assertEqual(p.wait(120), 0)
            depth, peak, qdepth, qpeak = 0, 0, 0, 0
            lines = pathlib.Path(d, 'log').read_text().split('\n')
            for line in filter(None, lines):
                e, project = line.split()
                step = 1 if e == 'in' else -1
                depth += step
                peak = max(peak, depth)
                if project == 'q':
                    qdepth += step
                    qpeak = max(qpeak, qdepth)
            self.assertEqual(sum(1 for x in lines if x.startswith('in ')), 9)
            self.assertEqual(peak, 2)
            self.assertEqual(qpeak, 1)
            self.assertEqual([n for n in os.listdir(os.path.join(d, 'slot.queue')) if n.endswith('.wait')], [])


if __name__ == '__main__':
    unittest.main()
