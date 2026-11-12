"""Busy-host avoidance (design decision 13.2): the load record, the busy rule and where
``part`` reads it."""
from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from ci_runners import hostload, inventory
from tests.test_part import Host

PEER = 'peer_labels = { part = ["hz-cpu"], light = ["hz-light"] }\n'
STAT = 'cpu  100 20 30 1000 50 5 5 10 0 0\ncpu0 1 1 1 1 1 1 1 1 0 0\nintr 1\n'
MEMINFO = 'MemTotal:       197000000 kB\nMemFree:  1 kB\nMemAvailable:   187000000 kB\n'
CGROUP = '0::/user.slice/user-4242.slice/user@4242.service/app.slice/ci-hostload.service\n'
MANAGER = '/user.slice/user-4242.slice/user@4242.service'
TH = {'window_minutes': 10, 'cpu_cores': 14, 'memory_gib': 94.0, 'gpu_mib': 2048, 'lock_minutes': 20}
NOW = 1_800_000_000.0


def minutes(n, now=NOW, **values):
    """``n`` samples, one a minute, the newest at ``now``."""
    return [{'t': now - 60 * i, **values} for i in reversed(range(n))]


class Parsers(unittest.TestCase):
    def test_cpu_busy_seconds(self):
        # user nice system irq softirq steal; idle and iowait are idle; the per-CPU lines are not read
        self.assertEqual(hostload.cpu_busy_seconds(STAT, 100), (100 + 20 + 30 + 5 + 5 + 10) / 100)
        self.assertIsNone(hostload.cpu_busy_seconds('cpu0 1 2 3 4 5 6 7 8\n', 100))
        self.assertIsNone(hostload.cpu_busy_seconds('cpu  1 2 3\n', 100))
        self.assertIsNone(hostload.cpu_busy_seconds('cpu  1 2 x 4 5 6 7 8\n', 100))

    def test_keyed(self):
        self.assertEqual(hostload.keyed(MEMINFO, 'MemAvailable'), 187000000)
        self.assertEqual(hostload.keyed('usage_usec 123\nuser_usec 100\n', 'usage_usec'), 123)
        self.assertIsNone(hostload.keyed('anon x\n', 'anon'))
        self.assertIsNone(hostload.keyed('', 'anon'))

    def test_the_ci_accounts_manager_cgroup(self):
        self.assertEqual(hostload.manager_cgroup(CGROUP), MANAGER)
        self.assertEqual(hostload.manager_cgroup('0::/system.slice/x.service\n'), '')  # not under a user manager
        self.assertEqual(hostload.manager_cgroup('12:cpu:/a\n0::/user.slice/user@1.service\n'), '')  # cgroup v1
        self.assertEqual(hostload.manager_cgroup('0::/user.slice/user@1.service\n1:name=systemd:/user.slice\n'), '')  # hybrid
        self.assertEqual(hostload.manager_cgroup(''), '')

    def test_exclusive_flocks_on_one_file(self):
        locks = ('1: FLOCK  ADVISORY  WRITE 4242 fa:02:1234 0 EOF\n'
                 '2: FLOCK  ADVISORY  READ  4343 fa:02:1234 0 EOF\n'   # shared: not exclusive
                 '3: POSIX  ADVISORY  WRITE 4444 fa:02:1234 0 EOF\n'   # fcntl, not flock
                 '4: FLOCK  ADVISORY  WRITE 4545 fa:02:9999 0 EOF\n'   # another file
                 '5: FLOCK  ADVISORY  WRITE 4646 fa:03:1234 0 EOF\n'   # another device
                 '6: FLOCK  ADVISORY  WRITE 4747 zz:02:1234 0 EOF\n'
                 '7: FLOCK  ADVISORY  WRITE 4848 fa:02:1234 0 EOF\n')
        self.assertEqual(hostload.write_flocks(locks, (0xfa, 2), 1234), [4242, 4848])

    def test_compute_apps(self):
        csv = '123, 00000000:01:00.0, 500\n[N/A], 00000000:01:00.0, 1\n7, 00000000:4D:00.0, [N/A]\n8, x\n'
        self.assertEqual(hostload.compute_mib(csv), [(123, '00000000:01:00.0', 500)])


