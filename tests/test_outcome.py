"""Outcome classification and retry hints (design §4.4, decision 8), on
``memory.events``, boot-id changes, the early stop and a stopped runner."""
from __future__ import annotations

import pathlib
import unittest

from ci_runners import outcome as oc

FX = pathlib.Path(__file__).resolve().parent / 'fixtures' / 'host'
A8 = '00000000:A8:00.0'  # card 3 in smi_bus.csv


def E(**kw):
    kw.setdefault('rc', 1)
    return oc.Evidence(**kw)


def kind(**kw):
    return oc.classify(E(**kw)).kind


class Bus(unittest.TestCase):
    def test_bus_key(self):
        for s in (A8, 'a8', 'A8', '0000:a8:00.0', 'a8:00.0', ' 00A8 '):
            self.assertEqual(oc.bus_key(s), 'a8', s)
        self.assertEqual(oc.bus_key('00000000:00:00.0'), '0')


class Oom(unittest.TestCase):
    def test_memory_events(self):
        self.assertEqual(oc.memory_oom_kills((FX / 'memory.events').read_text()), 2)
        self.assertEqual(oc.memory_oom_kills((FX / 'memory.events.clean').read_text()), 0)
        self.assertEqual(oc.memory_oom_kills('oom 3\n'), 0)  # oom without a kill is not a kill
        self.assertEqual(oc.memory_oom_kills(''), 0)


class Classify(unittest.TestCase):
    def test_each_outcome_from_its_own_evidence(self):
        self.assertEqual(kind(rc=0), 'pass')
        self.assertEqual(kind(rc=1), 'red')
        self.assertEqual(kind(rc=0, timed_out=True), 'red')  # the inner timeout is red
        self.assertEqual(kind(rc=None), 'lost')  # the record never got an exit status
        self.assertEqual(kind(rc=137, oom_kills=1), 'oom')  # a nonzero exit with an OOM kill is not red
        self.assertEqual(kind(rc=143, stopped=True), 'stopped')
        self.assertEqual(kind(rc=None, boot_start='b1', boot_end='b2'), 'reboot')
        self.assertEqual(kind(rc=None, boot_start='b1', boot_end='b1'), 'lost')
        self.assertEqual(kind(rc=None, boot_start='', boot_end='b2'), 'lost')  # no first id: no evidence
        self.assertEqual(kind(rc=None, early_stop=True), 'thermal')
        self.assertEqual(kind(rc=None, unavailable='no card fits'), 'unavailable')
        self.assertEqual(oc.classify(E(rc=None, unavailable='low disk: x')).reason, 'low disk: x')

    def test_precedence(self):
        """stopped, reboot, thermal, oom, unavailable, lost, red, pass: each piece of
        evidence removed in turn exposes the next."""
        ev = dict(rc=1, stopped=True, boot_start='b1', boot_end='b2', early_stop=True, oom_kills=1, unavailable='x')
        order = []
        for drop in (('stopped',), ('boot_end',), ('early_stop',), ('oom_kills',), ('unavailable',)):
            order.append(kind(**ev))
            for k in drop:
                del ev[k]
        order.append(kind(**ev))
        order.append(kind(**{**ev, 'rc': None}))
        order.append(kind(**{**ev, 'rc': 0}))
        self.assertEqual(order, ['stopped', 'reboot', 'thermal', 'oom', 'unavailable', 'red', 'lost', 'pass'])
        self.assertEqual(oc.PRECEDENCE, ('stopped', 'reboot', 'thermal', 'oom', 'unavailable', 'lost', 'red', 'pass'))
        self.assertEqual(kind(rc=0, oom_kills=1), 'oom')  # design §4.4: oom ranks above pass


class Hint(unittest.TestCase):
    def test_hints(self):
        H = oc.Hint
        table = {
            'pass': H('none'), 'red': H('none'), 'stopped': H('retry', False, 'any'),
            'oom': H('retry', True, 'more-memory'), 'reboot': H('retry', False, 'same-host'),
            'unavailable': H('fallback'), 'lost': H('retry', False, 'any'),
            'thermal': H('fallback'),
        }
        for k, want in table.items():
            self.assertEqual(oc.hint(k), want, k)
        self.assertEqual(set(table), set(oc.KINDS))
        self.assertEqual(oc.hint('thermal', cool_card=True), H('retry', where='cool-card'))
        # a reboot is retried on the same host, a cool card or not (no h100 rule, decision 13.4)
        self.assertEqual(oc.hint('reboot', cool_card=True), H('retry', where='same-host'))

    def test_a_host_fault_is_not_retried(self):
        """Decision 12.6: a failed P2P self-test is a host fault, so ``next`` is ``none``."""
        self.assertEqual(oc.hint('unavailable', host_fault=True), oc.Hint('none'))
        self.assertEqual(oc.hint('unavailable', host_fault=False), oc.Hint('fallback'))

    def test_a_cancel_asks_for_one_retry(self):
        """Decision 15.4 (#110): ``stopped`` is not final; the consumer's retry job, skipped on
        a cancelled run, decides. Its second attempt is."""
        self.assertNotIn('stopped', oc.FINAL)
        self.assertEqual(oc.hint('stopped'), oc.Hint('retry', where='any'))
        self.assertEqual(oc.hint('stopped', attempt=2), oc.Hint('none'))

    def test_at_most_one_retry(self):
        for k in oc.KINDS:
            self.assertEqual(oc.hint(k, attempt=2), oc.Hint('none'), k)  # a second infrastructure end
            self.assertEqual(oc.hint(k, retries=0), oc.Hint('none'), k)
        self.assertEqual(oc.hint('oom', attempt=2, retries=2).next, 'retry')
        with self.assertRaises(ValueError):
            oc.hint('melted')


if __name__ == '__main__':
    unittest.main()
