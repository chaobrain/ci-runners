"""The hooks' copies in the CI account's private ``/tmp`` (design decision 13.5).

On a host whose CI account runs in its own mount namespace (12.16: the inventory names
``local_accounts``), ``ci-hooks.service`` copies ``job-started.sh`` and
``job-completed.sh`` from ``<data>/ci-runners/hooks/`` into ``/tmp/ci-runners-hooks/``,
and every runner's ``.env`` names the copies, so the "Run '<hook path>'" line of a job log
names no data disk. The directory is the account's, mode 700, and refused if it is a link
or another account's; each copy is written under a temporary name, mode 500, and renamed
in. The reaper rewrites a missing or different copy (``refresh``). Elsewhere the runners
keep the hooks under ``data``: in a shared ``/tmp`` another account could plant the
directory first.

CLI (as the CI account, from ``ci-hooks.service``)::

    ci-run hooks copy      copy both hooks into /tmp/ci-runners-hooks/
"""
from __future__ import annotations

import os
import stat
import sys
from collections.abc import Mapping

DIR = '/tmp/ci-runners-hooks'
NAMES = ('job-started.sh', 'job-completed.sh')
MODE = 0o500


class CopyError(RuntimeError):
    pass


def used(h: Mapping[str, object]) -> bool:
    """Whether this host's runners run the copies: its CI account has the private
    namespace (decision 12.16), which setup plans where the inventory names local
    accounts. Pure."""
    return bool(h.get('local_accounts'))


def hooks_dir(h: Mapping[str, object]) -> str:
    """The directory the runners' ``.env`` names for the hooks. Pure."""
    return DIR if used(h) else os.path.join(str(h['data']), 'ci-runners', 'hooks')


def private_tmp(mountinfo: str) -> bool:
    """Whether ``/tmp`` in this mount table (``/proc/self/mountinfo``) is a ``PrivateTmp``
    one: the last mount at ``/tmp`` comes from a ``systemd-private-`` directory. Outside
    the account's user manager (a ``sudo -u`` shell) it is the host's, where a copy would
    land in the shared ``/tmp``, out of the runners' sight. Pure."""
    roots = [f[3] for f in (line.partition(' - ')[0].split() for line in mountinfo.splitlines())
             if len(f) >= 5 and f[4] == '/tmp']
    return bool(roots) and '/systemd-private-' in roots[-1]


def _mountinfo() -> str:
    try:
        with open('/proc/self/mountinfo', encoding='utf-8', errors='replace') as f:
            return f.read(4 << 20)
    except OSError:
        return ''


def _source(data: str, name: str) -> bytes:
    path = os.path.join(data, 'ci-runners', 'hooks', name)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as e:
        raise CopyError(f'{path}: {e.strerror}') from None
    with os.fdopen(fd, 'rb') as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            raise CopyError(f'{path} is not a regular file')
        return f.read(1 << 20)


def _directory(dest: str) -> None:
    """``dest`` as the account's own directory, mode 700; refused if it is a link,
    not a directory or another account's."""
    try:
        os.mkdir(dest, 0o700)
    except FileExistsError:
        pass
    try:
        fd = os.open(dest, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        raise CopyError(f'{dest} is not a directory (a link?): refused') from None
    try:
        st = os.fstat(fd)
        if st.st_uid != os.getuid():
            raise CopyError(f'{dest} belongs to uid {st.st_uid}, not this account: refused')
        if stat.S_IMODE(st.st_mode) != 0o700:
            os.fchmod(fd, 0o700)
    finally:
        os.close(fd)


def _same(path: str, content: bytes) -> bool:
    """The copy at ``path`` is a regular file of mode 500 holding ``content``."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return False
    with os.fdopen(fd, 'rb') as f:
        st = os.fstat(f.fileno())
        return (stat.S_ISREG(st.st_mode) and stat.S_IMODE(st.st_mode) == MODE and st.st_uid == os.getuid()
                and f.read(len(content) + 1) == content)


def copy(data: str, dest: str | None = None, only_changed: bool = False) -> list[str]:
    """Copy both hooks from ``<data>/ci-runners/hooks/`` into ``dest``, and return the
    names written. ``only_changed`` (the reaper) leaves a copy that matches its source.
    Raises CopyError when a source or the directory is refused: the unit fails, and the
    runners, which require it, do not start."""
    sources = {n: _source(data, n) for n in NAMES}  # both read before anything is written
    dest = DIR if dest is None else dest
    _directory(dest)
    written = []
    for n, content in sources.items():
        path = os.path.join(dest, n)
        if only_changed and _same(path, content):
            continue
        tmp = os.path.join(dest, f'.{n}.{os.getpid()}.new')
        if os.path.lexists(tmp):
            os.unlink(tmp)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, MODE)
        try:
            os.fchmod(fd, MODE)  # whatever the umask
            os.write(fd, content)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
        written.append(n)
    return written


def refresh(h: Mapping[str, object]) -> str:
    """The reaper's pass: rewrite a missing or different copy, on a host that uses them;
    one line for its log, empty when nothing was done."""
    if not used(h):
        return ''
    if not private_tmp(_mountinfo()):
        return 'hook copies: refused (/tmp is not private here)'
    try:
        written = copy(str(h['data']), only_changed=True)
    except (CopyError, OSError) as e:
        return f'hook copies: refused ({e})'
    return f'hook copies: rewrote {", ".join(written)}' if written else ''


def main(argv: list[str], h: Mapping[str, object]) -> int:
    if argv != ['copy']:
        print('usage: ci-run hooks copy', file=sys.stderr)
        return 2
    if not used(h):
        print('ci-run hooks copy: this host keeps its hooks under data (no private /tmp, decision 13.5)',
              file=sys.stderr)
        return 2
    if not private_tmp(_mountinfo()):
        print('ci-run hooks copy: /tmp is not private here: run it as ci-hooks.service, in the account\'s '
              'user manager (decision 13.5)', file=sys.stderr)
        return 2
    try:
        written = copy(str(h['data']))
    except (CopyError, OSError) as e:
        print(f'ci-run hooks copy: {e}', file=sys.stderr)
        return 1
    print(f'hooks: copied {" ".join(written)} to {DIR}')
    return 0
