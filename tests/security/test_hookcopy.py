"""The hooks' copies in the CI account's private /tmp (design decision 13.5)."""
from __future__ import annotations

import contextlib
import io
import os
import pathlib
import stat
import tempfile
import unittest
from unittest import mock

from ci_runners.security import hookcopy

HOST = '22 1 250:2 / / rw,relatime shared:1 - ext4 /dev/x rw\n'
PRIVATE = (HOST + '600 22 250:2 /tmp/systemd-private-0123abcd-user@4242.service-XyZ/tmp /tmp rw,relatime '
                  'shared:300 - ext4 /dev/x rw\n')
STARTED, COMPLETED = b'#!/bin/sh\necho started\n', b'#!/bin/sh\necho completed\n'


class Copy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = pathlib.Path(self.tmp.name)
        self.data, self.dest = t / 'data', t / 'private-tmp' / 'ci-runners-hooks'
        self.src = self.data / 'ci-runners' / 'hooks'
        self.src.mkdir(parents=True)
        self.dest.parent.mkdir()
        (self.src / 'job-started.sh').write_bytes(STARTED)
        (self.src / 'job-completed.sh').write_bytes(COMPLETED)
        self.h = {'data': str(self.data), 'local_accounts': ['localuser']}
        patcher = mock.patch.object(hookcopy, 'DIR', str(self.dest))
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(hookcopy, '_mountinfo', lambda: PRIVATE)  # as in ci-hooks.service
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        for p in self.dest.parent.rglob('*'):
            if p.is_dir() and not p.is_symlink():
                p.chmod(0o700)
        self.tmp.cleanup()

    def mode(self, p):
        return stat.S_IMODE(os.lstat(p).st_mode)

    def test_both_copies_mode_500_in_a_directory_mode_700(self):
        self.assertEqual(hookcopy.copy(str(self.data)), ['job-started.sh', 'job-completed.sh'])
        self.assertEqual(self.mode(self.dest), 0o700)
        for n, content in (('job-started.sh', STARTED), ('job-completed.sh', COMPLETED)):
            self.assertEqual((self.dest / n).read_bytes(), content)
            self.assertEqual(self.mode(self.dest / n), 0o500)
        self.assertEqual(sorted(os.listdir(self.dest)), ['job-completed.sh', 'job-started.sh'])  # no temporary left
        self.dest.chmod(0o755)
        old = os.umask(0o777)  # a umask that would leave the copies unreadable
        try:
            hookcopy.copy(str(self.data))
        finally:
            os.umask(old)
        self.assertEqual(self.mode(self.dest), 0o700)  # the account's directory, mode 700 again
        self.assertEqual(self.mode(self.dest / 'job-started.sh'), 0o500)

    def test_a_link_a_file_or_another_accounts_directory_is_refused(self):
        other = self.dest.parent / 'elsewhere'
        other.mkdir()
        self.dest.symlink_to(other)
        with self.assertRaisesRegex(hookcopy.CopyError, 'not a directory'):
            hookcopy.copy(str(self.data))
        self.assertEqual(os.listdir(other), [])  # nothing written through the link
        self.dest.unlink()
        self.dest.write_text('')
        with self.assertRaisesRegex(hookcopy.CopyError, 'not a directory'):
            hookcopy.copy(str(self.data))
        self.dest.unlink()
        self.dest.mkdir(mode=0o700)
        with mock.patch('os.getuid', return_value=os.getuid() + 1):
            with self.assertRaisesRegex(hookcopy.CopyError, 'not this account'):
                hookcopy.copy(str(self.data))
        self.assertEqual(os.listdir(self.dest), [])

    def test_a_missing_or_linked_source_writes_nothing(self):
        (self.src / 'job-completed.sh').unlink()
        with self.assertRaisesRegex(hookcopy.CopyError, 'job-completed.sh'):
            hookcopy.copy(str(self.data))
        self.assertFalse(self.dest.exists())  # both read before anything is made
        (self.src / 'job-completed.sh').symlink_to(self.src / 'job-started.sh')
        with self.assertRaises(hookcopy.CopyError):
            hookcopy.copy(str(self.data))
        (self.src / 'job-completed.sh').unlink()
        os.mkfifo(self.src / 'job-completed.sh')  # never blocks
        with self.assertRaisesRegex(hookcopy.CopyError, 'not a regular file'):
            hookcopy.copy(str(self.data))

    def test_only_a_missing_or_different_copy_is_rewritten(self):
        hookcopy.copy(str(self.data))
        self.assertEqual(hookcopy.copy(str(self.data), only_changed=True), [])
        (self.dest / 'job-started.sh').unlink()
        self.assertEqual(hookcopy.copy(str(self.data), only_changed=True), ['job-started.sh'])
        (self.src / 'job-completed.sh').write_bytes(COMPLETED + b'# changed\n')
        self.assertEqual(hookcopy.copy(str(self.data), only_changed=True), ['job-completed.sh'])
        self.assertEqual((self.dest / 'job-completed.sh').read_bytes(), COMPLETED + b'# changed\n')
        (self.dest / 'job-started.sh').chmod(0o700)  # writable: a changed mode is rewritten too
        self.assertEqual(hookcopy.copy(str(self.data), only_changed=True), ['job-started.sh'])
        self.assertEqual(self.mode(self.dest / 'job-started.sh'), 0o500)
        (self.dest / 'job-started.sh').unlink()
        same = self.dest.parent / 'same'
        same.write_bytes(STARTED)
        same.chmod(0o500)
        (self.dest / 'job-started.sh').symlink_to(same)  # the same bytes and mode, through a link
        self.assertEqual(hookcopy.copy(str(self.data), only_changed=True), ['job-started.sh'])
        self.assertFalse((self.dest / 'job-started.sh').is_symlink())
        self.assertEqual(hookcopy.copy(str(self.data)), ['job-started.sh', 'job-completed.sh'])  # copy: always both

    def test_a_leftover_temporary_is_replaced(self):
        self.dest.mkdir(mode=0o700)
        left = self.dest / f'.job-started.sh.{os.getpid()}.new'
        left.write_text('stale')
        hookcopy.copy(str(self.data))
        self.assertFalse(left.exists())
        self.assertEqual((self.dest / 'job-started.sh').read_bytes(), STARTED)

    def test_where_the_runners_find_the_hooks(self):
        self.assertTrue(hookcopy.used(self.h))
        self.assertEqual(hookcopy.hooks_dir(self.h), str(self.dest))
        plain = {'data': '/d'}
        self.assertFalse(hookcopy.used(plain))
        self.assertFalse(hookcopy.used({'data': '/d', 'local_accounts': []}))
        self.assertEqual(hookcopy.hooks_dir(plain), '/d/ci-runners/hooks')

    def test_the_reapers_refresh(self):
        self.assertEqual(hookcopy.refresh({'data': str(self.data)}), '')  # no private /tmp: nothing
        self.assertFalse(self.dest.exists())
        self.assertEqual(hookcopy.refresh(self.h), 'hook copies: rewrote job-started.sh, job-completed.sh')
        self.assertEqual(hookcopy.refresh(self.h), '')
        (self.src / 'job-started.sh').unlink()
        self.assertRegex(hookcopy.refresh(self.h), r'^hook copies: refused \(.*job-started.sh')

    def test_only_in_a_private_tmp(self):
        """A shell from ``sudo -u`` sees the host's /tmp: the copy would be out of the runners'
        sight, in a directory any account could plant."""
        self.assertTrue(hookcopy.private_tmp(PRIVATE))
        self.assertFalse(hookcopy.private_tmp(HOST))
        tmpfs = HOST + '30 22 0:40 / /tmp rw,nosuid,nodev shared:20 - tmpfs tmpfs rw\n'
        self.assertFalse(hookcopy.private_tmp(tmpfs))  # the host's own tmpfs
        self.assertFalse(hookcopy.private_tmp(PRIVATE + '700 600 0:41 / /tmp rw - tmpfs tmpfs rw\n'))  # the last one counts
        self.assertTrue(hookcopy.private_tmp(tmpfs + PRIVATE.splitlines()[1] + '\n'))
        self.assertFalse(hookcopy.private_tmp(''))
        err = io.StringIO()
        with mock.patch.object(hookcopy, '_mountinfo', lambda: HOST), contextlib.redirect_stderr(err):
            self.assertEqual(hookcopy.main(['copy'], self.h), 2)
            self.assertEqual(hookcopy.refresh(self.h), 'hook copies: refused (/tmp is not private here)')
        self.assertIn('/tmp is not private here', err.getvalue())
        self.assertFalse(self.dest.exists())

    def test_the_cli(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(hookcopy.main(['copy', 'x'], self.h), 2)
            self.assertEqual(hookcopy.main(['copy'], {'data': str(self.data)}), 2)
            self.assertFalse(self.dest.exists())
            self.assertEqual(hookcopy.main(['copy'], self.h), 0)
            (self.src / 'job-started.sh').unlink()
            self.assertEqual(hookcopy.main(['copy'], self.h), 1)
        self.assertEqual(out.getvalue(), f'hooks: copied job-started.sh job-completed.sh to {self.dest}\n')
        self.assertIn('keeps its hooks under data', err.getvalue())
        self.assertIn('ci-run hooks copy: ', err.getvalue())

    def test_ci_run_hooks_and_reap(self):
        etc = self.data / 'ci-runners' / 'etc'
        etc.mkdir()
        (etc / 'inventory.toml').write_text(
            f'schema = 1\nthis = "h"\n[hosts.h]\naccount = "a"\nhome = "{self.tmp.name}"\ndata = "{self.data}"\n'
            'slots = 1\nmemory_max = "1G"\ndisk_min_gib = 0\ngpus = []\nmax_temp_c = 80\npools = { part = 1 }\n'
            'local_accounts = ["localuser"]\n'
            f'files = {{ slot_lock = "/tmp/s.lock", slot_queue = "/tmp/s.queue" }}\n')
        from ci_runners import cli
        with mock.patch.dict(os.environ, {'CI_RUNNERS_ETC': str(etc)}), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(['hooks', 'copy']), 0)
            (self.dest / 'job-completed.sh').unlink()
            self.assertEqual(cli.main(['reap']), 0)
        self.assertIn('hook copies: rewrote job-completed.sh\n', out.getvalue())
        self.assertEqual((self.dest / 'job-completed.sh').read_bytes(), COMPLETED)


if __name__ == '__main__':
    unittest.main()
