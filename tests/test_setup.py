"""Host setup (design §5.2, §5.3): the plan lists every change, apply makes exactly an
approved plan and refuses another, and a second run changes nothing. On a fake host
tree; the systemctl commands are recorded, not run."""
from __future__ import annotations

import contextlib
import glob
import hashlib
import http.server
import io
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
import uuid
from unittest import mock

from ci_runners import inventory, setup

ROOT = pathlib.Path(__file__).resolve().parents[1]
SYSTEM_DIRS = setup.SYSTEM_DIRS  # before any test patches it


class Setup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = pathlib.Path(self.tmp.name)
        self.home, self.data = t / 'home', t / 'data'
        self.etc = self.data / 'ci-runners' / 'etc'
        for d in (self.home, self.etc / 'projects', self.data / 'ci-runners' / 'releases'):
            d.mkdir(parents=True)
        os.symlink(ROOT, self.data / 'ci-runners' / 'releases' / 'hook')
        (self.etc / 'inventory.toml').write_text(f'''schema = 1
this = "hb"
[hosts.hb]
account = "acct-x"
home = "{self.home}"
data = "{self.data}"
slots = 2
memory_max = "120G"
disk_min_gib = 50
gpus = [0, 1]
max_temp_c = 80
pools = {{ light = 1, part = 2 }}
files = {{ slot_lock = "/tmp/s.lock", slot_queue = "/tmp/s.queue" }}
''')
        (self.etc / 'projects' / 'p.toml').write_text('name = "p"\n[hosts.hb]\nmemory_max = "80G"\n')
        for name in ('hb-light-1', 'hb-part-1', 'hb-part-2'):  # the runner app is in place: no download
            d = self.data / 'actions-runner' / name
            for f in setup.RUNNER_FILES:
                (d / f).parent.mkdir(parents=True, exist_ok=True)
                (d / f).write_text('#!/bin/sh\n')
        self.ran = []

    def tearDown(self):
        self.tmp.cleanup()

    def run_(self, argv, **_kw):
        self.ran.append(argv)

    def apply(self, text):
        f = pathlib.Path(self.tmp.name) / 'plan.txt'
        f.write_text(text)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return setup.apply(str(self.etc), str(f), run=self.run_)

    def test_plan_apply_and_a_second_run(self):
        text = setup.build(str(self.etc)).text()
        for want in (f'mkdir  {self.etc}/ mode 700',
                     f'| ACTIONS_RUNNER_HOOK_JOB_STARTED={self.data}/ci-runners/hooks/job-started.sh',
                     '| CI_RUNNER_CLASS=part', '| MemoryMax=120G', '| MemoryMax=80G', 'labels part,hb',
                     'systemd --user (units):', 'fetched directly'):
            self.assertIn(want, text)
        for gone in ('ci-proxy', 'ci-tunnel', 'wait   '):  # no egress on this host (decision 11.12)
            self.assertNotIn(gone, text)
        self.assertEqual(self.apply(text), 0)
        self.assertEqual((self.etc.stat().st_mode & 0o777), 0o700)
        self.assertEqual(sorted(p.name for p in (self.home / '.config' / 'systemd' / 'user').glob('ci-*')),
                         ['ci-envrefresh.service', 'ci-envrefresh.timer', 'ci-hostload.service', 'ci-hostload.timer',
                          'ci-p.slice', 'ci-reaper.service', 'ci-reaper.timer', 'ci-runner@.service'])
        self.assertIn(['systemctl', '--user', 'enable', 'ci-runner@hb-light-1.service', 'ci-runner@hb-part-1.service',
                       'ci-runner@hb-part-2.service'], self.ran)
        state = json.loads((self.data / 'ci-runners' / 'state' / 'setup.json').read_text())
        self.assertEqual(len(state['applied']), 1)
        again = setup.build(str(self.etc))
        self.assertEqual(again.actions, [])  # a second run is a no-op
        self.assertNotIn('run    ', again.text())
        self.ran.clear()
        self.assertEqual(self.apply(text), 0)  # the approved plan again: nothing left to do
        self.assertEqual(self.ran, [])

    def test_a_hung_or_missing_python_is_refused(self):
        def hung(argv, **kw):
            raise subprocess.TimeoutExpired(argv, kw.get('timeout', 60))
        with self.assertRaisesRegex(ValueError, 'did not answer'):
            setup.check_python(sys.executable, run=hung)
        with self.assertRaisesRegex(ValueError, 'not an executable'):
            setup.check_python('/nonexistent/python3')

    def test_a_failed_apply_records_what_it_made(self):
        """An apply that fails half way (a systemctl call here; on h100 the runner download)
        still records the files it wrote, so the next plan owns its units instead of
        refusing them as another user's."""
        text = setup.build(str(self.etc)).text()

        def failing(argv, **_kw):
            if 'enable' in argv:
                raise subprocess.CalledProcessError(1, argv)
        f = pathlib.Path(self.tmp.name) / 'plan.txt'
        f.write_text(text)
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(subprocess.CalledProcessError):
            setup.apply(str(self.etc), str(f), run=failing)
        st = json.loads((self.data / 'ci-runners' / 'state' / 'setup.json').read_text())
        made = {m.get('path') for a in st['applied'] for m in a['made']}
        self.assertIn(str(self.home / '.config/systemd/user/ci-reaper.timer'), made)
        setup.build(str(self.etc))  # no refusal of our own units

    def test_apply_refuses_a_plan_that_is_not_the_fresh_one(self):
        text = setup.build(str(self.etc)).text()
        self.assertEqual(self.apply(text.replace('mode 700', 'mode 777', 1)), 1)
        self.assertFalse((self.data / 'ci-runners' / 'state' / 'setup.json').exists())
        self.assertEqual(self.ran, [])

    def test_a_changed_file_shows_its_diff(self):
        self.apply(setup.build(str(self.etc)).text())
        env = self.data / 'actions-runner' / 'hb-part-1' / '.env'
        env.write_text(env.read_text().replace('CI_RUNNER_CLASS=part', 'CI_RUNNER_CLASS=compile'))
        text = setup.build(str(self.etc)).text()
        self.assertIn('-CI_RUNNER_CLASS=compile', text)
        self.assertIn('+CI_RUNNER_CLASS=part', text)

    def test_a_planted_link_is_not_followed(self):
        victim = pathlib.Path(self.tmp.name) / 'victim'
        victim.write_text('kept\n')
        env = self.data / 'actions-runner' / 'hb-part-1' / '.env'
        os.symlink(victim, str(env) + '.ci-new')
        self.assertEqual(self.apply(setup.build(str(self.etc)).text()), 0)
        self.assertEqual(victim.read_text(), 'kept\n')
        self.assertFalse(env.is_symlink())
        self.assertIn('CI_RUNNER_CLASS=part', env.read_text())
        self.assertEqual(env.stat().st_mode & 0o777, 0o600)

    def test_project_names_and_memory_are_single_words(self):
        for name, mem in (('p\nExecStart=/bin/x', '80G'), ('P', '80G'), ('p', '80G\nx=1'), ('p', '-1')):
            (self.etc / 'projects' / 'p.toml').write_text(
                f'name = {json.dumps(name)}\n[hosts.hb]\nmemory_max = {json.dumps(mem)}\n')
            with self.assertRaises(ValueError, msg=(name, mem)):
                setup.build(str(self.etc))

    def test_the_retired_keys_plan_nothing(self):
        """Decision 11.12: the first path's keys (h100's inventory still holds some) and an
        old proxy table are ignored, not set up: the plan is the same without them."""
        inv = self.etc / 'inventory.toml'
        before = setup.build(str(self.etc)).text()
        text = inv.read_text().replace('slots = 2\n', 'ssh = "<alias>"\ntrusted = true\nremote = false\n'
                                       'cpu_slowdown = 2.7\nslots = 2\n', 1)
        text = text.replace('{ slot_lock = ', '{ host_lock = "/tmp/h.lock", slot_lock = ', 1)
        self.assertNotEqual(text, inv.read_text())
        self.assertIn('host_lock', text)
        inv.write_text(text + '[hosts.hb.proxy]\nclient = "/usr/bin/x -c /tmp/y"\nhttp_port = 20809\n')
        self.assertEqual(setup.build(str(self.etc)).text(), before)

    def test_the_runner_app_is_checked_and_unpacked_whole(self):
        t = pathlib.Path(self.tmp.name)
        app = t / 'app'
        for f in setup.RUNNER_FILES:
            (app / f).parent.mkdir(parents=True, exist_ok=True)
            (app / f).write_text('x')
        cache = t / 'cache' / 'runner.tgz'
        cache.parent.mkdir()
        with tarfile.open(cache, 'w:gz') as tf:
            for f in setup.RUNNER_FILES:
                tf.add(app / f, arcname=f)
        with self.assertRaisesRegex(RuntimeError, 'sha256'):
            setup._unpack(str(t / 'r1'), str(cache), 0)  # the real digest: this stand-in does not match
        self.assertFalse(cache.exists())  # a bad download is not kept
        self.assertFalse((t / 'r1').exists())
        with tarfile.open(cache, 'w:gz') as tf:
            tf.add(app / 'config.sh', arcname='config.sh')  # no listener
        with mock.patch.object(setup, 'RUNNER_SHA256', hashlib.sha256(cache.read_bytes()).hexdigest()):
            with self.assertRaisesRegex(RuntimeError, 'incomplete'):
                setup._unpack(str(t / 'r1'), str(cache), 0)
        self.assertFalse(setup.runner_complete(str(t / 'r1')))
        with tarfile.open(cache, 'w:gz') as tf:
            for f in setup.RUNNER_FILES:
                tf.add(app / f, arcname=f)
        (t / 'r2').mkdir()
        (t / 'r2' / '.env').write_text('kept')
        with mock.patch.object(setup, 'RUNNER_SHA256', hashlib.sha256(cache.read_bytes()).hexdigest()):
            setup._unpack(str(t / 'r2'), str(cache), 0)
        self.assertTrue(setup.runner_complete(str(t / 'r2')))
        self.assertEqual((t / 'r2' / '.env').read_text(), 'kept')
        self.assertFalse((t / 'r2.ci-new').exists())

    def test_the_download_waits_for_the_forward_to_listen(self):
        t = pathlib.Path(self.tmp.name)
        app = t / 'app'
        for f in setup.RUNNER_FILES:
            (app / f).parent.mkdir(parents=True, exist_ok=True)
            (app / f).write_text('x')
        tgz = t / 'src.tgz'
        with tarfile.open(tgz, 'w:gz') as tf:
            for f in setup.RUNNER_FILES:
                tf.add(app / f, arcname=f)
        body = tgz.read_bytes()

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # a proxy for plain HTTP gets the absolute URL
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_a):
                pass
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            port = s.getsockname()[1]
        servers = []

        def late():  # the forward starts listening a second after its unit started
            time.sleep(1)
            srv = http.server.HTTPServer(('127.0.0.1', port), H)
            servers.append(srv)
            srv.serve_forever()
        threading.Thread(target=late, daemon=True).start()
        try:
            with mock.patch.object(setup, 'RUNNER_URL', 'http://example.invalid/runner.tgz'), \
                    mock.patch.object(setup, 'RUNNER_SHA256', hashlib.sha256(body).hexdigest()):
                setup._unpack(str(t / 'r'), str(t / 'cache' / 'runner.tgz'), port)
        finally:
            for srv in servers:
                srv.shutdown()
                srv.server_close()
        self.assertTrue(setup.runner_complete(str(t / 'r')))

    def test_a_forward_that_never_listens_fails_within_the_bound(self):
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            port = s.getsockname()[1]  # bound, never listening
            code = (f'import sys; sys.path.insert(0, {str(ROOT)!r}); from ci_runners import setup; '
                    f'setup.wait_port({port}, timeout=1, poll=0.2)')
            t0 = time.monotonic()
            r = subprocess.run([sys.executable, '-I', '-c', code], capture_output=True, text=True, timeout=20)
            self.assertLess(time.monotonic() - t0, 10)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('nothing listens on the egress port', r.stderr)

    def test_the_org_snapshot_timer_only_where_the_inventory_asks(self):
        text = setup.build(str(self.etc)).text()
        self.assertNotIn('ci-orgsync', text)
        inv = self.etc / 'inventory.toml'
        inv.write_text(inv.read_text().replace('max_temp_c = 80\n', 'max_temp_c = 80\norgsync = true\n'))
        plan = setup.build(str(self.etc))
        self.assertIn('ci-orgsync.service', plan.text())
        enable = [a['argv'] for a in plan.actions if a.get('kind') == 'run' and '--now' in a['argv']
                  and 'ci-reaper.timer' in a['argv']]
        self.assertEqual(enable, [['systemctl', '--user', 'enable', '--now', 'ci-reaper.timer', 'ci-orgsync.timer',
                                   'ci-envrefresh.timer', 'ci-hostload.timer']])
        self.assertEqual(setup.timers({}), ['ci-reaper.timer', 'ci-envrefresh.timer', 'ci-hostload.timer'])
        self.assertEqual(setup.timers({'orgsync': True}), ['ci-reaper.timer', 'ci-orgsync.timer', 'ci-envrefresh.timer',
                                                           'ci-hostload.timer'])

    def test_runner_names_and_labels(self):
        inv = inventory.load((self.etc / 'inventory.toml').read_text())
        self.assertNotIn('CI_RUNNER_POOL_LABELS', setup.env_file(inv, 'part', '/e'))  # its class is its label
        self.assertIsNone(inventory.pool_labels(inv, 'part'))
        self.assertEqual(setup.runners(inv), [('hb-light-1', ['light', 'hb'], 'light'),
                                              ('hb-part-1', ['part', 'hb'], 'part'),
                                              ('hb-part-2', ['part', 'hb'], 'part')])

    def test_render(self):
        self.assertEqual(setup.render('a=@X@\n# @NOTE@ in a comment\n', {'X': '1'}), 'a=1\n# @NOTE@ in a comment\n')
        with self.assertRaises(ValueError):
            setup.render('a=@Y@\n', {'X': '1'})

    def test_a_host_without_a_tunnel_has_a_plain_runner_unit(self):
        """The runner unit with the data root spelled out, and no order after an egress."""
        self.apply(setup.build(str(self.etc)).text())
        got = (self.home / '.config' / 'systemd' / 'user' / 'ci-runner@.service').read_text()
        d = self.data
        self.assertEqual([x for x in got.splitlines() if not x.startswith('#')], [
            '[Unit]', 'Description=ci-runners: GitHub Actions runner %i', '', '[Service]', f'WorkingDirectory={d}/actions-runner/%i',
            f'ExecStart={d}/actions-runner/%i/run.sh', 'NoNewPrivileges=yes', 'KillMode=mixed', 'KillSignal=SIGTERM',
            'TimeoutStopSec=5min', 'Restart=always', 'RestartSec=30', 'Slice=ci.slice', '', '[Install]',
            'WantedBy=default.target'])
        reaper = (self.home / '.config' / 'systemd' / 'user' / 'ci-reaper.service').read_text()
        self.assertIn(f'ExecStart={self.data}/ci-runners/releases/hook/bin/ci-run reap\n', reaper)
        self.assertIn(f'Environment=CI_RUNNERS_ETC={self.etc}\n', reaper)
        self.assertIn('Environment=CI_RUNNERS_PYTHON=/usr/bin/python3\n', reaper)


