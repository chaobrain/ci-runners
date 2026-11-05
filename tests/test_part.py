"""The executor and salvage (design §4.3-§4.5), on a fake host tree, without systemd:
the supervisor runs directly (``scope=False``). The scope itself is checked live on
3090b (RUN-1 acceptance 6)."""
from __future__ import annotations

import fcntl
import json
import os
import pathlib
import subprocess
import tempfile
import unittest

from ci_runners import inventory, part, registry

ROOT = pathlib.Path(__file__).resolve().parents[1]


class Host(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = pathlib.Path(self.tmp.name)
        self.data, self.etc, self.ws = t / 'data', t / 'data' / 'ci-runners' / 'etc', t / 'ws'
        for d in (self.etc / 'projects', self.ws, t / 'locks'):
            d.mkdir(parents=True)
        self.slot = t / 'locks' / 's.lock'
        self.slot.write_text('')
        self.inv_text = f'''schema = 1
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
gpus = [0, 1]
max_temp_c = 80
pools = {{ light = 1, part = 1 }}
files = {{ host_lock = "{t}/locks/h.lock", slot_lock = "{self.slot}", slot_queue = "{t}/locks/s.queue" }}
'''
        self.inv = inventory.load(self.inv_text)
        (self.etc / 'projects' / 'p.toml').write_text(
            'name = "p"\nrepository = "Org/Repo"\ncontext = "p/ci"\nmemory = "1G"\n'
            '[fallback]\npart = ["self-hosted", "part", "other"]\n'
            '[hosts.hb]\nmemory_max = "2G"\nenv = "/opt/env-p"\nvariables = { P_HOST = "hb" }\n')
        self.out = t / 'github_output'
        self.env = {'GITHUB_REPOSITORY': 'org/repo', 'GITHUB_RUN_ID': '77', 'GITHUB_RUN_ATTEMPT': '1',
                    'GITHUB_WORKSPACE': str(self.ws), 'RUNNER_NAME': 'hb-part-1', 'CI_RUNNER_CLASS': 'part',
                    'CI_RUNNERS_ETC': 'from-the-runner', 'CI_PROJECT': 'x', 'GITHUB_OUTPUT': str(self.out), 'PATH': '/usr/bin:/bin',
                    'HOME': str(t)}

    def tearDown(self):
        self.tmp.cleanup()

    def execute(self, env=None, **kw):
        kw.setdefault('project', 'p')
        kw.setdefault('id', 'unit')
        kw.setdefault('timeout_minutes', 1)
        kw.setdefault('queue_timeout_minutes', 0.02)
        kw.setdefault('command', 'true')
        ex = part.Executor(self.inv, str(self.etc), env or self.env, release=str(ROOT), scope=False, poll=0.1)
        return ex.execute(part.Inputs(**kw))

    def path(self, out):
        return str(self.data / 'ci-runners/state/parts' / out['record'].split(':', 1)[1])

    def outputs(self):
        return dict(line.split('=', 1) for line in self.out.read_text().splitlines())


class Execute(Host):
    def test_a_passing_part_and_its_environment(self):
        code, out = self.execute(command='env > "$CI_PART_DIR/env.txt"; pwd > "$CI_PART_DIR/pwd.txt"')
        self.assertEqual((code, out['outcome'], out['next']), (0, 'pass', 'none'))
        self.assertEqual(out['record'], 'hb:org/repo/77-1/unit-1')  # no home path in the outputs
        rec = self.path(out)
        env = dict(x.split('=', 1) for x in pathlib.Path(rec, 'env.txt').read_text().splitlines() if '=' in x)
        self.assertEqual(env['CI_PROJECT'], 'p')  # ours, not the job's
        self.assertEqual(env['P_HOST'], 'hb')
        self.assertEqual(env['CI_RUNNERS_HOST'], 'hb')
        self.assertNotIn('CI_RUNNERS_ETC', env)  # the runner's own CI_ names do not leak
        self.assertTrue(env['PATH'].startswith('/opt/env-p/bin:'))
        self.assertEqual((env['CUDA_VISIBLE_DEVICES'], env['CI_RESUME']), ('', '0'))
        self.assertEqual(env['CI_SLOT_HELD'], str(self.slot))  # a part runner takes a slot by default
        self.assertEqual(pathlib.Path(rec, 'pwd.txt').read_text().strip(), str(self.ws))
        o = self.outputs()
        self.assertEqual((o['outcome'], o['next'], o['retry-on'], o['verify'], o['attempt']),
                         ('pass', 'none', '[]', 'false', '1'))
        self.assertEqual(json.loads(pathlib.Path(rec, 'outcome.json').read_text())['outcome'], 'pass')
        self.assertIn('### part `unit`', pathlib.Path(rec, 'summary.md').read_text())

    def test_red_is_the_only_failing_exit(self):
        code, out = self.execute(command='exit 3')
        self.assertEqual((code, out['outcome'], out['reason'], out['next']), (1, 'red', 'exit 3', 'none'))

    def test_the_inner_timeout_ends_the_command(self):
        code, out = self.execute(command='sleep 30', timeout_minutes=0.02)
        self.assertEqual((code, out['outcome'], out['reason']), (1, 'red', 'the inner timeout ended the command'))
        self.assertLess(out['run-seconds'], 20)

    def test_refusals(self):
        bad = {'another repository': ({**self.env, 'GITHUB_REPOSITORY': 'org/other'}, {}),
               'not our runner': ({k: v for k, v in self.env.items() if k != 'CI_RUNNER_CLASS'}, {}),
               'memory above the ceiling': (None, {'memory': '3G'}),
               'a bad id': (None, {'id': '../x'}),
               'an unknown project': (None, {'project': 'q'}),
               'a malformed project name': (None, {'project': '../p'}),
               'too many cards': (None, {'gpus': 3}),
               'an endless timeout': (None, {'timeout_minutes': 241})}
        for what, (env, kw) in bad.items():
            with self.assertRaises(part.Refused, msg=what):
                self.execute(env=env, **kw)
        self.assertFalse((self.data / 'ci-runners/state/parts').exists())  # refused before any record

    def test_low_disk_is_unavailable_before_the_command(self):
        self.inv = inventory.load(self.inv_text.replace('disk_min_gib = 0', 'disk_min_gib = 100000000'))
        code, out = self.execute(command='touch "$CI_PART_DIR/ran"')
        self.assertEqual((code, out['outcome'], out['next']), (0, 'unavailable', 'fallback'))
        self.assertEqual(out['retry-on'], ['self-hosted', 'part', 'other'])
        self.assertFalse(os.path.exists(os.path.join(self.path(out), 'ran')))

    def test_a_held_slot_makes_the_part_wait_then_unavailable(self):
        fd = os.open(self.slot, os.O_WRONLY)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            code, out = self.execute(command='touch "$CI_PART_DIR/ran"')
        finally:
            os.close(fd)
        self.assertEqual((out['outcome'], out['reason']), ('unavailable', 'queue timeout: no CI slot'))
        self.assertEqual(os.listdir(self.slot.parent / 's.queue') and
                         [n for n in os.listdir(self.slot.parent / 's.queue') if n.endswith('.wait')], [])
        code, out = self.execute(command='true', slot='none')  # a part that takes no slot does not wait
        self.assertEqual(out['outcome'], 'pass')

    def test_the_slot_is_held_while_the_command_runs(self):
        code, out = self.execute(command='flock -n "$CI_SLOT_HELD" true && echo free > "$CI_PART_DIR/probe" || '
                                         'echo held > "$CI_PART_DIR/probe"')
        self.assertEqual(pathlib.Path(self.path(out), 'probe').read_text().strip(), 'held')
        fd = os.open(self.slot, os.O_WRONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # released at the end
        finally:
            os.close(fd)

    def test_a_retry_counts_its_attempt(self):
        _, first = self.execute(command='exit 0')
        _, second = self.execute(command='exit 0', attempt_of=first['record'])
        self.assertEqual(second['attempt'], 2)
        self.assertTrue(second['record'].endswith('/unit-2'))
        with self.assertRaises(part.Refused):
            self.execute(attempt_of='hb:../../etc')


class Salvage(Host):
    def test_salvage_reads_a_finished_record(self):
        _, out = self.execute(command='exit 0')
        rec = self.path(out)
        os.unlink(os.path.join(rec, 'outcome.json'))  # the job died before it published
        self.out.write_text('')
        code, again = part.salvage(self.inv, out['record'], self.env)
        self.assertEqual((code, again['outcome']), (0, 'pass'))
        self.assertEqual(self.outputs()['outcome'], 'pass')
        self.out.write_text('')
        code, again = part.salvage(self.inv, out['record'], self.env)  # published now: read as it is
        self.assertEqual((again['outcome'], self.outputs()['outcome']), ('pass', 'pass'))

    def test_a_record_that_never_finished_is_lost(self):
        _, out = self.execute(command='exit 0')
        rec = self.path(out)
        for n in ('outcome.json', 'exit.json'):
            os.unlink(os.path.join(rec, n))
        _, again = part.salvage(self.inv, out['record'], self.env)
        self.assertEqual((again['outcome'], again['next'], again['retry-on']), ('lost', 'retry', ['self-hosted', 'part']))

    def test_only_our_part_records(self):
        _, out = self.execute(command='exit 0')
        outside = pathlib.Path(self.tmp.name, 'outside')
        outside.mkdir()
        for n in ('record.json', 'outcome.json', 'exit.json'):  # a record planted outside the parts
            (outside / n).write_bytes(pathlib.Path(self.path(out), n).read_bytes())
        link = self.data / 'ci-runners/state/parts/org/repo/77-1/linked'
        os.symlink(outside, link)
        for ref in (f'hb:{outside}', 'hb:../../../outside', 'hb:org/repo/77-1/linked', 'other:org/repo/77-1/unit-1'):
            with self.assertRaises(part.Refused, msg=ref):
                part.salvage(self.inv, ref, self.env)


class Pure(unittest.TestCase):
    REC = {'class': 'part', 'host': 'hb', 'attempt': 1, 'retries': 1, 'boot_start': 'b1', 'run_epoch': 100.0}

    def test_oom_retries_once_with_resume(self):
        out = part.finish('/r', self.REC, {'rc': 137, 'oom_kills': 1, 'boot_end': 'b1', 'end_epoch': 160.0})
        self.assertEqual((out['outcome'], out['next'], out['resume'], out['run-seconds']), ('oom', 'retry', True, 60))
        out = part.finish('/r', self.REC, {'rc': 137, 'boot_end': 'b1'}, unit_result='oom-kill')
        self.assertEqual(out['outcome'], 'oom')
        out = part.finish('/r', {**self.REC, 'attempt': 2}, {'rc': 137, 'oom_kills': 1, 'boot_end': 'b1'})
        self.assertEqual((out['outcome'], out['next']), ('oom', 'none'))  # the second end is the consumer's

    def test_reboot_stopped_and_lost(self):
        self.assertEqual(part.finish('/r', self.REC, {'rc': 0, 'boot_end': 'b2'})['retry-on'],
                         ['self-hosted', 'part', 'hb'])  # a reboot retries on the same host
        self.assertEqual(part.finish('/r', self.REC, {'rc': 143, 'boot_end': 'b1'}, stopped=True)['outcome'], 'stopped')
        self.assertEqual(part.finish('/r', self.REC, None)['outcome'], 'lost')

    def test_scope_and_command(self):
        argv = part.scope_argv('ci-part-r-x-1.scope', 'p', '4G', ['sup'])
        for want in ('--user', '--scope', '--unit=ci-part-r-x-1.scope', '--slice=ci-p.slice', 'MemoryMax=4G',
                     'MemorySwapMax=0', 'OOMPolicy=continue'):
            self.assertIn(want, argv)
        self.assertEqual(argv[-1], 'sup')
        self.assertEqual(part.command_argv('c', compile_part=False, lock='none', lock_bin='L', host_lock='H'),
                         ['nice', '-n', '10', 'ionice', '-c', '2', '-n', '7', 'bash', '-c', 'c'])
        self.assertEqual(part.command_argv('c', compile_part=True, lock='s', lock_bin='L', host_lock='H')[2:3], ['0'])
        self.assertIn('-s', part.command_argv('c', compile_part=True, lock='s', lock_bin='L', host_lock='H'))
        self.assertEqual(part.unit_name('a b', 'x', '7-1-1'), 'ci-part-a_b-x-7-1-1.scope')

    def test_outputs_are_one_line_each(self):
        with tempfile.NamedTemporaryFile('r') as f:
            part.write_outputs({'outcome': 'pass', 'retry-on': ['a', 'b'], 'verify': False}, f.name)
            text = f.read()
        self.assertIn('retry-on=["a", "b"]\n', text)
        self.assertIn('verify=false\n', text)
        with self.assertRaises(ValueError):
            part.write_outputs({'reason': 'a\nb'}, '/dev/null')

    def test_the_supervisor_records_the_exit(self):
        with tempfile.TemporaryDirectory() as d:
            r = subprocess.run(['python3', '-I', '-c', 'import sys; sys.path.insert(0, sys.argv.pop(1)); '
                                'from ci_runners.part import supervise; sys.exit(supervise(sys.argv[1], 10, ["sh", "-c", '
                                '"cat /proc/self/oom_score_adj > $0/adj; exit 4", sys.argv[1]]))', str(ROOT), d],
                               capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 4, r.stderr)
            ex = json.loads(pathlib.Path(d, 'exit.json').read_text())
            self.assertEqual((ex['rc'], ex['timed_out']), (4, False))
            self.assertEqual(pathlib.Path(d, 'adj').read_text().strip(), '500')


class Registry(unittest.TestCase):
    BASE = {'name': 'p', 'repository': 'o/r', 'context': 'p/ci', 'hosts': {'hb': {'memory_max': '2G'}}}

    def test_defaults(self):
        p = registry.parse(self.BASE, 'hb', 'p')
        self.assertEqual((p.retries, p.max_waiting, p.max_slots, p.env, p.variables), (1, 1, 1, '', {}))

    def test_malformed(self):
        bad = {'stem': ({}, 'q'), 'unknown': ({'x': 1}, 'p'), 'repo': ({'repository': 'r'}, 'p'),
               'retries 2': ({'retries': 2}, 'p'), 'retries bool': ({'retries': True}, 'p'),
               'memory above': ({'memory': '3G'}, 'p'), 'other host': ({'hosts': {'hc': {'memory_max': '2G'}}}, 'p'),
               'env relative': ({'hosts': {'hb': {'memory_max': '2G', 'env': 'env'}}}, 'p'),
               'reserved var': ({'hosts': {'hb': {'memory_max': '2G', 'variables': {'LD_PRELOAD': 'x'}}}}, 'p'),
               'ci var': ({'hosts': {'hb': {'memory_max': '2G', 'variables': {'CI_RESUME': '1'}}}}, 'p'),
               'multi-line var': ({'hosts': {'hb': {'memory_max': '2G', 'variables': {'A': 'x\ny'}}}}, 'p'),
               'fallback': ({'fallback': {'part': []}}, 'p')}
        for what, (change, stem) in bad.items():
            with self.assertRaises(registry.RegistryError, msg=what):
                registry.parse({**self.BASE, **change}, 'hb', stem)


if __name__ == '__main__':
    unittest.main()
