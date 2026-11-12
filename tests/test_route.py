"""The plan-time route and the retry table (decision 16.5; RUN-10 R5), on capacity rows
read through ``capacity.rows_from`` as the plan job reads them."""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest

from ci_runners import capacity, cli
from ci_runners import route as rt

T0 = 1_762_905_600.0


def R(free=0, registered=2, online=None):
    return {'registered': registered, 'online': free if online is None else online, 'free': free}


def host(key='hosta', problems=(), age=10, pools=None, **labels):
    """A host row: ``labels`` maps a runner label (``ci_cpu`` for ``ci-cpu``) to its counts."""
    row = {'key': key, 'access': 'ssh', 'target': '', 'read_at': T0 - age, 'problems': list(problems),
           'slots_free': 1, 'labels': {}, 'label_runners': {k.replace('_', '-'): v for k, v in labels.items()}}
    if pools is not None:
        row['pools'] = pools
    return row


def target(name='cpu', jobs=1, gpu='', price=0.1, age=10, problems=(), **kw):
    return {'key': f'cl:{name}', 'access': 'slurm', 'target': name, 'read_at': T0 - age, 'problems': list(problems),
            'start_now': jobs > 0, 'slots_free': 1 if jobs else 0, 'jobs_free': jobs, 'gpu_model': gpu, 'price': price,
            'numerics_ok': True, 'labels': {}, 'multi_gpu': False, **kw}


def rows(*rs):
    return capacity.rows_from(list(rs), now=T0)


def part(pid='p', needs='cpu', minutes=10, **kw):
    return {'id': pid, 'command': 'python -m pytest', 'needs': needs, 'minutes': minutes, **kw}


SLURMS = host('hostb', ci_slurm=R(4))  # a host that drives cluster parts


def one(p, *rs):
    return rt.route([p], rows(*rs))[0]


class Labels(unittest.TestCase):
    def test_each_need_its_capability_label_and_never_a_host_label(self):
        cap = rows(host(ci_cpu=R(1), ci_gpu=R(1), ci_gpu2=R(1), ci_timing=R(1), ci_light=R(1), hosta_cpu=R(1),
                        hosta_gpu0=R(1)), SLURMS, target())
        got = rt.route([part(n, n) for n in rt.NEEDS], cap)
        self.assertEqual([g['labels'] for g in got], [['self-hosted', f'ci-{n}'] for n in rt.NEEDS])
        self.assertEqual({g['where'] for g in got}, {'host'})
        for g in got:
            self.assertLessEqual(set(g['labels']), rt.SHARED)
        self.assertEqual(rt.NEEDS, {'cpu': 'ci-cpu', 'gpu': 'ci-gpu', 'gpu2': 'ci-gpu2', 'timing': 'ci-timing',
                                    'light': 'ci-light'})

    def test_the_part_keeps_its_own_keys(self):
        got = one(part(results='out', memory_gib=4), host(ci_cpu=R(1)))
        self.assertEqual((got['id'], got['command'], got['results'], got['memory_gib'], got['target']),
                         ('p', 'python -m pytest', 'out', 4, ''))
        self.assertIs(one(part(history=True), host(ci_cpu=R(1)))['history'], True)  # #228, for part.yml's checkout
        self.assertNotIn('history', one(part(), host(ci_cpu=R(1))))  # absent: one commit, as before
        self.assertEqual(rt.load_plan({'schema': 1, 'parts': [part(history=False)]})[0]['history'], False)

    def test_the_timeout_is_minutes_by_the_largest_speed_plus_15(self):
        pools = [{'labels': ['ci-cpu', 'hosta-cpu'], 'memory_gib': 48, 'speed': 1.5}]
        slow = [{'labels': ['ci-cpu'], 'memory_gib': 48, 'speed': 2.25}]
        self.assertEqual(one(part(minutes=10), host(ci_cpu=R(1), pools=pools))['timeout_minutes'], 30)
        self.assertEqual(one(part(minutes=10), host(ci_cpu=R(1), pools=pools),
                             host('hostc', problems=['drained'], ci_cpu=R(0), pools=slow))['timeout_minutes'], 38)
        self.assertEqual(one(part(minutes=10), host(ci_cpu=R(1)))['timeout_minutes'], 25)  # unreported: 1.0
        self.assertEqual(one(part(minutes=7.1), host(ci_cpu=R(1)))['timeout_minutes'], 23)  # whole minutes, up
        gpu = [{'labels': ['ci-gpu'], 'memory_gib': 48, 'speed': 3.0}]
        self.assertEqual(one(part(minutes=10), host(ci_cpu=R(1), ci_gpu=R(1), pools=pools + gpu))['timeout_minutes'], 30)


