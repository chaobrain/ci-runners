"""The executor and salvage (design §4.3-§4.5), on a fake host tree, without systemd:
the supervisor runs directly (``scope=False``). The scope itself is checked live on
h100 (RUN-5)."""
from __future__ import annotations

import fcntl
import json
import signal
import sys
import time
import os
import pathlib
import subprocess
import tempfile
import threading
import unittest
import unittest.mock

from ci_runners import envuse, inventory, part, registry, slots
from tests.p2p_fake import zeros_release

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
account = "acct-x"
home = "{t}"
data = "{self.data}"
slots = 1
memory_max = "120G"
disk_min_gib = 0
gpus = [0, 1]
max_temp_c = 80
pools = {{ light = 1, part = 1 }}
files = {{ slot_lock = "{self.slot}", slot_queue = "{t}/locks/s.queue" }}
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
        env = dict(x.split('=', 1) for x in pathlib.Path(rec, 'work', 'env.txt').read_text().splitlines() if '=' in x)
        self.assertEqual((env['CI_PART_DIR'], env['CI_RESULTS_DIR']), (f'{rec}/work', f'{rec}/work/results'))
        self.assertEqual(env['CI_PROJECT'], 'p')  # ours, not the job's
        self.assertEqual(env['P_HOST'], 'hb')
        self.assertEqual(env['CI_RUNNERS_HOST'], 'hb')
        self.assertNotIn('CI_RUNNERS_ETC', env)  # the runner's own CI_ names do not leak
        self.assertTrue(env['PATH'].startswith('/opt/env-p/bin:'))
        self.assertEqual((env['CUDA_VISIBLE_DEVICES'], env['CI_RESUME']), ('', '0'))
        self.assertEqual(env['CI_SLOT_HELD'], str(self.slot))  # a part runner takes a slot by default
        self.assertEqual(pathlib.Path(rec, 'work', 'pwd.txt').read_text().strip(), str(self.ws))
        o = self.outputs()
        self.assertEqual((o['outcome'], o['next'], o['retry-on'], o['attempt']), ('pass', 'none', '[]', '1'))
        self.assertNotIn('verify', o)
        self.assertNotIn('CI_HOST_LOCK', env)  # the host lock is retired (decision 11.12)
        self.assertEqual(json.loads(pathlib.Path(rec, 'outcome.json').read_text())['outcome'], 'pass')
        self.assertEqual(json.loads(pathlib.Path(rec, 'outcome.json').read_text())['run-attempt'], 1)  # 77-1 (issue #64)
        self.assertEqual(part.record_run_attempt('/d/org/repo/77-12/unit-2'), 12)
        self.assertEqual(part.record_run_attempt('/d/org/repo/x/unit-2'), 0)
        self.assertIn('### part `unit`', pathlib.Path(rec, 'summary.md').read_text())
        self.assertEqual(o['summary'], 'org/repo/77-1/unit-1/summary.md')
        self.assertNotIn('GITHUB_OUTPUT', env)  # the command cannot write the step's outputs

    def test_red_is_the_only_failing_exit(self):
        temp = pathlib.Path(self.tmp.name, 'runner-temp')
        code, out = self.execute(env={**self.env, 'RUNNER_TEMP': str(temp)}, command='exit 3')
        self.assertEqual(json.loads((temp / 'ci-runners/unit/outcome.json').read_text())['outcome'], 'red')  # the artifact
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
               'an endless timeout': (None, {'timeout_minutes': 241}),
               'a negative card need': (None, {'gpu_mem_mib': -1}),
               'a negative queue wait': (None, {'queue_timeout_minutes': -1})}
        for what, (env, kw) in bad.items():
            with self.assertRaises(part.Refused, msg=what):
                self.execute(env=env, **kw)
        self.assertFalse((self.data / 'ci-runners/state/parts').exists())  # refused before any record

    def test_low_disk_is_unavailable_before_the_command(self):
        self.inv = inventory.load(self.inv_text.replace('disk_min_gib = 0', 'disk_min_gib = 100000000'))
        code, out = self.execute(command='touch "$CI_PART_DIR/ran"')
        self.assertEqual((code, out['outcome'], out['next']), (0, 'unavailable', 'fallback'))
        self.assertEqual(out['retry-on'], ['self-hosted', 'part', 'other'])
        self.assertFalse(os.path.exists(os.path.join(self.path(out), 'work', 'ran')))

    def test_a_labelled_pool_names_its_labels_in_the_record(self):
        """RUN-5: a CPU pool's runners carry `hb-cpu`, not `part`; a retry goes there."""
        self.inv = inventory.load(self.inv_text.replace('disk_min_gib = 0', 'disk_min_gib = 100000000').replace(
            'pools = { light = 1, part = 1 }', 'pools = { light = 1, part = { count = 1, labels = ["hb-cpu"] } }'))
        (self.etc / 'projects' / 'p.toml').write_text((self.etc / 'projects' / 'p.toml').read_text().replace(
            '[fallback]\npart = ["self-hosted", "part", "other"]\n', ''))
        code, out = self.execute()
        self.assertEqual((out['outcome'], out['retry-on']), ('unavailable', ['self-hosted', 'hb-cpu']))
        rec = json.loads(pathlib.Path(self.path(out), 'record.json').read_text())
        self.assertEqual(rec['pool_labels'], ['hb-cpu'])

    def test_a_held_slot_makes_the_part_wait_then_unavailable(self):
        fd = os.open(self.slot, os.O_WRONLY)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            code, out = self.execute(command='touch "$CI_PART_DIR/ran"')
            light = self.execute({**self.env, 'CI_RUNNER_CLASS': 'light'}, command='true', id='unit2')[1]
        finally:
            os.close(fd)
        self.assertEqual((out['outcome'], out['reason']), ('unavailable', 'queue timeout: no CI slot'))
        self.assertEqual(os.listdir(self.slot.parent / 's.queue') and
                         [n for n in os.listdir(self.slot.parent / 's.queue') if n.endswith('.wait')], [])
        self.assertEqual(light['outcome'], 'pass')  # light takes no slot: it runs while the slot is held

    def test_the_slot_is_held_while_the_command_runs(self):
        code, out = self.execute(command='flock -n "$CI_SLOT_HELD" true && echo free > "$CI_PART_DIR/probe" || '
                                         'echo held > "$CI_PART_DIR/probe"')
        self.assertEqual(pathlib.Path(self.path(out), 'work', 'probe').read_text().strip(), 'held')
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

    def test_an_older_ticket_keeps_its_turn(self):
        q = self.slot.parent / 's.queue'
        q.mkdir()
        (self.etc / 'projects' / 'q.toml').write_text(
            (self.etc / 'projects' / 'p.toml').read_text().replace('name = "p"', 'name = "q"'))
        (q / '000000000001.wait').write_text(f'1 {os.getpid()} {int(time.time())} /x ci-slot project=q cmd\n')
        _, out = self.execute(command='true')
        self.assertEqual((out['outcome'], out['reason']), ('unavailable', 'queue timeout: no CI slot'))

    def test_the_command_cannot_rewrite_its_record(self):
        _, out = self.execute(command='echo "{}" > "$CI_PART_DIR/../record.json" 2>/dev/null; '
                                      'echo x > "$CI_PART_DIR/record.json"; exit 3')
        self.assertEqual(out['outcome'], 'red')
        self.assertEqual(json.loads(pathlib.Path(self.path(out), 'record.json').read_text())['id'], 'unit')

    def test_an_id_runs_once_per_run(self):
        self.execute(command='true')
        with self.assertRaises(part.Refused):
            self.execute(command='true')

    def two_slots(self):
        self.inv = inventory.load(self.inv_text.replace('slots = 1\n', 'slots = 2\n'))
        (self.slot.parent / 's.2.lock').write_text('')

    def test_a_holder_without_a_record_counts_for_no_project(self):
        self.two_slots()
        (self.etc / 'projects' / 'p.toml').write_text(
            (self.etc / 'projects' / 'p.toml').read_text().replace('[hosts.hb]\n', '[hosts.hb]\nmax_slots = 1\n'))
        fd = os.open(self.slot, os.O_WRONLY)  # someone holds slot 1, with no holder record
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            code, out = self.execute(command='true')
        finally:
            os.close(fd)
        self.assertEqual(out['outcome'], 'pass')  # slot 2 is free and p holds none

    def test_the_cap_holds_with_a_free_slot(self):
        self.two_slots()
        (self.etc / 'projects' / 'p.toml').write_text(
            (self.etc / 'projects' / 'p.toml').read_text().replace('[hosts.hb]\n', '[hosts.hb]\nmax_slots = 0\n'))
        code, out = self.execute(command='true')
        self.assertEqual((out['outcome'], out['reason']), ('unavailable', 'queue timeout: no CI slot'))

    def test_a_retry_queues_past_a_full_project_queue(self):
        """Decision 13.6: the cap (``max_waiting``, §7 rule 2) refuses a first attempt, which
        goes elsewhere; a retry has nowhere else to go, so it queues, and the cap counts it."""
        self.two_slots()
        (self.etc / 'projects' / 'p.toml').write_text(
            (self.etc / 'projects' / 'p.toml').read_text().replace('[hosts.hb]\n', '[hosts.hb]\nmax_slots = 2\n'))
        q = self.slot.parent / 's.queue'
        q.mkdir()
        (q / '000000000001.wait').write_text(f'1 {os.getpid()} {int(time.time())} /x ci-slot project=p cmd\n')
        _, out = self.execute(id='a')
        self.assertEqual((out['outcome'], out['reason'], out['next']), ('unavailable', 'project queue full', 'fallback'))
        _, out = self.execute(id='a', attempt_of='hz:org/repo/77-1/a-1', command='touch "$CI_PART_DIR/ran"')
        self.assertEqual((out['outcome'], out['attempt']), ('pass', 2), out['reason'])
        self.assertTrue(pathlib.Path(self.path(out), 'work', 'ran').exists())

    def test_cards_are_leased(self):
        smi = '0, 24576, 576\n1, 24576, 24000\n'

        def run(argv, **kw):
            if argv[0] == 'nvidia-smi':
                return subprocess.CompletedProcess(argv, 0, smi, '')
            return subprocess.run(argv, **kw)
        ex = part.Executor(self.inv, str(self.etc), {**self.env, 'CI_RUNNER_CLASS': 'gpu'}, release=str(ROOT),
                           scope=False, poll=0.1, run=run)
        code, out = ex.execute(part.Inputs(project='p', id='g', timeout_minutes=1, queue_timeout_minutes=0.02, gpus=1,
                                           gpu_mem_mib=6000,
                                           command='echo $CUDA_VISIBLE_DEVICES > "$CI_PART_DIR/cards"'))
        self.assertEqual((out['outcome'], out['gpus']), ('pass', '0'))  # card 1 has too little free
        self.assertEqual(pathlib.Path(self.path(out), 'work', 'cards').read_text().strip(), '0')
        leases = part.Leases(str(self.data / 'ci-runners/state/leases'))
        self.assertEqual(leases.try_take(1, [0, 1], {0: 24000, 1: 24000}, 6000), [0])
        other = part.Leases(str(self.data / 'ci-runners/state/leases'))
        self.assertEqual(other.try_take(1, [0, 1], {0: 24000, 1: 24000}, 6000), [1])  # 0 is leased
        self.assertIsNone(part.Leases(str(self.data / 'ci-runners/state/leases')).try_take(1, [0, 1],
                                                                                          {0: 24000, 1: 24000}, 6000))
        leases.release()
        other.release()

    def test_the_command_does_not_inherit_the_slot(self):
        code, out = self.execute(command='for f in /proc/self/fd/*; do readlink "$f"; done > "$CI_PART_DIR/fds"')
        self.assertNotIn(str(self.slot), pathlib.Path(self.path(out), 'work', 'fds').read_text())

    def test_a_stopped_executor_leaves_the_part_to_salvage(self):
        code = (f'import sys, json; sys.path.insert(0, {str(ROOT)!r}); from ci_runners import inventory, part; '
                f'inv = inventory.load(open({str(self.tmp.name + "/inv.toml")!r}).read()); '
                f'ex = part.Executor(inv, {str(self.etc)!r}, json.loads(sys.argv[1]), release={str(ROOT)!r}, '
                f'scope=False, poll=0.1); '
                f'ex.execute(part.Inputs(project="p", id="long", timeout_minutes=1, '
                f'command="sleep 3; echo after the runner; head -c 8000000 /dev/zero; touch \\"$CI_PART_DIR/done\\""))')
        pathlib.Path(self.tmp.name, 'inv.toml').write_text(self.inv_text)
        child = subprocess.Popen([sys.executable, '-I', '-c', code, json.dumps(self.env)], stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, start_new_session=True)  # the runner's pipe
        rec = self.data / 'ci-runners/state/parts/org/repo/77-1/long-1'
        for _ in range(100):
            if (rec / 'scope.json').exists():
                break
            time.sleep(0.05)
        os.killpg(child.pid, signal.SIGTERM)  # the runner stops: it signals the step's process group
        self.assertEqual(child.wait(timeout=20), 128 + signal.SIGTERM)
        child.stdout.close()  # the runner is gone: the command's next write meets a closed pipe
        self.assertFalse((rec / 'outcome.json').exists())
        fd = os.open(self.slot, os.O_WRONLY)
        try:
            with self.assertRaises(BlockingIOError):  # the supervisor still holds the slot
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
        for _ in range(200):
            if (rec / 'exit.json').exists():
                break
            time.sleep(0.05)
        code, out = part.salvage(self.inv, 'hb:org/repo/77-1/long-1', self.env)
        self.assertEqual((out['outcome'], (rec / 'work' / 'done').exists()), ('pass', True))


