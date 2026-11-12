"""A part runner is one slot (design decision 16.2, RUN-10's R2): on a runner whose pool has a
``slot`` table and whose ``.env`` names its slot (``CI_RUNNER_SLOT``), the part is pinned to
the slot's cpuset, capped at its memory, runs its command with ``CI_WORKERS``, ``CI_SPEED``
and ``CI_ISA``, and has an inner timeout of its minutes times the slot's ``speed``; a runner
without a slot runs exactly as before. On a fake host tree, without systemd (the
supervisor runs directly), or with ``spawn`` captured to read the scope's argv."""
from __future__ import annotations

import fcntl
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

from ci_runners import inventory, part

ROOT = pathlib.Path(__file__).resolve().parents[1]
#: the supervisor's ``-c`` program, as the executor has written it since the own cap (11.11)
SUPERVISOR = ('import json, sys; sys.path.insert(0, sys.argv.pop(1)); from ci_runners.part import supervise; '
              'sys.exit(supervise(sys.argv[1], float(sys.argv[2]), sys.argv[5:], int(sys.argv[3]), '
              'json.loads(sys.argv[4])))')
SMI = {'--query-gpu=index,memory': '0, 24576, 576\n1, 24576, 576\n',
       '--query-gpu=index,pci.bus_id': '0, 00000000:01:00.0, 40, 0\n1, 00000000:02:00.0, 41, 0\n'}
#: the pools of the test host: two CPU slots with an ``isa``, a per-card pool with one slot
#: per card and none, and a compile runner without a slot
SLOTTED = ('pools = { light = 1, part = { count = 2, slot = { cpus = ["0", "1"], workers = 3, memory = "1500M", '
           'speed = 2.5, isa = "x86-64-v3" } }, cards = { per_card = true, class = "gpu", labels = ["hb-gpu"], '
           'slot = { cpus = ["1", "0"], workers = 2, memory = "4G", speed = 1 } }, compile = 1 }')
UNSLOTTED = ('pools = { light = 1, part = 2, cards = { per_card = true, class = "gpu", labels = ["hb-gpu"] }, '
             'compile = 1 }')


class FakeProc:
    pid = 0

    def wait(self):
        return 0

    def poll(self):
        return 0


