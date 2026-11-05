"""The reaper (design §5.3, ``ci-reaper.timer``): what stopped jobs leave behind.

RUN-1b removes the dated hook logs (``logs/hook-<date>.log``) and the part records of
runs (``state/parts/<owner>/<repo>/<run>/``) that nothing touched for 14 days: a run
counts by the newest change anywhere under it, so a run still writing is never reaped.
RUN-1b-3 adds the part scopes of gone jobs and stale leases.
"""
from __future__ import annotations

import os
import shutil
import time

KEEP_DAYS = 14
RUN_DEPTH = 3  # state/parts/<owner>/<repo>/<run>


def newest(path: str) -> float:
    """The newest mtime of ``path`` and everything under it (links not followed)."""
    t = os.lstat(path).st_mtime
    if os.path.isdir(path) and not os.path.islink(path):
        for top, dirs, files in os.walk(path, onerror=lambda _e: None):
            for n in dirs + files:
                try:
                    t = max(t, os.lstat(os.path.join(top, n)).st_mtime)
                except OSError:
                    pass
    return t


def _entries(root: str, depth: int) -> list[str]:
    """The entries ``depth`` levels under ``root`` (real directories only on the way)."""
    level = [root]
    for _ in range(depth):
        nxt = []
        for d in level:
            if os.path.islink(d) or not os.path.isdir(d):
                continue
            try:
                nxt += [os.path.join(d, n) for n in sorted(os.listdir(d))]
            except OSError:
                pass
        level = nxt
    return level


def old_entries(root: str, now: float, days: float = KEEP_DAYS, depth: int = 1) -> list[str]:
    """The entries ``depth`` levels under ``root`` with nothing under them changed for ``days``."""
    out = []
    for p in _entries(root, depth):
        try:
            if now - newest(p) > days * 86400:
                out.append(p)
        except OSError:
            pass
    return out


def _remove(p: str) -> None:
    if os.path.isdir(p) and not os.path.islink(p):
        shutil.rmtree(p, ignore_errors=True)
    else:
        os.unlink(p)


def reap(data: str, now: float | None = None) -> str:
    now = time.time() if now is None else now
    base = os.path.join(data, 'ci-runners')
    gone = [p for p in old_entries(os.path.join(base, 'logs'), now) if os.path.basename(p).startswith('hook-')]
    parts = os.path.join(base, 'state', 'parts')
    gone += old_entries(parts, now, depth=RUN_DEPTH)
    for p in gone:
        _remove(p)
    for depth in range(RUN_DEPTH - 1, 0, -1):  # the owner and repository directories left empty
        for d in _entries(parts, depth):
            try:
                os.rmdir(d)
            except OSError:
                pass
    return f'reaper: removed {len(gone)} old entries'
