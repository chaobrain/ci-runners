"""``place`` (design §4.2): hosts and cards from probe results; a host that fails a probe
gets no part; one card per h100 part (D7)."""
from __future__ import annotations

import unittest

from ci_runners import place as pl
from ci_runners.place import Card, Part, Probe

R3090 = frozenset({'light', 'part', 'compile'})
H100_RUNNERS = frozenset(f'h100-gpu{i}' for i in range(5))


def p3090(**kw):
    base = dict(reachable=True, runners=R3090, proxy=True, slots=2, cards=(Card(0, 20000), Card(1, 24000)))
    return Probe(**{**base, **kw})


def h100(**kw):
    base = dict(reachable=True, runners=H100_RUNNERS, proxy=True, slots=5, slowdown=2.7, remote=True,
                cards=tuple(Card(i, 80000, temp_c=40 + i) for i in range(5)))
    return Probe(**{**base, **kw})


PREFER = {'gpu': ['h100', '3090a', '3090b'], 'cpu': ['3090a', '3090b'], 'compile': ['3090b']}
GPU = Part('g', 'gpu', gpus=1, gpu_mem_mib=6000, estimate_minutes=10)
CPU = Part('c', 'cpu', estimate_minutes=20)


def plan(parts, probes, prefer=PREFER, exclude=()):
    return pl.place(parts, probes, prefer, exclude)


