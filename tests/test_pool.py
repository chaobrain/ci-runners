"""The machine registry, ``pool status``, the claim, the env check and ``pool run``
(decision 15.1; tasks ci-cluster-pool, ci-pool-run)."""
from __future__ import annotations

import fcntl
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest

from ci_runners import pool
from ci_runners import slots, slurmgate

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

REGISTRY = textwrap.dedent('''\
    schema = 1
    project = "proj"
    repository = "Owner/Proj"
    check = ["numpy"]

    [machine.hosta]
    access = "ssh"
    ssh = "alias-a"
    hostname = "host-a"
    cpus = 16
    memory_gib = 64
    gpus = 2
    gpu_model = "3090"
    gpu_mib = 24576
    p2p = true
    python = "/opt/env/bin/python"
    work = "/var/pool-work"
    slots = { lock = "/tmp/x.lock", count = 2 }
    labels = { cpu = "hosta-cpu" }
    numerics_ok = true

    [machine.hostb]
    access = "local"
    hostname = "host-b"
    cpus = 16
    memory_gib = 64
    python = "/opt/env/bin/python"
    work = "/var/pool-work"
    slots = { lock = "/tmp/x.lock", count = 1 }

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
    price = 0.04

    [machine.cl.targets.v100]
    partition = "part_v"
    cpus = 10
    memory_gib = 470
    max_minutes = 45
    variant = "cuda12"
    gpu_model = "v100"
    cards = 2
    max_cards = 2
    gpu_mib = 32768
    price = 1.6

    [machine.cl.targets.a40]
    partition = "part_a"
    cpus = 8
    memory_gib = 470
    max_minutes = 45
    variant = "cuda12"
    gpu_model = "a40"
    cards = 4
    max_cards = 2
    gpu_mib = 46068
    price = 4.5
''')


def reg():
    return pool.load(REGISTRY)


class Registry(unittest.TestCase):
    def test_a_registry_loads(self):
        r = reg()
        self.assertEqual(list(r.machines), ['hosta', 'hostb', 'cl'])
        self.assertEqual(r.machines['hosta'].slots, pool.Slots('/tmp/x.lock', 2))
        self.assertTrue(r.machines['cl'].billed)  # a cluster is billed unless it says otherwise
        self.assertFalse(r.machines['hosta'].billed)
        self.assertEqual(r.machines['cl'].base, '/fs/root/ci-runners')
        self.assertEqual(r.machines['cl'].targets['v100'].max_cards, 2)

    def test_malformed_entries_are_refused(self):
        for bad, why in [(REGISTRY.replace('schema = 1', 'schema = 2'), 'schema'),
                         (REGISTRY.replace('ssh = "alias-a"\n', ''), 'ssh alias'),
                         (REGISTRY.replace('"/opt/env/bin/python"', '"env/bin/python"', 1), 'absolute'),
                         (REGISTRY.replace('cpus = 16\n', 'cpus = 16\ncolour = "red"\n', 1), 'unknown keys'),
                         (REGISTRY.replace('access = "local"', 'access = "ftp"'), 'access'),
                         (REGISTRY.replace('count = 1', 'count = 0'), 'count'),
                         (REGISTRY.replace('max_cards = 2\ngpu_mib = 32768', 'gpu_mib = 32768'), 'max_cards')]:
            with self.subTest(why):
                with self.assertRaisesRegex(pool.RegistryError, why):
                    pool.load(bad)

    def test_a_registry_others_may_write_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'pool.toml')
            with open(p, 'w') as f:
                f.write(REGISTRY)
            os.chmod(p, 0o620)
            with self.assertRaisesRegex(pool.RegistryError, 'writable'):
                pool.load_file(p)
            os.chmod(p, 0o600)
            self.assertEqual(pool.load_file(p).project, 'proj')

    def test_deny_tokens_hold_the_inventory_not_the_labels(self):
        import tomllib
        toks = pool.deny_tokens(tomllib.loads(REGISTRY))
        self.assertIn('alias-a', toks)
        self.assertIn('host-a', toks)
        self.assertIn('part_v', toks)
        self.assertNotIn('hosta-cpu', toks)
        self.assertNotIn('3090', toks)


# ------------------------------------------------------------------ the claim: one slot, a CI part and a pool run


