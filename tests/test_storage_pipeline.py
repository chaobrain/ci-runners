"""The normal v1 status path must consume verified internal evidence."""
from __future__ import annotations
import json
import os
import unittest
from unittest import mock

from tests.test_storage_v1 import Bundles, RUN


@unittest.skipIf(os.name == 'nt', 'the v1 execution modules use Linux locks')
class Pipeline(unittest.TestCase):
    setUp = Bundles.setUp

    def test_part_preserves_only_the_logical_storage_requirement(self):
        from ci_runners import part, registry
        project = registry.Project('project', 'example/project', 'project/ci')
        env = part.part_env({'CI_STORAGE_STORE': 'ci-files-v1', 'CI_RUNNERS_ETC': '/private/config',
                             'CI_STORAGE_KEY': '/private/key', 'PATH': '/usr/bin'},
                            role='fixture', project=project, part_dir='/part', results='/results',
                            cards=[], cpus='', files={'slot_lock': '/lock', 'slot_queue': '/queue'},
                            slot='', resume=False)
        self.assertEqual(env['CI_STORAGE_STORE'], 'ci-files-v1')
        self.assertNotIn('CI_RUNNERS_ETC', env)
        self.assertNotIn('CI_STORAGE_KEY', env)

    def verdict(self):
        from ci_runners import verdict
        api = mock.Mock()
        fetch = mock.Mock(side_effect=AssertionError('GitHub artifact API must not be called'))
        env = dict(GITHUB_REPOSITORY=RUN['repository'], GITHUB_RUN_ID=str(RUN['id']),
                   GITHUB_RUN_ATTEMPT='1', GITHUB_TOKEN='fixture')
        with mock.patch.dict(os.environ, env, clear=True), mock.patch(
                'ci_runners.storage_v1.open_current', return_value=self.bundles):
            code = verdict.main(['--context', 'project/ci', '--sha', 'a' * 40, '--parts', 'cpu'], None,
                                call=api, fetch=fetch)
        fetch.assert_not_called()
        api.assert_called_once()
        self.assertEqual(api.call_args.args[0], 'POST')
        return code, api.call_args.args[3]['state']

    def test_no_success_without_both_outcome_and_report_receipt(self):
        (self.source / 'outcome.json').write_text(json.dumps({'outcome': 'pass', 'run-attempt': 1}))
        self.bundles.publish('ci-part-cpu-1', str(self.source))
        self.assertEqual(self.verdict(), (1, 'error'))
        self.bundles.seal('cpu', 1, 'success', [])
        self.assertEqual(self.verdict(), (0, 'success'))

    def test_red_tests_stay_red_without_a_success_receipt(self):
        (self.source / 'outcome.json').write_text(json.dumps({'outcome': 'red', 'run-attempt': 1}))
        self.bundles.publish('ci-part-cpu-1', str(self.source))
        self.assertEqual(self.verdict(), (1, 'failure'))

    def test_corrupt_transport_posts_error_instead_of_reading_github(self):
        self.bundles.publish('ci-part-cpu-1', str(self.source))
        self.transport.objects.clear()
        self.assertEqual(self.verdict(), (1, 'error'))
