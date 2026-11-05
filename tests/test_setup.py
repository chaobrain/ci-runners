"""Host setup (design §5.2, §5.3): the plan lists every change and withholds secrets,
apply makes exactly an approved plan and refuses another, and a second run changes
nothing. On a fake host tree; the systemctl commands are recorded, not run."""
from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import tempfile
import unittest

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
            d.mkdir(parents=True)
            (d / 'config.sh').write_text('#!/bin/sh\n')
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