class Standalone(unittest.TestCase):
    """A standalone host of the RUN-5 model (decision 11.3, 11.6; h100's, while it ran): no
    CI proxy, its own interpreter, labelled pools and a runner per card, on an account
    whose unit directory may hold another user's ci-* unit."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = pathlib.Path(self.tmp.name)
        self.home, self.data = t / 'home', t.joinpath(*getattr(self, 'DATA', ('home', 'sub')))  # h100: data in the account's home
        self.etc = self.data / 'ci-runners' / 'etc'
        for d in (self.etc / 'projects', self.data / 'ci-runners' / 'releases'):
            d.mkdir(parents=True)
        os.symlink(ROOT, self.data / 'ci-runners' / 'releases' / 'hook')
        self.inv = self.etc / 'inventory.toml'
        self.inv.write_text(f'''schema = 1
this = "hx"
[hosts.hx]
account = "acct-x"
home = "{self.home}"
data = "{self.data}"
slots = 2
memory_max = "512G"
disk_min_gib = 50
gpus = [0, 3]
max_temp_c = 80
cpus = "0-43,88-131"
python = "{sys.executable}"
orgsync = true
files = {{ slot_lock = "/tmp/s.lock", slot_queue = "/tmp/s.queue" }}
[hosts.hx.pools]
light = {{ count = 1, labels = ["hx-light"] }}
part = {{ count = 1, labels = ["hx-cpu"] }}
cards = {{ per_card = true, class = "gpu", labels = ["hx-gpu"] }}
''')
        self.units = self.home / '.config' / 'systemd' / 'user'
        self.ran = []

    def tearDown(self):
        self.tmp.cleanup()

    def run_(self, argv, **_kw):
        self.ran.append(argv)

    def test_no_root_steps_without_local_accounts(self):
        plan = setup.build(str(self.etc))  # h100: its own host layer (§5.3), no drop-in, nothing blocked
        for word in ('root steps', 'ci-runners-private.conf', 'tmpfiles', 'disable --now', 'user@'):
            self.assertNotIn(word, plan.text())
        self.assertEqual(plan.blocked, '')

    def apply(self, text):
        f = pathlib.Path(self.tmp.name) / 'plan.txt'
        f.write_text(text)
        def unpack(path, _cache, port):
            os.makedirs(path, exist_ok=True)
            for f in setup.RUNNER_FILES:
                pathlib.Path(path, f).parent.mkdir(parents=True, exist_ok=True)
                pathlib.Path(path, f).write_text('#!/bin/sh\n')
            self.unpacked.append((path, port))
        with mock.patch.object(setup, '_unpack', unpack), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return setup.apply(str(self.etc), str(f), run=self.run_)

    def test_runners_labels_and_cards(self):
        inv = inventory.load(self.inv.read_text())
        self.assertEqual(setup.runners(inv), [('hx-light-1', ['hx-light'], 'light'), ('hx-part-1', ['hx-cpu'], 'part'),
                                              ('hx-gpu0', ['hx-gpu', 'hx-gpu0'], 'gpu'),
                                              ('hx-gpu3', ['hx-gpu', 'hx-gpu3'], 'gpu')])
        self.assertEqual(setup.env_file(inv, 'gpu', '/e', 3).splitlines(),
                         [f'ACTIONS_RUNNER_HOOK_JOB_STARTED={self.data}/ci-runners/hooks/job-started.sh',
                          f'ACTIONS_RUNNER_HOOK_JOB_COMPLETED={self.data}/ci-runners/hooks/job-completed.sh',
                          'CI_RUNNER_CLASS=gpu', 'CI_RUNNERS_ETC=/e', 'CI_RUNNER_POOL_LABELS=hx-gpu', 'CI_RUNNER_CARD=3',
                          f'CI_RUNNERS_PYTHON={sys.executable}', 'LANG=C.UTF-8'])
        self.assertIn('CI_RUNNER_CARD=0\n', setup.env_file(inv, 'gpu', '/e', 0))  # card 0 is a card
        self.assertNotIn('CI_RUNNER_CARD', setup.env_file(inv, 'part', '/e'))
        self.assertIn('CI_RUNNER_POOL_LABELS=hx-cpu\n', setup.env_file(inv, 'part', '/e'))
        self.assertEqual((inventory.pool_labels(inv, 'part'), inventory.pool_labels(inv, 'nope')), (['hx-cpu'], None))
        # two per-card pools cannot load (one pool per class); built by hand, setup still refuses them
        two = inventory.Inventory('hx', {'hx': {**inv.host, 'pools': {**inv.host['pools'],
                                                                     'more': {'per_card': True, 'class': 'gpu'}}}})
        with self.assertRaisesRegex(ValueError, 'same name'):
            setup.runners(two)  # both per-card pools would name their runners hx-gpu0, hx-gpu3

    def test_a_3090_host_s_runners(self):
        """Decision 12.5, design §5.1: the 3090 pools, each labelled with the host's own
        labels; the per-card pool's class is gpu, which its runners' .env names."""
        text = self.inv.read_text().replace('cards = { per_card = true, class = "gpu", labels = ["hx-gpu"] }',
                                            'cards = { per_card = true, class = "gpu", labels = ["hx-gpu"] }\n'
                                            'multi = { count = 1, labels = ["hx-multi"] }\n'
                                            'compile = { count = 1, labels = ["hx-compile"] }\n'
                                            'nightly = { count = 1, labels = ["hx-nightly"] }')
        inv = inventory.load(text)
        self.assertEqual(setup.runners(inv)[2:], [('hx-gpu0', ['hx-gpu', 'hx-gpu0'], 'gpu'),
                                                  ('hx-gpu3', ['hx-gpu', 'hx-gpu3'], 'gpu'),
                                                  ('hx-multi-1', ['hx-multi'], 'multi'),
                                                  ('hx-compile-1', ['hx-compile'], 'compile'),
                                                  ('hx-nightly-1', ['hx-nightly'], 'nightly')])
        env = setup.env_file(inv, 'gpu', '/e', 3)
        for line in ('CI_RUNNER_CLASS=gpu\n', 'CI_RUNNER_POOL_LABELS=hx-gpu\n', 'CI_RUNNER_CARD=3\n'):
            self.assertIn(line, env)
        self.assertIn('CI_RUNNER_POOL_LABELS=hx-multi\n', setup.env_file(inv, 'multi', '/e'))

    def test_plan_without_a_proxy(self):
        self.unpacked = []
        text = setup.build(str(self.etc)).text()
        for no in ('ci-proxy', '_proxy=', 'through the CI proxy', 'socks'):
            self.assertNotIn(no, text)
        for want in ('labels hx-light;', 'labels hx-cpu;', 'labels hx-gpu,hx-gpu0;', 'labels hx-gpu,hx-gpu3;',
                     '| CI_RUNNER_CARD=3', f'| CI_RUNNERS_PYTHON={sys.executable}', '| MemoryMax=512G',
                     'fetched directly',
                     'run    systemctl --user enable --now ci-reaper.timer ci-orgsync.timer ci-envrefresh.timer '
                     'ci-hostload.timer\n'):
            self.assertIn(want, text)
        self.assertEqual(self.apply(text), 0)
        self.assertEqual(sorted(p.name for p in self.units.iterdir()),
                         ['ci-envrefresh.service', 'ci-envrefresh.timer', 'ci-hostload.service', 'ci-hostload.timer',
                          'ci-orgsync.service', 'ci-orgsync.timer', 'ci-reaper.service', 'ci-reaper.timer',
                          'ci-runner@.service', 'ci.slice'])
        load = (self.units / 'ci-hostload.service').read_text()  # decision 13.2: the load record
        self.assertIn(f'ExecStart={self.data}/ci-runners/releases/hook/bin/ci-run hostload sample\n', load)
        self.assertIn('OnUnitActiveSec=60s\n', (self.units / 'ci-hostload.timer').read_text())
        orgsync = (self.units / 'ci-orgsync.service').read_text()
        self.assertIn(f'ExecStart={self.data}/ci-runners/releases/hook/bin/ci-run org-sync\n', orgsync)
        refresh = (self.units / 'ci-envrefresh.service').read_text()  # decision 12.11: every host refreshes
        self.assertIn(f'ExecStart={self.data}/ci-runners/releases/hook/bin/ci-run env refresh --all\n', refresh)
        self.assertIn(f'EnvironmentFile=-{self.etc}/envrefresh.env\n', refresh)
        self.assertNotIn('@', refresh)
        self.assertIn(f'Environment=CI_RUNNERS_PYTHON={sys.executable}\n', orgsync)
        hook = (self.data / 'ci-runners' / 'hooks' / 'job-started.sh').read_text()
        self.assertIn(f'\npy={sys.executable}\n', hook)  # written in: no job's environment picks it
        self.assertNotIn('CI_RUNNERS_PYTHON', hook)
        self.assertIn(f'\nbase={self.data}/ci-runners\n', hook)  # its release, wherever it is copied (13.5)
        self.assertFalse((self.units / 'ci-hooks.service').exists())  # no private /tmp: hooks stay under data
        env = (self.data / 'actions-runner' / 'hx-part-1' / '.env').read_text()
        self.assertIn(f'ACTIONS_RUNNER_HOOK_JOB_STARTED={self.data}/ci-runners/hooks/job-started.sh\n', env)
        runner = (self.units / 'ci-runner@.service').read_text()
        self.assertNotIn('ci-hooks', runner)
        self.assertNotIn('ci-proxy', runner)
        self.assertIn('Description=ci-runners: GitHub Actions runner %i\n\n[Service]', runner)
        self.assertIn(f'ExecStart={self.data}/actions-runner/%i/run.sh\n', runner)
        reaper = (self.units / 'ci-reaper.service').read_text()
        self.assertIn(f'Environment=CI_RUNNERS_PYTHON={sys.executable}\n', reaper)
        self.assertIn(f'Environment=CI_RUNNERS_ETC={self.etc}\n', reaper)
        self.assertEqual([port for _, port in self.unpacked], [0, 0, 0, 0])  # no proxy to wait for
        self.assertEqual(setup.build(str(self.etc)).actions, [])  # a second run is a no-op

    def test_another_users_unit_stops_the_plan(self):
        self.units.mkdir(parents=True)
        for name in ('ci-reaper.service', 'ci-runner@.service'):
            (self.units / name).write_text('[Service]\nExecStart=/bin/true\n')
            with self.assertRaisesRegex(ValueError, 'no apply of ci-runners wrote it'):
                setup.build(str(self.etc))
            (self.units / name).unlink()
        (self.units / 'other.service').write_text('x')  # not a name setup writes: left alone
        self.unpacked = []
        text = setup.build(str(self.etc)).text()
        self.assertEqual(self.apply(text), 0)
        self.assertEqual((self.units / 'other.service').read_text(), 'x')
        (self.units / 'ci-reaper.service').write_text('changed\n')  # ours: planned as an update
        self.assertIn(f'update {self.units}/ci-reaper.service', setup.build(str(self.etc)).text())
        (self.data / 'ci-runners' / 'state' / 'setup.json').write_text('{not json')
        with self.assertRaisesRegex(ValueError, 'unreadable'):
            setup.build(str(self.etc))

    def test_adopt_takes_over_unrecorded_units(self):
        """An older release wrote the units and kept no record: --adopt lists each and plans
        it as usual; the apply needs --adopt too, then records them, so uninstall removes
        them later. A link is never adopted."""
        self.units.mkdir(parents=True)
        reaper = self.units / 'ci-reaper.service'
        reaper.write_text('[Service]\nExecStart=/old/ci-run reap\n')
        os.chmod(reaper, 0o644)
        with self.assertRaisesRegex(ValueError, 'or plan with --adopt'):
            setup.build(str(self.etc))
        text = setup.build(str(self.etc), adopt=True).text()
        self.assertIn(f'  adopt  {reaper} (no apply recorded it; the lead names it ours)\n  update {reaper}', text)
        self.assertEqual(text.count('  adopt  '), 1)
        self.unpacked = []
        with self.assertRaisesRegex(ValueError, 'or plan with --adopt'):
            self.apply(text)  # applied without --adopt: refused
        self.assertIn('/old/', reaper.read_text())
        f = pathlib.Path(self.tmp.name) / 'adopt.txt'
        f.write_text(text)
        with mock.patch.object(setup, '_unpack', lambda path, cache, port: [
                pathlib.Path(path, x).parent.mkdir(parents=True, exist_ok=True) or pathlib.Path(path, x).write_text('')
                for x in setup.RUNNER_FILES]), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(setup.apply(str(self.etc), str(f), run=self.run_, adopt=True), 0)
        self.assertNotIn('/old/', reaper.read_text())
        self.assertIn(str(reaper), setup._ours(str(self.data / 'ci-runners' / 'state' / 'setup.json')))
        self.assertNotIn('adopt', setup.build(str(self.etc)).text())  # ours now: no flag needed
        # adopted as it is (already the planned content): the apply records it without writing
        (self.data / 'ci-runners' / 'state' / 'setup.json').unlink()  # the record lost again
        text = setup.build(str(self.etc), adopt=True).text()
        self.assertIn(f'  adopt  {reaper} (no apply recorded it; the lead names it ours)\n  keep   {reaper}', text)
        f.write_text(text)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(setup.apply(str(self.etc), str(f), run=self.run_, adopt=True), 0)
        self.assertIn(str(reaper), setup._ours(str(self.data / 'ci-runners' / 'state' / 'setup.json')))
        setup.build(str(self.etc))  # no flag needed now
        (self.data / 'ci-runners' / 'state' / 'setup.json').unlink()  # the record lost again
        link = self.units / 'ci-orgsync.timer'  # a name setup writes here (orgsync = true)
        link.unlink()
        os.symlink(reaper, link)
        with self.assertRaisesRegex(ValueError, 'cannot be adopted'):
            setup.build(str(self.etc), adopt=True)

    def test_the_adopt_flag_reaches_plan_and_apply(self):
        with mock.patch.object(setup, 'build', return_value=setup.Plan()) as b, contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()), mock.patch.dict(os.environ, {'CI_RUNNERS_ETC': '/e'}):
            setup.main(['plan', '--adopt'])
            setup.main(['plan'])
        self.assertEqual([c.kwargs.get('adopt') for c in b.call_args_list], [True, False])
        with mock.patch.object(setup, 'apply', return_value=0) as a, mock.patch.dict(os.environ, {'CI_RUNNERS_ETC': '/e'}):
            setup.main(['apply', '--plan', 'f', '--adopt'])
            setup.main(['apply', '--plan', 'f'])
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(setup.main(['apply', '--plan', 'f', '--other']), 2)
        self.assertEqual([c.kwargs.get('adopt') for c in a.call_args_list], [True, False])

    def test_the_python_must_be_3_11_or_later(self):
        old = pathlib.Path(self.tmp.name) / 'py310'
        old.write_text('#!/bin/sh\nexit 1\n')  # what a 3.10 answers to the version check
        old.chmod(0o755)
        for bad, why in ((str(old), '3.11 or later'), ('/nonexistent/python3', 'not an executable'),
                         ('python3', 'python is malformed')):
            self.inv.write_text(self.inv.read_text().replace(f'python = "{sys.executable}"', f'python = "{bad}"'))
            with self.assertRaisesRegex((ValueError, inventory.InventoryError), why, msg=bad):
                setup.build(str(self.etc))
            self.inv.write_text(self.inv.read_text().replace(f'python = "{bad}"', f'python = "{sys.executable}"'))
        self.assertIn('python', setup.build(str(self.etc)).text())

    def test_the_accounts_unit_directory_keeps_its_mode(self):
        """On a shared account the unit directory is not ours: an existing one is kept as is."""
        self.units.mkdir(parents=True, mode=0o700)
        os.chmod(self.units, 0o700)
        text = setup.build(str(self.etc)).text()
        self.assertIn(f'keep   {self.units}/', text)
        self.unpacked = []
        self.assertEqual(self.apply(text), 0)
        self.assertEqual(self.units.stat().st_mode & 0o777, 0o700)

    def test_etc_must_be_beside_the_data(self):
        """The units and the launcher find etc/ at <data>/ci-runners/etc: a plan for an etc
        elsewhere would install units that read another inventory."""
        other = pathlib.Path(self.tmp.name) / 'elsewhere' / 'etc'
        other.mkdir(parents=True)
        (other / 'inventory.toml').write_text(self.inv.read_text())
        with self.assertRaisesRegex(ValueError, 'the etc directory must be'):
            setup.build(str(other))

class Egress(unittest.TestCase):
    """h100's egress (decision 11.10): a tunnel table gives ci-tunnel, an ssh forward of the
    local http port to the egress proxy; the runners' proxy lines point at the forward;
    the lead's files in etc/tunnel/ are checked, never written; a recorded ci-bridge.service
    is removed."""

    TUNNEL = """[hosts.hx.tunnel]
