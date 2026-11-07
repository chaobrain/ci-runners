"""``ci-run env build`` and ``env refresh`` (design decisions 11.4, 12.10, 12.11, §4.9,
consumers §3a): the spec and its files come from the default branch's head only; a spec
cannot set a reserved variable or pass pip an option; the ``conda`` table is solved once,
before pip, and a malformed one builds nothing; the env is linked only once it is
complete; a refresh rebuilds only on a moved upstream, holds no token and keeps the old
env when it fails."""
from __future__ import annotations

import base64
import fcntl
import io
import json
import os
import pathlib
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from ci_runners import envbuild, envuse, inventory, registry
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
#: the first consumer: live NEST from conda-forge with Python 3.13, two upstreams
RICH = '''schema = 1
python = "3.13"
requirements = ["requirements-dev.txt"]
[conda]
channels = ["conda-forge"]
packages = ["nest-simulator=3.10"]
[[upstream]]
repository = "chaobrain/brainstate"
ref = "origin/main"
[[upstream]]
repository = "chaobrain/brainevent"
ref = "origin/main"
'''
BAD_CONDA = [
    '[conda]\nchannels = "conda-forge"\n',  # not a list
    '[conda]\nchannels = []\n',
    '[conda]\nchannels = ["https://evil.example/c"]\n',
    '[conda]\nchannels = ["/abs/dir"]\n',
    '[conda]\nchannels = ["a", "a"]\n',
    '[conda]\nchannels = [1]\n',
    '[conda]\npackages = "nest-simulator"\n',  # not a list
    '[conda]\npackages = [1]\n',
    '[conda]\npackages = ["--channel=evil"]\n',
    '[conda]\npackages = ["-c evil"]\n',
    '[conda]\npackages = ["evil::nest-simulator"]\n',
    '[conda]\npackages = ["https://evil.example/x.tar.bz2"]\n',
    '[conda]\npackages = ["./pkg.conda"]\n',
    '[conda]\npackages = ["/abs/pkg.conda"]\n',
    '[conda]\npackages = ["pkg; rm -rf /"]\n',
    '[conda]\npackages = ["pkg=1 --force"]\n',
    '[conda]\npackages = ["python=3.9"]\n',  # the python key sets it
    '[conda]\npackages = ["Python"]\n',
    '[conda]\npackages = ["numpy", "numpy=2"]\n',
    '[conda]\npackages = [""]\n',
    '[conda]\nextra = 1\n',
    'conda = 3\n',
    'conda = ["nest-simulator"]\n',
]


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

    def test_the_conda_table_can_ask_for_live_nest_on_python_313(self):
        s = envbuild.parse_spec(RICH)
        self.assertEqual(s.python, '3.13')
        self.assertEqual(s.conda, envbuild.Conda(('conda-forge',), ('nest-simulator=3.10',)))

    def test_the_conda_table_is_optional_and_its_parts_default(self):
        self.assertEqual(envbuild.parse_spec(GOOD).conda, envbuild.Conda(('conda-forge',), ()))
        s = envbuild.parse_spec(GOOD + '[conda]\npackages = ["numpy>=1.26,<3", "nest-simulator=3.10=*_mpi_*", "x==1.2.*"]\n')
        self.assertEqual(s.conda.channels, ('conda-forge',))
        self.assertEqual(len(s.conda.packages), 3)
        s = envbuild.parse_spec(GOOD.replace('[[upstream]]', '[conda]\n[[upstream]]', 1))  # an empty table
        self.assertEqual(s.conda, envbuild.Conda())
        s = envbuild.parse_spec(GOOD.replace('[[upstream]]', '[conda]\nchannels = ["bioconda", "conda-forge"]\n[[upstream]]', 1))
        self.assertEqual(s.conda.channels, ('bioconda', 'conda-forge'))

    def test_a_malformed_conda_table_is_refused(self):
        for bad in BAD_CONDA:
            text = bad + GOOD if bad.startswith('conda') else GOOD.replace('[[upstream]]', bad + '[[upstream]]', 1)
            with self.assertRaises(SpecError, msg=bad):
                envbuild.parse_spec(text)

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

    def test_the_conda_packages_are_solved_with_python_in_one_step_before_pip(self):
        spec = envbuild.parse_spec(RICH.replace('["conda-forge"]', '["bioconda", "conda-forge"]'))
        cmds = envbuild.steps(spec, project='p', envs='/e', stamp='20261106T010203Z', reqdir='/r', conda='conda')
        self.assertEqual(cmds[0], ['conda', 'create', '--yes', '--quiet', '--prefix', '/e/p-20261106T010203Z',
                                   '--override-channels', '-c', 'bioconda', '-c', 'conda-forge', 'python=3.13', 'pip',
                                   'nest-simulator=3.10'])
        self.assertEqual([c for c in cmds if 'conda' in c[0]], cmds[:1])  # the only conda command: one solve
        pips = [i for i, c in enumerate(cmds) if 'pip' in c and c[0] != 'conda']
        self.assertTrue(pips and min(pips) > 0)
        self.assertEqual(sum('nest-simulator=3.10' in c for c in cmds), 1)  # no pip step carries it

    def test_a_spec_without_conda_keeps_its_one_channel(self):
        cmds = envbuild.steps(envbuild.parse_spec(GOOD), project='p', envs='/e', stamp='20261106T010203Z', reqdir='/r',
                              conda='conda')
        self.assertEqual(cmds[0][cmds[0].index('--override-channels'):],
                         ['--override-channels', '-c', 'conda-forge', 'python=3.12', 'pip'])
        self.assertEqual(cmds[0].count('-c'), 1)

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
        self.assertEqual(spec.text, GOOD)  # the spec as read, for the env to keep
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


