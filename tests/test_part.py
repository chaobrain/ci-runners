"""The executor and salvage (design §4.3-§4.5), on a fake host tree, without systemd:
the supervisor runs directly (``scope=False``). The scope itself is checked live on
3090b (RUN-1 acceptance 6)."""
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
        self.assertEqual((o['outcome'], o['next'], o['retry-on'], o['verify'], o['attempt']),
                         ('pass', 'none', '[]', 'false', '1'))
        self.assertEqual(json.loads(pathlib.Path(rec, 'outcome.json').read_text())['outcome'], 'pass')
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
        """h100 (RUN-5): its CPU pool's runners carry `h100-cpu`, not `part`; a retry goes there."""
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
        finally:
            os.close(fd)
        self.assertEqual((out['outcome'], out['reason']), ('unavailable', 'queue timeout: no CI slot'))
        self.assertEqual(os.listdir(self.slot.parent / 's.queue') and
                         [n for n in os.listdir(self.slot.parent / 's.queue') if n.endswith('.wait')], [])
        code, out = self.execute(command='true', slot='none', id='unit2')  # a part that takes no slot does not wait
        self.assertEqual(out['outcome'], 'pass')

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

    def test_an_older_local_ticket_keeps_its_turn(self):
        q = self.slot.parent / 's.queue'
        q.mkdir()
        (q / '000000000001.ticket').write_text(f'1 {os.getpid()} {int(time.time())} /x local_ci.sh\n')  # ci_slot.sh's
        _, out = self.execute(command='true')
        self.assertEqual((out['outcome'], out['reason']), ('unavailable', 'queue timeout: no CI slot'))

    def test_two_default_projects_are_refused(self):
        original = (self.etc / 'projects' / 'p.toml').read_text()
        for n in ('p', 'q'):
            text = original.replace('name = "p"', f'name = "{n}"')
            (self.etc / 'projects' / f'{n}.toml').write_text(text.replace('[hosts.hb]\n', '[hosts.hb]\ndefault = true\n'))
        _, out = self.execute(command='true')
        self.assertEqual(out['outcome'], 'unavailable')
        self.assertIn('both the default project', out['reason'])

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

    def test_a_local_holder_counts_as_the_default_project(self):
        self.two_slots()
        (self.etc / 'projects' / 'p.toml').write_text(
            (self.etc / 'projects' / 'p.toml').read_text().replace('[hosts.hb]\n', '[hosts.hb]\nmax_slots = 1\n'))
        fd = os.open(self.slot, os.O_WRONLY)  # a local run holds slot 1, with no project
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

    def test_cards_are_leased(self):
        smi = '0, 24576, 576\n1, 24576, 24000\n'

        def run(argv, **kw):
            if argv[0] == 'nvidia-smi':
                return subprocess.CompletedProcess(argv, 0, smi, '')
            return subprocess.run(argv, **kw)
        ex = part.Executor(self.inv, str(self.etc), self.env, release=str(ROOT), scope=False, poll=0.1, run=run)
        code, out = ex.execute(part.Inputs(project='p', id='g', timeout_minutes=1, queue_timeout_minutes=0.02, gpus=1,
                                           gpu_mem_mib=6000, slot='none',
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
        """h100 (RUN-5): `part` is not a label of h100's CPU runners; `h100-cpu` is."""
        rec = {**self.REC, 'host': 'h100', 'pool_labels': ['h100-cpu']}
        self.assertEqual(part.finish('/r', rec, {'rc': 0, 'boot_end': 'b2'})['retry-on'], ['self-hosted', 'h100-cpu'])
        self.assertEqual(part.finish('/r', rec, None)['retry-on'], ['self-hosted', 'h100-cpu'])
        out = part.finish('/r', rec, None, unavailable='low disk')
        self.assertEqual((out['next'], out['retry-on']), ('fallback', ['self-hosted', 'h100-cpu']))
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
        self.assertEqual(part.command_argv('c', compile_part=False, lock='none', lock_bin='L', host_lock='H'),
                         ['nice', '-n', '10', 'ionice', '-c', '2', '-n', '7', 'bash', '-c', 'c'])
        self.assertEqual(part.command_argv('c', compile_part=True, lock='s', lock_bin='L', host_lock='H')[2:3], ['0'])
        self.assertIn('-s', part.command_argv('c', compile_part=True, lock='s', lock_bin='L', host_lock='H'))
        self.assertEqual(part.unit_name('a b', 'x', '7-1-1'), 'ci-part-a_b-x-7-1-1.scope')

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
            part.write_outputs({'outcome': 'pass', 'retry-on': ['a', 'b'], 'verify': False}, f.name)
            text = f.read()
        self.assertIn('retry-on=["a", "b"]\n', text)
        self.assertIn('verify=false\n', text)
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
               'default as str': ({'hosts': {'hb': {'memory_max': '2G', 'default': 'yes'}}}, 'p'),
               'multi-line var': ({'hosts': {'hb': {'memory_max': '2G', 'variables': {'A': 'x\ny'}}}}, 'p'),
               'fallback': ({'fallback': {'part': []}}, 'p')}
        for what, (change, stem) in bad.items():
            with self.assertRaises(registry.RegistryError, msg=what):
                registry.parse({**self.BASE, **change}, 'hb', stem)


if __name__ == '__main__':
    unittest.main()