class SlotHost(unittest.TestCase):
    def setUp(self):
        if not {0, 1} <= os.sched_getaffinity(0):
            self.skipTest('needs CPUs 0 and 1')
        self.tmp = tempfile.TemporaryDirectory()
        t = pathlib.Path(self.tmp.name)
        self.data, self.etc, self.ws = t / 'data', t / 'data' / 'ci-runners' / 'etc', t / 'ws'
        for d in (self.etc / 'projects', self.ws, t / 'locks'):
            d.mkdir(parents=True)
        self.slot = t / 'locks' / 's.lock'
        for n in ('s.lock', 's.2.lock'):
            (t / 'locks' / n).write_text('')
        self.base = f'''schema = 1
this = "hb"
[hosts.hb]
account = "acct-x"
home = "{t}"
data = "{self.data}"
slots = 2
memory_max = "120G"
disk_min_gib = 0
gpus = [0, 1]
max_temp_c = 80
cpus = "1"
compile_cpus = "0"
{UNSLOTTED}
files = {{ slot_lock = "{self.slot}", slot_queue = "{t}/locks/s.queue" }}
'''
        self.slotted = self.base.replace(UNSLOTTED, SLOTTED)
        self.inv = inventory.load(self.slotted)
        (self.etc / 'projects' / 'p.toml').write_text(
            'name = "p"\nrepository = "Org/Repo"\ncontext = "p/ci"\nmemory = "1G"\n'
            '[hosts.hb]\nmemory_max = "2G"\nenv = "/opt/env-p"\nvariables = { P_HOST = "hb" }\n')
        self.env = {'GITHUB_REPOSITORY': 'org/repo', 'GITHUB_RUN_ID': '77', 'GITHUB_RUN_ATTEMPT': '1',
                    'GITHUB_WORKSPACE': str(self.ws), 'RUNNER_NAME': 'hb-part-1', 'CI_RUNNER_CLASS': 'part',
                    'CI_RUNNERS_ETC': 'from-the-runner', 'GITHUB_OUTPUT': str(t / 'github_output'),
                    'PATH': '/usr/bin:/bin', 'HOME': str(t)}

    def tearDown(self):
        self.tmp.cleanup()

    def runner(self, cls='part', slot=None, card=''):
        env = {**self.env, 'CI_RUNNER_CLASS': cls, 'RUNNER_NAME': f'hb-{cls}-1'}
        if card:
            env['CI_RUNNER_CARD'] = card
        if slot is not None:
            env['CI_RUNNER_SLOT'] = slot
        return env

    @staticmethod
    def fake_run(argv, **kw):
        if argv[0] == 'nvidia-smi':
            return subprocess.CompletedProcess(argv, 0, next((v for k, v in SMI.items() if argv[1].startswith(k)), ''), '')
        if argv[:2] == ['systemctl', '--version']:
            return subprocess.CompletedProcess(argv, 0, 'systemd 255 (255)\n', '')
        return subprocess.CompletedProcess(argv, 0, '', '')

    def go(self, env, **kw):
        """Run a part for real (no systemd): the command sees its pin and environment."""
        kw.setdefault('project', 'p')
        kw.setdefault('id', 'c')
        kw.setdefault('timeout_minutes', 1)
        kw.setdefault('queue_timeout_minutes', 0.02)
        kw.setdefault('command', 'touch "$CI_PART_DIR/ran"')
        ex = part.Executor(self.inv, str(self.etc), env, release=str(ROOT), scope=False, poll=0.05, run=self.fake_run,
                           sample_s=0.05, own_cap=kw.pop('own_cap', None))
        return ex.execute(part.Inputs(**kw))

    def spawned(self, env, own_cap=True, **kw):
        """The scope's argv and the command's environment of a part, as the executor spawns
        them (``scope=True``, nothing run), and its record."""
        got = {}

        def spawn(argv, **skw):
            got.update(argv=list(argv), env=dict(skw['env']), cwd=skw['cwd'])
            return FakeProc()
        kw.setdefault('project', 'p')
        kw.setdefault('id', 'c')
        kw.setdefault('timeout_minutes', 2.0)  # a float, as the action's argparse gives it
        kw.setdefault('queue_timeout_minutes', 0.02)
        kw.setdefault('command', 'pytest -q')
        ex = part.Executor(self.inv, str(self.etc), env, release=str(ROOT), scope=True, poll=0.05, run=self.fake_run,
                           spawn=spawn, own_cap=own_cap, sample_s=3600)
        _, out = ex.execute(part.Inputs(**kw))
        rec = json.loads(pathlib.Path(self.path(out), 'record.json').read_text())
        return got, rec, out

    def path(self, out):
        return str(self.data / 'ci-runners/state/parts' / out['record'].split(':', 1)[1])

    def work(self, out, name):
        return pathlib.Path(self.path(out), 'work', name).read_text().strip()


