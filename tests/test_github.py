"""The ``claim`` step (design §4.1), with the GitHub API stood in for."""
from __future__ import annotations

import contextlib
import io
import json
import pathlib
import tempfile
import time
import unittest
import urllib.error
from unittest import mock

from ci_runners import github

SHA = 'a' * 40


class Claim(unittest.TestCase):
    ENV = {'GITHUB_REPOSITORY': 'org/repo', 'GITHUB_TOKEN': 't0k', 'GITHUB_RUN_ID': '5'}

    def api(self, statuses):
        calls = []

        def call(method, url, token, body=None):
            calls.append((method, url, body))
            return statuses() if callable(statuses) else statuses if method == 'GET' else {}
        return call, calls

    def status(self, state, login, age=0.0):
        t = time.gmtime(time.time() - age)
        return {'context': 'p/ci', 'state': state, 'creator': {'login': login},
                'created_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', t)}

    def claim(self, call, **kw):
        kw.setdefault('wait_seconds', 0)
        return github.run_claim(self.ENV, context='p/ci', sha=SHA, lapse_minutes=45, description='queued',
                                actors=frozenset({'alice'}), bot='github-actions[bot]', call=call,
                                sleep=lambda s: None, **kw)

    def test_run_posts_pending(self):
        call, calls = self.api([])
        self.assertTrue(self.claim(call).run)
        self.assertEqual(calls[-1][0], 'POST')
        self.assertTrue(calls[-1][1].endswith(f'/repos/org/repo/statuses/{SHA}'))
        self.assertEqual((calls[-1][2]['state'], calls[-1][2]['context']), ('pending', 'p/ci'))

    def test_skip_posts_nothing(self):
        call, calls = self.api([self.status('success', 'github-actions[bot]')])
        c = self.claim(call)
        self.assertEqual((c.run, c.reason, [m for m, *_ in calls]), (False, 'green', ['GET']))

    def test_the_wait_lets_a_local_claim_appear(self):
        seen = []

        def statuses():
            seen.append(1)
            return [self.status('pending', 'alice')] if len(seen) > 2 else []
        call, calls = self.api(statuses)
        clock = iter(range(0, 1000, 5))
        c = github.run_claim(self.ENV, context='p/ci', sha=SHA, wait_seconds=60, lapse_minutes=45, description='q',
                             actors=frozenset({'alice'}), bot='github-actions[bot]', call=call,
                             now=lambda: float(next(clock)) + time.time(), sleep=lambda s: None)
        self.assertEqual((c.run, c.reason), (False, 'claimed by a local run'))
        self.assertNotIn('POST', [m for m, *_ in calls])

    def test_bad_inputs(self):
        call, _ = self.api([])
        for env, sha in (({**self.ENV, 'GITHUB_TOKEN': ''}, SHA), (self.ENV, 'abc'),
                         ({**self.ENV, 'GITHUB_REPOSITORY': 'x/../y z'}, SHA)):
            with self.assertRaises(ValueError):
                github.run_claim(env, context='p/ci', sha=sha, wait_seconds=0, lapse_minutes=45, description='q',
                                 actors=frozenset(), bot='b[bot]', call=call)

    def test_api_retries_server_errors_only(self):
        tries = []

        def opener(req, timeout):
            tries.append(timeout)
            raise urllib.error.HTTPError(req.full_url, 502 if len(tries) < 3 else 200, 'x', {}, io.BytesIO())
        with self.assertRaises(github.ApiError):
            github.api('GET', 'https://x/y', 't', opener=opener, pause=0)
        self.assertEqual(tries, [30, 30, 30])
        tries.clear()

        def limited(req, timeout):
            tries.append(timeout)
            raise urllib.error.HTTPError(req.full_url, 429, 'x', {}, io.BytesIO())
        with self.assertRaises(github.ApiError):
            github.api('GET', 'https://x/y', 't', opener=limited, pause=0)
        self.assertEqual(len(tries), 3)  # a rate limit is retried
        tries.clear()

        def refused(req, timeout):
            tries.append(timeout)
            raise urllib.error.HTTPError(req.full_url, 403, 'x', {}, io.BytesIO())
        with self.assertRaises(github.ApiError):
            github.api('GET', 'https://x/y', 't', opener=refused, pause=0)
        self.assertEqual(len(tries), 1)

        class R(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        req_seen = []

        def ok(req, timeout):
            req_seen.append(req)
            return R(b'[1]')
        self.assertEqual(github.api('POST', 'https://x/y', 'tk', {'a': 1}, opener=ok), [1])
        self.assertEqual(req_seen[0].get_header('Authorization'), 'Bearer tk')
        self.assertEqual(json.loads(req_seen[0].data), {'a': 1})

    def test_statuses_are_read_page_by_page(self):
        pages = {1: [self.status('pending', 'bob')] * 100, 2: [self.status('pending', 'alice')]}
        urls = []

        def call(method, url, token, body=None):
            urls.append(url)
            return pages.get(int(url.rsplit('page=', 1)[1]), []) if method == 'GET' else {}
        c = self.claim(call)
        self.assertEqual((c.run, c.reason), (False, 'claimed by a local run'))  # the claim on page 2 counts
        self.assertEqual(len(urls), 2)
        pages[1] = [self.status('success', 'github-actions[bot]')] + [self.status('pending', 'bob')] * 99
        pages[2] = [self.status('pending', 'alice', age=60)]
        self.assertEqual(self.claim(call).reason, 'green')  # page 1, the newest, counts too
        with self.assertRaises(github.ApiError):
            github.statuses(lambda *a, **k: {'message': 'x'}, 'https://x/y?per_page=100', 't')


class ClaimMain(unittest.TestCase):
    """``ci-run claim`` end to end: the outputs a workflow reads, and exit 2 on failure."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = pathlib.Path(self.tmp.name)
        self.etc = t / 'etc'
        self.etc.mkdir()
        (self.etc / 'policy.toml').write_text('schema = 2\nactors = ["alice"]\n')
        self.out = t / 'out'
        self.out.write_text('')
        self.env = {'GITHUB_REPOSITORY': 'org/repo', 'GITHUB_TOKEN': 't0k', 'GITHUB_RUN_ID': '5',
                    'GITHUB_SHA': SHA, 'GITHUB_OUTPUT': str(self.out)}

    def tearDown(self):
        self.tmp.cleanup()

    def main(self, call, *extra, etc=None):
        err = io.StringIO()
        with mock.patch.dict('os.environ', self.env, clear=True), contextlib.redirect_stderr(err):
            rc = github.main(['--context', 'p/ci', *extra], str(etc or self.etc), call=call)
        return rc, self.out.read_text(), err.getvalue()

    @staticmethod
    def answer(statuses):
        posts = []

        def call(method, url, token, body=None):
            if method == 'POST':
                posts.append(body)
                return {}
            return statuses
        return call, posts

    def local_pending(self):
        return [{'context': 'p/ci', 'state': 'pending', 'creator': {'login': 'alice'},
                 'created_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}]

    def test_an_api_error_exits_2_without_a_verdict(self):
        def broken(*a, **k):
            raise github.ApiError('GET /x: HTTP 502')
        rc, out, err = self.main(broken)
        self.assertEqual((rc, out), (2, ''))
        self.assertIn('HTTP 502', err)
        self.assertNotIn('t0k', err)

    def test_a_skip_writes_run_false(self):
        call, posts = self.answer(self.local_pending())
        self.assertEqual(self.main(call)[:2], (0, 'run=false\nreason=claimed by a local run\n'))
        self.assertEqual(posts, [])

    def test_a_run_writes_run_true_and_posts(self):
        call, posts = self.answer([])
        self.assertEqual(self.main(call)[:2], (0, 'run=true\nreason=no verdict on this head\n'))
        self.assertEqual([b['state'] for b in posts], ['pending'])

    def test_the_policy_is_required(self):
        call, _ = self.answer(self.local_pending())
        self.assertEqual(self.main(call, etc=pathlib.Path(self.tmp.name) / 'absent')[0], 2)


if __name__ == '__main__':
    unittest.main()
