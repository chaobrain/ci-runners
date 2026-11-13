"""The action archive cache (design decision 20): what a release names, the resolution of
its refs, the archive's checks, the atomic store, the manifest, the atime signal, the
diagnostic logs' counts and the removal. Fixtures only: no network."""
from __future__ import annotations

import contextlib
import gzip
import hashlib
import io
import json
import os
import pathlib
import shutil
import stat
import tarfile
import tempfile
import unittest
from unittest import mock

from ci_runners import actioncache as ac
from ci_runners import stamp

ROOT = pathlib.Path(__file__).resolve().parents[1]
SHA_A = 'a' * 40
SHA_B = 'b' * 40
SHA_C = 'c' * 40
TAG_OBJ = 'd' * 40


def tgz(members, top_dir=True, fmt=tarfile.PAX_FORMAT, pax=True) -> bytes:
    """A gzip tar archive: ``members`` are ``(name, kind, data_or_link)``, kind one of
    ``dir``, ``file``, ``sym``, ``link``, ``chr``, ``fifo``; GitHub's archives carry a pax
    global header (the commit), which is not a member."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz', format=fmt,
                      pax_headers={'comment': SHA_A} if pax else None) as t:
        for name, kind, extra in members:
            i = tarfile.TarInfo(name)
            if kind == 'dir':
                i.type, i.mode = tarfile.DIRTYPE, 0o755
                t.addfile(i)
            elif kind == 'file':
                i.size = len(extra)
                t.addfile(i, io.BytesIO(extra))
            elif kind in ('sym', 'link'):
                i.type, i.linkname = (tarfile.SYMTYPE if kind == 'sym' else tarfile.LNKTYPE), extra
                t.addfile(i)
            else:
                i.type = tarfile.CHRTYPE if kind == 'chr' else tarfile.FIFOTYPE
                t.addfile(i)
    return buf.getvalue()


def good(top='o-r-aaaaaaa', body=b'name: x\n') -> bytes:
    return tgz([(top, 'dir', None), (f'{top}/action.yml', 'file', body), (f'{top}/sub', 'dir', None),
                (f'{top}/sub/link', 'sym', '../action.yml'), (f'{top}/sub/up', 'sym', '..'),
                (f'{top}/hard', 'link', f'{top}/action.yml')])


class Uses(unittest.TestCase):
    def test_actions_are_read_and_the_rest_skipped(self):
        text = ('steps:\n  - uses: actions/checkout@v4\n    with: {}\n'
                '      uses: "chaobrain/ci-runners/actions/part@v1.2.3"  # a comment\n'
                "  - uses: 'actions/upload-artifact@" + SHA_A + "'\n"
                '  - uses: ./actions/local\n  - uses: docker://alpine:3\n'
                '    uses: chaobrain/ci-runners/.github/workflows/part.yml@v1\n'
                '# uses: not/a-step@v1 is a comment\n  run: echo "uses: x/y@z"\n')
        self.assertEqual(ac.uses(text), [('actions/checkout', 'v4'), ('chaobrain/ci-runners', 'v1.2.3'),
                                         ('actions/upload-artifact', SHA_A)])

    def test_another_form_is_refused(self):
        for spec in ('actions/checkout', 'checkout@v4', 'actions/check out@v4', '-x/y@v1', 'a/b@-v1', 'a/b@v1..2',
                     'a/..@v1', 'a/b@'):
            with self.assertRaises(ValueError, msg=spec):
                ac.uses(f'  - uses: {spec}\n', 'f.yml')


class Tree(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tree = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, rel, text):
        p = self.tree / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def test_refs_from_a_fixture_tree(self):
        """Every action's action.yml and the stamped workflows; not this repository's own
        CI (test.yml), whose actions run on hosted runners; once each, sorted."""
        self.put('actions/RELEASE', 'v1.2.3\n')
        self.put('actions/a/action.yml', '  - uses: actions/upload-artifact@v4\n')
        self.put('actions/b/action.yml', '  - uses: actions/download-artifact@v4\n  - uses: actions/upload-artifact@v4\n')
        self.put('actions/lib/ci-run.sh', 'uses: not/an-action@v1\n')
        self.put('.github/workflows/ci.yml', '  - uses: chaobrain/ci-runners/actions/flow@v1.2.3\n'
                 '  - uses: actions/checkout@v4\n  - uses: chaobrain/ci-runners/actions/part@v1.2.3\n')
        self.put('.github/workflows/test.yml', '  - uses: actions/setup-python@v5\n')
        self.assertEqual(ac.sources(str(self.tree)), ['actions/a/action.yml', 'actions/b/action.yml',
                                                      '.github/workflows/ci.yml'])
        self.assertEqual(ac.refs(str(self.tree)), [('actions/checkout', 'v4'), ('actions/download-artifact', 'v4'),
                                                   ('actions/upload-artifact', 'v4'), ('chaobrain/ci-runners', 'v1.2.3')])
        self.assertEqual(ac.release_of(str(self.tree)), 'v1.2.3')
        self.put('actions/RELEASE', 'v1\n')
        with self.assertRaisesRegex(ac.CacheError, 'not hook or vX.Y.Z'):
            ac.release_of(str(self.tree))
        (self.tree / 'actions' / 'RELEASE').unlink()
        with self.assertRaisesRegex(ac.CacheError, 'no actions/RELEASE'):
            ac.release_of(str(self.tree))
        shutil.rmtree(self.tree / 'actions')
        (self.tree / '.github' / 'workflows' / 'ci.yml').write_text('jobs: {}\n')
        with self.assertRaisesRegex(ac.CacheError, 'names no action'):
            ac.refs(str(self.tree))

    def test_refs_of_the_real_tree_follow_its_stamp(self):
        """This repository's archive at the ref its stamp writes (``stamp.refs``), whatever the
        stamp reads now; and again after stamping a copy to a release."""
        texts = [(ROOT / w).read_text() for w in stamp.WORKFLOWS]
        own = {ref for t in texts for _, ref in stamp.refs(t)}
        got = ac.refs(str(ROOT))
        self.assertEqual({ref for repo, ref in got if repo == 'chaobrain/ci-runners'}, own)
        self.assertEqual(ac.release_of(str(ROOT)), (ROOT / 'actions' / 'RELEASE').read_text().strip())
        names = {repo for repo, _ in got}
        self.assertLessEqual({'actions/checkout', 'actions/upload-artifact', 'actions/download-artifact'}, names)
        self.assertNotIn('actions/setup-python', names)  # test.yml's only
        for rel in ['actions/RELEASE', *stamp.WORKFLOWS, *ac.sources(str(ROOT))]:
            (self.tree / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(ROOT / rel, self.tree / rel)
        stamp.stamp(self.tree, 'v9.8.7')
        self.assertEqual({ref for repo, ref in ac.refs(str(self.tree)) if repo == 'chaobrain/ci-runners'}, {'v9.8.7'})
        self.assertEqual(ac.release_of(str(self.tree)), 'v9.8.7')


def remote(table):
    """A fake ``git ls-remote``: the lines of ``table`` (name -> sha) the patterns name,
    and the calls."""
    calls = []

    def ls(url, patterns):
        calls.append((url, patterns))
        return ''.join(f'{sha}\t{name}\n' for name, sha in table.get(url, {}).items() if name in patterns)
    return ls, calls


class Resolve(unittest.TestCase):
    URL = 'https://github.com/o/r'

    def test_the_order(self):
        """A 40-hex ref is itself (no call); the peeled tag before the tag, the tag before the
        branch, as the runner resolves it."""
        ls, calls = remote({})
        self.assertEqual(ac.resolve('o/r', SHA_C, ls), SHA_C)
        self.assertEqual(calls, [])
        both = {'refs/tags/v4': TAG_OBJ, 'refs/tags/v4^{}': SHA_A, 'refs/heads/v4': SHA_B}
        ls, calls = remote({self.URL: both})
        self.assertEqual(ac.resolve('o/r', 'v4', ls), SHA_A)  # an annotated tag: its commit
        self.assertEqual(calls, [(self.URL, ['refs/tags/v4', 'refs/tags/v4^{}', 'refs/heads/v4'])])
        ls, _ = remote({self.URL: {'refs/tags/v4': SHA_C, 'refs/heads/v4': SHA_B}})
        self.assertEqual(ac.resolve('o/r', 'v4', ls), SHA_C)  # a lightweight tag before a branch
        ls, _ = remote({self.URL: {'refs/heads/v4': SHA_B, 'refs/heads/x/refs/tags/v4': SHA_C}})
        self.assertEqual(ac.resolve('o/r', 'v4', ls), SHA_B)
        ls, _ = remote({self.URL: {'refs/heads/main': SHA_B}})
        with self.assertRaisesRegex(ac.CacheError, 'no tag or branch'):
            ac.resolve('o/r', 'v4', ls)
        with self.assertRaisesRegex(ac.CacheError, 'no tag or branch'):
            ac.resolve('o/r', 'v4', lambda u, p: 'not-a-sha\trefs/tags/v4\n')


class Check(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, 'x.tar.gz')

    def tearDown(self):
        self.tmp.cleanup()

    def verdict(self, data, **kw):
        with open(self.path, 'wb') as f:
            f.write(data)
        return ac.check(self.path, **kw)

    def test_a_github_like_archive_passes(self):
        self.assertEqual(self.verdict(good()), 'o-r-aaaaaaa')
        self.assertEqual(self.verdict(tgz([('t/a', 'file', b'x')])), 't')  # its directory implied
        self.assertEqual(self.verdict(tgz([('t', 'dir', None)], fmt=tarfile.GNU_FORMAT, pax=False)), 't')

    def test_refusals(self):
        cases = {
            'more than one top-level': tgz([('t', 'dir', None), ('u', 'dir', None)]),
            'more than one top-level ': tgz([('t/a', 'file', b''), ('u/a', 'file', b'')]),
            'is not a directory': tgz([('t', 'file', b'x')]),
            "'..'": tgz([('t', 'dir', None), ('t/../evil', 'file', b'x')]),
            'absolute': tgz([('/t', 'dir', None)]),
            'empty or "." part': tgz([('t', 'dir', None), ('t//a', 'file', b'')]),
            'empty or "." part ': tgz([('t', 'dir', None), ('t/./a', 'file', b'')]),
            'symbolic link that leaves': tgz([('t', 'dir', None), ('t/l', 'sym', '../outside')]),
            'symbolic link that leaves ': tgz([('t', 'dir', None), ('t/l', 'sym', '/etc/passwd')]),
            'symbolic link that leaves  ': tgz([('t', 'dir', None), ('t/a/l', 'sym', '../../../x')]),
            'hard link that leaves': tgz([('t', 'dir', None), ('t/h', 'link', 'other/file')]),
            'hard link that leaves ': tgz([('t', 'dir', None), ('t/h', 'link', '/etc/passwd')]),
            'device, FIFO': tgz([('t', 'dir', None), ('t/dev', 'chr', None)]),
            'device, FIFO ': tgz([('t', 'dir', None), ('t/fifo', 'fifo', None)]),
            'an empty archive': tgz([], pax=False),
            'not a gzip tar    ': tgz([]),  # a global header alone: tarfile reads no member
            'not a gzip tar': b'plain bytes, not gzip',
            'not a gzip tar ': gzip.compress(b'gzip, but not a tar archive' * 40),
            'not a gzip tar  ': good()[:-30],  # truncated: the runner's tar would fail the job
            'not a gzip tar   ': good() + b'trailing',
        }
        for want, data in cases.items():
            with self.assertRaisesRegex(ac.CacheError, want.strip(), msg=want):
                self.verdict(data)

    def test_the_bounds(self):
        data = good()
        self.assertEqual(self.verdict(data, limit=len(data)), 'o-r-aaaaaaa')
        with self.assertRaisesRegex(ac.CacheError, 'over the bound'):
            self.verdict(data, limit=len(data) - 1)
        bomb = tgz([('t', 'dir', None), ('t/zeros', 'file', bytes(1 << 20))])
        self.assertEqual(self.verdict(bomb), 't')
        with self.assertRaisesRegex(ac.CacheError, 'unpacked'):
            self.verdict(bomb, unpacked=1 << 20)
        os.unlink(self.path)
        os.symlink(os.devnull, self.path)
        with self.assertRaises(OSError):  # never through a link
            ac.check(self.path)
        os.unlink(self.path)
        os.mkfifo(self.path)
        with self.assertRaisesRegex(ac.CacheError, 'not a regular file'):  # at once: no writer is waited for
            ac.check(self.path)

    def test_download_is_bounded(self):
        class Body(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        out = io.BytesIO()
        with mock.patch('urllib.request.urlopen', lambda req, timeout: Body(b'x' * 10)):
            ac.download('https://api.github.com/repos/o/r/tarball/' + SHA_A, out, 10)
            self.assertEqual(out.getvalue(), b'x' * 10)
            with self.assertRaisesRegex(ac.CacheError, 'larger than 9 bytes'):
                ac.download('https://api.github.com/repos/o/r/tarball/' + SHA_A, io.BytesIO(), 9)


class Cache(unittest.TestCase):
    """A fill, status and removal on a fixture tree, with a fake ls-remote and download."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = pathlib.Path(self.tmp.name)
        self.tree, self.cache = t / 'tree', str(t / 'data' / 'ci-runners' / 'cache' / 'actions')
        self.set_tree('v1.2.3', 'v1.2.3')
        self.table = {'https://github.com/actions/checkout': {'refs/tags/v4': TAG_OBJ, 'refs/tags/v4^{}': SHA_A},
                      'https://github.com/chaobrain/ci-runners': {'refs/tags/v1.2.3': SHA_B, 'refs/tags/v1.3.0': SHA_C}}
        self.bodies = {}
        self.fetched = []
        self.clock = 1_800_000_000.0

    def tearDown(self):
        self.tmp.cleanup()

    def set_tree(self, release, ref):
        (self.tree / 'actions' / 'part').mkdir(parents=True, exist_ok=True)
        (self.tree / '.github' / 'workflows').mkdir(parents=True, exist_ok=True)
        (self.tree / 'actions' / 'RELEASE').write_text(release + '\n')
        (self.tree / 'actions' / 'part' / 'action.yml').write_text('  - uses: actions/checkout@v4\n')
        (self.tree / '.github' / 'workflows' / 'ci.yml').write_text(f'  - uses: chaobrain/ci-runners/actions/part@{ref}\n')

    def fetch(self, url, out, limit):
        self.fetched.append(url)
        sha = url.rsplit('/', 1)[1]
        body = self.bodies.get(sha, good(f'top-{sha[:7]}', sha.encode()))
        if isinstance(body, Exception):
            out.write(b'half an archive')
            raise body
        out.write(body)

    def fill(self, **kw):
        ls, _ = remote(self.table)
        lines = []
        rc = ac.fill(self.cache, str(self.tree), ls_remote=ls, fetch=self.fetch, now=lambda: self.clock,
                     out=lines.append, **kw)
        return rc, lines

    def status(self, mountinfo=None, **kw):
        lines = []
        info = mountinfo if mountinfo is not None else '1 0 0:1 / / rw,relatime - ext4 /dev/x rw\n'
        rc = ac.status(self.cache, mountinfo=lambda: info, now=lambda: self.clock, out=lines.append, **kw)
        return rc, lines

    def entry(self, repo, sha):
        return pathlib.Path(ac.entry_dir(self.cache, repo), f'{sha}.tar.gz')

    def manifest(self):
        return json.loads(pathlib.Path(self.cache, ac.MANIFEST).read_text())

    def leftovers(self):
        return [p for p in pathlib.Path(self.cache).rglob('*') if p.name.endswith('.tmp')]

    def test_a_fill_stores_each_entry_and_a_second_keeps_them(self):
        rc, lines = self.fill()
        self.assertEqual(rc, 0)
        self.assertEqual(self.fetched, [f'https://api.github.com/repos/actions/checkout/tarball/{SHA_A}',
                                        f'https://api.github.com/repos/chaobrain/ci-runners/tarball/{SHA_B}'])
        for repo, sha in (('actions/checkout', SHA_A), ('chaobrain/ci-runners', SHA_B)):
            p = self.entry(repo, sha)
            self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o444)
            self.assertEqual(ac.check(str(p)), f'top-{sha[:7]}')
        self.assertEqual(sorted(os.listdir(self.cache)), [ac.LOCK, 'actions_checkout', 'chaobrain_ci-runners', ac.MANIFEST])
        self.assertEqual(stat.S_IMODE(os.stat(self.cache).st_mode), 0o700)
        self.assertEqual(lines[0], f'stored actions/checkout@aaaaaaa (v4), {self.entry("actions/checkout", SHA_A).stat().st_size} bytes')
        self.assertEqual(lines[-1], 'fill for v1.2.3: 2 stored, 0 kept, 0 failed')
        self.assertNotIn(self.tmp.name, '\n'.join(lines))
        self.fetched.clear()
        rc, lines = self.fill()
        self.assertEqual((rc, self.fetched), (0, []))  # intact by the manifest: not downloaded again
        self.assertEqual(lines[-1], 'fill for v1.2.3: 0 stored, 2 kept, 0 failed')
        self.assertTrue(lines[0].startswith('kept actions/checkout@aaaaaaa (v4), '))

    def test_the_manifest(self):
        self.fill()
        m = self.manifest()
        self.assertEqual((m['schema'], m['filled']), (1, '2027-01-15T08:00:00Z'))
        e = m['entries'][0]
        data = self.entry('actions/checkout', SHA_A).read_bytes()
        self.assertEqual(e, {'repo': 'actions/checkout', 'sha': SHA_A, 'refs': ['v4'], 'releases': ['v1.2.3'],
                             'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data),
                             'filled': '2027-01-15T08:00:00Z'})
        self.assertEqual([x['repo'] for x in m['entries']], ['actions/checkout', 'chaobrain/ci-runners'])
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.cache, ac.MANIFEST)).st_mode), 0o600)
        self.set_tree('v1.3.0', 'v1.3.0')  # the next release: checkout's commit is the same
        self.clock += 3600
        self.fill()
        m = self.manifest()
        self.assertEqual(m['filled'], '2027-01-15T09:00:00Z')
        by = {(x['repo'], x['sha']): x for x in m['entries']}
        self.assertEqual(by[('actions/checkout', SHA_A)]['releases'], ['v1.2.3', 'v1.3.0'])
        self.assertEqual(by[('actions/checkout', SHA_A)]['filled'], '2027-01-15T08:00:00Z')  # kept, not refilled
        self.assertEqual(by[('chaobrain/ci-runners', SHA_B)]['releases'], ['v1.2.3'])
        self.assertEqual(by[('chaobrain/ci-runners', SHA_C)]['releases'], ['v1.3.0'])
        self.assertEqual(by[('chaobrain/ci-runners', SHA_C)]['refs'], ['v1.3.0'])
        self.set_tree('hook', 'v1')  # main after v1 moved to v1.3.0: the same entry, one ref more
        self.table['https://github.com/chaobrain/ci-runners']['refs/tags/v1'] = SHA_C
        self.fetched.clear()
        self.fill()
        self.assertEqual(self.fetched, [])
        by = {(x['repo'], x['sha']): x for x in self.manifest()['entries']}
        self.assertEqual((by[('chaobrain/ci-runners', SHA_C)]['refs'], by[('chaobrain/ci-runners', SHA_C)]['releases']),
                         (['v1', 'v1.3.0'], ['hook', 'v1.3.0']))
        with open(os.path.join(self.cache, ac.MANIFEST), 'w') as f:
            f.write('{"schema": 1, "entries": [{"repo": "../..", "sha": "x"}]}')
        with self.assertRaisesRegex(ac.CacheError, 'not of schema 1'):
            ac.read_manifest(self.cache)
        with open(os.path.join(self.cache, ac.MANIFEST), 'w') as f:
            f.write('not json')
        with self.assertRaisesRegex(ac.CacheError, 'not JSON'):
            ac.read_manifest(self.cache)

    def test_an_unreadable_manifest_checks_every_entry_again(self):
        self.fill()
        pathlib.Path(self.cache, ac.MANIFEST).write_text('{"schema": 2}')
        self.fetched.clear()
        rc, lines = self.fill()
        self.assertEqual(rc, 0)
        self.assertEqual(lines[0], 'the manifest is not of schema 1: every entry is checked again')
        self.assertEqual(len(self.fetched), 2)
        self.assertEqual(len(self.manifest()['entries']), 2)

    def test_a_refused_or_failed_download_leaves_no_file_and_the_others_stored(self):
        self.bodies[SHA_A] = tgz([('t', 'dir', None), ('u', 'dir', None)])
        rc, lines = self.fill()
        self.assertEqual(rc, 1)
        self.assertIn('failed actions/checkout@aaaaaaa: more than one top-level entry', lines)
        self.assertEqual(lines[-1], 'fill for v1.2.3: 1 stored, 0 kept, 1 failed')
        self.assertFalse(self.entry('actions/checkout', SHA_A).exists())
        self.assertTrue(self.entry('chaobrain/ci-runners', SHA_B).exists())
        self.assertEqual(self.leftovers(), [])
        self.assertEqual([e['repo'] for e in self.manifest()['entries']], ['chaobrain/ci-runners'])
        self.bodies[SHA_A] = OSError('connection reset')
        rc, lines = self.fill()
        self.assertEqual(rc, 1)
        self.assertIn('failed actions/checkout@aaaaaaa: connection reset', lines)
        self.assertFalse(self.entry('actions/checkout', SHA_A).exists())
        self.assertEqual(self.leftovers(), [])
        del self.table['https://github.com/actions/checkout']
        rc, lines = self.fill()
        self.assertEqual(rc, 1)
        self.assertIn('failed actions/checkout@v4: actions/checkout@v4: no tag or branch of that name', lines)

    def test_a_changed_entry_is_removed_before_it_is_stored_again(self):
        self.fill()
        p = self.entry('actions/checkout', SHA_A)
        os.chmod(p, 0o644)
        p.write_bytes(b'planted')
        self.bodies[SHA_A] = OSError('egress down')
        rc, _ = self.fill()
        self.assertEqual(rc, 1)
        self.assertFalse(p.exists())  # a miss for the runner, never the changed file
        del self.bodies[SHA_A]
        self.fetched.clear()
        self.assertEqual(self.fill()[0], 0)
        self.assertEqual(self.fetched, [f'https://api.github.com/repos/actions/checkout/tarball/{SHA_A}'])
        self.assertEqual(ac.check(str(p)), 'top-aaaaaaa')

    def test_one_fill_or_remove_at_a_time(self):
        os.makedirs(self.cache)
        with ac.locked(self.cache):
            with self.assertRaisesRegex(ac.CacheError, 'another fill or remove'):
                self.fill()
            with self.assertRaisesRegex(ac.CacheError, 'another fill or remove'):
                ac.remove(self.cache, everything=True, out=lambda s: None)
        self.assertEqual(self.fill()[0], 0)

    def test_a_cache_directory_that_is_a_link_is_refused(self):
        os.makedirs(os.path.dirname(self.cache))
        os.makedirs(self.cache + '-elsewhere')
        os.symlink(self.cache + '-elsewhere', self.cache)
        with self.assertRaisesRegex(ac.CacheError, 'a link or not a directory'):
            self.fill()
        os.unlink(self.cache)
        os.makedirs(self.cache)
        os.symlink(self.cache + '-elsewhere', os.path.join(self.cache, 'actions_checkout'))
        rc, lines = self.fill()
        self.assertEqual(rc, 1)
        self.assertIn('failed actions/checkout@aaaaaaa: a cache directory is a link or not a directory', lines)
        self.assertEqual(os.listdir(self.cache + '-elsewhere'), [])

    def test_the_fill_resets_the_atime_and_status_reads_it(self):
        self.fill()
        a, b = self.entry('actions/checkout', SHA_A), self.entry('chaobrain/ci-runners', SHA_B)
        self.assertEqual((a.stat().st_atime_ns, b.stat().st_atime_ns), (0, 0))
        self.assertGreater(a.stat().st_mtime_ns, 0)
        rc, lines = self.status()
        self.assertEqual(rc, 0)
        self.assertEqual(lines[0], 'action archive cache: 2 entries, last fill 2027-01-15T08:00:00Z')
        self.assertIn('actions/checkout@aaaaaaa (v4; v1.2.3): ok; not used since the fill', lines)
        self.assertEqual(a.stat().st_atime_ns, 0)  # status's own reads leave it (O_NOATIME)
        os.utime(a, ns=(a.stat().st_mtime_ns + 1, a.stat().st_mtime_ns))  # what the runner's copy does
        rc, lines = self.status()
        self.assertIn('actions/checkout@aaaaaaa (v4; v1.2.3): ok; used since the fill', lines)
        self.assertIn('chaobrain/ci-runners@bbbbbbb (v1.2.3; v1.2.3): ok; not used since the fill', lines)
        self.assertEqual(lines[-1], 'status: ok')
        self.assertNotIn(self.tmp.name, '\n'.join(lines))
        self.fill()  # a kept entry too: used since this fill, not before
        self.assertEqual(a.stat().st_atime_ns, 0)
        self.assertFalse(ac.used(a.stat()))

    def test_a_noatime_mount_says_the_signal_is_unavailable(self):
        self.fill()
        real = os.path.realpath(self.cache)
        top = real.split('/')[1]
        info = ('1 0 0:1 / / rw,relatime - ext4 /dev/x rw\n'
                f'2 1 0:2 / /{top} rw,noatime shared:1 - ext4 /dev/y rw\n')
        rc, lines = self.status(info)
        self.assertEqual(rc, 0)
        self.assertIn('atime: the cache\'s file system is mounted noatime: "used since the fill" is not available', lines)
        self.assertIn('actions/checkout@aaaaaaa (v4; v1.2.3): ok', lines)
        rc, lines = self.status('')
        self.assertIn('atime: its mount is unknown: "used since the fill" is not available', lines)

    def test_mount_options(self):
        info = ('1 0 0:1 / / rw,relatime - ext4 /dev/x rw\n'
                '2 1 0:2 / /data rw,noatime - ext4 /dev/y rw\n'
                '3 1 0:3 / /data2 rw,strictatime - ext4 /dev/z rw\n'
                '4 2 0:4 / /data/with\\040space rw,relatime - ext4 /dev/w rw\n'
                '5 1 0:5 / /data rw,nodiratime - ext4 /dev/v rw\n'
                'short line\n')
        self.assertEqual(ac.mount_options('/data/a/b', info), {'rw', 'nodiratime'})  # the last at /data
        self.assertEqual(ac.mount_options('/data2/a', info), {'rw', 'strictatime'})
        self.assertEqual(ac.mount_options('/datax/a', info), {'rw', 'relatime'})
        self.assertEqual(ac.mount_options('/data/with space/c', info), {'rw', 'relatime'})
        self.assertEqual(ac.mount_options('/data', info), {'rw', 'nodiratime'})
        self.assertIsNone(ac.mount_options('/x', ''))

    def test_status_reports_each_problem(self):
        self.set_tree('v1.2.3', 'v1.2.3')
        self.table['https://github.com/actions/upload-artifact'] = {'refs/tags/v4': SHA_C}
        (self.tree / 'actions' / 'up').mkdir()
        (self.tree / 'actions' / 'up' / 'action.yml').write_text('  - uses: actions/upload-artifact@v4\n')
        self.fill()
        self.entry('actions/checkout', SHA_A).unlink()
        p = self.entry('chaobrain/ci-runners', SHA_B)
        os.chmod(p, 0o644)
        p.write_bytes(b'changed')
        bad = tgz([('t', 'dir', None), ('u', 'dir', None)])  # held as intact, but no longer one top directory
        q = self.entry('actions/upload-artifact', SHA_C)
        os.chmod(q, 0o644)
        q.write_bytes(bad)
        m = self.manifest()
        for e in m['entries']:
            if e['repo'] == 'actions/upload-artifact':
                e.update(sha256=hashlib.sha256(bad).hexdigest(), bytes=len(bad))
        ac.write_manifest(self.cache, m)
        stray = pathlib.Path(ac.entry_dir(self.cache, 'other/repo'))
        stray.mkdir()
        (stray / f'{SHA_A}.tar.gz').write_bytes(good())
        rc, lines = self.status()
        self.assertEqual(rc, 1)
        self.assertIn('actions/checkout@aaaaaaa (v4; v1.2.3): missing', lines)
        self.assertIn('chaobrain/ci-runners@bbbbbbb (v1.2.3; v1.2.3): changed; not used since the fill', lines)
        self.assertTrue(any(x.startswith('actions/upload-artifact@ccccccc (v4; v1.2.3): invalid (more than one top')
                            for x in lines), lines)
        self.assertIn('1 archive(s) the manifest does not hold: remove --all, then fill', lines)
        self.assertEqual(lines[-1], 'status: 4 problem(s)')
        self.assertNotIn(self.tmp.name, '\n'.join(lines))
        shutil.rmtree(self.cache)
        rc, lines = self.status()
        self.assertEqual((rc, lines), (1, ['action archive cache: none on this account']))

    def test_the_diagnostic_logs_count_the_hits(self):
        self.fill()
        runners = pathlib.Path(self.tmp.name) / 'runners'
        for name in ('r1', 'r2'):
            (runners / name / '_diag').mkdir(parents=True)
        hit = (f"[2027-01-15 08:10:00Z INFO ActionManager] Found action archive "
               f"'{self.entry('actions/checkout', SHA_A)}' in cache directory '{self.cache}'\n")
        (runners / 'r1' / '_diag' / 'Worker_20270115-081000-utc.log').write_text('other\n' + hit + hit)
        (runners / 'r2' / '_diag' / 'Worker_20270115-081500-utc.log').write_text(hit)
        old = runners / 'r2' / '_diag' / 'Worker_20261201-000000-utc.log'
        old.write_text(hit * 5)
        os.utime(old, (self.clock - 8 * 86400, self.clock - 8 * 86400))
        (runners / 'r2' / '_diag' / 'Runner_20270115-081500-utc.log').write_text(hit * 7)  # the listener's: not a job's
        for p in runners.rglob('*_2027*'):  # recent, the listener's too
            os.utime(p, (self.clock - 60, self.clock - 60))
        self.assertEqual(ac.diag_counts(str(runners), 7, self.clock), {('actions_checkout', SHA_A): 3})
        self.assertEqual(ac.diag_counts(str(runners), 9, self.clock), {('actions_checkout', SHA_A): 8})
        rc, lines = self.status(diag=str(runners), days=7)
        self.assertEqual(rc, 0)
        self.assertIn('actions/checkout@aaaaaaa (v4; v1.2.3): ok; not used since the fill; 3 hits in the '
                      'diagnostic logs (7 d)', lines)
        self.assertIn('chaobrain/ci-runners@bbbbbbb (v1.2.3; v1.2.3): ok; not used since the fill; 0 hits in the '
                      'diagnostic logs (7 d)', lines)

    def test_remove_prunes_by_release_and_all_empties(self):
        self.fill()
        self.set_tree('v1.3.0', 'v1.3.0')
        self.fill()
        lines = []
        self.assertEqual(ac.remove(self.cache, ['v1.3.0'], out=lines.append), 0)
        self.assertEqual(lines, ['removed chaobrain/ci-runners@bbbbbbb (v1.2.3)', 'kept 2 entries'])
        self.assertFalse(self.entry('chaobrain/ci-runners', SHA_B).exists())
        self.assertTrue(self.entry('chaobrain/ci-runners', SHA_C).exists())
        self.assertTrue(self.entry('actions/checkout', SHA_A).exists())
        by = {(x['repo'], x['sha']): x['releases'] for x in self.manifest()['entries']}
        self.assertEqual(by, {('actions/checkout', SHA_A): ['v1.3.0'], ('chaobrain/ci-runners', SHA_C): ['v1.3.0']})
        self.assertEqual(ac.remove(self.cache, ['v9.9.9'], out=lines.append), 0)
        self.assertEqual(self.manifest()['entries'], [])
        self.assertEqual(sorted(os.listdir(self.cache)), [ac.LOCK, ac.MANIFEST])  # the emptied directories too
        self.fill()
        pathlib.Path(self.cache, 'stray').write_text('x')
        lines = []
        self.assertEqual(ac.remove(self.cache, everything=True, out=lines.append), 0)
        self.assertEqual(lines, ['removed 2 archive(s) and the manifest'])
        self.assertEqual(os.listdir(self.cache), [ac.LOCK])  # the directory stays: the runners download
        rc, lines = self.status()
        self.assertEqual((rc, lines[0]), (0, 'action archive cache: 0 entries, last fill never'))
        shutil.rmtree(self.cache)
        self.assertEqual(ac.remove(self.cache, everything=True, out=lines.append), 0)
        self.assertFalse(os.path.exists(self.cache))


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = pathlib.Path(self.tmp.name)
        self.h = {'data': str(self.data), 'actions_cache': True}
        (self.data / 'ci-runners' / 'releases' / 'v1.2.3').mkdir(parents=True)
        os.symlink('v1.2.3', self.data / 'ci-runners' / 'releases' / 'hook')

    def tearDown(self):
        self.tmp.cleanup()

    def run_(self, argv, h=None):
        calls = []
        err = io.StringIO()
        with mock.patch.object(ac, 'fill', lambda cache, tree: calls.append(('fill', cache, tree)) or 0), \
                mock.patch.object(ac, 'status', lambda cache, diag, days: calls.append(('status', cache, diag, days)) or 0), \
                mock.patch.object(ac, 'remove', lambda cache, keep, everything: calls.append(('remove', cache, keep, everything)) or 0), \
                contextlib.redirect_stderr(err):
            rc = ac.main(argv, h or self.h)
        return rc, calls, err.getvalue()

    def test_the_commands(self):
        cache = str(self.data / 'ci-runners' / 'cache' / 'actions')
        rel = str(self.data / 'ci-runners' / 'releases' / 'v1.2.3')
        self.assertEqual(self.run_(['fill']), (0, [('fill', cache, rel)], ''))  # releases/hook, by its real path
        self.assertEqual(self.run_(['fill', '--release', 'v1.2.3'])[1], [('fill', cache, rel)])
        self.assertEqual(self.run_(['fill', '--tree', '/t'])[1], [('fill', cache, '/t')])
        for bad in ('../x', 'hookx', 'v1.2.3/../../x', 'main-0123'):
            rc, calls, err = self.run_(['fill', '--release', bad])
            self.assertEqual((rc, calls), (2, []), bad)
            self.assertIn('is not hook, vX.Y.Z or main-<sha12>', err)
        self.assertEqual(self.run_(['fill', '--release', 'main-0123456789ab'])[1],
                         [('fill', cache, str(self.data / 'ci-runners' / 'releases' / 'main-0123456789ab'))])
        self.assertEqual(self.run_(['status', '--diag', '/r', '--days', '2'])[1], [('status', cache, '/r', 2.0)])
        self.assertEqual(self.run_(['remove', '--keep', 'v1.2.3', 'v1.3.0'])[1],
                         [('remove', cache, ['v1.2.3', 'v1.3.0'], False)])
        self.assertEqual(self.run_(['remove', '--all'])[1], [('remove', cache, None, True)])
        for bad in ('v1', 'hookx', 'main-0123456789ab'):
            rc, calls, err = self.run_(['remove', '--keep', 'v1.2.3', bad])
            self.assertEqual((rc, calls), (2, []), bad)
            self.assertIn('--keep takes releases', err)
        with contextlib.redirect_stderr(io.StringIO()):
            for bad in ([], ['remove'], ['remove', '--all', '--keep', 'v1.2.3'], ['fill', '--tree', 'a', '--release', 'hook']):
                with self.assertRaises(SystemExit, msg=bad):
                    ac.main(bad, self.h)
        rc, calls, err = self.run_(['status'], {'data': str(self.data)})
        self.assertEqual(rc, 0)
        self.assertIn('does not set actions_cache', err)
        self.assertEqual(self.run_(['remove', '--all'], {'data': str(self.data)})[2], '')

    def test_ci_run_dispatches_with_the_inventory_s_data(self):
        etc = self.data / 'ci-runners' / 'etc'
        etc.mkdir()
        (etc / 'inventory.toml').write_text(
            f'schema = 1\nthis = "h"\n[hosts.h]\naccount = "a"\nhome = "{self.tmp.name}"\ndata = "{self.data}"\n'
            'slots = 1\nmemory_max = "1G"\ndisk_min_gib = 0\ngpus = []\nmax_temp_c = 80\npools = { part = 1 }\n'
            'actions_cache = true\nfiles = { slot_lock = "/tmp/s.lock", slot_queue = "/tmp/s.queue" }\n')
        from ci_runners import cli
        out = io.StringIO()
        with mock.patch.dict(os.environ, {'CI_RUNNERS_ETC': str(etc)}), contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(['actions-cache', 'remove', '--all']), 0)
            self.assertEqual(cli.main(['actions-cache', 'status']), 1)
        self.assertEqual(out.getvalue(), 'action archive cache: none on this account\n' * 2)


if __name__ == '__main__':
    unittest.main()
