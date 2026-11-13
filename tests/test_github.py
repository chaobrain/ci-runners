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

    def claim(self, call):
        return github.run_claim(self.ENV, context='p/ci', sha=SHA, description='queued', bot='github-actions[bot]',
                                call=call)

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

    def test_a_persons_pending_or_green_does_not_hold_the_head(self):
        """Decision 15.9: an old-style local run's claim or verdict by a person is ignored;
        the runners take the head and post their own pending, in one read."""
        for state in ('pending', 'success'):
            call, calls = self.api([self.status(state, 'alice')])
            c = self.claim(call)
            self.assertEqual((c.run, c.reason), (True, 'no verdict on this head'), state)
            self.assertEqual([m for m, *_ in calls], ['GET', 'POST'], state)
            self.assertEqual(calls[-1][2]['state'], 'pending')

    def test_bad_inputs(self):
        call, _ = self.api([])
        for env, sha in (({**self.ENV, 'GITHUB_TOKEN': ''}, SHA), (self.ENV, 'abc'),
                         ({**self.ENV, 'GITHUB_REPOSITORY': 'x/../y z'}, SHA)):
            with self.assertRaises(ValueError):
                github.run_claim(env, context='p/ci', sha=sha, description='q', bot='b[bot]', call=call)

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
        pages = {1: [self.status('pending', 'bob')] * 100, 2: [self.status('success', 'github-actions[bot]')]}
        urls = []

        def call(method, url, token, body=None):
            urls.append(url)
            return pages.get(int(url.rsplit('page=', 1)[1]), []) if method == 'GET' else {}
        c = self.claim(call)
        self.assertEqual((c.run, c.reason), (False, 'green'))  # the runners' green on page 2 counts
        self.assertEqual(len(urls), 2)
        pages[1] = [self.status('success', 'github-actions[bot]')] + [self.status('pending', 'bob')] * 99
        pages[2] = [self.status('failure', 'github-actions[bot]', age=60)]
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

    @staticmethod
    def status(state, login):
        return [{'context': 'p/ci', 'state': state, 'creator': {'login': login},
                 'created_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}]

    def test_an_api_error_exits_2_without_a_verdict(self):
        def broken(*a, **k):
            raise github.ApiError('GET /x: HTTP 502')
        rc, out, err = self.main(broken)
        self.assertEqual((rc, out), (2, ''))
        self.assertIn('HTTP 502', err)
        self.assertNotIn('t0k', err)

    def test_a_skip_writes_run_false(self):
        call, posts = self.answer(self.status('success', 'github-actions[bot]'))
        self.assertEqual(self.main(call)[:2], (0, 'run=false\nreason=green\n'))
        self.assertEqual(posts, [])

    def test_a_persons_pending_writes_run_true(self):
        call, posts = self.answer(self.status('pending', 'alice'))  # alice is the policy's actor: no matter
        self.assertEqual(self.main(call)[:2], (0, 'run=true\nreason=no verdict on this head\n'))
        self.assertEqual([b['state'] for b in posts], ['pending'])

    def test_the_policy_names_the_runners_identity(self):
        (self.etc / 'policy.toml').write_text('schema = 2\nbot = "ci-app[bot]"\nactors = ["alice"]\n')
        call, posts = self.answer(self.status('success', 'github-actions[bot]'))
        self.assertEqual(self.main(call)[:2], (0, 'run=true\nreason=no verdict on this head\n'))
        self.out.write_text('')
        call, posts = self.answer(self.status('success', 'ci-app[bot]'))
        self.assertEqual(self.main(call)[:2], (0, 'run=false\nreason=green\n'))

    def test_a_run_writes_run_true_and_posts(self):
        call, posts = self.answer([])
        self.assertEqual(self.main(call)[:2], (0, 'run=true\nreason=no verdict on this head\n'))
        self.assertEqual([b['state'] for b in posts], ['pending'])

    def test_the_policy_is_required(self):
        call, _ = self.answer(self.status('success', 'github-actions[bot]'))
        self.assertEqual(self.main(call, etc=pathlib.Path(self.tmp.name) / 'absent')[0], 2)


#: a GitHub URL at the limit: the longest owner (39) and repository (100) names, a long run id
LONG_URL = f'https://api.github.com/repos/{"o" * 39}/{"r" * 100}/actions/runs/{"9" * 20}/artifacts'


def whole_urls(text: str, source: str) -> bool:
    """Every word of ``text`` that holds ``://`` is a whole word of ``source``."""
    words = source.split()
    return all(w in words for w in text.split() if '://' in w)


class StatusText(unittest.TestCase):
    """Issue #250: a status description is one line of at most 140 characters, its URLs
    dropped first when it is longer, never cut inside one."""

    def test_a_short_description_is_kept_on_one_line(self):
        self.assertEqual(github.status_text('passed: a, b'), 'passed: a, b')
        self.assertEqual(github.status_text('a\nb  c'), 'a b c')
        self.assertEqual(github.status_text(f'see {LONG_URL[:120]}'), f'see {LONG_URL[:120]}')  # it fits: kept
        at = 'x' * 140
        self.assertEqual(github.status_text(at), at)  # at the limit: kept

    def test_one_past_the_limit_is_cut_with_dots(self):
        got = github.status_text('word ' * 28 + 'w')  # 141
        self.assertLessEqual(len(got), github.STATUS_MAX)
        self.assertTrue(got.endswith('word...'), got)  # at a word's end
        self.assertEqual(len(github.status_text('x' * 141)), github.STATUS_MAX)  # one long word: cut where room ends
        self.assertEqual(github.status_text('a ' + 'x' * 200), 'a ' + 'x' * 135 + '...')  # not 'a...'

    def test_a_url_is_dropped_before_anything_else_is_cut(self):
        text = f'verdict: artifacts not listed (GET {LONG_URL}: HTTP 403); the job needs actions: read'
        got = github.status_text(text)
        self.assertEqual(got, 'verdict: artifacts not listed (GET HTTP 403); the job needs actions: read')
        self.assertNotIn('api.github.com', got)
        words = ' '.join(['x'] * 60)
        self.assertEqual(github.status_text(f'{words} ({LONG_URL})'), words)  # the word that holds it goes whole

    def test_a_cut_never_falls_inside_a_url_at_any_length(self):
        for n in range(0, 200, 3):
            for text in (f'cause HTTP 502 {"w" * n} {LONG_URL} and more words after it',
                         f'{"a " * n}{LONG_URL}',
                         f'{LONG_URL} {"b" * n}',
                         f'x {"c " * n} (http://a.example/{"p" * n}) tail'):
                got = github.status_text(text)
                self.assertLessEqual(len(got), github.STATUS_MAX, text)
                self.assertNotIn('\n', got)
                self.assertTrue(whole_urls(got, text), got)
                self.assertNotIn('//api', got.replace(LONG_URL, ''))  # no piece of a URL is left
        self.assertTrue(github.status_text('cause HTTP 502 ' + 'w ' * 80 + LONG_URL).startswith('cause HTTP 502 '))

    def test_an_api_errors_cause_is_its_short_end(self):
        self.assertEqual(github.ApiError(f'GET {LONG_URL}: HTTP 403').cause, 'HTTP 403')
        self.assertEqual(github.ApiError('GET x: whatever', 'HTTP 502').cause, 'HTTP 502')
        self.assertEqual(github.ApiError('no ci-plan-files artifact listed').cause, 'no ci-plan-files artifact listed')
        self.assertEqual(str(github.ApiError(f'GET {LONG_URL}: HTTP 403')), f'GET {LONG_URL}: HTTP 403')  # kept whole

    def test_the_api_raises_its_cause(self):
        def opener(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 403, 'x', {}, io.BytesIO())
        with self.assertRaises(github.ApiError) as cm:
            github.api('GET', LONG_URL + '?page=1', 't', opener=opener, pause=0)
        self.assertEqual((cm.exception.cause, str(cm.exception)), ('HTTP 403', f'GET {LONG_URL}: HTTP 403'))

        def slow(req, timeout):
            raise TimeoutError()
        with self.assertRaises(github.ApiError) as cm:
            github.api('GET', LONG_URL, 't', opener=slow, pause=0, tries=1)
        self.assertEqual(cm.exception.cause, 'TimeoutError')

    def test_the_claims_pending_description_fits(self):
        posted = []

        def call(method, url, token, body=None):
            if method == 'POST':
                posted.append(body)
            return []
        env = {'GITHUB_REPOSITORY': 'o/r', 'GITHUB_TOKEN': 't'}
        github.run_claim(env, context='c/ci', sha='a' * 40, description=f'queued {LONG_URL} ' + 'y ' * 80, bot='b',
                         call=call)
        self.assertLessEqual(len(posted[0]['description']), github.STATUS_MAX)
        self.assertTrue(whole_urls(posted[0]['description'], LONG_URL))


if __name__ == '__main__':
    unittest.main()
