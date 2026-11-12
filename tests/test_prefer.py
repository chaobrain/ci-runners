"""Decision 19 (RUN-10 R5b): the preferred host first by presence (a later tier's idle
runners yield to an earlier tier's listening ones), the route's room with the yielded
runners and its retries on a yielding host, a simulated burst, the rows' pools, policy,
health and target bounds, the policy drift warning, the part's ``pool-labels`` and
``cool-card``, and the slot speed check."""
from __future__ import annotations

import collections
import contextlib
import datetime
import io
import json
import os
import re
import tempfile
import textwrap
import unittest
import unittest.mock

from ci_runners import capacity, inventory, part, policy, pool, presence
from ci_runners import route as rt
from tests import test_presence as tp
from tests.test_pool import REGISTRY, inventory_text

T0 = 1_762_905_600.0
CAPS = re.compile(r'self-hosted|ci-[a-z0-9]+|mem-big')  # the labels a plan may name (16.1)


def P(name, labels, free=1, registered=None, **kw):
    """A ``pools`` entry: ``labels`` its runners carry, ``free`` of ``registered``."""
    n = max(free, 1) if registered is None else registered
    return {'pool': name, 'class': kw.pop('cls', name), 'labels': list(labels), 'registered': n,
            'online': None if free is None else n, 'free': free, **kw}


def host(key, *pools, tier=None, role=None, age=10, problems=(), rep=None):
    """A host row whose ``label_runners`` follow its ``pools``."""
    lr: dict = {}
    for p in pools:
        for lb in p['labels']:
            c = lr.setdefault(lb, {'registered': 0, 'online': 0, 'free': 0})
            c['registered'] += p['registered']
            c['online'] = None if p['online'] is None or c['online'] is None else c['online'] + p['online']
            c['free'] = None if p['free'] is None or c['free'] is None else c['free'] + p['free']
    row = {'key': key, 'access': 'ssh', 'target': '', 'read_at': T0 - age, 'problems': list(problems),
           'slots_free': 1, 'labels': {}, 'label_runners': lr, 'pools': list(pools), 'role': role or key}
    if tier is not None:
        row['tier'] = tier
    if rep is not None:
        row['policy'] = {'file': 'f' * 12, 'repositories': rep}
    return row


def cpu(key, free=1, **kw):
    return P('part', [f'{key}-cpu', 'ci-cpu'], free, **kw)


def target(name='cpu', jobs=4, **kw):
    return {'key': f'cl:{name}', 'access': 'slurm', 'target': name, 'read_at': T0, 'problems': [],
            'start_now': jobs > 0, 'slots_free': 1, 'jobs_free': jobs, 'gpu_model': '', 'price': 0.1,
            'numerics_ok': True, 'labels': {}, 'multi_gpu': False, **kw}


SLURMS = host('hostd', P('slurm', ['hostd-slurm', 'ci-slurm'], 4))


def rows(*rs):
    return capacity.rows_from(list(rs), now=T0)


def part_(pid='p', needs='cpu', minutes=10, **kw):
    return {'id': pid, 'command': 'python -m pytest', 'needs': needs, 'minutes': minutes, **kw}


class Route(unittest.TestCase):
    """19.3: the route counts yielded runners as room and names no host."""

    def snapshot(self, yielded=3):
        return rows(host('h1', cpu('h1', 3), tier=1), host('h2', cpu('h2', 0, registered=3, yielded=yielded), tier=2),
                    SLURMS, target(jobs=8))

    def test_yielded_runners_are_room_not_cluster(self):
        got = rt.route([part_(f'p{i}') for i in range(7)], self.snapshot())
        self.assertEqual([g['where'] for g in got], ['host'] * 6 + ['slurm'])
        self.assertTrue(all(g['labels'] == ['self-hosted', 'ci-cpu'] for g in got[:6]))
        got = rt.route([part_(f'p{i}') for i in range(4)], self.snapshot(yielded=0))
        self.assertEqual([g['where'] for g in got], ['host'] * 3 + ['slurm'])  # without them: the cluster
        bad = self.snapshot(yielded=0)
        for r in bad:
            for p in r.row.get('pools', []):
                p['yielded'] = True  # not a count
        self.assertEqual(rt.route([part_(f'p{i}') for i in range(4)], bad)[3]['where'], 'slurm')
        # a pool of other labels lends no room
        rs = rows(host('h1', cpu('h1', 0), P('gpu', ['h1-gpu', 'ci-gpu'], 0, registered=2, yielded=2)), SLURMS, target())
        self.assertEqual(rt.route([part_()], rs)[0]['where'], 'slurm')

    def test_n_plans_from_one_reading_name_only_capability_labels(self):
        snap = self.snapshot()
        for n in range(8):  # a batch of eight plans, each from the same reading
            got = rt.route([part_(f'p{i}', minutes=5 + i) for i in range(3)], snap)
            for g in got:
                self.assertTrue(all(CAPS.fullmatch(x) for x in g['labels']), g['labels'])
                self.assertNotIn('host', g)

    def test_a_retry_never_waits_on_a_yielding_host(self):
        gpu = lambda k, free=1: P('cards', [f'{k}-gpu', 'ci-gpu'], free, registered=2)  # noqa: E731
        p = rt.route([part_(needs='gpu')], rows(host('h1', gpu('h1'), tier=1), host('h2', gpu('h2'), tier=2)))[0]
        first = {'outcome': 'thermal', 'next': 'retry', 'attempt': 1, 'host': 'h2', 'cool_card': 1,
                 'pool_labels': ['h2-gpu', 'ci-gpu']}
        tiered = rows(host('h1', gpu('h1'), tier=1), host('h2', gpu('h2'), tier=2))
        cc = rt.retry(p, first, tiered)
        self.assertEqual((cc['row'], cc['labels']), ('fallback', ['self-hosted', 'ci-gpu']))
        sh = rt.retry(p, {**first, 'outcome': 'reboot'}, tiered)
        self.assertEqual((sh['row'], sh['labels']), ('any', ['self-hosted', 'ci-gpu']))
        self.assertIn('yielding', sh['why'])
        for name, rs in (('no tier', rows(host('h1', gpu('h1')), host('h2', gpu('h2')))),
                         ('the first tier', rows(host('h1', gpu('h1'), tier=2), host('h2', gpu('h2'), tier=1))),
                         ('equal tiers', rows(host('h1', gpu('h1'), tier=2), host('h2', gpu('h2'), tier=2))),
                         ('earlier one down', rows(host('h1', gpu('h1'), tier=1, problems=['drained']),
                                                   host('h2', gpu('h2'), tier=2))),
                         ('earlier one without the label', rows(host('h1', cpu('h1'), tier=1), host('h2', gpu('h2'), tier=2))),
                         ('rows unread', None)):
            with self.subTest(name):
                self.assertEqual(rt.retry(p, first, rs)['labels'], ['self-hosted', 'h2-gpu1'])
                self.assertEqual(rt.retry(p, {**first, 'outcome': 'reboot'}, rs)['labels'], ['self-hosted', 'h2-gpu'])
        # by role as well as by key; a bad tier is none
        self.assertTrue(rt.may_yield(rows(host('h1', gpu('h1'), tier=1), host('k2', gpu('h2'), tier=2, role='h2')),
                                     'h2', 'ci-gpu'))
        self.assertFalse(rt.may_yield(rows(host('h1', gpu('h1'), tier=1), host('h2', gpu('h2'), tier=True)), 'h2', 'ci-gpu'))
        self.assertFalse(rt.may_yield(rows(host('h1', gpu('h1'), tier=0), host('h2', gpu('h2'), tier=2)), 'h2', 'ci-gpu'))
        self.assertFalse(rt.may_yield(tiered, '', 'ci-gpu'))
        cl = dict(p, where='slurm', target='v100')
        self.assertEqual(rt.retry(cl, {**first, 'outcome': 'reboot'}, tiered)['why'], 'same target v100')


