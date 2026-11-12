"""Decision 19 (RUN-10 R5b): the preferred host first in the route, the spill's fields, the
rows' pools, policy, health and target bounds, the policy drift warning, the part's
``pool-labels`` and ``cool-card``, and the slot speed check."""
from __future__ import annotations

import io
import json
import os
import re
import tempfile
import textwrap
import unittest
import unittest.mock

from ci_runners import capacity, inventory, part, policy, pool
from ci_runners import route as rt
from tests.test_pool import REGISTRY, inventory_text

T0 = 1_762_905_600.0


def P(name, labels, free=1, registered=None, **kw):
    """A ``pools`` entry: ``labels`` its runners carry, ``free`` of ``registered``."""
    n = max(free, 1) if registered is None else registered
    return {'pool': name, 'class': kw.pop('cls', name), 'labels': list(labels), 'registered': n,
            'online': None if free is None else n, 'free': free, **kw}


def host(key, *pools, prefer=None, role=None, age=10, problems=(), rep=None):
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
    if prefer is not None:
        row['prefer'] = prefer
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


def routed(parts, *rs, prefer=True):
    return rt.route(parts, rows(*rs), prefer=prefer)


def runs_on(labels, rs):
    """GitHub's label matching (§2): the (host, pool) whose runners carry every label."""
    want = set(labels) - {'self-hosted'}
    return {(r['key'], p['pool']) for r in rs if not r.get('target') for p in r['pools'] if want <= set(p['labels'])}