SMI_MEMORY = '0, 24576, 576\n1, 24576, 576\n'
SMI_STATUS = '0, 00000000:01:00.0, 40, 0\n1, 00000000:02:00.0, 41, 0\n'
#: a P2P self-test whose peer copies read zeros (h100's fault, design §5.4), over the fixture driver
P2P_ZEROS = [sys.executable, '-I', '-c', f'import sys; sys.path.insert(0, {str(ROOT)!r}); '
             'from ci_runners import p2p; from tests.p2p_fake import Fake; '
             'why = p2p.self_test(Fake(mode="zeros"), 4096); print("p2p: " + (why or "ok")); sys.exit(1 if why else 0)']
#: one that passes, and leaves what it saw in the part's directory
P2P_OK = [sys.executable, '-I', '-c', 'import os; open(os.environ["CI_PART_DIR"] + "/p2p", "w").write('
          'os.environ["CUDA_VISIBLE_DEVICES"]); print("p2p: ok")']


class ClassHost(Host):
    """A 3090-style host: two cards, two slots, a per-card ``gpu`` pool, ``multi``,
    ``compile`` and ``nightly``."""

    def setUp(self):
        super().setUp()
        (self.slot.parent / 's.2.lock').write_text('')
        self.hl = pathlib.Path(self.tmp.name, 'locks', 'h.lock')
        self.inv_text = self.inv_text.replace('slots = 1\n', 'slots = 2\n').replace(
            'pools = { light = 1, part = 1 }',
            'cpus = "1"\ncompile_cpus = "0"\npools = { light = 1, part = 1, cards = { per_card = true, class = "gpu", '
            'labels = ["hb-gpu"] }, multi = 1, compile = 1, nightly = 1 }').replace(
            'files = { ', f'files = {{ host_lock = "{self.hl}", ')
        self.inv = inventory.load(self.inv_text)
        self.status = SMI_STATUS
        self.memory = SMI_MEMORY

    def run_fake(self, argv, **kw):
        if argv[0] == 'nvidia-smi':
            q = argv[1]
            text = self.memory if q.startswith('--query-gpu=index,memory') else \
                self.status if q.startswith('--query-gpu=index,pci.bus_id') else ''
            return subprocess.CompletedProcess(argv, 0, text, '')
        return subprocess.run(argv, **kw)

    def go(self, cls, *, card='', p2p=P2P_OK, release=str(ROOT), env_cover='', **kw):
        kw.setdefault('project', 'p')
        kw.setdefault('id', 'c')
        kw.setdefault('timeout_minutes', 1)
        kw.setdefault('queue_timeout_minutes', 0.02)
        kw.setdefault('command', 'touch "$CI_PART_DIR/ran"')
        env = {**self.env, 'CI_RUNNER_CLASS': cls, 'RUNNER_NAME': f'hb-{cls}-1', 'CI_RUNNER_CARD': card}
        if env_cover:
            env['BRAINSPIKE_HOST_LOCK'] = env_cover  # a stale cover in the job's environment
        ex = part.Executor(self.inv, str(self.etc), env, release=release, scope=False, poll=0.05, run=self.run_fake,
                           sample_s=0.05, p2p=p2p)
        return ex.execute(part.Inputs(**kw))

    def ran(self, out):
        return pathlib.Path(self.path(out), 'work', 'ran').exists()

    def work(self, out, name):
        return pathlib.Path(self.path(out), 'work', name).read_text().strip()


