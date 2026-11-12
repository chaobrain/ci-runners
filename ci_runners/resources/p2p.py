"""The P2P self-test of a ``multi`` part (design decision 12.6, §4.3).

Before a two-card part's command, its supervisor runs this test inside the part's scope,
with the part's ``CUDA_VISIBLE_DEVICES``: for each ordered pair of the visible cards it
fills the source with a 64 MiB pattern, clears the destination, copies the source to the
destination with ``cuMemcpyPeer`` and reads the destination back, byte for byte. It
checks the data and not the driver's capability flags, because h100's fault (§5.4)
reports P2P access and returns zeros. Where the driver reports peer access, it is
enabled first, so that the copy takes the direct path the part's own copies take.

``self_test`` is a pure function of a driver object, so a fixture driver can stand in
for the CUDA one (``Driver``, over ``libcuda`` with ``ctypes``). ``main`` prints one
line and exits 0 on a pass, 1 on a mismatch or an error; the supervisor bounds it at
``BOUND`` s and ends the part ``unavailable`` (``p2p``) with ``next`` ``none`` on
anything but a pass.
"""
from __future__ import annotations

import ctypes
import os
import sys
from collections.abc import Sequence

SIZE = 64 << 20  # bytes copied per ordered pair
BOUND = 60  # s: the supervisor's bound on the whole test
_ALREADY_ENABLED = 704  # CUDA_ERROR_PEER_ACCESS_ALREADY_ENABLED


class P2PError(Exception):
    """A driver call failed; its text is the test's reason."""