class Host:
    """Fake ``conda``, ``pip`` and ``git``: nothing touches the network or a package index.
    ``heads`` maps a repository to the commit of its ``main``; ``private`` repositories
    refuse any read without credentials."""

    def __init__(self, heads=None, private=(), fail=None):
        self.heads, self.private, self.fail = dict(heads or {}), set(private), fail
        self.calls, self.cloned, self.pip_files = [], {}, []

    def __call__(self, cmd, env=None, **kw):
        self.calls.append((cmd, env))
        out, rc = '', 0
        if cmd[0] == 'git' and cmd[1] == 'ls-remote':
            repo = cmd[-2].split('github.com/')[1][:-4]
            if repo in self.private:
                rc = 128
            else:
                out = f'{self.heads[repo]}\t{cmd[-1]}\n'
        elif cmd[0] == 'git' and cmd[1] == 'clone':
            repo = cmd[cmd.index('--branch') + 2].split('github.com/')[1][:-4]
            if repo in self.private and not (env or {}).get('GIT_CONFIG_VALUE_0'):
                rc = 128  # no credentials, no private repository
            else:
                os.makedirs(cmd[-1])
                self.cloned[cmd[-1]] = self.heads[repo]
        elif cmd[0] == 'git' and cmd[1] == '-C':
            out = self.cloned[cmd[2]] + '\n'
        elif 'create' in cmd:
            os.makedirs(os.path.join(cmd[cmd.index('--prefix') + 1], 'bin'))
        elif '-r' in cmd:  # a pip step: the requirement files exist while it runs
            path = cmd[cmd.index('-r') + 1]
            self.pip_files.append((path, pathlib.Path(path).read_text()))
        if self.fail and self.fail(cmd, len(self.calls)):
            rc = 1
        return types.SimpleNamespace(returncode=rc, stdout=out)

    def ran(self, kind):
        return [c for c, _ in self.calls if kind(c)]


def is_conda(c):
    return 'create' in c


