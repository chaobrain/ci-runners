"""``place``'s probes and the ``claim`` step (design §4.1, §4.2), with the host and the
GitHub API stood in for."""
from __future__ import annotations

import io
import json
import pathlib
import tempfile
import time
import unittest
import urllib.error

from ci_runners import github, inventory, place, probe

SHA = 'a' * 40
SMI = '0, 24576, 576, 41\n1, 24576, 20576, 83\n2, 24576, 0, 30\n'
UNITS = ('ci-runner@hb-light-1.service loaded active running x\n'
         'ci-runner@hb-part-2.service loaded active running x\n'
         'ci-runner@other-compile-1.service loaded active running x\n')


class Fake:
    def __init__(self, **out):
        self.out, self.calls = out, []

    def __call__(self, argv, **_kw):
        self.calls.append(argv)
        key = argv[0]
        rc, text = self.out.get(key, (0, ''))

        class R:
            returncode, stdout, stderr = rc, text, ''
        return R()


class Probe(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = pathlib.Path(self.tmp.name)
        (t / 'q').mkdir()
        for n in ('s.lock', 's.2.lock'):
            (t / n).write_text('')
        self.inv = inventory.load(f'''schema = 1
this = "hb"
[hosts.hb]
ssh = "alias-x"
account = "acct-x"
home = "{t}"
data = "{t}"
trusted = true
cpu_slowdown = 1.5
slots = 2
memory_max = "120G"
disk_min_gib = 1
gpus = [0, 1]
max_temp_c = 80
pools = {{ light = 1, part = 2 }}
files = {{ host_lock = "{t}/h.lock", slot_lock = "{t}/s.lock", slot_queue = "{t}/q" }}
[hosts.hb.proxy]
client = "/bin/true"
config = "{t}"
nodes = ["n"]
http_port = 1
socks_port = 2
no_proxy = "localhost"
[hosts.hc]
ssh = "alias-y"
account = "acct-y"
home = "/h"
data = "/d"
trusted = true
cpu_slowdown = 1.0
slots = 2
memory_max = "80G"
disk_min_gib = 1
gpus = []
max_temp_c = 80
pools = {{ part = 1 }}
files = {{ host_lock = "/tmp/h.lock", slot_lock = "/tmp/s.lock", slot_queue = "/tmp/s.queue" }}
''')

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_local_probe(self):
        p = probe.local(self.inv, run=Fake(systemctl=(0, UNITS), **{'nvidia-smi': (0, SMI)}), http_ok=lambda *a, **k: True)
        self.assertEqual(p.runners, frozenset({'light', 'part'}))  # another role's runner is not ours
        self.assertEqual([(c.index, c.free_mib, c.temp_c) for c in p.cards], [(0, 24000, 41.0), (1, 4000, 83.0)])
        self.assertEqual((p.reachable, p.proxy, p.slots, p.busy, p.slowdown), (True, True, 2, 0, 1.5))
        down = probe.local(self.inv, run=Fake(systemctl=(0, UNITS), **{'nvidia-smi': (9, SMI)}),
                           http_ok=lambda *a, **k: False)
        self.assertEqual((down.proxy, down.cards), (False, ()))
        self.assertEqual(place.host_fault(down), 'proxy down')  # the host is skipped

    def test_json_round_trip_and_fail_closed(self):
        p = probe.local(self.inv, run=Fake(systemctl=(0, UNITS), **{'nvidia-smi': (0, SMI)}), http_ok=lambda *a, **k: True)
        self.assertEqual(probe.from_json(json.loads(json.dumps(probe.to_json(p)))), p)
        d = probe.to_json(p)
        del d['reserved']
        self.assertTrue(probe.from_json(d).reserved)  # a field it does not state fails
        self.assertEqual(place.host_fault(probe.from_json({'reachable': 'yes', 'proxy': True})), 'unreachable')
        self.assertEqual(place.host_fault(probe.from_json({'reachable': True, 'proxy': True})), 'reserved')
        self.assertEqual(probe.from_json([1]), place.Probe())

    def test_remote_probe_over_ssh(self):
        good = json.dumps(probe.to_json(place.Probe(reachable=True, runners=frozenset({'part'}), proxy=True, slots=2)))
        fake = Fake(ssh=(0, good))
        self.assertTrue(probe.remote(self.inv, 'hc', run=fake).reachable)
        self.assertIn('BatchMode=yes', fake.calls[0])
        self.assertIn('alias-y', fake.calls[0])
        self.assertFalse(probe.remote(self.inv, 'hc', run=Fake(ssh=(255, ''))).reachable)
        self.assertFalse(probe.remote(self.inv, 'hc', run=Fake(ssh=(1, good))).reachable)  # a failed probe's output
        self.assertFalse(probe.remote(self.inv, 'hc', run=Fake(ssh=(0, 'not json'))).reachable)

    def test_a_slow_probe_skips_its_host_within_the_bound(self):
        ok = place.Probe(reachable=True, runners=frozenset({'part'}), proxy=True, slots=2)

        def slow(_r):
            time.sleep(3)
            return ok
        t0 = time.monotonic()
        got = probe.probe_all(self.inv, local_probe=lambda _r: ok, remote_probe=slow, bound=0.5)
        self.assertLess(time.monotonic() - t0, 2.5)
        self.assertEqual((got['hb'], got['hc']), (ok, place.Probe()))

    def test_runner_pools(self):
        self.assertEqual(probe.runner_pools('ci-runner@hb-compile-1.service a\nnoise\n', 'hb'), frozenset({'compile'}))
        self.assertEqual(probe.runner_pools('ci-runner@hbx-part-1.service a\n', 'hb'), frozenset())


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
                                actors=frozenset({'alice'}), bot='github-actions[bot]', hosted=False, call=call,
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
                             actors=frozenset({'alice'}), bot='github-actions[bot]', hosted=False, call=call,
                             now=lambda: float(next(clock)) + time.time(), sleep=lambda s: None)
        self.assertEqual((c.run, c.reason), (False, 'claimed by a local run'))
        self.assertNotIn('POST', [m for m, *_ in calls])

    def test_bad_inputs(self):
        call, _ = self.api([])
        for env, sha in (({**self.ENV, 'GITHUB_TOKEN': ''}, SHA), (self.ENV, 'abc'),
                         ({**self.ENV, 'GITHUB_REPOSITORY': 'x/../y z'}, SHA)):
            with self.assertRaises(ValueError):
                github.run_claim(env, context='p/ci', sha=sha, wait_seconds=0, lapse_minutes=45, description='q',
                                 actors=frozenset(), bot='b[bot]', hosted=True, call=call)

    def test_api_retries_server_errors_only(self):
        tries = []

        def opener(req, timeout):
            tries.append(timeout)
            raise urllib.error.HTTPError(req.full_url, 502 if len(tries) < 3 else 200, 'x', {}, io.BytesIO())
        with self.assertRaises(github.ApiError):
            github.api('GET', 'https://x/y', 't', opener=opener, pause=0)
        self.assertEqual(tries, [30, 30, 30])
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


if __name__ == '__main__':
    unittest.main()
