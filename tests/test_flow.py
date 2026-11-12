"""The reusable workflow's commands (decision 16.6, design §4.10): the contract check, the
plan's checks and route, the retry by the five rows, the salvage leg's host and the
status, each with the GitHub API faked."""
from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from ci_runners import cli, flow, github, registry
from ci_runners import route as rt
from tests.test_route import R, T0, host, part, rows

SHA = 'a' * 40
REPO = 'chaobrain/myproj'


class Api:
    """A fake ``github.api``: ``files`` maps a ref to the spec's text (absent: 404)."""

    def __init__(self, files=None, jobs=None, fail='', private=True):
        self.files, self.jobs, self.fail, self.calls = files or {}, jobs or [], fail, []
        self.private = private

    def __call__(self, method, url, token, body=None):
        self.calls.append((method, url, body))
        if self.fail and self.fail in url:
            raise github.ApiError(f'{method} x: HTTP 502')
        if '/contents/' in url:
            ref = url.split('?ref=')[1]
            if ref not in self.files:
                raise github.ApiError(f'{method} {url}: HTTP 404')
            return {'encoding': 'base64', 'content': base64.b64encode(self.files[ref].encode()).decode()}
        if url.endswith(f'/repos/{REPO}'):
            return {'default_branch': 'main', 'private': self.private}
        if '/jobs?' in url:
            return {'jobs': self.jobs}
        return {}

    def posted(self):
        return [b for m, u, b in self.calls if m == 'POST']


class Env:
    """A job's environment with its output and summary files."""

    def __init__(self, d):
        self.out, self.summ = os.path.join(d, 'out'), os.path.join(d, 'summary')
        self.env = {'GITHUB_REPOSITORY': REPO, 'GITHUB_TOKEN': 't', 'GITHUB_RUN_ID': '77', 'GITHUB_RUN_ATTEMPT': '1',
                    'GITHUB_OUTPUT': self.out, 'GITHUB_STEP_SUMMARY': self.summ}

    def outputs(self):
        if not os.path.exists(self.out):
            return {}
        with open(self.out) as f:
            return dict(line.split('=', 1) for line in f.read().splitlines())

    def summary(self):
        if not os.path.exists(self.summ):
            return ''
        with open(self.summ) as f:
            return f.read()


def run(argv, env, api, etc='/nonexistent'):
    err = io.StringIO()
    out = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
        code = flow.main(argv, etc, call=api, env=env)
    return code, out.getvalue(), err.getvalue()


class Contract(unittest.TestCase):
    def test_the_rule(self):
        c = flow.check_contract
        self.assertEqual(c(None, None, 'main')[0], 'ok')  # no spec on the default branch
        self.assertEqual(c('schema = 1', None, 'main')[0], 'ok')  # no contract there
        self.assertEqual(c('contract = 2', 'contract = 2', 'main')[0], 'ok')
        self.assertEqual(c('contract = 2', 'contract = 3', 'main')[0], 'ok')
        self.assertEqual(c('contract = 0', 'contract = 0', 'main')[0], 'ok')
        for head in ('contract = 1', 'schema = 1', None, 'contract = "3"', 'contract = -1', 'contract = [', 'contract = 2.5'):
            state, text = c('contract = 2', head, 'main')
            self.assertEqual(state, 'failure', head)
            self.assertTrue(text.endswith('merge origin/main'), text)
        self.assertEqual(c('contract = 2', 'contract = 1', 'trunk')[1], 'contract 1 here, 2 on trunk: merge origin/trunk')
        for bad in ('contract = "2"', 'contract = -2', 'contract = ['):
            self.assertEqual(c(bad, 'contract = 9', 'main')[0], 'error', bad)  # the default branch's own fault

    def test_an_old_head_is_red_and_posted_with_merge_origin_main(self):
        with tempfile.TemporaryDirectory() as d:
            e = Env(d)
            api = Api({'main': 'schema = 1\ncontract = 2\n', SHA: 'schema = 1\ncontract = 1\n'})
            code, _, err = run(['contract', '--context', 'myproj/ci', '--sha', SHA, '--default-branch', 'main'], e.env, api)
            self.assertEqual(code, 1)
            self.assertEqual(api.posted(), [])  # read-only: the verdict job posts the state
            o = e.outputs()
            self.assertEqual((o['ok'], o['state']), ('false', 'failure'))
            self.assertIn('merge origin/main', o['description'])
            urls = sorted(u.split('/repos/')[1] for _, u, _ in api.calls)  # at once (#239): in any order
            self.assertEqual(urls, [REPO, f'{REPO}/contents/.github/ci-runners.toml?ref={SHA}',
                                    f'{REPO}/contents/.github/ci-runners.toml?ref=main'])  # private or not, and both specs

    def test_a_head_that_keeps_up_posts_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            e = Env(d)
            api = Api({'main': 'contract = 2\n', SHA: 'contract = 2\n'})
            code, _, _ = run(['contract', '--context', 'myproj/ci', '--sha', SHA], e.env, api)
            self.assertEqual(code, 0)
            self.assertEqual(api.posted(), [])
            self.assertEqual(e.outputs(), {'ok': 'true'})
            urls = [u for _, u, _ in api.calls]
            repo = [i for i, u in enumerate(urls) if u.endswith(f'/repos/{REPO}')]
            main = [i for i, u in enumerate(urls) if u.endswith('?ref=main')]
            self.assertEqual((len(urls), len(repo), len(main)), (3, 1, 1))
            self.assertLess(repo[0], main[0])  # no default branch given: read from the repository first

    def test_the_three_reads_overlap(self):
        """#239: the repository and both specs are read at once, not one after another
        (each read is a round trip through the egress, about 0.7 s): every read here waits
        until all three have started, and fails where they would run in turn."""
        import threading
        gate = threading.Barrier(3, timeout=5)

        class Slow(Api):
            def __call__(self, *a, **k):
                try:
                    gate.wait()
                except threading.BrokenBarrierError:
                    raise github.ApiError('GET x: the reads ran one after another') from None
                return super().__call__(*a, **k)

        with tempfile.TemporaryDirectory() as d:
            e = Env(d)
            api = Slow({'main': 'contract = 2\n', SHA: 'contract = 2\n'})
            code, _, err = run(['contract', '--context', 'myproj/ci', '--sha', SHA, '--default-branch', 'main'], e.env, api)
            self.assertEqual((code, e.outputs()), (0, {'ok': 'true'}), err)
            self.assertEqual(len(api.calls), 3)

    def test_the_first_failed_read_is_the_one_reported(self):
        """Read at once, a failure still names the read the order gives: the repository's,
        then the default branch's spec, then the head's."""
        for fail, want in ((f'/repos/{REPO}?', f'/repos/{REPO}'), ('ref=main', 'contents'), (f'ref={SHA}', 'contents')):
            with tempfile.TemporaryDirectory() as d:
                e = Env(d)

                class One(Api):
                    def __call__(self, method, url, token, body=None):
                        if fail in url + '?':
                            self.calls.append((method, url, body))
                            raise github.ApiError(f'{method} {url.split("?")[0]}: HTTP 502')
                        return super().__call__(method, url, token, body)

                api = One({'main': 'contract = 2\n', SHA: 'contract = 2\n'})
                code, _, _ = run(['contract', '--context', 'c/ci', '--sha', SHA, '--default-branch', 'main'], e.env, api)
                self.assertEqual((code, e.outputs()['state']), (1, 'error'), fail)
                self.assertIn(want, e.outputs()['description'], fail)

    def test_a_public_repository_is_refused_before_anything_of_its_head_runs(self):
        with tempfile.TemporaryDirectory() as d:
            e = Env(d)
            api = Api({'main': 'contract = 2\n', SHA: 'contract = 2\n'}, private=False)
            code, _, _ = run(['contract', '--context', 'myproj/ci', '--sha', SHA], e.env, api)
            self.assertEqual(code, 1)
            self.assertEqual(api.posted(), [])
            self.assertEqual(e.outputs(), {'ok': 'false', 'state': 'error', 'description': flow.PUBLIC})

    def test_absent_on_the_head_where_the_default_branch_sets_one(self):
        with tempfile.TemporaryDirectory() as d:
            e = Env(d)
            api = Api({'main': 'contract = 1\n'})
            self.assertEqual(run(['contract', '--context', 'c/ci', '--sha', SHA, '--default-branch', 'main'], e.env, api)[0], 1)
            self.assertIn('merge origin/main', e.outputs()['description'])

    def test_an_unreadable_spec_is_an_error_posted(self):
        with tempfile.TemporaryDirectory() as d:
            e = Env(d)
            api = Api({'main': 'contract = 1\n'}, fail='/contents/')
            self.assertEqual(run(['contract', '--context', 'c/ci', '--sha', SHA, '--default-branch', 'main'], e.env, api)[0], 1)
            self.assertEqual(e.outputs()['state'], 'error')
            self.assertEqual(api.posted(), [])