def pattern(src: int, dst: int, size: int) -> bytes:
    """The bytes copied from card ``src`` to card ``dst``: no zero byte (the fault's
    reading), and another phase for each pair, so a stale buffer of an earlier pair does
    not pass. Pure."""
    base = bytes(range(1, 256))
    k = (src * 31 + dst * 7 + 1) % 255
    ring = base[k:] + base[:k]
    return (ring * (size // 255 + 1))[:size]


def first_difference(a: bytes, b: bytes, chunk: int = 1 << 16) -> int:
    """The first index where ``a`` and ``b`` differ, or the shorter's length. Pure."""
    n = min(len(a), len(b))
    for k in range(0, n, chunk):
        e = min(k + chunk, n)
        if a[k:e] != b[k:e]:
            return next(j for j in range(k, e) if a[j] != b[j])
    return n


def self_test(drv: object, size: int = SIZE, names: Sequence[str] = ()) -> str:
    """Copy ``size`` bytes for each ordered pair of ``drv``'s cards and compare. Returns
    ``''`` on a pass, else the reason: the first pair and byte that differ, or the driver
    error. ``names`` labels the cards in the reason (the part's card numbers); by default
    their visible index. Fewer than two cards fail."""
    try:
        n = drv.count()
        if n < 2:
            return f'{n} card(s) visible: the test needs two'
        label = list(names) if len(names) == n else [str(i) for i in range(n)]
        ctxs = [drv.context(i) for i in range(n)]
        drv.enable_peers(ctxs)
        bufs = [drv.alloc(ctxs[i], size) for i in range(n)]
        try:
            for a in range(n):
                for b in range(n):
                    if a == b:
                        continue
                    want = pattern(a, b, size)
                    drv.upload(ctxs[a], bufs[a], want)
                    drv.fill(ctxs[b], bufs[b], 0, size)
                    drv.peer(ctxs[b], bufs[b], ctxs[a], bufs[a], size)
                    got = drv.download(ctxs[b], bufs[b], size)
                    if got != want:
                        i = first_difference(got, want)
                        if i >= len(got):
                            return f'card {label[a]} -> {label[b]}: {len(got)} of {len(want)} bytes read back'
                        zeros = ' (zeros)' if not got.strip(b'\0') else ''
                        return (f'card {label[a]} -> {label[b]}: byte {i} reads {got[i]:#04x}, '
                                f'not {want[i]:#04x}{zeros}')
        finally:
            for i, p in enumerate(bufs):
                drv.free(ctxs[i], p)
            drv.close()
    except P2PError as e:
        return str(e)
    return ''


class Driver:
    """The CUDA driver API calls the test needs, over ``libcuda.so.1`` (``ctypes``)."""

    def __init__(self, lib: str = 'libcuda.so.1') -> None:
        try:
            self.cu = ctypes.CDLL(lib)
        except OSError as e:
            raise P2PError(f'no CUDA driver: {e}') from None
        c = self.cu
        u64, vp, sz = ctypes.c_ulonglong, ctypes.c_void_p, ctypes.c_size_t
        sigs = {
            'cuInit': [ctypes.c_uint], 'cuDeviceGetCount': [ctypes.POINTER(ctypes.c_int)],
            'cuDeviceGet': [ctypes.POINTER(ctypes.c_int), ctypes.c_int],
            'cuDevicePrimaryCtxRetain': [ctypes.POINTER(vp), ctypes.c_int],
            'cuDevicePrimaryCtxRelease_v2': [ctypes.c_int], 'cuCtxSetCurrent': [vp], 'cuCtxSynchronize': [],
            'cuDeviceCanAccessPeer': [ctypes.POINTER(ctypes.c_int), ctypes.c_int, ctypes.c_int],
            'cuCtxEnablePeerAccess': [vp, ctypes.c_uint],
            'cuMemAlloc_v2': [ctypes.POINTER(u64), sz], 'cuMemFree_v2': [u64],
            'cuMemcpyHtoD_v2': [u64, vp, sz], 'cuMemcpyDtoH_v2': [vp, u64, sz],
            'cuMemsetD8_v2': [u64, ctypes.c_ubyte, sz], 'cuMemcpyPeer': [u64, vp, u64, vp, sz],
            'cuGetErrorName': [ctypes.c_int, ctypes.POINTER(ctypes.c_char_p)],
        }
        for name, args in sigs.items():
            try:
                f = getattr(c, name)
            except AttributeError:
                raise P2PError(f'the CUDA driver has no {name}') from None
            f.argtypes, f.restype = args, ctypes.c_int
        self._call('cuInit', 0)
        self.devs: list[int] = []

    def _call(self, name: str, *args: object, ok: Sequence[int] = (0,)) -> int:
        r = getattr(self.cu, name)(*args)
        if r not in ok:
            s = ctypes.c_char_p()
            self.cu.cuGetErrorName(r, ctypes.byref(s))
            raise P2PError(f'{name} returned {(s.value or b"").decode() or r}')
        return r

    def count(self) -> int:
        n = ctypes.c_int()
        self._call('cuDeviceGetCount', ctypes.byref(n))
        return n.value

    def context(self, i: int) -> int:
        d, ctx = ctypes.c_int(), ctypes.c_void_p()
        self._call('cuDeviceGet', ctypes.byref(d), i)
        self._call('cuDevicePrimaryCtxRetain', ctypes.byref(ctx), d.value)
        self.devs.append(d.value)
        return ctx.value

    def enable_peers(self, ctxs: Sequence[int]) -> None:
        for a, ca in enumerate(ctxs):
            for b, cb in enumerate(ctxs):
                can = ctypes.c_int()
                if a != b:
                    self._call('cuDeviceCanAccessPeer', ctypes.byref(can), self.devs[a], self.devs[b])
                if can.value:
                    self._call('cuCtxSetCurrent', ctypes.c_void_p(ca))
                    self._call('cuCtxEnablePeerAccess', ctypes.c_void_p(cb), 0, ok=(0, _ALREADY_ENABLED))

    def alloc(self, ctx: int, size: int) -> int:
        self._call('cuCtxSetCurrent', ctypes.c_void_p(ctx))
        p = ctypes.c_ulonglong()
        self._call('cuMemAlloc_v2', ctypes.byref(p), size)
        return p.value

    def free(self, ctx: int, ptr: int) -> None:
        try:
            self._call('cuCtxSetCurrent', ctypes.c_void_p(ctx))
            self._call('cuMemFree_v2', ptr)
        except P2PError:
            pass

    def upload(self, ctx: int, ptr: int, data: bytes) -> None:
        self._call('cuCtxSetCurrent', ctypes.c_void_p(ctx))
        self._call('cuMemcpyHtoD_v2', ptr, ctypes.c_char_p(data), len(data))

    def fill(self, ctx: int, ptr: int, value: int, size: int) -> None:
        self._call('cuCtxSetCurrent', ctypes.c_void_p(ctx))
        self._call('cuMemsetD8_v2', ptr, value, size)
        self._call('cuCtxSynchronize')

    def peer(self, dst_ctx: int, dst: int, src_ctx: int, src: int, size: int) -> None:
        self._call('cuCtxSetCurrent', ctypes.c_void_p(dst_ctx))
        self._call('cuMemcpyPeer', dst, ctypes.c_void_p(dst_ctx), src, ctypes.c_void_p(src_ctx), size)
        self._call('cuCtxSynchronize')

    def download(self, ctx: int, ptr: int, size: int) -> bytes:
        self._call('cuCtxSetCurrent', ctypes.c_void_p(ctx))
        buf = ctypes.create_string_buffer(size)
        self._call('cuMemcpyDtoH_v2', buf, ptr, size)
        return buf.raw

    def close(self) -> None:
        for d in self.devs:
            try:
                self._call('cuDevicePrimaryCtxRelease_v2', d)
            except P2PError:
                pass
        self.devs = []


def main() -> int:
    """Run the test on the visible cards; print ``p2p: ok`` or ``p2p: <reason>``."""
    names = [x for x in os.environ.get('CUDA_VISIBLE_DEVICES', '').split(',') if x]
    try:
        why = self_test(Driver(), names=names)
    except P2PError as e:
        why = str(e)
    print(f'p2p: {why or "ok"}', flush=True)
    return 1 if why else 0


if __name__ == '__main__':
    sys.exit(main())