class Overflow(unittest.TestCase):
    def test_only_at_zero_free_and_a_free_job(self):
        self.assertEqual(one(part(), host(ci_cpu=R(1)), SLURMS, target())['where'], 'host')
        got = one(part(), host(ci_cpu=R(0)), SLURMS, target())
        self.assertEqual((got['labels'], got['where'], got['target']), (['self-hosted', 'ci-slurm'], 'slurm', 'cpu'))
        self.assertIn('cl:cpu can start it now', got['why'])
        got = one(part(), host(ci_cpu=R(0)), SLURMS, target(jobs=0))
        self.assertEqual((got['labels'], got['where']), (['self-hosted', 'ci-cpu'], 'host'))
        self.assertIn('no cluster target can start it now', got['why'])

    def test_the_plans_parts_book_free_runners_and_jobs(self):
        got = rt.route([part(f'p{i}') for i in range(4)], rows(host(ci_cpu=R(1)), SLURMS, target(jobs=2)))
        self.assertEqual([g['where'] for g in got], ['host', 'slurm', 'slurm', 'host'])
        two = rows(host(ci_cpu=R(0)), SLURMS, target('a', jobs=1, partition='x'), target('b', jobs=1, partition='x'))
        self.assertEqual([g['where'] for g in rt.route([part('a'), part('b')], two)], ['slurm', 'host'])  # one room
        apart = rows(host(ci_cpu=R(0)), SLURMS, target('a', jobs=1), target('b', jobs=1, price=0.2))
        self.assertEqual([g['target'] for g in rt.route([part('a'), part('b')], apart)], ['a', 'b'])
        got = rt.route([part('a'), part('b')], rows(host(ci_cpu=R(0)), host('hostb', ci_slurm=R(1)), target(jobs=5)))
        self.assertEqual([g['where'] for g in got], ['slurm', 'host'])  # one free ci-slurm runner
        self.assertEqual(rt.route([part()], rows(host(ci_cpu=R(0)), host('hostb', ci_slurm=R(1)), target(jobs=5)))[0]['where'],
                         'slurm')  # pure: a second call books afresh

    def test_timing_never_overflows(self):
        got = one(part(needs='timing'), host(ci_timing=R(0)), SLURMS, target(), target('g', gpu='m'))
        self.assertEqual((got['labels'], got['where']), (['self-hosted', 'ci-timing'], 'host'))
        self.assertIn('timing never overflows', got['why'])
        got = one(part(needs='light'), host(ci_light=R(0)), SLURMS, target())
        self.assertEqual((got['labels'], got['where']), (['self-hosted', 'ci-light'], 'host'))
        self.assertIn('light never overflows', got['why'])

    def test_unknown_free_never_overflows(self):
        hidden = {'registered': 2, 'online': None, 'free': None}
        self.assertEqual(one(part(), host(ci_cpu=hidden), SLURMS, target())['where'], 'host')
        self.assertEqual(one(part(), host(ci_cpu=R(0)), host('hostc'), SLURMS, target())['where'], 'host')  # might serve

    def test_no_host_serving_ci_slurm_no_overflow(self):
        got = one(part(), host(ci_cpu=R(0)), host('hostb', problems=['drained'], ci_slurm=R(4)), target())
        self.assertEqual(got['where'], 'host')
        self.assertIn('no host serves ci-slurm', got['why'])

    def test_the_target_fits_the_need(self):
        cpu, gpu, multi = target('c'), target('g', gpu='m', price=0.01), target('m', gpu='m', multi_gpu=True, price=0.5)
        self.assertEqual(one(part(), host(ci_cpu=R(0)), SLURMS, gpu, cpu)['target'], 'c')
        self.assertEqual(one(part(needs='gpu'), host(ci_gpu=R(0)), SLURMS, cpu, gpu, multi)['target'], 'g')  # cheapest
        self.assertEqual(one(part(needs='gpu2'), host(ci_gpu2=R(0)), SLURMS, cpu, gpu, multi)['where'], 'host')  # 1 job
        self.assertEqual(one(part(needs='gpu2'), host(ci_gpu2=R(0)), SLURMS, cpu, gpu, dict(multi, jobs_free=2))['target'],
                         'm')
        self.assertEqual(one(part(needs='gpu'), host(ci_gpu=R(0)), SLURMS, target('a', gpu='m', price=0.9),
                             target('z', gpu='m', price=0.1))['target'], 'z')  # by price, then key
        self.assertEqual(one(part(), host(ci_cpu=R(0)), SLURMS, target(numerics_ok=False))['where'], 'host')
        self.assertEqual(one(part(), host(ci_cpu=R(0)), SLURMS, target(problems=['cluster unreachable']))['where'], 'host')
        self.assertEqual(one(part(minutes=100), host(ci_cpu=R(0)), SLURMS, target(max_minutes=60))['where'], 'host')
        self.assertEqual(one(part(memory_gib=64), host(ci_cpu=R(0)), SLURMS, target(memory_gib=32))['where'], 'host')
        got = one(part(minutes=10), host(ci_cpu=R(0)), SLURMS, target(speed=2.0, max_minutes=60, memory_gib=64))
        self.assertEqual((got['where'], got['timeout_minutes']), ('slurm', 35))

    def test_stale_rows(self):
        with self.assertRaisesRegex(rt.RouteError, 'ci-cpu: part p: no host serves'):
            one(part(), host(ci_cpu=R(1), age=121), host('hostb', ci_gpu=R(1)))
        got = one(part(needs='gpu2'), host(ci_cpu=R(1), age=121), host('hostb', age=200, ci_slurm=R(1)))
        self.assertEqual(got['labels'], ['self-hosted', 'ci-gpu2'])  # every host row stale: the file, not the hosts
        self.assertIn('capacity unreadable (every host row is stale)', got['why'])
        got = one(part(), host(ci_cpu=R(0)), SLURMS, target(age=capacity.CLUSTER_STALE_S + 1))
        self.assertEqual(got['where'], 'host')  # a stale cluster row cannot start it now
        self.assertEqual(one(part(), host(ci_cpu=R(0)), host('hostb', age=121, ci_slurm=R(4)), target())['where'], 'host')