class Plan(unittest.TestCase):
    CAP = [host(ci_cpu=R(2), ci_gpu=R(1))]

    def plan(self, doc=None, *extra, outcome='', cap=None, project=None):
        with tempfile.TemporaryDirectory() as d:
            e = Env(d)
            api = Api()
            rows_file = os.path.join(d, 'rows.json')
            with open(rows_file, 'w') as f:
                json.dump(cap if cap is not None else [dict(r, read_at=r['read_at']) for r in self.CAP], f)
            argv = ['plan', '--context', 'myproj/ci', '--sha', SHA, '--project', 'myproj', '--rows', rows_file, *extra]
            if doc is not None:
                pf = os.path.join(d, 'plan.json')
                with open(pf, 'w') as f:
                    f.write(doc if isinstance(doc, str) else json.dumps(doc))
                argv += ['--plan-file', pf]
            if outcome:
                argv += ['--plan-outcome', outcome]
            with mock.patch('time.time', return_value=T0), mock.patch.object(flow, '_project', return_value=project):
                code, _, err = run(argv, e.env, api)
            self.assertEqual(api.calls, [])  # the plan job holds a read-only token: it posts nothing
            o = e.outputs()
            # the run's end where the plan ends it, as an output: the verdict job posts it
            ended = [{'state': o['state'], 'description': o['description']}] if 'state' in o else []
            return code, o, ended, e.summary(), err

    def test_without_a_plan_command_one_pytest_part(self):
        code, out, posted, summ, _ = self.plan()
        self.assertEqual(code, 0)
        self.assertEqual(posted, [])
        [p] = json.loads(out['parts'])
        self.assertEqual((p['id'], p['command'], p['needs'], p['minutes'], p['labels']),
                         ('pytest', 'python -m pytest', 'cpu', 60, ['self-hosted', 'ci-cpu']))
        self.assertEqual((out['ids'], out['count'], out['max_parallel'], out['routed']), ('pytest', '1', '16', 'true'))
        self.assertIn('| `pytest` | cpu | ci-cpu |', summ)

    def test_the_matrix_carries_what_part_yml_reads(self):
        doc = {'schema': 1, 'parts': [part('a', 'cpu', 10, memory_gib=1.5, results='*.xml'), part('b', 'gpu', 5)]}
        doc['parts'].append(part('c' * 64))
        code, out, _, _, _ = self.plan(doc, project=registry.Project('myproj', REPO, 'myproj/ci', max_parallel=3))
        self.assertEqual(code, 0)
        a, b, _ = json.loads(out['parts'])
        self.assertEqual((a['gpus'], a['memory'], a['results'], a['timeout_minutes']), (0, '1536M', '*.xml', 25 + 15))
        self.assertEqual((b['gpus'], b['memory'], b['results'], b['labels']), (1, '', '', ['self-hosted', 'ci-gpu']))
        self.assertEqual((a['salvage_minutes'], a['salvage_wait']), (50, 2400))
        self.assertEqual(flow.entry({**b, 'needs': 'gpu2'})['gpus'], 2)
        self.assertIs(flow.entry({**b, 'history': True})['history'], True)  # #228: the plan's key reaches part.yml
        self.assertEqual((out['ids'], out['max_parallel']), ('a b ' + 'c' * 64, '3'))
        self.assertNotIn('\n', out['parts'])

    def test_a_plan_that_does_not_load_is_red_with_its_reason(self):
        for doc, why in [('not json', 'not one JSON document'),
                         ({'schema': 2, 'parts': []}, 'schema'),
                         ({'schema': 1, 'parts': [part('plan')]}, 'reserved'),
                         ({'schema': 1, 'parts': [part('a' * 65)]}, 'at most 64'),
                         ({'schema': 1, 'parts': [part('a', results=['x'])]}, 'results is a string'),
                         ({'schema': 1, 'parts': [part('a', gpus=1)]}, "unknown key(s) ['gpus']"),
                         ({'schema': 1, 'parts': [part('a'), part('a')]}, 'unique'),
                         ({'schema': 1, 'parts': [part('a', 'tpu')]}, 'needs')]:
            code, out, posted, summ, _ = self.plan(doc)
            self.assertEqual(code, 2, doc)
            self.assertEqual((posted[0]['state'], 'routed' in out), ('failure', False), doc)
            self.assertIn(why, posted[0]['description'] + summ, doc)
            self.assertNotIn('parts', out)

    def test_the_plan_commands_own_end(self):
        code, out, posted, _, _ = self.plan({'schema': 1, 'parts': [part()]}, outcome='red')
        self.assertEqual((code, posted[0]['state'], 'routed' in out), (1, 'failure', False))
        for o in ('unavailable', 'lost', 'oom'):
            code, _, posted, _, _ = self.plan({'schema': 1, 'parts': [part()]}, outcome=o)
            self.assertEqual((code, posted[0]['state']), (1, 'error'), o)
            self.assertIn(f'plan: {o}', posted[0]['description'])
        self.assertEqual(self.plan({'schema': 1, 'parts': [part()]}, outcome='pass')[0], 0)

    def test_the_plan_options_and_part_keys_beyond_the_first_text(self):
        doc = {'schema': 1, 'max_parallel': 2, 'parts': [
            part('a', 'light', 15, queue_minutes=5, artifacts={'rows': 'bs/out', 'impact': 'bs/imp'}), part('b')]}
        code, out, _, _, _ = self.plan(doc, cap=[dict(host(ci_cpu=R(2), ci_light=R(2)))],
                                       project=registry.Project('myproj', REPO, 'myproj/ci', max_parallel=8))
        self.assertEqual(code, 0)
        a, b = json.loads(out['parts'])
        self.assertEqual((a['labels'], a['queue'], b['queue']), (['self-hosted', 'ci-light'], '5', '15'))
        # the job's bound waits the queue, then runs: minutes x speed 1 + 15, plus the queue
        self.assertEqual((a['timeout_minutes'], b['timeout_minutes']), (30 + 5, 25 + 15))
        self.assertEqual(a['artifacts'], [{'name': 'impact', 'path': 'bs/imp'}, {'name': 'rows', 'path': 'bs/out'}])
        self.assertEqual((b['artifacts'], a['files']), ([], False))
        self.assertEqual(out['max_parallel'], '2')  # the plan lowers the registry's cap
        doc['max_parallel'] = 50
        self.assertEqual(self.plan(doc, cap=[host(ci_cpu=R(2), ci_light=R(2))],
                                   project=registry.Project('myproj', REPO, 'myproj/ci', max_parallel=8))[1]['max_parallel'],
                         '8')  # never raises it

    def test_bad_options_are_named(self):
        for doc, why in [({'schema': 1, 'parts': [], 'files': '../x'}, 'files is a relative path'),
                         ({'schema': 1, 'parts': [], 'files': '/abs'}, 'files is a relative path'),
                         ({'schema': 1, 'parts': [], 'max_parallel': 0}, 'max_parallel is an integer in [1, 256]'),
                         ({'schema': 1, 'parts': [part('a', queue_minutes=0)]}, 'queue_minutes is a number'),
                         ({'schema': 1, 'parts': [part('a', artifacts={'a-b': 'x'})]}, 'artifacts maps'),
                         ({'schema': 1, 'parts': [part('a', artifacts={'a': '../x'})]}, 'artifacts maps'),
                         ({'schema': 1, 'parts': [part('a', artifacts={str(i): 'x' for i in range(5)})]}, 'at most 4'),
                         ({'schema': 1, 'parts': [part('a', artifacts={'ci': 'x'})]}, 'name ci is reserved'),
                         ({'schema': 1, 'parts': [part('a', memory_gib=5000)]}, 'memory_gib is at most 4096'),
                         ({'schema': 1, 'parts': [dict(part('a'), command='x' * 40000)]}, 'at most 32768 bytes')]:
            code, _, posted, summ, _ = self.plan(doc)
            self.assertEqual((code, posted[0]['state']), (2, 'failure'), doc)
            self.assertIn(why, summ, why)

    def test_the_files_the_plan_hands_on(self):
        with tempfile.TemporaryDirectory() as ws:
            os.makedirs(os.path.join(ws, 'out', 'sub'))
            with open(os.path.join(ws, 'out', 'sub', 'sel.txt'), 'w') as f:
                f.write('x')
            self.assertEqual(flow.check_files(ws, 'out'), '')
            self.assertEqual(flow.check_files(ws, 'out/'), '')
            self.assertEqual(flow.check_files(ws, 'none'), 'files: not a directory')
            os.symlink('/etc/passwd', os.path.join(ws, 'out', 'sub', 'l'))
            self.assertEqual(flow.check_files(ws, 'out'), 'files: sub/l is a link')
            os.unlink(os.path.join(ws, 'out', 'sub', 'l'))
            os.symlink(os.path.join(ws, 'out'), os.path.join(ws, 'via'))  # a link on the path, not in the tree
            self.assertEqual(flow.check_files(ws, 'via/sub'), 'files: via/sub goes through a link')
            with mock.patch.object(flow, 'MAX_FILES_BYTES', 0):
                self.assertIn('more than', flow.check_files(ws, 'out'))
            code, out, _, _, _ = self.plan({'schema': 1, 'parts': [part('a')], 'files': 'out'}, '--workspace', ws)
            self.assertEqual((code, out['files'], json.loads(out['parts'])[0]['files']), (0, 'out', True))
            code, _, posted, _, _ = self.plan({'schema': 1, 'parts': [part('a')], 'files': 'none'}, '--workspace', ws)
            self.assertEqual((code, posted[0]['description']), (2, 'plan: files: not a directory'))

    def test_hidden_files_are_handed_on_and_what_the_upload_cannot_carry_is_refused(self):
        """#227: the upload carries hidden entries (ci.yml's include-hidden-files), so a hidden
        directory and a hidden file inside a plain one are a plan's files like any other;
        what the upload cannot carry is the plan's failure, not every part lost."""
        with tempfile.TemporaryDirectory() as ws:
            os.makedirs(os.path.join(ws, '.ci-plan', '.cache'))
            with open(os.path.join(ws, '.ci-plan', '.cache', 'sel.txt'), 'w') as f:
                f.write('x')
            os.makedirs(os.path.join(ws, 'plain'))
            with open(os.path.join(ws, 'plain', '.hidden'), 'w') as f:
                f.write('x')
            self.assertEqual(flow.check_files(ws, '.ci-plan'), '')
            self.assertEqual(flow.check_files(ws, 'plain'), '')
            code, out, _, _, _ = self.plan({'schema': 1, 'parts': [part('a')], 'files': '.ci-plan'}, '--workspace', ws)
            self.assertEqual((code, out['files'], json.loads(out['parts'])[0]['files']), (0, '.ci-plan', True))
            with mock.patch.object(flow, 'MAX_FILES', 1):  # hidden entries count toward the bounds
                self.assertIn('more than', flow.check_files(ws, '.ci-plan'))
            for bad in ('a:b', 'a?b', 'a|b', 'a*b', 'a"b', 'a<b', 'a>b', 'a\nb'):
                p = os.path.join(ws, 'plain', bad)
                with open(p, 'w') as f:
                    f.write('x')
                self.assertEqual(flow.check_files(ws, 'plain'),
                                 f'files: {bad!r} has a character the upload refuses (" : < > | * ? or a line break)', bad)
                os.remove(p)
            os.makedirs(os.path.join(ws, 'plain', 'sub', 'none'))
            self.assertEqual(flow.check_files(ws, 'plain'),
                             'files: sub/none is an empty directory (the upload carries files)')
            os.rmdir(os.path.join(ws, 'plain', 'sub', 'none'))
            os.rmdir(os.path.join(ws, 'plain', 'sub'))
            os.makedirs(os.path.join(ws, 'plain', 'clone', '.git'))  # a clone may hold the plan's token
            with open(os.path.join(ws, 'plain', 'clone', '.git', 'config'), 'w') as f:
                f.write('x')
            self.assertEqual(flow.check_files(ws, 'plain'), 'files: clone/.git is a git directory (it may hold a credential)')
            shutil.rmtree(os.path.join(ws, 'plain', 'clone'))
            bad = os.path.join(os.fsencode(ws), b'plain', b'\xff')
            with open(bad, 'w') as f:
                f.write('x')
            self.assertEqual(flow.check_files(ws, 'plain'),
                             "files: '\\udcff' is not a UTF-8 name (the upload cannot name it)")
            os.remove(bad)
            os.makedirs(os.path.join(ws, 'empty'))
            self.assertEqual(flow.check_files(ws, 'empty'), 'files: empty is an empty directory (the upload carries files)')
            os.mkfifo(os.path.join(ws, 'plain', 'pipe'))
            self.assertEqual(flow.check_files(ws, 'plain'), 'files: pipe is not a file (a pipe, a socket or a device)')
            code, _, posted, _, _ = self.plan({'schema': 1, 'parts': [part('a')], 'files': 'empty'}, '--workspace', ws)
            self.assertEqual((code, posted[0]['state'], posted[0]['description']),
                             (2, 'failure', 'plan: files: empty is an empty directory (the upload carries files)'))

    def test_nothing_to_run_is_green_and_plan_only_runs_no_part(self):
        code, out, posted, _, _ = self.plan({'schema': 1, 'parts': []})
        self.assertEqual((code, out['parts'], out['state'], 'routed' in out), (0, '[]', 'success', False))
        self.assertEqual((posted[0]['state'], posted[0]['description']), ('success', 'nothing to run'))
        code, out, posted, summ, _ = self.plan({'schema': 1, 'parts': [part('a'), part('b')]}, '--plan-only', 'true')
        self.assertEqual((code, out['parts'], out['ids'], out['state'], 'routed' in out), (0, '[]', '', 'success', False))
        self.assertEqual((posted[0]['state'], posted[0]['description']), ('success', 'plan only: 2 part(s) routed, none run'))
        self.assertIn('| `a` |', summ)

    def test_a_matrix_too_large_for_one_output(self):
        with mock.patch.object(flow, 'MAX_MATRIX', 100):
            code, _, posted, _, _ = self.plan({'schema': 1, 'parts': [part('a'), part('b')]})
        self.assertEqual((code, posted[0]['state']), (2, 'failure'))
        self.assertIn('more than 100', posted[0]['description'])

    def test_a_plan_larger_than_the_bound(self):
        code, _, posted, _, _ = self.plan(' ' * (flow.MAX_PLAN + 1))
        self.assertEqual((code, posted[0]['description']), (2, f'plan: the plan is larger than {flow.MAX_PLAN} bytes'))

    def test_no_plan_output_is_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            e = Env(d)
            api = Api()
            code, _, _ = run(['plan', '--context', 'c/ci', '--sha', SHA, '--project', 'p', '--plan-file',
                              os.path.join(d, 'none.json'), '--plan-outcome', 'pass', '--rows', os.path.join(d, 'x')],
                             e.env, api)
            self.assertEqual((code, e.outputs()['state'], api.calls), (2, 'error', []))

    def test_a_route_error_is_an_error_naming_the_label(self):
        code, out, posted, _, err = self.plan({'schema': 1, 'parts': [part('a', 'gpu2')]})
        self.assertEqual((code, posted[0]['state'], 'routed' in out), (1, 'error', False))
        self.assertTrue(posted[0]['description'].startswith('route: ci-gpu2: part a'))
        self.assertIn('no host serves', err)

    def test_unreadable_capacity_routes_on_the_shared_labels(self):
        code, out, posted, summ, _ = self.plan({'schema': 1, 'parts': [part('a', 'gpu2')]}, cap='x')
        self.assertEqual((code, posted), (0, []))
        self.assertEqual(json.loads(out['parts'])[0]['labels'], ['self-hosted', 'ci-gpu2'])
        self.assertIn('capacity: unreadable', summ)

    def test_a_description_is_one_line_of_140(self):
        code, out, _, _, _ = self.plan('{"schema": 1, "parts": [{"id": "a\\nb"}]}')
        self.assertEqual(code, 2)
        self.assertNotIn('\n', out['description'])
        self.assertLessEqual(len(out['description']), 140)
        self.assertEqual(flow._text('a\nb  c'), 'a b c')

    def test_the_registry_max_parallel(self):
        self.assertEqual(flow.MAX_PARALLEL, 16)
        doc = {'name': 'myproj', 'repository': REPO, 'context': 'myproj/ci', 'hosts': {'h': {'memory_max': '8G'}}}
        self.assertEqual(registry.parse(doc, 'h').max_parallel, 16)
        self.assertEqual(registry.parse({**doc, 'max_parallel': 4}, 'h').max_parallel, 4)
        for bad in (0, 257, '4', True):
            with self.assertRaises(registry.RegistryError):
                registry.parse({**doc, 'max_parallel': bad}, 'h')

    def test_the_registry_entry_of_this_host(self):
        with tempfile.TemporaryDirectory() as etc:
            os.makedirs(os.path.join(etc, 'projects'))
            with open(os.path.join(etc, 'projects', 'myproj.toml'), 'w') as f:
                f.write('name = "myproj"\nrepository = "chaobrain/myproj"\ncontext = "myproj/ci"\nmax_parallel = 5\n'
                        '[hosts.hosta]\nmemory_max = "8G"\n')
            with mock.patch.object(cli, 'load_inventory', return_value=mock.Mock(this='hosta')):
                self.assertEqual(flow._project(etc, 'myproj').max_parallel, 5)
            # a light runner's host that does not run the project reads the same top-level keys
            with mock.patch.object(cli, 'load_inventory', return_value=mock.Mock(this='hostb')):
                self.assertEqual(flow._project(etc, 'myproj').max_parallel, 5)
            with mock.patch.object(cli, 'load_inventory', side_effect=OSError):
                self.assertEqual(flow._project(etc, 'myproj').max_parallel, 5)
            self.assertIsNone(flow._project(etc, 'other'))
            self.assertIsNone(flow._project(etc, '../x'))
            with open(os.path.join(etc, 'projects', 'myproj.toml'), 'a') as f:
                f.write('bad = 1\n')
            self.assertIsNone(flow._project(etc, 'myproj'))  # an entry that does not load: the defaults


