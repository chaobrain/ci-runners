"""Interop with brainspike's host lock and slots (decision 12.9, D5, §7; RUN-7
acceptance 2).

``ci-lock`` and ``host_lock.sh`` (and ``host_lock.py``, the same protocol in Python)
exclude each other in every mode pair, in both directions, keep the turnstile's writer
preference and the light entry across implementations, and honour each other's nesting
cover. ``ci-slot`` and ``ci_slot.sh`` together never hold more than the slot count.

Two accounts. The CI account and the local account share the files: root-owned in
/tmp with ACL entries for both, the queue directory with default ACL entries (12.9).
The tests run as one account and show the two-account part through what each side
needs from the files: ours are opened without ``O_CREAT`` (``fs.protected_regular``),
never created in a sticky directory, and what either side creates in the queue
directory carries the default ACL's ``rw`` to a second account's entry (``TwoAccounts``).

The originals are copies of brainspike's ``scripts/host_lock.sh``, ``host_lock.py`` and
``ci_slot.sh`` (``tests/fixtures/brainspike/``, see its README); set
``CI_RUNNERS_INTEROP_SRC`` to a brainspike ``scripts/`` directory to test a live
checkout instead. Linux with bash and flock(1) only.
"""
from __future__ import annotations

import itertools
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from ci_runners import locks, slots

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = pathlib.Path(os.environ.get('CI_RUNNERS_INTEROP_SRC') or ROOT / 'tests' / 'fixtures' / 'brainspike')
HOST_LOCK_SH = SRC / 'host_lock.sh'
CI_SLOT_SH = SRC / 'ci_slot.sh'
HOST_LOCK_PY = SRC / 'host_lock.py'
CI_LOCK = ROOT / 'bin' / 'ci-lock'
CI_SLOT = ROOT / 'bin' / 'ci-slot'
PY = sys.executable

RUNNABLE = sys.platform.startswith('linux') and shutil.which('flock') and shutil.which('bash')


