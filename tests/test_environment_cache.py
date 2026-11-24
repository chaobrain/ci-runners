import base64
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

from ci_runners import environment_cache as cache


SHA, OTHER = '1' * 40, '2' * 40
REPO = 'example/model'
BASE = "schema: 1\npython: '3.12'\n"


def source(spec=BASE, files=None, upstream=SHA):
    responses = {cache.MANIFEST: spec, **(files or {})}
    def api(_method, path):
        if path.startswith(f'/repos/{REPO}/contents/'):
            stem, revision = path.split('?ref=')
            assert revision == SHA
            raw = responses[stem.split('/contents/')[1]].encode()
            return dict(type='file', encoding='base64', content=base64.b64encode(raw).decode())
        assert path == '/repos/example/up/commits/work%2Fbranch', path
        return {'sha': upstream}
    return cache.resolve(api, REPO, SHA)


def fake(argv, **kw):
    if '--prefix' in argv:
        path = Path(argv[argv.index('--prefix') + 1])
        (path / 'bin').mkdir(parents=True)
        (path / 'bin/python').symlink_to(sys.executable)
    return SimpleNamespace(returncode=0)


class EnvironmentCacheTests(unittest.TestCase):
    def test_fixed_host_index_only_for_pip_steps(self):
        recipe = source(BASE + f"install: [pytest]\nupstream: [{{repository: example/up, ref: '{SHA}'}}]\n")
        url = 'http://packages.internal:3141/root/stable/+simple/'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'pip-index-url').write_text(url)
            with self.assertRaisesRegex(ValueError, 'missing host pip-reader'):
                cache.ensure(root, recipe, run=lambda *a, **kw: self.fail())
            auth = root / 'pip-reader.netrc'
            auth.touch(mode=0o600)
            pip_calls = []

            def build(argv, **kw):
                env = kw['env']
                if argv[1:4] == ['-m', 'pip', 'install']:
                    self.assertEqual(env['PIP_INDEX_URL'], url)
                    self.assertEqual(env['PIP_TRUSTED_HOST'], 'packages.internal:3141')
                    self.assertEqual(env['NETRC'], str(auth))
                    self.assertEqual(env['no_proxy'], 'localhost,packages.internal')
                    self.assertNotIn('PIP_EXTRA_INDEX_URL', env)
                    pip_calls.append(argv)
                else:
                    self.assertNotIn('NETRC', env)
                    self.assertNotIn('PIP_INDEX_URL', env)
                return fake(argv, **kw)

            cache.ensure(root, recipe, run=build, build_env={'no_proxy': 'localhost', 'PIP_EXTRA_INDEX_URL': 'bad'})
            self.assertEqual(len(pip_calls), 2)
            for line in ('--extra-index-url https://other/simple', '--only-binary=:all: -i other',
                         'pkg @ https://other/pkg.whl'):
                override = source(BASE + 'requirements: [r.txt]\n', {'r.txt': line})
                with self.subTest(line=line), self.assertRaisesRegex(ValueError, 'host pip index'):
                    cache.ensure(root, override, run=lambda *a, **kw: self.fail())

    def test_pinned_file_closure_and_changed_requirements_or_upstream_invalidate(self):
        doc = BASE + ('requirements: [dev.txt]\nupstream:\n  - repository: example/up\n'
                      '    ref: origin/work/branch\nvariant:\n  cpu:\n    install: [numpy]\n')
        files = {'dev.txt': '-rreq/main.txt\n', 'req/main.txt': '--constraint=lock.txt # pinned\npkg\n',
                 'req/lock.txt': 'pkg==1\n'}
        recipe = source(doc, files)
        self.assertEqual(recipe['files'], files)
        self.assertEqual(recipe['commits'], {'up': SHA})
        base = cache.key(recipe)
        self.assertNotEqual(base, cache.key(recipe, 'cpu'))
        self.assertNotEqual(base, cache.key(recipe, 'gpu'))
        self.assertEqual(base, cache.key(dict(recipe, revision=OTHER)))
        self.assertNotEqual(base, cache.key(dict(recipe, repository='example/other')))
        self.assertNotEqual(base, cache.key(source(doc, {**files, 'req/lock.txt': 'pkg==2\n'})))
        self.assertNotEqual(base, cache.key(source(doc, files, OTHER)))

    def test_rejects_malformed_yaml_or_forged_closure(self):
        bad = [BASE + 'schema: 1\n', BASE + 'vars: &a [a]\ninstall: *a\n',
               BASE + 'install: !!python/name:os.system\n', BASE + 'variables: {GIT_CONFIG_GLOBAL: bad}\n',
               BASE + 'conda: {channels: [file:///somewhere]}\n', 'schema: true\npython: "3.12"']
        for doc in bad:
            with self.subTest(doc=doc), self.assertRaises(ValueError):
                source(doc)
        recipe = source(BASE + 'requirements: [req.txt]\n', {'req.txt': 'pkg\n'})
        for invalid in ({}, {'req.txt': 'pkg\n', 'other': 'extra'}, {'req.txt': '-r /outside\n'},
                        {'req.txt': '-rreq.txt'}, {'req.txt': 'x' * cache.MAX_RECIPE}):
            with self.subTest(invalid=list(invalid)), self.assertRaises(ValueError):
                cache.validate({**recipe, 'files': invalid})

    def test_direct_commit_cannot_be_changed_during_transport(self):
        spec = BASE + f"upstream: [{{repository: example/up, ref: '{SHA}'}}]\n"
        recipe = source(spec)
        self.assertEqual(recipe['commits']['up'], SHA)
        recipe['commits']['up'] = OTHER
        with self.assertRaisesRegex(ValueError, 'explicit upstream'):
            cache.validate(recipe)

    def test_reads_bounded_utf8_blob_at_exact_sha(self):
        bad_responses = [
            dict(type='dir', encoding='base64', content='eA=='),
            dict(type='file', encoding='base64', content='%'),
            dict(type='file', encoding='base64', content='/w=='),
            dict(type='file', encoding='base64', content='x' * (2 * cache.MAX_MANIFEST + 1)),
        ]
        for bad in bad_responses:
            with self.subTest(size=len(bad['content'])), self.assertRaises(ValueError):
                cache.resolve(lambda *_: bad, REPO, SHA)
        valid_document = base64.b64encode(BASE.encode()).decode()
        rejected = dict(type='file', encoding='base64', content=valid_document + '%')
        with self.assertRaisesRegex(ValueError, 'invalid base64'):
            cache.resolve(lambda *_: rejected, REPO, SHA)
        with self.assertRaisesRegex(ValueError, 'invalid trusted'):
            cache.resolve(lambda *_: self.fail(), REPO, 'main')

    def test_build_deduplication_publication_and_warm_lookup(self):
        recipe = source()
        started, resume = threading.Event(), threading.Event()
        calls = []
        def build(argv, **kwargs):
            calls.append(argv)
            answer = fake(argv, **kwargs)
            started.set()
            self.assertTrue(resume.wait(5))
            return answer
        with tempfile.TemporaryDirectory() as tmp, ThreadPoolExecutor(2) as pool:
            root = Path(tmp) / 'clean'
            self.assertIsNone(cache.lookup(root, recipe))
            self.assertFalse(root.exists())
            first = pool.submit(cache.ensure, root, recipe, run=build)
            self.assertTrue(started.wait(5))
            self.assertIsNone(cache.lookup(root, recipe))
            second = pool.submit(cache.ensure, root, recipe, run=build)
            resume.set()
            prefix = first.result(timeout=5)
            self.assertEqual(second.result(timeout=5), prefix)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][calls[0].index('--prefix') + 1], prefix)
            self.assertEqual(cache.lookup(root, recipe), prefix)
            self.assertEqual(cache.ensure(root, recipe, run=lambda *_a, **_k: self.fail()), prefix)
            self.assertIsNone(cache.lookup(root, recipe, variant='cpu'))

    def test_build_failure_cleans_attempt_and_retries(self):
        recipe = source(BASE + 'install: [example]\n')
        def fail(argv, **kwargs):
            if '--prefix' in argv:
                return fake(argv, **kwargs)
            return SimpleNamespace(returncode=19)
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(ValueError, 'pip install failed'):
                cache.ensure(root, recipe, run=fail)
            self.assertIsNone(cache.lookup(root, recipe))
            entry = Path(root) / 'entries' / cache.key(recipe)
            self.assertEqual({p.name for p in entry.iterdir()}, {'build.lock', 'last-failure'})
            prefix = cache.ensure(root, recipe, run=fake)
            self.assertEqual(prefix, cache.lookup(root, recipe))
            (Path(prefix) / 'bin/python').unlink()
            self.assertIsNone(cache.lookup(root, recipe))
            rebuilt = cache.ensure(root, recipe, run=fake)
            self.assertNotEqual(prefix, rebuilt)
            self.assertEqual(cache.lookup(root, recipe), rebuilt)

    def test_concurrent_cold_failures_share_one_attempt_then_a_later_job_can_retry(self):
        recipe = source()
        claimed, waiting = threading.Event(), threading.Event()
        calls = []
        def fail(argv, **kwargs):
            calls.append(argv)
            claimed.set()
            self.assertTrue(waiting.wait(4))
            return SimpleNamespace(returncode=13)
        acquire = cache._lock
        def observed(file, deadline):
            if claimed.is_set():
                waiting.set()
            acquire(file, deadline)
        with tempfile.TemporaryDirectory() as tmp, ThreadPoolExecutor(2) as pool, patch.object(cache, '_lock', observed):
            first = pool.submit(cache.ensure, tmp, recipe, run=fail)
            self.assertTrue(claimed.wait(4))
            second = pool.submit(cache.ensure, tmp, recipe, run=fail)
            for operation in (first, second):
                with self.assertRaisesRegex(ValueError, 'conda create failed'):
                    operation.result(timeout=6)
            self.assertEqual(len(calls), 1)
            prefix = cache.ensure(tmp, recipe, run=fake)
            self.assertEqual(cache.lookup(tmp, recipe), prefix)

    def test_default_build_ignores_checkout_path_and_installer_settings(self):
        unsafe = {k: '/checkout/untrusted' for k in
                  ('PATH', 'HOME', 'TMPDIR', 'GIT_CONFIG_GLOBAL', 'CONDA_PKGS_DIRS', 'HTTPS_PROXY', 'SSL_CERT_FILE')}
        def build(argv, **kwargs):
            env = kwargs['env']
            self.assertEqual(env['PATH'], cache.BUILD_PATH)
            self.assertNotIn('/checkout/untrusted', env.values())
            self.assertNotIn('HTTPS_PROXY', env)
            self.assertNotIn('CONDA_PKGS_DIRS', env)
            self.assertNotIn('SSL_CERT_FILE', env)
            self.assertEqual(env['GIT_CONFIG_GLOBAL'], os.devnull)
            return fake(argv, **kwargs)
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, unsafe):
            cache.ensure(root, source(), run=build)

    def test_fetches_exact_upstream_commit_in_build_scope_without_workflow_credentials(self):
        spec = BASE + ('requirements: [r.txt]\nupstream:\n  - repository: example/up\n'
                       '    ref: origin/work/branch\n')
        recipe = source(spec, {'r.txt': '-ccon.txt\n', 'con.txt': 'pkg==1\n'}, OTHER)
        with tempfile.TemporaryDirectory() as root:
            mirror = Path(root) / 'upstreams/example/up.git'
            mirror.mkdir(parents=True)
            calls = []
            def run(argv, **kwargs):
                self.assertTrue(argv[:1] == ['scope'])
                self.assertNotIn('GH_TOKEN', kwargs['env'])
                self.assertNotIn('PYTHONPATH', kwargs['env'])
                self.assertEqual(kwargs['env']['HTTPS_PROXY'], 'http://proxy')
                calls.append(argv)
                return fake(argv, **kwargs)
            cache.ensure(root, recipe, scope=('scope',), run=run,
                         build_env={'GH_TOKEN': 'inaccessible', 'PYTHONPATH': 'bad', 'HTTPS_PROXY': 'http://proxy'})
            fetch = next(c for c in calls if 'fetch' in c)
            self.assertEqual(fetch[-2:], [str(mirror), OTHER])
            self.assertTrue(any('checkout' in c and c[-1] == OTHER for c in calls))
            self.assertTrue(any('-e' in c and '-c' in c and c[c.index('-c') + 1].endswith('/con.txt') for c in calls))

    def test_legacy_requires_equal_spec_files_and_commits_under_an_explicit_alias(self):
        recipe = source(BASE + 'requirements: [r.txt]\n', {'r.txt': 'pkg==1\n'})
        old = dict(repository='example/model-a', sha=OTHER, spec='schema=1\npython="3.12"\nrequirements=["r.txt"]',
                   files={'r.txt': 'pkg==1\n'}, upstream=[])
        with tempfile.TemporaryDirectory() as tmp:
            legacy = Path(tmp) / 'old'
            (legacy / 'bin').mkdir(parents=True)
            (legacy / 'bin/python').symlink_to(sys.executable)
            metadata = legacy / 'conda-meta/python-3.12.4-build.json'
            metadata.parent.mkdir()
            metadata.write_text(json.dumps({'version': '3.12.4', 'subdir': 'linux-64', 'name': 'python'}))
            (legacy / 'ci-env.json').write_text(json.dumps(old))
            args = dict(root=Path(tmp) / 'cache', recipe=recipe, legacy_prefix=legacy,
                        legacy_repository='example/model-a', run=lambda *_a, **_k: self.fail())
            with patch.object(cache.platform, 'machine', return_value='x86_64'), patch.object(cache.platform, 'system', return_value='Linux'):
                self.assertEqual(cache.ensure(**args), str(legacy))
                metadata.write_text(json.dumps({'version': '3.12.4', 'subdir': 'linux-aarch64', 'name': 'python'}))
                self.assertNotEqual(cache.ensure(**{**args, 'root': Path(tmp) / 'other', 'run': fake}), str(legacy))
                metadata.write_text(json.dumps({'version': '3.12.4', 'subdir': 'linux-64', 'name': 'python'}))
            old['files']['r.txt'] = 'pkg==2\n'
            (legacy / 'ci-env.json').write_text(json.dumps(old))
            self.assertNotEqual(cache.ensure(**{**args, 'run': fake}), str(legacy))
