"""The ``claim`` rule (design §4.1) and the status parser; forged statuses do not count
(T11)."""
from __future__ import annotations

import unittest

from ci_runners import claim

BOT = 'github-actions[bot]'
NOW = 1_800_000_000.0


def S(state, creator='alice', age_min=1.0):
    return claim.Status(state, creator, NOW - age_min * 60)


def run(statuses, lapse=45):
    c = claim.decide(statuses, actors=['Alice'], bot=BOT, now=NOW, lapse_minutes=lapse)
    return c.run, c.reason


class Decide(unittest.TestCase):
    def test_no_status_runs(self):
        self.assertEqual(run([]), (True, 'no verdict on this head'))

    def test_green_by_the_bot_or_a_member_skips(self):
        self.assertEqual(run([S('success', BOT)]), (False, 'green'))
        self.assertEqual(run([S('success', 'ALICE')]), (False, 'green'))

    def test_a_local_red_skips_but_a_runner_red_runs(self):
        self.assertEqual(run([S('failure')]), (False, 'red in a local run'))
        self.assertEqual(run([S('failure', BOT)])[0], True)
        self.assertEqual(run([S('error')])[0], True)  # error is not a verdict

    def test_a_live_local_claim_skips_until_it_lapses(self):
        self.assertEqual(run([S('pending', age_min=44.9)]), (False, 'claimed by a local run'))
        self.assertEqual(run([S('pending', age_min=45)]), (True, 'a local claim lapsed'))
        self.assertEqual(run([S('pending', age_min=10)], lapse=5), (True, 'a local claim lapsed'))
        self.assertEqual(run([S('pending', BOT, age_min=1)])[0], True)  # "queued on the runners"

    def test_only_the_newest_counted_status_decides(self):
        self.assertEqual(run([S('pending', age_min=1), S('success', BOT, age_min=5)]),
                         (False, 'claimed by a local run'))
        self.assertEqual(run([S('failure', BOT, age_min=1), S('success', 'alice', age_min=5)])[0], True)
        self.assertEqual(run([S('success', BOT, age_min=5), S('pending', BOT, age_min=1)])[0], True)  # order-free
        # equal times: the first listed (the API's newest) wins
        self.assertEqual(run([S('success', BOT, 2), S('pending', BOT, 2)])[0], False)
        self.assertEqual(run([S('pending', BOT, 2), S('success', BOT, 2)])[0], True)

    def test_forged_statuses_do_not_count(self):
        self.assertEqual(run([S('success', 'mallory')]), (True, 'no verdict on this head'))
        self.assertEqual(run([S('success', 'mallory', 0), S('failure', BOT, 5)])[0], True)
        self.assertEqual(run([S('success', 'github-actions')])[0], True)  # not the bot

    def test_the_bot_listed_as_an_actor_is_still_the_bot(self):
        c = claim.decide([S('failure', BOT)], actors=['alice', BOT], bot=BOT, now=NOW)
        self.assertTrue(c.run)


class Parse(unittest.TestCase):
    API = [
        {'state': 'pending', 'context': 'p/ci', 'creator': {'login': BOT}, 'created_at': '2026-11-05T10:00:00Z',
         'description': 'queued on the runners'},
        {'state': 'success', 'context': 'other/ci', 'creator': {'login': BOT}, 'created_at': '2026-11-05T09:00:00Z'},
        {'state': 'success', 'context': 'p/ci', 'creator': None, 'created_at': '2026-11-05T09:00:00Z'},
        {'state': 'success', 'context': 'p/ci', 'creator': {'login': 'alice'}, 'created_at': 'yesterday'},
        {'state': 'success', 'context': 'p/ci', 'creator': {'login': 'alice'}, 'created_at': '2026-11-05T09:00:00'},
        {'state': 'failure', 'context': 'p/ci', 'creator': {'login': 'alice'}, 'created_at': '2026-11-05T08:00:00+00:00',
         'description': None},
        'garbage',
    ]

    def test_parse(self):
        got = claim.parse_statuses(self.API, 'p/ci')
        self.assertEqual([(s.state, s.creator, s.description) for s in got],
                         [('pending', BOT, 'queued on the runners'), ('failure', 'alice', '')])
        self.assertEqual(got[0].time - got[1].time, 7200)
        self.assertEqual(claim.parse_statuses({'message': 'Not Found'}, 'p/ci'), [])


if __name__ == '__main__':
    unittest.main()
