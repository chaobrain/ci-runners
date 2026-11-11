"""Adding a machine to the pool: ``pool add``, its checks and ``pool ready`` (decision 15.1,
item 6; task ci-pool-run, step 2)."""
from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock
from contextlib import redirect_stderr, redirect_stdout

from ci_runners import pool, pooladd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TOP = textwrap.dedent('''\
    schema = 1
    project = "proj"
    repository = "Owner/Proj"
    check = ["numpy"]
    warm = "python scripts/warm.py"
    numerics = "python scripts/numerics.py"

    # the first host, by hand
    [machine.hosta]
    access = "ssh"
    ssh = "alias-a"
    hostname = "host-a"
    cpus = 16
    memory_gib = 64
    python = "/opt/env/bin/python"
    work = "/var/pool-work"
    slots = { lock = "/tmp/x.lock", count = 2 }
    numerics_ok = true

    [machine.cl]
    access = "slurm"
    ssh = "alias-c"
    gate = "/fs/root/ci-runners/releases/hook/bin/ci-slurm-gate"

    [machine.cl.targets.cpu]
    partition = "part_c"
    cpus = 32
    memory_gib = 1900
    max_minutes = 90
    variant = "cpu"
''')

PROBE = {'hostname': 'host-n', 'cpus': 28, 'memory_gib': 125, 'cards': [['NVIDIA GeForce RTX 3090', 24576]] * 2,
         'cuda_major': 13, 'missing': []}
ARGS = dict(python='/opt/env/bin/python', work='/var/pool-work', lock='/tmp/n.lock', count=2)


def entry(**kw):
    return pooladd.entry('ssh', 'alias-n', {**PROBE, **kw}, labels={'multi': 'hostn-multi'}, **ARGS)


def fake_probe(answer=PROBE, calls=None):
    def run(argv, **kw):
        if calls is not None:
            calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, json.dumps(answer) + '\n', '')
    return run


class Etc(unittest.TestCase):
    def setUp(self):
        self.etc = tempfile.mkdtemp()
        self.path = os.path.join(self.etc, 'pool.toml')
        with open(self.path, 'w') as f:
            f.write(TOP)
        os.chmod(self.path, 0o600)

    def tearDown(self):
        subprocess.run(['rm', '-rf', self.etc])

    def text(self):
        with open(self.path) as f:
            return f.read()

    def add(self, key='hostn', **kw):
        with redirect_stderr(io.StringIO()):
            return pooladd.add(self.etc, key, 'ssh', 'alias-n', labels={'multi': 'hostn-multi'}, run=fake_probe(), **ARGS,
                               **kw)


class Registry(unittest.TestCase):
    def test_ready_warm_and_numerics_parse_and_ready_is_the_default(self):
        r = pool.load(TOP + '\n' + pooladd.render('hostn', entry()))
        self.assertTrue(r.machines['hosta'].ready)  # no key: ready (the live registry has none)
        self.assertFalse(r.machines['hostn'].ready)
        self.assertEqual((r.warm, r.numerics), ('python scripts/warm.py', 'python scripts/numerics.py'))
        with self.assertRaisesRegex(pool.RegistryError, 'ready must be true or false'):
            pool.load(TOP.replace('numerics_ok = true', 'ready = "no"'))

    def test_a_machine_not_ready_is_skipped_unless_on_names_it(self):
        r = pool.load(TOP + '\n' + pooladd.render('hostn', entry()))
        rows = [pool.host_row(r.machines[k], {'slots_free': 1, 'cpus': 16, 'load': 0, 'mem_free_gib': 60})
                for k in ('hosta', 'hostn')]
        now, _, skipped = pool.pick(rows, pool.Ask(), r)
        self.assertEqual([x['key'] for x in now], ['hosta'])
        self.assertIn('hostn: not ready (pool add\'s checks; --on hostn runs there)', skipped)
        now, _, _ = pool.pick(rows, pool.Ask(), r, only='hostn')
        self.assertEqual([x['key'] for x in now], ['hostn'])

    def test_pool_main_hands_add_check_and_ready_to_pooladd(self):
        with tempfile.TemporaryDirectory() as etc, redirect_stderr(io.StringIO()) as err:
            self.assertEqual(pool.main(['ready', 'x'], etc), pool.EXIT_USAGE)  # no registry: pooladd says so
        self.assertIn('pool add:', err.getvalue())