class AsBefore(SlotHost):
    """A runner without a slot runs exactly as before R2: the same scope argv (pin, memory
    cap, inner timeout), the same environment and the same record, whether its pool has no
    ``slot`` table or has one and the runner's ``.env`` names no slot."""

    def golden(self, cls, record, *, pin, memory, cap, timeout, cards='', held='', command='pytest -q', p2p='[]'):
        """The argv and the environment the executor gave a part before R2, written out."""
        unit = f'ci-part-hb-{cls}-1-c-77-1-1.scope'
        argv = ['systemd-run', '--user', '--scope', '--quiet', f'--unit={unit}', '--slice=ci-p.slice',
                '-p', f'MemoryMax={memory}', '-p', 'MemorySwapMax=0', '-p', 'OOMPolicy=continue',
                sys.executable, '-I', '-c', SUPERVISOR, str(ROOT), record, timeout, str(cap), p2p,
                'taskset', '-c', pin, 'nice', '-n', '0' if cls == 'compile' else '10', 'ionice', '-c', '2', '-n', '7',
                'bash', '-c', command]
        env = {'GITHUB_REPOSITORY': 'org/repo', 'GITHUB_RUN_ID': '77', 'GITHUB_RUN_ATTEMPT': '1',
               'GITHUB_WORKSPACE': str(self.ws), 'RUNNER_NAME': f'hb-{cls}-1', 'HOME': self.env['HOME'],
               'P_HOST': 'hb', 'PATH': '/opt/env-p/bin:/usr/bin:/bin', 'CONDA_PREFIX': '/opt/env-p',
               'CI_RUNNERS_HOST': 'hb', 'CI_PROJECT': 'p', 'CI_PART_DIR': f'{record}/work',
               'CI_RESULTS_DIR': f'{record}/work/results', 'CUDA_VISIBLE_DEVICES': cards,
               'CUDA_DEVICE_ORDER': 'PCI_BUS_ID', 'CI_CPUS': pin, 'CI_SLOT_LOCK': str(self.slot),
               'CI_SLOT_QUEUE': str(self.slot.parent / 's.queue'), 'CI_SLOT_HELD': held, 'CI_RESUME': '0'}
        return argv, env

    def test_a_runner_without_a_slot_runs_as_before(self):
        cases = [  # (class, card, inputs, pin, MemoryMax, own cap, inner timeout in s, takes a CI slot)
            ('part', '', {}, '1', '1G', 1 << 30, '120.0', True),
            ('part', '', {'memory': '2G', 'timeout_minutes': 0.5}, '1', '2G', 2 << 30, '30.0', True),
            ('gpu', '1', {'gpus': 1}, '1', '1G', 1 << 30, '120.0', False),
        ]
        for text, slot in ((self.base, None), (self.slotted, None), (self.slotted, '')):
            self.inv = inventory.load(text)
            for n, (cls, card, kw, pin, memory, cap, timeout, takes) in enumerate(cases):
                with self.subTest(slotted=text is self.slotted, slot=slot, cls=cls, n=n):
                    got, rec, out = self.spawned(self.runner(cls, slot, card), id=f'c{n}', **kw)
                    record = self.path(out)
                    argv, env = self.golden(cls, record, pin=pin, memory=memory, cap=cap, timeout=timeout,
                                            cards='1' if card else '', held=str(self.slot) if takes else '')
                    argv[4] = f'--unit=ci-part-hb-{cls}-1-c{n}-77-1-1.scope'
                    self.assertEqual(got['argv'], argv)
                    self.assertEqual(got['env'], env)
                    self.assertNotIn('runner_slot', rec)
                    self.assertEqual(set(rec) - {'leased', 'pids', 'buses', 'early_stop', 'peak_c'},
                                     {'project', 'id', 'class', 'host', 'runner', 'attempt', 'retries', 'fallback',
                                      'inputs', 'start_epoch', 'boot_start', 'executor', 'release', 'lock',
                                      'queue_seconds', 'cards', 'slot', 'run_epoch', 'env_build', 'unit',
                                      'pool_labels'} - (set() if cls == 'gpu' else {'pool_labels'}))
            shutil.rmtree(self.data / 'ci-runners/state/parts')  # the same ids again

    def test_without_the_own_cap_the_supervisor_caps_nothing_as_before(self):
        self.inv = inventory.load(self.base)
        got, _, out = self.spawned(self.runner(), own_cap=False)
        self.assertEqual(got['argv'][self.argv_at(got['argv'], 3)], '0')

    @staticmethod
    def argv_at(argv, k):
        """The index of the supervisor's ``k``-th argument (1: the record, 2: the timeout, 3: the cap)."""
        return argv.index(str(ROOT)) + k


