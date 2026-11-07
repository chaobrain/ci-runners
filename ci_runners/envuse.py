"""Which build of a project's env a live part uses (design §4.9, decision 12.11).

``<data>/ci-runners/envs/<project>`` is a link that ``env build`` and ``env refresh`` move.
A part resolves it once, at its start, and holds a shared ``flock`` on ``<build>/.inuse``
for its life; the supervisor inherits the descriptor, so the lock outlives the executor.
The prune of old builds removes a build only under an exclusive, non-blocking ``flock`` on
the same file, so a build a part holds is never removed. Anything uncertain keeps the build.
"""
from __future__ import annotations

import fcntl
import os
import shutil

INUSE = '.inuse'


def hold(link: str) -> tuple[str, int | None]:
    """The build directory ``link`` names now, and a descriptor holding it (None when it
    cannot be held: the path is no directory, or ``.inuse`` cannot be opened). The caller
    keeps the descriptor open for as long as the build is in use."""
    for _ in range(5):
        build = os.path.realpath(link)
        if not os.path.isdir(build):
            return link, None
        try:
            fd = os.open(os.path.join(build, INUSE), os.O_RDONLY | os.O_CREAT | os.O_CLOEXEC, 0o600)
        except OSError:
            return build, None
        fcntl.flock(fd, fcntl.LOCK_SH)  # waits only for a prune that is deleting this build
        if os.path.isdir(build):
            return build, fd
        os.close(fd)  # the build went while we waited: take what the link names now
    return os.path.realpath(link), None


def remove_unused(build: str) -> bool:
    """Remove ``build`` unless a part holds it; True if removed. Fail-safe: a lock that
    cannot be taken or tested keeps the build."""
    try:
        fd = os.open(os.path.join(build, INUSE), os.O_RDONLY | os.O_CREAT | os.O_CLOEXEC, 0o600)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        shutil.rmtree(build, ignore_errors=True)
        return True
    finally:
        os.close(fd)