def _python_holding(lock, n, cmd, project='other'):
    """A ``ci-slot`` process (the parts' claim) running ``cmd`` while it holds a slot."""
    code = ('import sys; sys.path.insert(0, sys.argv.pop(1)); from ci_runners.slots import main; '
            'sys.exit(main(sys.argv[1:]))')
    return subprocess.Popen([sys.executable, '-I', '-c', code, ROOT, '--lock', lock, '--slots', str(n), '--project',
                             project, '--default', project, '--poll', '0.1', *cmd],
                            stderr=subprocess.PIPE, text=True)


def _wait_holding(p):
    line = p.stderr.readline()
    assert 'holding' in line, line


class Claim(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.lock = os.path.join(self.d, 'slot.lock')
        open(self.lock, 'w').close()

    def tearDown(self):
        subprocess.run(['rm', '-rf', self.d])

    def spec(self, cmd, **kw):
        s = {'lock': self.lock, 'count': 1, 'queue': '', 'project': 'proj', 'command': cmd, 'python': sys.executable,
             'memory_gib': 4, 'cards': 0, 'need_mib': 0, 'timeout_seconds': 60, 'no_wait': False,
             'queue_seconds': 30, 'poll': 0.1}
        s.update(kw)
        return s

    def remote(self, spec):
        """``pool.remote`` in a child process (it clears its environment), its run directory
        under ``self.d``; its stdin stays open, as ``run_host``'s ssh keeps it."""
        run_dir = tempfile.mkdtemp(prefix='pool-', dir=self.d)
        os.makedirs(os.path.join(run_dir, 'ws'))
        code = ('import sys; sys.path.insert(0, sys.argv.pop(1)); from ci_runners.pool import remote; '
                'sys.exit(remote(sys.argv[1:]))')
        out, err = (open(os.path.join(self.d, f'{n}.{os.path.basename(run_dir)}'), 'w+') for n in ('out', 'err'))
        p = subprocess.Popen([sys.executable, '-I', '-c', code, ROOT, run_dir, json.dumps(spec)],
                             cwd=os.path.join(run_dir, 'ws'), stdin=subprocess.PIPE, stdout=out, stderr=err, text=True)
        p.files = (out, err)
        return p, run_dir

    @staticmethod
    def finish(p, timeout=60):
        """Wait for ``p`` with its stdin open, then its ``(stdout, stderr)``."""
        p.wait(timeout=timeout)
        p.stdin.close()
        texts = []
        for f in p.files:
            f.seek(0)
            texts.append(f.read())
            f.close()
        return texts

    def test_a_ci_part_holding_the_one_slot_keeps_a_pool_run_out(self):
        marker = os.path.join(self.d, 'ran')
        part = _python_holding(self.lock, 1, ['sleep', '30'])
        try:
            _wait_holding(part)
            p = pool.probe(self.lock, 1, '')
            self.assertEqual(p['slots_free'], 0)
            r, run_dir = self.remote(self.spec(f'touch {marker}', no_wait=True))
            out, err = self.finish(r)
            self.assertEqual(r.returncode, pool.NOT_STARTED, err)
            self.assertIn('no CI slot free', err)
            self.assertFalse(os.path.exists(marker))
            self.assertFalse(os.path.exists(run_dir))  # the run's directory goes either way
        finally:
            part.kill()
            part.wait()

    def test_a_pool_run_waits_in_line_and_runs_when_the_part_ends(self):
        part = _python_holding(self.lock, 1, ['sleep', '2'])
        _wait_holding(part)
        r, _ = self.remote(self.spec('echo ran; exit 7'))
        out, err = self.finish(r)
        part.wait()
        self.assertIn('waiting in line', err)
        self.assertIn('ran', out)
        self.assertEqual(r.returncode, 7)  # the command's code

    def test_a_ci_part_waits_while_a_pool_run_holds_the_slot(self):
        go = os.path.join(self.d, 'go')
        r, _ = self.remote(self.spec(f'while [ ! -e {go} ]; do sleep 0.1; done'))
        try:
            deadline = time.time() + 20
            while pool.probe(self.lock, 1, '')['slots_free'] and time.time() < deadline:
                time.sleep(0.1)
            self.assertEqual(pool.probe(self.lock, 1, '')['slots_free'], 0)
            q = slots.SlotQueue(self.lock, 1, poll=0.1, default_project='proj')
            rc = q.hold('other', {}, ['true'], deadline=time.time() + 1)  # returns only on its queue timeout
            self.assertEqual(rc, 4)
        finally:
            open(go, 'w').close()
            self.finish(r)

    def test_a_closed_stdin_ends_the_command_and_frees_the_slot(self):
        marker = os.path.join(self.d, 'late')
        r, _ = self.remote(self.spec(f'sleep 20; touch {marker}'))
        deadline = time.time() + 20
        while pool.probe(self.lock, 1, '')['slots_free'] and time.time() < deadline:
            time.sleep(0.1)
        t0 = time.time()
        r.stdin.close()  # the caller is gone
        r.wait(timeout=30)
        self.assertLess(time.time() - t0, 15)
        self.assertEqual(r.returncode, pool.EXIT_SIGNALLED)
        self.assertEqual(pool.probe(self.lock, 1, '')['slots_free'], 1)
        time.sleep(0.5)
        self.assertFalse(os.path.exists(marker))

    def test_the_run_gets_its_env_and_no_cards(self):
        r, _ = self.remote(self.spec('echo "[$CUDA_VISIBLE_DEVICES] $CI_PROJECT $FOO_X $CONDA_PREFIX"',
                                     variables={'FOO_X': 'kept'}))
        out, err = self.finish(r)
        self.assertEqual(r.returncode, 0, err)
        self.assertEqual(out.strip(), f'[] proj kept {os.path.dirname(os.path.dirname(sys.executable))}')

    def test_the_timeout_ends_it(self):
        r, _ = self.remote(self.spec('sleep 30', timeout_seconds=1))
        self.finish(r)
        self.assertEqual(r.returncode, pool.EXIT_TIMEOUT)


class Probe(unittest.TestCase):
    def test_free_slots_are_read_without_taking_them(self):
        with tempfile.TemporaryDirectory() as d:
            lock = os.path.join(d, 's.lock')
            for p in (lock, os.path.join(d, 's.2.lock')):
                open(p, 'w').close()
            os.makedirs(os.path.join(d, 's.queue'))
            open(os.path.join(d, 's.queue', '000000000001.ticket'), 'w').close()
            fd = os.open(lock, os.O_RDONLY)
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                p = pool.probe(lock, 2, '')
            finally:
                os.close(fd)
            self.assertEqual((p['slots_free'], p['slots_missing'], p['queue']), (1, 0, 1))
            self.assertEqual(pool.probe(lock, 3, '')['slots_missing'], 1)

    def test_a_host_row_has_the_hoststate_fields(self):
        m = reg().machines['hosta']
        row = pool.host_row(m, {'slots_free': 1, 'queue': 2, 'load': 5.2, 'cpus': 16, 'mem_free_gib': 40,
                                'gpu_free': {'0': 20000}}, here='host-a')
        for k in ('labels', 'problems', 'queue', 'gpu_free', 'multi_gpu', 'gpu_model', 'cuda', 'cpus_free', 'billed',
                  'price', 'target', 'numerics_ok', 'start_now'):
            self.assertIn(k, row)
        self.assertEqual((row['cpus_free'], row['queue'], row['gpu_free'], row['here']), (10, 2, {0: 20000}, True))
        busy = pool.host_row(m, {'slots_free': 0, 'load': 1.0, 'cpus': 16})
        self.assertEqual(busy['cpus_free'], 0)  # no slot: no cores to place on
        self.assertEqual(pool.host_row(m, None, 'unreachable (exit 255)')['problems'], ['unreachable (exit 255)'])

    def test_probe_host_runs_the_probe_through_ssh(self):
        m = reg().machines['hosta']
        seen = {}

        def run(argv, **kw):
            seen['argv'], seen['input'] = argv, kw['input']
            return subprocess.CompletedProcess(argv, 0, json.dumps({'slots_free': 2, 'load': 0, 'cpus': 16}) + '\n', '')
        row = pool.probe_host(m, run)
        self.assertEqual(seen['argv'][-2:], ['alias-a', '/usr/bin/python3 -I -'])
        self.assertIn("probe('/tmp/x.lock', 2, '')", seen['input'])
        self.assertEqual(row['slots_free'], 2)
        self.assertEqual(pool.probe_host(m, lambda a, **k: subprocess.CompletedProcess(a, 255, '', ''))['problems'],
                         ['unreachable (exit 255)'])


SINFO = '\n'.join(['cn1|part_c|mix|4/68/0/72|2060000|0|(null)|(null)|',
                   'gn1|part_v|mix|2/18/0/20|510000|0|gpu:tesla:2|gpu:tesla:2(IDX:0-1)|',
                   'gn2|part_v|idle|0/20/0/20|510000|0|gpu:tesla:2|gpu:tesla:0(IDX:N/A)|',
                   'gn3|part_a|down*|0/128/0/128|510000|0|gpu:tesla:4|gpu:tesla:0(IDX:N/A)|',
                   'garbage'])


class Cluster(unittest.TestCase):
    def test_parsers(self):
        nodes = pool.parse_sinfo(SINFO)
        self.assertEqual([(n.name, n.cpus_idle, n.cards_free) for n in nodes],
                         [('cn1', 68, 0), ('gn1', 18, 0), ('gn2', 20, 2), ('gn3', 128, 4)])
        self.assertEqual(pool.parse_assoc('part_c|cpu=72,node=1\npart_v|cpu=200,gres/gpu:tesla=20,node=10\n'),
                         {'part_c': {'cpu': 72}, 'part_v': {'cpu': 200, 'gpu': 20}})
        self.assertEqual(pool.parse_squeue('part_v|10|gres/gpu:tesla:1\npart_v|10|gres/gpu:2\nPART_C|32|N/A\n'),
                         {'part_v': {'cpu': 20, 'gpu': 3}, 'part_c': {'cpu': 32, 'gpu': 0}})

    def test_target_rows_start_now_within_the_association(self):
        r = reg()
        m = r.machines['cl']
        nodes = pool.parse_sinfo(SINFO)
        cpu = pool.target_row(m, m.targets['cpu'], nodes, {'part_c': {'cpu': 72}}, {})
        self.assertEqual((cpu['key'], cpu['start_now'], cpu['cpus_free'], cpu['billed']), ('cl:cpu', True, 68, True))
        full = pool.target_row(m, m.targets['cpu'], nodes, {'part_c': {'cpu': 72}}, {'part_c': {'cpu': 64}})
        self.assertEqual((full['start_now'], full['cpus_free']), (False, 8))  # 8 of the account's 72 left
        v = pool.target_row(m, m.targets['v100'], nodes, {}, {}, cards=2)
        self.assertEqual((v['start_now'], sorted(v['gpu_free'])), (True, [0, 1]))
        v_lim = pool.target_row(m, m.targets['v100'], nodes, {'part_v': {'cpu': 200, 'gpu': 20}}, {'part_v': {'gpu': 19}},
                                cards=2)
        self.assertFalse(v_lim['start_now'])
        a = pool.target_row(m, m.targets['a40'], nodes, {}, {})
        self.assertEqual((a['start_now'], a['problems']), (False, ['no usable node in its partition']))  # down

    def test_an_unreachable_cluster_starts_nothing(self):
        rows = pool.probe_cluster(reg().machines['cl'], lambda a, **k: subprocess.CompletedProcess(a, 255, '', ''))
        self.assertTrue(all(not r['start_now'] and r['problems'] for r in rows))


def row(key, **kw):
    r = {'key': key, 'access': 'ssh', 'labels': {}, 'problems': [], 'queue': 0, 'gpu_free': {}, 'multi_gpu': False,
         'gpu_model': '', 'cuda': '', 'cpus_free': 16, 'billed': False, 'price': 0.0, 'target': '', 'numerics_ok': True,
         'start_now': True, 'slots_free': 1, 'slots': 2, 'mem_free_gib': 60, 'here': False}
    r.update(kw)
    return r


class Pick(unittest.TestCase):
    def keys(self, rows, ask, **kw):
        now, later, skipped = pool.pick(rows, ask, reg(), **kw)
        return [r['key'] for r in now], [r['key'] for r in later], skipped

    def test_free_hosts_first_then_the_cheapest_target_that_starts_now(self):
        rows = [row('cl:cpu', billed=True, price=0.04, target='cpu', cpus_free=68), row('hosta', cpus_free=4),
                row('hostb', cpus_free=12)]
        self.assertEqual(self.keys(rows, pool.Ask(cpus=4))[0], ['hostb', 'hosta', 'cl:cpu'])

    def test_the_callers_own_host_is_skipped_unless_asked(self):
        rows = [row('hostb', here=True)]
        now, _, skipped = self.keys(rows, pool.Ask())
        self.assertEqual(now, [])
        self.assertIn('own host', skipped[0])
        self.assertEqual(self.keys(rows, pool.Ask(), here_ok=True)[0], ['hostb'])

    def test_a_host_without_a_free_slot_is_where_it_waits_in_line(self):
        rows = [row('hosta', slots_free=0, cpus_free=0, queue=3), row('hostb', slots_free=0, cpus_free=0, queue=1),
                row('cl:cpu', billed=True, target='cpu', start_now=False)]
        now, later, skipped = self.keys(rows, pool.Ask())
        self.assertEqual((now, later), ([], ['hostb', 'hosta']))
        self.assertIn('cl:cpu: cannot start it now', skipped)

    def test_cards_by_model_count_and_p2p(self):
        rows = [row('hosta', gpu_model='3090', multi_gpu=True, gpu_free={0: 20000, 1: 3000}),
                row('cl:v100', billed=True, price=1.6, target='v100', gpu_model='v100', multi_gpu=True,
                    gpu_free={0: 32768, 1: 32768}),
                row('cl:a40', billed=True, price=4.5, target='a40', gpu_model='a40', multi_gpu=True,
                    gpu_free={0: 1, 1: 1}),
                row('cl:cpu', billed=True, target='cpu')]
        now, _, skipped = self.keys(rows, pool.Ask(cards=1))
        self.assertEqual(now, ['cl:v100', 'cl:a40'])  # a host's cards are its parts' leases
        self.assertIn('leased to its parts', skipped[0])
        self.assertEqual(self.keys(rows, pool.Ask(cards=2))[0], ['cl:v100', 'cl:a40'])
        self.assertEqual(self.keys(rows, pool.Ask(cards=1, gpu_model='a40'))[0], ['cl:a40'])
        self.assertEqual(self.keys(rows, pool.Ask(cards=1, gpu_model='a4'))[0], [])  # a model, not a prefix
        _, _, skipped = self.keys(rows, pool.Ask(cards=0))
        self.assertIn('cl:v100: a GPU target, and no card asked', skipped)

    def test_sizes_beyond_a_machine_are_skipped(self):
        _, _, skipped = self.keys([row('hosta')], pool.Ask(cpus=64))
        self.assertEqual(skipped, ['hosta: 16 cores, 64 asked'])
        _, _, skipped = self.keys([row('cl:cpu', billed=True, target='cpu')], pool.Ask(memory_gib=4000))
        self.assertEqual(skipped, ['cl:cpu: 1900 GiB a node, 4000 asked'])

    def test_on_keeps_one_machine_or_target(self):
        rows = [row('hosta'), row('cl:cpu', billed=True, target='cpu')]
        self.assertEqual(self.keys(rows, pool.Ask(), only='cl')[0], ['cl:cpu'])
        self.assertEqual(self.keys(rows, pool.Ask(), only='hosta')[0], ['hosta'])

    def test_gpu_argument(self):
        self.assertEqual(pool.parse_gpu('v100:2'), ('v100', 2))
        self.assertEqual(pool.parse_gpu('any'), ('', 1))
        with self.assertRaises(ValueError):
            pool.parse_gpu('v100:x')


class EnvCheck(unittest.TestCase):
    def test_diff(self):
        self.assertEqual(pool.env_diff({'a': '1.0', 'b': '2.0@abc'}, {'a': '1.0', 'b': '2.0@abc'}), [])
        self.assertEqual(pool.env_diff({'a': '1.0'}, {'a': '1.1'}), ['a: 1.0 here, 1.1 there'])
        self.assertEqual(pool.env_diff({'b': '2.0@abc'}, {'b': '2.0@def'}), ['b: 2.0@abc here, 2.0@def there'])
        self.assertEqual(pool.env_diff({'b': '2.0@abc'}, {'b': '2.0'}), [])  # editable on one side only: the version
        self.assertEqual(pool.env_diff({'c': None}, {'c': '1'}), ['c: missing here, 1 there'])

    def test_fingerprint_reads_this_env(self):
        fp = pool.fingerprint([], sys.executable, ['pip', 'no-such-dist-xyz'])
        self.assertIsNone(fp['no-such-dist-xyz'])
        self.assertTrue(fp['pip'] is None or fp['pip'][0].isdigit())

    def test_a_mismatch_refuses_unless_warned(self):
        fps = iter([{'numpy': '2.0'}, {'numpy': '2.1'}] * 2)
        orig = pool.fingerprint
        pool.fingerprint = lambda *a, **k: next(fps)
        try:
            a = pool.RunArgs('true', '.', pool.Ask())
            self.assertFalse(pool._env_ok(reg(), a, [], '/x/python', None))
            a.env_mismatch = 'warn'
            self.assertTrue(pool._env_ok(reg(), a, [], '/x/python', None))
        finally:
            pool.fingerprint = orig

    def test_forwarded_variables(self):
        self.assertEqual(pool.forwarded({'JAX_ENABLE_X64': '1', 'XLA_FLAGS': 'x', 'HOME': '/h', 'CI_TEST_TIMEOUT': '9',
                                         'GH_TOKEN': 's'}), {'JAX_ENABLE_X64': '1', 'XLA_FLAGS': 'x', 'CI_TEST_TIMEOUT': '9'})


class Exit(unittest.TestCase):
    def test_exit_codes(self):
        self.assertEqual(pool._exit_code({'rc': 3}), 3)
        self.assertEqual(pool._exit_code({'rc': -9}), 137)
        self.assertEqual(pool._exit_code({'rc': 0, 'timed_out': True}), pool.EXIT_TIMEOUT)
        self.assertEqual(pool._exit_code({'rc': -9, 'oom_kills': 1}), pool.EXIT_OOM)
        self.assertEqual(pool._exit_code({'rc': None, 'signalled': True}), pool.EXIT_SIGNALLED)


class Tree(unittest.TestCase):
    def test_tracked_and_untracked_files_but_not_ignored_ones(self):
        with tempfile.TemporaryDirectory() as d:
            git = lambda *a: subprocess.run(['git', '-C', d, *a], check=True, capture_output=True)  # noqa: E731
            git('init', '-q')
            for name, text in [('a.py', 'a'), ('.gitignore', 'build/\n'), ('build/x', 'x'), ('new_test.py', 'n')]:
                os.makedirs(os.path.dirname(os.path.join(d, name)), exist_ok=True)
                with open(os.path.join(d, name), 'w') as f:
                    f.write(text)
            git('add', 'a.py', '.gitignore')
            files = pool.tree_files(d)
            self.assertEqual(files, ['.gitignore', 'a.py', 'new_test.py'])
            import tarfile
            names = tarfile.open(fileobj=io.BytesIO(pool.bundle(d, files, os.path.join(ROOT, 'ci_runners')))).getnames()
            self.assertIn('ws/new_test.py', names)
            self.assertIn('pkg/ci_runners/pool.py', names)
            self.assertNotIn('ws/build/x', names)


    def test_the_staged_git_sees_the_callers_merge_base_files_and_status(self):
        with tempfile.TemporaryDirectory() as d:
            src, ws = os.path.join(d, 'src'), os.path.join(d, 'out')
            os.makedirs(src)

            def git(*a, cwd=src):
                return subprocess.run(['git', '-C', cwd, '-c', 'user.name=t', '-c', 'user.email=t@t', *a], check=True,
                                      capture_output=True, text=True).stdout.strip()
            git('init', '-q')
            for i in range(3):  # main's history
                with open(os.path.join(src, f'm{i}.py'), 'w') as f:
                    f.write(str(i))
                git('add', '.')
                git('commit', '-q', '-m', f'm{i}')
            base = git('rev-parse', 'HEAD')
            git('update-ref', 'refs/remotes/origin/main', base)
            git('checkout', '-q', '-b', 'topic')
            for i in range(2):  # the branch's commits
                with open(os.path.join(src, f't{i}.py'), 'w') as f:
                    f.write(str(i))
                git('add', '.')
                git('commit', '-q', '-m', f't{i}')
            with open(os.path.join(src, 'm0.py'), 'w') as f:
                f.write('changed')  # a local change
            with open(os.path.join(src, 'staged.py'), 'w') as f:
                f.write('s')
            git('add', 'staged.py')  # added, not committed
            with open(os.path.join(src, 'loose.py'), 'w') as f:
                f.write('u')  # untracked
            with tempfile.TemporaryDirectory() as g:
                data = pool.bundle(src, pool.tree_files(src), git=pool.git_dir(src, g))
            import tarfile
            with tarfile.open(fileobj=io.BytesIO(data)) as tar:
                tar.extractall(ws, filter='tar')
            there = os.path.join(ws, 'ws')
            self.assertEqual(git('merge-base', 'origin/main', 'HEAD', cwd=there), base)
            self.assertEqual(git('rev-parse', 'HEAD', cwd=there), git('rev-parse', 'HEAD'))
            self.assertEqual(git('ls-files', cwd=there), git('ls-files'))
            self.assertEqual(git('status', '--porcelain', cwd=there), git('status', '--porcelain'))
            self.assertEqual(git('diff', '--name-only', 'origin/main', cwd=there), git('diff', '--name-only', 'origin/main'))

class FakeGate:
    """The gate's verbs as ``OwnerGate`` calls them, over a scripted job."""

    def __init__(self, states, log=b'line 1\nline 2\n', exit=None, fail_on=None):
        self.states, self.log, self.exit, self.fail_on = list(states), log, exit or {'rc': 0}, fail_on
        self.calls = []

    def __call__(self, argv, **kw):
        if argv[0] == 'rsync':
            self.calls.append(('stage', argv[-1]))
            return subprocess.CompletedProcess(argv, 0, b'', b'')
        cmd = argv[-1]
        if 'fingerprint' in cmd or 'importlib.metadata' in cmd:
            return subprocess.CompletedProcess(argv, 0, '{"numpy": "1"}\n', '')
        words = cmd.split("'")[1].split()  # SSH_ORIGINAL_COMMAND='ci-slurm-gate VERB ARGS'
        verb = words[1]
        self.calls.append((verb, kw.get('input')))
        if verb == self.fail_on:
            raise KeyboardInterrupt
        if verb == 'submit':
            return subprocess.CompletedProcess(argv, 0, b'{"job": 42}', b'')
        if verb == 'status':
            st = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            done = st not in ('PENDING', 'RUNNING')
            return subprocess.CompletedProcess(argv, 0, json.dumps({'state': st, 'log_size': 0 if st == 'PENDING' else
                                                                    len(self.log), 'exit': self.exit if done else None,
                                                                    'reason': 'Resources'}).encode(), b'')
        if verb == 'log':
            return subprocess.CompletedProcess(argv, 0, self.log[int(words[3]):], b'')
        if verb == 'cancel':
            return subprocess.CompletedProcess(argv, 0, b'{}', b'')
        raise AssertionError(verb)


class ClusterRun(unittest.TestCase):
    def setUp(self):
        self.r = reg()
        self.m = self.r.machines['cl']
        self.root = tempfile.mkdtemp()
        with open(os.path.join(self.root, 'x.py'), 'w') as f:
            f.write('x')
        self.orig = pool.fingerprint
        pool.fingerprint = lambda *a, **k: {'numpy': '1'}

    def tearDown(self):
        pool.fingerprint = self.orig
        subprocess.run(['rm', '-rf', self.root])

    def go(self, gate, target='cpu', **kw):
        a = pool.RunArgs('pytest -q', self.root, pool.Ask(cards=kw.pop('cards', 0)), timeout_min=kw.pop('timeout', 20),
                         queue_min=kw.pop('queue', 0.0))
        out = io.BytesIO()
        rc = pool.run_cluster(self.r, self.m, self.m.targets[target], a, ['x.py'], gate, sleep=lambda s: None, out=out)
        return rc, out.getvalue(), [c[0] for c in gate.calls]

    def test_a_job_runs_its_log_is_relayed_and_its_code_returned(self):
        g = FakeGate(['PENDING', 'RUNNING', 'COMPLETED'], exit={'rc': 1})
        a = pool.RunArgs('pytest -q', self.root, pool.Ask(), queue_min=5)
        out = io.BytesIO()
        rc = pool.run_cluster(self.r, self.m, self.m.targets['cpu'], a, ['x.py'], g, sleep=lambda s: None, out=out)
        self.assertEqual(rc, 1)
        self.assertEqual(out.getvalue(), b'line 1\nline 2\n')
        verbs = [c[0] for c in g.calls]
        self.assertEqual(verbs[:2], ['stage', 'submit'])
        self.assertNotIn('cancel', verbs)

    def test_the_request_passes_the_gates_checks_and_the_key_its_pattern(self):
        g = FakeGate(['COMPLETED'])
        self.go(g, target='v100', cards=1, queue=1)
        req = json.loads(dict(g.calls)['submit'])
        for k, (ok, required) in slurmgate.REQUEST.items():
            self.assertTrue(k in req or not required, k)
            if k in req:
                self.assertTrue(ok(req[k]), k)
        self.assertEqual((req['target'], req['cards'], req['host']), ('v100', 1, 'pool'))
        key = pool.run_key('Owner/Proj', 1_800_000_000.5, 1234)
        self.assertRegex(key, slurmgate.KEY)
        self.assertEqual(key, 'owner/proj/1800000000500-1/pool-1234-1')

    def test_a_job_not_started_within_the_bound_is_withdrawn(self):
        g = FakeGate(['PENDING'])
        rc, _, verbs = self.go(g, queue=0.0)
        self.assertEqual(rc, pool.NOT_STARTED)
        self.assertEqual(verbs[-1], 'cancel')

    def test_a_submit_that_fails_after_sbatch_still_cancels(self):
        g = FakeGate(['RUNNING'], fail_on='submit')
        with self.assertRaises(KeyboardInterrupt):
            self.go(g, queue=5)
        self.assertEqual([c[0] for c in g.calls][-1], 'cancel')

    def test_a_failed_status_read_is_tried_again(self):
        g = FakeGate(['RUNNING', 'COMPLETED'], exit={'rc': 0})
        real, fails = g.__call__, [1]

        def flaky(argv, **kw):
            if 'status' in str(argv[-1]) and fails:
                fails.pop()
                return subprocess.CompletedProcess(argv, 1, b'', b'ci-slurm-gate: refused: squeue timed out')
            return real(argv, **kw)
        a = pool.RunArgs('pytest -q', self.root, pool.Ask(), queue_min=5)
        rc = pool.run_cluster(self.r, self.m, self.m.targets['cpu'], a, ['x.py'], flaky, sleep=lambda s: None,
                              out=io.BytesIO())
        self.assertEqual(rc, 0)

    def test_an_interrupted_run_cancels_its_job(self):
        g = FakeGate(['RUNNING'], fail_on='log')
        with self.assertRaises(KeyboardInterrupt):
            self.go(g, queue=5)
        self.assertEqual(g.calls[-1][0], 'cancel')

    def test_a_timeout_past_the_targets_bound_is_refused(self):
        g = FakeGate(['COMPLETED'])
        rc, _, verbs = self.go(g, target='v100', cards=1, timeout=60)
        self.assertEqual((rc, verbs), (pool.EXIT_USAGE, []))

    def test_the_gate_command_is_the_forced_commands_form(self):
        g = pool.OwnerGate(self.m)
        cmd = g._cmd(['status', 'owner/proj/1-1/pool-1-1'])
        self.assertEqual(cmd, "SSH_ORIGINAL_COMMAND='ci-slurm-gate status owner/proj/1-1/pool-1-1' exec "
                              "/fs/root/ci-runners/releases/hook/bin/ci-slurm-gate")


class PoolRun(unittest.TestCase):
    def test_no_room_anywhere_says_so(self):
        r = reg()
        rows = [row('hosta', slots_free=0, cpus_free=0)]
        a = pool.RunArgs('true', ROOT, pool.Ask(), no_wait=True)
        self.assertEqual(pool.pool_run(r, a, rows=rows), pool.EXIT_NO_ROOM)

    def test_it_falls_through_to_the_next_machine_when_one_has_no_room(self):
        r = reg()
        rows = [row('hosta', cpus_free=12), row('cl:cpu', billed=True, target='cpu')]
        tried = []
        orig_h, orig_c = pool.run_host, pool.run_cluster
        pool.run_host = lambda reg_, m, *a, **k: tried.append(m.key) or pool.NOT_STARTED
        pool.run_cluster = lambda reg_, m, t, *a, **k: tried.append(f'{m.key}:{t.name}') or 5
        try:
            rc = pool.pool_run(r, pool.RunArgs('true', ROOT, pool.Ask()), rows=rows)
        finally:
            pool.run_host, pool.run_cluster = orig_h, orig_c
        self.assertEqual((rc, tried), (5, ['hosta', 'cl:cpu']))

    def test_a_commands_own_75_is_its_code_not_a_retry(self):
        r = reg()
        rows = [row('hosta', cpus_free=12), row('cl:cpu', billed=True, target='cpu')]
        tried = []
        orig = pool.run_host
        pool.run_host = lambda reg_, m, *a, **k: tried.append(m.key) or 75
        try:
            rc = pool.pool_run(r, pool.RunArgs('true', ROOT, pool.Ask()), rows=rows)
        finally:
            pool.run_host = orig
        self.assertEqual((rc, tried), (75, ['hosta']))


if __name__ == '__main__':
    unittest.main()