class PoolRows(unittest.TestCase):
    """Rows as ``pool`` writes them (``host_row``, ``target_row``), read by ``capacity``."""

    def test_the_route_reads_pools_own_rows(self):
        from ci_runners import pool
        from tests.test_pool import SINFO, reg
        r = reg()
        cl = r.machines['cl']
        lr = {'ci-cpu': {'registered': 2, 'online': 1, 'free': 0}, 'ci-slurm': {'registered': 8, 'online': 8, 'free': 8},
              'hosta-cpu': {'registered': 2, 'online': 1, 'free': 0}}
        h = dict(pool.host_row(r.machines['hosta'], {'slots_free': 0, 'load': 0, 'cpus': 16, 'label_runners': lr}),
                 read_at=T0)
        self.assertEqual(h['problems'], [])
        ts = [dict(pool.target_row(cl, cl.targets[t], pool.parse_sinfo(SINFO), {}, {}), read_at=T0) for t in cl.targets]
        self.assertEqual(one(part(), h, *ts)['where'], 'host')  # the fixture's targets have not passed numerics
        ts = [dict(t, numerics_ok=True) for t in ts]
        got = rt.route([part('a'), part('b'), part('c')], rows(h, *ts))
        self.assertEqual([(g['where'], g['target']) for g in got], [('slurm', 'cpu'), ('slurm', 'cpu'), ('host', '')])
        self.assertLessEqual({x for g in got for x in g['labels']}, rt.SHARED)


