"""Sized CPU pools, from inventory and setup through capacity and the plan."""
import unittest

from ci_runners import drain, flow, inventory, part, pool, presence, route, setup
from tests.test_inventory import PLAIN
from tests.test_route import SLURMS, host, part as planned, rows, target


def fixture():
    role = inventory.load(PLAIN).this
    old = f'part = {{ count = 3, labels = ["{role}-cpu"] }}'
    lines = [old.replace(f'"{role}-cpu"', f'"{role}-cpu", "ci-cpu"')]
    for width, cpus in ((4, '16-19'), (6, '20-25'), (8, '26-33')):
        lines.append(f'cpu{width} = {{ class = "part", count = 1, '
                     f'labels = ["{role}-cpu{width}", "ci-cpu-{width}", "ci-cpu"], '
                     f'slot = {{ cpus = ["{cpus}"], workers = {width}, memory = "10G", speed = 1.0 }} }}')
    return PLAIN.replace(old, '\n'.join(lines)).replace('slots = 2', 'slots = 5', 1)


def dynamic_fixture():
    return PLAIN.replace('part = { count = 3, labels = ["3090b-cpu"] }',
                         'part = { count = 2, labels = ["3090b-cpu", "ci-cpu", "ci-cpu-dynamic"], '
                         'slot = { cpus = ["0-7", "8-17"], workers = 6, memory = "10G", speed = 1.0 } }')


def snapshot(inv, alive, busy=(), yielded=()):
    p = {'runner_names': list(alive), 'runner_busy': list(busy)}
    return host(pools=pool.pool_table(inv, p, set(yielded)),
                **pool._label_counts([(n, labels) for n, labels, _ in setup.runners(inv)], p))


