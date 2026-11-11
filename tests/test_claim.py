"""The ``claim`` rule (design §4.1) and the status parser; only the runners' statuses count
(T11, decision 15.9)."""
from __future__ import annotations

import unittest

from ci_runners import claim

BOT = 'github-actions[bot]'
NOW = 1_800_000_000.0


def S(state, creator='alice', age_min=1.0):
    return claim.Status(state, creator, NOW - age_min * 60)


def run(statuses):
    c = claim.decide(statuses, bot=BOT)
    return c.run, c.reason


class Decide(unittest.TestCase):
    def test_no_status_runs(self):
        self.assertEqual(run([]), (True, 'no verdict on this head'))

    def test_green_by_the_runners_skips(self):
        self.assertEqual(run([S('success', BOT)]), (False, 'green'))
        self.assertEqual(run([S('success', 'GitHub-Actions[bot]')]), (False, 'green'))  # logins ignore case

    def test_a_runner_red_pending_or_error_runs(self):
        self.assertEqual(run([S('failure', BOT)]), (True, 'failure by the runners'))
        self.assertEqual(run([S('error', BOT)]), (True, 'error by the runners'))  # error is not a verdict
        self.assertEqual(run([S('pending', BOT)]), (True, 'pending by the runners'))  # "queued on the runners"

    def test_a_persons_status_neither_holds_nor_skips_a_head(self):
        """Decision 15.9: an old local run's claim (pending) or verdict (success, failure) by
        a person counts for nothing, however fresh; a forged green neither."""
        for state in ('pending', 'success', 'failure'):
            self.assertEqual(run([S(state, 'alice', age_min=0)]), (True, 'no verdict on this head'), state)
        self.assertEqual(run([S('pending', 'alice', 0), S('success', BOT, 5)]), (False, 'green'))  # under it, the runners' green
        self.assertEqual(run([S('success', 'alice', 0), S('failure', BOT, 5)]), (True, 'failure by the runners'))
        self.assertEqual(run([S('success', 'mallory', 0)])[0], True)
        self.assertEqual(run([S('success', 'github-actions')])[0], True)  # not the bot

    def test_only_the_newest_runner_status_decides(self):
        self.assertEqual(run([S('pending', BOT, age_min=1), S('success', BOT, age_min=5)])[0], True)
        self.assertEqual(run([S('success', BOT, age_min=1), S('failure', BOT, age_min=5)]), (False, 'green'))
        self.assertEqual(run([S('success', BOT, age_min=5), S('pending', BOT, age_min=1)])[0], True)  # order-free
        # equal times: the first listed (the API's newest) wins
        self.assertEqual(run([S('success', BOT, 2), S('pending', BOT, 2)])[0], False)
        self.assertEqual(run([S('pending', BOT, 2), S('success', BOT, 2)])[0], True)


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
