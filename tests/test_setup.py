"""Host setup (design §5.2, §5.3): the plan lists every change, apply makes exactly an
approved plan and refuses another, and a second run changes nothing. On a fake host
tree; the systemctl commands are recorded, not run."""
from __future__ import annotations

import contextlib
import hashlib
import http.server
import io
import json
import os
import pathlib
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from unittest import mock

from ci_runners import inventory, setup

ROOT = pathlib.Path(__file__).resolve().parents[1]


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
                         ['ci-p.slice', 'ci-reaper.service', 'ci-reaper.timer', 'ci-runner@.service'])
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

    def test_a_retired_proxy_table_plans_nothing(self):
        """Decision 11.12: an inventory's old proxy table is ignored, not set up."""
        inv = self.etc / 'inventory.toml'
        before = setup.build(str(self.etc)).text()
        inv.write_text(inv.read_text() + '[hosts.hb.proxy]\nclient = "/usr/bin/x -c /tmp/y"\nhttp_port = 20809\n')
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
        self.assertEqual(enable, [['systemctl', '--user', 'enable', '--now', 'ci-reaper.timer', 'ci-orgsync.timer']])
        self.assertEqual(setup.timers({}), ['ci-reaper.timer'])
        self.assertEqual(setup.timers({'orgsync': True}), ['ci-reaper.timer', 'ci-orgsync.timer'])

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
    """h100 with RUN-5 (decision 11.3, 11.6): no CI proxy, its own interpreter, labelled
    pools and a runner per card, on a shared account whose unit directory may hold
    another user's ci-* unit."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = pathlib.Path(self.tmp.name)
        self.home, self.data = t / 'home', t / 'home' / 'sub'
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
h100 = {{ per_card = true, labels = ["h100"] }}
''')
        self.units = self.home / '.config' / 'systemd' / 'user'
        self.ran = []

    def tearDown(self):
        self.tmp.cleanup()

    def run_(self, argv, **_kw):
        self.ran.append(argv)

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
                                              ('hx-gpu0', ['h100', 'hx-gpu0'], 'h100'),
                                              ('hx-gpu3', ['h100', 'hx-gpu3'], 'h100')])
        self.assertEqual(setup.env_file(inv, 'h100', '/e', 3).splitlines(),
                         [f'ACTIONS_RUNNER_HOOK_JOB_STARTED={self.data}/ci-runners/hooks/job-started.sh',
                          f'ACTIONS_RUNNER_HOOK_JOB_COMPLETED={self.data}/ci-runners/hooks/job-completed.sh',
                          'CI_RUNNER_CLASS=h100', 'CI_RUNNERS_ETC=/e', 'CI_RUNNER_POOL_LABELS=h100', 'CI_RUNNER_CARD=3',
                          f'CI_RUNNERS_PYTHON={sys.executable}', 'LANG=C.UTF-8'])
        self.assertIn('CI_RUNNER_CARD=0\n', setup.env_file(inv, 'h100', '/e', 0))  # card 0 is a card
        self.assertNotIn('CI_RUNNER_CARD', setup.env_file(inv, 'part', '/e'))
        self.assertIn('CI_RUNNER_POOL_LABELS=hx-cpu\n', setup.env_file(inv, 'part', '/e'))
        self.assertEqual((inventory.pool_labels(inv, 'part'), inventory.pool_labels(inv, 'nope')), (['hx-cpu'], None))
        two = inventory.load(self.inv.read_text().replace('h100 = {', 'gpu = { per_card = true }\nh100 = {'))
        with self.assertRaisesRegex(ValueError, 'same name'):
            setup.runners(two)  # both per-card pools would name their runners hx-gpu0, hx-gpu3

    def test_plan_without_a_proxy(self):
        self.unpacked = []
        text = setup.build(str(self.etc)).text()
        for no in ('ci-proxy', '_proxy=', 'through the CI proxy', 'socks'):
            self.assertNotIn(no, text)
        for want in ('labels hx-light;', 'labels hx-cpu;', 'labels h100,hx-gpu0;', 'labels h100,hx-gpu3;',
                     '| CI_RUNNER_CARD=3', f'| CI_RUNNERS_PYTHON={sys.executable}', '| MemoryMax=512G',
                     'fetched directly', 'run    systemctl --user enable --now ci-reaper.timer ci-orgsync.timer\n'):
            self.assertIn(want, text)
        self.assertEqual(self.apply(text), 0)
        self.assertEqual(sorted(p.name for p in self.units.iterdir()),
                         ['ci-orgsync.service', 'ci-orgsync.timer', 'ci-reaper.service', 'ci-reaper.timer',
                          'ci-runner@.service', 'ci.slice'])
        orgsync = (self.units / 'ci-orgsync.service').read_text()
        self.assertIn(f'ExecStart={self.data}/ci-runners/releases/hook/bin/ci-run org-sync\n', orgsync)
        self.assertIn(f'Environment=CI_RUNNERS_PYTHON={sys.executable}\n', orgsync)
        hook = (self.data / 'ci-runners' / 'hooks' / 'job-started.sh').read_text()
        self.assertIn(f'\npy={sys.executable}\n', hook)  # written in: no job's environment picks it
        self.assertNotIn('CI_RUNNERS_PYTHON', hook)
        runner = (self.units / 'ci-runner@.service').read_text()
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
                     'run    systemctl --user enable --now ci-tunnel.service\n'):
            self.assertIn(want, text)
        self.assertNotIn('ci-proxy', text)
        self.assertNotIn('ci-bridge', text)
        self.assertNotIn('  remove ', text)  # nothing recorded: nothing to remove
        self.assertNotIn('egress.example', text)  # the lead's files are not shown
        self.assertEqual(self.apply(text), 0)
        self.assertEqual(sorted(p.name for p in self.units.iterdir()),
                         ['ci-reaper.service', 'ci-reaper.timer', 'ci-runner@.service', 'ci-tunnel.service', 'ci.slice'])
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

if __name__ == '__main__':
    unittest.main()