ENV = {'GITHUB_REPOSITORY': REPO, 'GITHUB_RUN_ID': '77', 'GITHUB_RUN_ATTEMPT': '2'}


def routed(pid='p', needs='cpu', **kw):
    return flow.entry(rt.route([part(pid, needs, 10, **kw)], None)[0])


class Retry(unittest.TestCase):
    def decide(self, doc, p=None, cap=None, retries=1, role=''):
        return flow.decide(p or routed(), doc, role, cap, '', retries, ENV)

    def out(self, outcome, nxt, **kw):
        # the record of an earlier run attempt: never the name the retry would make up
        return {'outcome': outcome, 'next': nxt, 'attempt': 1, 'host': 'hosta', 'record': 'hosta:chaobrain/myproj/77-1/p-1',
                'pool-labels': ['ci-cpu', 'hosta-cpu'], 'cool-card': '', **kw}

    def test_a_final_end_has_no_retry(self):
        for o in ('pass', 'red'):
            self.assertIsNone(self.decide(self.out(o, 'none')), o)
        self.assertIsNone(self.decide(self.out('unavailable', 'none', reason='p2p: x')))

    def test_any_row_on_the_parts_labels_naming_the_first_record(self):
        r = self.decide(self.out('stopped', 'retry'))
        self.assertEqual((r['row'], r['labels'], r['attempt_of'], r['resume']),
                         ('any', ['self-hosted', 'ci-cpu'], 'hosta:chaobrain/myproj/77-1/p-1', 'false'))
        self.assertEqual(r['timeout_minutes'], 25 + 15)  # the queue on top of the route's bound

    def test_lost_without_outputs_is_retried_once_as_attempt_2(self):
        r = self.decide(None, role='hostb')
        self.assertEqual((r['row'], r['labels'], r['first']), ('any', ['self-hosted', 'ci-cpu'], 'lost'))
        self.assertEqual(r['attempt_of'], 'hostb:chaobrain/myproj/77-2/p-1')  # by name: attempt 2 wherever it runs
        from ci_runners import part as pt
        m = pt.RECORD_REF.fullmatch(r['attempt_of'])
        self.assertEqual((m['id'], m['n']), ('p', '1'))
        self.assertTrue(self.decide(None)['attempt_of'].startswith('unknown:'))  # no host known
        self.assertTrue(self.decide({'outcome': 3})['attempt_of'].startswith('unknown:'))  # malformed: lost

    def test_a_second_attempt_is_never_retried(self):
        self.assertIsNone(self.decide(self.out('lost', 'retry', attempt=2)))
        self.assertIsNone(self.decide(self.out('lost', 'retry', attempt='x')))

    def test_the_registry_may_allow_none(self):
        self.assertIsNone(self.decide(None, retries=0))
        self.assertIsNone(self.decide(self.out('stopped', 'retry'), retries=0))

    def test_cool_card_names_the_first_hosts_card(self):
        p = routed('g', 'gpu')
        r = self.decide(self.out('thermal', 'retry', **{'cool-card': '3', 'pool-labels': ['ci-gpu', 'hosta-gpu']}), p)
        self.assertEqual((r['row'], r['labels']), ('cool-card', ['self-hosted', 'hosta-gpu3']))

    def test_cool_card_never_on_a_host_that_may_yield(self):
        p = routed('g', 'gpu')
        cap = rows({**host('hosta', ci_gpu=R(1)), 'tier': 2}, {**host('h1', ci_gpu=R(1)), 'tier': 1})
        r = self.decide(self.out('thermal', 'retry', **{'cool-card': '3'}), p, cap)
        self.assertEqual(r['row'], 'fallback')
        self.assertNotIn('hosta-gpu3', r['labels'])

    def test_same_host_names_the_first_hosts_pool(self):
        r = self.decide(self.out('reboot', 'retry'))
        self.assertEqual((r['row'], r['labels']), ('same-host', ['self-hosted', 'hosta-cpu']))

    def test_more_memory_drops_the_plans_cap_and_resumes(self):
        p = routed(memory_gib=40)
        cap = rows(host(ci_cpu=R(1), mem_big=R(1)))
        r = self.decide(self.out('oom', 'retry', resume=True), p, cap)
        self.assertEqual((r['row'], r['labels'], r['resume'], r['memory']),
                         ('more-memory', ['self-hosted', 'ci-cpu', 'mem-big'], 'true', ''))
        self.assertEqual(self.decide(self.out('stopped', 'retry'), p)['memory'], '40960M')

    def test_fallback_routes_again(self):
        r = self.decide(self.out('unavailable', 'fallback', reason='busy: x'), cap=rows(host(ci_cpu=R(1))))
        self.assertEqual((r['row'], r['labels']), ('fallback', ['self-hosted', 'ci-cpu']))
        self.assertEqual(r['timeout_minutes'], 25 + 15)  # routed again, the queue on top as for the first

    def test_the_first_outcome_of_this_run_attempt_among_the_files(self):
        """The salvage leg reads its own upload, else the artifact it downloaded (an upload that
        reported a failure yet landed): a known outcome is never taken for lost."""
        with tempfile.TemporaryDirectory() as d:
            e = Env(d)
            env = {**e.env, 'GITHUB_RUN_ATTEMPT': '2'}
            old, red = os.path.join(d, 'old.json'), os.path.join(d, 'red.json')
            with open(old, 'w') as fh:
                json.dump(self.out('lost', 'retry', **{'run-attempt': 1}), fh)
            with open(red, 'w') as fh:
                json.dump(self.out('red', 'none', **{'run-attempt': 2}), fh)
            p = json.dumps(routed())
            run(['retry', '--part', p, '--outcome-file', os.path.join(d, 'none.json'), '--outcome-file', old,
                 '--outcome-file', red, '--rows', os.path.join(d, 'x')], env, Api())
            self.assertEqual(e.outputs()['retry'], 'none')  # red: not retried as lost, not read from run attempt 1

    def test_the_command_prints_the_labels_or_none(self):
        with tempfile.TemporaryDirectory() as d:
            e = Env(d)
            env = {**e.env, 'GITHUB_RUN_ATTEMPT': '2'}
            f = os.path.join(d, 'outcome.json')
            with open(f, 'w') as fh:
                json.dump(self.out('stopped', 'retry'), fh)
            p = json.dumps(routed())
            code, out, _ = run(['retry', '--part', p, '--outcome-file', f, '--rows', os.path.join(d, 'x')], env, Api())
            self.assertEqual((code, out.strip()), (0, '["self-hosted", "ci-cpu"]'))
            self.assertEqual(json.loads(e.outputs()['retry'])['attempt_of'], 'hosta:chaobrain/myproj/77-1/p-1')
            with open(f, 'w') as fh:
                json.dump(self.out('pass', 'none'), fh)
            os.unlink(e.out)
            code, out, _ = run(['retry', '--part', p, '--outcome-file', f], env, Api())
            self.assertEqual((code, out.strip(), e.outputs()['retry']), (0, 'none', 'none'))
            os.unlink(e.out)
            code, out, _ = run(['retry', '--part', p, '--outcome-file', os.path.join(d, 'missing.json'), '--role', 'hostb'],
                               env, Api())
            self.assertEqual(json.loads(e.outputs()['retry'])['attempt_of'], 'hostb:chaobrain/myproj/77-2/p-1')
            self.assertEqual(run(['retry', '--part', '[1]'], env, Api())[0], 2)


