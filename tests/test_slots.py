"""The fair slot queue (design §7, rules 1, 2 and 4)."""
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

    def test_tickets_without_a_project_are_the_default_projects(self):
        """Rule 4: ci_slot.sh's tickets name no project and count as the host's default."""
        def gd(waiting, held=None, free=1):
            return grants(waiting, held or {}, CAPS, free, 'myproj')
        self.assertEqual(gd([T(1), T(2)], held={'myproj': 1}, free=2), [1])  # myproj's cap is 2
        self.assertEqual(gd([T(1), T(2, 'other')], held={'myproj': 1}), [2])  # myproj holds more
        self.assertEqual(gd([T(1), T(2)], free=2), [1, 2])

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

    def _outside_holder(self, path):
        """A slot flocked by a process with no holder record: a ci_slot.sh holder."""
        hold = subprocess.Popen(['flock', '-x', path, 'sleep', '30'], start_new_session=True)
        self.addCleanup(lambda: (os.killpg(hold.pid, 9), hold.wait()))
        for _ in range(500):
            if slots._flocked(path):
                return
            time.sleep(0.01)
        self.fail('the outside holder did not take the slot')

    def _sh_ticket(self, q, seq):
        """A live ci_slot.sh ticket (this process's pid) in ``q``'s queue."""
        os.makedirs(q.queue, exist_ok=True)
        with open(os.path.join(q.queue, f'{seq:012d}.ticket'), 'w') as f:
            f.write(f'{seq} {os.getpid()} {int(time.time())} /x scripts/local_ci.sh affected\n')

    def test_ci_slot_sh_holders_and_tickets_are_the_default_projects(self):
        """Rule 4 on disk: a slot held without our holder record is the default project's,
        so another project that holds fewer takes the free slot before an older ci_slot.sh
        ticket."""
        with tempfile.TemporaryDirectory() as d:
            q = slots.SlotQueue(os.path.join(d, 's.lock'), 2, poll=0.05, default_project='p')
            self._outside_holder(q.slots[0])
            self._sh_ticket(q, 1)
            q.take_ticket('q', 'cmd')
            fd = q.try_take('q', {'p': 2, 'q': 1})
            self.assertIsNotNone(fd)  # p holds 1 (ci_slot.sh's), q holds 0: q first
            os.close(fd)

    def test_an_older_ci_slot_sh_ticket_goes_first(self):
        """A ci_slot.sh ticket is in the order: a newer ticket of the same project waits
        behind it."""
        with tempfile.TemporaryDirectory() as d:
            q = slots.SlotQueue(os.path.join(d, 's.lock'), 2, poll=0.05, default_project='p')
            self._outside_holder(q.slots[0])
            self._sh_ticket(q, 1)
            q.take_ticket('p', 'cmd')
            self.assertEqual(q.seq, 2)
            self.assertIsNone(q.try_take('p', {'p': 2}))

    def test_the_default_projects_cap_is_the_slot_count(self):
        """Rule 4: ci_slot.sh's waiters apply no caps, so the default project's is the
        slot count, whatever its registry says."""
        with tempfile.TemporaryDirectory() as d:
            q = slots.SlotQueue(os.path.join(d, 's.lock'), 2, poll=0.05, default_project='p')
            self._outside_holder(q.slots[0])
            q.take_ticket('p', 'cmd')
            fd = q.try_take('p', {'p': 1})
            self.assertIsNotNone(fd)
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