class Base(unittest.TestCase):
    """One host ``hb``, one project ``p`` (repository ``Org/Repo``), and a contents API."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = pathlib.Path(self.tmp.name)
        self.etc, self.data = t / 'etc', t / 'data'
        (self.etc / 'projects').mkdir(parents=True)
        self.entry('p', 'Org/Repo')
        self.inv = inventory.load(f'''schema = 1
this = "hb"
[hosts.hb]
account = "acct-x"
home = "{t}"
data = "{self.data}"
slots = 1
memory_max = "120G"
disk_min_gib = 0
gpus = [0]
max_temp_c = 80
pools = {{ light = 1 }}
files = {{ slot_lock = "{t}/s.lock", slot_queue = "{t}/s.queue" }}
''')
        self.api = Api({HEAD: {'.github/ci-runners.toml': GOOD, 'requirements-dev.txt': 'pytest\n'}})
        self.host = Host({'chaobrain/brainstate': 'a' * 40, 'chaobrain/brainevent': 'd' * 40})
        self.envs = self.data / 'ci-runners' / 'envs'
        self.clock = 0
        patch = mock.patch.object(envbuild, '_stamp', self.stamp)
        patch.start()
        self.addCleanup(patch.stop)

    def stamp(self):
        """The builds' clock: each call is a second later."""
        self.clock += 1
        return f'20261107T0000{self.clock:02d}Z'

    def tearDown(self):
        self.tmp.cleanup()

    def entry(self, name, repo, extra=''):
        (self.etc / 'projects' / f'{name}.toml').write_text(
            f'name = "{name}"\nrepository = "{repo}"\ncontext = "{name}/ci"\n{extra}[hosts.hb]\nmemory_max = "2G"\n')

    def main(self, *args, run=None, verb='build', project='p', token=True, env=None):
        out, err = io.StringIO(), io.StringIO()
        env = {'PATH': '/usr/bin:/bin', 'https_proxy': 'http://127.0.0.1:1', **(env or {})}
        if token:
            env['GH_TOKEN'] = 'SECRET-TKN-9'
        with mock.patch.dict(os.environ, env, clear=True), redirect_stderr(err), redirect_stdout(out):
            code = envbuild.main([verb, *([project] if project else []), *args], self.inv, str(self.etc),
                                 call=self.api, run=run or self.host)
        self.err = err.getvalue()
        return code, out.getvalue()


