"""The P2P self-test of a ``multi`` part (design decision 12.6), on fixture drivers: one
that copies faithfully, one whose peer copy returns zeros (h100's fault, §5.4), one that
leaves the destination as it was, and one whose driver call fails."""
from __future__ import annotations

import os
import sys
import unittest

from ci_runners import p2p, part


class Fake:
    """A CUDA driver stand-in: ``cards`` cards whose memory is ``bytearray``s."""

    def __init__(self, cards: int = 2, mode: str = 'ok', fail: str = '') -> None:
        self.cards, self.mode, self.fail = cards, mode, fail
        self.mem: dict[int, bytearray] = {}
        self.copies: list[tuple[int, int]] = []
        self.closed = self.peers = False

    def _check(self, name: str) -> None:
        if name == self.fail:
            raise p2p.P2PError(f'{name} returned CUDA_ERROR_UNKNOWN')

    def count(self) -> int:
        self._check('count')
        return self.cards

    def context(self, i: int) -> int:
        return 100 + i

    def enable_peers(self, ctxs) -> None:
        self.peers = True

    def alloc(self, ctx: int, size: int) -> int:
        self.mem[ctx] = bytearray(size)
        return ctx

    def free(self, ctx: int, ptr: int) -> None:
        self.mem.pop(ptr, None)

    def upload(self, ctx: int, ptr: int, data: bytes) -> None:
        self.mem[ptr][:] = data

    def fill(self, ctx: int, ptr: int, value: int, size: int) -> None:
        self.mem[ptr][:] = bytes([value]) * size

    def peer(self, dst_ctx: int, dst: int, src_ctx: int, src: int, size: int) -> None:
        self._check('peer')
        self.copies.append((src - 100, dst - 100))
        if self.mode == 'ok':
            self.mem[dst][:] = self.mem[src]
        elif self.mode == 'zeros':
            self.mem[dst][:] = bytes(size)
        elif self.mode == 'tail' and src == 100:  # the last byte of card 0's copies goes wrong
            self.mem[dst][:] = self.mem[src][:-1] + b'\x00'
        elif self.mode == 'tail':
            self.mem[dst][:] = self.mem[src]
        # 'none': the copy does nothing, the destination stays cleared

    def download(self, ctx: int, ptr: int, size: int) -> bytes:
        return bytes(self.mem[ptr])

    def close(self) -> None:
        self.closed = True


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


@unittest.skipUnless(os.environ.get('CI_RUNNERS_P2P_LIVE') == '1', 'set CI_RUNNERS_P2P_LIVE=1 on a host with two cards')
class Live(unittest.TestCase):
    def test_the_driver(self):
        self.assertEqual(p2p.self_test(p2p.Driver()), '')


if __name__ == '__main__':
    unittest.main()
