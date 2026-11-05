"""Host setup (design §5.2, §5.3): the plan lists every change and withholds secrets,
apply makes exactly an approved plan and refuses another, and a second run changes
nothing. On a fake host tree; the systemctl commands are recorded, not run."""
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
        src = t / 'proxysrc'
        for d in (self.home, self.etc / 'projects', src, self.data / 'ci-runners' / 'releases'):
            d.mkdir(parents=True)
        os.symlink(ROOT, self.data / 'ci-runners' / 'releases' / 'hook')
        for n in ('n1', 'n2'):
            (src / f'{n}.json').write_text(json.dumps({
                'log': {'loglevel': 'warning'}, 'inbounds': [{'port': 10808, 'protocol': 'socks'}],
                'outbounds': [{'protocol': 'vmess', 'settings': {'id': f'SECRET-ID-{n}'}}]}))
        (src / 'routing.json').write_text('{"routing": {"rules": []}, "note": "SECRET-ROUTE"}')
        (self.etc / 'inventory.toml').write_text(f'''schema = 1
this = "hb"
[hosts.hb]
ssh = "alias-x"
account = "acct-x"
home = "{self.home}"
data = "{self.data}"
trusted = true
cpu_slowdown = 1.0
slots = 2
memory_max = "120G"
disk_min_gib = 50
gpus = [0, 1]
max_temp_c = 80
pools = {{ light = 1, part = 2 }}
files = {{ host_lock = "/tmp/h.lock", slot_lock = "/tmp/s.lock", slot_queue = "/tmp/s.queue" }}
[hosts.hb.proxy]
client = "/usr/bin/proxy-client"
config = "{src}"
nodes = ["n1", "n2"]
http_port = 20809
socks_port = 20808
no_proxy = "localhost,127.0.0.1"
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
        self.assertNotIn('SECRET', text)  # the proxy configs are withheld
        self.assertIn('(content withheld: it holds secrets)', text)
        for want in (f'mkdir  {self.etc}/ mode 700', 'inbounds 127.0.0.1:20808 socks, 127.0.0.1:20809 http',
                     f'| ACTIONS_RUNNER_HOOK_JOB_STARTED={self.data}/ci-runners/hooks/job-started.sh',
                     '| CI_RUNNER_CLASS=part', '| https_proxy=http://127.0.0.1:20809', '| MemoryMax=120G',
                     '| MemoryMax=80G', 'labels part,hb', 'run    systemctl --user enable --now ci-proxy.service'):
            self.assertIn(want, text)
        self.assertEqual(self.apply(text), 0)
        self.assertEqual((self.etc.stat().st_mode & 0o777), 0o700)
        node = json.loads((self.etc / 'proxy' / 'nodes' / 'n1.json').read_text())
        self.assertEqual([i['port'] for i in node['inbounds']], [20808, 20809])
        self.assertEqual(node['outbounds'][0]['settings']['id'], 'SECRET-ID-n1')
        self.assertEqual(os.readlink(self.etc / 'proxy' / 'active.json'), 'nodes/n1.json')
        self.assertEqual((self.etc / 'proxy' / 'nodes' / 'n1.json').stat().st_mode & 0o777, 0o600)
        unit = (self.home / '.config' / 'systemd' / 'user' / 'ci-proxy.service').read_text()
        self.assertIn('ExecStart=/usr/bin/proxy-client run -c', unit)
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

    def test_the_proxy_client_is_one_absolute_path(self):
        inv = self.etc / 'inventory.toml'
        for bad in ('proxy-client', '/usr/bin/x -c /tmp/y', '/usr/bin/x\\nExecStartPre=/bin/y'):
            inv.write_text(inv.read_text().replace('client = "/usr/bin/proxy-client"', f'client = "{bad}"'))
            with self.assertRaises(ValueError, msg=bad):
                setup.build(str(self.etc))
            inv.write_text(inv.read_text().replace(f'client = "{bad}"', 'client = "/usr/bin/proxy-client"'))

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

    def test_the_download_waits_for_the_proxy_to_listen(self):
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

        def late():  # the proxy starts listening a second after its unit started
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

    def test_a_proxy_that_never_listens_fails_within_the_bound(self):
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            port = s.getsockname()[1]  # bound, never listening
            code = (f'import sys; sys.path.insert(0, {str(ROOT)!r}); from ci_runners import setup; '
                    f'setup.wait_port({port}, timeout=1, poll=0.2)')
            t0 = time.monotonic()
            r = subprocess.run([sys.executable, '-I', '-c', code], capture_output=True, text=True, timeout=20)
            self.assertLess(time.monotonic() - t0, 10)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('nothing listens on the CI proxy port', r.stderr)

    def test_the_org_snapshot_timer_only_where_the_inventory_asks(self):
        text = setup.build(str(self.etc)).text()
        self.assertNotIn('ci-orgsync', text)
        inv = self.etc / 'inventory.toml'
        inv.write_text(inv.read_text().replace('max_temp_c = 80\n', 'max_temp_c = 80\norgsync = true\n'))
        plan = setup.build(str(self.etc))
        self.assertIn('ci-orgsync.service', plan.text())
        enable = [a['argv'] for a in plan.actions if a.get('kind') == 'run' and '--now' in a['argv']
                  and 'ci-reaper.timer' in a['argv']]
        self.assertEqual(enable, [['systemctl', '--user', 'enable', '--now', 'ci-proxy-check.timer', 'ci-reaper.timer',
                                   'ci-orgsync.timer']])
        self.assertEqual(setup.timers({}), ['ci-reaper.timer'])
        self.assertEqual(setup.timers({'orgsync': True}), ['ci-reaper.timer', 'ci-orgsync.timer'])

    def test_runner_names_and_labels(self):
        inv = inventory.load((self.etc / 'inventory.toml').read_text())
        self.assertEqual(setup.runners(inv), [('hb-light-1', ['light', 'hb'], 'light'),
                                              ('hb-part-1', ['part', 'hb'], 'part'),
                                              ('hb-part-2', ['part', 'hb'], 'part')])

    def test_render(self):
        self.assertEqual(setup.render('a=@X@\n# @NOTE@ in a comment\n', {'X': '1'}), 'a=1\n# @NOTE@ in a comment\n')
        with self.assertRaises(ValueError):
            setup.render('a=@Y@\n', {'X': '1'})


if __name__ == '__main__':
    unittest.main()
