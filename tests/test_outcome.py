"""Outcome classification and retry hints (design §4.4, decision 8), on sanitised journal
OOM lines (systemd-oomd included), the GPU watchdog's kill lines, ``memory.events``,
boot-id changes and a stopped runner."""
from __future__ import annotations

import pathlib
import unittest

from ci_runners import outcome as oc

FX = pathlib.Path(__file__).resolve().parent / 'fixtures' / 'host'
UNIT = 'ci-brainspike-20261105-000734-4870627.scope'
OOMD = (FX / 'journal_oomd.txt').read_text()
UNIT_ONLY = (FX / 'journal_unit_only.txt').read_text()
KERNEL = (FX / 'journal_kernel.txt').read_text()
CLEAN = (FX / 'journal_clean.txt').read_text()
WATCHDOG = (FX / 'watchdog.log').read_text()
A8 = '00000000:A8:00.0'  # card 3 in smi_bus.csv


def E(**kw):
    kw.setdefault('rc', 1)
    return oc.Evidence(**kw)


def kind(**kw):
    return oc.classify(E(**kw)).kind


class Watchdog(unittest.TestCase):
    def test_bus_key(self):
        for s in (A8, 'a8', 'A8', '0000:a8:00.0', 'a8:00.0', ' 00A8 '):
            self.assertEqual(oc.bus_key(s), 'a8', s)
        self.assertEqual(oc.bus_key('00000000:00:00.0'), '0')

    def test_kill_lines_only(self):
        kills = oc.watchdog_kills(WATCHDOG)
        self.assertEqual([(k.bus, k.victims) for k in kills],
                         [('a8', (('user2', 222), ('user1', 12345))), ('9a', ()), ('b8', (('user1', 777),))])
        self.assertEqual([k.bus for k in oc.watchdog_kills(WATCHDOG, '2026-11-05 10:05:00')], ['9a', 'b8'])
        self.assertEqual([k.bus for k in oc.watchdog_kills(WATCHDOG, '', '2026-11-05 10:05:00')], ['a8', '9a'])
        self.assertEqual(oc.watchdog_kills(''), [])

    def test_ours_needs_our_card_and_a_recorded_pid(self):
        kills = oc.watchdog_kills(WATCHDOG)
        self.assertEqual([k.bus for k in oc.killed_ours(kills, [12345], [A8])], ['a8'])
        self.assertEqual(oc.killed_ours(kills, [12345], ['00000000:B8:00.0']), [])  # our pid, another card
        self.assertEqual(oc.killed_ours(kills, [777, 1], [A8]), [])  # our card, another user's pid
        self.assertEqual([k.bus for k in oc.killed_ours(kills, [777], ['b8', A8])], ['b8'])


