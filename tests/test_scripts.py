"""The setup scripts the lead runs (design §5.2): ``register.sh`` keeps the
registration token off every command line and out of a shell-sourced file, and
``install.sh`` installs only the commit the lead released, never through a link,
and moves the hook only forward. Each runs against stand-ins on PATH."""
from __future__ import annotations

import json
import os
import pathlib
import stat
import subprocess
import tarfile
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
TOKEN = 'AAAATESTREGISTRATIONTOKEN0000'
SHA = 'a' * 40
SHA2 = 'b' * 40
TAGOBJ = 'c' * 40


def _exe(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)


class Register(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.tmp.name)
        self.bin = self.home / 'stubs'
        self.out = self.home / 'out'
        self.out.mkdir()
        _exe(self.home / 'data/ci-runners/releases/hook/bin/ci-run',
             '#!/bin/sh\n[ "$1" = runners ] && printf "r-light-1 light,r light\\nr-part-1 part,r part\\n"\n')
        _exe(self.bin / 'systemctl', f'#!/bin/sh\necho "$*" >> {self.out}/systemctl\n'
                                     f'[ "$3" = --state=active ] && cat {self.out}/active 2>/dev/null\nexit 0\n')
        for r in ('r-light-1', 'r-part-1'):
            d = self.home / 'data/actions-runner' / r
            # the stand-in config.sh records its argv and environment, then reads the token as the runner does
            _exe(d / 'config.sh', f'#!/bin/sh\nprintf "%s\\n" "$@" > {self.out}/{r}.argv\n'
                                  f'env > {self.out}/{r}.env\ntouch .runner\n')
            _exe(d / 'bin/Runner.Listener', '#!/bin/sh\n')
            (d / '.env').write_text('https_proxy=http://127.0.0.1:20809\nhttp_proxy=http://127.0.0.1:20809\n'
                                    'no_proxy=localhost,127.0.0.1\nCI_RUNNER_CLASS=light\n'
                                    f'LANG=C.UTF-8\nx=$(touch {self.out}/sourced)\n')

    def tearDown(self):
        self.tmp.cleanup()

    def register(self, *names, token=TOKEN):
        return subprocess.run(['sh', str(ROOT / 'setup/register.sh'), *names], input=token + '\n',
                              env={'HOME': str(self.home), 'PATH': f'{self.bin}:/usr/bin:/bin', 'SECRET_X': 'leak'},
                              capture_output=True, text=True, timeout=60)

    def test_the_token_reaches_config_only_through_its_environment(self):
        r = self.register('r-light-1', 'r-part-1')
        self.assertEqual(r.returncode, 0, r.stderr)
        argv = (self.out / 'r-part-1.argv').read_text().splitlines()
        self.assertNotIn(TOKEN, argv)
        self.assertEqual(argv[argv.index('--labels') + 1], 'part,r')
        self.assertEqual(argv[argv.index('--runnergroup') + 1], 'Default')
        env = dict(x.split('=', 1) for x in (self.out / 'r-part-1.env').read_text().splitlines() if '=' in x)
        env.pop('PWD', None), env.pop('SHLVL', None), env.pop('_', None)  # the shell's own
        self.assertEqual(env, {'PATH': '/usr/bin:/bin', 'HOME': str(self.home), 'LANG': 'C.UTF-8',
                               'ACTIONS_RUNNER_INPUT_TOKEN': TOKEN, 'https_proxy': 'http://127.0.0.1:20809',
                               'http_proxy': 'http://127.0.0.1:20809', 'no_proxy': 'localhost,127.0.0.1'})
        self.assertFalse((self.out / 'sourced').exists())  # .env is read as data, never run
        calls = (self.out / 'systemctl').read_text()
        self.assertIn('enable --now ci-runner@r-light-1.service', calls)
        self.assertIn('enable --now ci-runner@r-part-1.service', calls)

    def test_a_hostile_env_line_stops_registration(self):
        _exe(self.bin / 'evil', f'#!/bin/sh\ncat > {self.out}/stolen\n')
        env = self.home / 'data/actions-runner/r-part-1/.env'
        env.write_text(env.read_text().replace('no_proxy=localhost,127.0.0.1', f'no_proxy=a {self.bin}/evil'))
        r = self.register('r-part-1')
        self.assertEqual(r.returncode, 1)
        self.assertIn('not one word', r.stderr)
        self.assertFalse((self.out / 'stolen').exists())
        self.assertFalse((self.out / 'r-part-1.argv').exists())

    def test_refusals(self):
        self.assertEqual(self.register('r-gpu-9').returncode, 2)
        self.assertEqual(self.register('r-light-1', token='').returncode, 2)
        (self.out / 'active').write_text('ci-runner@r-part-1.service loaded active running x\n')
        r = self.register('r-light-1')
        self.assertEqual(r.returncode, 1)
        self.assertIn('stop the account', r.stderr)
        (self.out / 'active').unlink()
        os.unlink(self.home / 'data/actions-runner/r-part-1/bin/Runner.Listener')  # an unfinished unpack
        self.assertEqual(self.register('r-part-1').returncode, 1)
        self.assertEqual(self.register('r-light-1').returncode, 0)
        self.assertEqual(self.register('r-light-1').returncode, 1)  # registered already
        self.assertFalse((self.out / 'r-part-1.argv').exists())


