"""The capacity file, ``pool status --fast`` and the route's reader (decision 16.5, RUN-10 R4)."""
from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
import unittest.mock

from ci_runners import capacity, pool, setup
from tests import test_pool
from tests.test_pool import CI_REGISTRY, SITE, inventory_text

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T0 = 1_800_000_000.0


def clock(t):
    return lambda: t


def answer(p, rc=0):
    return lambda argv, **kw: subprocess.CompletedProcess(argv, rc, json.dumps(p) + '\n', '')


class Account(unittest.TestCase):
    """A CI account's tree: ``<tmp>/ci-runners/etc`` with the registry (hostb local, hosta
    forced, the cluster through the site), the forced key and the site config."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.etc = os.path.join(self.tmp.name, 'ci-runners', 'etc')
        os.makedirs(os.path.join(self.etc, 'slurm', 'keys'))
        os.makedirs(os.path.join(self.etc, 'pool', 'keys'))
        site = os.path.join(self.etc, 'slurm', 'cluster1.toml')
        with open(site, 'w') as f:
            f.write(SITE)
        os.chmod(site, 0o600)
        open(os.path.join(self.etc, 'pool', 'keys', 'hosta'), 'w').close()
        self.lock = os.path.join(self.tmp.name, 'x.lock')
        open(self.lock, 'w').close()
        text = CI_REGISTRY.replace('lock = "/tmp/x.lock", count = 1', f'lock = "{self.lock}", count = 1')
        path = os.path.join(self.etc, 'pool.toml')
        with open(path, 'w') as f:
            f.write(text)
        os.chmod(path, 0o600)
        self.reg = pool.load(text)
        self.file = capacity.path(self.etc)
        self.order = list(self.reg.machines)

    def doc(self):
        with open(self.file) as f:
            return json.load(f)


class Writer(Account):
    def test_the_file_is_beside_etc_in_state(self):
        self.assertEqual(self.file, os.path.join(self.tmp.name, 'ci-runners', 'state', 'capacity.json'))

    def test_a_machines_rows_replace_its_own_and_keep_the_others_in_the_registrys_order(self):
        a = {'key': 'hosta', 'read_at': T0, 'problems': []}
        cl = [{'key': f'cl:{t}', 'read_at': T0, 'problems': []} for t in ('cpu', 'v100', 'a40')]
        capacity.merge(self.file, cl, self.order, now=clock(T0 + 1))
        capacity.merge(self.file, [a], self.order, {'key': 'hostb', 'read_at': T0, 'probe': {'slots_free': 1}},
                       now=clock(T0 + 2))
        d = self.doc()
        self.assertEqual((d['schema'], d['written_at']), (1, T0 + 2))
        self.assertEqual([r['key'] for r in d['rows']], ['hosta', 'cl:cpu', 'cl:v100', 'cl:a40'])
        capacity.merge(self.file, [dict(a, read_at=T0 + 5)], self.order, now=clock(T0 + 5))
        d = self.doc()
        self.assertEqual([(r['key'], r['read_at']) for r in d['rows']][:2], [('hosta', T0 + 5), ('cl:cpu', T0)])
        self.assertEqual(d['local'], {'key': 'hostb', 'read_at': T0, 'probe': {'slots_free': 1}})  # kept
        capacity.merge(self.file, [{'key': 'cl:cpu', 'read_at': T0 + 6}], self.order)  # a target gone from the registry
        self.assertEqual([r['key'] for r in self.doc()['rows']], ['hosta', 'cl:cpu'])
        capacity.merge(self.file, [], ['cl'])  # a machine gone from the registry: its rows and its probe go
        d = self.doc()
        self.assertEqual(([r['key'] for r in d['rows']], 'local' in d), (['cl:cpu'], False))
        self.assertEqual(os.stat(self.file).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(os.path.dirname(self.file)).st_mode & 0o777, 0o700)
        self.assertEqual(sorted(os.listdir(os.path.dirname(self.file))), ['capacity.json', 'capacity.lock'])

    def test_a_malformed_file_or_one_of_another_schema_starts_afresh(self):
        os.makedirs(os.path.dirname(self.file))
        for text in ('not json', '[]', '{"schema": 2, "rows": [{"key": "hosta"}]}', '{"schema": 1, "rows": {}}'):
            with open(self.file, 'w') as f:
                f.write(text)
            self.assertEqual(capacity.read(self.file), {'schema': 1, 'rows': []}, msg=text)
            capacity.merge(self.file, [{'key': 'cl:cpu', 'read_at': T0}], self.order)
            self.assertEqual([r['key'] for r in self.doc()['rows']], ['cl:cpu'])
        with open(self.file, 'w') as f:
            f.write('{"schema": 1, "rows": [{"key": "hosta"}, {"nokey": 1}, 3]}')
        self.assertEqual(capacity.read(self.file)['rows'], [{'key': 'hosta'}])

    def test_writers_in_parallel_lose_no_machine(self):
        threads = [threading.Thread(target=capacity.merge, args=(self.file, [{'key': k, 'read_at': T0}], self.order))
                   for k in ('hosta', 'hostb', 'cl:cpu')] * 1
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual([r['key'] for r in self.doc()['rows']], ['hosta', 'hostb', 'cl:cpu'])

    def test_status_writes_each_machine_as_it_answers(self):
        """pool status (the timer's) writes every row with read_at, the local probe with its
        row, one machine at a time: a slow cluster does not hold back the hosts' rows."""
        seen_at_cluster = []

        def run(argv, **kw):
            if 'ci-slurm-gate' in argv:  # the cluster, last: the hosts' rows are already written
                for _ in range(100):
                    rows = capacity.read(self.file)['rows']
                    if len(rows) == 2:
                        break
                    time.sleep(0.02)
                seen_at_cluster.extend(r['key'] for r in rows)
                return subprocess.CompletedProcess(argv, 255, b'', b'')
            if 'ci-run pool probe' in argv:
                return answer({'slots_free': 0, 'load': 0, 'cpus': 16, 'age': 7.5})(argv)
            return answer({'slots_free': 1, 'load': 0, 'cpus': 16, 'gpu_free': {'0': 9000}})(argv)
        rows = pool.status(self.reg, run, here='', etc=self.etc, on_rows=pool._writer(self.reg, self.etc),
                           now=clock(T0))
        self.assertEqual(sorted(seen_at_cluster), ['hosta', 'hostb'])
        d = self.doc()
        self.assertEqual(d['rows'], json.loads(json.dumps(rows)))  # the --json rows, in the registry's order
        self.assertEqual([r['read_at'] for r in d['rows']], [T0 - 7.5, T0, T0, T0, T0])  # hosta: its file's age
        self.assertEqual(d['local'], {'key': 'hostb', 'read_at': T0,
                                      'probe': {'slots_free': 1, 'load': 0, 'cpus': 16, 'gpu_free': {'0': 9000}}})
        self.assertEqual(d['rows'][2]['problems'], ['cluster unreachable'])

    def test_a_file_that_cannot_be_written_is_said_once_and_status_still_prints(self):
        os.makedirs(os.path.dirname(os.path.dirname(self.file)), exist_ok=True)
        open(os.path.dirname(self.file), 'w').close()  # state is a file: nothing can be written under it
        err, out = io.StringIO(), io.StringIO()
        with unittest.mock.patch.object(pool, 'probe_cluster', lambda m, run, *a, **k: [
                dict(pool.target_row(m, t, [], {}, {}), problems=['x']) for t in m.targets.values()]), \
                unittest.mock.patch.object(pool, '_host', lambda m, *a, **k: (dict(pool.host_row(m, None, 'y'),
                                                                                    read_at=T0), None)), \
                unittest.mock.patch('sys.stderr', err), unittest.mock.patch('sys.stdout', out):
            self.assertEqual(pool.main(['status', '--json'], self.etc), 0)
        self.assertEqual(len(json.loads(out.getvalue())), 5)
        self.assertEqual(err.getvalue().count('capacity.json not written'), 1)


class Staleness(unittest.TestCase):
    ROW = {'key': 'cl:cpu', 'access': 'slurm', 'read_at': T0, 'problems': [], 'start_now': True, 'slots_free': 1,
           'cpus_free': 40, 'gpu_free': {'0': 1}, 'jobs_free': 2, 'labels': {'cpu': 'cl-cpu'}, 'target': 'cpu'}

    def test_a_row_older_than_two_minutes_counts_as_down(self):
        self.assertEqual(capacity.STALE_S, 120)
        self.assertEqual(capacity.mark(self.ROW, T0 + 120), self.ROW)  # 2 min: still fresh
        old = capacity.mark(self.ROW, T0 + 121)
        self.assertEqual(old['problems'], ['stale (read 121 s ago)'])
        self.assertEqual({k: old[k] for k in ('start_now', 'slots_free', 'cpus_free', 'gpu_free', 'jobs_free')},
                         {'start_now': False, 'slots_free': 0, 'cpus_free': 0, 'gpu_free': {}, 'jobs_free': 0})
        self.assertEqual(self.ROW['problems'], [])  # pure
        host = {k: v for k, v in self.ROW.items() if k != 'jobs_free'}
        self.assertNotIn('jobs_free', capacity.mark(host, T0 + 500))  # a host row gains none
        for when in ({k: v for k, v in self.ROW.items() if k != 'read_at'}, dict(self.ROW, read_at='1')):
            self.assertEqual(capacity.mark(when, T0)['problems'], ['stale'])  # read at no known time

    def test_rows_for_the_route(self):
        table = {'x-cpu': {'registered': 3, 'online': 2, 'free': 1}, 'x': {'registered': 1, 'online': None, 'free': None},
                 'bad': {'registered': '3'}}
        host = {'key': 'hostb', 'access': 'local', 'read_at': T0 - 30, 'problems': [], 'slots_free': 2, 'target': '',
                'labels': {'cpu': 'x-cpu'}, 'label_runners': table}
        h, c, s = capacity.rows_from([host, self.ROW, dict(self.ROW, key='cl:v100', read_at=T0 - 200)], now=T0)
        self.assertEqual((h.key, h.machine, h.target, h.access, h.age, h.down, h.jobs_free, h.slots_free),
                         ('hostb', 'hostb', '', 'local', 30, False, None, 2))
        self.assertEqual(h.runners, {'x-cpu': capacity.Runners(3, 2, 1), 'x': capacity.Runners(1, None, None)})
        self.assertEqual(dict(h.labels), {'cpu': 'x-cpu'})
        self.assertEqual((c.machine, c.target, c.jobs_free, c.down, c.runners), ('cl', 'cpu', 2, False, {}))
        self.assertEqual((s.down, s.jobs_free, s.slots_free, s.age), (True, 0, 0, 200))
        self.assertEqual(s.problems, ('stale (read 200 s ago)',))
        self.assertTrue(capacity.rows_from([dict(host, problems=['drained'])], now=T0)[0].down)  # any problem


class Reader(Account):
    def test_load_reads_the_file_or_says_why(self):
        with self.assertRaisesRegex(capacity.CapacityError, 'no capacity file'):
            capacity.load(self.etc, fast=False)
        os.makedirs(os.path.dirname(self.file))
        with open(self.file, 'w') as f:
            f.write('{"schema": 9, "rows": []}')
        with self.assertRaisesRegex(capacity.CapacityError, 'not of schema 1'):
            capacity.load(self.etc, fast=False)
        capacity.merge(self.file, [{'key': 'hostb', 'read_at': T0, 'problems': [], 'slots_free': 1}], self.order)
        rows = capacity.load(self.etc, fast=False, now=T0 + 10)
        self.assertEqual([(r.key, r.age, r.down) for r in rows], [('hostb', 10, False)])
        self.assertTrue(capacity.load(self.etc, fast=False, now=T0 + 121)[0].down)

    def test_load_fast_is_pool_status_fast(self):
        with self.assertRaisesRegex(capacity.CapacityError, 'no registry'):
            capacity.load(os.path.join(self.tmp.name, 'nowhere'))
        rows = capacity.load(self.etc, run=answer({'slots_free': 1, 'load': 0, 'cpus': 16}))
        self.assertEqual([r.key for r in rows], ['hosta', 'hostb', 'cl:cpu', 'cl:v100', 'cl:a40'])
        self.assertEqual([r.down for r in rows], [False, False, True, True, True])  # no cluster cache yet
        self.assertEqual(rows[2].problems, ('not read', 'stale'))


class ForcedProbe(Account):
    """The forced probe returns the receiving account's file while it is fresh (16.5)."""

    def test_it_returns_the_files_probe_without_probing_while_fresh(self):
        mine = {'slots_free': 1, 'load': 0.5, 'cpus': 16, 'gpu_free': {'0': 20000}}
        row = pool.record_local(self.reg, self.etc, dict(mine), here='', now=clock(T0))
        self.assertEqual((row['key'], row['read_at'], row['slots_free']), ('hostb', T0, 1))
        self.assertEqual(self.doc()['rows'], [json.loads(json.dumps(row))])

        def no_probe(*a, **k):
            raise AssertionError('probed')
        with unittest.mock.patch.object(pool, 'probe', no_probe):
            self.assertEqual(pool.local_probe(self.reg, self.etc, now=clock(T0 + 30)), {**mine, 'age': 30})
            self.assertEqual(pool.local_probe(self.reg, self.etc, now=clock(T0 + 120))['age'], 120)
            out = io.StringIO()
            with unittest.mock.patch.object(time, 'time', clock(T0 + 3)), unittest.mock.patch('sys.stdout', out):
                self.assertEqual(pool.main(['probe'], self.etc), 0)
            self.assertEqual(json.loads(out.getvalue())['age'], 3)
        live = {'slots_free': 0, 'load': 0, 'cpus': 16}
        with unittest.mock.patch.object(pool, 'probe', lambda *a, **k: dict(live)):
            for t in (T0 + 121, T0 - 1):  # stale, or from the future: probed now
                self.assertEqual(pool.local_probe(self.reg, self.etc, now=clock(t)), live)
            other = pool.load(CI_REGISTRY.replace('[machine.hostb]', '[machine.hostc]'))
            self.assertEqual(pool.local_probe(other, self.etc, now=clock(T0 + 1)), live)  # another machine's file

    def test_the_caller_dates_the_row_by_the_answers_age(self):
        m = self.reg.machines['hosta']
        got = pool.probe_host(m, answer({'slots_free': 1, 'load': 0, 'cpus': 16, 'age': 40}), etc=self.etc, now=clock(T0))
        self.assertEqual((got['read_at'], got['slots_free'], 'age' in got), (T0 - 40, 1, False))
        for bad in (-1, '40', None):
            got = pool.probe_host(m, answer({'slots_free': 1, 'age': bad}), etc=self.etc, now=clock(T0))
            self.assertEqual(got['read_at'], T0, msg=repr(bad))
        self.assertEqual(capacity.mark(pool.probe_host(m, answer({'slots_free': 1, 'age': 130}), etc=self.etc,
                                                       now=clock(T0)), T0)['problems'], ['stale (read 130 s ago)'])


class Fast(Account):
    def cache_cluster(self, at):
        rows = [dict(pool.target_row(self.reg.machines['cl'], t, [], {}, {}), problems=[], start_now=True, jobs_free=3,
                     read_at=at) for t in self.reg.machines['cl'].targets.values()]
        capacity.merge(self.file, rows, self.order)

    def test_the_cluster_from_the_timers_file_the_hosts_asked_without_nvidia_smi(self):
        self.cache_cluster(T0 - 50)
        capacity.merge(self.file, [dict(pool.host_row(self.reg.machines['hostb'], {'slots_free': 1,
                                                                                     'gpu_free': {'1': 7000}}),
                                        read_at=T0 - 60)], self.order)
        seen = []

        def run(argv, **kw):
            seen.append((argv, kw))
            if 'ci-slurm-gate' in argv:
                raise AssertionError('the cluster is not asked')
            return answer({'slots_free': 1, 'load': 0, 'cpus': 16, 'gpu_free': {'0': 1}})(argv)
        rows = pool.fast_status(self.reg, run, here='', etc=self.etc, now=clock(T0))
        self.assertEqual([r['key'] for r in rows], ['hosta', 'hostb', 'cl:cpu', 'cl:v100', 'cl:a40'])
        self.assertEqual([r['problems'] for r in rows], [[]] * 5)
        self.assertEqual([r['jobs_free'] for r in rows[2:]], [3, 3, 3])
        self.assertEqual(rows[2]['read_at'], T0 - 50)
        forced, local = sorted(seen, key=lambda s: 'ci-run pool probe' not in s[0])
        self.assertIn('ConnectTimeout=2', forced[0])
        self.assertEqual((forced[1]['timeout'], local[1]['timeout']), (pool.FAST_TIMEOUT, pool.FAST_TIMEOUT))
        self.assertIn('smi=False)', local[1]['input'])  # no nvidia-smi on the host this account probes
        self.assertEqual(rows[1]['gpu_free'], {1: 7000})  # its cards from the file, not the answer
        self.assertEqual(rows[0]['gpu_free'], {0: 1})  # a forced answer's cards are its file's
        self.cache_cluster(T0 - 121)  # the timer stopped: the cluster's rows count as down
        capacity.merge(self.file, [dict(pool.host_row(self.reg.machines['hostb'], {'gpu_free': {'1': 7000}}),
                                        read_at=T0 - 121)], self.order)
        rows = pool.fast_status(self.reg, run, here='', etc=self.etc, now=clock(T0))
        self.assertEqual([r['start_now'] for r in rows[2:]], [False] * 3)
        self.assertEqual([r['jobs_free'] for r in rows[2:]], [0] * 3)
        self.assertEqual(rows[1]['gpu_free'], {})  # stale cards are none
        self.assertEqual(rows[1]['problems'], [])  # the slots were read now

    def test_a_host_down_unreachable_or_slow_ends_within_two_seconds(self):
        self.cache_cluster(T0)

        def hang(argv, **kw):  # a stub that ignores its timeout: the join's deadline still holds
            time.sleep(4)
            return answer({'slots_free': 1})(argv)

        def timeout(argv, **kw):
            raise subprocess.TimeoutExpired(argv, kw['timeout'])
        for name, stub, why in (('down', answer({}, rc=255), 'unreachable (exit 255)'),
                                ('slow', timeout, 'unreachable (no answer within 1.5 s)'),
                                ('hung', hang, 'not read'),
                                ('missing ssh', lambda a, **k: (_ for _ in ()).throw(FileNotFoundError('ssh')),
                                 'probe failed: FileNotFoundError')):
            with self.subTest(name):
                def run(argv, **kw):
                    if 'ci-run pool probe' in argv:
                        return stub(argv, **kw)
                    return answer({'slots_free': 1, 'load': 0, 'cpus': 16})(argv)
                t = time.monotonic()
                rows = pool.fast_status(self.reg, run, here='', etc=self.etc, now=clock(T0))
                self.assertLess(time.monotonic() - t, 2.0)
                self.assertEqual(rows[0]['problems'][0], why)
                self.assertFalse(rows[0]['start_now'] and rows[0]['problems'] == [])
                self.assertEqual(rows[1]['problems'], [])  # the others are read
                self.assertEqual(rows[2]['jobs_free'], 3)

    def test_main_fast_prints_the_rows_and_writes_nothing(self):
        out = io.StringIO()
        with unittest.mock.patch.object(pool, 'fast_status', lambda reg, etc: [{'key': 'k'}]), \
                unittest.mock.patch.object(pool, 'status', lambda *a, **k: self.fail('a full status')), \
                unittest.mock.patch('sys.stdout', out):
            self.assertEqual(pool.main(['status', '--fast', '--json'], self.etc), 0)
        self.assertEqual(json.loads(out.getvalue()), [{'key': 'k'}])
        self.assertFalse(os.path.exists(self.file))
        with unittest.mock.patch.object(pool, 'subprocess') as sp:  # a real run: nothing reachable, no file
            sp.run.side_effect = OSError('no ssh')
            sp.TimeoutExpired, sp.SubprocessError, sp.DEVNULL = subprocess.TimeoutExpired, subprocess.SubprocessError, -3
            rows = pool.fast_status(self.reg, sp.run, here='', etc=self.etc, now=clock(T0))
        self.assertTrue(all(r['problems'] for r in rows))
        self.assertFalse(os.path.exists(self.file))


class Runners(Account):
    def test_busy_runners_run_a_worker(self):
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(d, 'actions-runner')
            os.makedirs(root)
            proc = test_pool.Probe.fake_proc(d, [
                [f'{root}/hx-part-1/bin/Runner.Listener', 'run'],
                [f'{root}/hx-part-1/bin/Runner.Worker', 'spawnclient'],
                [f'{root}/hx-part-2/bin/Runner.Listener', 'run'],
                [f'{root}/hx-part-3/bin/Runner.Worker', 'spawnclient'],  # no listener: not online, so not busy
                ['python3', f'{root}/hx-part-2/bin/Runner.Worker'],  # a worker's path at argv[1]: not one
            ])
            self.assertEqual(pool.runner_procs(root, proc), ({'hx-part-1', 'hx-part-2'}, {'hx-part-1'}))
            self.assertEqual(pool.runners_alive(root, proc), {'hx-part-1', 'hx-part-2'})
            self.assertIsNone(pool.runner_procs(root, os.path.join(d, 'none')))
            with unittest.mock.patch.object(pool, 'runner_procs', lambda r: ({'b', 'a'}, {'a'})):
                p = pool.probe(self.lock, 1, '', runners=root)
            self.assertEqual((p['runners'], p['runner_names'], p['runner_busy']), (2, ['a', 'b'], ['a']))

    def test_the_fast_probe_runs_no_nvidia_smi(self):
        ran = []
        with unittest.mock.patch('subprocess.run', lambda argv, **kw: ran.append(argv[0]) or
                                 subprocess.CompletedProcess(argv, 0, '0, 1000\n', '')):
            fast, full = pool.probe(self.lock, 1, '', smi=False), pool.probe(self.lock, 1, '')
        self.assertEqual(ran, ['nvidia-smi'])  # the full probe's only
        self.assertNotIn('gpu_free', fast)
        self.assertEqual(full['gpu_free'], {0: 1000})

    def test_each_label_with_its_online_and_free_runners(self):
        self.assertIsNone(pool.label_runners(self.etc, {}))  # no inventory
        with open(os.path.join(self.etc, 'inventory.toml'), 'w') as f:
            f.write(inventory_text('hx', slurm=2))
        p = {'runner_names': ['hx-part-1', 'hx-part-2', 'hx-gpu0', 'hx-slurm-1'], 'runner_busy': ['hx-part-2', 'hx-gpu0']}
        t = pool.label_runners(self.etc, p)
        self.assertEqual(t['hx-cpu'], {'registered': 3, 'online': 2, 'free': 1})
        self.assertEqual(t['hx-gpu'], {'registered': 2, 'online': 1, 'free': 0})
        self.assertEqual(t['hx-gpu0'], {'registered': 2, 'online': 1, 'free': 0})  # a label carried twice counts once
        self.assertEqual(t['hx-gpu3'], {'registered': 1, 'online': 0, 'free': 0})
        self.assertEqual(t['shared-slurm'], {'registered': 2, 'online': 1, 'free': 1})
        self.assertEqual(t['light'], {'registered': 2, 'online': 0, 'free': 0})
        self.assertEqual(pool.label_runners(self.etc, {'runners': None})['hx-cpu'],
                         {'registered': 3, 'online': None, 'free': None})  # hidden: not known
        got = pool.probe_host(self.reg.machines['hostb'], answer({'slots_free': 1, **p}), etc=self.etc)
        self.assertEqual(got['label_runners'], t)  # the local row: this account's inventory
        m = self.reg.machines['hosta']
        self.assertEqual(pool.host_row(m, {'label_runners': t})['label_runners'], t)  # a forced answer's own
        for bad in ({'a': {'registered': 1, 'online': 2, 'free': 0}}, {'a': {'registered': 2, 'online': 1, 'free': 2}},
                    {'a': {'registered': 1, 'online': None, 'free': 0}}, {'a': {'registered': 1}}, {'a': 1}, [],
                    {'a': {'registered': -1, 'online': None, 'free': None}},
                    {'a': {'registered': 1, 'online': 1, 'free': -1}}):
            self.assertNotIn('label_runners', pool.host_row(m, {'label_runners': bad}), msg=repr(bad))


class Timer(unittest.TestCase):
    def test_the_capacity_timer_keeps_rows_fresher_than_the_staleness_bound(self):
        units = os.path.join(ROOT, 'setup', 'units')
        with open(os.path.join(units, 'ci-capacity.service')) as f:
            service = f.read()
        with open(os.path.join(units, 'ci-capacity.timer')) as f:
            timer = f.read()
        for want in ('ConditionPathExists=@CI_ETC@/pool.toml\n',  # an account without a registry: skipped
                     'ExecStart=@CI_DATA@/ci-runners/releases/hook/bin/ci-run pool status --json\n',
                     'Environment=CI_RUNNERS_ETC=@CI_ETC@\n', 'NoNewPrivileges=yes\n', 'StandardOutput=null\n'):
            self.assertIn(want, service)
        self.assertIn('OnUnitActiveSec=60s\n', timer)
        self.assertLess(60 + 5, capacity.STALE_S)  # a tick and its accuracy within the bound
        self.assertIn('WantedBy=timers.target\n', timer)
        self.assertIn('ci-capacity.timer', setup.TIMERS)
        self.assertIn('ci-capacity.service', setup.UNITS)


if __name__ == '__main__':
    unittest.main()
