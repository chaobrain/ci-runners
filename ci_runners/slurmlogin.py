"""What ``ci-run slurm`` runs on a cluster's login host through the account owner's own
login (design decision 14.9): the steps that set an account's ``ci-runners`` tree up and
take it down, which the CI key's gate cannot do (it is forced to ``ci-slurm-gate``).

The driver (``ci_runners.slurmsetup``) sends this module's source, and the gate's for
``facts``, on stdin to the site's ``login_python`` (``slurmsetup.BOOT``) and reads one
JSON value back; nothing is installed for it and nothing stays running. Each function
takes and returns JSON values; a refusal raises ``LoginError``, which the boot prints on
stderr (exit 2).

Python 3.11 or later; stdlib only.
"""
from __future__ import annotations

import getpass
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time

#: the subdirectories of ``<root>/ci-runners``, each mode 700
DIRS = ('releases', 'etc', 'envs', 'work', 'state', 'logs', 'run')
CANCEL_WAIT = 120  # s ``uninstall --cancel`` waits for the account's ci- jobs to end


class LoginError(Exception):
    """A step refused: its reason."""


def _gate():
    m = sys.modules.get('ci_runners.slurmgate')
    if m is None:
        from ci_runners import slurmgate as m
    return m


def _own_dir(path: str) -> bool:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise LoginError(f'{path} is not a directory of this account')
    return True


def _write(path: str, text: str) -> None:
    tmp = f'{path}.{os.getpid()}.new'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(text)
    os.replace(tmp, path)


def facts(root: str, entry: dict) -> dict:
    """``check`` before an install: the gate's ``check`` in a temporary tree under
    ``root``, with ``entry`` as its account, removed afterwards."""
    if not _own_dir(root):
        return {'root': f'{root} does not exist'}
    tmp = tempfile.mkdtemp(prefix='.ci-runners-check-', dir=root)
    try:
        os.mkdir(os.path.join(tmp, 'etc'), 0o700)
        _write(os.path.join(tmp, 'etc', 'account.json'), json.dumps(entry))
        return _gate().check(tmp, os.path.join(tmp, 'releases', 'hook'))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def prepare(base: str) -> dict:
    """``<root>/ci-runners`` and its directories, mode 700; ``root`` must exist."""
    if not _own_dir(os.path.dirname(base)):
        raise LoginError(f'{os.path.dirname(base)} does not exist')
    made = []
    for d in (base, *(os.path.join(base, x) for x in DIRS)):
        if not _own_dir(d):
            os.mkdir(d, 0o700)
            made.append(d)
        os.chmod(d, 0o700)
    return {'made': made}


def install(base: str, tag: str, files: dict) -> dict:
    """``etc/`` files (mode 600) and ``releases/hook`` -> ``tag``, a release ``rsync``
    copied there before."""
    rel = os.path.join(base, 'releases')
    if not _own_dir(os.path.join(rel, tag)) or not os.path.isfile(os.path.join(rel, tag, 'bin', 'ci-slurm-gate')):
        raise LoginError(f'release {tag} is not in place')
    for name, text in sorted(files.items()):
        if '/' in name or name.startswith('.'):
            raise LoginError(f'etc/{name}: not a plain name')
        _write(os.path.join(base, 'etc', name), text)
    hook = os.path.join(rel, 'hook')
    if os.path.lexists(hook) and not os.path.islink(hook):
        raise LoginError(f'{hook} is not a link')
    tmp = f'{hook}.{os.getpid()}.new'
    os.symlink(tag, tmp)
    os.replace(tmp, hook)
    return {'hook': tag, 'etc': sorted(files)}


def _jobs(slurm_account: str) -> list[str]:
    """The account's ``ci-`` jobs that are still there: ``id`` each."""
    r = subprocess.run(['squeue', '-h', '-A', slurm_account, '-u', getpass.getuser(), '-o', '%i|%j|%T'],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise LoginError(f'squeue failed: {(r.stderr or "").strip()[-200:]}')
    return [line.split('|')[0] for line in r.stdout.splitlines() if line.count('|') == 2 and
            line.split('|')[1].startswith('ci-')]


def ci_jobs(slurm_account: str) -> dict:
    return {'jobs': _jobs(slurm_account)}


def uninstall(base: str, slurm_account: str, gate: str) -> dict:
    """Take the account's install down, in an order that lets nothing new start: the
    ``authorized_keys`` lines that force ``gate`` go first (no new submit), then every
    ``ci-`` job of the account is cancelled and its end awaited, then the tree ``base`` is
    removed. A link in place of ``authorized_keys`` or of ``base`` refuses."""
    if os.path.basename(base) != 'ci-runners':
        raise LoginError(f'{base} is not a ci-runners tree')
    present = _own_dir(base)
    keys = os.path.join(os.path.expanduser('~'), '.ssh', 'authorized_keys')
    removed = 0
    try:
        fd = os.open(keys, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        fd = None
    except OSError:
        raise LoginError(f'{keys} is a link: edit it by hand') from None
    if fd is not None:
        with os.fdopen(fd, encoding='utf-8') as f:
            mode = stat.S_IMODE(os.fstat(f.fileno()).st_mode)
            lines = f.read().splitlines(keepends=True)
        keep = [x for x in lines if f'command="{gate}"' not in x]
        removed = len(lines) - len(keep)
        if removed:
            _write(keys, ''.join(keep))
            os.chmod(keys, mode)
    jobs = _jobs(slurm_account)
    if jobs:
        subprocess.run(['scancel', *jobs], capture_output=True, timeout=60)
        end = time.monotonic() + CANCEL_WAIT
        while (left := _jobs(slurm_account)) and time.monotonic() < end:
            time.sleep(2)
        if left:
            raise LoginError(f'ci- jobs still there after scancel: {" ".join(left)}')
    if present:
        shutil.rmtree(base)
    return {'cancelled': jobs, 'key_lines': removed, 'removed': present}
