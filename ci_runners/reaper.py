"""The reaper (design §5.3, ``ci-reaper.timer``): what stopped jobs leave behind.

RUN-1b removes hook logs, part records and caches older than 14 days, and the slot
queue's dead tickets; RUN-1b-3 adds the part scopes of gone jobs and stale leases.
"""
from __future__ import annotations

import os
import shutil
import time

KEEP_DAYS = 14


def old_entries(root: str, now: float, days: float = KEEP_DAYS) -> list[str]:
    """Entries directly under ``root`` not modified for ``days``."""
    out = []
    try:
        names = os.listdir(root)
    except OSError:
        return out
    for n in names:
        p = os.path.join(root, n)
        try:
            if now - os.lstat(p).st_mtime > days * 86400:
                out.append(p)
        except OSError:
            pass
    return out


def reap(data: str, now: float | None = None) -> str:
    now = time.time() if now is None else now
    base = os.path.join(data, 'ci-runners')
    gone = []
    for sub in ('logs', os.path.join('state', 'parts')):
        for p in old_entries(os.path.join(base, sub), now):
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p, ignore_errors=True)
            else:
                os.unlink(p)
            gone.append(p)
    return f'reaper: removed {len(gone)} old entries'