class MemBig(unittest.TestCase):
    POOLS = [{'labels': ['ci-cpu', 'hosta-cpu'], 'memory_gib': 56}]
    BIG = [{'labels': ['ci-cpu', 'mem-big', 'hostc-cpu'], 'memory_gib': 160}]

    def cap(self):
        return (host(ci_cpu=R(1), pools=self.POOLS), host('hostc', ci_cpu=R(1), mem_big=R(1), pools=self.BIG))

    def test_mem_big_where_the_memory_exceeds_the_largest_slot_without_it(self):
        self.assertEqual(one(part(memory_gib=56), *self.cap())['labels'], ['self-hosted', 'ci-cpu'])
        self.assertEqual(one(part(memory_gib=57), *self.cap())['labels'], ['self-hosted', 'ci-cpu', 'mem-big'])
        self.assertEqual(one(part(), *self.cap())['labels'], ['self-hosted', 'ci-cpu'])  # the registry's default
        unreported = one(part(memory_gib=500), host(ci_cpu=R(1)))
        self.assertEqual(unreported['labels'], ['self-hosted', 'ci-cpu'])
        self.assertIn('slot memory not reported', unreported['why'])

    def test_a_memory_no_slot_fits_is_a_plan_time_error(self):
        with self.assertRaisesRegex(rt.RouteError, r'ci-cpu: part p: 161 GiB fits no slot .*largest 160 GiB'):
            one(part(memory_gib=161), *self.cap())
        with self.assertRaisesRegex(rt.RouteError, 'fits no slot'):
            one(part(memory_gib=57), host(ci_cpu=R(1), pools=self.POOLS))

    def test_a_mem_big_part_overflows_only_where_the_target_holds_it(self):
        full = (host(ci_cpu=R(0), pools=self.POOLS), host('hostc', ci_cpu=R(0), mem_big=R(0), pools=self.BIG), SLURMS)
        self.assertEqual(one(part(memory_gib=100), *full, target())['where'], 'host')
        self.assertEqual(one(part(memory_gib=100), *full, target(memory_gib=128))['target'], 'cpu')
        self.assertEqual(one(part(memory_gib=40), *full, target())['where'], 'slurm')  # not mem-big: allowed


class PlanTime(unittest.TestCase):
    def test_a_label_no_host_serves_fails_the_plan_naming_it(self):
        cap = rows(host(ci_cpu=R(1)), host('hostb', problems=['drained'], ci_gpu2=R(1)), SLURMS, target())
        with self.assertRaises(rt.RouteError) as e:
            rt.route([part('a'), part('b', 'gpu2'), part('c', 'timing')], cap)
        self.assertEqual([x.split(':')[0] for x in e.exception.errors], ['ci-gpu2', 'ci-timing'])
        for why in (['not read'], ['no runner online'], ['2 slot lock file(s) missing']):
            with self.assertRaisesRegex(rt.RouteError, r'ci-gpu: part p: no host serves the label \(every host'):
                one(part(needs='gpu'), host(problems=why, ci_gpu=R(1)))
        pools = [{'labels': ['ci-cpu'], 'memory_gib': 8}, {'labels': ['ci-cpu', 'mem-big'], 'memory_gib': 200}]
        with self.assertRaisesRegex(rt.RouteError, 'ci-cpu [+] mem-big: part p: no host serves the labels together'):
            one(part(memory_gib=100), host(ci_cpu=R(1), pools=pools))  # its runners do not carry mem-big

    def test_a_runner_held_back_by_presence_still_serves(self):
        held = host(ci_gpu=R(0, registered=2, online=0))  # cards in use: presence stopped both runners
        self.assertEqual(one(part(needs='gpu'), held)['labels'], ['self-hosted', 'ci-gpu'])
        self.assertEqual(one(part(needs='gpu'), held, SLURMS, target(gpu='m'))['where'], 'slurm')  # 0 free
        with self.assertRaises(rt.RouteError):
            one(part(needs='gpu'), host(ci_gpu=R(0, registered=0, online=0)))

    def test_a_host_whose_runners_are_not_read_might_serve(self):
        self.assertEqual(one(part(needs='gpu2'), host('hostc'))['labels'], ['self-hosted', 'ci-gpu2'])
        with self.assertRaises(rt.RouteError):
            one(part(needs='gpu2'), host('hostc', problems=['drained']))

    def test_an_unknown_need_is_the_plans_fault(self):
        with self.assertRaisesRegex(rt.PlanError, "p: needs 'tpu' is not one of cpu, gpu, gpu2, timing, light"):
            rt.route([part(needs='tpu')], rows(host(ci_cpu=R(1))))
        with self.assertRaisesRegex(rt.PlanError, r"p: needs \['cpu'\]"):
            rt.route([part(needs=['cpu'])], rows(host(ci_cpu=R(1))))
        with self.assertRaisesRegex(rt.PlanError, 'needs'):
            rt.route([part(needs='tiny')], None)