http_port = 18182
egress_port = 18443
direct = ["localhost", "127.0.0.1", "::1"]
"""

    setUp_ = Standalone.setUp
    tearDown = Standalone.tearDown
    run_ = Standalone.run_
    apply_ = Standalone.apply

    def apply(self, text):
        with mock.patch.object(setup, 'wait_port', self.waited.append):
            return self.apply_(text)

    def setUp(self):
        self.setUp_()
        self.inv.write_text(self.inv.read_text().replace('orgsync = true\n', '').replace(
            '[hosts.hx.pools]', self.TUNNEL + '[hosts.hx.pools]'))
        self.waited = []
        self.tun = self.etc / 'tunnel'
        self.tun.mkdir(mode=0o700)
        os.chmod(self.etc, 0o700)
        for name in setup.TUNNEL_FILES:
            (self.tun / name).write_text('Host egress\n  HostName egress.example\n' if name == 'ssh_config' else 'x\n')
            os.chmod(self.tun / name, 0o600)

    def test_plan_with_a_tunnel(self):
        self.unpacked = []
        text = setup.build(str(self.etc)).text()
        for want in ('| https_proxy=http://127.0.0.1:18182', '| http_proxy=http://127.0.0.1:18182',
                     '| no_proxy=localhost,127.0.0.1,::1', 'fetched through the egress tunnel',
                     f'keep   {self.tun}/id_ed25519',
                     'forward 127.0.0.1:18182 -> the egress proxy, 127.0.0.1:18443 there; direct: localhost, 127.0.0.1, ::1',
                     'run    systemctl --user enable --now ci-tunnel.service\n',
                     '    restrict,port-forwarding,permitopen="127.0.0.1:18443",permitlisten="127.0.0.1:9",'
                     'command="/usr/sbin/nologin" <the public key of etc/tunnel/id_ed25519>\n'):
            self.assertIn(want, text)
        self.assertNotIn('ci-proxy', text)
        self.assertNotIn('ci-bridge', text)
        self.assertNotIn('  remove ', text)  # nothing recorded: nothing to remove
        self.assertNotIn('egress.example', text)  # the lead's files are not shown
        self.assertEqual(self.apply(text), 0)
        self.assertEqual(sorted(p.name for p in self.units.iterdir()),
                         ['ci-envrefresh.service', 'ci-envrefresh.timer', 'ci-hostload.service', 'ci-hostload.timer',
                          'ci-reaper.service', 'ci-reaper.timer', 'ci-runner@.service', 'ci-tunnel.service', 'ci.slice'])
        tunnel = (self.units / 'ci-tunnel.service').read_text()
        for want in (f'ExecStart=/usr/bin/ssh -F {self.etc}/tunnel/ssh_config -N -L 127.0.0.1:18182:127.0.0.1:18443 ',
                     f'-o IdentityFile={self.etc}/tunnel/id_ed25519 ', '-o StrictHostKeyChecking=yes ',
                     f'-o UserKnownHostsFile={self.etc}/tunnel/known_hosts ', '-o ExitOnForwardFailure=yes ',
                     '-o BatchMode=yes ', '-o GatewayPorts=no ', '-o IdentityAgent=none ', ' egress\n'):
            self.assertIn(want, tunnel)
        self.assertNotIn(' -D ', tunnel)
        runner = (self.units / 'ci-runner@.service').read_text()
        self.assertIn('After=ci-tunnel.service\nWants=ci-tunnel.service\n', runner)
        self.assertEqual([port for _, port in self.unpacked], [18182] * 4)  # the runner app through the forward
        self.assertEqual(self.waited, [18182])  # the forward's port: it binds once ssh is in
        self.assertEqual((self.tun / 'ssh_config').read_text(), 'Host egress\n  HostName egress.example\n')
        self.assertEqual(setup.build(str(self.etc)).actions, [])
        self.assertNotIn('systemctl --user restart', text)  # a first apply starts it

    def test_a_changed_port_restarts_the_tunnel(self):
        self.unpacked = []
        self.assertEqual(self.apply(setup.build(str(self.etc)).text()), 0)
        self.inv.write_text(self.inv.read_text().replace('egress_port = 18443', 'egress_port = 18444'))
        text = setup.build(str(self.etc)).text()
        self.assertIn(f'update {self.units}/ci-tunnel.service', text)
        self.assertIn('run    systemctl --user restart ci-tunnel.service\n', text)
        self.assertNotIn('  wait ', text)  # the runners are there: nothing to fetch

    def recorded_bridge(self):
        """ci-bridge.service as an earlier release's apply left it, on record."""
        self.unpacked = []
        self.assertEqual(self.apply(setup.build(str(self.etc)).text()), 0)
        bridge = self.units / 'ci-bridge.service'
        bridge.write_text('[Service]\nExecStart=/old/ci-run bridge\n')
        state = self.data / 'ci-runners' / 'state' / 'setup.json'
        doc = json.loads(state.read_text())
        doc['applied'].insert(0, {'plan': 'old', 'made': [{'kind': 'file', 'path': str(bridge)}]})
        state.write_text(json.dumps(doc))
        return bridge, state

    def test_a_recorded_unit_the_plan_no_longer_holds_is_removed(self):
        bridge, state = self.recorded_bridge()
        text = setup.build(str(self.etc)).text()
        self.assertIn(f'  remove {bridge} (disable --now, then delete)\n', text)
        self.assertIn('run    systemctl --user daemon-reload', text)
        self.ran.clear()
        self.assertEqual(self.apply(text), 0)
        self.assertFalse(bridge.exists())
        self.assertEqual(self.ran[0], ['systemctl', '--user', 'disable', '--now', 'ci-bridge.service'])
        self.assertIn(['systemctl', '--user', 'daemon-reload'], self.ran[1:])  # after the removal
        self.assertNotIn(str(bridge), setup._ours(str(state)))  # off the record
        self.assertIn({'kind': 'remove', 'path': str(bridge)}, json.loads(state.read_text())['applied'][-1]['made'])
        self.assertEqual(setup.build(str(self.etc)).actions, [])  # a second apply is a no-op

    def test_a_recorded_unit_already_gone_is_only_forgotten(self):
        bridge, state = self.recorded_bridge()
        bridge.unlink()
        text = setup.build(str(self.etc)).text()
        self.assertIn(f'  forget {bridge} (already gone)\n', text)
        self.ran.clear()
        self.assertEqual(self.apply(text), 0)
        self.assertNotIn(['systemctl', '--user', 'disable', '--now', 'ci-bridge.service'], self.ran)
        # but its unit may run on from memory, holding its port: stopped and disabled anyway
        self.assertIn(['systemctl', '--user', 'stop', 'ci-bridge.service'], self.ran)
        self.assertIn(['systemctl', '--user', 'disable', 'ci-bridge.service'], self.ran)
        self.assertNotIn(str(bridge), setup._ours(str(state)))

    def test_a_link_in_place_of_a_recorded_unit_is_not_removed(self):
        bridge, _ = self.recorded_bridge()
        target = pathlib.Path(self.tmp.name) / 'elsewhere.service'
        target.write_text('kept\n')
        bridge.unlink()
        os.symlink(target, bridge)
        with self.assertRaisesRegex(ValueError, 'not a regular file of this account'):
            setup.build(str(self.etc))
        with self.assertRaisesRegex(RuntimeError, 'not a regular file'):  # checked again at apply time
            setup._remove_unit(str(bridge), self.run_)
        self.assertTrue(bridge.is_symlink())
        self.assertEqual(target.read_text(), 'kept\n')

    def test_uninstall_skips_a_removed_unit(self):
        bridge, state = self.recorded_bridge()
        self.assertEqual(self.apply(setup.build(str(self.etc)).text()), 0)
        script = (ROOT / 'setup' / 'uninstall.sh').read_text()
        code = script.split("units=$(\"$py\" -I - \"$base/state/setup.json\" \"$unit_dir\" <<'PY'\n", 1)[1].split('\nPY\n', 1)[0]
        out = subprocess.run([sys.executable, '-I', '-', str(state), str(self.units)], input=code, text=True,
                             capture_output=True, check=True).stdout.split()
        self.assertNotIn('ci-bridge.service', out)
        self.assertIn('ci-tunnel.service', out)

    def test_the_leads_files_are_required(self):
        def refused(why):
            with self.assertRaisesRegex(ValueError, why):
                setup.build(str(self.etc))
        os.chmod(self.tun / 'id_ed25519', 0o644)
        refused('readable by others')
        os.chmod(self.tun / 'id_ed25519', 0o600)
        os.chmod(self.tun, 0o755)
        refused('mode 700')
        os.chmod(self.tun, 0o700)
        (self.tun / 'known_hosts').unlink()
        refused('known_hosts must be a regular file')
        os.symlink(self.tun / 'ssh_config', self.tun / 'known_hosts')  # a link to a regular file
        refused('known_hosts must be a regular file')
        (self.tun / 'known_hosts').unlink()
        (self.tun / 'known_hosts').write_text('x\n')
        for cfg, why in (('Host other\n  HostName h\n', 'line 1: only Host egress'),
                         ('Host egress\n', 'with its HostName'),
                         ('Host egress\n  HostName h\nHost egress\n  HostName i\n', 'one "Host egress"'),
                         ('Host egress\n  HostName h\n  IdentityFile /tmp/k\n', 'line 3: only'),
                         ('Host egress\n  HostName h\n  DynamicForward 0.0.0.0:1080\n', 'line 3: only'),
                         ('Host egress\n  HostName h\n  LocalForward 0.0.0.0:1080 x:1\n', 'line 3: only'),
                         ('Host egress\n  HostName h\n  ProxyCommand sh -c x\n', 'line 3: only'),
                         ('Host egress\n  HostName h; x\n', 'line 2: only'),
                         ('Match all\n  HostName h\n', 'line 1: only')):
            (self.tun / 'ssh_config').write_text(cfg)
            refused(why)
        (self.tun / 'ssh_config').write_text('# the egress host\nHost egress\n  HostName h.example\n  User u\n  Port 22\n')
        setup.build(str(self.etc))
        real = self.tun / 'real_config'
        (self.tun / 'ssh_config').rename(real)
        os.symlink(real, self.tun / 'ssh_config')
        refused('ssh_config must be a regular file')

    def test_the_tunnel_table(self):
        good = self.inv.read_text()
        for old, new, why in (('egress_port = 18443\n', '', 'egress_port is missing'),
                              ('http_port = 18182\n', '', 'http_port is missing'),
                              ('egress_port = 18443', 'egress_port = 0', 'egress_port is malformed'),
                              ('http_port = 18182', 'http_port = 70000', 'http_port is malformed'),
                              ('http_port = 18182', 'http_port = 18182\nsocks_port = 18181', r"unknown keys \['socks_port'\]"),
                              ('http_port = 18182', 'http_port = 18182\nallow = ["github.com"]', r"unknown keys \['allow'\]"),
                              ('"::1"]', '"::1,example.com"]', 'direct is malformed'),
                              ('"::1"]', '"::1", "a.org,b.org"]', 'direct is malformed')):  # one name each
            with self.assertRaisesRegex(inventory.InventoryError, why, msg=new):
                inventory.load(good.replace(old, new))
        self.assertEqual(inventory.load(good).host['tunnel'],
                         {'http_port': 18182, 'egress_port': 18443, 'direct': ['localhost', '127.0.0.1', '::1']})
        public = inventory.tomllib.loads(good.replace('"::1"]', '"::1", "api.github.com", ".blob.core.windows.net"]'))
        self.assertEqual(inventory.deny_tokens(public) & {'api.github.com', '.blob.core.windows.net'},
                         set())  # public names: the repository may hold them
        private = inventory.tomllib.loads(good.replace('"::1"]', '"::1", "mirror.lab.internal", "fd00::5"]'))
        self.assertLessEqual({'mirror.lab.internal', 'fd00::5'}, inventory.deny_tokens(private))  # inventory
        self.assertIn('secret-name.org', inventory.deny_tokens(
            {'hosts': {'hx': {'tunnel': {'allow': ['secret-name.org']}}}}))  # no tunnel key is exempt but direct's
        self.assertEqual(inventory.deny_tokens({'org': {'name': 'chaobrain'}, 'x': {'name': 'secretorg'}}),
                         {'secretorg'})  # the policy's org name is public; another name is not
        self.assertIn('api.github.com', inventory.deny_tokens({'hosts': {'hx': {'other': ['api.github.com']}}}))

    def test_the_egress_command(self):
        from ci_runners import cli
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(['egress', '--port', '18443', '--allow', str(self.tmp.name) + '/absent']), 2)
            self.assertEqual(cli.main(['bridge']), 2)  # gone
        self.assertIn('ci-run egress:', err.getvalue())

    def test_the_egress_unit(self):
        unit = (ROOT / 'setup' / 'egress' / 'ci-egress.service').read_text()
        for want in ('DynamicUser=yes\n', 'ExecStart=/opt/ci-runners/releases/hook/bin/ci-run egress --port 18443 '
                     '--allow /opt/ci-runners/etc/egress-allow\n', 'Environment=CI_RUNNERS_PYTHON=/usr/bin/python3\n',
                     'NoNewPrivileges=yes\n', 'ProtectSystem=strict\n', 'ProtectHome=yes\n', 'PrivateTmp=yes\n',
                     'RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX\n', 'CapabilityBoundingSet=\n',
                     'MemoryMax=512M\n', 'Restart=always\n'):
            self.assertIn(want, unit)
        example = (ROOT / 'setup' / 'inventory.example.toml').read_text()
        self.assertIn('egress_port = 18443', example)  # the unit's port