class Probe(unittest.TestCase):
    def test_gpu_model_names(self):
        for name, model in [('NVIDIA GeForce RTX 3090', '3090'), ('Tesla V100-SXM2-32GB', 'v100'), ('NVIDIA A40', 'a40'),
                            ('NVIDIA H100 80GB HBM3', 'h100'), ('NVIDIA RTX A6000', 'a6000'),
                            ('NVIDIA A100-SXM4-80GB', 'a100')]:
            with self.subTest(name):
                self.assertEqual(pooladd.gpu_model(name), model)
        with self.assertRaises(pooladd.AddError):
            pooladd.gpu_model('NVIDIA')

    def test_the_entry_from_a_probe(self):
        e = entry()
        self.assertEqual((e['gpus'], e['gpu_model'], e['gpu_mib'], e['cuda'], e['p2p']), (2, '3090', 24576, 'cuda13', False))
        self.assertEqual((e['ready'], e['numerics_ok'], e['hostname'], e['ssh']), (False, False, 'host-n', 'alias-n'))
        self.assertEqual(e['slots'], {'lock': '/tmp/n.lock', 'count': 2})
        cpu = pooladd.entry('local', '', {**PROBE, 'cards': [], 'cuda_major': 0}, **ARGS)
        self.assertEqual(cpu['gpus'], 0)
        self.assertNotIn('ssh', cpu)
        self.assertNotIn('gpu_model', cpu)
        with self.assertRaisesRegex(pooladd.AddError, 'more than one model'):
            entry(cards=[['NVIDIA A40', 46068], ['NVIDIA GeForce RTX 3090', 24576]])

    def test_the_probe_runs_on_the_machine_and_says_what_is_missing(self):
        calls = []

        def run(argv, **kw):  # the ssh command's python, here
            calls.append(argv)
            return subprocess.run([sys.executable, '-I', '-'], input=kw['input'], capture_output=True, text=True)
        with tempfile.TemporaryDirectory() as d:
            p = pooladd.probe('ssh', 'alias-n', {'python': os.path.join(d, 'no-python'), 'work': d,
                                                 'lock': os.path.join(d, 's.lock'), 'count': 2, 'cache': ''}, run)
        self.assertEqual(calls[0][:1] + calls[0][-2:], ['ssh', 'alias-n', '/usr/bin/python3 -I -'])
        self.assertGreater(p['cpus'], 0)
        self.assertGreater(p['memory_gib'], 0)
        self.assertEqual(len(p['missing']), 2, p['missing'])  # the python and both slot files; the work dir is there
        self.assertIn('no-python', p['missing'][0])
        self.assertIn('2 CI slot file(s)', p['missing'][1])

    def test_the_probe_and_the_entry_take_the_host_python(self):
        """``pool add --host-python`` (decision 15.8, item 9): the probe runs it, the entry names it."""
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, json.dumps(PROBE) + '\n', '')
        pooladd.probe('ssh', 'alias-n', {'python': '/p', 'work': '/w', 'lock': '/l', 'count': 1, 'cache': ''}, run,
                      '/opt/py/bin/python3')
        self.assertEqual(calls[0][-1], '/opt/py/bin/python3 -I -')
        e = pooladd.entry('ssh', 'alias-n', PROBE, host_python='/opt/py/bin/python3', **ARGS)
        self.assertEqual(e['host_python'], '/opt/py/bin/python3')
        self.assertEqual(pool.load(TOP + '\n' + pooladd.render('hostn', e)).machines['hostn'].host_python,
                         '/opt/py/bin/python3')
        self.assertNotIn('host_python', entry())

    def test_the_probe_reads_the_cuda_major_of_either_driver_header(self):
        """Driver 610's nvidia-smi says 'CUDA UMD Version: 13.3' where older ones say 'CUDA Version: 12.4'."""
        def run(argv, **kw):
            return subprocess.run([sys.executable, '-I', '-'], input=kw['input'], capture_output=True, text=True)
        for header, want in (('| NVIDIA-SMI 610.43.02   KMD Version: 610.43.02   CUDA UMD Version: 13.3 |', 13),
                             ('| NVIDIA-SMI 550.54.15   Driver Version: 550.54.15   CUDA Version: 12.4 |', 12),
                             ('| NVIDIA-SMI 1.0 |', 0)):
            with tempfile.TemporaryDirectory() as d:
                smi = os.path.join(d, 'nvidia-smi')
                with open(smi, 'w') as f:
                    f.write('#!/bin/sh\ncase "$1" in --query-gpu=*) echo "NVIDIA H100 PCIe, 81559" ;; '
                            f'*) echo "{header}" ;; esac\n')
                os.chmod(smi, 0o755)
                with mock.patch.dict(os.environ, {'PATH': f'{d}:/usr/bin:/bin'}):
                    p = pooladd.probe('ssh', 'alias-n', {'python': sys.executable, 'work': d,
                                                         'lock': os.path.join(d, 's.lock'), 'count': 1, 'cache': ''}, run)
            self.assertEqual((p['cards'], p['cuda_major']), ([['NVIDIA H100 PCIe', 81559]], want), header)


