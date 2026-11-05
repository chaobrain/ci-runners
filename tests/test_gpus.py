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


if __name__ == '__main__':
    unittest.main()
