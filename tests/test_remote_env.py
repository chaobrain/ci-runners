"""The remote env allowlist (design §5.4; T8): only allowlisted names reach a remote,
and GitHub, Actions and runner variables, the agent socket and secret-like names never
do, even when a project lists them."""
from __future__ import annotations

import unittest

from ci_runners import remote

FIXED = {'PATH': '/usr/bin:/bin', 'HOME': '/remote/home', 'LANG': 'C.UTF-8'}
JOB = {
    'PATH': '/job/path', 'HOME': '/job/home', 'LANG': 'xx', 'CUDA_VISIBLE_DEVICES': '3',
    'CI_PROJECT': 'p', 'CI_RESUME': '1', 'CI_GH_TOKEN': 'x', 'BRAINSPIKE_HOST': 'h',
    'GITHUB_TOKEN': 'ghs', 'GITHUB_SHA': 'a', 'ACTIONS_RUNTIME_TOKEN': 't', 'ACTIONS_ID_TOKEN_REQUEST_URL': 'u',
    'RUNNER_TEMP': '/t', 'SSH_AUTH_SOCK': '/s', 'MY_SECRET': 's', 'db_password': 'p', 'apitoken': 't',
    'UNLISTED': 'u', 'BAD\nNAME': 'v', 'NUL_VALUE': 'a\0b',
}


class RemoteEnv(unittest.TestCase):
    def test_only_the_allowlist_passes(self):
        names = ['BRAINSPIKE_HOST', 'GITHUB_TOKEN', 'SSH_AUTH_SOCK', 'MY_SECRET', 'db_password', 'apitoken',
                 'ACTIONS_ID_TOKEN_REQUEST_URL', 'RUNNER_TEMP', 'NUL_VALUE', 'ABSENT', 'BAD\nNAME']
        got = remote.remote_env(JOB, fixed=FIXED, names=names, cuda='2')
        self.assertEqual(got, {**FIXED, 'CUDA_VISIBLE_DEVICES': '2', 'CI_PROJECT': 'p', 'CI_RESUME': '1',
                               'BRAINSPIKE_HOST': 'h'})  # the job's CUDA_VISIBLE_DEVICES=3 is ignored

    def test_the_card_is_the_drivers(self):
        got = remote.remote_env({'CUDA_VISIBLE_DEVICES': '0,1,2,3,4'}, fixed=FIXED, names=['CUDA_VISIBLE_DEVICES'],
                                cuda='4')
        self.assertEqual(got['CUDA_VISIBLE_DEVICES'], '4')
        for bad in ('', '0,', 'all', None, '0 1'):
            with self.assertRaises(ValueError, msg=bad):
                remote.remote_env({}, fixed=FIXED, names=[], cuda=bad)

    def test_token_like_values_never_leave(self):
        tok = 'gh' + 's_' + 'a' * 36
        got = remote.remote_env({'CI_X': f'x {tok}', 'MY_VAR': 'github' + '_pat_' + 'b' * 30, 'CI_OK': 'ghs'},
                                fixed=FIXED, names=['MY_VAR'], cuda='0')
        self.assertEqual(sorted(got), sorted([*FIXED, 'CUDA_VISIBLE_DEVICES', 'CI_OK']))

    def test_fixed_values_come_from_the_inventory(self):
        got = remote.remote_env(JOB, fixed=FIXED, names=['PATH', 'HOME', 'CUDA_VISIBLE_DEVICES'], cuda='2')
        self.assertEqual({k: got[k] for k in remote.FIXED}, FIXED)
        for k in remote.FIXED:
            with self.assertRaises(ValueError):
                remote.remote_env(JOB, fixed={**FIXED, k: ''}, names=[], cuda='2')

    def test_refused(self):
        for n in ('GITHUB_X', 'github_x', 'ACTIONS_X', 'RUNNER_X', 'Runner_Name', 'SSH_AUTH_SOCK', 'ssh_auth_sock',
                  'X_TOKEN', 'XsecretY', 'PASSWORD', 'CI_PASSWORD_FILE', 'CI_PASSWD', 'CI_CREDENTIALS',
                  'CI_API_KEY', 'MY_APIKEY', 'SSH_PRIVATE_KEY', 'BASH_ENV', 'ENV', 'LD_PRELOAD', 'LD_AUDIT', '', '1X', 'A-B', 'A B', 'É', 'X' * 129, 7):
            self.assertTrue(remote.refused(n), n)
        for n in ('CI_PROJECT', 'CUDA_VISIBLE_DEVICES', 'BRAINSPIKE_HOST', 'XLA_PYTHON_CLIENT_PREALLOCATE', '_X',
                  'TOKE', 'X' * 128):
            self.assertFalse(remote.refused(n), n)

    def test_check_names_reports_what_a_registry_may_not_list(self):
        self.assertEqual(remote.check_names(['BRAINSPIKE_HOST', 'GITHUB_TOKEN', 'PATH', 'a-b', 'CUDA_VISIBLE_DEVICES']),
                         ['CUDA_VISIBLE_DEVICES', 'GITHUB_TOKEN', 'PATH', 'a-b'])


if __name__ == '__main__':
    unittest.main()