class Listening(unittest.TestCase):
    """19.2: a listener that holds an established TCP connection."""

    def proc(self, d, root):
        def listener(pid, name, sockets, ns='net:[1]', tcp='', tcp6=''):
            base = os.path.join(d, str(pid))
            os.makedirs(os.path.join(base, 'fd'))
            os.makedirs(os.path.join(base, 'net'))
            os.makedirs(os.path.join(base, 'ns'))
            with open(os.path.join(base, 'cmdline'), 'wb') as f:
                f.write(f'{root}/{name}/bin/Runner.Listener\0run\0'.encode())
            for i, s in enumerate(sockets):
                os.symlink(s, os.path.join(base, 'fd', str(i + 3)))
            os.symlink(ns, os.path.join(base, 'ns', 'net'))
            with open(os.path.join(base, 'net', 'tcp'), 'w') as f:
                f.write('  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n' + tcp)
            with open(os.path.join(base, 'net', 'tcp6'), 'w') as f:
                f.write('  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n' + tcp6)
        return listener

    def test_an_established_socket_of_the_listener(self):
        line = '   0: 0100007F:9C40 0100007F:0CEA {st} 00000000:00000000 00:00000000 00000000  1000        0 {ino} 1 x\n'
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(d, 'actions-runner')
            os.makedirs(root)
            proc = os.path.join(d, 'proc')
            os.makedirs(proc)
            add = self.proc(proc, root)
            tcp = line.format(st='01', ino='111') + line.format(st='0A', ino='222') + line.format(st='01', ino='333')
            add(10, 'r-up', ['socket:[111]', '/dev/null'], tcp=tcp)
            add(11, 'r-listen-only', ['socket:[222]'], tcp=tcp)  # a listening socket is no connection
            add(12, 'r-none', ['pipe:[333]'], tcp=tcp)  # not a socket
            add(13, 'r-other-ns', ['socket:[111]'], ns='net:[2]', tcp='')  # its own namespace has none
            six = ('   0: 0000000000000000FFFF00000100007F:9C40 0000000000000000FFFF00000100007F:0CEA 01 00000000:00000000 '
                   '00:00000000 00000000  1000        0 444 1 x\n')
            add(15, 'r-six', ['socket:[444]'], ns='net:[3]', tcp6=six)  # a dual-mode socket shows in tcp6
            add(16, 'r-theirs', ['socket:[111]'], tcp=tcp)  # another account's process naming the path
            real = os.stat

            def stat(path, *a, **kw):
                st = real(path, *a, **kw)
                return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, st.st_uid + 1, *st[5:10])) \
                    if str(path).endswith(os.sep + '16') else st
            with unittest.mock.patch('os.stat', stat):
                got = pool.runner_listening(root, proc)
            self.assertEqual(got, {'r-up', 'r-six'})
            os.makedirs(os.path.join(proc, '14', 'fd'))
            os.makedirs(os.path.join(proc, '14', 'ns'))
            os.symlink('net:[1]', os.path.join(proc, '14', 'ns', 'net'))
            os.symlink('socket:[111]', os.path.join(proc, '14', 'fd', '3'))
            with open(os.path.join(proc, '14', 'cmdline'), 'wb') as f:
                f.write(f'{root}/r-worker/bin/Runner.Worker\0'.encode())
            with unittest.mock.patch('os.stat', stat):
                self.assertEqual(pool.runner_listening(root, proc), {'r-up', 'r-six'})  # a worker is not a listener
            self.assertIsNone(pool.runner_listening(root, os.path.join(d, 'none')))
        self.assertIn('def runner_listening', pool._probe_src())

    def test_the_probe_and_the_pool_table(self):
        with unittest.mock.patch.object(pool, 'runner_procs', lambda r: ({'a', 'b', 'c'}, {'a'})), \
                unittest.mock.patch.object(pool, 'runner_listening', lambda r: {'a', 'b', 'z'}):
            p = pool.probe('/nonexistent/x.lock', 1, '', runners='/nonexistent', smi=False)
        self.assertEqual(p['runner_listening'], ['a', 'b'])
        with unittest.mock.patch.object(pool, 'runner_procs', lambda r: ({'a'}, set())), \
                unittest.mock.patch.object(pool, 'runner_listening', lambda r: None):
            self.assertNotIn('runner_listening', pool.probe('/nonexistent/x.lock', 1, '', runners='/nonexistent', smi=False))
        inv = inventory.load(inventory_text('hx'))
        p = {'runner_names': ['hx-part-1', 'hx-part-2', 'hx-gpu0'], 'runner_busy': ['hx-part-2'],
             'runner_listening': ['hx-part-1', 'hx-part-2', 'hx-gpu0']}
        got = {e['pool']: e for e in pool.pool_table(inv, p, {'hx-part-3', 'hx-part-1', 'hx-gpu3'})}
        self.assertEqual((got['part']['listening'], got['part']['yielded']), (1, 1))  # busy is not; online is not yielded
        self.assertEqual((got['cards']['listening'], got['cards']['yielded']), (1, 1))
        got = {e['pool']: e for e in pool.pool_table(inv, {**p, 'cards_leased': {'0': 1, '3': 0}})}
        self.assertEqual(got['cards']['listening'], 0)  # its card is leased, our own parts' too: it cannot start a job
        self.assertEqual(got['part']['listening'], 1)
        got = {e['pool']: e for e in pool.pool_table(inv, {k: v for k, v in p.items() if k != 'runner_listening'})}
        self.assertNotIn('listening', got['part'])  # not read: not proven
        self.assertNotIn('yielded', got['part'])
        self.assertNotIn('yielded', {e['pool']: e for e in pool.pool_table(inv, {}, set())}['part'])  # runners unknown
        m = pool.load(REGISTRY).machines['hosta']
        good = P('part', ['hosta-cpu', 'ci-cpu'], 1, listening=1, yielded=0)
        self.assertEqual(pool.host_row(m, {'pools': [good]})['pools'], [good])
        for bad in (dict(good, listening=-1), dict(good, yielded=True), dict(good, listening=1.0)):
            self.assertNotIn('pools', pool.host_row(m, {'pools': [bad]}), msg=repr(bad))

    def test_the_yielded_runners_from_presences_state(self):
        with tempfile.TemporaryDirectory() as d:
            inv = inventory.load(inventory_text('hx').replace('data = "/srv/ci/hx/data"', f'data = "{d}"'))
            data = str(inv.host['data'])
            self.assertEqual(data, d)
            self.assertEqual(pool.yielded_runners(inv), set())  # no state yet: nothing yielded
            presence.write_state(data, {'runners': {'hx-part-1': {'stopped': 1.0, 'yielded': True},
                                                    'hx-part-2': {'stopped': 1.0, 'yielded': False},
                                                    'hx-part-3': {'stopped': None, 'yielded': True}},
                                        'counts': {}, 'infra': {'since': 0.0, 'drained': False, 'seen': {}}})
            self.assertEqual(pool.yielded_runners(inv), {'hx-part-1'})
            with open(presence.state_path(data), 'w') as f:
                f.write('{')
            self.assertIsNone(pool.yielded_runners(inv))


EARLIER = 'r' * 12  # the repository entries' digest both hosts hold


def offered(labels=('part', 'h1', 'ci-cpu'), listening=1, age=5, tier=1, problems=(), rep=EARLIER, now=None):
    """An earlier tier's row as its forced probe gives it."""
    row = {'key': 'h1', 'tier': tier, 'read_at': (now or 0) - age, 'problems': list(problems), 'target': '',
           'pools': [{'pool': 'part', 'class': 'part', 'labels': list(labels), 'registered': 3, 'online': 3,
                      'free': listening, 'listening': listening}]}
    if rep is not None:
        row['policy'] = {'file': 'f' * 12, 'repositories': rep}
    return row


class TierHost(tp.Fixture):
    """test_presence's host with capability labels on its part pool, a tier of 2 and the
    earlier tier's row given by ``self.earlier`` (None: no row)."""
    part = '{ count = 2, labels = ["ci-cpu"] }'

    def setUp(self):
        super().setUp()
        self.earlier: dict | None = offered()
        self.age = 5  # s: how old the earlier tier's row is when this host's tick reads it
        self.me = 2

    def probe(self):
        p = super().probe()
        p._tiers = lambda: None if self.earlier is None else (
            self.me, [dict(self.earlier, read_at=self.now - self.age)], EARLIER)
        return p