class Add(Etc):
    def test_the_entry_is_appended_validated_and_written_600(self):
        reg = self.add()
        self.assertFalse(reg.machines['hostn'].ready)
        text = self.text()
        self.assertTrue(text.startswith(TOP))  # the hand-written part kept as it was
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        m = pool.load_file(self.path).machines['hostn']
        self.assertEqual((m.gpus, m.gpu_model, m.labels, m.ready, m.numerics_ok), (2, '3090', {'multi': 'hostn-multi'},
                                                                                  False, False))

    def test_a_dry_run_writes_nothing(self):
        with redirect_stdout(io.StringIO()) as out:
            self.add(dry_run=True)
        self.assertEqual(self.text(), TOP)
        self.assertIn('[machine.hostn]', out.getvalue())

    def test_an_existing_key_or_host_is_refused(self):
        self.add()
        with self.assertRaisesRegex(pooladd.AddError, 'hostn is in the registry already'):
            self.add()
        with self.assertRaisesRegex(pooladd.AddError, 'host-a is in the registry already, as hosta'):
            pooladd.add_text(TOP, 'other', {**entry(), 'hostname': 'host-a'})
        with self.assertRaisesRegex(pooladd.AddError, 'machine hosta is in the registry already'):
            pooladd.add_text(TOP, 'hosta', entry())


def fake_runs(codes):
    """A ``pool_run`` that answers each step's command by its name, recording the calls."""
    calls = []

    def pool_run(reg, a, **kw):
        name = 'canary' if 'jax.devices' in a.command else 'p2p' if 'self_test' in a.command else \
            'warm' if 'warm' in a.command else 'numerics'
        calls.append((name, a, kw))
        return codes.get(name, 0)
    return pool_run, calls


def no_status(argv, **kw):  # pool status's probe: the machine unreachable (the fake pool_run ignores it)
    return subprocess.CompletedProcess(argv, 255, '', '')


