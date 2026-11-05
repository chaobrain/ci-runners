"""``ci-run env build`` (design decision 11.4, consumers §3a): the spec and its files come
from the default branch's head only; a spec cannot set a reserved variable or pass pip
an option; the env is linked only once it is complete."""
from __future__ import annotations

import base64
import io
import os
import pathlib
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from ci_runners import envbuild, inventory
from ci_runners.envbuild import SpecError

GOOD = '''schema = 1
python = "3.12"
requirements = ["requirements-dev.txt"]
install = ["pytest-xdist", "numpy>=1.26,<3", "jax[cuda12]==0.4.30"]
[[upstream]]
repository = "chaobrain/brainstate"
ref = "origin/main"
[variables]
XLA_PYTHON_CLIENT_PREALLOCATE = "false"
'''
HEAD, OTHER = 'b' * 40, 'c' * 40


class Spec(unittest.TestCase):
    def test_a_good_spec(self):
        s = envbuild.parse_spec(GOOD)
        self.assertEqual(s.python, '3.12')
        self.assertEqual(s.requirements, ('requirements-dev.txt',))
        self.assertEqual(s.upstream, (envbuild.Upstream('chaobrain/brainstate', 'main'),))
        self.assertEqual(s.variables, {'XLA_PYTHON_CLIENT_PREALLOCATE': 'false'})

    def test_a_reserved_variable_is_refused(self):
        for name in ('PYTHONPATH', 'LD_PRELOAD', 'CUDA_VISIBLE_DEVICES', 'GITHUB_TOKEN', 'CI_RESULTS_DIR', 'PATH',
                     'lower'):
            with self.assertRaises(SpecError, msg=name):
                envbuild.parse_spec(GOOD.replace('XLA_PYTHON_CLIENT_PREALLOCATE', name))

    def test_install_takes_specifiers_only(self):
        for bad in ('--index-url=https://evil', '-e .', '.', './pkg', 'git+https://github.com/x/y', 'x @ https://y',
                    '/abs/path', 'pkg; rm -rf /'):
            with self.assertRaises(SpecError, msg=bad):
                envbuild.parse_spec(GOOD.replace('"pytest-xdist"', f'"{bad}"'))

    def test_paths_stay_inside_the_repository(self):
        for bad in ('../x.txt', '/etc/passwd', 'a/../../x', 'a//b', './a', ''):
            with self.assertRaises(SpecError, msg=bad):
                envbuild.parse_spec(GOOD.replace('"requirements-dev.txt"', f'"{bad}"'))

    def test_upstream_refs_are_branches_of_origin(self):
        for bad in ('main', 'refs/pull/1/head', 'origin/', 'origin/a..b', 'upstream/main'):
            with self.assertRaises(SpecError, msg=bad):
                envbuild.parse_spec(GOOD.replace('"origin/main"', f'"{bad}"'))
        with self.assertRaises(SpecError):
            envbuild.parse_spec(GOOD.replace('"chaobrain/brainstate"', '"chaobrain/.."'))

    def test_schema_and_keys(self):
        for bad in (GOOD.replace('schema = 1', 'schema = 2'), GOOD + 'extra = 1\n', GOOD.replace('"3.12"', '"py3"'),
                    'not toml = ='):
            with self.assertRaises(SpecError):
                envbuild.parse_spec(bad)

    def test_nested_requirement_files(self):
        text = '-r base.txt\n--requirement=../top.txt\n-c constraints/c.txt\nnumpy\n'
        self.assertEqual(envbuild.nested('req/dev.txt', text), ['req/base.txt', 'top.txt', 'req/constraints/c.txt'])
        with self.assertRaises(SpecError):
            envbuild.nested('dev.txt', '-r ../../outside.txt\n')


