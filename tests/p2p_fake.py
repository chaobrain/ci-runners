"""A CUDA driver stand-in for the P2P self-test (design decision 12.6): a faithful copy, one
whose peer copy returns zeros (h100's fault, §5.4), one that does nothing, one that goes
wrong at the last byte, and one whose driver call fails. It imports nothing of the
package at import time, so a copy of a release can load it in place of ``p2p.Driver``."""
from __future__ import annotations

import pathlib
import shutil

ROOT = pathlib.Path(__file__).resolve().parents[1]


class Fake:
    """A CUDA driver stand-in: ``cards`` cards whose memory is ``bytearray``s."""

    def __init__(self, cards: int = 2, mode: str = 'ok', fail: str = '') -> None:
        self.cards, self.mode, self.fail = cards, mode, fail
        self.mem: dict[int, bytearray] = {}
        self.copies: list[tuple[int, int]] = []
        self.closed = self.peers = False

    def _check(self, name: str) -> None:
        if name == self.fail:
            from ci_runners import p2p  # the release's own, where a test runs it as one
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


def zeros_release(dest: str) -> str:
    """A copy of this tree's package under ``dest`` whose ``p2p.Driver`` is ``Fake`` in
    zeros mode, so the release's own entry point (``part.p2p_argv``) runs against the
    fault. Returns the release directory."""
    pkg = pathlib.Path(dest) / 'ci_runners'
    shutil.copytree(ROOT / 'ci_runners', pkg, ignore=shutil.ignore_patterns('__pycache__'))
    with open(pkg / 'p2p.py', 'a', encoding='utf-8') as f:
        f.write(f'\n\nsys.path.append({str(ROOT)!r})\nfrom tests.p2p_fake import Fake as _Fake  # noqa: E402\n'
                "Driver = lambda: _Fake(mode='zeros')  # noqa: E731\n")
    return dest