class Oom(unittest.TestCase):
    def test_journal(self):
        self.assertTrue(oc.journal_oom(OOMD, UNIT))  # systemd-oomd and "Failed with result 'oom-kill'"
        self.assertTrue(oc.journal_oom(OOMD.splitlines()[1], UNIT))  # the oomd line alone
        self.assertTrue(oc.journal_oom(OOMD.splitlines()[2], UNIT))
        self.assertFalse(oc.journal_oom(OOMD, 'ci-other.scope'))  # another scope's lines
        self.assertTrue(oc.journal_oom(UNIT_ONLY, UNIT))  # `journalctl -u UNIT` lines name no scope
        self.assertTrue(oc.journal_oom(KERNEL, 'ci-p-A.scope'))  # the kernel's line, our memcg
        self.assertFalse(oc.journal_oom(KERNEL, 'p-A.scope'))  # a suffix of another scope's name
        self.assertFalse(oc.journal_oom(KERNEL, 'ci-p-B.scope'))
        self.assertTrue(oc.journal_oom(KERNEL))  # no unit asked: any
        self.assertFalse(oc.journal_oom(CLEAN, 'ci-p-A.scope'))
        self.assertFalse(oc.journal_oom('', UNIT))

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
        self.assertEqual(kind(rc=1, journal=OOMD, unit=UNIT), 'oom')
        self.assertEqual(kind(rc=1, journal=OOMD, unit='ci-other.scope'), 'red')
        self.assertEqual(kind(rc=143, stopped=True), 'stopped')
        self.assertEqual(kind(rc=None, boot_start='b1', boot_end='b2'), 'reboot')
        self.assertEqual(kind(rc=None, boot_start='b1', boot_end='b1'), 'lost')
        self.assertEqual(kind(rc=None, boot_start='', boot_end='b2'), 'lost')  # no first id: no evidence
        self.assertEqual(kind(rc=None, early_stop=True), 'thermal')
        self.assertEqual(kind(rc=137, watchdog=WATCHDOG, pids=(12345,), buses=(A8,), start='2026-11-05 09:00:00',
                              end='2026-11-05 10:30:00'), 'thermal')
        self.assertEqual(kind(rc=137, watchdog=WATCHDOG, pids=(12345,), buses=(A8,), start='2026-11-05 10:01:00'),
                         'red')  # the kill was before the part
        self.assertEqual(kind(rc=137, watchdog=WATCHDOG, pids=(12345,), buses=(A8,), end='2026-11-05 09:59:59'),
                         'red')  # and after it
        self.assertEqual(kind(rc=137, watchdog=WATCHDOG, pids=(222,), buses=('b8',)), 'red')  # not ours
        self.assertEqual(kind(rc=1, yield_marker=200.0, start_epoch=100.0), 'yielded')
        self.assertEqual(kind(rc=1, yield_marker=100.0, start_epoch=100.0), 'red')  # an old marker
        self.assertEqual(kind(rc=None, unavailable='no card fits'), 'unavailable')
        self.assertIn('a8', oc.classify(E(rc=1, watchdog=WATCHDOG, pids=(12345,), buses=(A8,))).reason)
        self.assertEqual(oc.classify(E(rc=None, unavailable='manifest: x')).reason, 'manifest: x')

    def test_precedence(self):
        """stopped, reboot, thermal, oom, yielded, unavailable, lost, red, pass: each
        piece of evidence removed in turn exposes the next."""
        ev = dict(rc=1, stopped=True, boot_start='b1', boot_end='b2', early_stop=True, oom_kills=1,
                  yield_marker=2.0, start_epoch=1.0, unavailable='x')
        order = []
        for drop in (('stopped',), ('boot_end',), ('early_stop',), ('oom_kills',), ('yield_marker',),
                     ('unavailable',)):
            order.append(kind(**ev))
            for k in drop:
                del ev[k]
        order.append(kind(**ev))
        order.append(kind(**{**ev, 'rc': None}))
        order.append(kind(**{**ev, 'rc': 0}))
        self.assertEqual(order, ['stopped', 'reboot', 'thermal', 'oom', 'yielded', 'unavailable', 'red', 'lost',
                                 'pass'])
        self.assertEqual(oc.PRECEDENCE, ('stopped', 'reboot', 'thermal', 'oom', 'yielded', 'unavailable', 'lost',
                                         'red', 'pass'))
        self.assertEqual(kind(rc=0, oom_kills=1), 'oom')  # design §4.4: oom ranks above pass

    def test_the_watchdog_ranks_with_thermal_above_oom(self):
        self.assertEqual(kind(rc=1, oom_kills=1, watchdog=WATCHDOG, pids=(12345,), buses=(A8,)), 'thermal')


class Hint(unittest.TestCase):
    def test_hints(self):
        H = oc.Hint
        table = {
            'pass': H('none'), 'red': H('none'), 'stopped': H('none'), 'disagreement': H('none'),
            'oom': H('retry', True, 'more-memory'), 'reboot': H('retry', False, 'same-host'),
            'yielded': H('fallback'), 'unavailable': H('fallback'), 'lost': H('retry', False, 'any'),
            'thermal': H('fallback'),
        }
        for k, want in table.items():
            self.assertEqual(oc.hint(k), want, k)
        self.assertEqual(set(table), set(oc.KINDS))
        self.assertEqual(oc.hint('thermal', cool_card=True), H('retry', where='cool-card'))
        self.assertEqual(oc.hint('reboot', remote=True), H('fallback'))  # h100: as thermal
        self.assertEqual(oc.hint('reboot', remote=True, cool_card=True), H('retry', where='cool-card'))

    def test_at_most_one_retry(self):
        for k in oc.KINDS:
            self.assertEqual(oc.hint(k, attempt=2), oc.Hint('none'), k)  # a second infrastructure end
            self.assertEqual(oc.hint(k, retries=0), oc.Hint('none'), k)
        self.assertEqual(oc.hint('oom', attempt=2, retries=2).next, 'retry')
        with self.assertRaises(ValueError):
            oc.hint('melted')


if __name__ == '__main__':
    unittest.main()
