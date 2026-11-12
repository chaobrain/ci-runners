"""The health gate of a per-card host, the GPU sampler and the early thermal stop, run
locally by the executor (design §5.5 items 1, 5 and 6, §5.1), on nvidia-smi fixtures."""
from __future__ import annotations

import pathlib
import subprocess
import threading
import time
import unittest

from ci_runners.resources import gpus
from ci_runners.config import inventory
from ci_runners.execution import part, sampler
from tests.execution.test_part import ROOT, Host

HOST = ROOT / 'tests' / 'fixtures' / 'host'
STATUS = (HOST / 'smi_status.csv').read_text()
APPS = (HOST / 'smi_apps.csv').read_text()


def epoch(local: str) -> float:
    return time.mktime(time.strptime(local, '%Y-%m-%d %H:%M:%S'))


class Parse(unittest.TestCase):
    def test_status_and_compute_apps(self):
        st = gpus.gpu_status(STATUS)
        self.assertEqual(sorted(st), [0, 1, 2, 3])  # card 4 reads [N/A]: no reading
        self.assertEqual(st[3], ('00000000:9E:00.0', 88.0, 100.0))
        self.assertEqual(gpus.compute_apps(APPS)[0], (12345, '00000000:9E:00.0'))
        self.assertEqual(len(gpus.compute_apps(APPS + 'x, y\n\n')), 3)

    def test_cool_cards_are_below_the_limit_coolest_first(self):
        st = gpus.gpu_status(STATUS)
        self.assertEqual(gpus.cool_cards([0, 1, 2, 3, 4], st, 80), [2, 0, 1])  # 3 is hot, 4 unread
        self.assertEqual(gpus.cool_cards([0], {0: ('b', 80.0, 0.0)}, 80), [])  # the limit itself is hot
        self.assertEqual(gpus.cool_cards([1, 0], {0: ('a', 50.0, 0.0), 1: ('b', 50.0, 0.0)}, 80), [0, 1])

    def test_taskset_where_the_inventory_gives_cpus(self):
        argv = part.command_argv('c', cpus='0-43,88-131')
        self.assertEqual(argv[:3], ['taskset', '-c', '0-43,88-131'])
        self.assertEqual(argv[-3:], ['bash', '-c', 'c'])
        self.assertNotIn('taskset', part.command_argv('c'))


class Watch(unittest.TestCase):
    def test_three_minutes_at_85_and_not_before(self):
        w = sampler.HotWatch()
        for t in range(0, 180, 30):
            self.assertIsNone(w.update(t, {3: 85.0}), t)
        self.assertEqual(w.update(180, {3: 86.0}), 3)

    def test_a_cooler_reading_ends_the_streak(self):
        w = sampler.HotWatch()
        for t in (0, 30, 60):
            w.update(t, {3: 90.0})
        w.update(90, {3: 84.0})
        for t in range(120, 300, 30):
            self.assertIsNone(w.update(t, {3: 90.0}), t)
        self.assertEqual(w.update(300, {3: 90.0}), 3)

    def test_a_missing_reading_keeps_the_streak(self):
        w = sampler.HotWatch()
        w.update(0, {3: 90.0})
        self.assertIsNone(w.update(90, {}))
        self.assertEqual(w.update(180, {3: 90.0}), 3)