class Unreadable(unittest.TestCase):
    def test_shared_labels_without_overflow_and_said(self):
        for cap in (None, []):
            got = rt.route([part('a', 'gpu2', memory_gib=900), part('b', 'timing')], cap, 'no capacity file capacity.json')
            self.assertEqual([g['labels'] for g in got], [['self-hosted', 'ci-gpu2'], ['self-hosted', 'ci-timing']])
            self.assertEqual([g['timeout_minutes'] for g in got], [25, 25])
            self.assertIn('capacity unreadable', got[0]['why'])


class Plan(unittest.TestCase):
    def test_a_schema_1_plan_loads(self):
        doc = {'schema': 1, 'parts': [part(), part('q', 'gpu', 240, memory_gib=8, results='r'), part('l', 'light', 15)]}
        self.assertEqual(rt.load_plan(doc), doc['parts'])
        self.assertEqual(rt.load_plan({'schema': 1, 'parts': [], 'files': 'x', 'max_parallel': 2}), [])  # nothing to run

    def test_each_fault_is_named(self):
        cases = [({'schema': 2, 'parts': [part()]}, 'schema 2'), ({'schema': 1}, 'and optionally files, max_parallel'),
                 ({'schema': 1, 'parts': [], 'other': 1}, 'and optionally'),
                 ({'schema': 1, 'parts': {}}, 'at most 256'), ({'schema': 1, 'parts': [part()] * 257}, 'at most 256'),
                 ({'schema': 1, 'parts': [part(needs='light', minutes=16)]}, r'minutes is a number in \(0, 15\] for needs light'),
                 ({'schema': 1, 'parts': [part(extra=1)]}, r"unknown key\(s\) \['extra'\]"),
                 ({'schema': 1, 'parts': [{'id': 'p'}]}, 'missing'),
                 ({'schema': 1, 'parts': [part(), part()]}, 'unique'), ({'schema': 1, 'parts': [part('-x')]}, 'unique'),
                 ({'schema': 1, 'parts': [part(needs='tpu')]}, "needs 'tpu'"),
                 ({'schema': 1, 'parts': [part(minutes=241)]}, 'minutes'), ({'schema': 1, 'parts': [part(minutes=0)]}, 'minutes'),
                 ({'schema': 1, 'parts': [part(minutes=True)]}, 'minutes'),
                 ({'schema': 1, 'parts': [part(memory_gib=0)]}, 'memory_gib'),
                 ({'schema': 1, 'parts': [part(history='yes')]}, 'p: history is true or false'),
                 ({'schema': 1, 'parts': [part(history=1)]}, 'p: history is true or false'),
                 ({'schema': 1, 'parts': [dict(part(), command=' ')]}, 'command'), ({'schema': 1, 'parts': [3]}, 'not an object'),
                 ({'schema': True, 'parts': [part()]}, 'schema True'), ({'schema': 1, 'parts': [part(5)]}, 'part 0: an id'),
                 ({'schema': 1, 'parts': [part(['x'])]}, 'part 0: an id'),
                 ({'schema': 1, 'parts': [part(needs=['cpu'])]}, r"needs \['cpu'\]")]
        for doc, why in cases:
            with self.assertRaisesRegex(rt.PlanError, why):
                rt.load_plan(doc)


def first(outcome, next_='retry', **kw):
    return {'outcome': outcome, 'next': next_, 'attempt': 1, 'host': 'hosta', **kw}


