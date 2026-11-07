"""GPU facts from nvidia-smi CSV and the lease choice (design §7)."""
from __future__ import annotations

import pathlib
import unittest

from ci_runners import gpus

FX = pathlib.Path(__file__).resolve().parent / 'fixtures' / 'host'


class Gpus(unittest.TestCase):
    def test_free_memory_and_rank(self):
        free = gpus.gpu_free_mib((FX / 'smi_memory.csv').read_text())
        self.assertEqual(free, {0: 79090, 1: 559, 2: 21559, 3: 81558})  # the [N/A] line is skipped
        self.assertEqual(gpus.rank_gpus(free, 6000), [3, 0, 2])  # most free first; card 1 is short
        self.assertEqual(gpus.rank_gpus({0: 7000, 1: 7000}, 6000), [0, 1])  # a tie: the lower index
        self.assertEqual(gpus.rank_gpus({0: 5999}, 6000), [])
        self.assertEqual(gpus.rank_gpus({0: 6000}, 6000), [0])
        self.assertEqual(gpus.rank_gpus({}, 0), [])  # nvidia-smi failed: nothing, even for 0 MiB
        self.assertEqual(gpus.usable_gpus([1, 3, 7], free, 6000), [3])  # 7 is not listed
        self.assertEqual(gpus.usable_gpus([7], free, 0), [])  # not even for 0 MiB

    def test_buses(self):
        self.assertEqual(gpus.gpu_buses((FX / 'smi_bus.csv').read_text())[3], '00000000:A8:00.0')
        self.assertEqual(gpus.gpu_buses('x, y\n'), {})

    def test_lease(self):
        free = {0: 20000, 1: 24000}
        self.assertEqual(gpus.choose_lease(1, cards=[0, 1], free=free, need_mib=6000, leased={}), [1])
        self.assertEqual(gpus.choose_lease(1, cards=[0, 1], free=free, need_mib=6000, leased={1: 1}), [0])
        self.assertIsNone(gpus.choose_lease(1, cards=[0, 1], free=free, need_mib=6000, leased={0: 1, 1: 1}))
        self.assertEqual(gpus.choose_lease(1, cards=[0, 1], free=free, need_mib=6000, leased={0: 1, 1: 1},
                                           max_parts=2), [1])
        self.assertEqual(gpus.choose_lease(2, cards=[0, 1], free=free, need_mib=6000, leased={}), [1, 0])
        self.assertIsNone(gpus.choose_lease(2, cards=[0, 1], free=free, need_mib=21000, leased={}))
        self.assertEqual(gpus.choose_lease(1, cards=[0], free=free, need_mib=6000, leased={}), [0])  # host's cards only
        self.assertEqual(gpus.choose_lease(0, cards=[], free={}, need_mib=0, leased={}), [])


L = gpus.LeaseTicket
FREE = {0: 24000, 1: 24000}


class LeaseOrder(unittest.TestCase):
    """Leases in ticket order per card (decision 12.6, design §7)."""

    def grants(self, tickets, leased=None, free=FREE, max_parts=1):
        return gpus.lease_grants(tickets, free=free, leased=leased or {}, max_parts=max_parts)

    def test_a_waiting_two_card_lease_holds_back_a_later_one_card_lease(self):
        # card 1 is leased: the multi part (1) waits, and card 0 stays free for it
        self.assertEqual(self.grants([L(1, 2, (0, 1)), L(2, 1, (0,))], leased={1: 1}), {1: None, 2: None})
        self.assertEqual(self.grants([L(1, 2, (0, 1)), L(2, 1, (1,))], leased={1: 1}), {1: None, 2: None})
        # another card's later lease is not held back
        three = {0: 24000, 1: 24000, 2: 24000}
        self.assertEqual(self.grants([L(1, 2, (0, 1)), L(2, 1, (2,))], leased={1: 1}, free=three), {1: None, 2: [2]})
        # once card 1 is free, the multi part takes both and the later one waits for them
        self.assertEqual(self.grants([L(1, 2, (0, 1)), L(2, 1, (0,))]), {1: [0, 1], 2: None})

    def test_an_older_one_card_lease_goes_first(self):
        self.assertEqual(self.grants([L(2, 2, (0, 1)), L(1, 1, (0,))]), {1: [0], 2: None})
        self.assertEqual(self.grants([L(1, 1, (0,)), L(2, 1, (1,))]), {1: [0], 2: [1]})
        self.assertEqual(self.grants([L(1, 1, (0,)), L(2, 1, (0,))], max_parts=2), {1: [0], 2: [0]})

    def test_a_ticket_short_of_memory_waits_and_holds_back(self):
        self.assertEqual(self.grants([L(1, 1, (0,), 30000), L(2, 1, (0,), 1000)]), {1: None, 2: None})
        self.assertEqual(self.grants([L(1, 1, (0, 1), 30000)]), {1: None})

    def test_every_takes_each_card_whole(self):
        """A nightly part (decision 12.8) takes every lease of every card, and only free cards."""
        self.assertEqual(self.grants([L(1, 2, (0, 1), every=True), L(2, 1, (0,))], max_parts=2), {1: [0, 1], 2: None})
        self.assertEqual(self.grants([L(1, 2, (0, 1), every=True)], leased={0: 1}, max_parts=2), {1: None})
        self.assertEqual(self.grants([L(1, 1, (0,)), L(2, 2, (0, 1), every=True)], max_parts=2), {1: [0], 2: None})


if __name__ == '__main__':
    unittest.main()