class Sample(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(self._tmp())
        self.now, self.stops = 0.0, []

    def _tmp(self):
        import tempfile
        self._d = tempfile.TemporaryDirectory()
        self.addCleanup(self._d.cleanup)
        return self._d.name

    def make(self, *, early_stop=True, procs=frozenset({12345, 1})):
        def clock():
            return self.now
        return sampler.Sampler([3], csv_path=str(self.tmp / 'samples.csv'),
                               query=lambda: (gpus.gpu_status(STATUS), gpus.compute_apps(APPS)),
                               procs=lambda: set(procs), stop=lambda: self.stops.append(self.now),
                               early_stop=early_stop, clock=clock)

    def test_only_our_pids_on_our_card(self):
        s = self.make()
        s.sample()
        self.assertEqual(s.pids, {12345})  # 777 is another user's on the same card; 4242 another card's
        self.assertEqual(s.buses, {3: '00000000:9E:00.0'})
        text = (self.tmp / 'samples.csv').read_text()
        self.assertTrue(text.startswith(sampler.CSV_HEADER))
        self.assertIn(',3,00000000:9E:00.0,88,100,12345\n', text)

    def test_the_early_stop_stops_once(self):
        s = self.make()
        for self.now in range(0, 400, 30):
            s.sample()
        self.assertEqual(self.stops, [180])
        self.assertEqual((s.stopped_card, s.peak), (3, {3: 88.0}))

    def test_no_early_stop_where_it_is_off(self):
        s = self.make(early_stop=False)
        for self.now in range(0, 400, 30):
            self.assertFalse(s.sample())
        self.assertEqual(self.stops, [])

    def test_a_failed_reading_is_a_missing_sample(self):
        s = sampler.Sampler([3], csv_path=str(self.tmp / 's.csv'), query=lambda: 1 / 0, procs=set,
                            stop=lambda: self.stops.append(0), early_stop=True)
        self.assertFalse(s.sample())
        done = threading.Event()
        done.set()
        s.loop(done, 0)  # returns at once once done is set


class Finish(unittest.TestCase):
    REC = {'class': 'gpu', 'host': 'hx', 'attempt': 1, 'retries': 1, 'boot_start': 'b1',
           'run_epoch': epoch('2026-11-05 09:59:00'), 'buses': ['00000000:A8:00.0'], 'pids': [12345],
           'fallback': ['self-hosted', 'gpu']}
    EX = {'rc': 0, 'boot_end': 'b1', 'end_epoch': epoch('2026-11-05 10:01:00')}

    def test_the_early_stop_retries_on_a_cool_card(self):
        out = part.finish('/r', {**self.REC, 'early_stop': True}, {**self.EX, 'rc': -9}, cool_card=2)
        self.assertEqual((out['outcome'], out['next'], out['retry-on']), ('thermal', 'retry', ['self-hosted', 'hx-gpu2']))

    def test_thermal_without_a_cool_card_falls_back(self):
        out = part.finish('/r', {**self.REC, 'early_stop': True}, {**self.EX, 'rc': -9})
        self.assertEqual((out['outcome'], out['next'], out['retry-on']), ('thermal', 'fallback', ['self-hosted', 'gpu']))

    def test_a_reboot_is_retried_on_the_same_host(self):
        """Decision 13.4: no host counts a reboot as thermal any more, a cool card or not."""
        out = part.finish('/r', self.REC, {**self.EX, 'boot_end': 'b2'}, cool_card=0)
        self.assertEqual((out['outcome'], out['next'], out['retry-on']), ('reboot', 'retry', ['self-hosted', 'gpu', 'hx']))
        out = part.finish('/r', {**self.REC, 'class': 'part'}, {**self.EX, 'boot_end': 'b2'}, cool_card=0)
        self.assertEqual(out['retry-on'], ['self-hosted', 'part', 'hx'])


class PerCard(Host):
    """The executor on a host with a per-card pool (class ``gpu``, design §5.1)."""

    def setUp(self):
        super().setUp()
        self.inv_text = self.inv_text.replace('gpus = [0, 1]', 'gpus = [0, 1, 2, 3, 4]').replace(
            'pools = { light = 1, part = 1 }',
            'cpus = "0"\npools = { light = 1, part = 1, cards = { per_card = true, class = "gpu" } }')
        self.inv = inventory.load(self.inv_text)
        (self.etc / 'projects' / 'p.toml').write_text(
            'name = "p"\nrepository = "Org/Repo"\ncontext = "p/ci"\nmemory = "1G"\n'
            '[fallback]\ngpu = ["self-hosted", "gpu", "other"]\n'
            '[hosts.hb]\nmemory_max = "2G"\n')
        self.env = {**self.env, 'CI_RUNNER_CLASS': 'gpu', 'RUNNER_NAME': 'hb-gpu3', 'CI_RUNNER_CARD': '3'}
        self.status = [STATUS]  # successive answers; the last one repeats
        self.apps = APPS

    def run_fake(self, argv, **kw):
        if argv[0] == 'nvidia-smi':
            q = argv[1]
            if q.startswith('--query-gpu=index,memory'):
                text = ''.join(f'{i}, 81559, 1000\n' for i in range(5))
            elif q.startswith('--query-gpu=index,pci.bus_id'):
                text = self.status.pop(0) if len(self.status) > 1 else self.status[0]
            else:
                text = self.apps
            return subprocess.CompletedProcess(argv, 0, text, '')
        return subprocess.run(argv, **kw)

    def go(self, *, env=None, procs=None, clock=time.time, sample_s=0.05, **kw):
        kw.setdefault('project', 'p')
        kw.setdefault('id', 'g')
        kw.setdefault('timeout_minutes', 1)
        kw.setdefault('queue_timeout_minutes', 0.02)
        kw.setdefault('gpus', 1)
        kw.setdefault('command', 'touch "$CI_PART_DIR/ran"')
        ex = part.Executor(self.inv, str(self.etc), env or self.env, release=str(ROOT), scope=False, poll=0.05,
                           run=self.run_fake, procs=procs, clock=clock, sample_s=sample_s)
        return ex.execute(part.Inputs(**kw))

    def ran(self, out):
        return pathlib.Path(self.path(out), 'work', 'ran').exists()

    def test_a_hot_card_refuses_to_a_cool_cards_runner(self):
        code, out = self.go()  # card 3 is at 88 C
        self.assertEqual((code, out['outcome'], out['next']), (0, 'unavailable', 'fallback'))
        self.assertEqual(out['retry-on'], ['self-hosted', 'hb-gpu2'])  # the coolest that passes
        self.assertIn('health gate', out['reason'])
        self.assertFalse(self.ran(out))

    def test_an_unread_card_is_not_cool(self):
        code, out = self.go(env={**self.env, 'CI_RUNNER_CARD': '4'})
        self.assertEqual((out['outcome'], out['retry-on']), ('unavailable', ['self-hosted', 'hb-gpu2']))

    def test_every_card_hot_goes_to_the_fallback(self):
        self.status = [''.join(f'{i}, 00000000:9{i}:00.0, 85, 100\n' for i in range(5))]
        code, out = self.go()
        self.assertEqual((out['outcome'], out['retry-on']), ('unavailable', ['self-hosted', 'gpu', 'other']))

    def test_a_gpu_runner_takes_one_card(self):
        """The class row (design §4.8): one card on a per-card runner; D7's own refusal is gone (13.4)."""
        code, out = self.go(env={**self.env, 'CI_RUNNER_CARD': '2'}, gpus=2)
        self.assertEqual(out['outcome'], 'unavailable')
        self.assertIn('take 1 card', out['reason'])
        self.assertNotIn('D7', out['reason'])

    def test_cpu_runners_take_no_card(self):
        code, out = self.go(env={**self.env, 'CI_RUNNER_CLASS': 'part', 'CI_RUNNER_CARD': ''}, id='c')
        self.assertEqual(out['outcome'], 'unavailable')
        self.assertIn('take no card', out['reason'])
        code, out = self.go(env={**self.env, 'CI_RUNNER_CLASS': 'part', 'CI_RUNNER_CARD': ''}, id='c0', gpus=0)
        self.assertEqual(out['outcome'], 'pass')  # a CPU part runs

    def test_the_runner_card_must_be_the_hosts(self):
        with self.assertRaises(part.Refused):
            self.go(env={**self.env, 'CI_RUNNER_CARD': '7'})

    def test_a_cool_card_runs_pinned_and_sampled(self):
        code, out = self.go(env={**self.env, 'CI_RUNNER_CARD': '2'},
                            command='echo $CUDA_VISIBLE_DEVICES > "$CI_PART_DIR/cards"; '
                                    'grep Cpus_allowed_list /proc/self/status > "$CI_PART_DIR/cpus"; sleep 0.3')
        self.assertEqual((out['outcome'], out['gpus']), ('pass', '2'))
        work = pathlib.Path(self.path(out), 'work')
        self.assertEqual((work / 'cards').read_text().strip(), '2')
        self.assertEqual((work / 'cpus').read_text().split()[-1], '0')  # taskset -c 0
        self.assertIn(',2,00000000:9D:00.0,44,3,', pathlib.Path(self.path(out), 'samples.csv').read_text())
        self.assertIn('card 2 44 C', pathlib.Path(self.path(out), 'summary.md').read_text())

    def test_the_early_stop_ends_a_hot_part_as_thermal(self):
        hot = STATUS.replace('00000000:9D:00.0, 44, 3', '00000000:9D:00.0, 90, 100')
        self.status = [STATUS, hot]  # cool at the gate, hot from the first sample on
        ticks = iter(range(0, 10_000, 60))
        code, out = self.go(env={**self.env, 'CI_RUNNER_CARD': '2'}, command='sleep 30', sample_s=0.3,
                            clock=lambda: next(ticks))
        self.assertEqual((out['outcome'], out['reason']), ('thermal', 'our card stayed hot: stopped early'))
        self.assertEqual((out['next'], out['retry-on']), ('retry', ['self-hosted', 'hb-gpu0']))
        self.assertLess(out['run-seconds'], 20)


if __name__ == '__main__':
    unittest.main()