class Locate(unittest.TestCase):
    def test_the_runner_of_the_first_attempt(self):
        jobs = [{'name': 'ci / part a / first', 'runner_name': 'h-100-part-2'},
                {'name': 'ci / part ab / first', 'runner_name': 'x-part-1'},
                {'name': 'ci / part a / retry', 'runner_name': 'y-part-1'}]
        self.assertEqual(flow.first_runner(jobs, 'a'), 'h-100-part-2')
        self.assertEqual(flow.first_runner(jobs, 'b'), '')
        self.assertEqual(flow.first_runner(jobs + jobs[:1], 'a'), '')  # ambiguous: no guess
        self.assertEqual(flow.first_runner([{'name': 'part a / first', 'runner_name': None}], 'a'), '')

    def test_the_longest_role_the_name_starts_with(self):
        self.assertEqual(flow.role_of('h-100-part-2', ['h', 'h-100', 'h-10']), 'h-100')
        self.assertEqual(flow.role_of('hosta-gpu3', ['hosta', 'host']), 'hosta')
        self.assertEqual(flow.role_of('hostab-part-1', ['hosta']), '')
        self.assertEqual(flow.role_of('', ['hosta']), '')

    def test_the_salvage_leg_on_that_hosts_light_label(self):
        with tempfile.TemporaryDirectory() as d:
            e = Env(d)
            rf = os.path.join(d, 'rows.json')
            with open(rf, 'w') as f:
                json.dump([host('hosta', ci_cpu=R(1)), {**host('hostb', hb_light=R(1)), 'role': 'hb'}], f)
            api = Api(jobs=[{'name': 'x / part a / first', 'runner_name': 'hb-part-1'}])
            with mock.patch('time.time', return_value=T0):
                code, _, _ = run(['locate', '--part-id', 'a', '--rows', rf], e.env, api)
            self.assertEqual((code, e.outputs()), (0, {'labels': '["self-hosted", "hb-light"]', 'role': 'hb'}))
            self.assertIn('/actions/runs/77/attempts/1/jobs?per_page=100&page=1', api.calls[0][1])
            os.unlink(e.out)
            with mock.patch('time.time', return_value=T0):
                run(['locate', '--part-id', 'a', '--rows', rf], e.env, Api(fail='/jobs'))
            self.assertEqual(e.outputs(), {'labels': '', 'role': ''})  # unread: no salvage leg, no guess

    def test_only_a_host_that_is_up_with_a_light_runner(self):
        """A leg on a dead host's label would queue 24 h: the retry goes at once instead."""
        got = rows(host('hosta', hosta_light=R(1)), host('hostb', problems=['unreachable'], hostb_light=R(1)),
                   host('hostc', ci_cpu=R(1)), {**host('hostd'), 'label_runners': {}},
                   {'key': 'hosta:cpu', 'access': 'slurm', 'target': 'cpu', 'read_at': T0, 'problems': []})
        self.assertEqual(flow.light_roles(got), ['hosta', 'hostd'])  # hostd: its runners not read, might

    def test_the_jobs_are_read_page_by_page(self):
        api = Api(jobs=[{'name': f'j{i}'} for i in range(100)])
        self.assertEqual(len(flow.run_jobs({'GITHUB_REPOSITORY': REPO, 'GITHUB_TOKEN': 't', 'GITHUB_RUN_ID': '7',
                                            'GITHUB_RUN_ATTEMPT': '1'}, api)), 100 * flow.MAX_JOB_PAGES)
        self.assertGreaterEqual(100 * flow.MAX_JOB_PAGES, 2 + 256 * 4)