class Slots(SlotHost):
    """On each slot of the test inventory, the pin, the cap, the environment and the inner
    timeout are the slot's."""

    def test_each_slot_pins_its_part_and_sets_its_environment(self):
        probe = ('grep Cpus_allowed_list /proc/self/status | cut -f2 > "$CI_PART_DIR/cpus"; '
                 'echo "$CI_CPUS ${CI_WORKERS-none} ${CI_SPEED-none} ${CI_ISA-none}" > "$CI_PART_DIR/env"')
        for cls, card, slot, cpus, env in (('part', '', '0', '0', '3 2.5 x86-64-v3'),
                                           ('part', '', '1', '1', '3 2.5 x86-64-v3'),
                                           ('gpu', '0', '0', '1', '2 1.0 none'),
                                           ('gpu', '1', '1', '0', '2 1.0 none')):
            with self.subTest(cls=cls, slot=slot):
                _, out = self.go(self.runner(cls, slot, card), command=probe, id=f'{cls}{slot}',
                                 gpus=1 if card else 0)
                self.assertEqual(out['outcome'], 'pass', out['reason'])
                self.assertEqual(self.work(out, 'cpus'), cpus)  # enforced: taskset to the slot's cpuset
                self.assertEqual(self.work(out, 'env'), f'{cpus} {env}')
        _, out = self.go(self.runner('part'), command=probe, id='none')  # no CI_RUNNER_SLOT: the host's pin
        self.assertEqual((self.work(out, 'cpus'), self.work(out, 'env')), ('1', '1 none none none'))

    def test_the_job_cannot_set_the_slot_variables(self):
        env = {**self.runner('part', '0'), 'CI_WORKERS': '64', 'CI_SPEED': '9', 'CI_ISA': 'any', 'CI_CPUS': '0-63'}
        got, _, _ = self.spawned(env)
        self.assertEqual([got['env'][k] for k in ('CI_WORKERS', 'CI_SPEED', 'CI_ISA', 'CI_CPUS')],
                         ['3', '2.5', 'x86-64-v3', '0'])
        got, _, _ = self.spawned({**env, 'CI_RUNNER_SLOT': ''}, id='none')
        self.assertEqual([got['env'].get(k) for k in ('CI_WORKERS', 'CI_SPEED', 'CI_ISA', 'CI_CPUS')],
                         [None, None, None, '1'])

    def test_a_slotted_part_is_capped_at_its_slots_memory(self):
        at = AsBefore.argv_at
        for slot_mem, kw, want in (('1500M', {}, '1500M'),  # the slot's, not the registry's default (1G)
                                   ('1500M', {'memory': '1G'}, '1G'),  # the part asks for less
                                   ('1500M', {'memory': '2G'}, '1500M'),  # never more than the slot
                                   ('4G', {}, '2G')):  # never above the registry's maximum
            with self.subTest(slot_mem=slot_mem, kw=kw):
                self.inv = inventory.load(self.slotted.replace('memory = "1500M"', f'memory = "{slot_mem}"'))
                got, rec, _ = self.spawned(self.runner('part', '1'), id=f'm{slot_mem}{len(kw)}{kw.get("memory", "")}',
                                           **kw)
                self.assertIn(f'MemoryMax={want}', got['argv'])
                self.assertEqual(got['argv'][at(got['argv'], 3)], str(part.registry.memory_bytes(want)))
                self.assertEqual(rec['runner_slot']['memory'], want)

    def test_the_supervisor_enforces_the_slots_memory_without_a_controller(self):
        """Decision 11.11: where the user manager has no memory controller, the supervisor
        kills a part over its slot's cap."""
        self.inv = inventory.load(self.slotted.replace('memory = "1500M"', 'memory = "64M"'))
        hog = 'python3 -c "import time; b = b\'x\' * (160 << 20); time.sleep(30)"'
        _, out = self.go(self.runner('part', '0'), command=f'{hog} & wait', own_cap=True)
        self.assertEqual(out['outcome'], 'oom')
        self.inv = inventory.load(self.slotted)  # 1500M: the same command runs past a reading
        _, out = self.go(self.runner('part', '0'), command=hog.replace('30', '3'), own_cap=True, id='fits')
        self.assertEqual(out['outcome'], 'pass')

    def test_the_inner_timeout_is_the_minutes_times_the_slots_speed(self):
        at = AsBefore.argv_at
        got, rec, _ = self.spawned(self.runner('part', '0'), timeout_minutes=2)
        self.assertEqual(float(got['argv'][at(got['argv'], 2)]), 300.0)  # 2 min x 2.5
        self.assertEqual(rec['inputs']['timeout_minutes'], 2)  # the plan's minutes, as given
        self.assertEqual(rec['runner_slot']['timeout_minutes'], 5.0)
        # for real: 0.03 min is 1.8 s, and 4.5 s at speed 2.5; a 3-s command passes only on the slot
        _, out = self.go(self.runner('part', '0'), command='sleep 3', timeout_minutes=0.03, id='slow')
        self.assertEqual(out['outcome'], 'pass', out['reason'])
        _, out = self.go(self.runner('part'), command='sleep 3', timeout_minutes=0.03, id='plain')
        self.assertEqual((out['outcome'], out['reason']), ('red', 'the inner timeout ended the command'))

    def test_the_record_keeps_the_slot(self):
        _, rec, _ = self.spawned(self.runner('part', '1'))
        self.assertEqual(rec['runner_slot'], {'index': 1, 'cpus': '1', 'workers': 3, 'memory': '1500M', 'speed': 2.5,
                                              'isa': 'x86-64-v3', 'timeout_minutes': 5.0})
        _, rec, _ = self.spawned(self.runner('gpu', '0', '0'), gpus=1, id='g')
        self.assertEqual(rec['runner_slot'], {'index': 0, 'cpus': '1', 'workers': 2, 'memory': '2G', 'speed': 1.0,
                                              'timeout_minutes': 2.0})

    def test_a_slot_the_inventory_does_not_give_is_unavailable(self):
        """Never a silent fallback to the runner without a slot: the part ends unavailable
        before it takes anything, with the reason."""
        cases = [(self.slotted, 'part', '', '2', 'CI_RUNNER_SLOT=2 is out of range: the part pool has 2 slots'),
                 (self.slotted, 'part', '', '01', "CI_RUNNER_SLOT '01' is no slot index"),
                 (self.slotted, 'part', '', '-1', "CI_RUNNER_SLOT '-1' is no slot index"),
                 (self.slotted, 'part', '', 'x', "CI_RUNNER_SLOT 'x' is no slot index"),
                 (self.base, 'part', '', '0', 'CI_RUNNER_SLOT=0, but the part pool here has no slot table'),
                 (self.slotted, 'light', '', '0', 'CI_RUNNER_SLOT=0, but the light pool here has no slot table'),
                 (self.slotted, 'gpu', '0', '1', "CI_RUNNER_SLOT=1 is the slot of card 1, not of CI_RUNNER_CARD's"),
                 (self.slotted.replace('["0", "1"]', '["0", "4000"]'), 'part', '', '1',
                  "slot 1's cpus 4000 are not all CPUs this runner may use")]
        for n, (text, cls, card, slot, why) in enumerate(cases):
            with self.subTest(slot=slot, why=why):
                self.inv = inventory.load(text)
                _, out = self.go(self.runner(cls, slot, card), id=f'u{n}', gpus=1 if card else 0)
                self.assertEqual((out['outcome'], out['reason'], out['next']), ('unavailable', f'slot: {why}', 'fallback'))
                self.assertFalse(pathlib.Path(self.path(out), 'work', 'ran').exists())
        queue = self.slot.parent / 's.queue'
        self.assertEqual(sorted(os.listdir(queue)) if queue.exists() else [], [])  # no ticket was taken

    def test_a_slotted_part_still_takes_a_ci_slot(self):
        """Until v2 (16.2, §7): the slot files admit a CPU part, as they admit a local run;
        the slot table gives its CPUs. A part on runner slot 1 takes whichever CI slot is
        free and runs on slot 1's cpuset; with every CI slot held it waits, then ends
        unavailable, however free its runner's slot is."""
        probe = 'echo "$CI_CPUS $CI_SLOT_HELD" > "$CI_PART_DIR/env"'
        _, out = self.go(self.runner('part', '1'), command=probe)
        self.assertEqual(self.work(out, 'env'), f'1 {self.slot}')  # CI slot file 0, runner slot 1
        fds = [os.open(self.slot.parent / n, os.O_WRONLY) for n in ('s.lock', 's.2.lock')]
        try:
            for fd in fds:
                fcntl.flock(fd, fcntl.LOCK_EX)  # a local run's ci_slot.sh holds both
            _, out = self.go(self.runner('part', '0'), id='held')
        finally:
            for fd in fds:
                os.close(fd)
        self.assertEqual((out['outcome'], out['reason']), ('unavailable', 'queue timeout: no CI slot'))
        _, out = self.go(self.runner('gpu', '0', '0'), id='gpu', gpus=1)  # a card's lease is its slot: no CI slot
        self.assertEqual((out['outcome'], json.loads(pathlib.Path(self.path(out), 'record.json').read_text())['slot']),
                         ('pass', ''))


class Pure(unittest.TestCase):
    HOST = {'gpus': [0, 1], 'pools': {'part': {'count': 2, 'slot': {'cpus': ['0-3', '4-7'], 'workers': 4,
                                                                    'memory': '8G', 'speed': 2}},
                                      'light': 1}}

    def test_runner_slot(self):
        s, why = part.runner_slot(self.HOST, 'part', {'CI_RUNNER_SLOT': '1'})
        self.assertEqual((s, why), (part.RunnerSlot(1, '4-7', 4, '8G', 2.0), ''))
        self.assertEqual(s.env(), {'CI_WORKERS': '4', 'CI_SPEED': '2.0'})  # no isa: no CI_ISA
        self.assertEqual(part.slot_cpus(s), {4, 5, 6, 7})
        self.assertEqual(part.runner_slot(self.HOST, 'part', {}), (None, ''))
        self.assertEqual(part.runner_slot(self.HOST, 'light', {}), (None, ''))
        self.assertEqual(part.slot_memory(s, '', '120G'), '8G')


if __name__ == '__main__':
    unittest.main()