class Main(Base):
    @property
    def ran(self):
        return self.host.calls

    def run_step(self, fail_at=None):
        self.host.fail = (lambda cmd, n: n == fail_at) if fail_at is not None else None
        return self.host

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
        self.assertEqual(len(gits), 2)  # the clone, and the commit it is at
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
        self.assertEqual(sorted(n for n in os.listdir(self.envs) if n[0] != '.'),
                         ['p', 'p-20250101T000000Z'])  # the failed build is gone

    def test_a_real_directory_in_the_way_is_not_replaced(self):
        (self.envs / 'p').mkdir(parents=True)
        code, _ = self.main()
        self.assertEqual(code, 2)
        self.assertEqual(self.ran, [])

    def test_a_conda_table_builds_in_one_solve_before_pip(self):
        self.api = Api({HEAD: {'.github/ci-runners.toml': RICH, 'requirements-dev.txt': 'pytest\n'}})
        code, _ = self.main('--conda', '/c/conda')
        self.assertEqual(code, 0, self.err)
        cmds = [c for c, _ in self.ran if c[0] != 'git']
        creates = [i for i, c in enumerate(cmds) if is_conda(c)]
        self.assertEqual(creates, [0])  # one solve, first
        create = cmds[0]
        self.assertTrue({'python=3.13', 'pip', 'nest-simulator=3.10'} <= set(create), create)
        self.assertEqual(create[create.index('--override-channels') + 1:][:2], ['-c', 'conda-forge'])
        self.assertTrue(all('pip' in c and 'install' in c for c in cmds[1:]))
        self.assertFalse(any('nest-simulator=3.10' in c for c in cmds[1:]))

    def test_a_malformed_conda_table_builds_nothing(self):
        for bad in BAD_CONDA:
            text = bad + GOOD if bad.startswith('conda') else GOOD.replace('[[upstream]]', bad + '[[upstream]]', 1)
            self.api = Api({HEAD: {'.github/ci-runners.toml': text, 'requirements-dev.txt': 'pytest\n'}})
            self.host.calls.clear()
            code, _ = self.main()
            self.assertEqual(code, 2, bad)
            self.assertEqual(self.ran, [], bad)  # not one command
            self.assertFalse(self.envs.exists(), bad)  # not a directory

    def test_the_dry_run_shows_the_conda_packages(self):
        self.api = Api({HEAD: {'.github/ci-runners.toml': RICH, 'requirements-dev.txt': 'pytest\n'}})
        code, out = self.main('--dry-run')
        self.assertEqual(code, 0)
        self.assertIn('python=3.13 pip nest-simulator=3.10', out)
        self.assertEqual(self.ran, [])

    def test_a_build_keeps_its_spec_files_and_upstream_commits(self):
        self.api = Api({HEAD: {'.github/ci-runners.toml': RICH, 'requirements-dev.txt': 'pytest\n'}})
        self.assertEqual(self.main()[0], 0, self.err)
        rec = json.loads((self.envs / 'p' / 'ci-env.json').read_text())
        self.assertEqual((rec['sha'], rec['spec'], rec['files']), (HEAD, RICH, {'requirements-dev.txt': 'pytest\n'}))
        self.assertEqual([(u['repository'], u['branch'], u['commit']) for u in rec['upstream']],
                         [('chaobrain/brainstate', 'main', 'a' * 40), ('chaobrain/brainevent', 'main', 'd' * 40)])
        spec, files, *_ = envbuild.parse_spec(rec['spec']), rec['files']
        self.assertEqual(spec.conda.packages, ('nest-simulator=3.10',))

    def test_a_build_never_removes_a_directory_that_is_not_its_own(self):
        code, _ = self.main()
        self.assertEqual(code, 0)
        first = os.readlink(self.envs / 'p')
        self.clock = 0  # the same second again: the same directory name
        code, _ = self.main()
        self.assertEqual(code, 2)
        self.assertEqual(os.readlink(self.envs / 'p'), first)
        self.assertTrue((self.envs / first / 'bin').is_dir())  # the linked env is intact

    def test_a_concurrent_build_of_the_project_is_refused(self):
        self.envs.mkdir(parents=True)
        with open(self.envs / '.p.lock', 'a') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            code, _ = self.main()
        self.assertEqual(code, 2)
        self.assertEqual(self.ran, [])
        self.assertIn('running', self.err)

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


NEW_BRAINSTATE = 'e' * 40