class Claim(unittest.TestCase):
    """The claim's decision in the plan job (read-only), its pending post in a job of its own."""
    BOT = 'github-actions[bot]'

    def st(self, state, t, who=BOT, context='myproj/ci'):
        return {'state': state, 'context': context, 'creator': {'login': who}, 'description': '',
                'created_at': f'2026-11-12T10:00:{t:02d}Z'}

    def api(self, statuses):
        api = Api()
        inner = api.__call__

        def call(method, url, token, body=None):
            if '/commits/' in url and method == 'GET':
                api.calls.append((method, url, body))
                return statuses() if callable(statuses) else statuses
            return inner(method, url, token, body)
        api.__call__ = call
        return api, call

    def go(self, argv, statuses):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, 'policy.toml'), 'w') as f:
                f.write('schema = 2\n')
            e = Env(d)
            api, call = self.api(statuses)
            code, _, _ = run(argv, e.env, call, etc=d)
            return code, e.outputs(), api.posted()

    def claim(self, statuses):
        return self.go(['claim', '--context', 'myproj/ci', '--sha', SHA], statuses)

    def pending(self, seen, statuses):
        return self.go(['pending', '--context', 'myproj/ci', '--sha', SHA, '--seen', seen], statuses)

    def test_the_decision_posts_nothing(self):
        code, out, posted = self.claim([self.st('success', 5), self.st('failure', 9, who='someone')])
        self.assertEqual((code, out['run'], posted), (0, 'false', []))  # green by the runners: skipped
        code, out, posted = self.claim([self.st('failure', 7), self.st('success', 3), self.st('success', 9, context='x')])
        self.assertEqual((out['run'], posted), ('true', []))
        self.assertEqual(float(out['seen']), flow.claim._epoch('2026-11-12T10:00:07+00:00'))  # the newest it read
        self.assertEqual(self.claim([self.st('success', 9, who='someone')])[1]['seen'], '0.0')  # a person's counts not

    def test_pending_only_where_nothing_was_posted_since_the_claim(self):
        seen = repr(flow.claim._epoch('2026-11-12T10:00:07+00:00'))
        code, out, posted = self.pending(seen, [self.st('failure', 7)])
        self.assertEqual((code, out['posted'], [(p['state'], p['description']) for p in posted]),
                         (0, 'true', [('pending', 'queued on the runners')]))
        for later in ('failure', 'success'):  # a red attempt, or a green, is never covered
            code, out, posted = self.pending(seen, [self.st(later, 8), self.st('failure', 7)])
            self.assertEqual((code, out['posted'], posted), (0, 'false', []), later)
        self.assertEqual(self.pending(seen, [self.st('failure', 8, who='someone')])[1]['posted'], 'true')
        self.assertEqual(self.pending('0.0', [])[1]['posted'], 'true')
        self.assertEqual(self.pending('soon', [])[0], 2)

    def test_a_red_posted_between_the_read_and_the_pending_is_put_back(self):
        seen = repr(flow.claim._epoch('2026-11-12T10:00:07+00:00'))
        red = dict(self.st('failure', 8), description='red at part a; the other parts run on')
        reads = iter([[self.st('failure', 7)], [self.st('pending', 9), red, self.st('failure', 7)]])
        code, out, posted = self.pending(seen, lambda: next(reads))
        self.assertEqual([(p['state'], p['description']) for p in posted],
                         [('pending', 'queued on the runners'), ('failure', 'red at part a; the other parts run on')])
        code, out, posted = self.pending(seen, [self.st('failure', 7)])  # nothing meanwhile: the pending stays
        self.assertEqual([p['state'] for p in posted], ['pending'])

    def test_no_policy_no_decision(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(run(['claim', '--context', 'c', '--sha', SHA], Env(d).env, Api(), etc=d)[0], 2)


class Start(unittest.TestCase):
    """The plan job's first step (``flow start``): the registry key, the claim's decision and
    the contract in one step, before anything of the head runs; each step of a self-hosted
    job costs about 1 s of log upload at its end."""
    BOT = 'github-actions[bot]'
    OLD = {'main': 'contract = 2\n', SHA: 'contract = 1\n'}
    NEW = {'main': 'contract = 2\n', SHA: 'contract = 2\n'}

    def st(self, state, t=5):
        return {'state': state, 'context': 'myproj/ci', 'creator': {'login': self.BOT}, 'description': '',
                'created_at': f'2026-11-12T10:00:{t:02d}Z'}

    def go(self, files, statuses, *extra, api_cls=Api):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, 'policy.toml'), 'w') as f:
                f.write('schema = 2\n')
            e = Env(d)
            api = api_cls(files)
            inner = api.__call__

            def call(method, url, token, body=None):
                if '/commits/' in url and method == 'GET':
                    api.calls.append((method, url, body))
                    if isinstance(statuses, Exception):
                        raise statuses
                    return statuses
                return inner(method, url, token, body)
            code, out, err = run(['start', '--context', 'myproj/ci', '--sha', SHA, '--default-branch', 'main', *extra],
                                 e.env, call, etc=d)
            return code, e.outputs(), api, out + err, e.summary()

    def test_a_head_to_run_gets_the_claim_and_the_contract_and_posts_nothing(self):
        code, o, api, _, _ = self.go(self.NEW, [self.st('failure')])
        self.assertEqual(code, 0)
        self.assertEqual((o['project'], o['run'], o['ok']), ('myproj', 'true', 'true'))
        self.assertEqual(float(o['seen']), flow.claim._epoch('2026-11-12T10:00:05+00:00'))
        self.assertNotIn('state', o)
        self.assertEqual(api.posted(), [])  # read-only: the pending job posts the claim's pending
        self.assertEqual(sorted(u.split('/repos/')[1].split('?')[0] for _, u, _ in api.calls),
                         [REPO, f'{REPO}/commits/{SHA}/statuses', f'{REPO}/contents/.github/ci-runners.toml',
                          f'{REPO}/contents/.github/ci-runners.toml'])

    def test_an_old_head_the_claim_runs_ends_the_run_as_the_contract_does(self):
        code, o, api, _, summary = self.go(self.OLD, [])
        self.assertEqual(code, flow.EXIT_RED)
        self.assertEqual((o['run'], o['ok'], o['state']), ('true', 'false', 'failure'))
        self.assertIn('merge origin/main', o['description'])
        self.assertIn('merge origin/main', summary)
        self.assertEqual(api.posted(), [])

    def test_a_green_head_ends_the_step_with_no_contract_output(self):
        """A head the claim skips ends the step at exit 0 whatever its contract: no ``ok``
        (no checkout, no plan command), no end for the verdict job, no summary."""
        for files in (self.OLD, self.NEW, {'main': 'contract = "x"\n'}):
            code, o, _, _, summary = self.go(files, [self.st('success')])
            self.assertEqual(code, 0)
            self.assertEqual(set(o), {'project', 'run', 'reason', 'seen'})
            self.assertEqual(o['run'], 'false')
            self.assertEqual(summary, '')

    def test_plan_only_reads_no_claim(self):
        code, o, api, _, _ = self.go(self.NEW, Exception('no statuses read'), '--plan-only', 'true')
        self.assertEqual((code, o), (0, {'project': 'myproj', 'ok': 'true'}))
        self.assertFalse([u for _, u, _ in api.calls if '/commits/' in u])
        code, o, _, _, _ = self.go(self.OLD, Exception('no statuses read'), '--plan-only', 'true')
        self.assertEqual((code, o['state'], 'run' in o), (flow.EXIT_RED, 'failure', False))
        code, o, api, _, _ = self.go(self.NEW, [], '--plan-only', 'false')
        self.assertEqual(o['run'], 'true')

    def test_the_claim_and_the_contract_read_at_once(self):
        """The claim's statuses and the contract's three reads overlap: each read here waits
        until all four have started, and fails where they would run in turn."""
        import threading
        gate = threading.Barrier(4, timeout=5)

        class Slow(Api):
            def __call__(self, *a, **k):
                try:
                    gate.wait()
                except threading.BrokenBarrierError:
                    raise github.ApiError('GET x: the reads ran one after another') from None
                return super().__call__(*a, **k)

        def statuses(*_):  # the claim's read, the fourth at the gate
            try:
                gate.wait()
            except threading.BrokenBarrierError:
                raise github.ApiError('GET statuses: the reads ran one after another') from None
            return []
        with mock.patch.object(flow, '_statuses', statuses):
            code, o, api, text, _ = self.go(self.NEW, [], api_cls=Slow)
        self.assertEqual((code, o.get('ok'), o.get('run')), (0, 'true', 'true'), text)
        self.assertEqual(len(api.calls), 3)

    def test_the_registry_key_first(self):
        """The project input, else the repository's name (``GITHUB_REPOSITORY``: a schedule's
        payload has no repository object); a malformed one fails the step before any read."""
        self.assertEqual(self.go(self.NEW, [])[1]['project'], 'myproj')  # chaobrain/myproj
        code, o, _, _, _ = self.go(self.NEW, [], '--project', 'other.proj_1-x')
        self.assertEqual((code, o['project']), (0, 'other.proj_1-x'))
        for bad in ('a/b', 'a b', 'x;y', '\u00e9'):
            code, o, api, text, _ = self.go(self.NEW, [], '--project', bad)
            self.assertEqual((code, o, api.calls), (flow.EXIT_RED, {}, []), bad)
            self.assertIn('::error::the project input is not a registry name', text)
        with tempfile.TemporaryDirectory() as d:
            e = Env(d)
            code, _, err = run(['start', '--context', 'c/ci', '--sha', SHA, '--plan-only', 'true'],
                               {**e.env, 'GITHUB_REPOSITORY': 'chaobrain/'}, Api())
            self.assertEqual((code, e.outputs()), (flow.EXIT_RED, {}))

    def test_a_failed_claim_read_fails_the_step_with_no_ok(self):
        code, o, _, text, _ = self.go(self.NEW, github.ApiError('GET statuses: HTTP 502'))
        self.assertEqual((code, o), (2, {'project': 'myproj'}))
        self.assertIn('HTTP 502', text)