class Install(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.tmp.name)
        self.bin = self.home / 'stubs'
        self.rel = self.home / 'data/ci-runners/releases'
        src = self.home / 'src/ci-runners-x'
        (src / 'ci_runners').mkdir(parents=True)
        (src / 'ci_runners/__init__.py').write_text('')
        self.tar = self.home / 'archive.tar.gz'
        with tarfile.open(self.tar, 'w:gz') as t:
            t.add(src, arcname='ci-runners-x')
        # refs as git ls-remote prints them for an annotated tag, a lightweight one, and
        # a branch named to look like a tag
        self.refs = self.home / 'refs'
        self.refs.write_text(f'{SHA2}\trefs/heads/refs/tags/v1.0.0\n{TAGOBJ}\trefs/tags/v1.0.0\n'
                             f'{SHA}\trefs/tags/v1.0.0^{{}}\n{SHA2}\trefs/tags/v1.1.0\n{SHA}\trefs/tags/v0.9.0\n')
        _exe(self.bin / 'git', f'#!/bin/sh\n[ "$1" = ls-remote ] || exit 9\ncat {self.refs}\n')
        _exe(self.bin / 'curl', f'#!/bin/sh\ncat {self.tar}\n')

    def tearDown(self):
        for top, dirs, _ in os.walk(self.home):
            for d in dirs:
                p = os.path.join(top, d)
                if not os.path.islink(p):
                    os.chmod(p, 0o755)
        self.tmp.cleanup()

    def install(self, *args):
        return subprocess.run(['sh', str(ROOT / 'setup/install.sh'), *args],
                              env={'HOME': str(self.home), 'PATH': f'{self.bin}:/usr/bin:/bin'},
                              capture_output=True, text=True, timeout=60)

    def hook(self):
        return os.readlink(self.rel / 'hook')

    def test_an_annotated_tag_resolves_to_its_commit(self):
        self.assertEqual(self.install('--tag', 'v1.0.0', '--expect', TAGOBJ).returncode, 1)  # the tag object
        self.assertEqual(self.install('--tag', 'v1.0.0', '--expect', SHA2).returncode, 1)  # the look-alike branch
        r = self.install('--tag', 'v1.0.0', '--expect', SHA)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.hook(), 'v1.0.0')
        self.assertEqual((self.rel / 'v1.0.0/COMMIT').read_text(), SHA + '\n')
        self.assertFalse(os.access(self.rel / 'v1.0.0/ci_runners/__init__.py', os.W_OK))
        self.assertEqual(self.install('--tag', 'v1.1.0', '--expect', SHA2).returncode, 0)  # lightweight
        self.assertEqual(self.hook(), 'v1.1.0')
        self.assertEqual(self.install('--tag', 'v2.0.0', '--expect', SHA).returncode, 1)  # no such tag
        self.assertEqual(self.install('--tag', 'v1.0.0;x', '--expect', SHA).returncode, 2)

    def test_the_hook_moves_only_forward(self):
        self.assertEqual(self.install('--tag', 'v1.1.0', '--expect', SHA2).returncode, 0)
        r = self.install('--tag', 'v1.0.0', '--expect', SHA)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.hook(), 'v1.1.0')
        self.assertEqual(self.install('--tag', 'v1.0.0', '--expect', SHA, '--rollback').returncode, 0)
        self.assertEqual(self.hook(), 'v1.0.0')
        self.assertEqual(self.install('--commit', SHA).returncode, 1)  # a tag is not left for a main commit
        self.assertEqual(self.install('--tag', 'v1.0.0', '--expect', SHA).returncode, 0)  # again: no move

    def test_a_commit_bootstraps(self):
        r = self.install('--commit', SHA)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.hook(), 'main-' + SHA[:12])
        self.assertEqual(self.install('--commit', SHA2).returncode, 0)  # main to main moves freely
        self.assertEqual(self.install('--tag', 'v0.9.0', '--expect', SHA).returncode, 0)  # main to a tag too
        self.assertEqual(self.install('--commit', 'abc').returncode, 2)

    def test_an_existing_release_must_be_the_real_one(self):
        self.rel.mkdir(parents=True)
        elsewhere = self.home / 'planted'
        (elsewhere / 'ci_runners').mkdir(parents=True)
        (elsewhere / 'COMMIT').write_text(SHA + '\n')
        os.symlink(elsewhere, self.rel / 'v1.0.0')
        r = self.install('--tag', 'v1.0.0', '--expect', SHA)
        self.assertEqual(r.returncode, 1)
        self.assertFalse(os.path.lexists(self.rel / 'hook'))
        os.unlink(self.rel / 'v1.0.0')
        (self.rel / 'v1.0.0').mkdir()
        (self.rel / 'v1.0.0/COMMIT').write_text(SHA2 + '\n')  # some other commit
        self.assertEqual(self.install('--tag', 'v1.0.0', '--expect', SHA).returncode, 1)
        (self.rel / 'v1.0.0/COMMIT').write_text(SHA + '\n')
        self.assertEqual(self.install('--tag', 'v1.0.0', '--expect', SHA).returncode, 0)  # kept as it is


