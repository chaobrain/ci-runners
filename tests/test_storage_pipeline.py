"""The normal v1 status path must consume verified internal evidence."""
from __future__ import annotations
import json
import argparse
import os
import unittest
from unittest import mock

from tests.test_storage_v1 import Bundles, RUN


@unittest.skipIf(os.name == 'nt', 'the v1 execution modules use Linux locks')
class Pipeline(unittest.TestCase):
    setUp = Bundles.setUp

    def test_rerun_fetch_initializes_its_binding_before_reading_the_kept_plan(self):
        from ci_runners import flow, storage_v1
        self.bundles.publish('ci-plan-files', str(self.source))
        rerun = storage_v1.Bundles(self.transport, 'pilot', {**RUN, 'run_attempt': 2}, 100)
        dest = self.root / 'fetched'
        api = mock.Mock(side_effect=AssertionError('GitHub artifact API must not be called'))
        with mock.patch('ci_runners.flow.check_checkout', return_value=True), mock.patch(
                'ci_runners.storage_v1.open_current', return_value=rerun):
            self.assertEqual(flow.run_fetch(argparse.Namespace(workspace=str(self.root), dest=str(dest)), {}, api, api), 0)
        self.assertEqual((dest / 'pytest.xml').read_bytes(), (self.source / 'pytest.xml').read_bytes())
        api.assert_not_called()

    def test_rerun_download_command_initializes_the_new_attempt_only(self):
        from ci_runners import storage_v1
        self.bundles.publish('test-skip', str(self.source))
        rerun = storage_v1.Bundles(self.transport, 'pilot', {**RUN, 'run_attempt': 2}, 100)
        dest = self.root / 'downloaded'
        with mock.patch('ci_runners.storage_v1.open_current', return_value=rerun):
            self.assertEqual(storage_v1.main(['download', '--name', 'test-skip', '--path', str(dest)], '/unused', {}), 0)
        self.assertTrue((dest / 'pytest.xml').is_file())

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

    def verdict(self, jobs=None):
        from ci_runners import verdict
        api = mock.Mock(return_value={'jobs': jobs or []})
        fetch = mock.Mock(side_effect=AssertionError('GitHub artifact API must not be called'))
        env = dict(GITHUB_REPOSITORY=RUN['repository'], GITHUB_RUN_ID=str(RUN['id']),
                   GITHUB_RUN_ATTEMPT=str(self.bundles.run['run_attempt']), GITHUB_TOKEN='fixture')
        with mock.patch.dict(os.environ, env, clear=True), mock.patch(
                'ci_runners.storage_v1.open_current', return_value=self.bundles):
            code = verdict.main(['--context', 'project/ci', '--sha', 'a' * 40, '--parts', 'cpu'], None,
                                call=api, fetch=fetch)
        fetch.assert_not_called()
        self.assertEqual(sum(c.args[0] == 'POST' for c in api.call_args_list), 1)
        self.assertEqual(api.call_args.args[0], 'POST')
        return code, api.call_args.args[3]['state']

    def test_failed_rerun_upload_cannot_reveal_a_previous_success(self):
        from ci_runners import storage_v1
        (self.source / 'outcome.json').write_text(json.dumps({'outcome': 'pass', 'run-attempt': 1}))
        self.bundles.publish('ci-part-cpu-1', str(self.source))
        self.bundles.seal('cpu', 1, 'success', [])
        self.bundles = storage_v1.Bundles(self.transport, 'pilot', {**RUN, 'run_attempt': 2}, 100)
        self.bundles.bind()
        self.assertEqual(self.verdict([{'name': 'ci / part cpu / first', 'conclusion': 'failure'}]), (1, 'error'))
        self.assertEqual(self.verdict([{'name': 'ci / part cpu / first', 'conclusion': 'skipped'}]), (0, 'success'))

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