class Yield(TierHost):
    """19.2: a later tier's idle runners yield, come back at once, and do not flap."""
    PARTS = ['ci-runner@hz-part-1.service', 'ci-runner@hz-part-2.service']

    def state(self):
        st, ok = presence.read_state(str(self.data))
        self.assertTrue(ok)
        return st

    def test_down_after_two_ticks_and_up_at_once(self):
        self.tick()
        self.assertEqual(self.actions('stop'), [])
        self.tick()
        self.assertEqual(sorted(self.actions('stop')), self.PARTS)  # only the part runners: the others have no ci- label
        st = self.state()['runners']
        self.assertTrue(st['hz-part-1']['yielded'])
        self.assertIn('tier: h1 (tier 1) has 1 idle runner(s) of ci-cpu',
                      [x['cause'] for x in self.log() if x.get('action') == 'stop'])
        self.assertEqual(pool.yielded_runners(self.inv), {'hz-part-1', 'hz-part-2'})
        self.tick()
        self.assertEqual(self.actions('start'), [])
        self.earlier = offered(listening=0)  # the earlier tier is full
        self.tick()
        self.assertEqual(sorted(self.actions('start')), self.PARTS)  # at the first tick
        self.assertEqual(pool.yielded_runners(self.inv), set())
        self.earlier = offered()  # idle again: the dwell holds them
        for _ in range(presence.DWELL_S // 20 - 1):
            self.tick()
        self.assertEqual(len(self.actions('stop')), 2)
        for _ in range(3):
            self.tick()
        self.assertEqual(len(self.actions('stop')), 4)

    def test_a_runner_stopped_for_another_cause_starts_after_two_ticks(self):
        st = {'runners': {'hz-part-1': {'stopped': self.now - 100, 'yielded': False, 'stop_ticks': 0, 'start_ticks': 0,
                                        'load': False}}, 'counts': {}, 'infra': {'since': 0.0, 'drained': False, 'seen': {}}}
        presence.write_state(str(self.data), st)
        self.units['ci-runner@hz-part-1.service'] = ('inactive', 'success')
        self.earlier = None
        self.tick()
        self.assertEqual(self.actions('start'), [])  # two ticks without a cause, as before
        self.tick()
        self.assertEqual(self.actions('start'), ['ci-runner@hz-part-1.service'])
        # held by another cause, then by the tier alone: up at the first tick once that clears
        st['runners']['hz-part-1'].update(stopped=self.now - 100, yielded=False)
        presence.write_state(str(self.data), st)
        self.units['ci-runner@hz-part-1.service'] = ('inactive', 'success')
        self.calls.clear()
        self.earlier = offered()
        self.tick()
        self.assertEqual(self.actions('start'), [])
        self.earlier = offered(listening=0)
        self.tick()
        self.assertEqual(self.actions('start'), ['ci-runner@hz-part-1.service'])

    def test_what_offers_nothing(self):
        cases = {
            'no tier here or no registry': None,
            'stale': 'stale',
            'a problem': offered(problems=['drained']),
            'other repository entries': offered(rep='x' * 12),
            'no policy': offered(rep=None),
            'an older release (no listening)': {**offered(), 'pools': [{k: v for k, v in offered()['pools'][0].items()
                                                                        if k != 'listening'}]},
            'none listening': offered(listening=0),
            'not all of its labels': offered(labels=('part', 'h1', 'ci-gpu')),
            'a pool without a capability label': offered(labels=('part', 'h1', 'h1-cpu')),
            'the same tier': offered(tier=2),
            'a later tier': offered(tier=3),
            'a cluster row': {**offered(), 'target': 'cpu'},
        }
        for name, row in cases.items():
            with self.subTest(name):
                self.calls.clear()
                self.age = presence.YIELD_FRESH_S + 30 if row == 'stale' else 5
                self.earlier = offered() if row == 'stale' else row
                for _ in range(3):
                    self.tick()
                self.assertEqual(self.actions('stop'), [])

    def test_a_yielded_runner_comes_back_when_the_row_goes_bad(self):
        self.tick()
        self.tick()
        self.assertEqual(len(self.actions('stop')), 2)
        self.age = 120  # stale: unproven
        self.tick()
        self.assertEqual(sorted(self.actions('start')), self.PARTS)

    def test_a_read_that_fails_yields_nothing_and_is_logged(self):
        def boom():
            raise OSError('ssh')
        p = self.probe()
        p._tiers = boom
        out: list[str] = []
        presence.tick(self.inv, p, dry_run=True, now=lambda: self.now, out=out.append)
        self.assertEqual(self.log(), [])  # a dry run writes nothing
        presence.tick(self.inv, p, now=lambda: self.now)
        self.now += 20
        presence.tick(self.inv, p, now=lambda: self.now)
        self.assertEqual(self.actions('stop'), [])
        self.assertTrue(any(x.get('tier') == 'not read' for x in self.log()))

    def test_the_freshness_is_judged_after_the_probes(self):
        def slow():  # the probes take 10 s; the row is read at their end, stamped by this host's clock
            self.now += 10
            return self.me, [dict(offered(), read_at=self.now)], EARLIER
        for _ in range(2):
            p = self.probe()
            p._tiers = slow
            presence.tick(self.inv, p, now=lambda: self.now)
            self.now += 20
        self.assertEqual(sorted(self.actions('stop')), Yield.PARTS)

    def test_a_yielded_runner_comes_back_without_proc(self):
        self.tick()
        self.tick()
        self.assertEqual(len(self.actions('stop')), 2)
        self.earlier = offered(listening=0)
        p = self.probe()
        p.busy = lambda rs: None  # the processes cannot be read
        presence.tick(self.inv, p, now=lambda: self.now)
        self.assertEqual(sorted(self.actions('start')), Yield.PARTS)

    def test_the_condition_ignores_it_and_the_dry_run_shows_it(self):
        out: list[str] = []
        self.assertEqual(presence.check(self.inv, self.probe(), 'hz-part-1', out.append), 0)
        _, lines, _ = self.tick(dry_run=True)
        self.assertIn('tier: h1 (tier 1)', '\n'.join(lines))
        self.assertEqual(self.actions('stop'), [])

    def test_offers_and_tier_yield_are_pure(self):
        rs = presence.runners(self.inv)
        t = 1000.0
        tiers = (2, [offered(now=t)], EARLIER)
        why = {r.name: [] for r in rs}
        got = presence.tier_yield(rs, why, tiers, {'runners': {}}, t)
        self.assertEqual(sorted(got), ['hz-part-1', 'hz-part-2'])
        why['hz-part-1'] = ['slot: x']
        self.assertEqual(sorted(presence.tier_yield(rs, why, tiers, {'runners': {}}, t)), ['hz-part-2'])
        st = {'runners': {'hz-part-2': {'started': t - 299}}}  # the 300 s dwell
        self.assertEqual(presence.tier_yield(rs, why, tiers, st, t), {})
        st = {'runners': {'hz-part-2': {'started': t - 300}}}
        self.assertEqual(sorted(presence.tier_yield(rs, why, tiers, st, t)), ['hz-part-2'])
        self.assertEqual(presence.offers((2, [offered(now=t, age=45)], EARLIER), t)[0][:2], (1, 'h1'))  # 45 s
        self.assertEqual(presence.offers((2, [offered(now=t, age=46)], EARLIER), t), [])
        light = (2, [offered(labels=('ci-light', 'ci-cpu'), now=t)], EARLIER)  # a light runner never yields
        self.assertEqual(sorted(presence.tier_yield(rs, {r.name: [] for r in rs}, light, {'runners': {}}, t)),
                         ['hz-part-1', 'hz-part-2'])
        big = presence.Runner('hz-big-1', 'big', 'part', ('ci-cpu', 'mem-big', 'hz'), (), None, frozenset())
        self.assertEqual(presence.tier_yield([big], {}, tiers, {'runners': {}}, t), {})  # it needs mem-big too
        both = (2, [offered(labels=('ci-cpu', 'mem-big'), now=t)], EARLIER)
        self.assertEqual(list(presence.tier_yield([big], {}, both, {'runners': {}}, t)), ['hz-big-1'])
        self.assertEqual(presence.offers((2, [offered(now=t, age=-6)], EARLIER), t), [])  # from the future
        self.assertEqual(presence.offers((2, [offered(now=t)], ''), t), [])  # this host's entries unknown
        self.assertEqual(presence.offers((2, [offered(now=t, rep='')], ''), t), [])  # unknown on both: not equal
        self.assertEqual(presence.offers((None, [offered(now=t)], EARLIER), t), [])
        two = (3, [dict(offered(now=t), key='h2', tier=2), offered(now=t)], EARLIER)
        self.assertEqual([o[1] for o in presence.offers(two, t)], ['h1', 'h2'])  # the earliest first
        self.assertEqual(presence.capability(rs[0]), frozenset({'ci-light'}))


class ProbeTiers(unittest.TestCase):
    def test_the_earlier_tiers_rows_read_now(self):
        reg = REGISTRY.replace('[machine.hostb]\naccess = "local"\n', '[machine.hostb]\naccess = "local"\ntier = 3\n')
        reg = reg.replace('numerics_ok = true\n', 'numerics_ok = true\ntier = 1\n', 1)
        reg += '\n[machine.hoste]\naccess = "ssh"\nssh = "alias-e"\ncpus = 8\nmemory_gib = 32\npython = "/p"\n' \
               'work = "/w"\nslots = { lock = "/tmp/y.lock", count = 1 }\ntier = 5\n'
        inv = inventory.load(inventory_text('hx'))
        with tempfile.TemporaryDirectory() as etc:
            path = os.path.join(etc, 'pool.toml')
            with open(path, 'w') as f:
                f.write(reg.replace('access = "local"\ntier = 3\n', 'access = "local"\n'))
            p = presence.Probe(inv, etc=etc)
            self.assertIsNone(p.tiers())  # this host has no tier
            with open(path, 'w') as f:
                f.write(reg)
            seen = []
            with unittest.mock.patch.object(pool, 'probe_host', lambda m, **kw: seen.append((m.key, kw)) or
                                            {'key': m.key, 'tier': m.tier}):
                me, got, rep = p.tiers()
            self.assertEqual((me, [r['key'] for r in got], rep), (3, ['hosta'], ''))
            self.assertEqual(seen, [('hosta', {'etc': etc, 'fast': True})])
            with open(path, 'w') as f:
                f.write(reg.replace('tier = 1\n', ''))
            self.assertIsNone(p.tiers())  # no earlier tier
        with tempfile.TemporaryDirectory() as etc:
            self.assertIsNone(presence.Probe(inv, etc=etc).tiers())  # no registry


class Burst(unittest.TestCase):
    """The design's claim, simulated: plans of one minute routed from one reading; GitHub
    hands each queued part to an idle online runner of its labels, the latest tier's first
    (the order least favourable to h100); each host's presence ticks every 20 s at its own
    phase with the real tick (``presence.tick``, its stubs from test_presence), reading the
    earlier tiers' rows as their last ticks published them; a started runner takes jobs
    ``START_S`` later. h100 (tier 1) takes the parts first, no part waits more than 60 s
    while a runner of its labels idles or is yielded anywhere, and nothing flaps."""
    START_S = 15
    PHASE = {'h100': 0, 'h2': 7, 'h3': 13}
    TIER = {'h100': 1, 'h2': 2, 'h3': 3}

    class Host(TierHost):
        def runTest(self):  # noqa: D102 - an instance of the fixture, not a test
            pass

    def hosts(self, published):
        out = {}
        for name in self.TIER:
            h = self.Host()
            h.setUp()
            self.addCleanup(h.tearDown)
            h.me = self.TIER[name]
            h.name = name
            out[name] = h
        for name, h in out.items():
            def tiers(h=h):
                h.now += 1.5  # the probes take their bound; the tick's clock moves on
                earlier = [published[k] for k in self.TIER if self.TIER[k] < h.me and k in published]
                return (h.me, earlier, EARLIER) if h.me > 1 else None
            h.tiers = tiers
        return out

    def run_burst(self, *, deaf=(), down=(), stale=False, held=(), bursts=((0, 7, 200), (450, 3, 100)), end=900):
        """Jobs (each with ``queued``, ``start``, ``host``), the longest wait while a runner
        of its labels idled or was yielded, and each runner's stop times. ``deaf``: hosts
        whose rows show no listening runner; ``down``: hosts whose rows have a problem;
        ``stale``: every published row too old; ``held``: hosts under a hold (19.8) for the
        whole run. A burst ``(t, n, seconds, host)`` names that host's own label."""
        published: dict[str, dict] = {}
        hs = self.hosts(published)
        machine = pool.load(REGISTRY).machines['hosta']
        names = ['hz-part-1', 'hz-part-2']  # each host's two part runners (ci-cpu)
        up_since = {(k, n): -10 ** 6 for k in hs for n in names}
        busy_until: dict[tuple[str, str], int] = {}
        queue: list[dict] = []
        jobs: list[dict] = []
        log = []
        idle_waits = 0
        worst = 0
        stops: dict[tuple[str, str], list[int]] = collections.defaultdict(list)
        t0 = -200
        for k in held:
            with unittest.mock.patch('builtins.print'):
                self.assertEqual(presence.hold(hs[k].inv, ['--minutes', '720'], env={}, now=lambda: T0 + t0), 0)
        for t in range(t0, end):
            for when, n, dur, *only in bursts:
                if t == when:
                    for i in range(n):
                        j = {'id': f'{when}-{i}{"-" + only[0] if only else ""}', 'queued': t, 'dur': dur,
                             'only': only[0] if only else None}
                        queue.append(j)
                        jobs.append(j)
            for (k, n), until in list(busy_until.items()):  # jobs that end
                if t >= until:
                    h = hs[k]
                    for pid, proc in list(h.procs.items()):
                        if f'/{n}/bin/Runner.Worker' in proc.get('cmdline', ''):
                            del h.procs[pid]
                    del busy_until[(k, n)]
            for k, h in hs.items():  # each host's tick at its phase
                if (t - self.PHASE[k]) % 20 == 0:
                    before = {n: h.units[f'ci-runner@{n}.service'][0] for n in names}
                    h.now = T0 + t
                    p = h.probe()
                    p._tiers = h.tiers
                    with unittest.mock.patch('builtins.print'):
                        presence.tick(h.inv, p, now=lambda h=h: h.now)
                    for n in names:
                        a = h.units[f'ci-runner@{n}.service'][0]
                        if before[n] != 'active' and a == 'active':
                            up_since[(k, n)] = t
                        if before[n] == 'active' and a != 'active':
                            stops[(k, n)].append(t)
                    # its row as its probe makes it (pool.pool_table, pool.host_row): online, busy, listening
                    online = [n for n in names if h.units[f'ci-runner@{n}.service'][0] == 'active']
                    probe = {'slots_free': 1, 'runner_names': online, 'runner_busy': [n for n in online if (k, n) in busy_until],
                             'runner_listening': [] if k in deaf else
                             [n for n in online if t - up_since[(k, n)] >= self.START_S],
                             'policy': {'file': 'f' * 12, 'repositories': EARLIER}, 'drained': k in down}
                    probe['pools'] = pool.pool_table(h.inv, probe, pool.yielded_runners(h.inv))
                    row = dict(pool.host_row(machine, probe), key=k, tier=self.TIER[k],
                               read_at=T0 + t - (600 if stale else 0))
                    published[k] = row
            # GitHub: each queued part to an idle online runner, the latest tier first
            ready = [(k, n) for k in sorted(hs, key=lambda k: -self.TIER[k]) for n in names
                     if hs[k].units[f'ci-runner@{n}.service'][0] == 'active' and t - up_since[(k, n)] >= self.START_S
                     and (k, n) not in busy_until]
            for j in list(queue):  # in order, each to the first ready runner its labels reach
                fit = [x for x in ready if j['only'] in (None, x[0])]
                if not fit:
                    continue
                k, n = fit[0]
                ready.remove((k, n))
                queue.remove(j)
                hs[k].job(n)
                busy_until[(k, n)] = t + j['dur']
                j.update(start=t, host=k)
                log.append((t, j['id'], k))
            # the wait the design bounds: a part queued while a runner of its labels idles or is yielded
            spare = any((k, n) not in busy_until and (hs[k].units[f'ci-runner@{n}.service'][0] == 'active'
                                                      or self.yielded(hs[k], n)) for k in hs for n in names)
            idle_waits = idle_waits + 1 if queue and spare else 0
            worst = max(worst, idle_waits)
        return jobs, worst, stops, hs

    @staticmethod
    def yielded(h, n):
        try:
            st, _ = presence.read_state(str(h.data))
        except Exception:  # noqa: BLE001
            return False
        v = st['runners'].get(n) or {}
        return bool(v.get('stopped') and v.get('yielded'))

    def test_the_burst_goes_to_h100_first_and_never_waits_long(self):
        snap = rows(host('h100', cpu('h100', 2), tier=1), host('h2', cpu('h2', 0, registered=2, yielded=2), tier=2),
                    host('h3', cpu('h3', 0, registered=2, yielded=2), tier=3), SLURMS, target(jobs=8))
        plans = [rt.route([part_(f'p{i}') for i in range(3)], snap) for _ in range(8)]  # one reading, eight plans
        for got in plans:
            self.assertTrue(all(set(g['labels']) == {'self-hosted', 'ci-cpu'} for g in got))
        self.assertEqual(sum(g['where'] == 'slurm' for g in plans[0]), 0)  # yielded runners are room
        jobs, worst, stops, hs = self.run_burst()
        first = sorted(jobs[:7], key=lambda j: j['start'])
        self.assertEqual([j['host'] for j in first[:2]], ['h100', 'h100'])  # h100 first, though GitHub favours the others
        self.assertTrue(all(j['start'] == 0 for j in first[:2]))
        self.assertLessEqual(max(j['start'] - j['queued'] for j in first[2:6]), 60)  # the overflow: one reaction
        self.assertEqual({j['host'] for j in first[2:6]}, {'h2', 'h3'})  # both later tiers at once
        self.assertLessEqual(worst, 60)
        second = [j for j in jobs if j['queued'] == 450]
        self.assertEqual([j['host'] for j in second[:2]], ['h100', 'h100'])  # at rest again: h100 first
        for times in stops.values():  # no flapping: a yielded runner stays down; one started stays the dwell
            self.assertTrue(all(b - a >= presence.DWELL_S for a, b in zip(times, times[1:])), stops)

    def test_a_held_later_tier_takes_its_host_label_parts_at_once(self):
        night = ((0, 2, 300, 'h3'), (0, 2, 300, 'h2'))  # parts on the 3090 hosts' own labels; h100 idles
        jobs, _, stops, _ = self.run_burst(held=('h2', 'h3'), bursts=night, end=400)
        self.assertEqual(sorted((j['host'], j['start']) for j in jobs), [('h2', 0), ('h2', 0), ('h3', 0), ('h3', 0)])
        self.assertFalse(any(stops.values()))  # held all along: nothing yields
        jobs, _, _, _ = self.run_burst(held=('h3',), bursts=night, end=400)
        self.assertEqual(sorted(j['host'] for j in jobs if 'start' in j), ['h3', 'h3'])  # h2, not held, yields
        self.assertFalse(any('start' in j for j in jobs if j['only'] == 'h2'))  # its parts wait while h100 idles
        jobs, _, _, _ = self.run_burst(held=('h3',), bursts=((0, 2, 100),), end=200)  # the cost: shared parts too
        self.assertIn('h3', {j['host'] for j in jobs})

    def test_a_black_hole_or_a_stale_row_yields_nothing(self):
        for kw in ({'stale': True}, {'down': ('h100', 'h2')}):
            jobs, worst, stops, _ = self.run_burst(bursts=((0, 6, 100),), end=300, **kw)
            self.assertEqual(sum(map(len, stops.values())), 0, kw)
            self.assertTrue(all(j['start'] == 0 for j in jobs))  # every runner online: none waits
        jobs, worst, stops, _ = self.run_burst(deaf=('h100',), bursts=((0, 6, 100),), end=300)
        self.assertFalse(any(stops[('h2', n)] for n in ('hz-part-1', 'hz-part-2')))  # h2 never yields to h100
        self.assertTrue(stops[('h3', 'hz-part-1')])  # h3 yields to h2, which listens
        self.assertLessEqual(worst, 60)
        self.assertEqual(sorted(j['host'] for j in jobs if j['start'] == 0), ['h100', 'h100', 'h2', 'h2'])


class HomeHold(TierHost):
    """19.8 with issue #232: a real inventory's home pool beside its ``ci-light`` pool; a job
    on its runner may hold the host, one on the light pool's may not."""
    def setUp(self):
        super().setUp()
        text = tp.INV.format(t=self.t, data=self.data, slot=self.slot, part=self.part, extra=self.extra)
        self.inv = inventory.load(text.replace(tp.LIGHT, f'{tp.LIGHT}, {tp.HOME}'))
        self.units['ci-runner@hz-home-1.service'] = ('active', 'success')
        (self.etc / 'policy.toml').write_text(POLICY)

    def test_a_job_on_the_home_runner_holds_its_host(self):
        job = {**Hold.JOB, 'RUNNER_NAME': 'hz-home-1'}
        self.assertEqual(presence.holder(self.inv, job, str(self.etc)), ('chaobrain/a#7-1', 'chaobrain/a', ''))
        self.assertIn('home runner', presence.holder(self.inv, {**job, 'RUNNER_NAME': 'hz-light-1'}, str(self.etc))[2])
        (self.etc / 'policy.toml').write_text(POLICY.replace('classes = ["compile", "nightly"]', 'classes = ["compile"]'))
        self.assertIn('lacks the class nightly', presence.holder(self.inv, job, str(self.etc))[2])
        (self.etc / 'policy.toml').write_text(POLICY)
        said: list[str] = []
        self.assertEqual(presence.hold(self.inv, ['--minutes', '5'], env=job, etc=str(self.etc), now=lambda: self.now,
                                       out=said.append), 0)
        self.assertEqual(list(presence.read_holds(str(self.data), self.now)), ['chaobrain/a#7-1'])
        for _ in range(3):  # held: the part runners do not yield, and the home runner never stops
            self.tick()
        self.assertEqual(self.actions('stop'), [])


class Hold(TierHost):
    """19.8: a time-bounded hold suspends the cause; the maintainer, or a job on the host's
    home runner whose repository's entry carries ``nightly``, sets it."""
    JOB = {'GITHUB_ACTIONS': 'true', 'GITHUB_REPOSITORY': 'chaobrain/a', 'GITHUB_REPOSITORY_ID': '1',
           'GITHUB_RUN_ID': '7', 'GITHUB_RUN_ATTEMPT': '1', 'RUNNER_NAME': 'hz-light-1'}

    def setUp(self):
        super().setUp()  # its light runner without a ci- label: the home runner
        self.inv = inventory.load(tp.INV.format(t=self.t, data=self.data, slot=self.slot, part=self.part,
                                                extra=self.extra).replace('["ci-light", "hz-light"]', '["hz-light"]'))
        (self.etc / 'policy.toml').write_text(POLICY)
        self.said: list[str] = []
        self.now = float(int(self.now))  # whole seconds: a hold's until is rounded to the ms

    def hold(self, *argv, env=None):
        return presence.hold(self.inv, list(argv), env={} if env is None else env, etc=str(self.etc),
                             now=lambda: self.now, out=self.said.append)

    def test_set_extend_release_and_expiry(self):
        data = str(self.data)
        self.assertEqual(self.hold(), 0)
        self.assertEqual(self.said[-1], 'presence: no hold on this host')
        self.assertEqual(self.hold('--minutes', '30'), 0)
        self.assertEqual(presence.hold_until(data, self.now), self.now + 1800)
        self.assertEqual(self.hold('--minutes', '720'), 0)  # extends; 720 at most
        self.assertEqual(presence.hold_until(data, self.now), self.now + 720 * 60)
        self.assertEqual(self.hold('--minutes', '30'), 0)  # a later hold is kept, not cut short
        self.assertEqual(presence.hold_until(data, self.now), self.now + 720 * 60)
        for bad in (('--minutes', '0'), ('--minutes', '721'), ('--minutes', 'x'), ('--until', '1'), ('--release', 'x')):
            with self.subTest(bad):
                self.assertEqual(self.hold(*bad), 2)
        self.assertEqual(self.hold('--minutes', '5', env=self.JOB), 0)
        self.assertEqual(sorted(presence.read_holds(data, self.now)), ['chaobrain/a#7-1', 'maintainer'])
        self.hold()
        self.assertTrue(any('held by chaobrain/a#7-1 until' in x for x in self.said))
        self.assertEqual(self.hold('--release'), 0)  # the maintainer's own: the job's stays
        self.assertEqual(list(presence.read_holds(data, self.now)), ['chaobrain/a#7-1'])
        self.assertEqual(presence.hold_until(data, self.now + 5 * 60), None)  # it ends by itself
        self.now += 5 * 60 + 1
        self.assertEqual(self.hold('--minutes', '1'), 0)  # a write drops the expired entry
        with open(presence.hold_path(data)) as f:
            self.assertEqual(list(json.load(f)['holders']), ['maintainer'])
        self.assertTrue(any(x.get('hold') == 'set' for x in self.log()))
        last = self.now + 60 * (presence.HOLD_MAX_MIN + 1)  # the latest until a marker may hold
        for junk in ('{', '[]', '{"holders": []}', '{"holders": {"m": {"until": "x"}}}',
                     '{"holders": {"m": {"until": Infinity}}}', '{"holders": {"m": {"until": NaN}}}',
                     '{"holders": {"m": {"until": %r}}}' % (last + 1), '{"holders": {"m": {"until": 1e13}}}'):
            with open(presence.hold_path(data), 'w') as f:
                f.write(junk)
            self.assertEqual(presence.read_holds(data, self.now), {}, junk)
            self.assertEqual(self.hold(), 0, junk)  # the listing does not break on it
        with open(presence.hold_path(data), 'w') as f:
            f.write('{"holders": {"m": {"until": %r}}}' % last)
        self.assertEqual(presence.hold_until(data, self.now), last)

    def test_who_may_hold(self):
        self.assertEqual(presence.holder(self.inv, self.JOB, str(self.etc)), ('chaobrain/a#7-1', 'chaobrain/a', ''))
        self.assertEqual(presence.holder(self.inv, {}, str(self.etc)), ('maintainer', 'maintainer', ''))
        no_class = POLICY.replace('classes = ["compile", "nightly"]', 'classes = ["compile"]')
        cases = {
            'a part runner': ({**self.JOB, 'RUNNER_NAME': 'hz-part-1'}, POLICY, 'home runner'),
            'no runner': ({**self.JOB, 'RUNNER_NAME': ''}, POLICY, 'home runner'),
            'no entry': ({**self.JOB, 'GITHUB_REPOSITORY': 'chaobrain/b'}, POLICY, 'no entry'),
            'paused': (self.JOB, POLICY.replace('bot_dispatch = true', 'bot_dispatch = true\npaused = true'), 'no entry'),
            'another id': ({**self.JOB, 'GITHUB_REPOSITORY_ID': '2'}, POLICY, 'repository id'),
            'no id': ({k: v for k, v in self.JOB.items() if k != 'GITHUB_REPOSITORY_ID'}, POLICY, 'repository id'),
            'a policy too large': (self.JOB, POLICY + '#' * pool.POLICY_MAX, 'does not load'),
            'no nightly class': (self.JOB, no_class, 'lacks the class nightly'),
            'no policy': (self.JOB, None, 'does not load'),
        }
        for name, (env, pol, why) in cases.items():
            with self.subTest(name):
                if pol is None:
                    (self.etc / 'policy.toml').unlink(missing_ok=True)
                else:
                    (self.etc / 'policy.toml').write_text(pol)
                self.assertIn(why, presence.holder(self.inv, env, str(self.etc))[2])
                self.assertEqual(self.hold('--minutes', '5', env=env), 1)
                self.assertIn('hold refused', self.said[-1])
        self.assertIsNone(presence.hold_until(str(self.data), self.now))

    def test_at_most_32_holders(self):
        for i in range(presence.HOLD_ENTRIES):
            self.assertEqual(self.hold('--minutes', '5', env={**self.JOB, 'GITHUB_RUN_ID': str(i)}), 0)
        self.assertEqual(self.hold('--minutes', '5', env={**self.JOB, 'GITHUB_RUN_ID': 'x'}), 1)
        self.assertEqual(self.hold('--minutes', '9', env={**self.JOB, 'GITHUB_RUN_ID': '0'}), 0)  # its own: extended
        self.assertEqual(presence.HOLD_ENTRIES, 32)

    def test_a_failed_write_or_inventory_is_one_line(self):
        with unittest.mock.patch.object(presence, '_write_holds', side_effect=PermissionError):
            self.assertEqual(self.hold('--minutes', '5'), 3)
        self.assertEqual(self.said[-1], 'presence: hold not written: PermissionError')
        self.assertIsNone(presence.hold_until(str(self.data), self.now))

        def unread():
            raise OSError('gone')
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            self.assertEqual(presence.main(['hold', '--minutes', '5'], unread), 3)
        self.assertEqual(buf.getvalue(), 'presence: hold: inventory unread: OSError\n')

    def test_a_hold_suspends_the_cause(self):
        self.tick()
        self.tick()
        self.assertEqual(sorted(self.actions('stop')), Yield.PARTS)
        self.hold('--minutes', '10')
        self.earlier = offered()  # an earlier tier still idles
        _, lines, _ = self.tick(dry_run=True)
        self.assertIn('held until', '\n'.join(lines))
        self.tick()
        self.assertEqual(sorted(self.actions('start')), Yield.PARTS)  # at the next tick
        for _ in range(10):
            self.tick()
        self.assertEqual(len(self.actions('stop')), 2)  # nothing yields while held
        self.assertNotIn('tier: ', json.dumps(self.log()[-3:]))
        self.now += 600
        for _ in range(presence.DWELL_S // 20 + 3):  # the hold ended: the dwell, then two ticks
            self.tick()
        self.assertEqual(len(self.actions('stop')), 4)

    def test_the_row_and_pool_status_show_it(self):
        self.assertIsNone(pool.holds_of(self.inv))
        self.hold('--minutes', '30')
        until, n = pool.holds_of(self.inv, self.now)
        self.assertEqual((until, n), (self.now + 1800, 1))
        m = pool.load(REGISTRY).machines['hosta']
        row = pool.host_row(m, {'slots_free': 1, 'hold_until': until, 'holds': n})
        self.assertEqual((row['hold_until'], row['holds']), (until, 1))
        self.assertIn('hold until ' + datetime.datetime.fromtimestamp(
            until, datetime.timezone.utc).strftime('%H:%MZ'), pool.table([row]))
        self.hold('--minutes', '5', env=self.JOB)  # two holders: the latest until
        self.assertEqual(pool.holds_of(self.inv, self.now), (self.now + 1800, 2))
        for bad in ({'hold_until': 'x', 'holds': 1}, {'hold_until': until, 'holds': 0}, {'hold_until': until}):
            self.assertNotIn('hold_until', pool.host_row(m, {'slots_free': 1, **bad}), bad)
        self.assertNotIn('hold until', pool.table([pool.host_row(m, {'slots_free': 1})]))
        with unittest.mock.patch.object(pool, '_inventory', return_value=self.inv), \
                unittest.mock.patch.object(pool, 'policy_digests', return_value=None), \
                unittest.mock.patch.object(pool, 'host_health', return_value=''):
            p = pool._with_inventory({'runner_names': [], 'runner_busy': []}, str(self.etc))
        self.assertEqual(p['holds'], 2)
        buf = io.StringIO()
        with unittest.mock.patch.dict(os.environ, {'GITHUB_ACTIONS': ''}), contextlib.redirect_stdout(buf):
            self.assertEqual(presence.main(['hold'], lambda: self.inv), 0)
        self.assertIn('held by maintainer', buf.getvalue())


POLICY = textwrap.dedent('''\
    schema = 2
    actors = ["alice"]
    [repositories."chaobrain/a"]
    id = 1
    visibility = "private"
    bot_dispatch = true
    classes = ["compile", "nightly"]
    ''')


class PolicyDigest(unittest.TestCase):
    def test_the_repository_entries_digest(self):
        d = policy.repositories_digest(policy.load(POLICY))
        same = POLICY.replace('actors = ["alice"]', '# a comment\nactors = ["bob", "carol"]').replace(
            'classes = ["compile", "nightly"]', 'classes = ["nightly", "compile"]')
        self.assertEqual(policy.repositories_digest(policy.load(same)), d)
        for why, text in {'no entry': 'schema = 2\n', 'no bot': POLICY.replace('bot_dispatch = true\n', ''),
                          'a class less': POLICY.replace('"compile", ', ''), 'another id': POLICY.replace('id = 1', 'id = 2'),
                          'paused': POLICY + 'paused = true\n',
                          'an event more': POLICY + 'extra_events = ["merge_group"]\n'}.items():
            self.assertNotEqual(policy.repositories_digest(policy.load(text)), d, why)

    def test_the_probe_reads_the_accounts_policy(self):
        with tempfile.TemporaryDirectory() as etc:
            self.assertIsNone(pool.policy_digests(etc))  # none: a maintainer's account
            with open(os.path.join(etc, 'policy.toml'), 'w') as f:
                f.write(POLICY)
            got = pool.policy_digests(etc)
            pol = policy.load(POLICY)
            self.assertEqual(got, {'file': pol.digest[:12], 'repositories': policy.repositories_digest(pol)[:12]})
            with open(os.path.join(etc, 'policy.toml'), 'w') as f:
                f.write('schema = 3\n')
            self.assertEqual(pool.policy_digests(etc), {'error': 'does not load'})
            with open(os.path.join(etc, 'policy.toml'), 'w') as f:
                f.write(POLICY + '#' * pool.POLICY_MAX)
            self.assertEqual(pool.policy_digests(etc), {'error': 'does not load'})


class Drift(unittest.TestCase):
    def test_the_norm_is_what_most_hosts_hold(self):
        broken = [dict(host(k), policy={'error': 'does not load'}) for k in ('e', 'f', 'g')]
        rs = [host('a', rep='x'), host('b', rep='x'), host('c', rep='y'), host('d'), target(), *broken]
        self.assertEqual(capacity.policy_drift(rs), {'c': 2})  # a policy that does not load is a problem, not a digest
        self.assertEqual(capacity.policy_drift(rs[:2]), {})
        self.assertEqual(capacity.policy_drift([host('a', rep='x'), host('b', rep='y')]), {'a': 1, 'b': 1})  # a tie
        self.assertEqual(capacity.policy_drift([host('a', rep='x'), host('b', rep='x'), host('c', rep='y'),
                                                host('e', rep='z')]), {'c': 3, 'e': 3})
        odd = dict(target(), policy={'repositories': 'q'})
        self.assertEqual(capacity.policy_drift([*rs[:2], odd]), {})  # a cluster row is no host

    def test_a_warning_not_a_problem_and_judged_afresh(self):
        rs = [host('a', cpu('a'), rep='x', tier=1), host('b', cpu('b'), rep='x'), host('c', cpu('c'), rep='y')]
        got = rows(*rs)
        c = got[2]
        self.assertFalse(c.down)
        self.assertEqual((c.row['policy_drift'], c.row['warnings']), (True, ['policy: repository entries differ from 2 other host(s)']))
        self.assertNotIn('policy_drift', got[0].row)
        fixed = dict(got[2].row, policy={'file': 'f' * 12, 'repositories': 'x'})  # c's entries brought along
        again = capacity.flag_policy([dict(r.row) for r in got[:2]] + [fixed])
        self.assertTrue(all('policy_drift' not in r and 'warnings' not in r for r in again))
        flagged = capacity.flag_policy([dict(r.row) for r in got])
        self.assertEqual(flagged[2]['warnings'], ['policy: repository entries differ from 2 other host(s)'])  # once
        shown = pool.table([dict(pool.host_row(pool.load(REGISTRY).machines['hosta'], {'slots_free': 1, 'drained': True}),
                                 warnings=['policy: x'])])
        self.assertIn('drained; warn: policy: x', shown)


class PoolStatus(unittest.TestCase):
    def test_pool_status_shows_the_drift(self):
        with tempfile.TemporaryDirectory() as etc:
            path = os.path.join(etc, 'pool.toml')
            with open(path, 'w') as f:
                f.write(REGISTRY)
            os.chmod(path, 0o600)
            got = [dict(host('hosta', rep='x'), read_at=T0), dict(host('hostb', rep='y'), read_at=T0),
                   dict(host('hostc', rep='x'), read_at=T0)]
            for r in got:
                r.update(queue=0, gpu_free={}, cpus_free=0, mem_free_gib=0, gpu_model='', billed=False, price=0.0,
                         start_now=True, numerics_ok=True, slots=1)
            out = io.StringIO()
            with unittest.mock.patch.object(pool, 'fast_status', return_value=got), \
                    unittest.mock.patch('sys.stdout', out):
                self.assertEqual(pool.main(['status', '--fast', '--json'], etc), 0)
            shown = {r['key']: r.get('policy_drift', False) for r in json.loads(out.getvalue())}
            self.assertEqual(shown, {'hosta': False, 'hostb': True, 'hostc': False})


class Action(unittest.TestCase):
    def test_the_part_actions_outputs_are_the_executors(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, 'actions', 'part', 'action.yml'), encoding='utf-8') as f:
            text = f.read()
        block = text.split('\noutputs:\n', 1)[1].split('\nruns:', 1)[0] + '\n'
        names = re.findall(r'^  ([a-z-]+):\n', block, re.M)
        self.assertEqual(set(names), set(part.OUTPUTS))
        for n in names:
            self.assertIn(f'    value: ${{{{ steps.part.outputs.{n} }}}}\n', block)


class Rows(unittest.TestCase):
    def test_the_registrys_tier(self):
        text = REGISTRY.replace('numerics_ok = true\n', 'numerics_ok = true\ntier = 2\n', 1)
        r = pool.load(text)
        self.assertEqual((r.machines['hosta'].tier, r.machines['hostb'].tier), (2, None))
        self.assertEqual(pool.host_row(r.machines['hosta'], None)['tier'], 2)
        self.assertNotIn('tier', pool.host_row(r.machines['hostb'], None))
        self.assertEqual(pool.load(text.replace('tier = 2', 'tier = 99')).machines['hosta'].tier, 99)
        self.assertEqual(pool.load(text.replace('tier = 2', 'tier = 1')).machines['hosta'].tier, 1)
        for bad in ('tier = 0', 'tier = 100', 'tier = true', 'tier = "1"', 'tier = 1.5'):
            with self.subTest(bad), self.assertRaisesRegex(pool.RegistryError, 'tier'):
                pool.load(text.replace('tier = 2', bad))
        with self.assertRaisesRegex(pool.RegistryError, 'tier'):
            pool.load(REGISTRY.replace('access = "slurm"\n', 'access = "slurm"\ntier = 1\n'))

    def test_a_host_row_takes_role_pools_policy_and_health(self):
        m = pool.load(REGISTRY).machines['hosta']
        pools = [P('part', ['hosta-cpu', 'ci-cpu'], 1, memory_gib=48.0, speed=1.5, workers=6)]
        p = {'slots_free': 1, 'role': 'h1', 'pools': pools, 'policy': {'file': 'a' * 12, 'repositories': 'b' * 12, 'x': 1}}
        row = pool.host_row(m, p)
        self.assertEqual((row['role'], row['pools'], row['policy'], row['problems']),
                         ('h1', pools, {'file': 'a' * 12, 'repositories': 'b' * 12}, []))
        row = pool.host_row(m, {**p, 'health': 'health: 3 GiB free under the data directory, below 50'})
        self.assertEqual(row['problems'], ['health: 3 GiB free under the data directory, below 50'])
        self.assertEqual(pool.host_row(m, {**p, 'health': 'odd'})['problems'], ['health: odd'])
        row = pool.host_row(m, {**p, 'policy': {'error': 'does not load'}})
        self.assertEqual(row['problems'], ['policy: does not load'])
        for bad in ([{'pool': 'x'}], [dict(pools[0], free=5)], [dict(pools[0], speed=float('inf'))],
                    [dict(pools[0], labels='ci-cpu')], [dict(pools[0], memory_gib=0)], 'x'):
            self.assertNotIn('pools', pool.host_row(m, {**p, 'pools': bad}), msg=repr(bad))
        self.assertNotIn('role', pool.host_row(m, {**p, 'role': ''}))

    def test_the_pool_table_from_the_inventory(self):
        inv = inventory.load(inventory_text('hx').replace(
            'part = { count = 3, labels = ["hx-cpu"] }',
            'part = { count = 3, labels = ["hx-cpu", "ci-cpu"], slot = { cpus = ["0-3", "4-7", "8-11"], workers = 4, '
            'memory = "48G", speed = 1.5 } }'))
        p = {'runner_names': ['hx-part-1', 'hx-part-2', 'hx-gpu0', 'hx-gpu3'], 'runner_busy': ['hx-part-2', 'hx-gpu3']}
        got = {e['pool']: e for e in pool.pool_table(inv, p)}
        self.assertEqual(got['part'], {'pool': 'part', 'class': 'part', 'labels': ['hx-cpu', 'ci-cpu'], 'registered': 3,
                                       'online': 2, 'free': 1, 'memory_gib': 48.0, 'speed': 1.5, 'workers': 4})
        self.assertEqual(got['cards'], {'pool': 'cards', 'class': 'gpu', 'labels': ['hx-gpu', 'hx-gpu0'], 'registered': 2,
                                        'online': 2, 'free': 1})  # gpu0 is the pool's own label too; gpu3 is not
        self.assertEqual((got['light']['labels'], got['light']['free']), (['light', 'hx'], 0))
        inv = inventory.load(inventory_text('hx').replace('labels = ["hx-gpu", "hx-gpu0"]', 'labels = ["hx-gpu"]'))
        self.assertEqual({e['pool']: e['labels'] for e in pool.pool_table(inv, p)}['cards'], ['hx-gpu'])  # not a card's
        self.assertEqual({e['pool']: (e['online'], e['free']) for e in pool.pool_table(inv, {})}['part'], (None, None))

    def test_the_home_pool_in_the_row(self):
        """Issue #232: the row's ``pools`` show the home pool with its labels, by its key (it
        shares its class with the light pool); ``ci-light``'s free runners are the light
        pool's alone; the consumers' rule (consumers §5.1) finds the home label; and a reader
        of the row's older release takes the entry as any other."""
        inv = inventory.load(inventory_text('hx').replace('light = 2', 'light = { count = 2, labels = ["ci-light", '
                                                          '"hx-light"] }\nhome = { count = 1, class = "light", '
                                                          'labels = ["hx-home", "hx-light"] }'))
        p = {'runner_names': ['hx-light-1', 'hx-light-2', 'hx-home-1'], 'runner_busy': ['hx-light-1']}
        with tempfile.TemporaryDirectory() as etc, unittest.mock.patch.object(pool, '_inventory', return_value=inv):
            p = pool._with_inventory(p, etc)
        got = {e['pool']: e for e in p['pools']}
        self.assertEqual(got['home'], {'pool': 'home', 'class': 'light', 'labels': ['hx-home', 'hx-light'],
                                       'registered': 1, 'online': 1, 'free': 1, 'yielded': 0})
        self.assertEqual({k: got['light'][k] for k in ('labels', 'registered', 'online', 'free')},
                         {'labels': ['ci-light', 'hx-light'], 'registered': 2, 'online': 2, 'free': 1})
        self.assertEqual((p['label_runners']['ci-light']['free'], p['label_runners']['hx-light']['free']), (1, 2))

        def home_label(pools):  # the consumers' rule, word for word
            mine = [q for q in pools if q['class'] == 'light' and not any(x.startswith('ci-') for x in q['labels'])]
            own = [x for q in mine for x in q['labels']
                   if not any(x in o['labels'] for o in pools if o is not q)]
            return own[0] if len(mine) == 1 and len(own) == 1 else None
        self.assertEqual(home_label(p['pools']), 'hx-home')
        self.assertIsNone(home_label(pool.pool_table(inventory.load(inventory_text('hx')), {})))  # no home pool
        self.assertTrue(all(pool._pool_entry(e) for e in p['pools']))  # an older reader's check: no new key
        row = {'key': 'hx', 'read_at': 1000.0, 'problems': [], 'label_runners': p['label_runners'], 'pools': p['pools']}
        facts = rt._Facts(capacity.rows_from([row], now=1001.0))
        self.assertEqual(facts.free(['ci-light']), 1)  # the light pool's free runner, never the home one
        self.assertEqual(facts.free(['hx-home']), 1)

    def test_the_ci_accounts_probe_carries_them(self):
        with tempfile.TemporaryDirectory() as etc:
            p = pool._with_inventory({}, etc)
            self.assertNotIn('role', p)  # no inventory: none of them
            with open(os.path.join(etc, 'inventory.toml'), 'w') as f:
                f.write(inventory_text('hx'))
            with open(os.path.join(etc, 'policy.toml'), 'w') as f:
                f.write(POLICY)
            p = pool._with_inventory({'runner_names': [], 'runner_busy': []}, etc)
            self.assertEqual(p['role'], 'hx')
            self.assertEqual([e['pool'] for e in p['pools']], ['light', 'part', 'cards', 'slurm'])
            self.assertEqual(p['policy']['repositories'], policy.repositories_digest(policy.load(POLICY))[:12])
            self.assertNotIn('health', p)  # its data directory is not there: nothing proven
            with unittest.mock.patch.object(pool, 'host_health', return_value='health: low disk'):
                self.assertEqual(pool._with_inventory({}, etc)['health'], 'health: low disk')

    def test_a_target_row_has_its_bounds_and_an_opaque_partition(self):
        r = pool.load(REGISTRY)
        m = r.machines['cl']
        rows_ = {t: pool.target_row(m, m.targets[t], [], {}, {}) for t in m.targets}
        self.assertEqual((rows_['cpu']['memory_gib'], rows_['cpu']['max_minutes']), (1900, 90))
        keys = {t: x['partition'] for t, x in rows_.items()}
        self.assertEqual(len(set(keys.values())), 3)
        self.assertNotIn('part', ''.join(keys.values()))
        self.assertRegex(keys['cpu'], r'\A[0-9a-f]{12}\Z')
        self.assertEqual(pool.partition_key('cl', 'PART_C'), pool.partition_key('cl', 'part_c'))
        self.assertNotEqual(pool.partition_key('cl', 'part_c'), pool.partition_key('c2', 'part_c'))
        shared = pool.load(REGISTRY.replace('partition = "part_a"', 'partition = "part_v"')).machines['cl']
        self.assertEqual(pool.target_row(shared, shared.targets['a40'], [], {}, {})['partition'], keys['v100'])


class PartOutputs(unittest.TestCase):
    REC = {'class': 'gpu', 'host': 'h1', 'attempt': 1, 'retries': 1, 'pool_labels': ['h1-gpu', 'ci-gpu'], 'cards': [2]}

    def test_pool_labels_and_cool_card(self):
        with tempfile.TemporaryDirectory() as d:
            ok = part.finish(d, self.REC, {'rc': 0, 'end_epoch': 1.0}, cool_card=3)
            self.assertEqual((ok['pool-labels'], ok['cool-card']), (['h1-gpu', 'ci-gpu'], ''))  # no thermal retry
            hot = part.finish(d, dict(self.REC, early_stop=True), {'rc': 1, 'end_epoch': 1.0}, cool_card=3)
            self.assertEqual((hot['outcome'], hot['retry-on'], hot['cool-card']),
                             ('thermal', ['self-hosted', 'h1-gpu3'], '3'))
            self.assertEqual(part.finish(d, {**self.REC, 'pool_labels': None}, {'rc': 0})['pool-labels'], [])
        self.assertIn('pool-labels', part.OUTPUTS)
        self.assertIn('cool-card', part.OUTPUTS)
        out = os.path.join(tempfile.mkdtemp(), 'o')
        self.addCleanup(lambda: os.path.exists(out) and os.unlink(out))
        part.write_outputs({'pool-labels': ['a', 'b'], 'cool-card': '3'}, out)
        with open(out, encoding='utf-8') as f:
            lines = f.read().splitlines()
        self.assertIn('pool-labels=["a", "b"]', lines)
        self.assertIn('cool-card=3', lines)


class SlotSpeed(unittest.TestCase):
    def test_a_slot_speed_is_finite_and_positive(self):
        base = inventory_text('hx').replace('part = { count = 3, labels = ["hx-cpu"] }',
                                            'part = { count = 1, labels = ["hx-cpu"], slot = { cpus = ["0-3"], '
                                            'workers = 4, memory = "8G", speed = SPEED } }')
        self.assertEqual(inventory.load(base.replace('SPEED', '1.25')).host['pools']['part']['slot']['speed'], 1.25)
        for bad in ('inf', '+inf', 'nan', '0', '-1.0'):
            with self.subTest(bad), self.assertRaisesRegex(inventory.InventoryError, 'pools'):
                inventory.load(base.replace('SPEED', bad))


if __name__ == '__main__':
    unittest.main()