class Steps(unittest.TestCase):
    def test_the_commands(self):
        s = envbuild.parse_spec(GOOD)
        cmds = envbuild.steps(s, project='myproj', envs='/e', stamp='20261106T010203Z', reqdir='/r', conda='/c/conda',
                              scope=('scope',))
        d = '/e/myproj-20261106T010203Z'
        self.assertEqual(cmds[0], ['scope', '/c/conda', 'create', '--yes', '--quiet', '--prefix', d,
                                   '--override-channels', '-c', 'conda-forge', 'python=3.12', 'pip'])
        self.assertEqual(cmds[1], ['git', 'clone', '--quiet', '--depth', '1', '--branch', 'main',
                                   'https://github.com/chaobrain/brainstate.git', f'{d}/src/brainstate'])
        self.assertEqual(cmds[2][-5:], ['-r', '/r/requirements-dev.txt', 'pytest-xdist', 'numpy>=1.26,<3',
                                        'jax[cuda12]==0.4.30'])
        self.assertEqual(cmds[3][-2:], ['-e', f'{d}/src/brainstate'])
        self.assertTrue(all(c[1] == f'{d}/bin/python' for c in cmds[2:]))

    def test_bad_names_never_make_a_path(self):
        s = envbuild.parse_spec(GOOD)
        for project, stamp in (('../x', '20261106T010203Z'), ('p', '../../x')):
            with self.assertRaises(SpecError):
                envbuild.steps(s, project=project, envs='/e', stamp=stamp, reqdir='/r', conda='c')

    def test_prune_keeps_the_newest(self):
        names = ['p-20260101T000000Z', 'p-20260301T000000Z', 'p-20260201T000000Z', 'p-20260401T000000Z', 'p',
                 'pq-20250101T000000Z', 'p-junk']
        self.assertEqual(envbuild.prune(names, 'p'), ['p-20260101T000000Z'])
        self.assertEqual(envbuild.prune(names[:3], 'p'), [])


class Api:
    """The contents API of one repository whose default branch is ``trunk``."""

    def __init__(self, files):
        self.files, self.calls = files, []

    def __call__(self, method, url, token, body=None):
        self.calls.append(url)
        base = 'https://api.github.com/repos/org/repo'
        if url == base:
            return {'default_branch': 'trunk'}
        if url == f'{base}/commits/trunk':
            return {'sha': HEAD}
        if url == f'{base}/commits/main':
            return {'sha': OTHER}
        for ref, files in self.files.items():
            for path, text in files.items():
                if url == f'{base}/contents/{path}?ref={ref}':
                    return {'type': 'file', 'encoding': 'base64', 'content': base64.b64encode(text.encode()).decode()}
        raise envbuild.github.ApiError(f'GET {url}: HTTP 404')


class Fetch(unittest.TestCase):
    def test_everything_is_read_at_the_default_branch_head(self):
        api = Api({HEAD: {'.github/ci-runners.toml': GOOD, 'requirements-dev.txt': '-r requirements.txt\npytest\n',
                          'requirements.txt': 'numpy\n'},
                   OTHER: {'.github/ci-runners.toml': GOOD.replace('3.12', '3.11')}})
        sha, spec, files = envbuild.fetch(api, 'tok', 'org/repo')
        self.assertEqual((sha, spec.python), (HEAD, '3.12'))
        self.assertEqual(sorted(files), ['requirements-dev.txt', 'requirements.txt'])
        contents = [u for u in api.calls if '/contents/' in u]
        self.assertEqual(len(contents), 3)
        self.assertTrue(all(u.endswith(f'?ref={HEAD}') for u in contents), contents)

    def test_too_many_files(self):
        files = {'.github/ci-runners.toml': GOOD.replace('"requirements-dev.txt"', '"r0.txt"')}
        files.update({f'r{i}.txt': f'-r r{i + 1}.txt\n' for i in range(40)})
        with self.assertRaises(SpecError):
            envbuild.fetch(Api({HEAD: files}), 'tok', 'org/repo')

    def test_the_token_reaches_git_only_by_its_environment(self):
        env = envbuild._git_env('secret-token')
        self.assertNotIn('secret-token', ' '.join(env.values()))  # encoded in a header, not a plain value
        self.assertIn(base64.b64encode(b'x-access-token:secret-token').decode(), env['GIT_CONFIG_VALUE_0'])