class Checks(Etc):
    def setUp(self):
        super().setUp()
        self.add()
        self.reg = pool.load_file(self.path)
        self.fps = {'numpy': '2.0'}
        self.orig = pool.fingerprint
        pool.fingerprint = lambda prefix, python, dists, run=None: dict(self.fps if prefix else {'numpy': '2.0'})

    def tearDown(self):
        pool.fingerprint = self.orig
        super().tearDown()

    def check(self, codes=None, **kw):
        pr, calls = fake_runs(codes or {})
        with redirect_stderr(io.StringIO()):
            rc = pooladd.check(self.reg, 'hostn', self.etc, ROOT, run=no_status, pool_run=pr, **kw)
        return rc, calls, pooladd.read_state(self.etc).get('hostn', {}).get('steps', {})

    def ready(self):
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            return pooladd.make_ready(self.etc, 'hostn')

    def test_the_checks_run_in_order_on_the_machine_and_are_recorded(self):
        rc, calls, steps = self.check()
        self.assertEqual(rc, 0)
        self.assertEqual([c[0] for c in calls], ['canary', 'p2p', 'warm', 'numerics'])
        self.assertEqual(sorted(steps), sorted(pooladd.STEPS))
        self.assertTrue(all(s['ok'] for s in steps.values()))
        for _, a, kw in calls:
            self.assertEqual((kw['only'], kw['here_ok']), ('hostn', True))
            self.assertEqual([r['key'] for r in kw['rows']], ['hostn'])  # pool status of that machine only
            self.assertEqual(a.variables['CUDA_VISIBLE_DEVICES'], '0,1')
        self.assertIn('raise SystemExit(0 if n == 2 else 1)', calls[0][1].command)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.etc, pooladd.STATE)).st_mode), 0o600)

    def test_an_env_difference_fails_first_and_stops_them(self):
        self.fps = {'numpy': '2.1'}
        rc, calls, steps = self.check()
        self.assertEqual((rc, calls), (1, []))
        self.assertEqual(steps['env'], {'ok': False, 'at': steps['env']['at'], 'why': 'numpy: 2.0 here, 2.1 there'})

    def test_a_failed_canary_stops_them_a_failed_p2p_does_not(self):
        rc, calls, _ = self.check({'canary': 1})
        self.assertEqual((rc, [c[0] for c in calls]), (1, ['canary']))
        rc, calls, steps = self.check({'p2p': 1})
        self.assertEqual((rc, [c[0] for c in calls]), (0, ['canary', 'p2p', 'warm', 'numerics']))
        self.assertFalse(steps['p2p']['ok'])

    def test_a_one_card_or_cpu_machine_has_no_p2p_step(self):
        e = {**entry(cards=[['NVIDIA A40', 46068]]), 'hostname': 'host-o'}
        reg = pool.load(pooladd.add_text(TOP, 'onecard', e))
        self.assertEqual(pooladd.required(reg, reg.machines['onecard']), ['env', 'canary', 'warm', 'numerics'])
        self.assertEqual([s.name for s in pooladd.steps(reg, reg.machines['onecard'])], ['canary', 'warm', 'numerics'])

    def test_ready_only_after_every_step_passed(self):
        self.assertEqual(self.ready(), 1)  # nothing ran
        self.check({'numerics': 1})
        self.assertEqual(self.ready(), 1)
        self.assertFalse(pool.load_file(self.path).machines['hostn'].ready)
        self.check({'p2p': 1})
        self.assertEqual(self.ready(), 0)
        m = pool.load_file(self.path).machines['hostn']
        self.assertEqual((m.ready, m.numerics_ok, m.p2p), (True, True, False))  # p2p: the self-test's result
        self.assertTrue(self.text().startswith(TOP))
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_a_pass_sets_p2p(self):
        self.check()
        self.assertEqual(self.ready(), 0)
        self.assertTrue(pool.load_file(self.path).machines['hostn'].p2p)

    def test_an_entry_edited_after_its_checks_is_not_ready(self):
        self.check()
        text = self.text()
        with open(self.path, 'w') as f:
            f.write(text.replace('cpus = 28', 'cpus = 20'))
        self.assertEqual(self.ready(), 1)
        reg = pool.load_file(self.path)
        self.assertIn('changed since the checks ran', pooladd.not_passed(pooladd.read_state(self.etc)['hostn'], reg,
                                                                         'hostn')[0])

    def test_without_the_registrys_commands_nothing_is_ready(self):
        reg = pool.load(pooladd.add_text(TOP.replace('numerics = "python scripts/numerics.py"\n', ''), 'hostn', entry()))
        rec = {'digest': pooladd.digest(reg, 'hostn'), 'steps': {s: {'ok': True} for s in pooladd.STEPS}}
        self.assertEqual(pooladd.not_passed(rec, reg, 'hostn'),
                         ["the registry names no numerics command (its top-level 'numerics')"])

    def test_the_cli_adds_checks_and_makes_ready(self):
        argv = ['add', 'hostm', '--ssh', 'alias-m', '--python', '/opt/env/bin/python', '--work', '/var/pool-work',
                '--slots-lock', '/tmp/m.lock', '--slots-count', '1', '--labels', 'cpu=hostm-cpu', '--dir', ROOT,
                '--caller-python', '/my/env/bin/python', '--host-python', '/opt/py/bin/python3']
        pr, calls = fake_runs({})
        probed = []
        probe = fake_probe({**PROBE, 'hostname': 'host-m', 'cards': []}, probed)
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(pooladd.main([*argv, '--dry-run'], self.etc, run=probe, pool_run=pr), 0)
            self.assertEqual(probed[0][0][-1], '/opt/py/bin/python3 -I -')
            self.assertNotIn('hostm', pool.load_file(self.path).machines)
            self.assertEqual(calls, [])  # a dry run runs no check
            self.assertEqual(pooladd.main(argv, self.etc, run=probe, pool_run=pr), 0)
            self.assertEqual([c[0] for c in calls], ['canary', 'warm', 'numerics'])
            self.assertEqual({c[1].python for c in calls}, {'/my/env/bin/python'})
            self.assertEqual(pool.main(['ready', 'hostm'], self.etc), 0)
        self.assertIn('setup/plan.sh ROLE', out.getvalue())
        self.assertIn('ci-run env build proj --dry-run', out.getvalue())
        m = pool.load_file(self.path).machines['hostm']
        self.assertEqual((m.ready, m.numerics_ok, m.labels, m.host_python),
                         (True, True, {'cpu': 'hostm-cpu'}, '/opt/py/bin/python3'))

    def test_ready_text_edits_that_entry_only(self):
        text = pooladd.add_text(TOP, 'hostn', entry()) + '\n# a trailing note\n'
        new = pooladd.ready_text(text, 'hostn', True)
        self.assertIn('# the first host, by hand', new)
        self.assertIn('# a trailing note', new)
        self.assertEqual(new.count('ready = '), 1)
        r = pool.load(new)
        self.assertEqual((r.machines['hostn'].ready, r.machines['hostn'].p2p), (True, True))
        self.assertEqual(r.machines['hosta'], pool.load(TOP).machines['hosta'])