class Retry(unittest.TestCase):
    HOSTP = {'id': 'p', 'command': 'c', 'needs': 'gpu', 'minutes': 10, 'labels': ['self-hosted', 'ci-gpu'],
             'where': 'host', 'target': '', 'timeout_minutes': 25}
    SLURMP = dict(HOSTP, labels=['self-hosted', 'ci-slurm'], where='slurm', target='v', timeout_minutes=40)

    def test_the_rows_from_outcome_and_next(self):
        self.assertEqual([rt.row_of(o, 'retry') for o in ('thermal', 'reboot', 'unavailable', 'oom', 'lost', 'stopped', 'red')],
                         ['cool-card', 'same-host', 'same-host', 'more-memory', 'any', 'any', ''])
        self.assertEqual((rt.row_of('unavailable', 'fallback'), rt.row_of('thermal', 'fallback'), rt.row_of('red', 'none')),
                         ('fallback', 'fallback', ''))

    def test_cool_card(self):
        got = rt.retry(self.HOSTP, first('thermal', cool_card=3))
        self.assertEqual((got['retry'], got['row'], got['labels'], got['resume']), (True, 'cool-card', ['self-hosted', 'hosta-gpu3'], False))
        got = rt.retry(self.HOSTP, first('thermal'), rows(host(ci_gpu=R(1))))  # no card recorded: routed again
        self.assertEqual((got['row'], got['labels']), ('fallback', ['self-hosted', 'ci-gpu']))
        two = dict(self.HOSTP, needs='gpu2', labels=['self-hosted', 'ci-gpu2'])
        got = rt.retry(two, first('thermal', cool_card=3), rows(host(ci_gpu2=R(1))))  # a card's runner holds one card
        self.assertEqual((got['row'], got['labels']), ('fallback', ['self-hosted', 'ci-gpu2']))

    def test_same_host(self):
        got = rt.retry(self.HOSTP, first('reboot', pool_labels=['ci-gpu', 'hosta-gpu', 'hosta-gpu1', 'mem-big']))
        self.assertEqual((got['row'], got['labels']), ('same-host', ['self-hosted', 'hosta-gpu']))
        got = rt.retry(self.SLURMP, first('unavailable', pool_labels=['ci-slurm', 'hostb-slurm']))
        self.assertEqual((got['labels'], got['where'], got['target'], got['timeout_minutes']),
                         (['self-hosted', 'ci-slurm'], 'slurm', 'v', 40))
        got = rt.retry(self.HOSTP, first('reboot'))
        self.assertEqual((got['row'], got['labels']), ('any', ['self-hosted', 'ci-gpu']))

    def test_more_memory(self):
        big = rows(host(ci_gpu=R(1), mem_big=R(1)))
        got = rt.retry(self.HOSTP, first('oom', where='more-memory'), big)
        self.assertEqual((got['labels'], got['resume']), (['self-hosted', 'ci-gpu', 'mem-big'], True))
        self.assertEqual(rt.retry(dict(self.HOSTP, labels=got['labels']), first('oom'), big)['labels'], got['labels'])
        self.assertEqual(rt.retry(self.SLURMP, first('oom'), big)['labels'], ['self-hosted', 'ci-gpu', 'mem-big'])
        for cap in (rows(host(ci_gpu=R(1))), None):  # mem-big not proven served: it would wait 24 h
            got = rt.retry(self.SLURMP, first('oom'), cap)
            self.assertEqual((got['retry'], got['labels'], got['where'], got['resume']),
                             (True, ['self-hosted', 'ci-gpu'], 'host', True))
            self.assertIn('no host proven to serve ci-gpu + mem-big', got['why'])

    def test_any(self):
        for o in ('lost', 'stopped'):
            got = rt.retry(self.SLURMP, first(o))
            self.assertEqual((got['row'], got['labels'], got['target']), ('any', ['self-hosted', 'ci-slurm'], 'v'))

    def test_fallback_routes_the_needs_again(self):
        got = rt.retry(self.HOSTP, first('unavailable', 'fallback'), rows(host(ci_gpu=R(0)), SLURMS, target(gpu='m')))
        self.assertEqual((got['row'], got['labels'], got['where'], got['target']),
                         ('fallback', ['self-hosted', 'ci-slurm'], 'slurm', 'cpu'))
        got = rt.retry(self.HOSTP, first('unavailable', 'fallback'))
        self.assertEqual((got['labels'], got['where']), (['self-hosted', 'ci-gpu'], 'host'))
        got = rt.retry(self.HOSTP, first('unavailable', 'fallback'), rows(host(problems=['drained'], ci_gpu=R(1))))
        self.assertFalse(got['retry'])
        self.assertIn('no host serves', got['why'])

    def test_at_most_one_retry(self):
        self.assertFalse(rt.retry(self.HOSTP, first('lost', attempt=2))['retry'])
        self.assertFalse(rt.retry(self.HOSTP, first('red', 'none'))['retry'])
        self.assertFalse(rt.retry(self.HOSTP, first('lost', where='elsewhere'))['retry'])
        self.assertEqual(rt.retry(self.HOSTP, first('lost', where='any'))['labels'], ['self-hosted', 'ci-gpu'])