class Main(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = pathlib.Path(self.tmp.name)
        self.etc, self.data = t / 'etc', t / 'data'
        (self.etc / 'projects').mkdir(parents=True)
        (self.etc / 'projects' / 'p.toml').write_text(
            'name = "p"\nrepository = "Org/Repo"\ncontext = "p/ci"\n[hosts.hb]\nmemory_max = "2G"\n')
        self.inv = inventory.load(f'''schema = 1
this = "hb"
[hosts.hb]
ssh = "alias-x"
account = "acct-x"
home = "{t}"
data = "{self.data}"
trusted = true
cpu_slowdown = 1.0
slots = 1
memory_max = "120G"
disk_min_gib = 0
gpus = [0]
max_temp_c = 80
pools = {{ light = 1 }}
files = {{ host_lock = "{t}/h.lock", slot_lock = "{t}/s.lock", slot_queue = "{t}/s.queue" }}
''')
        self.api = Api({HEAD: {'.github/ci-runners.toml': GOOD, 'requirements-dev.txt': 'pytest\n'}})
        self.ran = []
        self.envs = self.data / 'ci-runners' / 'envs'

    def tearDown(self):
        self.tmp.cleanup()

    def run_step(self, fail_at=None):
        def run(cmd, env=None):
            self.ran.append((cmd, env))
            if 'create' in cmd:
                d = pathlib.Path(cmd[cmd.index('--prefix') + 1])
                (d / 'bin').mkdir(parents=True)
            return mock.Mock(returncode=1 if fail_at is not None and len(self.ran) == fail_at else 0)
        return run

    def main(self, *args, run=None):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {'GH_TOKEN': 'SECRET-TKN-9', 'PATH': '/usr/bin:/bin'}, clear=True), \
                redirect_stderr(io.StringIO()), redirect_stdout(out):
            code = envbuild.main(['build', 'p', *args], self.inv, str(self.etc), call=self.api,
                                 run=run or self.run_step())
        return code, out.getvalue()

    def test_dry_run_prints_and_builds_nothing(self):
        code, out = self.main('--dry-run')
        self.assertEqual(code, 0)
        self.assertIn('conda create', out)
        self.assertIn(f'env = "{self.envs / "p"}"', out)
        self.assertIn('XLA_PYTHON_CLIENT_PREALLOCATE = "false"', out)
        self.assertEqual(self.ran, [])
        self.assertFalse(self.envs.exists())

    def test_a_build_is_linked_when_complete(self):
        code, out = self.main('--conda', '/c/conda')
        self.assertEqual(code, 0, out)
        link = self.envs / 'p'
        self.assertTrue(link.is_symlink())
        target = os.readlink(link)
        self.assertRegex(target, r'\Ap-[0-9]{8}T[0-9]{6}Z\Z')
        self.assertTrue((self.envs / target / 'ci-env.json').exists())
        gits = [env for cmd, env in self.ran if cmd[0] == 'git']
        self.assertEqual(len(gits), 1)
        self.assertNotIn('SECRET-TKN-9', ' '.join(c for cmd, _ in self.ran for c in cmd))  # never in an argv
        self.assertTrue(all(env is None for cmd, env in self.ran if cmd[0] != 'git'))  # only git sees the token
        self.assertTrue(all(cmd[0] == 'systemd-run' for cmd, _ in self.ran if cmd[0] != 'git'))

    def test_a_failed_step_leaves_the_link_alone(self):
        self.envs.mkdir(parents=True)
        (self.envs / 'p-20250101T000000Z').mkdir()
        (self.envs / 'p').symlink_to('p-20250101T000000Z')
        code, _ = self.main(run=self.run_step(fail_at=3))
        self.assertEqual(code, 2)
        self.assertEqual(os.readlink(self.envs / 'p'), 'p-20250101T000000Z')
        self.assertEqual(sorted(os.listdir(self.envs)), ['p', 'p-20250101T000000Z'])  # the failed build is gone

    def test_a_real_directory_in_the_way_is_not_replaced(self):
        (self.envs / 'p').mkdir(parents=True)
        code, _ = self.main()
        self.assertEqual(code, 2)
        self.assertEqual(self.ran, [])

    def test_no_token(self):
        with mock.patch.dict(os.environ, {'PATH': '/usr/bin:/bin'}, clear=True), redirect_stderr(io.StringIO()):
            self.assertEqual(envbuild.main(['build', 'p'], self.inv, str(self.etc), call=self.api), 2)

    def test_a_token_file_on_the_host_is_never_read(self):
        """A Contents-read token must not rest on h100's shared account (design §3.3)."""
        (self.etc / 'envbuild.token').write_text('STORED-TKN\n')
        seen = []

        def call(method, url, token, body=None):
            seen.append(token)
            return self.api(method, url, token, body)
        with mock.patch.dict(os.environ, {'PATH': '/usr/bin:/bin'}, clear=True), redirect_stderr(io.StringIO()) as err:
            self.assertEqual(envbuild.main(['build', 'p'], self.inv, str(self.etc), call=call), 2)
        self.assertEqual(seen, [])  # refused before any call: the stored token was not used
        self.assertIn('no token', err.getvalue())


if __name__ == '__main__':
    unittest.main()
