"""The CI proxy check's state machine (design §5.3: switch node after 3 failures)."""
from __future__ import annotations

import unittest

from ci_runners import proxy

NODES = ['n1', 'n2', 'n3']


class Step(unittest.TestCase):
    def test_three_failures_switch_to_the_next_node(self):
        s = {}
        for _ in range(2):
            s, sw = proxy.step(s, False, NODES)
            self.assertIsNone(sw)
        s, sw = proxy.step(s, False, NODES)
        self.assertEqual((s, sw), ({'node': 'n2', 'failures': 0}, 'n2'))
        s, _ = proxy.step({'node': 'n3', 'failures': 2}, False, NODES)
        self.assertEqual(s['node'], 'n1')  # wraps around

    def test_a_success_resets(self):
        self.assertEqual(proxy.step({'node': 'n2', 'failures': 2}, True, NODES), ({'node': 'n2', 'failures': 0}, None))
        self.assertEqual(proxy.step({'node': 'gone', 'failures': 9}, True, NODES)[0]['node'], 'n1')


if __name__ == '__main__':
    unittest.main()