class Cli(unittest.TestCase):
    PLAN = {'schema': 1, 'parts': [part('a'), part('b', 'gpu')]}

    def run_cli(self, plan, argv=(), etc='/nonexistent-etc'):
        out, err = io.StringIO(), io.StringIO()
        rc = rt.main(list(argv), etc, io.StringIO(plan if isinstance(plan, str) else json.dumps(plan)), out, err)
        return rc, out.getvalue(), err.getvalue()

    def rows_file(self, *rs):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        f = os.path.join(d.name, 'rows.json')
        with open(f, 'w') as fh:
            json.dump(list(rs), fh)
        return f

    def test_the_routed_plan_on_stdout(self):
        now = __import__('time').time()
        f = self.rows_file(dict(host(ci_cpu=R(1), ci_gpu=R(1)), read_at=now))
        rc, out, err = self.run_cli(self.PLAN, ['--rows', f])
        self.assertEqual((rc, err), (0, ''))
        doc = json.loads(out)
        self.assertEqual((doc['schema'], doc['capacity']), (1, 'read'))
        self.assertEqual([(p['id'], p['labels'], p['where'], p['timeout_minutes']) for p in doc['parts']],
                         [('a', ['self-hosted', 'ci-cpu'], 'host', 25), ('b', ['self-hosted', 'ci-gpu'], 'host', 25)])

    def test_a_plan_time_error_exits_1_naming_the_label(self):
        now = __import__('time').time()
        f = self.rows_file(dict(host(ci_cpu=R(1)), read_at=now))
        rc, out, err = self.run_cli(self.PLAN, ['--rows', f])
        self.assertEqual((rc, out), (1, ''))
        self.assertTrue(err.startswith('route: ci-gpu: part b: no host serves'), err)

    def test_a_plan_that_does_not_load_exits_2(self):
        for plan in ('{', {'schema': 1, 'parts': [part(needs='tpu')]}):
            rc, out, err = self.run_cli(plan)
            self.assertEqual((rc, out), (2, ''))
            self.assertIn('the plan does not load', err)

    def test_unreadable_capacity_routes_and_says_so_without_a_path(self):
        rc, out, err = self.run_cli(self.PLAN, etc='/nonexistent-etc/etc')
        self.assertEqual(rc, 0)
        doc = json.loads(out)
        self.assertTrue(doc['capacity'].startswith('unreadable: no registry'), doc['capacity'])
        self.assertNotIn('/nonexistent', out + err)
        self.assertIn('pool.toml', doc['capacity'])
        self.assertEqual([p['labels'][1] for p in doc['parts']], ['ci-cpu', 'ci-gpu'])
        self.assertIn('no overflow', err)
        rc, out, _ = self.run_cli(self.PLAN, ['--rows', self.rows_file()])  # no rows
        self.assertEqual((rc, json.loads(out)['capacity']), (0, 'unreadable: no rows'))

    def test_ci_run_route(self):
        import contextlib
        import sys
        old = os.environ.get('CI_RUNNERS_ETC')
        os.environ['CI_RUNNERS_ETC'] = '/nonexistent-etc'
        self.addCleanup(lambda: os.environ.pop('CI_RUNNERS_ETC') if old is None else os.environ.update(CI_RUNNERS_ETC=old))
        sys_in = sys.stdin
        sys.stdin = io.StringIO(json.dumps(self.PLAN))
        self.addCleanup(setattr, sys, 'stdin', sys_in)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(['route']), 0)
        self.assertEqual(len(json.loads(out.getvalue())['parts']), 2)

    def test_redact(self):
        self.assertEqual(rt.redact("no such file: '/a/b/etc/pool.toml' and /x/y"), "no such file: 'pool.toml' and y")
        self.assertEqual(rt.redact('a/b https://e.org/x'), 'a/b https://e.org/x')


if __name__ == '__main__':
    unittest.main()