class Sample(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = self.tmp.name
        self.lock = pathlib.Path(self.tmp.name, 'host.lock')
        self.lock.write_text('')
        st = os.stat(self.lock)
        dev = f'{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}'
        self.files = {
            '/proc/self/cgroup': CGROUP, '/proc/stat': STAT, '/proc/meminfo': MEMINFO,
            f'/sys/fs/cgroup{MANAGER}/cpu.stat': 'usage_usec 300000\n',
            f'/sys/fs/cgroup{MANAGER}/memory.stat': f'file 1\nanon {2 << 30}\n',
            '/proc/locks': f'1: FLOCK  ADVISORY  WRITE 10 {dev} 0 EOF\n2: FLOCK  ADVISORY  WRITE 900 {dev} 0 EOF\n'}
        self.smi = {'--query-gpu=index,pci.bus_id': '0, 00000000:01:00.0\n1, 00000000:4D:00.0\n',
                    '--query-compute-apps=pid,gpu_bus_id,used_memory':
                        '900, 00000000:4D:00.0, 3000\n901, 00000000:4D:00.0, 100\n10, 00000000:01:00.0, 5000\n'}

    def tearDown(self):
        self.tmp.cleanup()

    def run_smi(self, argv, **_kw):
        return subprocess.CompletedProcess(argv, 0, self.smi.get(argv[1], ''), '')

    def kw(self, t, **over):
        return {'read': lambda p: self.files.get(p, ''), 'run': self.run_smi, 'mine': lambda pid: pid < 100,
                'hz': 100, 'now': lambda: t, **over}

    def test_one_reading(self):
        """Every value is other accounts': the CI account's CPU and anon memory are taken off,
        a compute process or a lock holder of ours (by its cgroup, decision 16.4) is not counted."""
        r = hostload.raw(str(self.lock), **self.kw(NOW))
        self.assertEqual((r['t'], r['cpu_s'], r['ci_cpu_s']), (NOW, 1.7, 0.3))
        self.assertAlmostEqual(r['mem_total'], 197000000 / (1 << 20))
        self.assertAlmostEqual(r['mem'], 10000000 / (1 << 20) - 2)  # used minus the CI account's 2 GiB of anon
        self.assertEqual(r['gpu'], {'0': 0, '1': 3100})  # pid 10 is ours
        self.assertEqual(r['lock'], 900)
        self.assertIsNone(hostload.raw('', **self.kw(NOW))['lock'])  # no host lock on this host
        self.assertIsNone(hostload.raw(str(self.lock) + '.gone', **self.kw(NOW))['lock'])

    def test_without_the_ci_accounts_memory_no_memory_sample(self):
        """Its own parts would count as another account's: fail open, as for the CPU."""
        del self.files[f'/sys/fs/cgroup{MANAGER}/memory.stat']
        r = hostload.raw('', **self.kw(NOW))
        self.assertIsNone(r['mem'])
        self.assertIsNotNone(r['mem_total'])

    def test_unreadable_values_are_none(self):
        r = hostload.raw('', **self.kw(NOW, read=lambda p: '', run=mock.Mock(side_effect=OSError)))
        self.assertEqual({k: r[k] for k in ('cpu_s', 'ci_cpu_s', 'mem_total', 'mem', 'gpu', 'lock')},
                         {'cpu_s': None, 'ci_cpu_s': None, 'mem_total': None, 'mem': None, 'gpu': {}, 'lock': None})

    def test_derive_the_cpu_rate(self):
        prev = {'t': NOW - 60, 'cpu_s': 1000.0, 'ci_cpu_s': 100.0}
        line = {'t': NOW, 'cpu_s': 1960.0, 'ci_cpu_s': 160.0}
        self.assertEqual(hostload.derive(line, prev, 28)['cpu'], 15.0)  # (960 - 60) / 60
        self.assertIsNone(hostload.derive(line, None, 28)['cpu'])
        self.assertIsNone(hostload.derive(line, {**prev, 't': NOW - 301}, 28)['cpu'])  # too old a previous line
        self.assertIsNone(hostload.derive(line, {**prev, 't': NOW}, 28)['cpu'])
        self.assertIsNone(hostload.derive(line, {**prev, 'ci_cpu_s': None}, 28)['cpu'])
        self.assertEqual(hostload.derive({**line, 'ci_cpu_s': 1120.0}, prev, 28)['cpu'], 0.0)  # never negative (17 > 16)
        # the user manager restarted: its counter went back, and the delta would be the host's busy time
        self.assertIsNone(hostload.derive({**line, 'ci_cpu_s': 5.0}, prev, 28)['cpu'])
        self.assertIsNone(hostload.derive({**line, 'ci_cpu_s': 2000.0}, prev, 28)['cpu'])  # more than 28 cores
        self.assertIsNone(hostload.derive(line, prev, 14)['cpu'])  # 16 host cores on a 14-CPU host
        self.assertEqual(hostload.derive(line, prev, 16)['cpu'], 15.0)  # at the bound

    def test_sample_appends_a_line(self):
        first = hostload.sample(self.data, str(self.lock), cpus=28, **self.kw(NOW - 60))
        self.assertIsNone(first['cpu'])
        self.files['/proc/stat'] = 'cpu  60100 20 30 1000 50 5 5 10 0 0\n'  # +600 s busy
        self.files[f'/sys/fs/cgroup{MANAGER}/cpu.stat'] = 'usage_usec 60300000\n'  # +60 s the CI account's
        second = hostload.sample(self.data, str(self.lock), cpus=28, **self.kw(NOW))
        self.assertEqual(second['cpu'], 9.0)
        path = hostload.day_file(self.data, NOW)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        with open(path, 'a') as f:
            f.write('not json\n[]\n{"t": "x"}\n')
        got = hostload.lines(self.data, NOW - 120, NOW)
        self.assertEqual([d['t'] for d in got], [NOW - 60, NOW])
        self.assertEqual(hostload.lines(self.data, NOW - 30, NOW), [second])

    def test_lines_span_two_days_once_each(self):
        midnight = time.mktime(time.strptime('2026-11-08', '%Y-%m-%d'))
        d = hostload.record_dir(self.data)
        os.makedirs(d)
        for t in (midnight - 120, midnight - 60, midnight + 60):
            with open(hostload.day_file(self.data, t), 'a') as f:
                f.write(json.dumps({'t': t, 'cpu': 1}) + '\n')
        with open(hostload.day_file(self.data, midnight + 60), 'a') as f:
            f.write(json.dumps({'t': midnight + 60, 'cpu': 2}) + '\n')  # the same time twice: one line
        self.assertEqual(sorted(os.listdir(d)), ['2026-11-07.jsonl', '2026-11-08.jsonl'])
        got = hostload.lines(self.data, midnight - 90, midnight + 90)
        self.assertEqual([x['t'] for x in got], [midnight - 60, midnight + 60])


class Rule(unittest.TestCase):
    def test_the_defaults(self):
        """Decision 13.2: half the CPUs, half the memory rounded down, 2048 MiB, 20 and 10 min."""
        self.assertEqual(hostload.thresholds(None, cpus=28, mem_total_gib=188.4), TH)
        self.assertEqual(hostload.thresholds({}, cpus=24, mem_total_gib=125.7)['memory_gib'], 62.0)
        self.assertEqual(hostload.thresholds({}, cpus=1, mem_total_gib=None)['cpu_cores'], 1)
        self.assertIsNone(hostload.thresholds({}, cpus=4, mem_total_gib=None)['memory_gib'])
        th = hostload.thresholds({'cpu_cores': 6, 'lock_minutes': 2}, cpus=28, mem_total_gib=188.4)
        self.assertEqual((th['cpu_cores'], th['lock_minutes'], th['gpu_mib']), (6, 2, 2048))

    def test_cpu_held_in_every_sample_names_value_and_threshold(self):
        rec = minutes(10, cpu=17.2)
        rec[3]['cpu'] = 15.5
        self.assertEqual(hostload.busy(rec, NOW, TH), 'busy: cpu 15.5 >= 14 cores for 10 min')
        rec[5]['cpu'] = 14  # at the threshold is busy
        self.assertEqual(hostload.busy(rec, NOW, TH), 'busy: cpu 14 >= 14 cores for 10 min')
        rec[5]['cpu'] = 13.9  # one sample below: not held
        self.assertEqual(hostload.busy(rec, NOW, TH), '')

    def test_fewer_than_half_the_samples_is_not_busy(self):
        """Fail open: a stopped timer or a fresh boot never refuses."""
        self.assertEqual(hostload.busy(minutes(4, cpu=20), NOW, TH), '')
        self.assertEqual(hostload.busy(minutes(5, cpu=20), NOW, TH), 'busy: cpu 20 >= 14 cores for 10 min')
        rec = minutes(10, cpu=20)
        for d in rec[:6]:
            d['cpu'] = None  # no rate (the first line after a gap)
        self.assertEqual(hostload.busy(rec, NOW, TH), '')
        self.assertEqual(hostload.busy([], NOW, TH), '')

    def test_only_the_window_counts(self):
        rec = minutes(20, cpu=1.0)
        for d in rec[-10:]:
            d['cpu'] = 20.0
        self.assertEqual(hostload.busy(rec, NOW, TH), 'busy: cpu 20 >= 14 cores for 10 min')
        self.assertEqual(hostload.busy(rec, NOW - 60, TH), '')  # its window holds an idle minute
        self.assertEqual(hostload.busy(rec, NOW + 600, TH), '')  # every sample is older than the window
        rec = minutes(11, cpu=20.0)
        rec[-1]['cpu'] = 1.0
        self.assertEqual(hostload.busy(rec, NOW - 60, TH), 'busy: cpu 20 >= 14 cores for 10 min')  # not a later one

    def test_memory(self):
        self.assertEqual(hostload.busy(minutes(10, cpu=1, mem=100.04), NOW, TH),
                         'busy: memory 100.0 >= 94 GiB for 10 min')
        self.assertEqual(hostload.busy(minutes(10, cpu=1, mem=93.9), NOW, TH), '')
        self.assertEqual(hostload.busy(minutes(10, mem=500), NOW, {**TH, 'memory_gib': None}), '')

    def test_a_card_only_where_the_part_may_lease_it(self):
        rec = minutes(10, gpu={'0': 0, '1': 4000})
        self.assertEqual(hostload.busy(rec, NOW, TH, cards=[1]), 'busy: card 1 4000 >= 2048 MiB for 10 min')
        self.assertEqual(hostload.busy(rec, NOW, TH, cards=[0, 1]), 'busy: card 1 4000 >= 2048 MiB for 10 min')
        self.assertEqual(hostload.busy(rec, NOW, TH, cards=[0]), '')
        self.assertEqual(hostload.busy(rec, NOW, TH), '')  # a CPU part
        rec[-1]['gpu'] = {'0': 0, '1': 2000}
        self.assertEqual(hostload.busy(rec, NOW, TH, cards=[1]), '')

    def test_the_host_lock_held_by_one_holder_all_along(self):
        rec = minutes(20, lock=4242)
        why = 'busy: host lock held exclusively by another account for >= 20 min'
        self.assertEqual(hostload.busy(rec, NOW, TH, lock=True), why)
        self.assertEqual(hostload.busy(rec, NOW, TH, lock=False), '')  # no host lock on this host
        self.assertEqual(hostload.busy(rec[-9:], NOW, TH, lock=True), '')  # fewer than half the lock window
        self.assertEqual(hostload.busy(rec[-10:], NOW, TH, lock=True), why)
        rec[4]['lock'] = None
        self.assertEqual(hostload.busy(rec, NOW, TH, lock=True), '')  # released once
        rec[4]['lock'] = 4343
        self.assertEqual(hostload.busy(rec, NOW, TH, lock=True), '')  # another holder: two short holds

    def test_the_marker_is_per_repository_head_and_part(self):
        m = hostload.marker('/d', 'Org/Repo', 'abc', 'unit')
        self.assertEqual(m, hostload.marker('/d', 'org/repo', 'abc', 'unit'))
        self.assertTrue(m.startswith(os.path.join(hostload.record_dir('/d'), 'refused') + '/'))
        self.assertEqual(len({m, hostload.marker('/d', 'org/repo', 'abd', 'unit'),
                              hostload.marker('/d', 'org/repo', 'abc', 'unit2'),
                              hostload.marker('/d', 'org/other', 'abc', 'unit')}), 4)


class Gate(Host):
    """``part`` on a busy host: a first attempt goes to the peer pool once per head and part."""

    def setUp(self):
        super().setUp()
        self.inv_text = self.inv_text.replace('max_temp_c = 80\n', 'max_temp_c = 80\n' + PEER +
                                              'busy = { cpu_cores = 4, memory_gib = 500 }\n')
        self.inv = inventory.load(self.inv_text)
        self.env = {**self.env, 'GITHUB_SHA': 'a' * 40}
        self.now = time.time()

    def record(self, rows):
        os.makedirs(hostload.record_dir(str(self.data)), exist_ok=True)
        for d in rows:
            with open(hostload.day_file(str(self.data), d['t']), 'a') as f:
                f.write(json.dumps(d) + '\n')

    def busy_host(self):
        self.record(minutes(10, now=self.now - 1, cpu=6.5, mem=1.0, gpu={'0': 0, '1': 0}, lock=None))

    def ran(self, out):
        return pathlib.Path(self.path(out), 'work', 'ran').exists()

    def test_a_busy_host_sends_the_first_attempt_to_the_peer_once(self):
        self.busy_host()
        code, out = self.execute(command='touch "$CI_PART_DIR/ran"')
        self.assertEqual((code, out['outcome'], out['reason'], out['next'], out['retry-on']),
                         (0, 'unavailable', 'busy: cpu 6.5 >= 4 cores for 10 min', 'fallback', ['self-hosted', 'hz-cpu']))
        self.assertFalse(self.ran(out))
        mark = hostload.marker(str(self.data), 'org/repo', 'a' * 40, 'unit')
        self.assertEqual(pathlib.Path(mark).read_text(), 'busy: cpu 6.5 >= 4 cores for 10 min\n')
        rerun = {**self.env, 'GITHUB_RUN_ATTEMPT': '2'}
        _, out = self.execute(rerun, command='touch "$CI_PART_DIR/ran"')  # the same head and part: it stays
        self.assertEqual(out['outcome'], 'pass')
        _, out = self.execute(id='other')  # another part of the head is still sent
        self.assertEqual(out['reason'], 'busy: cpu 6.5 >= 4 cores for 10 min')
        _, out = self.execute({**self.env, 'GITHUB_SHA': 'b' * 40, 'GITHUB_RUN_ID': '78'})  # a new head
        self.assertEqual(out['outcome'], 'unavailable')

    def test_a_retry_light_or_no_peer_is_never_sent(self):
        self.busy_host()
        _, out = self.execute(attempt_of='hz:org/repo/77-1/unit-1')
        self.assertEqual((out['outcome'], out['attempt']), ('pass', 2))
        _, out = self.execute({**self.env, 'CI_RUNNER_CLASS': 'light'}, id='l')
        self.assertEqual(out['outcome'], 'pass')
        self.inv = inventory.load(self.inv_text.replace(PEER, ''))
        _, out = self.execute(id='p')
        self.assertEqual(out['outcome'], 'pass')
        self.assertFalse(os.path.isdir(os.path.join(hostload.record_dir(str(self.data)), 'refused')))

    def test_no_retry_budget_queues_here(self):
        """A project with ``retries = 0`` cannot go elsewhere: sent away it would end an
        error, so it waits here as before v1.5."""
        self.busy_host()
        p = self.etc / 'projects' / 'p.toml'
        p.write_text(p.read_text().replace('memory = "1G"\n', 'memory = "1G"\nretries = 0\n'))
        _, out = self.execute()
        self.assertEqual(out['outcome'], 'pass')
        self.assertFalse(os.path.isdir(os.path.join(hostload.record_dir(str(self.data)), 'refused')))

    def test_the_card_rule_through_part(self):
        """A GPU part is sent when another account holds its card; a CPU part on the same
        host is not."""
        text = self.inv_text.replace('pools = { light = 1, part = 1 }',
                                     'pools = { light = 1, part = 1, cards = { per_card = true, class = "gpu" } }')
        self.inv = inventory.load(text.replace(PEER, 'peer_labels = { part = ["hz-cpu"], gpu = ["hz-gpu"] }\n'))
        self.record(minutes(10, now=self.now - 1, cpu=0.5, mem=1.0, gpu={'0': 0, '1': 5000}, lock=None))
        gpu = {**self.env, 'CI_RUNNER_CLASS': 'gpu', 'RUNNER_NAME': 'hb-gpu1', 'CI_RUNNER_CARD': '1'}
        code, out = self.execute(gpu, id='g', gpus=1, command='touch "$CI_PART_DIR/ran"')
        self.assertEqual((out['outcome'], out['reason'], out['retry-on']),
                         ('unavailable', 'busy: card 1 5000 >= 2048 MiB for 10 min', ['self-hosted', 'hz-gpu']))
        self.assertFalse(self.ran(out))
        _, out = self.execute(id='c')  # the same host, a CPU part
        self.assertEqual(out['outcome'], 'pass')

    def test_an_idle_or_unrecorded_host_runs_it(self):
        _, out = self.execute(id='a')  # no record: fail open
        self.assertEqual(out['outcome'], 'pass')
        self.record(minutes(10, now=self.now - 1, cpu=3.9, mem=1.0))
        _, out = self.execute(id='b')
        self.assertEqual(out['outcome'], 'pass')

    def test_check_reads_the_hosts_thresholds_cards_and_lock(self):
        self.record(minutes(30, now=self.now - 1, cpu=0.5, mem=600.0, mem_total=900.0, gpu={'1': 3000}, lock=7))
        self.assertEqual(hostload.check(self.inv, now=self.now), 'busy: memory 600.0 >= 500 GiB for 10 min')
        inv = inventory.load(self.inv_text.replace('memory_gib = 500', 'memory_gib = 700'))
        self.assertEqual(hostload.check(inv, now=self.now), '')
        self.assertEqual(hostload.check(inv, cards=[1], now=self.now), 'busy: card 1 3000 >= 2048 MiB for 10 min')
        no_mem = inventory.load(self.inv_text.replace('cpu_cores = 4, memory_gib = 500', 'cpu_cores = 4'))
        self.assertEqual(hostload.check(no_mem, now=self.now), 'busy: memory 600.0 >= 450 GiB for 10 min')  # half of 900
        self.assertEqual(hostload.check(no_mem, now=self.now, cpus=1), 'busy: memory 600.0 >= 450 GiB for 10 min')
        self.assertEqual(hostload.check(inv, now=self.now), '')  # no host lock in the inventory
        locked = inventory.load(self.inv_text.replace('memory_gib = 500', 'memory_gib = 700, lock_minutes = 30').replace(
            'files = {', f'files = {{ host_lock = "{self.data}/h.lock", '))
        self.assertEqual(hostload.check(locked, now=self.now),
                         'busy: host lock held exclusively by another account for >= 30 min')  # read 30 min back

    def test_the_cli(self):
        line = {'t': self.now, 'cpu_s': 1.0, 'ci_cpu_s': 0.0, 'mem_total': 900.0, 'mem': 1.0, 'gpu': {}, 'lock': None}
        out = io.StringIO()
        with mock.patch.object(hostload, 'raw', return_value=line) as raw, contextlib.redirect_stdout(out):
            self.assertEqual(hostload.main(['sample'], self.inv), 0)
            self.assertEqual(hostload.main(['status'], self.inv), 0)
        raw.assert_called_once_with('')
        self.assertEqual(out.getvalue().splitlines(), ['{"cpu": null, "gpu": {}, "lock": null, "mem": 1.0}', 'not busy'])
        self.assertEqual(len(hostload.lines(str(self.data), self.now - 1, self.now + 1)), 1)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(hostload.main([], self.inv), 2)
            self.assertEqual(hostload.main(['sample', 'x'], self.inv), 2)

    def test_ci_run_hostload(self):
        (self.etc / 'inventory.toml').write_text(self.inv_text)
        from ci_runners import cli
        with mock.patch.dict(os.environ, {'CI_RUNNERS_ETC': str(self.etc)}), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(['hostload', 'status']), 0)
        self.assertEqual(out.getvalue(), 'not busy\n')


class Table(unittest.TestCase):
    def test_the_busy_table(self):
        base = ('schema = 1\nthis = "h"\n[hosts.h]\naccount = "a"\nhome = "/h"\ndata = "/d"\nslots = 1\n'
                'memory_max = "1G"\ndisk_min_gib = 0\ngpus = []\nmax_temp_c = 80\npools = { part = 1 }\n'
                'files = { slot_lock = "/l", slot_queue = "/q" }\n')
        ok = 'busy = { window_minutes = 10, lock_minutes = 2, cpu_cores = 14, memory_gib = 62.5, gpu_mib = 2048 }\n'
        self.assertEqual(inventory.load(base + ok).host['busy']['memory_gib'], 62.5)
        for bad in ('busy = { cpu = 1 }', 'busy = { window_minutes = 0 }', 'busy = { window_minutes = 1.5 }',
                    'busy = { cpu_cores = 0 }', 'busy = { gpu_mib = -1 }', 'busy = { cpu_cores = "14" }',
                    'busy = { lock_minutes = true }', 'busy = 3'):
            with self.assertRaises(inventory.InventoryError, msg=bad):
                inventory.load(base + bad + '\n')


if __name__ == '__main__':
    unittest.main()
