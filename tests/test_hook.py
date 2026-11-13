"""The job-started hook end to end, on files (design §6.3): it reads the policy and the
event payload, prints one fixed line, logs details on the host only, and refuses on
any error."""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock

from ci_runners import hook

ROOT = pathlib.Path(__file__).resolve().parents[1]
EVENTS = ROOT / 'tests' / 'fixtures' / 'events'
POLICY = '''schema = 2
actors = ["alice"]
[repositories."chaobrain/brainkit"]
id = 1001
visibility = "private"
'''

#: this host's inventory (C2c): the hook takes each runner's class and audience from it
INVENTORY = '''schema = 1
this = "r"
[hosts.r]
account = "acct-x"
home = "/h"
data = "/d"
slots = 2
memory_max = "8G"
disk_min_gib = 1
gpus = [0, 1]
max_temp_c = 80
isolation = "account"
pools = { part = 1, gpu = { per_card = true }, pub-gpu = { per_card = true, class = "gpu", audience = "public", labels = ["ci-pub-gpu", "r-pub-gpu"] } }
files = { slot_lock = "/d/s.lock", slot_queue = "/d/s.queue" }
'''
PUB_POOL = 'pub-gpu = { per_card = true, class = "gpu", audience = "public", labels = ["ci-pub-gpu", "r-pub-gpu"] }'
PUBLIC_REPO = '[repositories."chaobrain/brainevent"]\nid = 2002\nvisibility = "public"\npublic_runners = true\n'


def example() -> str:
    """The example inventory with its public pool, isolation and home runner (issue #232,
    once the light pool carries ci-light, as the example says) uncommented."""
    import re
    text = (ROOT / 'setup' / 'inventory.example.toml').read_text()
    text = re.sub(r'^# (isolation = "[^"]*")\s.*$', r'\1', text, flags=re.M)
    text = re.sub(r'^# (pub-gpu = .*)$', r'\1', text, flags=re.M)
    return re.sub(r'^# (home = \{.*)$', r'\1', text, flags=re.M).replace(
        'labels = ["3090b-light"] }', 'labels = ["ci-light", "3090b-light"] }', 1)