class Uninstall(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.tmp.name)
        self.bin = self.home / 'stubs'
        self.out = self.home / 'out'
        self.out.mkdir()
        _exe(self.home / 'data/ci-runners/releases/hook/bin/ci-run',
             '#!/bin/sh\ncase $1 in role) echo r ;; runners) echo "r-light-1 light,r light" ;; esac\n')
        # disable fails for a missing unit; is-active is true for what the test lists
        _exe(self.bin / 'systemctl', f'#!/bin/sh\necho "$*" >> {self.out}/calls\n'
                                     f'case $2 in disable) [ "$4" != ci-proxy.service ] ;;\n'
                                     f'  is-active) grep -qx "$4" {self.out}/active 2>/dev/null ;; esac\n')
        self.units = self.home / '.config/systemd/user'
        self.units.mkdir(parents=True)
        made = []
        for n in ('ci-runner@.service', 'ci-reaper.timer', 'ci-p.slice'):
            (self.units / n).write_text('x')
            made.append({'kind': 'file', 'path': str(self.units / n)})
        (self.units / 'ci-mine.slice').write_text('not setup\'s')
        env = self.home / 'data/actions-runner/r-light-1/.env'
        env.parent.mkdir(parents=True)
        env.write_text('x')
        made.append({'kind': 'file', 'path': str(env)})
        st = self.home / 'data/ci-runners/state/setup.json'
        st.parent.mkdir(parents=True)
        st.write_text(json.dumps({'applied': [{'plan': 'p', 'made': made}]}))

    def tearDown(self):
        self.tmp.cleanup()

    def uninstall(self):
        return subprocess.run(['sh', str(ROOT / 'setup/uninstall.sh'), 'r'],
                              env={'HOME': str(self.home), 'PATH': f'{self.bin}:/usr/bin:/bin'},
                              capture_output=True, text=True, timeout=60)

    def test_each_unit_is_disabled_and_only_setups_files_go(self):
        r = self.uninstall()
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = (self.out / 'calls').read_text()
        for u in ('ci-runner@r-light-1.service', 'ci-proxy-check.timer', 'ci-reaper.timer', 'ci-proxy.service'):
            self.assertIn(f'--user disable --now {u}\n', calls)
        self.assertEqual(sorted(p.name for p in self.units.iterdir()), ['ci-mine.slice'])
        self.assertTrue((self.home / 'data/actions-runner/r-light-1/.env').exists())

    def test_another_roles_inventory_is_refused(self):
        r = subprocess.run(['sh', str(ROOT / 'setup/uninstall.sh'), 'other'],
                           env={'HOME': str(self.home), 'PATH': f'{self.bin}:/usr/bin:/bin'},
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 2)
        self.assertFalse((self.out / 'calls').exists())
        self.assertEqual(len(list(self.units.iterdir())), 4)

    def test_a_unit_still_active_removes_nothing(self):
        (self.out / 'active').write_text('ci-reaper.timer\n')
        r = self.uninstall()
        self.assertEqual(r.returncode, 1)
        self.assertIn('ci-reaper.timer', r.stderr)
        self.assertEqual(len(list(self.units.iterdir())), 4)


if __name__ == '__main__':
    unittest.main()
