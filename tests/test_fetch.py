"""A part's plan files through the API (#245): ``ci-run flow fetch`` and what it refuses on the
way down, as the plan job refuses it on the way up (#235)."""
from __future__ import annotations

import contextlib
import io
import os
import pathlib
import stat
import tempfile
import unittest
import zipfile
from unittest import mock

from ci_runners import client, flow, github
from tests.test_flow import REPO, Env


def zipped(entries, *, method=zipfile.ZIP_DEFLATED) -> bytes:
    """``entries``: ``name -> text`` (a file), ``name/ -> None`` (a directory), or
    ``name -> (text, mode)`` with the unix mode the entry declares."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', method) as z:
        for name, v in entries.items():
            text, mode = v if isinstance(v, tuple) else (v, None)
            info = zipfile.ZipInfo(name)
            if mode is not None:
                info.external_attr = mode << 16
            elif name.endswith('/'):
                info.external_attr = (stat.S_IFDIR | 0o755) << 16
            info.compress_type = method
            z.writestr(info, '' if text is None else text)
    return buf.getvalue()


class Unpack(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dest = os.path.join(self.tmp.name, 'files')

    def tearDown(self):
        self.tmp.cleanup()

    def unpack(self, entries):
        return flow.unpack_files(entries if isinstance(entries, bytes) else zipped(entries), self.dest)

    def test_files_and_directories_hidden_ones_too(self):
        n = self.unpack({'keys.json': '{}', '.hidden': 'h', 'sub/': None, 'sub/.dir/a.txt': 'a',
                         'plain': ('p', stat.S_IFREG | 0o600), 'nomode': ('n', 0)})
        self.assertEqual(n, 5)
        root = pathlib.Path(self.dest)
        self.assertEqual(sorted(str(p.relative_to(root)) for p in root.rglob('*')),
                         ['.hidden', 'keys.json', 'nomode', 'plain', 'sub', 'sub/.dir', 'sub/.dir/a.txt'])
        self.assertEqual((root / 'sub/.dir/a.txt').read_text(), 'a')
        self.assertFalse(any(p.is_symlink() for p in root.rglob('*')))

    def test_a_name_outside_the_files_is_refused(self):
        for name in ('/etc/x', '../x', 'a/../../x', 'a\\..\\x', 'a//b', './a', 'a/./b', 'C:x', ''):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'not a path below the files'):
                self.unpack({name: 'x'})
            self.assertFalse(os.path.exists(self.dest), name)  # nothing made before the checks

    def test_git_a_link_and_what_is_neither_file_nor_directory_are_refused(self):
        cases = {'.git/config': 'git directory', 'sub/.git': 'git directory', '.git/': 'git directory',
                 'a"b': 'character the upload refuses', 'x/a:b': 'character the upload refuses',
                 'a\nb': 'character the upload refuses'}
        for name, why in cases.items():
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, why):
                self.unpack({name: None if name.endswith('/') else 'x'})
        for mode, why in ((stat.S_IFLNK | 0o777, 'is a link'), (stat.S_IFIFO | 0o644, 'not a file or a directory'),
                          (stat.S_IFCHR | 0o644, 'not a file or a directory'), (stat.S_IFDIR | 0o755, 'not a file')):
            with self.subTest(mode=oct(mode)), self.assertRaisesRegex(ValueError, why):
                self.unpack({'entry': ('/etc/passwd', mode)})  # a directory's mode on a file entry too
        self.assertFalse(os.path.exists(self.dest))

    def test_names_given_twice_or_as_file_and_directory_are_refused(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as z, contextlib.suppress(UserWarning), \
                mock.patch('warnings.warn'):
            z.writestr('a', '1')
            z.writestr('a', '2')
        with self.assertRaisesRegex(ValueError, 'named twice'):
            self.unpack(buf.getvalue())
        with self.assertRaisesRegex(ValueError, 'both a file and a directory'):
            self.unpack({'a': 'x', 'a/b': 'y'})
        self.assertFalse(os.path.exists(self.dest))

    def test_the_bounds(self):
        with mock.patch.object(flow, 'MAX_FILES', 3):
            self.assertEqual(self.unpack({'a': '1', 'b': '2', 'c': '3'}), 3)
            os.rename(self.dest, self.dest + '.1')
            with self.assertRaisesRegex(ValueError, 'more than 3 entries'):
                self.unpack({'a': '1', 'b': '2', 'c': '3', 'd': '4'})
            with self.assertRaisesRegex(ValueError, 'more than 3 entries'):  # the directories count, as uploaded
                self.unpack({'d/e/a': '1', 'b': '2'})
        with mock.patch.object(flow, 'MAX_FILES_BYTES', 10):
            self.assertEqual(self.unpack({'a': 'x' * 10}), 1)
            os.rename(self.dest, self.dest + '.2')
            with self.assertRaisesRegex(ValueError, 'MiB'):
                self.unpack({'a': 'x' * 6, 'b': 'y' * 5})
        self.assertFalse(os.path.exists(self.dest))

    def test_a_zip_that_cannot_be_read_leaves_nothing(self):
        with self.assertRaisesRegex(ValueError, 'cannot be read'):
            self.unpack(b'not a zip')
        bad = bytearray(zipped({'a': 'x' * 100, 'b': 'y'}, method=zipfile.ZIP_STORED))
        bad[bad.index(b'y' * 1, bad.index(b'x' * 100) + 100)] ^= 1  # b's data: its CRC fails after a is written
        with self.assertRaisesRegex(ValueError, 'cannot be unpacked'):
            self.unpack(bytes(bad))
        self.assertFalse(os.path.exists(self.dest))  # a's file went with it

    def test_a_link_planted_while_it_unpacks_is_not_written_through(self):
        outside = pathlib.Path(self.tmp.name, 'outside')
        outside.write_text('kept')
        makedirs = os.makedirs

        def planting(path, *a, **kw):  # another process puts a link where the next file goes
            makedirs(path, *a, **kw)
            target = os.path.join(path, 'a')
            if not os.path.lexists(target):
                os.symlink(outside, target)
        with mock.patch('os.makedirs', planting), self.assertRaisesRegex(ValueError, 'cannot be unpacked'):
            self.unpack({'sub/a': 'written'})
        self.assertEqual(outside.read_text(), 'kept')
        self.assertFalse(os.path.exists(self.dest))

    def test_nothing_is_merged_into_a_directory_that_is_there(self):
        os.mkdir(self.dest)
        with self.assertRaises(FileExistsError):
            self.unpack({'a': '1'})
        self.assertEqual(os.listdir(self.dest), [])
        os.rmdir(self.dest)
        os.symlink(self.tmp.name, self.dest)
        with self.assertRaises(FileExistsError):
            self.unpack({'a': '1'})
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, 'a')))


class Fetch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.e = Env(self.tmp.name)
        self.dest = os.path.join(self.tmp.name, 'files')
        self.listing = [{'id': 5, 'name': 'ci-plan-files', 'created_at': '2026-11-13T10:00:00Z'},
                        {'id': 3, 'name': 'ci-plan-files', 'created_at': '2026-11-13T11:00:00Z'},  # newer, lower id
                        {'id': 9, 'name': 'ci-plan-files', 'created_at': '2026-11-13T12:00:00Z', 'expired': True},
                        {'id': 7, 'name': 'ci-part-cpu-1', 'created_at': '2026-11-13T12:00:00Z'}]
        self.zips = {3: zipped({'keys.json': 'new'}), 5: zipped({'keys.json': 'old'})}
        self.calls, self.fetched = [], []

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, method, url, token, body=None):
        self.calls.append((method, url, token))
        if isinstance(self.listing, Exception):
            raise self.listing
        return {'artifacts': self.listing}

    def fetch(self, url, token, limit):
        self.fetched.append((url, token, limit))
        got = self.zips[int(url.split('/')[-2])]
        if isinstance(got, Exception):
            raise got
        return got

    def run_fetch(self, **kw):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = flow.main(['fetch', '--dest', self.dest], '/nonexistent', env=self.e.env, **kw)
        return code, err.getvalue()

    def test_the_newest_files_through_the_api(self):
        code, err = self.run_fetch(call=self.call, fetch=self.fetch)
        self.assertEqual(code, 0, err)
        self.assertEqual(pathlib.Path(self.dest, 'keys.json').read_text(), 'new')
        self.assertEqual(self.e.outputs(), {'fallback': 'false'})
        self.assertEqual(self.calls, [('GET', f'https://api.github.com/repos/{REPO}/actions/runs/77/artifacts'
                                             '?per_page=100&page=1', 't')])
        self.assertEqual(self.fetched, [(f'https://api.github.com/repos/{REPO}/actions/artifacts/3/zip', 't',
                                         flow.MAX_FILES_ZIP)])
        self.assertIn('1 file(s) of the plan', err)

    def test_where_the_api_cannot_be_read_download_artifact_does(self):
        for listing, hint in ((github.ApiError('GET x: HTTP 403'), True), (github.ApiError('GET x: HTTP 502'), False),
                              ([], False), (OSError('reset'), False)):
            with self.subTest(listing=listing):
                if os.path.exists(self.e.out):
                    os.unlink(self.e.out)
                self.listing = listing
                code, err = self.run_fetch(call=self.call, fetch=self.fetch)
                self.assertEqual(code, 0, err)
                self.assertEqual(self.e.outputs(), {'fallback': 'true'})
                self.assertIn('::notice::', err)
                self.assertEqual('give the job actions: read' in err, hint)
                self.assertFalse(os.path.exists(self.dest))
        self.listing = [{'id': 3, 'name': 'ci-plan-files', 'created_at': '2026-11-13T11:00:00Z'}]
        self.zips[3] = github.ApiError('GET zip: HTTP 410')
        os.unlink(self.e.out)
        self.assertEqual(self.run_fetch(call=self.call, fetch=self.fetch)[0], 0)
        self.assertEqual(self.e.outputs(), {'fallback': 'true'})

    def test_what_the_zip_holds_is_refused_never_a_reason_to_fall_back(self):
        for bad in (zipped({'../x': 'x'}), zipped({'.git/config': 'x'}), client.TooLarge('larger than 66 MiB')):
            with self.subTest(bad=bad if isinstance(bad, Exception) else 'zip'):
                if os.path.exists(self.e.out):
                    os.unlink(self.e.out)
                self.zips[3] = bad
                code, err = self.run_fetch(call=self.call, fetch=self.fetch)
                self.assertEqual(code, 2)
                self.assertEqual(self.e.outputs(), {})  # no fallback: the step fails
                self.assertFalse(os.path.exists(self.dest))

    def test_one_kept_client_serves_the_listing_and_the_zip(self):
        made = []
        test = self

        class Fake:
            def __init__(self, env):
                made.append(self)
                self.closed = False
                test.assertIs(env, test.e.env)

            def api(self, *a):
                return test.call(*a)

            def artifact(self, url, token, limit):
                return test.fetch(url, token, limit)

            def close(self):
                self.closed = True
        with mock.patch.object(client, 'Client', Fake):
            self.assertEqual(self.run_fetch()[0], 0)
        self.assertEqual(len(made), 1)
        self.assertTrue(made[0].closed)
        self.assertEqual((len(self.calls), len(self.fetched)), (1, 1))


if __name__ == '__main__':
    unittest.main()