class Refresh(Base):
    """``env refresh``: built once as a maintainer would, then the upstreams move."""

    def setUp(self):
        super().setUp()
        self.entry('p', 'Org/Repo', 'refresh = true\n')
        self.api = Api({HEAD: {'.github/ci-runners.toml': RICH, 'requirements-dev.txt': 'pytest\n'}})
        self.assertEqual(self.main()[0], 0, self.err)
        self.first = os.readlink(self.envs / 'p')
        self.host.calls.clear()
        self.host.cloned.clear()
        self.host.pip_files.clear()

    def refresh(self, *args, project='p'):
        return self.main(*args, verb='refresh', project=project, token=False)

    def test_an_unmoved_upstream_rebuilds_nothing(self):
        code, _ = self.refresh()
        self.assertEqual(code, 0, self.err)
        self.assertEqual([c[:2] for c in self.host.ran(lambda c: True)], [['git', 'ls-remote']] * 2)  # looks only
        self.assertEqual(os.readlink(self.envs / 'p'), self.first)
        self.assertEqual(sorted(n for n in os.listdir(self.envs) if n[0] != '.'), ['p', self.first])
        self.assertIn('up to date', self.err)

    def test_a_moved_upstream_rebuilds_from_the_kept_spec_and_links_in_one_rename(self):
        self.host.heads['chaobrain/brainstate'] = NEW_BRAINSTATE
        self.api = None  # the default branch is not read: only the kept spec is
        code, _ = self.refresh('--conda', '/c/conda')
        self.assertEqual(code, 0, self.err)
        new = os.readlink(self.envs / 'p')
        self.assertNotEqual(new, self.first)
        self.assertTrue((self.envs / self.first).is_dir())  # a running part keeps the env it started with
        creates = self.host.ran(is_conda)
        self.assertEqual(len(creates), 1)
        self.assertTrue({'python=3.13', 'nest-simulator=3.10'} <= set(creates[0]))  # the kept conda table
        self.assertEqual(self.host.pip_files, [(self.host.pip_files[0][0], 'pytest\n')])  # the kept file
        rec = json.loads((self.envs / new / 'ci-env.json').read_text())
        self.assertEqual({u['repository']: u['commit'] for u in rec['upstream']},
                         {'chaobrain/brainstate': NEW_BRAINSTATE, 'chaobrain/brainevent': 'd' * 40})
        self.assertEqual((rec['sha'], rec['spec']), (HEAD, RICH))

    def test_the_timer_finds_conda_by_the_environment(self):
        self.host.heads['chaobrain/brainstate'] = NEW_BRAINSTATE
        code, _ = self.main(verb='refresh', token=False, env={'CI_RUNNERS_CONDA': '/opt/c/conda'})
        self.assertEqual(code, 0, self.err)
        self.assertIn('/opt/c/conda', self.host.ran(is_conda)[0])

    def test_a_refresh_holds_no_token(self):
        self.host.heads['chaobrain/brainstate'] = NEW_BRAINSTATE
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {'PATH': '/usr/bin:/bin', 'GH_TOKEN': 'SECRET-TKN-9', 'HOME': '/h',
                                          'https_proxy': 'http://127.0.0.1:1'}, clear=True), \
                redirect_stderr(err), redirect_stdout(out):
            self.assertEqual(envbuild.main(['refresh', 'p'], self.inv, str(self.etc), run=self.host), 0, err.getvalue())
        gits = [(c, e) for c, e in self.host.calls if c[0] == 'git']
        self.assertTrue(gits)
        for cmd, env in gits:
            self.assertNotIn('SECRET-TKN-9', ' '.join(cmd) + ' '.join(env.values()))
            self.assertFalse(any(k.startswith('GIT_CONFIG_KEY') for k in env))  # no extraheader
            self.assertEqual(env['GIT_CONFIG_GLOBAL'], os.devnull)  # no stored credential either
            self.assertEqual(env['https_proxy'], 'http://127.0.0.1:1')  # the egress
        self.assertFalse(any('SECRET-TKN-9' in c for cmd, _ in self.host.calls for c in cmd))

    def test_a_private_upstream_refuses_the_refresh(self):
        self.host.heads['chaobrain/brainstate'] = NEW_BRAINSTATE  # the first moved ...
        self.host.private.add('chaobrain/brainevent')  # ... the second cannot be read without a token
        code, _ = self.refresh()
        self.assertEqual(code, 2)
        self.assertIn('without a token', self.err)
        self.assertEqual(self.host.ran(is_conda), [])  # nothing built
        self.assertEqual(os.readlink(self.envs / 'p'), self.first)
        self.assertEqual(sorted(n for n in os.listdir(self.envs) if n[0] != '.'), ['p', self.first])

    def test_a_failed_rebuild_keeps_the_old_env(self):
        self.host.heads['chaobrain/brainstate'] = NEW_BRAINSTATE
        for what in ('create', 'clone', '-r', '-e', 'rev-parse'):  # each kind of step, failing in turn
            self.host.calls.clear()
            self.host.fail = lambda cmd, n, what=what: what in cmd
            code, _ = self.refresh()
            self.assertEqual(code, 2, what)
            self.assertTrue(self.host.ran(lambda c, w=what: w in c), what)  # the step ran, and failed
            self.assertEqual(os.readlink(self.envs / 'p'), self.first, what)
            self.assertEqual(sorted(n for n in os.listdir(self.envs) if n[0] != '.'), ['p', self.first], what)
            self.assertTrue((self.envs / self.first / 'bin').is_dir(), what)
            rec = json.loads((self.envs / 'p' / 'ci-env.json').read_text())
            self.assertEqual(rec['upstream'][0]['commit'], 'a' * 40, what)  # still records what is installed

    def test_a_failed_refresh_is_tried_again_by_the_next(self):
        self.host.heads['chaobrain/brainstate'] = NEW_BRAINSTATE
        self.host.fail = lambda cmd, n: is_conda(cmd)
        self.assertEqual(self.refresh()[0], 2)
        self.host.fail = None
        self.assertEqual(self.refresh()[0], 0, self.err)
        self.assertNotEqual(os.readlink(self.envs / 'p'), self.first)

    def test_an_env_that_kept_no_spec_is_not_refreshed(self):
        rec = self.envs / self.first / 'ci-env.json'
        old = json.loads(rec.read_text())
        for bad in ({k: v for k, v in old.items() if k != 'spec'},
                    {**old, 'upstream': [{'repository': 'chaobrain/brainstate', 'branch': 'main'}]},  # no commit
                    {**old, 'upstream': [{**u, 'commit': 'not-a-commit'} for u in old['upstream']]},
                    {**old, 'repository': 'org/other'},
                    {**old, 'upstream': old['upstream'][:1]}):
            rec.write_text(json.dumps(bad))
            self.host.calls.clear()
            self.assertEqual(self.refresh()[0], 2)
            self.assertEqual(self.host.calls, [])

    def test_a_refresh_with_a_project_that_has_no_env_does_nothing(self):
        self.entry('q', 'Org/Q')
        self.assertEqual(self.refresh(project='q')[0], 2)
        self.assertEqual(self.host.calls, [])

    def test_a_refresh_during_a_build_waits_for_the_next_tick(self):
        self.host.heads['chaobrain/brainstate'] = NEW_BRAINSTATE
        with open(self.envs / '.p.lock', 'a') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            code, _ = self.refresh()
        self.assertEqual(code, 0)
        self.assertEqual(self.host.ran(is_conda), [])
        self.assertEqual(os.readlink(self.envs / 'p'), self.first)

    def test_all_takes_the_projects_whose_entry_sets_refresh(self):
        self.entry('q', 'Org/Q')  # no refresh
        self.entry('r', 'Org/R', 'refresh = false\n')
        (self.etc / 'projects' / 'bad.toml').write_text('refresh = "yes"\n')
        self.assertEqual(registry.refresh_projects(str(self.etc), 'hb'), ['p'])
        self.assertEqual(registry.refresh_projects(str(self.etc), 'elsewhere'), [])
        self.host.heads['chaobrain/brainstate'] = NEW_BRAINSTATE
        code, _ = self.refresh('--all', project=None)
        self.assertEqual(code, 0, self.err)
        self.assertNotEqual(os.readlink(self.envs / 'p'), self.first)

    def test_all_goes_on_after_a_failure_and_reports_it(self):
        self.entry('a', 'Org/A', 'refresh = true\n')  # refresh asked, but never built; sorts before p
        code, _ = self.refresh('--all', project=None)
        self.assertEqual(code, 2)
        self.assertIn('ci-run env refresh: a: ', self.err)
        self.assertIn('ci-run env refresh: p: up to date', self.err)  # p, after a, still ran

    def test_the_arguments(self):
        for args in ((['refresh'], {}), (['refresh', 'p', '--all'], {}), (['refresh', 'p', '--dry-run'], {}),
                     (['build', '--all'], {}), (['build'], {})):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit, msg=args):
                envbuild.main(args[0], self.inv, str(self.etc))

    def test_the_registry_flag_is_a_boolean(self):
        for bad in ('"yes"', '1', '[]'):
            self.entry('p', 'Org/Repo', f'refresh = {bad}\n')
            with self.assertRaises(registry.RegistryError, msg=bad):
                registry.load_file(str(self.etc), 'p', 'hb')
        self.entry('p', 'Org/Repo', 'refresh = true\n')
        self.assertTrue(registry.load_file(str(self.etc), 'p', 'hb').refresh)
        self.entry('p', 'Org/Repo')
        self.assertFalse(registry.load_file(str(self.etc), 'p', 'hb').refresh)