def wait_for(path: pathlib.Path, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if path.exists():
            return True
        time.sleep(0.01)
    return path.exists()


@unittest.skipUnless(RUNNABLE, 'Linux, bash and flock(1) only')
class LockInterop(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix='ci-lock-'))
        self.lock = str(self.dir / 'host.lock')
        self.env = dict(os.environ, LOCAL_CI_HOST_LOCK=self.lock, CI_RUNNERS_PYTHON=PY)
        self.env.pop('BRAINSPIKE_HOST_LOCK', None)
        self.env.pop('CI_HOST_LOCK', None)
        self.procs = []

    def tearDown(self):
        for p in self.procs:  # the holder and its command: kill the whole session
            try:
                os.killpg(p.pid, 9)
            except ProcessLookupError:
                pass
            p.wait()
        shutil.rmtree(self.dir, ignore_errors=True)

    def start(self, impl: str, mode: str, tag: str, body: str = '') -> subprocess.Popen:
        """A holder that marks ``<tag>.in``, runs ``body``, waits for ``<tag>.go`` and
        marks ``<tag>.out``."""
        d = self.dir
        script = (f'touch {d}/{tag}.in; {body} while [ ! -e {d}/{tag}.go ] && [ -d {d} ]; do sleep 0.01; done; '
                  f'touch {d}/{tag}.out')  # the loop ends with the test's directory
        if impl == 'bp':  # brainspike's host_lock.py: a HostLock held around the command (s or x only)
            drive = ('import subprocess, sys; sys.path.insert(0, sys.argv[1]); from host_lock import HostLock; '
                     'h = HostLock(); h.acquire(sys.argv[2]); r = subprocess.call(sys.argv[3:]); h.release(); '
                     'sys.exit(r)')
            argv = [PY, '-I', '-c', drive, str(SRC), mode, 'bash', '-c', script]
        else:
            argv = ([str(HOST_LOCK_SH)] if impl == 'sh' else [str(CI_LOCK)]) + [f'-{mode}', 'bash', '-c', script]
        p = subprocess.Popen(argv, env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        self.procs.append(p)
        return p

    def inside(self, tag: str, timeout: float = 5.0) -> bool:
        return wait_for(self.dir / f'{tag}.in', timeout)

    def go(self, tag: str) -> None:
        (self.dir / f'{tag}.go').touch()

    def test_every_mode_pair_both_ways(self):
        pairs = [('sh', 'py'), ('py', 'sh'), ('py', 'py'), ('bp', 'py'), ('py', 'bp')]
        for (ia, ib), (ma, mb) in itertools.product(pairs, itertools.product('sxl', repeat=2)):
            if (ia == 'bp' and ma == 'l') or (ib == 'bp' and mb == 'l'):
                continue  # host_lock.py takes s and x; its light mode is host_lock.sh's
            with self.subTest(a=f'{ia}-{ma}', b=f'{ib}-{mb}'):
                a, b = f'a{ia}{ma}{ib}{mb}', f'b{ia}{ma}{ib}{mb}'
                self.start(ia, ma, a)
                self.assertTrue(self.inside(a))
                pb = self.start(ib, mb, b)
                if 'x' in (ma, mb):
                    self.assertFalse(self.inside(b, 0.4), 'entered beside an exclusive holder')
                    self.go(a)
                    self.assertTrue(self.inside(b))
                else:
                    self.assertTrue(self.inside(b), 'two shared holders exclude each other')
                    self.go(a)
                self.go(b)
                self.assertEqual(pb.wait(10), 0)

    def test_a_waiting_exclusive_request_keeps_new_shared_ones_out(self):
        for first, writer, late in (('sh', 'py', 'sh'), ('py', 'sh', 'py'), ('sh', 'sh', 'py'), ('bp', 'py', 'bp')):
            with self.subTest(first=first, writer=writer, late=late):
                t = first + writer + late
                self.start(first, 's', f's1{t}')
                self.assertTrue(self.inside(f's1{t}'))
                self.start(writer, 'x', f'x{t}')
                time.sleep(0.3)  # the exclusive request holds the turnstile now
                self.start(late, 's', f's2{t}')
                self.assertFalse(self.inside(f's2{t}', 0.4))
                self.go(f's1{t}')
                self.assertTrue(self.inside(f'x{t}'))
                self.assertFalse(self.inside(f's2{t}', 0.3))
                self.go(f'x{t}')
                self.assertTrue(self.inside(f's2{t}'))
                self.go(f's2{t}')

    def test_light_enters_beside_a_normal_shared_holder_only(self):
        for holder, writer, light in (('sh', 'sh', 'py'), ('py', 'py', 'sh'), ('py', 'sh', 'py'), ('bp', 'py', 'py')):
            with self.subTest(holder=holder, writer=writer, light=light):
                t = holder + writer + light
                self.start(holder, 's', f's{t}')
                self.assertTrue(self.inside(f's{t}'))
                self.start(writer, 'x', f'x{t}')
                time.sleep(0.3)
                self.start(light, 'l', f'l{t}')
                self.assertTrue(self.inside(f'l{t}'), 'a light request waited behind the exclusive one')
                self.go(f's{t}')
                self.go(f'l{t}')
                self.assertTrue(self.inside(f'x{t}'))
                self.go(f'x{t}')
        # with only a light holder in, a light request queues behind the waiting writer
        self.start('sh', 'l', 'l1')
        self.assertTrue(self.inside('l1'))
        self.start('py', 'x', 'xw')
        time.sleep(0.3)
        self.start('py', 'l', 'l2')
        self.assertFalse(self.inside('l2', 0.4))
        self.go('l1')
        self.assertTrue(self.inside('xw'))
        self.go('xw')
        self.assertTrue(self.inside('l2'))
        self.go('l2')

    def test_nesting_covers_cross_implementations(self):
        out = self.dir / 'nested'
        cases = [
            ([str(HOST_LOCK_SH), '-x', str(CI_LOCK), '-s', 'touch', str(out)], 0),
            ([str(CI_LOCK), '-x', str(HOST_LOCK_SH), '-s', 'touch', str(out)], 0),
            ([str(CI_LOCK), '-s', str(HOST_LOCK_SH), '-s', 'touch', str(out)], 0),
            ([str(HOST_LOCK_SH), '-x', str(CI_LOCK), '-x', 'touch', str(out)], 0),
            ([str(CI_LOCK), '-x', str(HOST_LOCK_SH), '-x', 'touch', str(out)], 0),
            ([str(HOST_LOCK_SH), '-s', str(CI_LOCK), '-x', 'touch', str(out)], 3),
            ([str(CI_LOCK), '-s', str(HOST_LOCK_SH), '-x', 'touch', str(out)], 3),
        ]
        for argv, code in cases:
            with self.subTest(argv=' '.join(a.rsplit('/', 1)[-1] for a in argv[:4])):
                if out.exists():
                    out.unlink()
                r = subprocess.run(argv, env=self.env, capture_output=True, timeout=20)
                self.assertEqual(r.returncode, code, r.stderr.decode())
                self.assertEqual(out.exists(), code == 0)

    def test_a_stale_cover_takes_the_lock(self):
        env = dict(self.env, BRAINSPIKE_HOST_LOCK='x')  # nobody holds it
        r = subprocess.run([str(CI_LOCK), '-s', 'true'], env=env, capture_output=True, timeout=20)
        self.assertEqual(r.returncode, 0)
        self.assertIn(b'stale', r.stderr)
        r = subprocess.run([str(CI_LOCK), '-s', 'true'], env=dict(self.env, BRAINSPIKE_HOST_LOCK='y'),
                           capture_output=True, timeout=20)
        self.assertEqual(r.returncode, 2)

    def test_the_command_does_not_inherit_the_lock(self):
        # the command leaves a child behind; the lock is free once ci-lock exits
        r = subprocess.run([str(CI_LOCK), '-x', 'bash', '-c', 'sleep 30 >/dev/null 2>&1 & echo $!'], env=self.env,
                           capture_output=True, timeout=20)
        child = int(r.stdout)
        try:
            r2 = subprocess.run(['flock', '-n', '-x', self.lock, 'true'], timeout=10)
            self.assertEqual(r2.returncode, 0)
        finally:
            os.kill(child, 9)

    def test_a_timeout(self):
        self.start('sh', 'x', 'hold')
        self.assertTrue(self.inside('hold'))
        t0 = time.monotonic()
        r = subprocess.run([str(CI_LOCK), '--timeout', '0.5', '-s', 'true'], env=self.env, capture_output=True,
                           timeout=20)
        self.assertEqual(r.returncode, 4)
        self.assertLess(time.monotonic() - t0, 5)
        self.go('hold')


@unittest.skipUnless(RUNNABLE, 'Linux, bash and flock(1) only')
class SlotInterop(unittest.TestCase):
    def test_together_never_more_than_the_slot_count(self):
        with tempfile.TemporaryDirectory(prefix='ci-slot-') as d:
            d = pathlib.Path(d)
            lock, log = d / 'slot.lock', d / 'log'
            env = dict(os.environ, LOCAL_CI_SLOT_LOCK=str(lock), LOCAL_CI_SLOTS='2', LOCAL_CI_SLOT_POLL='0.1',
                       CI_RUNNERS_PYTHON=PY)
            body = (f'flock {d}/log.lock -c "echo in >> {log}"; sleep 0.3; '
                    f'flock {d}/log.lock -c "echo out >> {log}"')
            procs = []
            for i in range(10):
                if i % 2:
                    argv = [str(CI_SLOT_SH), 'bash', '-c', body]
                else:
                    project = 'p' if i % 4 else 'q'
                    argv = [str(CI_SLOT), '--project', project, '--default', 'p', '--cap', 'q=1', '--poll', '0.1',
                            'bash', '-c', body]
                procs.append(subprocess.Popen(argv, env=env, stderr=subprocess.DEVNULL))
            for p in procs:
                self.assertEqual(p.wait(60), 0)
            events = log.read_text().split()
            depth = peak = 0
            for e in events:
                depth += 1 if e == 'in' else -1
                peak = max(peak, depth)
            self.assertEqual(events.count('in'), 10)
            self.assertEqual(events.count('out'), 10)
            self.assertEqual(peak, 2)
            self.assertEqual([n for n in os.listdir(d / 'slot.queue') if n.endswith(('.ticket', '.wait'))], [])


def wait_for_glob(d: pathlib.Path, pattern: str, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if d.is_dir() and any(d.glob(pattern)):
            return True
        time.sleep(0.01)
    return False


def acl_effective(path: str, uid: int) -> str:
    """The effective permissions of the named-user ACL entry ``uid`` on ``path``
    (``getfacl``: the entry, masked by the mask), or '' without such an entry."""
    out = subprocess.run(['getfacl', '-n', '--omit-header', '-E', path], capture_output=True, text=True,
                         check=True).stdout
    entry = mask = None
    for line in out.splitlines():
        if line.startswith(f'user:{uid}:'):
            entry = line.split()[0].split(':')[2]
        elif line.startswith('mask::'):
            mask = line.split()[0].split(':')[2]
    if entry is None:
        return ''
    return ''.join(c if c != '-' and (mask is None or mask[i] != '-') else '-' for i, c in enumerate(entry))


OTHER = 65534  # the second account's uid in the ACL entries: any uid but ours


def acl_ok(d: str) -> bool:
    try:
        return subprocess.run(['setfacl', '-m', f'u:{OTHER}:rwx,d:u:{OTHER}:rwx', d], capture_output=True).returncode == 0
    except OSError:
        return False


@unittest.skipUnless(RUNNABLE, 'Linux, bash and flock(1) only')
class TwoAccounts(unittest.TestCase):
    """What the two accounts need from the shared files (decision 12.9, §5.3)."""

    def test_existing_files_are_opened_without_o_creat(self):
        """``fs.protected_regular`` refuses an O_CREAT open of another account's file in
        /tmp, so neither the host lock's three files nor a slot file is opened with it."""
        with tempfile.TemporaryDirectory(prefix='ci-lock-') as d:
            lock = os.path.join(d, 'host.lock')
            shared = list(locks.paths(lock))
            q = slots.SlotQueue(os.path.join(d, 'slot.lock'), 2)
            shared += q.slots
            for p in shared:
                pathlib.Path(p).touch()
            calls = []
            real = os.open

            def spy(path, flags, *a, **k):
                calls.append((os.path.abspath(path), flags))
                return real(path, flags, *a, **k)
            with mock.patch('os.open', spy):
                h = locks.HostLock(lock, held='')
                for mode in 'sxl':
                    h.acquire(mode)
                    h.release()
                holder = locks.HostLock(lock, held='')
                holder.acquire('s')  # a normal shared holder in: the light request enters beside it
                light = locks.HostLock(lock, held='')
                light.acquire('l')
                light.release()
                holder.release()
                q.take_ticket('p', 'cmd')
                fd = q.try_take('p', {'p': 2})
            self.assertIsNotNone(fd)
            os.close(fd)
            opened = {p for p, _ in calls}
            self.assertLessEqual(set(shared) - set(q.slots[1:]), opened)  # every file was opened
            for path, flags in calls:
                if path in shared:
                    self.assertFalse(flags & os.O_CREAT, path)

    def test_a_missing_shared_file_in_a_sticky_directory_is_not_created(self):
        """A lock file or a queue directory of ours in /tmp would lock the other account
        out until tmpfiles.d recreates it at boot: refused (status 2), nothing created."""
        with tempfile.TemporaryDirectory(prefix='ci-sticky-') as d:
            os.chmod(d, 0o1777)
            env = dict(os.environ, CI_RUNNERS_PYTHON=PY, LOCAL_CI_HOST_LOCK=os.path.join(d, 'host.lock'),
                       LOCAL_CI_SLOT_LOCK=os.path.join(d, 'slot.lock'))
            env.pop('BRAINSPIKE_HOST_LOCK', None)
            env.pop('CI_HOST_LOCK', None)
            r = subprocess.run([str(CI_LOCK), '-s', 'true'], env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertIn('tmpfiles.d', r.stderr)
            r = subprocess.run([str(CI_SLOT), '--project', 'p', '--default', 'p', 'true'], env=env,
                               capture_output=True, text=True, timeout=20)
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertEqual(os.listdir(d), [])
            os.makedirs(os.path.join(d, 'slot.queue'))  # the queue is there, a slot file is not
            r = subprocess.run([str(CI_SLOT), '--project', 'p', '--default', 'p', 'true'], env=env,
                               capture_output=True, text=True, timeout=20)
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertFalse(os.path.exists(os.path.join(d, 'slot.lock')))
            os.chmod(d, 0o700)  # outside a sticky directory both create what they need
            self.assertEqual(subprocess.run([str(CI_LOCK), '-s', 'true'], env=env, timeout=20).returncode, 0)
            self.assertTrue(os.path.exists(os.path.join(d, 'host.lock')))

    def test_queue_files_give_the_other_account_rw(self):
        """The queue directory's default ACL entries reach every file either side creates
        in it: our tickets, ``seq``, ``seq.lock``, ``take.lock`` and holder records, and
        ci_slot.sh's tickets. A file created with a narrower mode would mask them out."""
        with tempfile.TemporaryDirectory(prefix='ci-acl-') as d:
            queue = os.path.join(d, 'slot.queue')
            os.makedirs(queue)
            if not acl_ok(queue) or not shutil.which('getfacl'):
                self.skipTest('no ACL support here (setfacl/getfacl)')
            q = slots.SlotQueue(os.path.join(d, 'slot.lock'), 1, queue, poll=0.05)
            old = os.umask(0o077)  # a umask does not narrow the default ACL's entries
            try:
                ticket = q.take_ticket('p', 'cmd')
                self.assertEqual(acl_effective(ticket, OTHER), 'rw-', 'ticket')
                fd = q.try_take('p', {'p': 1})
            finally:
                os.umask(old)
            self.assertIsNotNone(fd)
            try:
                for name in ('seq', 'seq.lock', 'take.lock', 'holder.1'):
                    self.assertEqual(acl_effective(os.path.join(queue, name), OTHER)[:2], 'rw', name)
                env = dict(os.environ, LOCAL_CI_SLOT_LOCK=q.slots[0], LOCAL_CI_SLOTS='1', LOCAL_CI_SLOT_POLL='0.1')
                sh = subprocess.Popen([str(CI_SLOT_SH), 'true'], env=env, stderr=subprocess.DEVNULL,
                                      start_new_session=True)  # waits: we hold the one slot
                try:
                    self.assertTrue(wait_for_glob(pathlib.Path(queue), '*.ticket', 10))
                    for t in pathlib.Path(queue).glob('*.ticket'):
                        self.assertEqual(acl_effective(str(t), OTHER)[:2], 'rw', t.name)
                finally:
                    os.close(fd)
                    fd = None
                    self.assertEqual(sh.wait(30), 0)
            finally:
                if fd is not None:
                    os.close(fd)


if __name__ == '__main__':
    unittest.main()