class Started(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.tmp.name)
        self.etc = self.base / 'etc'
        self.etc.mkdir()
        (self.etc / 'policy.toml').write_text(POLICY)
        (self.etc / 'inventory.toml').write_text(INVENTORY)
        self.env = {'CI_RUNNERS_ETC': str(self.etc), 'CI_RUNNER_CLASS': 'part', 'RUNNER_NAME': 'r-part-1',
                    'GITHUB_REPOSITORY': 'chaobrain/brainkit', 'GITHUB_REPOSITORY_ID': '1001',
                    'GITHUB_EVENT_NAME': 'pull_request', 'GITHUB_ACTOR': 'alice', 'GITHUB_TRIGGERING_ACTOR': 'alice',
                    'GITHUB_EVENT_PATH': str(EVENTS / 'pr_same_repo.json')}

    def tearDown(self):
        self.tmp.cleanup()

    def log(self):
        return [json.loads(x) for p in sorted((self.base / 'logs').glob('hook-*.log')) for x in p.read_text().splitlines()]

    def test_accept_and_refuse(self):
        code, line = hook.started(self.env)
        self.assertEqual(code, 0)
        self.assertRegex(line, r'\Aci-runners: accepted ok policy=[0-9a-f]{12}\Z')
        code, line = hook.started({**self.env, 'GITHUB_EVENT_PATH': str(EVENTS / 'pr_fork.json')})
        self.assertEqual((code, line.split()[1:3]), (1, ['refused', 'fork']))
        self.assertEqual([r['reason'] for r in self.log()], ['ok', 'fork'])
        self.assertEqual(self.log()[0]['runner'], 'r-part-1')

    def public_event(self) -> pathlib.Path:
        """A same-repository pull request of the opted-in public repository."""
        ev = json.loads((EVENTS / 'pr_same_repo.json').read_text())
        pub = {'id': 2002, 'full_name': 'chaobrain/brainevent', 'private': False}
        ev['repository'] = ev['pull_request']['head']['repo'] = ev['pull_request']['base']['repo'] = pub
        path = self.base / 'public.json'
        path.write_text(json.dumps(ev))
        return path

    def test_the_public_pool_by_the_runner_s_audience(self):
        """C2 (decision 16.7, 18.7): the runner's audience, the inventory's for its name (C2c);
        an opted-in public repository on its pool's GPU runners only, with its reason logged."""
        (self.etc / 'policy.toml').write_text(POLICY + PUBLIC_REPO)
        path = self.public_event()
        env = {**self.env, 'GITHUB_REPOSITORY': 'chaobrain/brainevent', 'GITHUB_REPOSITORY_ID': '2002',
               'GITHUB_EVENT_PATH': str(path), 'CI_RUNNER_CLASS': 'gpu', 'CI_RUNNER_AUDIENCE': 'public',
               'RUNNER_NAME': 'r-pub-gpu0-17'}
        gpu = {**env, 'RUNNER_NAME': 'r-gpu0', 'CI_RUNNER_AUDIENCE': 'private'}  # a private gpu runner
        # what a caller's CI_RUNNER_CLASS and CI_RUNNER_AUDIENCE say changes nothing
        cases = [(env, 'ok'), (gpu, 'audience'), ({**gpu, 'CI_RUNNER_AUDIENCE': 'public'}, 'audience'),
                 ({**env, 'CI_RUNNER_CLASS': 'part', 'CI_RUNNER_AUDIENCE': 'private'}, 'ok'),
                 ({k: v for k, v in env.items() if not k.startswith('CI_RUNNER_')}, 'ok'),
                 ({**self.env, 'CI_RUNNER_AUDIENCE': 'public', 'CI_RUNNER_CLASS': 'gpu'}, 'ok'), (self.env, 'ok')]
        for e, why in cases:
            code, line = hook.started(e)
            self.assertEqual((code, line.split()[2]), (0 if why == 'ok' else 1, why), e)
        got = self.log()
        self.assertEqual([r['reason'] for r in got], [why for _, why in cases])
        self.assertEqual([(r['class'], r['audience']) for r in got],
                         [('gpu', 'public'), ('gpu', 'private'), ('gpu', 'private'), ('gpu', 'public'), ('gpu', 'public'),
                          ('part', 'private'), ('part', 'private')])

    def test_every_runner_setup_makes_is_accepted_as_its_own_env_says(self):
        """C2c: each runner setup registers (``setup._runners``), with the class its ``.env``
        says (``setup.env_file``), is the inventory's to the hook, on every host of the example."""
        import re
        from ci_runners import inventory, setup
        text = example()
        seen = []
        for role in inventory.load(text).hosts:
            doc = re.sub(r'^this = "[^"]*"', f'this = "{role}"', text, count=1, flags=re.M)
            (self.etc / 'inventory.toml').write_text(doc)
            i = inventory.load(doc)
            for (name, _, cls, card, slot), pool in zip(setup._runners(i), setup.runner_pools(i), strict=True):
                env = dict(x.split('=', 1) for x in setup.env_file(i, cls, str(self.etc), card, slot, pool).splitlines())
                self.assertEqual(hook.runner_facts(str(self.etc), name), (env['CI_RUNNER_CLASS'], 'private'), name)
                self.assertNotIn('CI_RUNNER_AUDIENCE', env)  # unset: private
                seen.append((name, env['CI_RUNNER_CLASS']))
        self.assertGreater(len(seen), 5)
        self.assertIn(('3090b-home-1', 'light'), seen)  # a home runner: class light, no ci- label
        self.assertIn(('3090b-light-1', 'light'), seen)

    def test_the_decision_s_environment_is_the_allowlist_for_every_pool_kind(self):
        """C2c (review): ``job-started.sh`` starts the decision under ``env -i`` with what the
        hook reads (``hook.ENV``) where it is set, ``HOME`` and a fixed ``PATH``. Every variable
        the hook reads arrives, as it was, on a runner of each pool kind of the example's hosts
        and on a public runner; nothing else does, whatever a job's ``env:`` or the runner's
        ``.env`` plants (``LD_*``, ``PYTHON*``, ``CI_RUNNER*``)."""
        import re
        from ci_runners import inventory, policy, setup
        src = (ROOT / 'hooks' / 'job-started.sh').read_text()
        listed = re.search(r'\nfor v in (.*?); do\n', src, re.S).group(1).replace('\\\n', ' ').split()
        self.assertEqual(listed, [*hook.ENV, 'HOME'])
        read = set()
        for mod in (hook, policy):
            read |= set(re.findall(r"env\.get\('((?:GITHUB|RUNNER)_[A-Z_]+)'", pathlib.Path(mod.__file__).read_text()))
        self.assertEqual(read, set(hook.ENV))  # what the hook reads, and nothing it does not
        rel = self.base / 'releases'
        rel.mkdir()
        os.symlink(ROOT, rel / 'hook')
        mark = self.base / 'env.0'
        fake = self.base / 'py' / 'python3'  # the interpreter setup wrote, recording what it gets
        fake.parent.mkdir()
        fake.write_text(f'#!/bin/sh\n/usr/bin/env -0 > {mark}\nexec {sys.executable} "$@"\n')
        fake.chmod(0o755)
        hooks = self.base / 'hooks'
        hooks.mkdir()
        (hooks / 'job-started.sh').write_text(setup.hook_text(src, str(fake), str(self.base), str(self.etc)))
        (self.etc / 'policy.toml').write_text(POLICY + 'classes = ["compile", "nightly", "slurm"]\n' + PUBLIC_REPO)
        planted = self.base / 'planted'
        planted.mkdir()
        (planted / 'policy.toml').write_text('schema = 2\n')
        forged = {'LD_PRELOAD': str(planted / 'x.so'), 'LD_LIBRARY_PATH': str(planted), 'PYTHONPATH': str(planted),
                  'PYTHONSTARTUP': str(planted / 'x.py'), 'PYTHONHOME': str(planted), 'CI_RUNNER_CLASS': 'slurm',
                  'CI_RUNNER_AUDIENCE': 'public', 'CI_RUNNERS_ETC': str(planted), 'CI_RUNNERS_PYTHON': '/bin/true',
                  'RUNNER_TEMP': str(planted), 'GITHUB_TOKEN': 'x', 'BASH_ENV': str(planted / 'x.sh'), 'ENV': 'x'}
        text = example()
        kinds = {}
        for role in inventory.load(text).hosts:
            doc = re.sub(r'^this = "[^"]*"', f'this = "{role}"', text, count=1, flags=re.M)
            i = inventory.load(doc)
            for (name, _, cls, card, slot), pool in zip(setup._runners(i), setup.runner_pools(i), strict=True):
                runner_env = dict(x.split('=', 1) for x in setup.env_file(i, cls, str(self.etc), card, slot, pool).splitlines())
                kinds.setdefault((cls, pool == inventory.home_pool(i.host)), (doc, name, runner_env, self.env))
            if inventory.public_pool(i) is not None:
                pub = {**self.env, 'GITHUB_REPOSITORY': 'chaobrain/brainevent', 'GITHUB_REPOSITORY_ID': '2002',
                       'GITHUB_EVENT_PATH': str(self.public_event())}
                kinds.setdefault(('gpu', 'public'), (doc, f'{role}-pub-gpu{i.host["gpus"][-1]}-17', {}, pub))
        self.assertEqual({k[0] for k in kinds}, {'light', 'part', 'gpu', 'multi', 'compile'})
        self.assertIn(('light', True), kinds)  # a home runner
        self.assertIn(('gpu', 'public'), kinds)
        odd = 'a b"\'$(touch x)`touch y`\\\n*'  # read for a protected push alone: passed as it is
        for n, (kind, (doc, name, runner_env, job)) in enumerate(sorted(kinds.items(), key=str)):
            with self.subTest(kind=kind, runner=name):
                (self.etc / 'inventory.toml').write_text(doc)
                env = {**runner_env, **forged, **job, 'RUNNER_NAME': name, 'HOME': str(self.base),
                       'PATH': f'{planted}:/usr/bin:/bin', 'GITHUB_RUN_ID': str(100 + n), 'GITHUB_REF_PROTECTED': odd}
                if n % 3 == 1:
                    del env['GITHUB_REF_PROTECTED']  # unset stays unset
                elif n % 3 == 2:
                    env['GITHUB_REF_PROTECTED'] = ''  # and set but empty, set
                r = subprocess.run(['sh', str(hooks / 'job-started.sh')], env=env, capture_output=True, text=True,
                                   timeout=60, cwd=self.base)
                self.assertEqual((r.returncode, r.stdout.split()[:3]), (0, ['ci-runners:', 'accepted', 'ok']), r.stderr)
                got = dict(x.split('=', 1) for x in mark.read_text().split('\0') if x)
                got = {k: v for k, v in got.items() if k not in ('PWD', 'SHLVL', '_')}  # the recorder's shell's
                self.assertEqual(got, {'PATH': '/usr/bin:/bin', 'HOME': str(self.base),
                                       **{k: env[k] for k in hook.ENV if k in env}})
        self.assertFalse((self.base / 'x').exists() or (self.base / 'y').exists())

    def test_the_hook_decides_from_the_public_runner_s_stripped_inventory(self):
        """C2's review (C3 generates it): a public runner's etc holds ``inventory.public_view``,
        never the host's inventory; the hook decides from it as from the host's."""
        from ci_runners import inventory
        host = INVENTORY.replace('isolation = "account"\n', 'isolation = "account"\npush_key = "ssh-ed25519 '
                                 f'AAAAC3NzaC1lZDI1NTE5AAAAI{"k" * 43}"\n'
                                 'cpus = "0-15"\n') + '[hosts.other]\n' + INVENTORY.split('[hosts.r]\n', 1)[1] \
            .replace('pub-gpu = { per_card = true, class = "gpu", audience = "public", labels = ["ci-pub-gpu", "r-pub-gpu"] }',
                     'light = 1')
        inv = inventory.load(host)
        view = inventory.public_view(inv, [1])
        for secret in ('acct-x', '"/h"', '"/d"', 'ssh-ed25519', 'other', '0-15', '/d/s.lock'):
            self.assertNotIn(secret, view)
        self.assertEqual(inventory.load(view).public, inv.public)
        (self.etc / 'policy.toml').write_text(POLICY + PUBLIC_REPO)
        (self.etc / 'inventory.toml').write_text(view)
        pub = {**self.env, 'GITHUB_REPOSITORY': 'chaobrain/brainevent', 'GITHUB_REPOSITORY_ID': '2002',
               'GITHUB_EVENT_PATH': str(self.public_event())}
        for name, why in (('r-pub-gpu1-17', 'ok'), ('r-pub-gpu0-17', 'runner'), ('r-part-1', 'runner'),
                          ('other-pub-gpu1-17', 'runner'), ('r-gpu1', 'audience')):
            self.assertEqual(hook.started({**pub, 'RUNNER_NAME': name})[1].split()[2], why, name)
        for bad in ([], [2], [0, 5]):
            with self.assertRaisesRegex(inventory.InventoryError, 'are not the host'):
                inventory.public_view(inv, bad)
        with self.assertRaisesRegex(inventory.InventoryError, 'no public pool'):
            inventory.public_view(inventory.load(INVENTORY.replace(', ' + PUB_POOL, '')), [0])
        self.assertEqual(inventory.public_view(inv, [1, 0, 1]), inventory.public_view(inv, [0, 1]))

    def test_the_runner_s_class_and_audience_are_the_inventory_s(self):
        """C2c (decision 18.7): by ``RUNNER_NAME``, never the environment's word."""
        def why(**kw):
            env = {k: v for k, v in {**self.env, **kw}.items() if v is not None}
            code, line = hook.started(env)
            self.assertEqual(code, 0 if line.split()[2] == 'ok' else 1)
            return line.split()[2]
        for kw in ({'RUNNER_NAME': 'r-gpu1'}, {'RUNNER_NAME': 'r-gpu1', 'CI_RUNNER_CLASS': None},
                   {'CI_RUNNER_CLASS': 'gpu', 'CI_RUNNER_AUDIENCE': 'public'}, {'CI_RUNNER_CLASS': 'nightly'}):
            self.assertEqual(why(**kw), 'ok', kw)
        self.assertEqual([(r['runner'], r['class'], r['audience']) for r in self.log()],
                         [('r-gpu1', 'gpu', 'private')] * 2 + [('r-part-1', 'part', 'private')] * 2)
        for kw in ({'RUNNER_NAME': 'r-part-2'}, {'RUNNER_NAME': 'x-part-1'}, {'RUNNER_NAME': None},
                   {'RUNNER_NAME': ''}, {'RUNNER_NAME': 'r-gpu2'},  # no such card
                   {'RUNNER_NAME': 'r-pub-gpu2-1'}, {'RUNNER_NAME': 'x-pub-gpu0-1'}, {'RUNNER_NAME': 'r-pub-gpu0'},
                   {'RUNNER_NAME': 'r-pub-gpu0-1x'}, {'RUNNER_NAME': 'r-pub-gpu0-12345678901234'}):
            self.assertEqual(why(**kw, CI_RUNNER_CLASS='gpu', CI_RUNNER_AUDIENCE='public'), 'runner', kw)
        self.assertEqual(self.log()[-1]['error'], 'runner: the inventory names no such runner')
        self.assertEqual(why(RUNNER_NAME='r-pub-gpu1-12345678'), 'audience')  # a public runner: brainkit is private
        self.assertEqual(self.log()[-1]['audience'], 'public')
        # the public pool's runners exist only where the inventory has its pool
        (self.etc / 'inventory.toml').write_text(INVENTORY.replace(', ' + PUB_POOL, ''))
        self.assertEqual(why(RUNNER_NAME='r-pub-gpu0-1'), 'runner')
        self.assertEqual(why(), 'ok')
        for text in ('schema = 7\n', 'not toml [', '\xff'):
            (self.etc / 'inventory.toml').write_bytes(text.encode('latin-1'))
            self.assertEqual(why(), 'inventory', text)
        (self.etc / 'inventory.toml').unlink()
        self.assertEqual(why(), 'inventory')
        self.assertTrue(self.log()[-1]['error'].startswith('inventory: '))
        saved = hook.MAX_INVENTORY
        hook.MAX_INVENTORY = 100
        try:
            (self.etc / 'inventory.toml').write_text(INVENTORY)
            self.assertEqual(why(), 'inventory')
        finally:
            hook.MAX_INVENTORY = saved

    def test_errors_refuse(self):
        cases = {
            'no etc': ({**self.env, 'CI_RUNNERS_ETC': ''}, 'error'),
            'no policy': ({**self.env, 'CI_RUNNERS_ETC': str(self.base / 'nowhere')}, 'policy'),
            'no event file': ({**self.env, 'GITHUB_EVENT_PATH': str(self.base / 'missing.json')}, 'payload'),
        }
        bad = self.base / 'bad.json'
        bad.write_text('{"repository": ')
        cases['bad json'] = ({**self.env, 'GITHUB_EVENT_PATH': str(bad)}, 'payload')
        deep = self.base / 'deep.json'
        deep.write_text('[' * 100000 + ']' * 100000)
        cases['deep json'] = ({**self.env, 'GITHUB_EVENT_PATH': str(deep)}, 'payload')
        for what, (env, reason) in cases.items():
            code, line = hook.started(env)
            self.assertEqual((code, line.split()[2]), (1, reason), what)
        (self.etc / 'policy.toml').write_text('schema = 3\n')
        self.assertEqual(hook.started(self.env)[1].split()[1:3], ['refused', 'policy'])

    def test_an_oversized_event_is_refused(self):
        saved = hook.MAX_EVENT
        hook.MAX_EVENT = 100  # a valid payload, but over the bound
        try:
            code, line = hook.started(self.env)
        finally:
            hook.MAX_EVENT = saved
        self.assertEqual((code, line.split()[2]), (1, 'payload'))
        self.assertEqual(self.log()[-1]['error'], 'event: ValueError')  # the bound, not a parse error

    def _main(self, env, bound=1):
        """``hook.main(['job-started'])`` in a child, its bound lowered; the child itself
        is bounded, so a missing alarm fails the test instead of hanging it."""
        code = (f'import sys; sys.path.insert(0, {str(ROOT)!r}); from ci_runners import hook; '
                f'hook.START_BOUND = {bound}; sys.exit(hook.main(["job-started"]))')
        return subprocess.run([sys.executable, '-I', '-c', code], env={**env, 'PATH': '/usr/bin:/bin'},
                              capture_output=True, text=True, timeout=20)

    def test_a_hung_read_is_refused_at_the_bound(self):
        fifo = self.base / 'event.fifo'
        os.mkfifo(fifo)  # nobody writes: opening it blocks until the alarm
        r = self._main({**self.env, 'GITHUB_EVENT_PATH': str(fifo)})
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertRegex(r.stdout, r'\Aci-runners: refused timeout policy=[0-9a-f]{12}\n\Z')
        self.assertEqual(self.log()[-1]['reason'], 'timeout')

    def test_a_planted_log_fifo_neither_blocks_nor_breaks_the_line(self):
        (self.base / 'logs').mkdir()
        day = datetime.datetime.now(datetime.timezone.utc)
        logs = [self.base / 'logs' / f'hook-{d:%Y-%m-%d}.log' for d in (day, day + datetime.timedelta(days=1))]
        for log in logs:  # today's, and tomorrow's in case the date turns
            os.mkfifo(log)
        r = self._main(self.env, bound=5)
        self.assertEqual((r.returncode, r.stdout.split()[:3]), (0, ['ci-runners:', 'accepted', 'ok']), r.stderr)
        self.assertEqual(r.stderr, '')
        victim = self.base / 'victim'
        victim.write_text('kept\n')
        for log in logs:
            os.unlink(log)
            os.symlink(victim, log)  # a link is not followed either
        r = self._main(self.env, bound=5)
        self.assertEqual((r.returncode, r.stderr, victim.read_text()), (0, '', 'kept\n'))

    def test_the_bound_outside_the_decision_still_prints_the_fixed_line(self):
        saved = hook._log
        def slow(*_a):
            raise hook._Timeout()  # as if the alarm fired while the log line was written
        hook._log = slow
        try:
            with unittest.mock.patch('builtins.print') as out:
                self.assertEqual(hook.main(['job-started']), 1)
        finally:
            hook._log = saved
        out.assert_called_once_with('ci-runners: refused timeout policy=none', flush=True)

    def test_the_wrapper_prints_one_line_and_exits_with_the_decision(self):
        rel = self.base / 'releases'
        rel.mkdir()
        os.symlink(ROOT, rel / 'hook')
        hooks = self.base / 'hooks'
        hooks.mkdir()
        for n in ('job-started.sh', 'job-completed.sh'):
            (hooks / n).write_bytes((ROOT / 'hooks' / n).read_bytes())
        # as the repository has it, no etc: the environment's is never read, so it refuses
        r = subprocess.run(['bash', '-e', str(hooks / 'job-started.sh')], env={**self.env, 'PATH': '/usr/bin:/bin'},
                           capture_output=True, text=True, timeout=60)
        self.assertEqual((r.returncode, r.stdout.split()[:3]), (1, ['ci-runners:', 'refused', 'error']), r.stderr)
        from ci_runners import setup
        src = (ROOT / 'hooks' / 'job-started.sh').read_text()
        (hooks / 'job-started.sh').write_text(setup.hook_text(src, sys.executable, str(self.base), str(self.etc)))
        r = subprocess.run(['bash', '-e', str(hooks / 'job-started.sh')], env={**self.env, 'PATH': '/usr/bin:/bin'},
                           capture_output=True, text=True, timeout=60)
        self.assertEqual((r.returncode, r.stdout.count('\n')), (0, 1), r.stderr)
        self.assertTrue(r.stdout.startswith('ci-runners: accepted ok'))
        os.unlink(rel / 'hook')
        r = subprocess.run(['bash', '-e', str(hooks / 'job-started.sh')], env=self.env, capture_output=True,
                           text=True, timeout=60)
        self.assertEqual((r.returncode, r.stdout), (1, 'ci-runners: refused error policy=none\n'))
        r = subprocess.run(['bash', '-e', str(hooks / 'job-completed.sh')], env=self.env, capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(r.returncode, 0)

    def test_the_wrappers_run_the_interpreter_setup_wrote(self):
        """The installed hook runs the inventory's python (h100: the system's is 3.10),
        written in by setup; a job's environment cannot choose another (CI_RUNNERS_PYTHON
        is ignored, so /bin/true cannot admit a job)."""
        from ci_runners import setup
        rel = self.base / 'releases'
        rel.mkdir()
        os.symlink(ROOT, rel / 'hook')
        hooks = self.base / 'copies'  # a copy away from <base>/hooks, as in the private /tmp (decision 13.5)
        hooks.mkdir()
        mark = self.base / 'ran'
        fake = self.base / 'py' / 'python3'
        fake.parent.mkdir()
        fake.write_text(f'#!/bin/sh\necho "$@" >> {mark}\nexec {sys.executable} "$@"\n')
        fake.chmod(0o755)
        for n in ('job-started.sh', 'job-completed.sh'):
            (hooks / n).write_text(setup.hook_text((ROOT / 'hooks' / n).read_text(), str(fake), str(self.base),
                                                   str(self.etc)))
        # C2c: the hook reads the etc setup wrote, never the one a job's environment names
        planted = self.base / 'planted'
        planted.mkdir()
        (planted / 'policy.toml').write_text(POLICY)
        (planted / 'inventory.toml').write_text(INVENTORY)
        (self.etc / 'policy.toml').write_text('schema = 2\n')  # refuses every job
        r = subprocess.run(['sh', str(hooks / 'job-started.sh')], env={**self.env, 'CI_RUNNERS_ETC': str(planted),
                                                                        'PATH': '/usr/bin:/bin'},
                           capture_output=True, text=True, timeout=60, cwd=self.base)
        # the planted policy would accept it; the host's has no such repository
        self.assertEqual((r.returncode, r.stdout.split()[:3]), (1, ['ci-runners:', 'refused', 'repository']), r.stderr)
        with self.assertRaisesRegex(ValueError, 'no absolute etc'):
            setup.hook_text((ROOT / 'hooks' / 'job-started.sh').read_text(), str(fake), str(self.base), 'etc')
        self.assertIn("\netc='/data/x y/etc'\n", setup.hook_text((ROOT / 'hooks' / 'job-started.sh').read_text(),
                                                               str(fake), str(self.base), '/data/x y/etc'))
        trap = self.base / 'trap'  # a job may put its own dirname and readlink first in PATH
        for tool in ('dirname', 'readlink'):
            (trap / tool).parent.mkdir(exist_ok=True)
            (trap / tool).write_text('#!/bin/sh\necho /nowhere\n')
            (trap / tool).chmod(0o755)
        for n, code in (('job-started.sh', 1), ('job-completed.sh', 0)):
            for py in ('/bin/true', str(fake), ''):
                if mark.exists():
                    mark.unlink()
                r = subprocess.run(['sh', str(hooks / n)], env={**self.env, 'PATH': f'{trap}:/usr/bin:/bin',
                                                                  'CI_RUNNERS_PYTHON': py},
                                   capture_output=True, text=True, timeout=60, cwd=self.base)
                self.assertEqual(r.returncode, code, (n, py, r.stdout, r.stderr))
                self.assertTrue(mark.read_text().startswith('-I -c '), (n, py))
        src = (ROOT / 'hooks' / 'job-started.sh').read_text()
        with self.assertRaisesRegex(ValueError, 'no interpreter line'):
            setup.hook_text(src.replace(setup.HOOK_PYTHON, 'exec python3 -I\n'), str(fake), str(self.base))
        with self.assertRaisesRegex(ValueError, 'no release line'):
            setup.hook_text(src.replace(setup.HOOK_BASE, ''), str(fake), str(self.base))
        # a copy whose release line names no release refuses every job (fail closed)
        (hooks / 'job-started.sh').write_text(setup.hook_text(src, str(fake), str(self.base / 'nowhere'), str(self.etc)))
        r = subprocess.run(['sh', str(hooks / 'job-started.sh')], env={**self.env, 'PATH': '/usr/bin:/bin'},
                           capture_output=True, text=True, timeout=60, cwd=self.base)
        self.assertEqual((r.returncode, r.stdout), (1, 'ci-runners: refused error policy=none\n'))
        text = setup.hook_text(src, '/opt/py/bin/python3', '/data/x/ci-runners', '/data/x/ci-runners/etc')
        self.assertIn('\nbase=/data/x/ci-runners\nroot=', text)
        self.assertNotIn('@CI_', text)


class Completed(unittest.TestCase):
    """Issue #257: job-completed marks the parts whose executor outlived the job (a cancel
    the step did not pass on), and stops one still waiting, before it stops the scopes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = pathlib.Path(self.tmp.name)  # <data>/ci-runners
        self.etc = self.base / 'etc'
        self.etc.mkdir()
        self.env = {'CI_RUNNERS_ETC': str(self.etc), 'RUNNER_NAME': 'r-gpu0', 'GITHUB_RUN_ID': '77',
                    'GITHUB_REPOSITORY': 'Org/Repo'}

    def child(self):
        p = subprocess.Popen(['sleep', '60'])
        self.addCleanup(lambda: (p.poll() is None and p.kill(), p.wait()))
        return p

    def record(self, rid, pid, *, runner='r-gpu0', run='77-1', start=None, **extra):
        d = self.base / 'state' / 'parts' / 'org' / 'repo' / run / f'{rid}-1'
        d.mkdir(parents=True)
        (d / 'record.json').write_text(json.dumps({'runner': runner, 'executor': pid,
                                                   'start_epoch': time.time() if start is None else start, **extra}))
        return d

    def test_the_parts_it_marks_and_stops(self):
        from ci_runners import outcome
        waiting, running, other, done, again = self.child(), self.child(), self.child(), self.child(), self.child()
        w = self.record('wait', waiting.pid)
        r = self.record('run', running.pid, run_epoch=time.time())  # its command runs: its scope is stopped
        o = self.record('other', other.pid, runner='r-gpu1')  # another runner's job
        f = self.record('done', done.pid)
        (f / 'outcome.json').write_text('{}')  # it ended before its job did
        a = self.record('again', again.pid, run='77-2')  # a later run attempt, by this runner: this job's
        n = self.record('next', again.pid, run='78-1')  # another run
        gone = subprocess.Popen(['true'])
        gone.wait()
        g = self.record('gone', gone.pid)  # its executor died while the job ran: the host's end, not a cancel
        reused = self.record('reused', other.pid, runner='r-gpu0', run='77-3', start=time.time() - 3600)  # a pid reused
        got = hook.cancel_parts(self.env)
        self.assertEqual(sorted(got), ['77-1/run-1', '77-1/wait-1', '77-2/again-1'])
        for d in (w, r, a):
            self.assertEqual(json.loads((d / outcome.CANCELLED).read_text())['by'], 'job-completed')
        for d in (o, f, n, g, reused):
            self.assertFalse((d / outcome.CANCELLED).exists(), d)
        self.assertEqual(waiting.wait(timeout=10), -15)  # still waiting: it ends stopped, and runs nothing
        self.assertEqual(again.wait(timeout=10), -15)
        self.assertIsNone(running.poll())  # left to its scope
        self.assertIsNone(other.poll())
        self.assertEqual(hook.cancel_parts(self.env), ['77-1/run-1'])  # marked once; the stopped ones are gone

    def test_a_job_without_its_names_marks_nothing(self):
        p = self.child()
        self.record('wait', p.pid)
        for k in ('RUNNER_NAME', 'GITHUB_RUN_ID', 'GITHUB_REPOSITORY'):
            self.assertEqual(hook.cancel_parts({**self.env, k: ''}), [], k)
        self.assertEqual(hook.cancel_parts({**self.env, 'GITHUB_RUN_ID': '7'}), [])  # not a prefix match
        self.assertIsNone(p.poll())

    def test_completed_marks_before_it_stops_the_scopes(self):
        from ci_runners import outcome
        p = self.child()
        w = self.record('wait', p.pid)
        calls = []

        def run(argv, **kw):
            calls.append((argv, (w / outcome.CANCELLED).exists()))
            return subprocess.CompletedProcess(argv, 0, 'ci-part-r-gpu0-x-77-1-1.scope loaded active running x\n', '')
        with unittest.mock.patch.object(hook.subprocess, 'run', run):
            self.assertEqual(hook.completed(self.env), 0)
        self.assertEqual([marked for _, marked in calls], [True, True])
        self.assertEqual(calls[1][0][:3], ['systemctl', '--user', 'stop'])
        [line] = [json.loads(x) for x in next((self.base / 'logs').glob('hook-*.log')).read_text().splitlines()]
        self.assertEqual((line['cancelled'], line['stopped']), (['77-1/wait-1'], ['ci-part-r-gpu0-x-77-1-1.scope']))
        with unittest.mock.patch.object(hook, 'cancel_parts', side_effect=OSError('x')), \
                unittest.mock.patch.object(hook.subprocess, 'run', run):
            self.assertEqual(hook.completed(self.env), 0)  # the scopes are stopped all the same
        self.assertEqual(len(calls), 4)

if __name__ == '__main__':
    unittest.main()