class Held(Base):
    """A build a live part holds is never pruned (design decision 12.11)."""

    def builds(self):
        return sorted(n for n in os.listdir(self.envs) if n[0] != '.' and n != 'p')

    def test_a_held_build_survives_the_prune_that_would_remove_it(self):
        self.assertEqual(self.main()[0], 0, self.err)
        oldest = self.builds()[0]
        fd = envuse.hold(str(self.envs / oldest))[1]  # a part that started on it
        self.assertIsNotNone(fd)
        for _ in range(3):  # three refreshes during the part: KEEP builds are newer than it
            self.assertEqual(self.main()[0], 0, self.err)
        self.assertIn(oldest, self.builds())  # kept, though over the limit
        self.assertEqual(len(self.builds()), envbuild.KEEP + 1)
        os.close(fd)  # the part ends
        self.assertEqual(self.main()[0], 0, self.err)
        self.assertNotIn(oldest, self.builds())  # the next build removes it
        self.assertEqual(len(self.builds()), envbuild.KEEP)

    def test_a_build_nobody_holds_is_removed_and_the_hold_keeps_the_link_target(self):
        self.envs.mkdir(parents=True)
        for n in ('a', 'b'):
            (self.envs / n).mkdir()
        fd = envuse.hold(str(self.envs / 'a'))[1]
        self.assertFalse(envuse.remove_unused(str(self.envs / 'a')))
        self.assertTrue((self.envs / 'a').is_dir())
        self.assertTrue(envuse.remove_unused(str(self.envs / 'b')))
        self.assertFalse((self.envs / 'b').exists())
        os.close(fd)

    def test_an_unlockable_build_is_kept(self):
        (self.envs / 'a').mkdir(parents=True)
        with mock.patch.object(envuse.os, 'open', side_effect=PermissionError):
            self.assertFalse(envuse.remove_unused(str(self.envs / 'a')))
        self.assertTrue((self.envs / 'a').is_dir())

    def test_the_hold_names_the_build_the_link_has_now(self):
        self.envs.mkdir(parents=True)
        for n in ('a', 'b'):
            (self.envs / n).mkdir()
        (self.envs / 'l').symlink_to('a')
        build, fd = envuse.hold(str(self.envs / 'l'))
        self.assertEqual(build, str(self.envs / 'a'))
        os.close(fd)
        self.assertEqual(envuse.hold(str(self.envs / 'nope')), (str(self.envs / 'nope'), None))