class Status(unittest.TestCase):
    def test_a_red_part_posts_failure_at_once(self):
        api = Api()
        code, _, _ = run(['status', '--context', 'myproj/ci', '--sha', SHA, '--state', 'failure', '--description',
                          'x' * 200], {'GITHUB_REPOSITORY': REPO, 'GITHUB_TOKEN': 't', 'GITHUB_RUN_ID': '9'}, api)
        self.assertEqual(code, 0)
        [(m, u, b)] = api.calls
        self.assertTrue(u.endswith(f'/repos/{REPO}/statuses/{SHA}'))
        self.assertEqual((b['state'], len(b['description']), b['target_url']),
                         ('failure', 140, f'https://github.com/{REPO}/actions/runs/9'))

    def test_only_a_final_state_and_a_well_formed_head(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            flow.main(['status', '--context', 'c', '--sha', SHA, '--state', 'pending', '--description', 'd'], '', Api(), {})
        env = {'GITHUB_REPOSITORY': REPO, 'GITHUB_TOKEN': 't'}
        self.assertEqual(run(['status', '--context', 'c', '--sha', 'abc', '--state', 'error', '--description', 'd'],
                             env, Api())[0], 2)
        self.assertEqual(run(['status', '--context', 'c', '--sha', SHA, '--state', 'error', '--description', 'd'],
                             {}, Api())[0], 2)


class Cli(unittest.TestCase):
    def test_ci_run_flow(self):
        with mock.patch.object(flow, 'main', return_value=0) as m:
            self.assertEqual(cli.main(['flow', 'status']), 0)
        self.assertEqual(m.call_args[0][0], ['status'])


if __name__ == '__main__':
    unittest.main()
