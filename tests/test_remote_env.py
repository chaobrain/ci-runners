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
        got = remote.remote_env(JOB, fixed=FIXED, names=names)
        self.assertEqual(got, {**FIXED, 'CUDA_VISIBLE_DEVICES': '3', 'CI_PROJECT': 'p', 'CI_RESUME': '1',
                               'BRAINSPIKE_HOST': 'h'})

    def test_fixed_values_come_from_the_inventory(self):
        got = remote.remote_env(JOB, fixed=FIXED, names=['PATH', 'HOME'])
        self.assertEqual({k: got[k] for k in remote.FIXED}, FIXED)
        for k in remote.FIXED:
            with self.assertRaises(ValueError):
                remote.remote_env(JOB, fixed={**FIXED, k: ''}, names=[])

    def test_refused(self):
        for n in ('GITHUB_X', 'github_x', 'ACTIONS_X', 'RUNNER_X', 'Runner_Name', 'SSH_AUTH_SOCK', 'ssh_auth_sock',
                  'X_TOKEN', 'XsecretY', 'PASSWORD', 'CI_PASSWORD_FILE', '', '1X', 'A-B', 'A B', 'É', 'X' * 129, 7):
            self.assertTrue(remote.refused(n), n)
        for n in ('CI_PROJECT', 'CUDA_VISIBLE_DEVICES', 'BRAINSPIKE_HOST', 'XLA_PYTHON_CLIENT_PREALLOCATE', '_X',
                  'TOKE', 'X' * 128):
            self.assertFalse(remote.refused(n), n)

    def test_check_names_reports_what_a_registry_may_not_list(self):
        self.assertEqual(remote.check_names(['BRAINSPIKE_HOST', 'GITHUB_TOKEN', 'PATH', 'a-b']),
                         ['GITHUB_TOKEN', 'PATH', 'a-b'])


if __name__ == '__main__':
    unittest.main()