class Prefer(unittest.TestCase):
    def test_a_preferred_host_with_room_gets_the_part_on_its_own_label(self):
        got = routed([part_()], host('hosta', cpu('hosta', 2), prefer=1), host('hostb', cpu('hostb', 3)))[0]
        self.assertEqual(got['labels'], ['self-hosted', 'ci-cpu', 'hosta-cpu'])
        self.assertEqual((got['host'], got['spill_labels'], got['spill_after_s']),
                         ('hosta', ['self-hosted', 'ci-cpu'], rt.SPILL_S))
        self.assertIn('prefer hosta: 2 free of hosta-cpu', got['why'])
        self.assertEqual(rt.SPILL_S, 60)

    def test_without_the_flag_no_host_label(self):
        got = routed([part_()], host('hosta', cpu('hosta', 2), prefer=1), prefer=False)[0]
        self.assertEqual((got['labels'], got['host'], got['spill_labels'], got['spill_after_s']),
                         (['self-hosted', 'ci-cpu'], '', ['self-hosted', 'ci-cpu'], 0))

    def test_the_plans_parts_fill_the_order_then_the_shared_label_then_the_cluster(self):
        rs = (host('hosta', cpu('hosta', 2), prefer=1), host('hostb', cpu('hostb', 1), prefer=2),
              host('hostc', cpu('hostc', 1)), SLURMS, target())
        got = routed([part_(f'p{i}') for i in range(6)], *rs)
        self.assertEqual([g['host'] for g in got], ['hosta', 'hosta', 'hostb', '', '', ''])
        self.assertEqual([g['labels'][-1] for g in got], ['hosta-cpu', 'hosta-cpu', 'hostb-cpu', 'ci-cpu',
                                                          'ci-slurm', 'ci-slurm'])
        self.assertEqual([g['where'] for g in got], ['host'] * 4 + ['slurm'] * 2)  # the host bookings count
        self.assertEqual([g['spill_after_s'] for g in got], [60, 60, 60, 0, 0, 0])
        self.assertEqual(got[4]['spill_labels'], ['self-hosted', 'ci-slurm'])

    def test_lower_first_and_equal_values_in_the_registrys_order(self):
        a, b, c = cpu('hosta'), cpu('hostb'), cpu('hostc')
        self.assertEqual(routed([part_()], host('hosta', a, prefer=2), host('hostb', b, prefer=1))[0]['host'], 'hostb')
        self.assertEqual(routed([part_()], host('hostc', c, prefer=1), host('hosta', a, prefer=1))[0]['host'], 'hostc')
        self.assertEqual(routed([part_()], host('hosta', a), host('hostb', b, prefer=3))[0]['host'], 'hostb')
        for bad in (0, 100, True, '1', 1.0):  # a malformed value: in no order
            self.assertEqual(routed([part_()], host('hosta', a, prefer=bad))[0]['host'], '', msg=repr(bad))
        self.assertEqual(routed([part_()], host('hosta', a, prefer=99))[0]['host'], 'hosta')

    def test_a_host_offers_only_up_fresh_reported_and_without_drift(self):
        other = host('hostb', cpu('hostb', 2))
        cases = {'up': ({}, 'hosta'), 'drained': ({'problems': ['drained']}, ''), 'stale': ({'age': 130}, ''),
                 'older than 60 s': ({'age': 61}, ''), 'drift': ({'rep': 'y'}, ''), 'at 60 s': ({'age': 60}, 'hosta')}
        for why, (kw, want) in cases.items():
            with self.subTest(why):
                norm = (host('hostc', cpu('hostc'), rep='x'), host('hoste', cpu('hoste'), rep='x'))
                got = routed([part_()], host('hosta', cpu('hosta'), prefer=1, **kw), other, *norm)[0]
                self.assertEqual(got['host'], want)
        for why, p in {'free unknown': cpu('hosta', None, registered=2), 'no free': cpu('hosta', 0),
                       'no capability': P('part', ['hosta-cpu'], 1), 'no host label': P('part', ['ci-cpu', 'x-cpu'], 1)}.items():
            with self.subTest(why):
                self.assertEqual(routed([part_()], host('hosta', p, prefer=1), other)[0]['host'], '')
        noroles = host('hosta', cpu('hosta'), prefer=1)
        noroles.pop('role')
        self.assertEqual(routed([part_()], noroles, other)[0]['host'], '')
        nopools = host('hosta', cpu('hosta'), prefer=1)
        nopools.pop('pools')
        self.assertEqual(routed([part_()], nopools, other)[0]['host'], '')

    def test_the_host_label_is_the_roles_never_a_shared_one(self):
        r = host('k', P('part', ['ci-cpu', 'ci-x', 'shared', 'h1', 'h1-cpu'], 1), role='h1', prefer=1)
        self.assertEqual(routed([part_()], r)[0]['labels'], ['self-hosted', 'ci-cpu', 'h1'])  # the bare role
        r = host('k', P('part', ['ci-cpu', 'h1x-cpu', 'h1-cpu'], 1), role='h1', prefer=1)
        self.assertEqual(routed([part_()], r)[0]['labels'], ['self-hosted', 'ci-cpu', 'h1-cpu'])
        r = host('k', P('part', ['ci-cpu', 'ci-cpu2'], 1), role='ci', prefer=1)  # a role named like the shared ones
        self.assertEqual(routed([part_()], r)[0]['host'], '')

    def test_the_needs_it_covers(self):
        pools = (cpu('hosta'), P('cards', ['hosta-gpu', 'ci-gpu'], 2, cls='gpu'), P('compile', ['hosta-compile', 'ci-timing'], 1))
        rs = (host('hosta', *pools, prefer=1), host('hostb', P('multi', ['hostb-multi', 'ci-gpu2'], 1)))
        got = routed([part_('c'), part_('g', 'gpu'), part_('t', 'timing'), part_('m', 'gpu2')], *rs)
        self.assertEqual([g['labels'] for g in got],
                         [['self-hosted', 'ci-cpu', 'hosta-cpu'], ['self-hosted', 'ci-gpu', 'hosta-gpu'],
                          ['self-hosted', 'ci-timing'], ['self-hosted', 'ci-gpu2']])  # timing never; gpu2 not offered
        rs = (host('hosta', *pools, P('multi', ['hosta-multi', 'ci-gpu2'], 1), prefer=1),)
        self.assertEqual(routed([part_('m', 'gpu2')], *rs)[0]['labels'], ['self-hosted', 'ci-gpu2', 'hosta-multi'])

    def test_a_mem_big_part_needs_a_pool_with_both(self):
        small = cpu('hosta', 3, memory_gib=48, speed=1.0)
        big = P('big', ['hosta-big', 'ci-cpu', 'mem-big'], 1, memory_gib=256, speed=1.0)
        other = host('hostb', P('part', ['hostb-cpu', 'ci-cpu', 'mem-big'], 1, memory_gib=200, speed=1.0))
        got = routed([part_('a', memory_gib=100), part_('b', memory_gib=100), part_('c', memory_gib=10)],
                     host('hosta', small, big, prefer=1), other)
        self.assertEqual([g['labels'] for g in got], [['self-hosted', 'ci-cpu', 'mem-big', 'hosta-big'],
                                                       ['self-hosted', 'ci-cpu', 'mem-big'],
                                                       ['self-hosted', 'ci-cpu', 'hosta-cpu']])

    def test_unreadable_or_stale_capacity_prefers_nothing(self):
        p = [part_()]
        self.assertEqual(rt.route(p, None, 'no file', prefer=True)[0]['labels'], ['self-hosted', 'ci-cpu'])
        self.assertEqual(rt.route(p, [], prefer=True)[0]['host'], '')
        allstale = rows(host('hosta', cpu('hosta', 3), prefer=1, age=500), host('hostb', cpu('hostb'), age=500))
        got = rt.route(p, allstale, prefer=True)[0]
        self.assertEqual((got['labels'], got['host'], got['spill_after_s']), (['self-hosted', 'ci-cpu'], '', 0))

    def test_github_matching_reaches_that_hosts_pool_and_the_spill_every_host(self):
        rs = [host('hosta', cpu('hosta', 1), P('light', ['hosta', 'ci-light'], 2), prefer=1, role='hosta'),
              host('hostb', cpu('hostb', 2), prefer=2), host('hostc', cpu('hostc', 1))]
        got = routed([part_(f'p{i}') for i in range(4)], *rs)
        for g in got:
            first, spill = runs_on(g['labels'], rs), runs_on(g['spill_labels'], rs)
            self.assertTrue(first, g)  # a runner takes it
            self.assertEqual(spill, {('hosta', 'part'), ('hostb', 'part'), ('hostc', 'part')})
            if g['host']:
                self.assertEqual(first, {(g['host'], 'part')})  # that host's pool of the class only
            else:
                self.assertEqual(first, spill)

    def test_retries_never_prefer(self):
        rs = rows(host('hosta', cpu('hosta', 2), P('big', ['hosta-big', 'ci-cpu', 'mem-big'], 1), prefer=1))
        p = rt.route([part_()], rs, prefer=True)[0]
        self.assertEqual(p['host'], 'hosta')
        first = {'outcome': 'lost', 'next': 'retry', 'attempt': 1, 'host': 'hosta', 'pool_labels': ['hosta-cpu', 'ci-cpu']}
        self.assertEqual(rt.retry(p, first, rs)['labels'], ['self-hosted', 'ci-cpu'])  # any
        mm = rt.retry(p, {**first, 'outcome': 'oom'}, rs)
        self.assertEqual((mm['row'], mm['labels']), ('more-memory', ['self-hosted', 'ci-cpu', 'mem-big']))
        fb = rt.retry(p, {**first, 'outcome': 'unavailable', 'next': 'fallback'}, rs)
        self.assertEqual((fb['row'], fb['labels']), ('fallback', ['self-hosted', 'ci-cpu']))
        sh = rt.retry(p, {**first, 'outcome': 'reboot'}, rs)
        self.assertEqual(sh['labels'], ['self-hosted', 'hosta-cpu'])  # same-host keeps its host label (16.5)
        old = {k: v for k, v in p.items() if k != 'spill_labels'}  # a part routed before 19: its labels
        self.assertEqual(rt.retry(old, first, rs)['labels'], p['labels'])

    def test_ci_run_route_prefer(self):
        rs = [host('hosta', cpu('hosta', 2), prefer=1, age=0)]
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f:
            json.dump([dict(r, read_at=T0 + 10 ** 9) for r in rs], f)  # read "now", whatever the clock
        self.addCleanup(os.unlink, f.name)
        plan = json.dumps({'schema': 1, 'parts': [part_()]})
        for argv, want in ((['--rows', f.name, '--prefer'], ['self-hosted', 'ci-cpu', 'hosta-cpu']),
                           (['--rows', f.name], ['self-hosted', 'ci-cpu'])):
            out, err = io.StringIO(), io.StringIO()
            self.assertEqual(rt.main(argv, '', io.StringIO(plan), out, err), 0, err.getvalue())
            got = json.loads(out.getvalue())['parts'][0]
            self.assertEqual(got['labels'], want)
            self.assertEqual(got['spill_labels'], ['self-hosted', 'ci-cpu'])


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
        rs = [host('a', rep='x'), host('b', rep='x'), host('c', rep='y'), host('d'), target()]
        self.assertEqual(capacity.policy_drift(rs), {'c': 2})
        self.assertEqual(capacity.policy_drift(rs[:2]), {})
        self.assertEqual(capacity.policy_drift([host('a', rep='x'), host('b', rep='y')]), {'a': 1, 'b': 1})  # a tie
        self.assertEqual(capacity.policy_drift([host('a', rep='x'), host('b', rep='x'), host('c', rep='y'),
                                                host('e', rep='z')]), {'c': 3, 'e': 3})
        odd = dict(target(), policy={'repositories': 'q'})
        self.assertEqual(capacity.policy_drift([*rs[:2], odd]), {})  # a cluster row is no host

    def test_a_warning_not_a_problem_and_judged_afresh(self):
        rs = [host('a', cpu('a'), rep='x', prefer=1), host('b', cpu('b'), rep='x'), host('c', cpu('c'), rep='y')]
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
        # the route names no part to it
        rs = [host('c', cpu('c', 3), rep='y', prefer=1), host('a', cpu('a'), rep='x'), host('b', cpu('b'), rep='x')]
        self.assertEqual(routed([part_()], *rs)[0]['host'], '')


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
    def test_the_registrys_prefer(self):
        text = REGISTRY.replace('numerics_ok = true\n', 'numerics_ok = true\nprefer = 2\n', 1)
        r = pool.load(text)
        self.assertEqual((r.machines['hosta'].prefer, r.machines['hostb'].prefer), (2, None))
        self.assertEqual(pool.host_row(r.machines['hosta'], None)['prefer'], 2)
        self.assertNotIn('prefer', pool.host_row(r.machines['hostb'], None))
        self.assertEqual(pool.load(text.replace('prefer = 2', 'prefer = 99')).machines['hosta'].prefer, 99)
        for bad in ('prefer = 0', 'prefer = 100', 'prefer = true', 'prefer = "1"', 'prefer = 1.5'):
            with self.subTest(bad), self.assertRaisesRegex(pool.RegistryError, 'prefer'):
                pool.load(text.replace('prefer = 2', bad))
        with self.assertRaisesRegex(pool.RegistryError, 'prefer'):
            pool.load(REGISTRY.replace('price = 0.04\n', 'price = 0.04\n', 1).replace(
                'access = "slurm"\n', 'access = "slurm"\nprefer = 1\n'))

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
        lines = open(out, encoding='utf-8').read().splitlines()
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