KEY = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIObYmbZUCCMvoLLQijA8VtgBpzN1Or0HIo1rYVmRJ8l7'


class Shared(unittest.TestCase):
    """A 3090 host (decisions 12.3, 12.4, 12.9; §5.3): the root steps that share brainspike's
    host lock and slot files with the local accounts, the push key's line, and the egress
    account's line for the tunnel key."""

    setUp_ = Standalone.setUp
    DATA = ('disk', 'ci')  # data on a data disk, out of the home (decision 12.16)
    run_ = Standalone.run_
    apply_ = Standalone.apply

    def setUp(self):
        self.setUp_()
        self.unpacked = []
        # shared files are plain names directly in /tmp (B1): unique ones, removed after
        self.p = s = f'/tmp/cirt{uuid.uuid4().hex[:12]}'
        self.inv.write_text(self.inv.read_text().replace(
            'files = { slot_lock = "/tmp/s.lock", slot_queue = "/tmp/s.queue" }',
            f'local_accounts = ["localuser"]\nfiles = {{ host_lock = "{s}-host.lock", slot_lock = "{s}-ci.lock", '
            f'slot_queue = "{s}-ci.queue" }}'))
        self.h = inventory.load(self.inv.read_text()).host
        disk, mounted = str(self.data.parent), os.path.ismount  # the fixture's data disk is a mount point
        patcher = mock.patch.object(setup.os.path, 'ismount', lambda p: p == disk or mounted(p))
        patcher.start()
        self.addCleanup(patcher.stop)
        # the fixture's data disk lies in /tmp: not a system directory here, but in its own test
        patcher = mock.patch.object(setup, 'SYSTEM_DIRS', tuple(d for d in SYSTEM_DIRS if d != '/tmp'))
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        for f in glob.glob(self.p + '*'):
            (shutil.rmtree if os.path.isdir(f) and not os.path.islink(f) else os.unlink)(f)
        self.tmp.cleanup()

    def apply(self, text):
        return self.apply_(text)

    def test_the_tmpfiles_d_file(self):
        s = self.p
        rw, rwx = 'u:acct-x:rw,u:localuser:rw,m::rw', 'u:acct-x:rwx,u:localuser:rwx,m::rwx'
        want = ['# ci-runners (decision 12.9): the host lock and slot files that the CI account shares',
                '# with the local accounts, root-owned in /tmp, recreated at boot, never age-cleaned.']
        files = [f'{s}-host.lock', f'{s}-host.turnstile.lock', f'{s}-host.shared.lock', f'{s}-host.probe.lock',
                 f'{s}-ci.lock', f'{s}-ci.2.lock']
        for f in files:
            want += [f'f {f} 0660 root root -', f'a+ {f} - - - - {rw}']
        want += [f'd {s}-ci.queue 0770 root root -',
                 f'A+ {s}-ci.queue - - - - {rwx},d:u::rwx,d:g::---,d:o::---,d:u:acct-x:rwx,d:u:localuser:rwx,d:m::rwx']
        want += [f'x {f}' for f in [*files, f'{s}-ci.queue']]
        self.assertEqual(setup.tmpfiles_text(self.h).splitlines(), want)
        for bad in ({'local_accounts': ['a b']}, *({'files': {**self.h['files'], 'slot_queue': q}} for q in
                    ('/tmp/a b', '/tmp/../etc/shadow', '/etc/shadow', '/tmp/x/../q', '/tmp//q', '/tmp/./q',
                     '/ho' + 'me/someone/q', '/tmp/', '/tmp/.q'))):
            with self.assertRaises(ValueError, msg=bad):
                setup.tmpfiles_text({**self.h, **bad})

    def test_a_lease_dir_is_shared_as_the_queue(self):
        """Decision 15.1: the inventory's lease_dir is a directory the root step makes as the
        slot queue (default ACL entries), binds into the CI account's namespace and checks."""
        s = self.p
        h = {**self.h, 'files': {**self.h['files'], 'lease_dir': f'{s}-leases'}}
        self.assertEqual(setup.shared_files(h)[1], [f'{s}-ci.queue', f'{s}-leases'])
        text = setup.tmpfiles_text(h).splitlines()
        self.assertIn(f'd {s}-leases 0770 root root -', text)
        self.assertIn(f'x {s}-leases', text)
        self.assertTrue(any(line.startswith(f'A+ {s}-leases - - - - u:acct-x:rwx') and 'd:u:localuser:rwx' in line
                            for line in text))
        self.assertTrue(setup.private_text(h).rstrip().endswith(f'{s}-ci.queue {s}-leases'))
        self.assertIn((f'{s}-leases', 'missing'), setup.shared_state(h, uid_of=lambda a: 0, read='/nonexistent'))
        self.assertEqual(setup.shared_files(self.h)[1], [f'{s}-ci.queue'])  # none named: as before
        # its queue (issue #125): checked, but made by the parts, so neither made nor bound by the root step
        self.assertEqual(setup.shared_files(h)[2], [f'{s}-leases/queue'])
        self.assertEqual(setup.shared_files(self.h)[2], [])
        self.assertNotIn('queue', setup.tmpfiles_text(h).replace(f'{s}-ci.queue', ''))
        self.assertNotIn('queue', setup.private_text(h).replace(f'{s}-ci.queue', ''))
        self.assertNotIn(f'{s}-leases/queue', [p for p, _ in setup.shared_state(h, uid_of=lambda a: 0,
                                                                                    read='/nonexistent')])

    def test_root_steps_not_done_block_apply(self):
        plan = setup.build(str(self.etc))
        text = plan.text()
        self.assertIn('root steps (decision 12.9, §5.3): the lead runs them as root, before apply; setup cannot:', text)
        self.assertIn(f'to do  write {setup.TMPFILES} mode 644 owner root', text)
        self.assertIn(f'| f {self.p}-host.turnstile.lock 0660 root root -', text)
        self.assertIn(f'{self.p}-ci.2.lock: missing', text)
        self.assertIn('apply refuses until they are done', text)
        self.assertEqual(plan.blocked, 'the root steps are not done')
        self.assertEqual(self.apply(text), 1)
        self.assertFalse((self.data / 'ci-runners' / 'state' / 'setup.json').exists())  # nothing made
        drop = setup.DROPIN.format(uid='<uid>')
        names = 'ci-runner@hx-light-1.service ci-runner@hx-part-1.service ci-runner@hx-gpu0.service ci-runner@hx-gpu3.service'
        steps = [f'  to do  write {drop} mode 644 owner root',
                 f'  to do  as the account, first: systemctl --user disable --now {names}  (the restart starts every '
                 'enabled runner, and a reboot before the drop-in would start them without it)',
                 '  to do  run    systemctl daemon-reload; systemctl restart user@<uid>.service',
                 '  to do  as the account, then: systemctl --user start ci-runner@hx-light-1.service  (only it, for '
                 'the leak check in a live job, T18; run apply only after that check has passed in a job on it: apply '
                 'enables every runner, and the next reboot starts them all)']
        at = [next(i for i, line in enumerate(text.splitlines()) if line.startswith(step)) for step in steps]
        self.assertEqual(at, sorted(at))  # in this order: disabled before the restart, one started after it
        self.assertIn('         | TemporaryFileSystem=/dev/shm:mode=1777 /home ' + str(self.data.parent), text)
        self.assertIn('acct-x: no such account', text)
        with mock.patch.object(setup, 'shared_state', lambda h: []):  # the tmpfiles steps done, not the drop-in
            plan = setup.build(str(self.etc))
            self.assertEqual(plan.blocked, 'the root steps are not done')
            self.assertEqual(self.apply(plan.text()), 1)
            with mock.patch.object(setup, 'account_uid', lambda a: 4242), \
                    mock.patch.object(setup, 'private_state',
                                      lambda h, uid: [(setup.DROPIN.format(uid=4242), 'missing')]):
                text = setup.build(str(self.etc)).text()  # the account known, the drop-in not written
                self.assertIn(f'to do  write {setup.DROPIN.format(uid=4242)}', text)
                self.assertEqual(self.apply(text), 1)
            with mock.patch.object(setup, 'account_uid', lambda a: 4242), \
                    mock.patch.object(setup, 'private_state', lambda h, uid: [('user@4242.service', 'runs without')]):
                text = setup.build(str(self.etc)).text()  # the drop-in written, the manager not restarted
                self.assertIn(f'done   write {setup.DROPIN.format(uid=4242)}', text)
                self.assertIn('to do  run    systemctl daemon-reload; systemctl restart user@4242.service', text)
                self.assertIn('apply refuses', text)
                self.assertEqual(self.apply(text), 1)
            self.assertFalse((self.data / 'ci-runners' / 'state' / 'setup.json').exists())
            with mock.patch.object(setup, 'account_uid', lambda a: 4242), \
                    mock.patch.object(setup, 'private_state', lambda h, uid: []):  # done: the plan goes ahead
                text = setup.build(str(self.etc)).text()
                self.assertIn(f'done   write {setup.TMPFILES}', text)
                self.assertIn('done   as the account, first: systemctl --user disable --now', text)
                self.assertIn('done   run    systemctl daemon-reload; systemctl restart user@4242.service', text)
                self.assertIn('done   as the account, then: systemctl --user start', text)
                self.assertNotIn('apply refuses', text)
                self.assertEqual(self.apply(text), 0)

    def test_apply_enables_the_runners_the_root_step_disabled(self):
        wants = self.home / '.config' / 'systemd' / 'user' / 'default.target.wants'
        names = ['hx-light-1', 'hx-part-1', 'hx-gpu0', 'hx-gpu3']
        enable = ['systemctl', '--user', 'enable', *(f'ci-runner@{n}.service' for n in names)]
        with mock.patch.object(setup, 'shared_state', lambda h: []), \
                mock.patch.object(setup, 'account_uid', lambda a: 4242), \
                mock.patch.object(setup, 'private_state', lambda h, uid: []):
            self.assertEqual(self.apply(setup.build(str(self.etc)).text()), 0)
            plan = setup.build(str(self.etc))  # all written, but no runner enabled (the mock ran nothing)
            self.assertIn(enable, [a.get('argv') for a in plan.actions])
            wants.mkdir(parents=True)
            for n in names:
                (wants / f'ci-runner@{n}.service').symlink_to('../ci-runner@.service')
            self.assertEqual(setup.build(str(self.etc)).actions, [])  # all enabled: a no-op
            (wants / 'ci-runner@hx-gpu0.service').unlink()  # disabled by the root step
            plan = setup.build(str(self.etc))
            self.assertIn(enable, [a.get('argv') for a in plan.actions])
            self.assertEqual(self.apply(plan.text()), 0)
            self.assertIn(enable, self.ran)

    def test_the_runners_run_the_hooks_copies(self):
        """Decision 13.5: on a host with the private namespace the runners' .env names the
        copies in /tmp, each runner requires ci-hooks.service and checks both copies before it
        starts, and apply starts the copies before it enables the runners; a changed hook is
        copied again by a reload, never a restart (which would restart every runner)."""
        with mock.patch.object(setup, 'shared_state', lambda h: []), \
                mock.patch.object(setup, 'account_uid', lambda a: 4242), \
                mock.patch.object(setup, 'private_state', lambda h, uid: []):
            plan = setup.build(str(self.etc))
            argv = [a['argv'] for a in plan.actions if a['kind'] == 'run']
            start = ['systemctl', '--user', 'enable', '--now', 'ci-hooks.service']
            reload = ['systemctl', '--user', 'reload', 'ci-hooks.service']
            enable = next(i for i, x in enumerate(argv) if x[:3] == ['systemctl', '--user', 'enable']
                          and x[3].startswith('ci-runner@'))
            self.assertLess(argv.index(start), argv.index(reload))
            self.assertLess(argv.index(reload), enable)  # the copies before the runners
            self.assertNotIn(['systemctl', '--user', 'restart', 'ci-hooks.service'], argv)
            self.assertEqual(self.apply(plan.text()), 0)
            hooks = (self.units / 'ci-hooks.service').read_text()
            self.assertIn(f'ExecStart={self.data}/ci-runners/releases/hook/bin/ci-run hooks copy\n', hooks)
            self.assertIn(f'ExecReload={self.data}/ci-runners/releases/hook/bin/ci-run hooks copy\n', hooks)
            self.assertIn('RemainAfterExit=yes\n', hooks)
            runner = (self.units / 'ci-runner@.service').read_text()
            self.assertIn('[Unit]\nDescription=ci-runners: GitHub Actions runner %i\n'
                          'Requires=ci-hooks.service\nAfter=ci-hooks.service\n', runner)
            for n in ('job-started.sh', 'job-completed.sh'):
                self.assertIn(f'\nExecStartPre=/usr/bin/test -f /tmp/ci-runners-hooks/{n} -a -x '
                              f'/tmp/ci-runners-hooks/{n}\n', runner)
            env = (self.data / 'actions-runner' / 'hx-part-1' / '.env').read_text()
            self.assertIn('ACTIONS_RUNNER_HOOK_JOB_STARTED=/tmp/ci-runners-hooks/job-started.sh\n'
                          'ACTIONS_RUNNER_HOOK_JOB_COMPLETED=/tmp/ci-runners-hooks/job-completed.sh\n', env)
            hook = (self.data / 'ci-runners' / 'hooks' / 'job-started.sh').read_text()
            self.assertIn(f'\nbase={self.data}/ci-runners\n', hook)
            argv = [a['argv'] for a in setup.build(str(self.etc)).actions if a['kind'] == 'run']
            self.assertNotIn(reload, argv)  # the hooks unchanged: no copy again
            (self.data / 'ci-runners' / 'hooks' / 'job-started.sh').write_text('#!/bin/sh\nexit 1\n')
            argv = [a['argv'] for a in setup.build(str(self.etc)).actions if a['kind'] == 'run']
            self.assertIn(reload, argv)

    def test_the_private_drop_in(self):
        s, home, data = self.p, str(self.home), str(self.data)
        shared = ' '.join(f'{s}{x}' for x in ('-host.lock', '-host.turnstile.lock', '-host.shared.lock',
                                              '-host.probe.lock', '-ci.lock', '-ci.2.lock', '-ci.queue'))
        self.assertEqual(setup.private_text(self.h).splitlines(),
                         ["# ci-runners (decision 12.16): the CI account sees no other account's files",
                          '[Service]', 'PrivateTmp=yes', 'ProtectProc=invisible',
                          f'TemporaryFileSystem=/dev/shm:mode=1777 /home {os.path.dirname(data)}',
                          f'BindPaths={home} {data} {shared}'])
        hm = '/ho' + 'me'  # (no home path in a tracked file, design §3.3)
        got = setup.private_text({**self.h, 'home': f'{hm}/acct-x', 'data': f'{hm}/disk2/acct-x'})
        self.assertIn(f'TemporaryFileSystem=/dev/shm:mode=1777 /home {hm}/disk2\n', got)
        got = setup.private_text({**self.h, 'home': f'{hm}/acct-x', 'data': f'{hm}/ci-data'})
        self.assertIn('TemporaryFileSystem=/dev/shm:mode=1777 /home\n', got)  # /home once
        got = setup.private_text({**self.h, 'home': '/d/a', 'data': '/d/ab/x'})  # a root that only shares a prefix
        self.assertIn('TemporaryFileSystem=/dev/shm:mode=1777 /home /d/ab\n', got)
        for bad in ({'data': '/ci'},  # the data disk root would be /
                    {'home': '/home'}, {'home': '/'}, {'home': '//home'},  # bound back over the emptied /home
                    {'data': '//ci'}, {'data': '//d/ci'},  # // is not / to normpath, but is to the kernel
                    {'home': '/d/a', 'data': '/d/a/data'}, {'home': '/d', 'data': '/d/x/data'},  # its root in home
                    {'home': f'{hm}/x/../y'}, {'data': data + '/'}, {'data': '/d//x'}, {'data': '/d e/x'},
                    {'files': {**self.h['files'], 'slot_queue': '/etc/q'}}):
            with self.assertRaises(ValueError, msg=bad):
                setup.private_text({**self.h, **bad})

    def test_the_private_paths_on_the_host(self):
        t = pathlib.Path(self.tmp.name)
        setup.check_private_paths(self.h)  # real paths, the data disk a mount point (the fixture's)
        (t / 'link').symlink_to(self.home)
        (t / 'disklink').symlink_to(self.data.parent)
        (t / 'other' / 'ci').mkdir(parents=True)
        for bad, why in (({'home': str(t / 'link')}, 'home .* is not its real path'),
                         ({'data': str(t / 'disklink' / 'ci')}, 'data .* is not its real path'),
                         ({'data': str(t / 'other' / 'ci')}, 'must be a mount point'),  # not a disk
                         ({'data': '/usr/ci'}, 'is a system directory'), ({'data': '/etc/ci'}, 'is a system directory')):
            with self.assertRaisesRegex(ValueError, why, msg=bad):
                setup.check_private_paths({**self.h, **bad})
        with mock.patch.object(setup, 'SYSTEM_DIRS', SYSTEM_DIRS), \
                mock.patch.object(setup.os.path, 'ismount', lambda p: True):  # each a separate file system
            for root in ('/', '/run', '/dev', '/proc', '/sys', '/tmp', '/var/tmp', '/usr', '/etc', '/boot', '/run/user',
                         '/usr/local', '/tmp/x', str(self.data.parent)):
                data = '/ci' if root == '/' else root + '/ci'
                with self.assertRaisesRegex(ValueError, 'is a system directory', msg=root):
                    setup.check_private_paths({**self.h, 'home': '/srv/h', 'data': data})
            setup.check_private_paths({**self.h, 'home': '/srv/h', 'data': '/data/ci'})  # a disk: passes
            setup.check_private_paths({**self.h, 'home': '/srv/h', 'data': '/runner-disk/ci'})  # only a prefix of /run
        self.inv.write_text(self.inv.read_text().replace(f'home = "{self.home}"', f'home = "{t / "link"}"'))
        with self.assertRaisesRegex(ValueError, 'home .* is not its real path'):  # the plan refuses it
            setup.build(str(self.etc))

    def namespace(self):
        """A user manager's mount table with the drop-in as planned (the fixture's paths),
        and the host's own mounts it shadows."""
        hm, root, home, data = '/ho' + 'me', str(self.data.parent), str(self.home), str(self.data)
        priv = '/tmp/systemd-private-0a1b-user@4242.service-xyz/tmp'
        files, (queue,), _ = setup.shared_files(self.h)
        return ['22 1 250:2 / / rw - ext4 /dev/x rw',
                '23 22 0:5 / /proc rw - proc proc rw',
                '25 22 0:24 / /dev/shm rw - tmpfs tmpfs rw',
                f'26 22 250:3 / {root} rw - ext4 /dev/y rw',  # the host's data disk, shadowed
                f'27 22 250:2 /x {hm}/someone rw - ext4 /dev/x rw',  # another account's mount, shadowed
                '50 23 0:60 / /proc rw - proc proc rw,nosuid,hidepid=invisible',
                '51 25 0:50 / /dev/shm rw - tmpfs tmpfs rw',
                '52 22 0:51 / /home rw - tmpfs tmpfs rw',
                f'53 22 250:2 {priv} /tmp rw - ext4 /dev/x rw',
                f'54 22 250:2 /var{priv} /var/tmp rw - ext4 /dev/x rw',
                f'55 53 0:52 / {root} rw - tmpfs tmpfs rw',
                f'56 53 250:2 {home} {home} rw - ext4 /dev/x rw',
                f'57 55 250:3 /ci {data} rw - ext4 /dev/y rw',
                *(f'{58 + k} 53 250:2 {f} {f} rw - ext4 /dev/x rw' for k, f in enumerate([*files, queue]))]

    def test_the_namespace_matches_the_drop_in_exactly(self):
        hm, root, home, data = '/ho' + 'me', str(self.data.parent), str(self.home), str(self.data)
        files, (queue,), _ = setup.shared_files(self.h)
        ns = self.namespace()
        SHM = "/dev/shm is the host's (or this plan runs in the namespace)"

        def faults(lines, here=ns[:5]):  # the plan's own table: the host's
            return setup.namespace_faults(self.h, '\n'.join(lines) + '\n', '\n'.join(here) + '\n')

        def without(mount_id):
            return [line for line in ns if not line.startswith(f'{mount_id} ')]
        self.assertEqual(faults(ns), [])
        self.assertEqual(faults(ns + [f'72 57 0:9 / {data}/sub rw - tmpfs tmpfs rw']), [])  # the account's own
        self.assertEqual(faults(ns[:5]), ['no tmpfs at /home', f'no tmpfs at {root}', SHM,
                                          '/tmp not private', '/var/tmp not private', 'other processes visible',
                                          *(f'no bind at {p}' for p in [home, data, *files, queue]),
                                          f'another mount at {hm}/someone'])
        for mount_id, fault in ((50, 'other processes visible'), (51, SHM),
                                (52, 'no tmpfs at /home'), (52, f'another mount at {hm}/someone'),
                                (53, '/tmp not private'), (54, '/var/tmp not private'), (55, f'no tmpfs at {root}'),
                                (55, f'no bind at {data}'), (56, f'no bind at {home}'), (58, f'no bind at {files[0]}'),
                                (58 + len(files), f'no bind at {queue}')):
            self.assertIn(fault, faults(without(mount_id)), msg=mount_id)
        self.assertEqual(faults([line.replace('rw,nosuid,hidepid=invisible', 'rw,hidepid=2') for line in ns]),
                         ['other processes visible'])
        gone = [line.replace(f'250:2 {files[1]} ', f'250:2 {files[1]}//deleted ') for line in ns]  # replaced since
        self.assertEqual(faults(gone), [f'{files[1]} bound from a deleted file'])
        self.assertEqual(faults(ns + [f'70 52 250:2 /x {hm}/other rw - ext4 /dev/x rw']), [f'another mount at {hm}/other'])
        self.assertEqual(faults(ns + ['71 53 250:2 /y /tmp/z rw - ext4 /dev/x rw']), ['another mount at /tmp/z'])
        self.assertEqual(faults(ns + [f'73 55 250:3 /z {root}/z rw - ext4 /dev/y rw']), [f'another mount at {root}/z'])
        self.assertEqual(faults(ns + ['74 52 250:2 / /home rw - ext4 /dev/x rw']), ['no tmpfs at /home'])  # over it
        self.assertEqual(faults(ns + ['75 22 250:2 / /home rw - ext4 /dev/x rw']), ['no tmpfs at /home'])  # later, beside
        tmp_tmpfs = [line.replace('- ext4', '- tmpfs') if line.startswith('53 ') else line for line in without(55)]
        self.assertIn(f'no tmpfs at {root}', faults(tmp_tmpfs))  # a tmpfs above the root is not one at it
        self.assertEqual(faults([line.replace(' 0:50 ', ' 0:24 ') for line in ns]), [SHM])  # the host's tmpfs
        self.assertEqual(faults(ns, here=ns), [SHM])  # a plan run inside the namespace cannot tell
        self.assertEqual(faults(ns, here=[]), [SHM])  # nor one that cannot read its own table
        stale = [line for line in without(51) if not line.startswith(f'{58 + len(files)} ')]  # an older drop-in's
        self.assertEqual(faults(stale), [SHM, f'no bind at {queue}'])

    def test_the_private_state_reads_the_drop_in_and_the_managers_mounts(self):
        conf = pathlib.Path(self.tmp.name, 'private.conf')
        unit, drop = 'user@4242.service', setup.DROPIN.format(uid=4242)
        ns = '\n'.join(self.namespace()) + '\n'

        def state(table):
            return setup.private_state(self.h, 4242, read=str(conf), mounts=lambda uid: table, here=str(host))
        host = pathlib.Path(self.tmp.name, 'host.mountinfo')
        host.write_text('\n'.join(self.namespace()[:5]) + '\n')
        self.assertEqual(setup.private_state(self.h, None), [('acct-x', 'no such account')])
        self.assertEqual(state(ns), [(drop, 'missing')])
        conf.write_text('changed\n')
        self.assertEqual(state(ns), [(drop, 'differs from the plan')])
        conf.write_text(setup.private_text(self.h))
        self.assertEqual(state(ns), [])
        self.assertEqual(state(None), [(unit, 'not running, or its mount table cannot be read')])
        self.assertEqual(state(ns.replace('hidepid=invisible', 'hidepid=0')),
                         [(unit, 'runs without the drop-in as planned (other processes visible): restart it')])

    def test_the_managers_mount_table(self):
        def ran(code, out):
            return lambda argv, **k: subprocess.CompletedProcess(argv, code, out, '')
        with open('/proc/self/mountinfo', encoding='utf-8') as f:
            mine = f.read()
        self.assertEqual(setup.manager_mounts(4242, run=ran(0, f'{os.getpid()}\n')), mine)
        for code, out in ((0, '0\n'), (1, f'{os.getpid()}\n'), (0, 'x\n'), (0, '999999999\n')):
            self.assertIsNone(setup.manager_mounts(4242, run=ran(code, out)), msg=(code, out))
        seen = []
        setup.manager_mounts(4242, run=lambda argv, **k: seen.append(argv) or subprocess.CompletedProcess(argv, 1, '', ''))
        self.assertEqual(seen, [['systemctl', 'show', '-p', 'MainPID', '--value', 'user@4242.service']])

    def test_the_shared_files_state_reads_their_acls(self):
        if not shutil.which('setfacl'):
            self.skipTest('no setfacl')
        me, other = os.getuid(), 65534
        names = {'acct-x': me, 'localuser': other}
        files, (queue,), _ = setup.shared_files(self.h)
        for f in files:
            pathlib.Path(f).touch()
        os.mkdir(queue)
        conf = pathlib.Path(self.tmp.name, 'tmpfiles.conf')
        conf.write_text(setup.tmpfiles_text(self.h))
        real = os.lstat

        def as_root(path):  # the tests cannot chown to root: the files read as root's
            st = real(path)
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, 0, 0, *st[6:10]))

        def state():
            with mock.patch.object(setup.os, 'lstat', as_root):
                return setup.shared_state(self.h, uid_of=names.__getitem__, read=str(conf))

        def acl(*args):
            r = subprocess.run(['setfacl', *args], capture_output=True, text=True)
            if r.returncode:
                self.skipTest(f'no ACL support here: {r.stderr.strip()}')
        self.assertEqual({w for _, w in state()}, {'no rw ACL entry for acct-x, localuser',
                                                   'no rwx ACL entry for acct-x, localuser',
                                                   'no default rwx ACL entry for acct-x, localuser'})
        for f in files:
            acl('-m', f'u:{me}:rw,u:{other}:rw,m::rw', f)
        acl('-m', f'u:{me}:rwx,u:{other}:rwx,m::rwx,d:u:{me}:rwx,d:u:{other}:rwx,d:m::rwx', queue)
        self.assertEqual(state(), [])
        acl('-m', 'm::r', files[1])  # the mask takes the entries' w away
        self.assertEqual(state(), [(files[1], 'no rw ACL entry for acct-x, localuser')])
        acl('-m', 'm::rw', files[1])
        acl('-x', f'd:u:{other}', queue)
        self.assertEqual(state(), [(queue, 'no default rwx ACL entry for localuser')])
        acl('-m', f'd:u:{other}:rwx', queue)
        acl('-m', f'u:{other}:rw', queue)  # the queue needs x too, to reach its tickets
        self.assertEqual(state(), [(queue, 'no rwx ACL entry for localuser')])
        acl('-m', f'u:{other}:rwx', queue)
        self.assertEqual(state(), [])
        os.unlink(files[0])  # a link in a file's place, even to a good file
        os.symlink(files[1], files[0])
        self.assertEqual(state(), [(files[0], 'not a regular file')])
        os.unlink(files[0])
        os.mkdir(files[0])  # a directory in a lock's place
        self.assertEqual(state(), [(files[0], 'not a regular file')])
        os.rmdir(files[0])
        pathlib.Path(files[0]).touch()
        acl('-m', f'u:{me}:rw,u:{other}:rw,m::rw', files[0])
        os.rmdir(queue)  # a file in the queue's place
        pathlib.Path(queue).touch()
        self.assertEqual(state(), [(queue, 'not a directory')])
        os.unlink(queue)
        os.symlink(files[1], queue)
        self.assertEqual(state(), [(queue, 'not a directory')])
        os.unlink(queue)
        os.mkdir(queue)
        acl('-m', f'u:{me}:rwx,u:{other}:rwx,m::rwx,d:u:{me}:rwx,d:u:{other}:rwx,d:m::rwx', queue)
        self.assertEqual(state(), [])
        conf.write_text('changed\n')
        self.assertEqual(state(), [(setup.TMPFILES, 'differs from the plan')])
        conf.write_text(setup.tmpfiles_text(self.h))
        self.assertEqual(setup.shared_state(self.h, uid_of=names.__getitem__, read=str(conf))[0][1], 'not root-owned')

    def _acl_state(self, h, names, has_account=lambda uid: True, mine=()):
        """``shared_state`` and ``stale_entries`` of ``h`` as root would see them (the tests
        cannot chown to root: the files read as root's, but those in ``mine``), with
        ``names``' uids; and a ``setfacl`` that skips the test where the file system has no
        ACLs."""
        real = os.lstat

        def as_root(path):
            st = real(path)
            if path in mine:
                return st
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, 0, 0, *st[6:10]))
        conf = pathlib.Path(self.tmp.name, 'tmpfiles.conf')
        conf.write_text(setup.tmpfiles_text(h))

        def state():
            with mock.patch.object(setup.os, 'lstat', as_root):
                return (setup.shared_state(h, uid_of=names.__getitem__, read=str(conf)),
                        setup.stale_entries(h, has_account=has_account))

        def acl(*args):
            r = subprocess.run(['setfacl', *args], capture_output=True, text=True)
            if r.returncode:
                self.skipTest(f'no ACL support here: {r.stderr.strip()}')
        return state, acl

    def _shared_made(self, h, acl, uids):
        """Every shared file and directory of ``h`` made, with the entries for ``uids``."""
        files, dirs, _ = setup.shared_files(h)
        rw, rwx = ','.join(f'u:{u}:rw' for u in uids), ','.join(f'u:{u}:rwx' for u in uids)
        dflt = ','.join(f'd:u:{u}:rwx' for u in uids)
        for f in files:
            pathlib.Path(f).touch()
            acl('-m', f'{rw},m::rw', f)
        for d in dirs:
            os.mkdir(d)
            acl('-m', f'{rwx},m::rwx,{dflt},d:m::rwx', d)
        return files, dirs

    def _with_leases(self):
        h = {**self.h, 'files': {**self.h['files'], 'lease_dir': f'{self.p}-leases'}}
        return h

    def test_the_lease_directorys_queue_is_checked(self):
        """Issue #125: the lease directory's queue, which the parts make, is checked as the
        directory it is in (its access and default entries for every account, no link), but
        may be missing and need not be root's."""
        if not shutil.which('setfacl'):
            self.skipTest('no setfacl')
        h, me, other, queue = self._with_leases(), os.getuid(), 65534, f'{self.p}-leases/queue'
        state, acl = self._acl_state(h, {'acct-x': me, 'localuser': other}, mine=(queue,))
        self._shared_made(h, acl, (me, other))
        self.assertEqual(state(), ([], []))  # not made yet: the first part makes it
        os.mkdir(queue)  # made by the parts with the inherited default entries, the maker's own
        self.assertEqual(state(), ([], []))
        acl('-x', f'u:{other}', queue)
        acl('-x', f'd:u:{me}', queue)
        self.assertEqual(state()[0], [(queue, 'no rwx ACL entry for localuser'),
                                      (queue, 'no default rwx ACL entry for acct-x')])
        os.rmdir(queue)
        os.symlink(f'{self.p}-ci.queue', queue)  # a link to a good directory is not one
        self.assertEqual(state()[0], [(queue, 'not a directory')])

    def test_a_renumbered_account(self):
        """Issue #125: the CI account renumbered, its old uid's entries left: the account's
        own entries are missing (a step), and the old uid's are reported as stale (a warning)."""
        if not shutil.which('setfacl'):
            self.skipTest('no setfacl')
        h, me, other, old = self._with_leases(), os.getuid(), 65534, 3999011
        state, acl = self._acl_state(h, {'acct-x': me, 'localuser': other}, has_account=lambda uid: uid != old)
        files, dirs = self._shared_made(h, acl, (old, other))
        os.mkdir(f'{self.p}-leases/queue')
        todo, stale = state()
        every = [*files, *dirs, f'{self.p}-leases/queue']
        self.assertEqual(sorted(p for p, _ in todo), sorted([*files, *dirs, *dirs, f'{self.p}-leases/queue',
                                                               f'{self.p}-leases/queue']))
        self.assertTrue(all(w.endswith(' for acct-x') for _, w in todo))
        self.assertEqual((setup._has_account(os.getuid()), setup._has_account(old)), (True, False))
        self.assertEqual(stale, [x for p in every for x in [
            (p, f'ACL entry for uid {old}, which has no account (a renumbered account?)'),
            *([(p, f'default ACL entry for uid {old}, which has no account (a renumbered account?)')]
              if p not in files else [])]])

    def test_a_stale_entry_beside_the_accounts_own_is_a_warning(self):
        """Issue #125: once the account's own entries are there, an old uid's are a warning in
        the plan, and apply goes ahead."""
        if not shutil.which('setfacl'):
            self.skipTest('no setfacl')
        me, other, old = os.getuid(), 65534, 3999011
        state, acl = self._acl_state(self.h, {'acct-x': me, 'localuser': other}, has_account=lambda uid: uid != old)
        files, (queue,) = self._shared_made(self.h, acl, (old, me, other))
        todo, stale = state()
        self.assertEqual(todo, [])
        self.assertEqual(len(stale), len(files) + 2)
        self.assertEqual(stale[-1], (queue, f'default ACL entry for uid {old}, which has no account '
                                            '(a renumbered account?)'))
        with mock.patch.object(setup, 'shared_state', lambda h: []), \
                mock.patch.object(setup, 'stale_entries', lambda h: stale), \
                mock.patch.object(setup, 'account_uid', lambda a: 4242), \
                mock.patch.object(setup, 'private_state', lambda h, uid: []):
            plan = setup.build(str(self.etc))
            self.assertIn(f'  warn   {queue}: default ACL entry for uid {old}, which has no account', plan.text())
            self.assertEqual(plan.blocked, '')
            self.assertEqual(self.apply(plan.text()), 0)

    def test_no_fixed_uid(self):
        """Issue #125: every uid comes from the account's name when the plan is made, so a
        renumbered account's plan names its new uid only (the drop-in's path, its user
        manager), and the tmpfiles.d entries name accounts, not uids."""
        def plan_with(uid):
            entry = mock.Mock(pw_uid=uid)
            with mock.patch.object(setup.pwd, 'getpwnam', lambda a: entry), \
                    mock.patch.object(setup, 'private_state', lambda h, u: []):
                return setup.build(str(self.etc)).text()
        before, after = plan_with(3999011), plan_with(3999009)
        self.assertIn(f'write {setup.DROPIN.format(uid=3999011)}', before)
        self.assertIn(f'write {setup.DROPIN.format(uid=3999009)}', after)
        self.assertIn('systemctl restart user@3999009.service', after)
        self.assertNotIn('3999011', after)
        self.assertEqual(before.replace('3999011', '3999009'), after)
        self.assertNotRegex(setup.tmpfiles_text(self.h), r'u:[0-9]')
        self.assertIn('u:acct-x:rw', setup.tmpfiles_text(self.h))

    def test_the_push_keys_line(self):
        old = f'ssh-ed25519 AAAA other-key\nrestrict,command="old" {KEY} {setup.PUSH_MARK}\n'
        got = setup.authorized_keys_text(old, KEY + ' a-comment', etc='/d/ci-runners/etc', python='/usr/bin/python3',
                                         base='/d/ci-runners').splitlines()
        self.assertEqual(got, ['ssh-ed25519 AAAA other-key',
                               'restrict,command="CI_RUNNERS_ETC=/d/ci-runners/etc CI_RUNNERS_PYTHON=/usr/bin/python3 exec '
                               f'/d/ci-runners/releases/hook/bin/ci-run org-sync --receive" {KEY} {setup.PUSH_MARK}'])
        for bad in (dict(key='ssh-rsa AAAA x'), dict(etc='/d e')):
            kw = {'key': KEY, 'etc': '/e', **bad}
            with self.assertRaises(ValueError, msg=bad):
                setup.authorized_keys_text('', kw['key'], etc=kw['etc'], python='/p', base='/b')
        self.inv.write_text(self.inv.read_text().replace('local_accounts = ["localuser"]\n', f'push_key = "{KEY}"\n'))
        text = setup.build(str(self.etc)).text()
        ak = self.home / '.ssh' / 'authorized_keys'
        self.assertIn(f'mkdir  {self.home}/.ssh/ mode 700', text)
        self.assertIn(f'write  {ak} mode 600', text)
        self.assertEqual(self.apply(text), 0)
        self.assertEqual(ak.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.home / '.ssh').stat().st_mode & 0o777, 0o700)
        self.assertTrue(ak.read_text().startswith('restrict,command="CI_RUNNERS_ETC='))
        self.assertEqual(setup.build(str(self.etc)).actions, [])  # a second run is a no-op
        os.chmod(self.home / '.ssh', 0o775)
        with self.assertRaisesRegex(ValueError, 'writable by it alone'):
            setup.build(str(self.etc))

    def test_the_pool_probe_keys_line(self):
        """The other host's CI account's key (decision 15.1, item 8): forced to pool probe."""
        other = KEY[:-5] + 'Zzzzz'
        old = f'ssh-ed25519 AAAA other-key\nrestrict,command="old" {other} {setup.PROBE_MARK}\n'
        got = setup.authorized_keys_text(old, KEY, etc='/d/ci-runners/etc', python='/usr/bin/python3',
                                         base='/d/ci-runners', probe_key=other + ' 3090a').splitlines()
        cmd = 'CI_RUNNERS_ETC=/d/ci-runners/etc CI_RUNNERS_PYTHON=/usr/bin/python3 exec /d/ci-runners/releases/hook/bin/ci-run'
        self.assertEqual(got, ['ssh-ed25519 AAAA other-key',
                               f'restrict,command="{cmd} org-sync --receive" {KEY} {setup.PUSH_MARK}',
                               f'restrict,command="{cmd} pool probe" {other} {setup.PROBE_MARK}'])
        gone = setup.authorized_keys_text('\n'.join(got), KEY, etc='/e', python='/p', base='/b').splitlines()
        self.assertEqual([x.split()[-1] for x in gone], ['other-key', setup.PUSH_MARK])  # a key no longer given: no line
        with self.assertRaisesRegex(ValueError, 'must not be the push_key'):
            setup.authorized_keys_text('', KEY, etc='/e', python='/p', base='/b', probe_key=KEY + ' again')
        with self.assertRaisesRegex(ValueError, 'probe_key must be'):
            setup.authorized_keys_text('', KEY, etc='/e', python='/p', base='/b', probe_key='ssh-rsa AAAA x')
        self.inv.write_text(self.inv.read_text().replace('local_accounts = ["localuser"]\n', f'probe_key = "{other}"\n'))
        text = setup.build(str(self.etc)).text()
        self.assertIn("the other host's pool probe key", text)
        self.assertEqual(self.apply(text), 0)
        ak = (self.home / '.ssh' / 'authorized_keys').read_text().splitlines()
        self.assertEqual(ak, [f'restrict,command="CI_RUNNERS_ETC={self.etc} CI_RUNNERS_PYTHON={sys.executable} exec '
                              f'{self.data}/ci-runners/releases/hook/bin/ci-run pool probe" {other} {setup.PROBE_MARK}'])
        self.assertEqual(setup.build(str(self.etc)).actions, [])

    def test_the_egress_accounts_line_for_the_tunnel_key(self):
        self.assertEqual(setup.EGRESS_LINE.format(port=18443, key=KEY),
                         'restrict,port-forwarding,permitopen="127.0.0.1:18443",permitlisten="127.0.0.1:9",'
                         f'command="/usr/sbin/nologin" {KEY}')
        ok = subprocess.CompletedProcess([], 0, KEY + ' comment\n', '')
        self.assertEqual(setup.tunnel_public_key('/e', run=lambda *a, **k: ok), KEY)  # the comment is not shown
        spaced = subprocess.CompletedProcess([], 0, KEY + ' a comment with spaces\n', '')  # as ssh-keygen -C wrote it
        self.assertEqual(setup.tunnel_public_key('/e', run=lambda *a, **k: spaced), KEY)
        for r in (subprocess.CompletedProcess([], 1, '', 'bad'), subprocess.CompletedProcess([], 0, 'ssh-rsa AAAA\n', '')):
            self.assertEqual(setup.tunnel_public_key('/e', run=lambda *a, _r=r, **k: _r),
                             '<the public key of etc/tunnel/id_ed25519>')


if __name__ == '__main__':
    unittest.main()