class SizedPools(unittest.TestCase):
    def setUp(self):
        self.inv = inventory.load(fixture())

    def names(self, *widths):
        return [f'{self.inv.this}-cpu{width}-1' for width in widths]

    def test_same_host_routes_and_binds_distinct_slots(self):
        inv = inventory.load(dynamic_fixture())
        cap = rows(snapshot(inv, [f'{inv.this}-part-{n}' for n in (1, 2)]))
        plans, _ = flow.load({'schema': 1, 'parts': [planned(str(n), cores=n) for n in (4, 6, 8)]})
        routed = route.route(plans, cap)
        for p in routed:
            self.assertEqual(p['labels'], ['self-hosted', 'ci-cpu-dynamic'])
        self.assertEqual([p['why'] for p in routed[:2]], ['2 free', '1 free'])
        self.assertIn('0 free', routed[2]['why'])
        self.assertEqual(next(p['max_cores'] for p in cap[0].row['pools'] if p['pool'] == 'part'), 8)
        for index, cpus in enumerate(('0-7', '8-17')):
            env = dict(line.split('=', 1) for line in setup.env_file(inv, 'part', '/etc/ci', slot=index,
                                                                    pool='part').splitlines())
            self.assertEqual(env['CI_RUNNER_SLOT'], str(index))
            slot, why = part.runner_slot(inv.host, 'part', env)
            self.assertEqual(why, '')
            self.assertEqual(slot.cpus, cpus)

        # An old installed inventory is still readable during migration, but is not routable by width.
        old_env = dict(line.split('=', 1) for line in setup.env_file(self.inv, 'part', '/etc/ci', slot=0,
                                                                    pool='cpu6').splitlines())
        self.assertEqual(old_env['CI_RUNNER_POOL'], 'cpu6')
        self.assertEqual(part.runner_slot(self.inv.host, 'part', old_env)[0].cpus, '20-25')

    def test_dynamic_inventory_requires_one_private_slotted_generic_pool(self):
        for text in (dynamic_fixture().replace('"ci-cpu", ', ''),
                     PLAIN.replace('"3090b-cpu"]','"3090b-cpu", "ci-cpu", "ci-cpu-dynamic"]'),
                     fixture().replace('"ci-cpu-4"', '"ci-cpu-dynamic"')):
            with self.subTest(text=text), self.assertRaisesRegex(inventory.InventoryError, 'private slotted ci-cpu pool'):
                inventory.load(text)

    def test_inventory_rejects_wrong_width_duplicate_and_overlap(self):
        source = fixture()
        for changed, why in (
            (source.replace('"20-25"', '"20-24"'), 'match ci-cpu-6'),
            (source.replace('"20-25"', '"19-24"'), 'overlapping slot CPUs'),
            (source.replace('"ci-cpu-6"', '"ci-cpu-4"'), 'duplicate CPU pool width'),
        ):
            with self.subTest(why=why), self.assertRaisesRegex(inventory.InventoryError, why):
                inventory.load(changed)

    def test_inventory_needs_one_shared_slot_per_cpu_and_timing_pool(self):
        source = fixture()
        with self.assertRaisesRegex(inventory.InventoryError, r'4 shared CI slots for 5 pools'):
            inventory.load(source.replace('slots = 5', 'slots = 4', 1))
        self.assertEqual(inventory.load(source).host['slots'], 5)
        self.assertEqual(inventory.load(PLAIN.replace('slots = 2', 'slots = 1', 1)).host['slots'], 1)

    def test_inventory_keeps_legacy_nonnumeric_cpu_labels(self):
        role = self.inv.this
        legacy = PLAIN.replace(f'"{role}-cpu"', f'"{role}-cpu", "ci-cpu-avx512"', 1)
        inv = inventory.load(legacy)
        self.assertIn('ci-cpu-avx512', inventory.labels_of(inv.host, 'part'))
        mixed = fixture().replace('"ci-cpu-4"', '"ci-cpu-4", "ci-cpu-fast"', 1)
        inv = inventory.load(mixed)
        self.assertIn('ci-cpu-fast', inventory.labels_of(inv.host, 'cpu4'))
        for invalid in ('ci-cpu-0', 'ci-cpu-04'):
            with self.subTest(label=invalid), self.assertRaisesRegex(inventory.InventoryError, 'ci-cpu-N label'):
                inventory.load(mixed.replace('"ci-cpu-4"', f'"{invalid}"', 1))

    def test_rejects_invalid_cores_and_public_route(self):
        for p in [planned(cores=x) for x in (0, -1, 6.0, '6', True, None)] + [planned(needs='gpu', cores=6)]:
            with self.subTest(p=p), self.assertRaisesRegex(route.PlanError, 'cores'):
                flow.load({'schema': 1, 'parts': [p]})
        with self.assertRaisesRegex(route.PlanError, 'public repositories'):
            flow.route_public([planned(cores=6)], 'owner/project')

    def test_shared_bookings_and_legacy_overflow(self):
        inv = inventory.load(dynamic_fixture())
        raw = snapshot(inv, [f'{inv.this}-part-1'])
        cap = rows(raw, SLURMS, target())
        sized, generic = route.route([planned('six', cores=6), planned('generic')], cap)
        self.assertEqual(sized['why'], '1 free')
        self.assertEqual(generic['where'], 'slurm')
        generic, sized = route.route([planned('generic'), planned('six', cores=6)], cap)
        self.assertEqual(generic['labels'], ['self-hosted', 'ci-cpu'])
        self.assertEqual(sized['where'], 'host')
        self.assertIn('0 free; exact CPU width never overflows', sized['why'])
        with self.assertRaisesRegex(route.RouteError, 'exceeds dynamic pool limit 8'):
            route.route([planned(cores=9)], cap)
        legacy = rows(snapshot(self.inv, self.names(6)))
        for unverified in (None, legacy, rows({**raw, 'read_at': 0})):
            with self.assertRaisesRegex(route.RouteError, 'no verified dynamic CPU capacity'):
                route.route([planned(cores=6)], unverified)
        self.assertEqual(route.route([planned()], None)[0]['labels'], ['self-hosted', 'ci-cpu'])

    def test_rejects_inconsistent_shared_label_limits(self):
        inv = inventory.load(dynamic_fixture())
        raw = snapshot(inv, [f'{inv.this}-part-1'])
        lower = {**raw, 'key': 'peer', 'pools': [
            {**p, 'max_cores': 4} if p['pool']=='part' else p for p in raw['pools']]}
        with self.assertRaisesRegex(route.RouteError, 'no verified dynamic CPU capacity'):
            route.route([planned(cores=6)], rows(raw, lower))

    def test_retries_keep_width(self):
        inv = inventory.load(dynamic_fixture())
        cap = rows(snapshot(inv, [f'{inv.this}-part-1']))
        p = route.route([planned(cores=6, workers=3)], cap)[0]
        for kind in ('same-host', 'fallback', 'more-memory', 'any'):
            retry = route.retry(p, {'where': kind, 'next': 'retry', 'host': self.inv.this,
                                    'pool_labels': ['ci-cpu-6']}, cap)
            with self.subTest(row=kind):
                self.assertEqual(retry['labels'], ['self-hosted', 'ci-cpu-dynamic'])
                self.assertFalse(route.retry(p, {'where':kind,'next':'retry'}, None)['retry'])
        matrix = flow.entry(p)
        self.assertEqual((matrix['cores'], matrix['workers']), ('6', '3'))
        self.assertEqual(flow.decide(matrix, None, '', cap, '', 1, {})['labels'], ['self-hosted','ci-cpu-dynamic'])
        self.assertIsNone(flow.decide(matrix, None, '', None, '', 1, {}))
        without_override = route.route([planned(cores=6)], cap)[0]
        self.assertEqual({k:v for k,v in p.items() if k!='workers'}, without_override)

    def test_drain_and_tier_by_pool(self):
        rs = [r for r in presence.runners(self.inv) if r.pool in ('cpu4', 'cpu6', 'cpu8')]
        state = {'host': None, 'pools': {drain.pool_key(self.inv.host, 'cpu4'): 1}}
        self.assertTrue(drain.refusal(state, self.inv.host, 'part', 'cpu4'))
        self.assertFalse(drain.refusal(state, self.inv.host, 'part', 'cpu6'))
        state['pools'] = {drain.pool_key(self.inv.host, 'class:part'): 1}
        self.assertTrue(all(drain.refusal(state, self.inv.host, r.cls, r.pool) for r in rs))
        peer = {'tier': 1, 'key': 'peer', 'read_at': 10, 'policy': {'repositories': 'same'},
                'pools': [{'labels': ['ci-cpu', 'ci-cpu-6'], 'listening': 1}]}
        self.assertEqual(set(presence.tier_yield(rs, {}, (2, [peer], 'same'), {}, 10)), set(self.names(6)))
