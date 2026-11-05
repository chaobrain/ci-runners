"""The fair slot queue (design §7, rules 1, 2 and 4)."""
from __future__ import annotations

import unittest

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


if __name__ == '__main__':
    unittest.main()
