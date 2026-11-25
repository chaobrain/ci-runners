"""Cluster cache admission, cold/hot execution and the restricted tunnel builder."""
import json
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from ci_runners import environment_cache, slurmcache, slurmgate, slurmpart, slurmsite
from tests.test_slurmgate import EXAMPLE, KEY, ROOT, request


def cached(**changes):
    grant = dict(allocation_schema=1, allocation_id='a', task_id='t', runner_tag='slurm0', host='controller',
                 device='slurm', backend='slurm', cpu_groups=1, cores=4, workers=1, RAM='4G', minutes=1)
    recipe = dict(format=1, repository='o/r', revision='1' * 40, files={}, commits={},
                  spec=dict(schema=1, python='3.12', variables={'PROJECT_MODE': 'cache'}))
    result = request(allocation=grant, cores=4, workers=1, minutes=1, memory='4G', environment_recipe=recipe)
    result.update(changes)
    return result


class Cache(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)/'ci-runners'
        for name in ('etc', 'run', 'envs', 'conda/bin'):
            (self.base/name).mkdir(parents=True)
        self.site = slurmsite.load(EXAMPLE.replace('egress = "tunnel"', 'egress = "direct"'))
        self.account = slurmsite.account_entry(self.site, 'acct1')
        self.account.update(login_python=sys.executable)
        self.account_file = self.base/'etc/account.json'
        self.account_file.write_text(json.dumps(self.account))
        self.req = cached(command='echo "$CONDA_PREFIX $PROJECT_MODE $CI_WORKERS" > "$CI_PART_DIR/results/seen"')
        self.conda = self.base/'conda/bin/conda'
        self.conda.write_text('#!/bin/sh\nset -eu\nwhile test "$1" != --prefix; do shift; done\n'
                              'mkdir -p "$2/bin"\nln -s '+shlex.quote(sys.executable)+' "$2/bin/python"\n')
        self.conda.chmod(0o750)

    def submit(self, req=None):
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, '5\n', ''))
        slurmgate.submit(str(self.base), str(ROOT), KEY, json.dumps(req or self.req).encode(), run=run)
        return self.base/'work'/KEY

    def run_job(self, d):
        # The fixture has no real Slurm cgroup. Only fake the reading of its
        # controller; use the real supervisor, cache, child Python, and shell.
        loader = ('import sys; sys.path.insert(0,sys.argv.pop(1)); '
                  'from parallel import task_execution as t; t.slurm_cpu_width=lambda:4; '
                  'from ci_runners.slurmgate import job; sys.exit(job(sys.argv[1]))')
        return subprocess.run([sys.executable, '-I', '-c', loader, str(ROOT), str(d)], cwd='/',
                              capture_output=True, text=True, timeout=12)

    def test_cold_submit_uses_target_variant_and_published_prefix_survives_absent_conda(self):
        d = self.submit()
        self.assertFalse(slurmcache.probe(self.base, KEY, json.dumps(self.req).encode())['ready'])
        for label in ('cold', 'warm'):
            with self.subTest(label=label):
                result = self.run_job(d)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                prefix, variable, workers = (d/'part/results/seen').read_text().split()
                expected = environment_cache.lookup(self.base/'envs/cache', self.req['environment_recipe'], variant='cpu')
                self.assertEqual((prefix, variable, workers), (expected, 'cache', '1'))
                self.assertTrue(Path(prefix).is_relative_to(self.base/'envs/cache'))
                self.assertTrue(slurmcache.probe(self.base, KEY, json.dumps(self.req).encode())['ready'])
            self.conda.unlink(missing_ok=True)

    def test_build_failure_reports_preparation_and_does_not_run_the_command(self):
        self.conda.unlink()
        d = self.submit()
        result = self.run_job(d)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('environment preparation failed: environment build',
                      json.loads((d/'record/exit.json').read_text())['unavailable'])
        self.assertFalse((d/'part/results/seen').exists())
        self.assertFalse(slurmcache.probe(self.base, KEY, json.dumps(self.req).encode())['ready'])

    def test_download_uses_site_proxy_and_never_inherits_the_test_proxy(self):
        self.account.update(cache_egress='proxy', cache_proxy='http://site-proxy.invalid:8080')
        self.account_file.write_text(json.dumps(self.account))
        self.req['variables']['HTTPS_PROXY'] = 'http://workflow.invalid'
        seen = self.base/'builder-proxy'
        self.conda.write_text(self.conda.read_text().replace('set -eu', f'set -eu\necho "$HTTPS_PROXY" > {shlex.quote(str(seen))}'))
        result = self.run_job(self.submit())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(seen.read_text().strip(), self.account['cache_proxy'])

    def test_gate_validates_cpu_grant_project_repository_key_and_recipe(self):
        req = cached()
        del req['allocation']
        cases = [req, cached(run_url='https://github.com/o/another/actions/runs/7'),
                 cached(environment_recipe={}), cached(target='a40', cards=1)]
        for bad in cases:
            with self.subTest(request=bad), self.assertRaises((slurmgate.GateError, ValueError)):
                self.submit(bad)
        self.account['cache_projects']['cpu'] = ['other-project']
        with self.assertRaisesRegex(slurmgate.GateError, 'not enabled'):
            slurmgate.check_request(cached(), self.account, self.base)
        with self.assertRaisesRegex(slurmgate.GateError, 'job key'):
            slurmcache.probe(self.base, 'o/else/7-1/p1-1', json.dumps(cached()).encode())

    def test_tunnel_warm_needs_no_build_connection_and_cold_must_use_the_same_account(self):
        site = slurmsite.load(EXAMPLE)
        g = mock.Mock(spec=slurmpart.Gate)
        g.run = mock.Mock()
        g.site, g.alias, g.host, g.ssh, g.ssh_config = site, 'acct1', 'submit', '/usr/bin/ssh', '/nonexistent'
        r = cached()
        g.json.return_value = {'ready': True}
        slurmcache.prepare_tunnel(g, KEY, r, None, lambda: False)
        g.run.assert_not_called()
        g.json.return_value = {'ready': False}
        with self.assertRaisesRegex(slurmpart.Refused, 'cache_build_via'):
            slurmcache.prepare_tunnel(g, KEY, r, None, lambda: False)
        site.accounts[g.alias].a['cache_build_via'] = 'builder'
        endpoint = subprocess.CompletedProcess([], 0, b'hostname login\nuser a\nport 22\n', b'')
        wrong = subprocess.CompletedProcess([], 0, b'hostname login\nuser b\nport 22\n', b'')
        g.run.side_effect = [endpoint, wrong]
        with self.assertRaisesRegex(slurmpart.Refused, 'SSH identity'):
            slurmcache.prepare_tunnel(g, KEY, r, 5656, lambda: False)
        g.run.side_effect = [endpoint, endpoint]
        g.json.side_effect = [{'ready': False}, {'ready': True}]
        with mock.patch.object(slurmcache, '_wait_for_builder', return_value=0) as wait:
            slurmcache.prepare_tunnel(g, KEY, r, 5656, lambda: False)
        argv, payload, *_ = wait.call_args.args
        self.assertTrue(argv[-1].endswith('/releases/hook/bin/ci-slurm-cache-build'))
        self.assertEqual(argv[-2], 'builder')
        self.assertNotIn(r['command'], argv)
        self.assertEqual(payload['request'], r)

    def test_tunnel_helper_prepares_only_the_recipe_and_requires_the_named_site_and_account(self):
        self.account['cache_egress'] = 'tunnel'
        self.account_file.write_text(json.dumps(self.account))
        req = cached(command='touch '+shlex.quote(str(self.base/'maintenance-ran-test')))
        nonce = '1'*16
        payload = dict(site=self.account['site'], account='wrong', key=KEY, request=req, nonce=nonce)
        with self.assertRaisesRegex(slurmgate.GateError, 'identity'):
            slurmcache.builder(str(self.base), json.dumps(payload).encode())
        payload['account'] = self.account['account']
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.bind(str(self.base/'run'/f'ec-{nonce}.sock'))
            sock.listen(1)
            # Execute in its own process: part.supervise handles signals and creates a session.
            launcher = ('import sys; sys.path.insert(0,sys.argv.pop(1)); from ci_runners.slurmcache import builder; '
                        'sys.exit(builder(sys.argv[1], sys.stdin.buffer.read()))')
            process = subprocess.run([sys.executable,'-I','-c',launcher,str(ROOT), str(self.base)],
                                     input=json.dumps(payload), text=True, capture_output=True, timeout=12)
        self.assertEqual(process.returncode, 0, process.stdout+process.stderr)
        self.assertFalse((self.base/'maintenance-ran-test').exists())
        self.assertIsNotNone(environment_cache.lookup(self.base/'envs/cache', req['environment_recipe'], variant='cpu'))
        self.assertFalse((self.base/'run'/f'ec-{nonce}.sock').exists())
        # Compute side has neither access to the tunnel nor a working installer.
        # It must find the login-side prefix for the same target variant and run the test.
        self.conda.unlink()
        req['command'] = self.req['command']
        d = self.submit(req)
        result = self.run_job(d)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn('/envs/cache/', (d/'part/results/seen').read_text())


if __name__ == '__main__':
    unittest.main()