class Classes(ClassHost):
    """The 3090 classes (decisions 12.5 to 12.8, design §4.8, §5.5)."""

    def test_each_class_refuses_gpus_and_lock_outside_its_row(self):
        bad = [('light', {'gpus': 1}, 'take no card'), ('part', {'gpus': 1}, 'take no card'),
               ('compile', {'gpus': 2}, 'take no card'), ('gpu', {'gpus': 0}, 'take 1 card'),
               ('gpu', {'gpus': 2}, 'take 1 card'),
               ('multi', {'gpus': 1}, 'take 2 cards'), ('multi', {'gpus': 0}, 'take 2 cards'),
               ('light', {'lock': 's'}, 'take lock none'), ('light', {'lock': 'x'}, 'take lock none'),
               ('compile', {'lock': 'none'}, 'take lock x'), ('compile', {'lock': 's'}, 'take lock x')]
        for n, (cls, kw, why) in enumerate(bad):
            code, out = self.go(cls, card='0' if cls == 'gpu' else '', id=f'b{n}', **kw)
            self.assertEqual((code, out['outcome'], out['next']), (0, 'unavailable', 'fallback'), (cls, kw))
            self.assertIn(why, out['reason'], (cls, kw))
            self.assertFalse(self.ran(out), (cls, kw))
        good = [('light', {}), ('part', {'lock': 's'}), ('gpu', {'gpus': 1, 'lock': 'x'}), ('multi', {'gpus': 2}),
                ('compile', {}), ('compile', {'lock': 'x'}), ('nightly', {'gpus': 0}), ('nightly', {'gpus': 1}),
                ('nightly', {'gpus': 2, 'lock': 'l'})]
        for n, (cls, kw) in enumerate(good):
            code, out = self.go(cls, card='0' if cls == 'gpu' else '', id=f'g{n}', **kw)
            self.assertEqual(out['outcome'], 'pass', (cls, kw, out['reason']))

    def test_each_class_refuses_a_timeout_outside_its_row(self):
        for cls, top in (('light', 30), ('part', 240), ('compile', 240), ('multi', 240), ('nightly', 600)):
            gpus = 2 if cls == 'multi' else 0
            with self.assertRaises(part.Refused, msg=cls):
                self.go(cls, timeout_minutes=top + 1, gpus=gpus, id=f'{cls}-over')
            self.assertEqual(self.go(cls, timeout_minutes=top, gpus=gpus, id=f'{cls}-top')[1]['outcome'], 'pass', cls)
        with self.assertRaises(part.Refused):
            self.go('cpu-ish')  # no class of ours
        with self.assertRaisesRegex(part.Refused, 'is no runner class'):
            self.go('h100', gpus=1, card='0', id='h100')  # gone with decision 13.4
        self.assertNotIn('h100', part.ROWS)

    def test_a_lock_needs_the_host_lock(self):
        self.inv = inventory.load(self.inv_text.replace(f'host_lock = "{self.hl}", ', ''))
        for n, (cls, kw) in enumerate((('part', {'lock': 's'}), ('compile', {}), ('nightly', {'lock': 'x'}))):
            code, out = self.go(cls, id=f'n{n}', **kw)
            self.assertEqual((out['outcome'], out['reason']), ('unavailable', f'lock {kw.get("lock", "x")}: this host '
                                                                              'has no host lock'), cls)
        self.assertEqual(self.go('part', id='none')[1]['outcome'], 'pass')  # lock none needs none
        with self.assertRaises(part.Refused):
            self.go('part', id='bad', lock='X')

    def test_a_compile_part_is_pinned_at_nice_0_and_takes_a_slot(self):
        probe = ('nice > "$CI_PART_DIR/nice"; grep Cpus_allowed_list /proc/self/status > "$CI_PART_DIR/cpus"; '
                 'echo "$CI_CPUS $CI_SLOT_HELD" > "$CI_PART_DIR/env"')
        _, out = self.go('compile', command=probe)
        rec = json.loads(pathlib.Path(self.path(out), 'record.json').read_text())
        self.assertEqual((out['outcome'], rec['lock']), ('pass', 'x'))  # lock defaults to x on compile
        self.assertEqual(self.work(out, 'nice'), str(os.nice(0)))
        self.assertEqual(self.work(out, 'cpus').split()[-1], '0')  # compile_cpus
        self.assertEqual(self.work(out, 'env'), f'0 {self.slot}')
        _, out = self.go('part', command=probe, id='p')
        self.assertEqual(self.work(out, 'nice'), str(min(19, os.nice(0) + 10)))
        self.assertEqual(self.work(out, 'cpus').split()[-1], '1')  # cpus
        self.inv = inventory.load(self.inv_text.replace('compile_cpus = "0"\n', ''))
        _, out = self.go('compile', id='nocpus')
        self.assertEqual((out['outcome'], out['reason']), ('unavailable', 'this host has no compile_cpus for compile parts'))

    def test_ci_cpus_without_a_pin_lists_the_cpus(self):
        """Where the inventory gives no ``cpus``, CI_CPUS lists the CPUs the part may use, as
        the inventory would; never a count (issue #99)."""
        have = sorted(os.sched_getaffinity(0))
        if len(have) < 3:
            self.skipTest('needs three CPUs')
        self.inv = inventory.load(self.inv_text.replace('cpus = "1"\n', ''))
        os.sched_setaffinity(0, {have[0], have[1], have[-1]})
        try:
            _, out = self.go('part', command='echo "$CI_CPUS" > "$CI_PART_DIR/env"', id='nopin')
        finally:
            os.sched_setaffinity(0, have)
        sep = '-' if have[1] == have[0] + 1 else ','
        self.assertEqual((out['outcome'], self.work(out, 'env')), ('pass', f'{have[0]}{sep}{have[1]},{have[-1]}'))

    def test_a_compile_part_holds_x_and_covers_its_commands_requests(self):
        """Decision 12.7 and 12.9: a compile part holds the host lock exclusively for its whole
        command; the command's own ``host_lock.sh -s`` is covered, not deadlocked, and the
        lock is free again after the part."""
        sh = ROOT / 'tests/fixtures/brainspike/host_lock.sh'
        probe = (
            'flock -n -s "$CI_HOST_LOCK" true && echo free > "$CI_PART_DIR/probe" || echo held > "$CI_PART_DIR/probe"; '
            'echo "$BRAINSPIKE_HOST_LOCK" > "$CI_PART_DIR/cover"; '
            # brainspike's script finds the file by LOCAL_CI_HOST_LOCK: never let it reach the host's real one
            f'if [ "$LOCAL_CI_HOST_LOCK" = "$CI_HOST_LOCK" ]; then timeout 10 {sh} -s true 2> "$CI_PART_DIR/sh"; '
            'echo $? > "$CI_PART_DIR/rc"; else echo nolocal > "$CI_PART_DIR/rc"; fi')
        _, out = self.go('compile', command=probe)
        self.assertEqual(out['outcome'], 'pass', out['reason'])
        self.assertEqual((self.work(out, 'probe'), self.work(out, 'cover'), self.work(out, 'rc')), ('held', 'x', '0'))
        self.assertIn("covered by the caller's x hold", self.work(out, 'sh'))
        self.assertEqual(subprocess.run(['flock', '-n', '-x', str(self.hl), 'true'], timeout=10).returncode, 0)
        _, out = self.go('part', command=probe, id='s', lock='s')  # a shared hold: -s covered too
        self.assertEqual((self.work(out, 'probe'), self.work(out, 'cover'), self.work(out, 'rc')), ('free', 's', '0'))
        _, out = self.go('part', command='echo "${BRAINSPIKE_HOST_LOCK-unset} $CI_HOST_LOCK" > "$CI_PART_DIR/cover"',
                         id='none', env_cover='x')
        self.assertEqual(self.work(out, 'cover'), f'unset {self.hl}')  # lock none: no cover, a job's is dropped

    def test_a_part_waits_for_the_host_lock_within_its_queue_timeout(self):
        hold = subprocess.Popen(['flock', '-x', str(self.hl), 'sleep', '20'], start_new_session=True)
        try:
            for _ in range(500):
                if part.slots._flocked(str(self.hl)):
                    break
                time.sleep(0.01)
            code, out = self.go('compile')
        finally:
            os.killpg(hold.pid, signal.SIGKILL)
            hold.wait()
        self.assertEqual((code, out['outcome'], out['reason']), (0, 'unavailable', 'queue timeout: no host lock (x)'))
        self.assertFalse(self.ran(out))
        for p in (self.slot, self.slot.parent / 's.2.lock'):  # its slot is free again
            self.assertEqual(subprocess.run(['flock', '-n', '-x', str(p), 'true'], timeout=10).returncode, 0)

    def _sh_shared_holder(self, seconds):
        """A ``host_lock.sh -s`` holder (brainspike's own script, the file the part uses) for
        ``seconds``, as a review probe or a local run takes it; returns once it holds."""
        sh = ROOT / 'tests/fixtures/brainspike/host_lock.sh'
        hold = subprocess.Popen([str(sh), '-s', 'sleep', str(seconds)], start_new_session=True,
                                env={**os.environ, 'LOCAL_CI_HOST_LOCK': str(self.hl)}, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (self._kill_group(hold), hold.wait()))
        shared = self.hl.with_name('h.shared.lock')
        for _ in range(500):  # its marker: held shared by every normal shared holder
            if shared.exists() and part.slots._flocked(str(shared)):
                return hold
            time.sleep(0.01)
        self.fail('the host_lock.sh -s holder did not take the lock')

    @staticmethod
    def _kill_group(proc):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def test_a_compile_part_sees_a_host_lock_sh_shared_holder(self):
        """Issue 80 (brainspike's #444 flake): a ``compile`` part's ``x`` waits for a
        ``host_lock.sh -s`` holder, and runs once it has gone. The wait is the part's queue time;
        ``local_ci.sh`` inside the part prints ``x 0m0s`` in its table whatever the part waited,
        since its own request is covered by the part's hold."""
        hold = self._sh_shared_holder(30)
        code, out = self.go('compile')  # queue timeout 1.2 s: the holder is still in
        self.assertEqual((code, out['outcome'], out['reason']), (0, 'unavailable', 'queue timeout: no host lock (x)'))
        self.assertFalse(self.ran(out))
        self._kill_group(hold)
        hold.wait()
        short = self._sh_shared_holder(2)
        t0 = time.monotonic()
        code, out = self.go('compile', id='after', queue_timeout_minutes=0.5)
        self.assertEqual((code, out['outcome']), (0, 'pass'))
        self.assertTrue(self.ran(out))
        self.assertGreaterEqual(time.monotonic() - t0, 1.0)  # it waited for the 2 s holder
        self.assertGreaterEqual(out['queue-seconds'], 1)
        short.wait(10)

    def test_the_supervisor_keeps_the_host_lock_after_the_executor_is_gone(self):
        """The hold lasts as long as the command (§5.5 item 3: released when the scope ends),
        not as long as the executor: a stopped runner does not let a timing part's x go."""
        pathlib.Path(self.tmp.name, 'inv.toml').write_text(self.inv_text)
        env = {**self.env, 'CI_RUNNER_CLASS': 'compile', 'RUNNER_NAME': 'hb-compile-1'}
        code = (f'import sys, json; sys.path.insert(0, {str(ROOT)!r}); from ci_runners import inventory, part; '
                f'inv = inventory.load(open({str(self.tmp.name + "/inv.toml")!r}).read()); '
                f'ex = part.Executor(inv, {str(self.etc)!r}, json.loads(sys.argv[1]), release={str(ROOT)!r}, '
                f'scope=False, poll=0.05); '
                f'ex.execute(part.Inputs(project="p", id="long", timeout_minutes=1, command="sleep 2"))')
        child = subprocess.Popen([sys.executable, '-I', '-c', code, json.dumps(env)], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
        rec = self.data / 'ci-runners/state/parts/org/repo/77-1/long-1'
        for _ in range(200):
            if (rec / 'scope.json').exists():
                break
            time.sleep(0.05)
        os.killpg(child.pid, signal.SIGKILL)  # the runner and its executor are gone
        child.wait(timeout=20)
        held = subprocess.run(['flock', '-n', '-s', str(self.hl), 'true'], timeout=10).returncode
        self.assertEqual(held, 1, 'the command still runs, so its x hold stands')
        for _ in range(200):
            if (rec / 'exit.json').exists():
                break
            time.sleep(0.05)
        self.assertEqual(subprocess.run(['flock', '-w', '10', '-x', str(self.hl), 'true'], timeout=20).returncode, 0)

    def test_a_part_waiting_for_a_slot_holds_no_host_lock(self):
        """The order (§5.5 item 3): slots, then leases, then the host lock. A compile part
        that waits behind a nightly part holding every slot holds no lock meanwhile, so
        the two cannot wait for each other."""
        fds = []
        for p in (self.slot, self.slot.parent / 's.2.lock'):  # every slot taken, as by a nightly part
            fd = os.open(p, os.O_WRONLY)
            fcntl.flock(fd, fcntl.LOCK_EX)
            fds.append(fd)
        seen, stop = [], threading.Event()

        def watch():
            while not stop.is_set():
                seen.append(part.slots._flocked(str(self.hl)))
                time.sleep(0.01)
        t = threading.Thread(target=watch)
        t.start()
        try:
            _, out = self.go('compile', queue_timeout_minutes=0.01)
        finally:
            stop.set()
            t.join()
            for fd in fds:
                os.close(fd)
        self.assertEqual(out['reason'], 'queue timeout: no CI slot')
        self.assertTrue(seen)
        self.assertFalse(any(seen), 'the host lock was held while the part waited for a slot')

    def test_a_multi_part_runs_after_the_p2p_self_test(self):
        _, out = self.go('multi', gpus=2, command='echo $CUDA_VISIBLE_DEVICES > "$CI_PART_DIR/cards"')
        self.assertEqual((out['outcome'], out['gpus']), ('pass', '0,1'))
        self.assertEqual((self.work(out, 'p2p'), self.work(out, 'cards')), ('0,1', '0,1'))
        self.assertIsNone(json.loads(pathlib.Path(self.path(out), 'exit.json').read_text()).get('p2p'))

    def test_a_failed_p2p_self_test_is_unavailable_p2p_with_next_none(self):
        code, out = self.go('multi', gpus=2, p2p=P2P_ZEROS)
        self.assertEqual((code, out['outcome'], out['next'], out['retry-on']), (0, 'unavailable', 'none', []))
        self.assertTrue(out['reason'].startswith('p2p: card 0 -> 1: byte 0 reads 0x00'), out['reason'])
        self.assertIn('(zeros)', out['reason'])
        self.assertFalse(self.ran(out))  # the command never ran
        leases = part.Leases(str(self.data / 'ci-runners/state/leases'))
        self.assertEqual(leases.leased([0, 1]), {0: 0, 1: 0})  # and the cards are free again
        _, out = self.go('gpu', card='1', gpus=1, p2p=P2P_ZEROS, id='one')  # one card: no P2P self-test
        self.assertEqual(out['outcome'], 'pass')

    def test_the_release_s_own_p2p_test_keeps_the_command_out(self):
        """The default self-test (``part.p2p_argv``, the release's ``p2p.main``) over the
        zeros fixture: the part ends unavailable (p2p), next none, and its command never runs."""
        with tempfile.TemporaryDirectory() as d:
            code, out = self.go('multi', gpus=2, p2p=None, release=zeros_release(d))
        self.assertEqual((code, out['outcome'], out['next']), (0, 'unavailable', 'none'))
        self.assertRegex(out['reason'], r'^p2p: card 0 -> 1: byte 0 reads 0x00, not 0x[0-9a-f]{2} \(zeros\)$')
        self.assertFalse(self.ran(out))

    def test_a_hot_card_keeps_a_multi_part_out(self):
        self.status = SMI_STATUS.replace('41, 0', '85, 100')
        _, out = self.go('multi', gpus=2)
        self.assertEqual((out['outcome'], out['next']), ('unavailable', 'fallback'))
        self.assertIn('health gate', out['reason'])
        _, out = self.go('gpu', card='0', gpus=1, id='cool')  # the other card's runner still runs
        self.assertEqual(out['outcome'], 'pass')

    def test_a_hot_card_is_not_leased(self):
        """A part that may take any card takes a cool one on a per-card host."""
        self.status = SMI_STATUS.replace('40, 0', '85, 100')
        _, out = self.go('gpu', gpus=1)
        self.assertEqual((out['outcome'], out['gpus']), ('pass', '1'))

    def test_a_nightly_part_takes_every_slot_and_card(self):
        leases = self.data / 'ci-runners/state/leases'
        held = ' '.join(f'"{p}"' for p in (self.slot, self.slot.parent / 's.2.lock', leases / 'card0.0.lock',
                                           leases / 'card1.0.lock'))
        probe = (f'for f in {held}; do flock -n "$f" true && echo free || echo held; done > "$CI_PART_DIR/probe"; '
                 'echo "[$CUDA_VISIBLE_DEVICES]" > "$CI_PART_DIR/cards"')
        for n, (gpus, cards) in enumerate(((0, '[]'), (1, '[0]'), (2, '[0,1]'))):
            _, out = self.go('nightly', gpus=gpus, command=probe, id=f'n{n}', timeout_minutes=600)
            self.assertEqual(out['outcome'], 'pass')
            self.assertEqual(self.work(out, 'probe').split(), ['held'] * 4, gpus)
            self.assertEqual(self.work(out, 'cards'), cards)
        for p in (self.slot, self.slot.parent / 's.2.lock'):  # released at the end
            fd = os.open(p, os.O_WRONLY)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)

    def test_a_nightly_parts_memory_check_covers_only_its_commands_cards(self):
        """Lead, 2026-11-07: a nightly part leases every card whatever they hold, and
        ``gpu-mem-mib`` is checked only on the cards its command gets, the lowest ones with
        that much free: another user's job on card 0 leaves it card 1."""
        self.memory = '0, 24576, 24000\n1, 24576, 576\n'  # card 0 nearly full
        probe = 'echo "[$CUDA_VISIBLE_DEVICES]" > "$CI_PART_DIR/cards"'
        _, out = self.go('nightly', gpus=1, gpu_mem_mib=8000, command=probe, timeout_minutes=600)
        self.assertEqual(out['outcome'], 'pass', out['reason'])
        self.assertEqual(self.work(out, 'cards'), '[1]')
        self.assertEqual(json.loads(pathlib.Path(self.path(out), 'record.json').read_text())['leased'], [0, 1])
        _, out = self.go('nightly', gpus=2, gpu_mem_mib=8000, id='both')  # two cards: card 0 does not fit
        self.assertEqual((out['outcome'], out['reason']), ('unavailable', 'queue timeout: no card fits'))
        _, out = self.go('nightly', gpus=0, gpu_mem_mib=8000, id='none')  # no card for the command: no check
        self.assertEqual(out['outcome'], 'pass', out['reason'])

    def test_a_nightly_part_gives_its_command_cool_cards_only(self):
        """A hot card is still leased (the nightly holds them all) but never handed to the
        command; with too few cool cards the health gate ends the part."""
        self.status = SMI_STATUS.replace('40, 0', '85, 100')  # card 0 hot
        probe = 'echo "[$CUDA_VISIBLE_DEVICES]" > "$CI_PART_DIR/cards"'
        _, out = self.go('nightly', gpus=1, command=probe, timeout_minutes=600)
        self.assertEqual(out['outcome'], 'pass', out['reason'])
        self.assertEqual(self.work(out, 'cards'), '[1]')
        self.assertEqual(json.loads(pathlib.Path(self.path(out), 'record.json').read_text())['leased'], [0, 1])
        _, out = self.go('nightly', gpus=2, id='both', timeout_minutes=600)
        self.assertEqual(out['outcome'], 'unavailable')
        self.assertIn('health gate', out['reason'])

    def test_a_nightly_part_waits_for_every_slot_and_card(self):
        fd = os.open(self.slot.parent / 's.2.lock', os.O_WRONLY)
        fcntl.flock(fd, fcntl.LOCK_EX)  # someone holds slot 2
        try:
            _, out = self.go('nightly')
        finally:
            os.close(fd)
        self.assertEqual((out['outcome'], out['reason']), ('unavailable', 'queue timeout: no CI slot'))
        self.assertFalse(self.ran(out))
        fd = os.open(self.slot, os.O_WRONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # the slot it held meanwhile is free again
        finally:
            os.close(fd)
        other = part.Leases(str(self.data / 'ci-runners/state/leases'))
        self.assertEqual(other.try_take(1, [1], {0: 24000, 1: 24000}, 0), [1])  # a part on card 1
        try:
            _, out = self.go('nightly', id='n2')
        finally:
            other.release()
        self.assertEqual((out['outcome'], out['reason']), ('unavailable', 'queue timeout: no card fits'))


class BehindNightly(ClassHost):
    """Decision 13.6: while a ``nightly`` part holds or waits for every slot and card, a
    first attempt with a peer pool goes there at once (``busy: nightly ...``) instead of
    waiting out its queue timeout; a retry queues. The hold lives as long as the executor or
    the supervisor that keeps the slots."""

    PEER = 'peer_labels = { part = ["hz-cpu"], gpu = ["hz-gpu"], multi = ["hz-multi"] }\n'

    def setUp(self):
        super().setUp()
        self.inv_text = self.inv_text.replace('max_temp_c = 80\n', 'max_temp_c = 80\n' + self.PEER)
        self.inv = inventory.load(self.inv_text)
        self.hold = pathlib.Path(part.every_path(str(self.data)))

    def test_a_nightly_part_keeps_the_hold_record_for_its_life(self):
        _, out = self.go('nightly', command=f'cp "{self.hold}" "$CI_PART_DIR/hold"', timeout_minutes=600)
        self.assertEqual(out['outcome'], 'pass', out['reason'])
        hold = json.loads(self.work(out, 'hold'))
        self.assertEqual(len(hold['procs']), 2)  # the executor and the supervisor
        self.assertEqual(hold['procs'][0][0], os.getpid())
        self.assertEqual(hold['record'], out['record'].split(':', 1)[1])
        self.assertFalse(self.hold.exists())  # gone with the part
        _, out = self.go('part', id='after')
        self.assertEqual(out['outcome'], 'pass', out['reason'])
        other = subprocess.Popen(['sleep', '30'])  # another writer's record, written while ours ran
        self.addCleanup(lambda: (other.kill(), other.wait()))
        mark = f'{{"procs": [[{other.pid}, {round(slots._start_of(other.pid))}]], "record": "x"}}'
        _, out = self.go('nightly', id='n2', command=f"printf '%s' '{mark}' > \"{self.hold}\"", timeout_minutes=600)
        self.assertEqual(out['outcome'], 'pass', out['reason'])
        self.assertEqual(json.loads(self.hold.read_text())['procs'][0][0], other.pid)  # not ours to delete

    def nightly(self, gate):
        """A nightly part in its own process (its runner), its command waiting for ``gate``."""
        inv = pathlib.Path(self.tmp.name, 'inv.toml')
        inv.write_text(self.inv_text)
        code = '\n'.join([
            'import json, subprocess, sys',
            f'sys.path.insert(0, {str(ROOT)!r})',
            'from ci_runners import inventory, part',
            f'inv = inventory.load(open({str(inv)!r}).read())',
            'def run(argv, **kw):',
            '    if argv[0] == "nvidia-smi":',
            f'        text = {SMI_MEMORY!r} if argv[1].startswith("--query-gpu=index,memory") else '
            f'{SMI_STATUS!r} if argv[1].startswith("--query-gpu=index,pci.bus_id") else ""',
            '        return subprocess.CompletedProcess(argv, 0, text, "")',
            '    return subprocess.run(argv, **kw)',
            f'ex = part.Executor(inv, {str(self.etc)!r}, json.loads(sys.argv[1]), release={str(ROOT)!r}, scope=False, '
            'poll=0.05, run=run)',
            'ex.execute(part.Inputs(project="p", id="night", timeout_minutes=1, queue_timeout_minutes=1, '
            f'command="while [ ! -e {gate} ]; do sleep 0.05; done"))'])
        env = {**self.env, 'CI_RUNNER_CLASS': 'nightly', 'RUNNER_NAME': 'hb-nightly-1'}
        return subprocess.Popen([sys.executable, '-I', '-c', code, json.dumps(env)], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)

    def stop(self, child, gate):
        gate.touch()
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()

    def test_a_waiting_nightly_part_sends_first_attempts_away(self):
        """Its ticket holds back every later one, so the hold begins with its wait."""
        gate = pathlib.Path(self.tmp.name, 'gate')
        proj = self.etc / 'projects' / 'p.toml'
        proj.write_text(proj.read_text().replace('memory = "1G"\n', 'memory = "1G"\nmax_waiting = 2\n'))  # room to wait
        fd = os.open(self.slot.parent / 's.2.lock', os.O_WRONLY)
        fcntl.flock(fd, fcntl.LOCK_EX)  # someone holds slot 2: the nightly part waits
        child = self.nightly(gate)
        try:
            q = self.slot.parent / 's.queue'
            for _ in range(200):
                if q.is_dir() and any(n.endswith('.wait') for n in os.listdir(q)):
                    break
                time.sleep(0.05)
            _, out = self.go('part', id='a', queue_timeout_minutes=0.05)
            self.assertEqual((out['outcome'], out['reason']), ('unavailable', part.NIGHTLY_BUSY))
        finally:
            os.close(fd)
            self.stop(child, gate)

    def later(self, write, delay=0.3):
        t = threading.Timer(delay, write)
        t.start()
        self.addCleanup(t.cancel)

    def test_a_waiting_part_yields_only_behind_a_nightly_ticket(self):
        """The race the gate cannot see: a nightly part that comes in while a first attempt
        waits. Behind its ticket the part goes elsewhere; ahead of it, it keeps its turn."""
        fds = [os.open(p, os.O_WRONLY) for p in (self.slot, self.slot.parent / 's.2.lock')]
        for fd in fds:
            fcntl.flock(fd, fcntl.LOCK_EX)  # both slots held: the part waits
        self.addCleanup(lambda: [os.close(fd) for fd in fds])
        q = self.slot.parent / 's.queue'
        q.mkdir()

        def every(seq):
            (q / f'{seq:012d}.wait').write_text(f'{seq} {os.getpid()} {int(time.time())} /x ci-slot project=q every n\n')

        every(1)  # the nightly's ticket, older than ours; its hold record comes once ours waits
        self.later(lambda: part.every_hold(str(self.data), [os.getpid()], 'n'))
        t0 = time.monotonic()
        _, out = self.go('part', id='behind', queue_timeout_minutes=0.05)
        self.assertEqual((out['reason'], out['retry-on']), (part.NIGHTLY_BUSY, ['self-hosted', 'hz-cpu']))
        self.assertLess(time.monotonic() - t0, 2.5)
        (q / '000000000001.wait').unlink()
        os.unlink(part.every_path(str(self.data)))

        def younger():  # the nightly comes in after our ticket: we are ahead of it
            mine = max(int(n[:12]) for n in os.listdir(q) if n.endswith('.wait'))
            every(mine + 1)
            part.every_hold(str(self.data), [os.getpid()], 'n')
        self.later(younger)
        _, out = self.go('part', id='ahead', queue_timeout_minutes=0.05)
        self.assertEqual(out['reason'], 'queue timeout: no CI slot')

    def test_a_part_waiting_for_its_card_yields_once_the_nightly_is_ahead(self):
        """A GPU part waiting for its lease goes elsewhere once a nightly part runs (it holds
        every card) or waits for the cards with an older lease ticket; else it waits."""
        leases = self.data / 'ci-runners/state/leases'
        other = part.Leases(str(leases))
        self.assertEqual(other.try_take(1, [1], {0: 24000, 1: 24000}, 0), [1])  # card 1 held: the part waits
        self.addCleanup(other.release)
        me = [os.getpid(), round(slots._start_of(os.getpid()))]

        def hold(procs):
            part._write_json(part.every_path(str(self.data)), {'procs': procs, 'record': 'n'})
        cases = (('running', [me, me], False, part.NIGHTLY_BUSY),
                 ('waiting, older ticket', [me], True, part.NIGHTLY_BUSY),
                 ('waiting for slots', [me], False, 'queue timeout: no card fits'))
        for n, (what, procs, ticket, reason) in enumerate(cases):
            with self.subTest(what):
                if ticket:
                    part._write_json(str(leases / 'queue' / '000000000001.json'),
                                     {'pid': me[0], 'start': me[1], 'n': 2, 'cards': [0, 1], 'need_mib': 0, 'every': True})
                self.later(lambda procs=procs: hold(procs))
                _, out = self.go('gpu', card='1', gpus=1, id=f'g{n}', queue_timeout_minutes=0.05)
                self.assertEqual(out['reason'], reason)
                if reason == part.NIGHTLY_BUSY:
                    self.assertEqual(out['retry-on'], ['self-hosted', 'hz-gpu'])
                for f in (leases / 'queue' / '000000000001.json', pathlib.Path(part.every_path(str(self.data)))):
                    if f.exists():
                        f.unlink()

    def test_a_hold_record_that_cannot_be_written_holds_nothing_back(self):
        """The record only places other parts: a nightly part whose record cannot be written
        runs (fail open)."""
        state = self.data / 'ci-runners/state'
        state.mkdir(parents=True)
        os.mkdir(part.every_path(str(self.data)))  # a directory where the record goes: every write fails
        _, out = self.go('nightly', timeout_minutes=600)
        self.assertEqual(out['outcome'], 'pass', out['reason'])
        self.assertTrue(self.ran(out))

    def test_first_attempts_go_to_the_peer_at_once_and_a_retry_queues(self):
        gate = pathlib.Path(self.tmp.name, 'gate')
        child = self.nightly(gate)
        rec = self.data / 'ci-runners/state/parts/org/repo/77-1/night-1'
        try:
            for _ in range(200):
                if (rec / 'scope.json').exists() and len(json.loads(self.hold.read_text())['procs']) == 2:
                    break
                time.sleep(0.05)
            seq = (self.slot.parent / 's.queue' / 'seq').read_text()
            t0 = time.monotonic()
            _, out = self.go('part', id='a', queue_timeout_minutes=0.05)
            self.assertEqual((out['outcome'], out['reason'], out['next'], out['retry-on']),
                             ('unavailable', part.NIGHTLY_BUSY, 'fallback', ['self-hosted', 'hz-cpu']))
            self.assertLess(time.monotonic() - t0, 2.5)  # at once, not after its queue timeout
            self.assertEqual((self.slot.parent / 's.queue' / 'seq').read_text(), seq)  # the gate: no ticket taken
            self.assertFalse(self.ran(out))
            _, out = self.go('gpu', card='1', gpus=1, id='g', queue_timeout_minutes=0.05)
            self.assertEqual((out['reason'], out['retry-on']), (part.NIGHTLY_BUSY, ['self-hosted', 'hz-gpu']))
            _, out = self.go('part', id='a', attempt_of='hz:org/repo/77-1/a-1')  # a retry waits for its slot
            self.assertEqual((out['outcome'], out['reason'], out['attempt']), ('unavailable', 'queue timeout: no CI slot', 2))
            _, out = self.go('light', id='l')
            self.assertEqual(out['outcome'], 'pass')
            os.killpg(child.pid, signal.SIGTERM)  # its runner stops: the supervisor keeps the slots, and the hold
            self.assertEqual(child.wait(timeout=20), 128 + signal.SIGTERM)
            _, out = self.go('part', id='b', queue_timeout_minutes=0.05)
            self.assertEqual(out['reason'], part.NIGHTLY_BUSY)
            sup = json.loads(self.hold.read_text())['procs'][1]
        finally:
            self.stop(child, gate)
        for _ in range(200):
            if not slots.alive(sup[0], sup[1]):
                break
            time.sleep(0.05)
        _, out = self.go('part', id='c')  # its processes are gone: the record holds nothing
        self.assertEqual(out['outcome'], 'pass', out['reason'])


class NoHostLock(Host):
    """A host without ``files.host_lock`` (D5) refuses any ``lock`` but ``none`` and takes
    no lock (decision 12.9)."""

    def test_no_lock_without_files_host_lock(self):
        self.inv = inventory.load(self.inv_text.replace(
            'pools = { light = 1, part = 1 }', 'pools = { light = 1, part = 1, cards = { per_card = true, class = "gpu" } }'))
        for n, (cls, kw) in enumerate((('part', {'lock': 's'}), ('part', {'lock': 'x'}), ('part', {'lock': 'l'}),
                                        ('gpu', {'lock': 'x', 'gpus': 1}))):
            env = {**self.env, 'CI_RUNNER_CLASS': cls, 'CI_RUNNER_CARD': '0'}
            _, out = self.execute(env=env, id=f'h{n}', **kw)
            self.assertEqual((out['outcome'], out['reason']), ('unavailable', f'lock {kw["lock"]}: this host has no '
                                                                              'host lock'), (cls, kw))
        _, out = self.execute(command='echo "${BRAINSPIKE_HOST_LOCK-unset} ${CI_HOST_LOCK-unset}" > "$CI_PART_DIR/c"',
                              id='none', lock='none')
        self.assertEqual(out['outcome'], 'pass')
        self.assertEqual(pathlib.Path(self.path(out), 'work', 'c').read_text().strip(), 'unset unset')


class SharedLeases(Host):
    """The inventory's ``files.lease_dir``: the leases the CI account shares with the pool
    runs of other accounts (decision 15.1)."""
    FREE = {0: 24000, 1: 24000}

    def test_a_gpu_part_leases_in_the_inventorys_lease_dir(self):
        shared = pathlib.Path(self.tmp.name) / 'leases'
        self.inv = inventory.load(self.inv_text.replace('slot_queue =', f'lease_dir = "{shared}", slot_queue ='))
        smi = '0, 24576, 576\n1, 24576, 576\n'

        def run(argv, **kw):
            if argv[0] == 'nvidia-smi':
                return subprocess.CompletedProcess(argv, 0, smi, '')
            return subprocess.run(argv, **kw)
        other = part.Leases(str(shared), shared=True)  # a pool run holds card 0 there
        try:
            self.assertEqual(other.try_take(1, [0], self.FREE, 0), [0])
            ex = part.Executor(self.inv, str(self.etc), {**self.env, 'CI_RUNNER_CLASS': 'gpu'}, release=str(ROOT),
                               scope=False, poll=0.1, run=run)
            _, out = ex.execute(part.Inputs(project='p', id='g', timeout_minutes=1, queue_timeout_minutes=0.02, gpus=1,
                                            command='true'))
        finally:
            other.release()
        self.assertEqual((out['outcome'], out['gpus']), ('pass', '1'))  # card 0 is the pool run's
        self.assertTrue((shared / 'card1.0.lock').exists())
        self.assertFalse((self.data / 'ci-runners/state/leases').exists())

    def test_what_it_creates_the_other_account_can_open(self):
        old = os.umask(0)  # the default ACL entries' effect: the mode asked for is the mode got
        try:
            for shared, want_dir, want_file in ((True, 0o770, 0o660), (False, 0o700, 0o600)):
                d = pathlib.Path(self.tmp.name) / f'l{int(shared)}'
                le = part.Leases(str(d), shared=shared)
                self.assertIsNone(le.try_take(1, [0], {0: 0}, 6000))  # waits: its ticket stays queued
                self.assertEqual(le.try_take(1, [0], self.FREE, 6000), [0])
                modes = {p.name: p.stat().st_mode & 0o777 for p in (d / 'queue', d / '.lock', d / 'card0.0.lock',
                                                                   d / 'queue' / 'seq')}
                self.assertEqual(modes, {'queue': want_dir, '.lock': want_file, 'card0.0.lock': want_file,
                                         'seq': want_file}, shared)
                le.release()
        finally:
            os.umask(old)

    def test_no_lease_is_taken_through_a_link(self):
        """Another account could plant a link in the shared directory: a lock file or the
        mutex that is a link is refused (O_NOFOLLOW), and its target is never created."""
        d = pathlib.Path(self.tmp.name) / 'l'
        le = part.Leases(str(d), shared=True)
        target = pathlib.Path(self.tmp.name) / 'target'
        for name in ('.lock', 'card0.0.lock'):
            (d / name).symlink_to(target)
            with self.assertRaises(OSError):
                le.try_take(1, [0], {0: 24000}, 0)
            self.assertFalse(target.exists())
            (d / name).unlink()
        le.release()

    def test_a_ticket_written_with_the_shared_mode(self):
        old = os.umask(0)
        try:
            le = part.Leases(str(pathlib.Path(self.tmp.name) / 'l'), shared=True)
            self.assertIsNone(le.try_take(1, [0], {0: 0}, 6000))
            self.assertEqual(os.stat(le.ticket).st_mode & 0o777, 0o660)
            le.release()
        finally:
            os.umask(old)

    def test_a_ticket_of_a_process_proc_hides_is_kept(self):
        """The CI account's /proc hides the pool runs' processes (decision 12.16): their
        tickets stay, as the slot queue keeps ci_slot.sh's."""
        d = pathlib.Path(self.tmp.name) / 'l'
        le = part.Leases(str(d), shared=True)
        q = d / 'queue'
        (q / '000000000007.json').write_text(json.dumps({'pid': 4242, 'start': int(time.time()), 'n': 1, 'cards': [0],
                                                         'need_mib': 0, 'every': False}))
        with unittest.mock.patch.object(slots, 'alive', lambda pid, start: False), \
                unittest.mock.patch.object(slots, 'unseen', lambda pid: pid == 4242):
            self.assertEqual([t.seq for t in le.tickets()], [7])
            self.assertIsNone(le.try_take(1, [0], self.FREE, 0))  # behind it, for card 0
        with unittest.mock.patch.object(slots, 'alive', lambda pid, start: False), \
                unittest.mock.patch.object(slots, 'unseen', lambda pid: False):
            le.tickets()
            self.assertFalse((q / '000000000007.json').exists())  # a dead one goes
            self.assertTrue(os.path.exists(le.ticket))  # ours stays
        le.release()

    def test_a_missing_shared_dir_is_not_made_in_a_sticky_one(self):
        sticky = pathlib.Path(self.tmp.name) / 'tmp'
        sticky.mkdir()
        sticky.chmod(0o1777)
        with self.assertRaises(part.locks.MissingSharedFile):
            part.Leases(str(sticky / 'leases'), shared=True)
        part.Leases(str(sticky / 'private'))  # the private one (state/leases) is made anywhere


class LeaseQueue(Host):
    """Card leases in ticket order (decision 12.6), on the lease files and tickets."""
    FREE = {0: 24000, 1: 24000}

    def leases(self):
        return part.Leases(str(self.data / 'ci-runners/state/leases'))

    def test_a_waiting_two_card_lease_holds_back_a_later_one_card_lease(self):
        x, a, b = self.leases(), self.leases(), self.leases()
        try:
            self.assertEqual(x.try_take(1, [1], self.FREE, 0), [1])  # a one-card part holds card 1
            self.assertIsNone(a.try_take(2, [0, 1], self.FREE, 0))  # the multi part waits
            self.assertIsNone(b.try_take(1, [0], self.FREE, 0))  # card 0 is free, but held back for it
            x.release()
            self.assertIsNone(b.try_take(1, [0], self.FREE, 0))  # still behind the multi part's ticket
            self.assertEqual(a.try_take(2, [0, 1], self.FREE, 0), [0, 1])
            self.assertFalse(os.path.exists(a.ticket or '/nonexistent'))
            self.assertIsNone(b.try_take(1, [0], self.FREE, 0))
            a.release()
            self.assertEqual(b.try_take(1, [0], self.FREE, 0), [0])
        finally:
            for le in (x, a, b):
                le.release()

    def test_every_takes_each_card_whole(self):
        """A nightly part (decision 12.8) holds every lease of its cards, where a card has more."""
        d = str(self.data / 'ci-runners/state/leases')
        night, other = part.Leases(d, max_parts=2), part.Leases(d, max_parts=2)
        try:
            self.assertEqual(night.try_take(2, [0, 1], self.FREE, 0, every=True), [0, 1])
            self.assertEqual(len(night.fds), 4)
            self.assertIsNone(other.try_take(1, [0, 1], self.FREE, 0))
        finally:
            night.release()
            other.release()

    def test_a_dead_or_malformed_ticket_holds_nothing_back(self):
        q = self.data / 'ci-runners/state/leases/queue'
        q.mkdir(parents=True)
        (q / '000000000001.json').write_text(json.dumps({'pid': 999999999, 'start': 1, 'n': 2, 'cards': [0, 1],
                                                         'need_mib': 0, 'every': False}))
        (q / '000000000002.json').write_text('{"pid": "x"}')
        b = self.leases()
        try:
            self.assertEqual(b.try_take(1, [0], self.FREE, 0), [0])
            self.assertEqual(b.seq, 3)  # after the others
            self.assertEqual(sorted(os.listdir(q)), ['seq'])
        finally:
            b.release()


class EnvLink(Host):
    """The project's env is a link that ``env refresh`` moves (design §4.9, decision 12.11):
    a part runs in the build the link named at its start, and holds it."""

    def setUp(self):
        super().setUp()
        self.envs = self.data / 'ci-runners' / 'envs'
        for b in ('p-20261107T000001Z', 'p-20261107T000002Z'):
            (self.envs / b / 'bin').mkdir(parents=True)
        self.link = self.envs / 'p'
        self.link.symlink_to('p-20261107T000001Z')
        entry = self.etc / 'projects' / 'p.toml'
        entry.write_text(entry.read_text().replace('/opt/env-p', str(self.link)))
        self.probe = pathlib.Path(self.tmp.name) / 'probe.py'
        self.probe.write_text('import fcntl, sys\nf = open(sys.argv[1])\ntry:\n'
                              '    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)\n    print("free")\n'
                              'except OSError:\n    print("held")\n')

    def test_the_part_resolves_the_link_once_and_names_the_build(self):
        a, b = 'p-20261107T000001Z', 'p-20261107T000002Z'
        # the link moves while the part runs: its later commands must see the first env
        code, out = self.execute(command=f'ln -sfn {b} {self.link}; env > "$CI_PART_DIR/env.txt"')
        self.assertEqual(code, 0, out)
        rec = self.path(out)
        env = dict(x.split('=', 1) for x in pathlib.Path(rec, 'work', 'env.txt').read_text().splitlines() if '=' in x)
        self.assertEqual(env['CONDA_PREFIX'], str(self.envs / a))  # the build, not the link
        self.assertTrue(env['PATH'].startswith(f'{self.envs / a}/bin:'), env['PATH'])
        self.assertEqual(json.loads(pathlib.Path(rec, 'record.json').read_text())['env_build'], a)
        self.assertEqual(os.readlink(self.link), b)  # and the link did move

    def test_the_build_is_held_for_the_parts_life_and_released_after(self):
        a = self.envs / 'p-20261107T000001Z'
        code, out = self.execute(command=f'{sys.executable} {self.probe} {a}/.inuse > "$CI_PART_DIR/lock.txt"')
        self.assertEqual(code, 0, out)
        self.assertEqual(pathlib.Path(self.path(out), 'work', 'lock.txt').read_text().strip(), 'held')
        self.assertTrue(envuse.remove_unused(str(a)))  # the part is over: nothing holds it
        self.assertFalse(a.exists())

    def test_the_supervisor_holds_the_build_after_the_executor_is_gone(self):
        code = (f'import sys, json; sys.path.insert(0, {str(ROOT)!r}); from ci_runners import inventory, part; '
                f'inv = inventory.load(open({str(self.tmp.name + "/inv.toml")!r}).read()); '
                f'ex = part.Executor(inv, {str(self.etc)!r}, json.loads(sys.argv[1]), release={str(ROOT)!r}, '
                f'scope=False, poll=0.1); '
                f'ex.execute(part.Inputs(project="p", id="long", timeout_minutes=1, command="sleep 2"))')
        pathlib.Path(self.tmp.name, 'inv.toml').write_text(self.inv_text)
        child = subprocess.Popen([sys.executable, '-I', '-c', code, json.dumps(self.env)], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
        rec = self.data / 'ci-runners/state/parts/org/repo/77-1/long-1'
        for _ in range(100):
            if (rec / 'scope.json').exists():
                break
            time.sleep(0.05)
        os.killpg(child.pid, signal.SIGTERM)  # the runner stops
        child.wait(timeout=20)
        build = self.envs / 'p-20261107T000001Z'
        self.assertFalse(envuse.remove_unused(str(build)))  # the supervisor still runs the command in it
        self.assertTrue(build.is_dir())
        for _ in range(200):
            if (rec / 'exit.json').exists():
                break
            time.sleep(0.05)
        time.sleep(0.3)  # the supervisor exits after writing exit.json
        self.assertTrue(envuse.remove_unused(str(build)))

    def test_a_part_with_no_env_directory_is_as_before(self):
        entry = self.etc / 'projects' / 'p.toml'
        entry.write_text(entry.read_text().replace(str(self.link), '/opt/env-p'))
        code, out = self.execute(command='env > "$CI_PART_DIR/env.txt"')
        env = dict(x.split('=', 1) for x in pathlib.Path(self.path(out), 'work', 'env.txt').read_text().splitlines() if '=' in x)
        self.assertEqual(env['CONDA_PREFIX'], '/opt/env-p')
        self.assertEqual(json.loads(pathlib.Path(self.path(out), 'record.json').read_text())['env_build'], 'env-p')


class OwnCap(Host):
    """The supervisor's memory cap (design decision 11.11), outside a scope: the command and
    its descendants count."""
    HOG = 'python3 -c "import time; b = b\'x\' * (160 << 20); time.sleep(30)"'

    def execute_capped(self, own_cap, **kw):
        ex = part.Executor(self.inv, str(self.etc), self.env, release=str(ROOT), scope=False, poll=0.1,
                           own_cap=own_cap)
        kw.setdefault('timeout_minutes', 1)
        kw.setdefault('queue_timeout_minutes', 0.02)
        return ex.execute(part.Inputs(project='p', id='hog', **kw))

    def test_the_executor_reads_the_systemd_version(self):
        def run_with(text):
            return lambda argv, **kw: subprocess.CompletedProcess(argv, 0, text, '')
        old = part.Executor(self.inv, str(self.etc), self.env, release=str(ROOT), run=run_with('systemd 252 (252)\n'))
        new = part.Executor(self.inv, str(self.etc), self.env, release=str(ROOT), run=run_with('systemd 253 (253)\n'))
        self.assertEqual((old.oom_policy(), new.oom_policy()), (False, True))

    def test_a_part_over_its_memory_ends_oom(self):
        t0 = time.monotonic()
        code, out = self.execute_capped(True, command=f'{self.HOG} & wait', memory='64M')
        self.assertEqual((code, out['outcome']), (0, 'oom'))
        self.assertLess(time.monotonic() - t0, 20)  # killed, not timed out

    def test_under_its_memory_it_runs(self):
        code, out = self.execute_capped(True, command='python3 -c "b = b\'x\' * (8 << 20)"', memory='64M')
        self.assertEqual(out['outcome'], 'pass')

    def test_without_the_own_cap_nothing_is_killed(self):
        code, out = self.execute_capped(False, command=self.HOG.replace('30', '3'), memory='64M')  # past a reading
        self.assertEqual(out['outcome'], 'pass')


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
        lookalike = pathlib.Path(self.tmp.name, 'evil/org/repo/77-1/unit-1')  # shaped like one of ours
        lookalike.mkdir(parents=True)
        for n in ('record.json', 'outcome.json', 'exit.json'):
            (lookalike / n).write_bytes((outside / n).read_bytes())
        for ref in (f'hb:{outside}', 'hb:../../../outside', 'hb:org/repo/77-1/linked', 'other:org/repo/77-1/unit-1',
                    f'hb:{lookalike}', 'hb:../../../../evil/org/repo/77-1/unit-1'):
            with self.assertRaises(part.Refused, msg=ref):
                part.salvage(self.inv, ref, self.env)

    def test_lost_is_not_kept_while_the_record_may_finish(self):
        _, out = self.execute(command='exit 0')
        rec = self.path(out)
        os.unlink(os.path.join(rec, 'outcome.json'))
        exit_json = pathlib.Path(rec, 'exit.json')
        saved = exit_json.read_bytes()
        exit_json.unlink()
        self.assertEqual(part.salvage(self.inv, out['record'], self.env)[1]['outcome'], 'lost')
        exit_json.write_bytes(saved)  # it finished after all
        self.assertEqual(part.salvage(self.inv, out['record'], self.env)[1]['outcome'], 'pass')

    def test_another_repository_cannot_salvage(self):
        _, out = self.execute(command='exit 0')
        with self.assertRaises(part.Refused):
            part.salvage(self.inv, out['record'], {**self.env, 'GITHUB_REPOSITORY': 'org/other'})


class Pure(unittest.TestCase):
    REC = {'class': 'part', 'host': 'hb', 'attempt': 1, 'retries': 1, 'boot_start': 'b1', 'run_epoch': 100.0}

    def test_oom_retries_once_with_resume(self):
        out = part.finish('/r', self.REC, {'rc': 137, 'oom_kills': 1, 'boot_end': 'b1', 'end_epoch': 160.0})
        self.assertEqual((out['outcome'], out['next'], out['resume'], out['run-seconds']), ('oom', 'retry', True, 60))
        out = part.finish('/r', self.REC, {'rc': 137, 'boot_end': 'b1'}, unit_result='oom-kill')
        self.assertEqual(out['outcome'], 'oom')
        out = part.finish('/r', {**self.REC, 'attempt': 2}, {'rc': 137, 'oom_kills': 1, 'boot_end': 'b1'})
        self.assertEqual((out['outcome'], out['next']), ('oom', 'none'))  # the second end is the consumer's

    def test_a_pool_with_its_own_labels_retries_on_them(self):
        """RUN-5: `part` is not a label of a host's CPU runners; `3090b-cpu` is."""
        rec = {**self.REC, 'host': '3090b', 'pool_labels': ['3090b-cpu']}
        self.assertEqual(part.finish('/r', rec, {'rc': 0, 'boot_end': 'b2'})['retry-on'], ['self-hosted', '3090b-cpu'])
        self.assertEqual(part.finish('/r', rec, None)['retry-on'], ['self-hosted', '3090b-cpu'])
        out = part.finish('/r', rec, None, unavailable='low disk')
        self.assertEqual((out['next'], out['retry-on']), ('fallback', ['self-hosted', '3090b-cpu']))
        out = part.finish('/r', {**rec, 'fallback': ['self-hosted', 'x']}, None, unavailable='low disk')
        self.assertEqual(out['retry-on'], ['self-hosted', 'x'])  # the registry's fallback still wins

    def test_reboot_stopped_and_lost(self):
        self.assertEqual(part.finish('/r', self.REC, {'rc': 0, 'boot_end': 'b2'})['retry-on'],
                         ['self-hosted', 'part', 'hb'])  # a reboot retries on the same host
        self.assertEqual(part.finish('/r', self.REC, {'rc': 143, 'boot_end': 'b1'}, stopped=True)['outcome'], 'stopped')
        self.assertEqual(part.finish('/r', self.REC, None)['outcome'], 'lost')
        stopped = part.finish('/r', self.REC, {'rc': -15, 'signalled': True, 'boot_end': 'b1'})
        self.assertEqual((stopped['outcome'], stopped['next']), ('stopped', 'none'))  # a scope stopped from outside
        self.assertEqual(part.finish('/r', self.REC, {'rc': -15, 'signalled': True, 'timed_out': True,
                                                      'boot_end': 'b1'})['outcome'], 'red')

    def test_scope_and_command(self):
        argv = part.scope_argv('ci-part-r-x-1.scope', 'p', '4G', ['sup'])
        for want in ('--user', '--scope', '--unit=ci-part-r-x-1.scope', '--slice=ci-p.slice', 'MemoryMax=4G',
                     'MemorySwapMax=0', 'OOMPolicy=continue'):
            self.assertIn(want, argv)
        self.assertEqual(argv[-1], 'sup')
        self.assertEqual(part.command_argv('c'), ['nice', '-n', '10', 'ionice', '-c', '2', '-n', '7', 'bash', '-c', 'c'])
        self.assertEqual(part.unit_name('a b', 'x', '7-1-1'), 'ci-part-a_b-x-7-1-1.scope')

    def test_cpu_list_is_a_cpuset_list_as_the_inventory_writes_it(self):
        self.assertEqual(part.cpu_list({0, 1, 2, 3, 8, 9}), '0-3,8-9')
        self.assertEqual(part.cpu_list([9, 8, 3, 2, 1, 0, 2]), '0-3,8-9')  # any order, once each
        self.assertEqual(part.cpu_list({5}), '5')  # one CPU: never a count
        self.assertEqual(part.cpu_list({0, 2, 4, 5}), '0,2,4-5')
        self.assertEqual(part.cpu_list(set()), '')

    def test_oom_policy_only_where_systemd_takes_it(self):
        self.assertNotIn('OOMPolicy=continue', part.scope_argv('u.scope', 'p', '4G', ['sup'], oom_policy=False))
        self.assertEqual(part.scope_argv('u.scope', 'p', '4G', ['sup'], oom_policy=False)[-1], 'sup')
        self.assertEqual(part.systemd_major('systemd 249 (249.11-0ubuntu3.23)\n+PAM'), 249)
        self.assertEqual(part.systemd_major('systemd 255 (255.4-1ubuntu8)'), 255)
        self.assertEqual(part.systemd_major('nothing'), 0)


    def test_where_systemd_enforces_memory(self):
        files = {'/user.slice/user-5.slice/user@5.service/cgroup.subtree_control': 'cpu io memory pids\n'}

        def read(path):
            if path not in files:
                raise FileNotFoundError(path)
            return files[path]
        unified = '0::/user.slice/user-5.slice/user@5.service/ci.slice/ci-runner@x.service\n'
        self.assertTrue(part.memory_delegated(unified, read))
        hybrid = '1:name=systemd:/user.slice/user-5.slice/user@5.service/ci.slice\n' + unified
        self.assertFalse(part.memory_delegated(hybrid, read))  # v1 or hybrid: never
        self.assertFalse(part.memory_delegated(unified + '1:name=systemd:/user.slice\n', read))
        self.assertFalse(part.memory_delegated('0::/system.slice/runner.service\n', read))  # no user manager
        files['/user.slice/user-5.slice/user@5.service/cgroup.subtree_control'] = 'cpu pids\n'
        self.assertFalse(part.memory_delegated(unified, read))
        self.assertFalse(part.memory_delegated('0::/user.slice/user-6.slice/user@6.service/a\n', read))  # unread

    def test_the_scope_procs_file(self):
        scope = '/user.slice/user-5.slice/user@5.service/ci.slice/ci-p.slice/ci-part-r-x-7-1-1.scope'
        self.assertEqual(part.scope_procs_file(f'0::{scope}\n'), f'/sys/fs/cgroup{scope}/cgroup.procs')
        self.assertEqual(part.scope_procs_file(f'11:memory:/user.slice\n1:name=systemd:{scope}\n0::{scope}\n'),
                         f'/sys/fs/cgroup/systemd{scope}/cgroup.procs')
        self.assertEqual(part.scope_procs_file(f'0::{scope}\n1:name=systemd:{scope}\n'),
                         f'/sys/fs/cgroup/systemd{scope}/cgroup.procs')
        self.assertEqual(part.scope_procs_file('0::/user.slice/user-5.slice/session-3.scope\n'), '')
        self.assertEqual(part.rss_anon('Name:\tx\nRssAnon:\t  2048 kB\nRssFile:\t 9 kB\n'), 2 << 20)
        self.assertEqual(part.rss_anon('Name:\tkthreadd\n'), 0)

    def test_outputs_are_one_line_each(self):
        with tempfile.NamedTemporaryFile('r') as f:
            part.write_outputs({'outcome': 'pass', 'retry-on': ['a', 'b']}, f.name)
            text = f.read()
        self.assertIn('retry-on=["a", "b"]\n', text)
        self.assertNotIn('verify', text)
        with self.assertRaises(ValueError):
            part.write_outputs({'reason': 'a\nb'}, '/dev/null')

    def test_a_command_that_ignores_term_is_killed(self):
        with tempfile.TemporaryDirectory() as d:
            r = subprocess.run(['python3', '-I', '-c', 'import sys; sys.path.insert(0, sys.argv.pop(1)); '
                                'from ci_runners import part; part.KILL_GRACE = 1; '
                                'sys.exit(part.supervise(sys.argv[1], 1, ["bash", "-c", "trap \'\' TERM; sleep 30"]))',
                                str(ROOT), d], capture_output=True, text=True, timeout=25)
            ex = json.loads(pathlib.Path(d, 'exit.json').read_text())
            self.assertEqual((r.returncode, ex['rc'], ex['timed_out']), (128 + 9, -9, True))

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
               'PATH var': ({'hosts': {'hb': {'memory_max': '2G', 'variables': {'PATH': '/x'}}}}, 'p'),
               'LD var': ({'hosts': {'hb': {'memory_max': '2G', 'variables': {'LD_BIND_NOW': '1'}}}}, 'p'),
               'default retired': ({'hosts': {'hb': {'memory_max': '2G', 'default': True}}}, 'p'),
               'multi-line var': ({'hosts': {'hb': {'memory_max': '2G', 'variables': {'A': 'x\ny'}}}}, 'p'),
               'fallback': ({'fallback': {'part': []}}, 'p')}
        for what, (change, stem) in bad.items():
            with self.assertRaises(registry.RegistryError, msg=what):
                registry.parse({**self.BASE, **change}, 'hb', stem)


if __name__ == '__main__':
    unittest.main()