class Steps(unittest.TestCase):
    def test_the_steps_variables_and_sizes(self):
        reg = pool.load(pooladd.add_text(TOP, 'hostn', {**entry(), 'cache': '/var/cache/x'}))
        s = {x.name: x for x in pooladd.steps(reg, reg.machines['hostn'])}
        self.assertEqual(s['canary'].variables['XLA_PYTHON_CLIENT_PREALLOCATE'], 'false')
        self.assertEqual(s['warm'].variables, {'CUDA_VISIBLE_DEVICES': '0,1', 'CI_POOL_CACHE': '/var/cache/x'})
        self.assertEqual(s['numerics'].command, 'python scripts/numerics.py')
        self.assertEqual((s['warm'].ask.cpus, s['warm'].ask.memory_gib), (8, 32))

    def test_the_canary_and_p2p_commands_run(self):
        r = subprocess.run(['bash', '-c', pooladd.p2p_command().replace('python', sys.executable, 1)],
                           env={**os.environ, 'CUDA_VISIBLE_DEVICES': ''}, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 1)  # no card visible: the self-test's own refusal, so its source ran whole
        self.assertTrue(r.stdout.startswith('p2p: '), r.stdout)
        cmd = pooladd.canary_command(3)
        self.assertTrue(cmd.startswith('python -I -c '))
        self.assertIn('raise SystemExit(0 if n == 3 else 1)', cmd)


if __name__ == '__main__':
    unittest.main()