class Place(unittest.TestCase):
    def test_preferred_host_and_its_fallback(self):
        out = plan([GPU, CPU, Part('k', 'compile', estimate_minutes=5)],
                   {'h100': h100(), '3090a': p3090(runners=frozenset({'part'})), '3090b': p3090()})
        self.assertEqual(out[0], {'id': 'g', 'runs-on': ['self-hosted', 'h100-gpu0'],
                                  'fallback': ['self-hosted', 'part', '3090a'], 'timeout-minutes': 84,
                                  'gpus': 1, 'inner-timeout-minutes': 74})
        self.assertEqual(out[1], {'id': 'c', 'runs-on': ['self-hosted', 'part', '3090a'],
                                  'fallback': ['self-hosted', 'part', '3090b'], 'timeout-minutes': 70,
                                  'gpus': 0, 'inner-timeout-minutes': 60})
        self.assertEqual(out[2], {'id': 'k', 'runs-on': ['self-hosted', 'compile', '3090b'], 'fallback': None,
                                  'timeout-minutes': 40, 'gpus': 0, 'inner-timeout-minutes': 30})
        for e in out:  # the inner bound leaves the job room, and stays a valid part timeout
            self.assertTrue(0 < e['inner-timeout-minutes'] <= e['timeout-minutes'] - pl.INNER_MARGIN)

    def test_a_host_that_fails_a_probe_gets_no_part(self):
        for bad in (dict(reachable=False), dict(proxy=False), dict(reserved=True), dict(quarantined=True),
                    dict(runners=frozenset())):
            out = plan([CPU], {'3090a': p3090(**bad), '3090b': p3090()})
            self.assertEqual(out[0]['runs-on'], ['self-hosted', 'part', '3090b'], bad)
            self.assertIsNone(out[0]['fallback'], bad)
        out = plan([CPU], {'3090b': p3090()})  # a host without a probe result
        self.assertEqual(out[0]['runs-on'], ['self-hosted', 'part', '3090b'])

    def test_no_candidate(self):
        out = plan([CPU], {'3090a': p3090(proxy=False), '3090b': p3090(reachable=False)})
        self.assertEqual((out[0]['runs-on'], out[0]['timeout-minutes'], out[0]['inner-timeout-minutes'], out[0]['gpus']),
                         (None, None, None, 0))
        self.assertEqual(out[0]['reason'], '3090a: proxy down; 3090b: unreachable')
        self.assertEqual(plan([GPU], {})[0]['gpus'], 1)  # the part's own count, with or without a host
        self.assertEqual(plan([CPU], {}, prefer={})[0]['reason'], 'no host for this class')
        self.assertEqual(plan([CPU], {'3090a': p3090(), '3090b': p3090()}, exclude=['3090a', '3090b'])[0]['runs-on'],
                         None)

    def test_excluded_hosts(self):
        out = plan([CPU], {'3090a': p3090(), '3090b': p3090()}, exclude=['3090a'])
        self.assertEqual(out[0]['runs-on'][-1], '3090b')

    def test_h100_takes_gpu_parts_with_one_card_only(self):
        probes = {'h100': h100(), '3090a': p3090()}
        two = Part('g2', 'gpu', gpus=2, gpu_mem_mib=6000)
        self.assertEqual(plan([two], probes)[0]['runs-on'], ['self-hosted', 'part', '3090a'])  # D7
        self.assertEqual(plan([CPU], probes, prefer={'cpu': ['h100', '3090a']})[0]['runs-on'][-1], '3090a')

    def test_each_h100_part_gets_its_own_coolest_fitting_card(self):
        cards = (Card(0, 80000, temp_c=85), Card(1, 80000, temp_c=50, others=True), Card(2, 1000, temp_c=30),
                 Card(3, 80000, temp_c=60, healthy=False), Card(4, 70000, temp_c=45), Card(5, 80000, temp_c=45),
                 Card(6, 80000, temp_c=20))
        runners = frozenset(f'h100-gpu{i}' for i in range(6))  # card 6 has no runner
        parts = [Part(f'g{i}', 'gpu', gpus=1, gpu_mem_mib=6000) for i in range(3)]
        out = plan(parts, {'h100': h100(cards=cards, runners=runners), '3090a': p3090()})
        self.assertEqual([o['runs-on'][-1] for o in out], ['h100-gpu5', 'h100-gpu4', '3090a'])
        self.assertEqual(out[0]['fallback'], ['self-hosted', 'part', '3090a'])

    def test_a_3090_needs_enough_fitting_cards(self):
        probes = {'3090a': p3090(cards=(Card(0, 5000), Card(1, 24000))), '3090b': p3090()}
        two = Part('g2', 'gpu', gpus=2, gpu_mem_mib=6000)
        self.assertEqual(plan([two], probes, prefer={'gpu': ['3090a', '3090b']})[0]['runs-on'][-1], '3090b')
        hot = {'3090a': p3090(cards=(Card(0, 24000, temp_c=80),)), '3090b': p3090()}
        self.assertEqual(plan([GPU], hot, prefer={'gpu': ['3090a', '3090b']})[0]['runs-on'][-1], '3090b')

    def test_load_spreads_to_a_host_with_room_and_waits_on_the_first_when_none_has(self):
        probes = {'3090a': p3090(busy=1), '3090b': p3090(busy=0)}
        out = plan([Part(f'c{i}', 'cpu') for i in range(5)], probes)
        self.assertEqual([o['runs-on'][-1] for o in out], ['3090a', '3090b', '3090b', '3090a', '3090a'])
        self.assertEqual(out[1]['fallback'][-1], '3090a')

    def test_timeout(self):
        self.assertEqual(pl.timeout_minutes(0), 30)
        self.assertEqual(pl.timeout_minutes(-5), 30)
        self.assertEqual(pl.timeout_minutes(10), 50)
        self.assertEqual(pl.timeout_minutes(10, 2.7), 84)  # ceil(2 * 27 + 30)
        self.assertEqual(pl.timeout_minutes(105), 240)
        self.assertEqual(pl.timeout_minutes(104.6), 240)
        self.assertEqual(pl.timeout_minutes(104.4), 239)
        self.assertEqual(pl.timeout_minutes(10, 0.5), 50)  # never below the reference host's
        self.assertEqual(pl.timeout_minutes(500, cap=600), 600)


class Parse(unittest.TestCase):
    def test_parse(self):
        got = pl.parse_parts([{'id': 'a', 'class': 'gpu', 'gpus': 1, 'gpu-mem-mib': 6000, 'memory': '56G',
                               'estimate-minutes': 12.5}, {'id': 'b', 'class': 'cpu'}])
        self.assertEqual(got, [Part('a', 'gpu', 1, 6000, '56G', 12.5), Part('b', 'cpu')])

    def test_malformed(self):
        for bad in ({}, [{'class': 'cpu'}], [{'id': 'a', 'class': 'tpu'}], [{'id': 'a', 'class': 'cpu', 'gpus': 1}],
                    [{'id': 'a', 'class': 'gpu'}], [{'id': 'a', 'class': 'gpu', 'gpus': True}],
                    [{'id': 'a', 'class': 'cpu', 'estimate-minutes': '5'}],
                    [{'id': 'a', 'class': 'cpu'}, {'id': 'a', 'class': 'cpu'}]):
            with self.assertRaises(ValueError, msg=bad):
                pl.parse_parts(bad)


if __name__ == '__main__':
    unittest.main()