class Units(unittest.TestCase):
    """The timer files: written by this repository, installed by a setup plan the lead applies."""
    ROOT = pathlib.Path(__file__).resolve().parents[1] / 'setup' / 'units'

    def test_the_timer_runs_the_service_every_30_minutes(self):
        timer = (self.ROOT / 'ci-envrefresh.timer').read_text()
        self.assertIn('OnUnitActiveSec=30min\n', timer)
        self.assertIn('OnBootSec=', timer)
        self.assertIn('WantedBy=timers.target\n', timer)

    def test_the_service_refreshes_every_project_that_asks_and_holds_no_token(self):
        service = (self.ROOT / 'ci-envrefresh.service').read_text()
        self.assertIn('ExecStart=@CI_DATA@/ci-runners/releases/hook/bin/ci-run env refresh --all\n', service)
        self.assertIn('Type=oneshot\n', service)
        self.assertIn('Environment=CI_RUNNERS_ETC=@CI_ETC@\n', service)
        self.assertIn('Environment=CI_RUNNERS_PYTHON=@CI_PYTHON@\n', service)
        self.assertIn('NoNewPrivileges=yes\n', service)
        self.assertNotIn('GH_TOKEN', service)
        self.assertNotIn('token', service.replace('no token', ''))


if __name__ == '__main__':
    unittest.main()
