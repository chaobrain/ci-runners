"""The reusable workflows ``ci.yml`` and ``part.yml`` (decision 16.6, design §4.10) and the
release stamp of their references (§9): their structure, read with the stdlib subset
reader (``tests/yamlsub.py``)."""
from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

from ci_runners import flow, stamp
from tests import yamlsub

ROOT = pathlib.Path(__file__).resolve().parents[1]
WF = ROOT / '.github' / 'workflows'
REUSABLE = ('ci.yml', 'part.yml', 'public.yml', 'public-part.yml')
#: a public caller's (decision 18.7): its jobs' labels, a hosted runner or its own pool only
PUBLIC_WF = ('public.yml', 'public-part.yml')
POOL_OR_HOSTED = ("${{ fromJSON(inputs.part).gpus > 0 && fromJSON('[\"self-hosted\",\"ci-pub-gpu\"]') "
                  "|| 'ubuntu-latest' }}")
#: the capability labels a job may name literally (16.1)
CAPABILITY = {'self-hosted', 'ci-light'}
#: every runs-on that is computed: the plan's route, the salvage leg's host, the retry's row
COMPUTED = {
    'public.yml': {j: 'ubuntu-latest' for j in ('admit', 'plan', 'pending', 'verdict')},
    'public-part.yml': {'first': POOL_OR_HOSTED, 'retry': POOL_OR_HOSTED, 'lost': 'ubuntu-latest',
                        'red': 'ubuntu-latest'},
    'part.yml': {
        'first': '${{ fromJSON(inputs.part).labels }}',
        'salvage': '${{ fromJSON(needs.locate.outputs.labels) }}',
        'retry': '${{ fromJSON(needs.first.outputs.retry || needs.salvage.outputs.retry || '
                 'needs.locate.outputs.retry).labels }}',
    },
}
#: each job's permissions, the least it needs (§4.8 rule 6)
PERMISSIONS = {
    'ci.yml': {'public': {'contents': 'read', 'statuses': 'write', 'actions': 'read', 'pull-requests': 'read'},
               # read-only (16.6 as amended): the head's plan command never holds a token that posts
               'plan': {'contents': 'read', 'statuses': 'read', 'actions': 'read', 'pull-requests': 'read'},
               'pending': {'statuses': 'write'},
               'parts': {'contents': 'read', 'statuses': 'write', 'actions': 'read'},
               'verdict': {'contents': 'read', 'statuses': 'write', 'actions': 'read'}},
    # no job that runs head code holds a write permission: the red job posts
    'part.yml': {'first': {'contents': 'read'},
                 'locate': {'contents': 'read', 'actions': 'read'},
                 'salvage': {'contents': 'read'},
                 'retry': {'contents': 'read'},
                 'red': {'statuses': 'write'}},
    # a public caller's: the same grants as a private caller's jobs, admit and lost reading only
    'public.yml': {'admit': {'contents': 'read'},
                   'plan': {'contents': 'read', 'statuses': 'read', 'actions': 'read', 'pull-requests': 'read'},
                   'pending': {'statuses': 'write'},
                   'parts': {'contents': 'read', 'statuses': 'write', 'actions': 'read'},
                   'verdict': {'contents': 'read', 'statuses': 'write', 'actions': 'read'}},
    'public-part.yml': {'first': {'contents': 'read'}, 'lost': {'contents': 'read'}, 'retry': {'contents': 'read'},
                        'red': {'statuses': 'write'}},
}


def load(name: str) -> dict:
    return yamlsub.load((WF / name).read_text())


def steps(doc: dict):
    for jid, job in doc['jobs'].items():
        for st in job.get('steps') or []:
            yield jid, st


def action_inputs(name: str) -> set[str]:
    return set(yamlsub.load((ROOT / 'actions' / name / 'action.yml').read_text())['inputs'])


class Reader(unittest.TestCase):
    def test_the_subset_reader_agrees_with_pyyaml_where_it_is_installed(self):
        try:
            import yaml
        except ImportError:
            self.skipTest('PyYAML is not installed')

        def norm(x):
            if isinstance(x, dict):
                return {('on' if k is True else k): norm(v) for k, v in x.items()}
            return [norm(v) for v in x] if isinstance(x, list) else x
        for f in [*WF.glob('*.yml'), *(ROOT / 'actions').glob('*/action.yml')]:
            self.assertEqual(yamlsub.load(f.read_text()), norm(yaml.safe_load(f.read_text())), f.name)

    def test_the_reader_refuses_what_it_does_not_know(self):
        self.assertEqual(yamlsub.load('a:\n  - b: 1\n    c: [x, "y z"]\n  - d\ne: >-\n  f\n  g\n'),
                         {'a': [{'b': 1, 'c': ['x', 'y z']}, 'd'], 'e': 'f g'})
        for bad in ('a: &x 1\n', 'a: 1\na: 2\n', 'a: 1\n b\n'):
            with self.assertRaises(ValueError, msg=bad):
                yamlsub.load(bad)


class Structure(unittest.TestCase):
    def test_workflow_call_only(self):
        for name in REUSABLE:
            doc = load(name)
            self.assertEqual(list(doc['on']), ['workflow_call'], name)
            self.assertNotIn('pull_request_target', (WF / name).read_text().replace(
                "github.event_name != 'pull_request_target'", ''), name)

    def test_the_plan_job_refuses_a_fork_and_pull_request_target(self):
        cond = load('ci.yml')['jobs']['plan']['if']
        self.assertIn("github.event_name != 'pull_request_target'", cond)
        self.assertIn('github.event.pull_request.head.repo.full_name == github.repository', cond)
        self.assertIn("needs.plan.result != 'skipped'", load('ci.yml')['jobs']['verdict']['if'])

    def test_runs_on_a_capability_label_or_a_computed_one_never_a_host(self):
        for name in REUSABLE:
            for jid, job in load(name)['jobs'].items():
                if 'uses' in job:
                    continue
                ro = job['runs-on']
                if isinstance(ro, list):
                    self.assertLessEqual(set(ro), CAPABILITY, f'{name}: {jid}')
                    self.assertIn('self-hosted', ro)
                else:
                    self.assertEqual(ro, COMPUTED.get(name, {}).get(jid), f'{name}: {jid}')

    def test_permissions_explicit_and_least(self):
        for name in REUSABLE:
            doc = load(name)
            self.assertNotIn('permissions', doc, name)  # a workflow-wide grant would hide a job's own
            self.assertEqual({j: job.get('permissions') for j, job in doc['jobs'].items()}, PERMISSIONS[name], name)

    def test_no_secret_reaches_a_part(self):
        for name in REUSABLE:
            text = (WF / name).read_text()
            self.assertNotRegex(text, r'secrets', name)
            for jid, job in load(name)['jobs'].items():
                self.assertNotIn('secrets', job, f'{name}: {jid}')
            for jid, st in steps(load(name)):
                if st.get('uses', '').startswith('actions/checkout@'):
                    self.assertIs(st['with']['persist-credentials'], False, f'{name}: {jid}')

    def test_no_expression_in_a_shell_script(self):
        """A consumer's string (its plan command, a part's id or command) reaches a shell only
        as an input or environment value, never pasted into a script."""
        for name in REUSABLE:
            for jid, st in steps(load(name)):
                if 'run' in st:
                    self.assertNotIn('${{', st['run'], f'{name}: {jid}')
        text = (ROOT / 'actions' / 'flow' / 'action.yml').read_text()
        run = yamlsub.load(text)['runs']['steps'][0]['run']
        self.assertNotIn('${{', run)

    def test_inputs_and_outputs_documented(self):
        for name in REUSABLE:
            call = load(name)['on']['workflow_call']
            for k, v in {**call.get('inputs', {}), **call.get('outputs', {})}.items():
                self.assertTrue(v.get('description'), f'{name}: {k}')
            for k, v in call.get('inputs', {}).items():
                self.assertEqual(v.get('type'), 'boolean' if k in ('plan-only', 'plan-token') else 'string',
                                 f'{name}: {k}')
        self.assertEqual(set(load('public.yml')['on']['workflow_call']['inputs']),
                         {'context', 'plan', 'project', 'sha', 'plan-only'})  # no token for a public plan command
        self.assertEqual(load('public-part.yml')['on']['workflow_call']['inputs'].keys(),
                         load('part.yml')['on']['workflow_call']['inputs'].keys())
        self.assertEqual(set(load('ci.yml')['on']['workflow_call']['inputs']),
                         {'context', 'plan', 'project', 'sha', 'plan-only', 'plan-token'})
        for k in ('plan-only', 'plan-token'):
            self.assertIs(load('ci.yml')['on']['workflow_call']['inputs'][k]['default'], False)
        self.assertTrue(load('ci.yml')['on']['workflow_call']['inputs']['context']['required'])

    def test_each_action_gets_only_inputs_it_declares(self):
        for name in REUSABLE:
            for jid, st in steps(load(name)):
                m = re.fullmatch(r'chaobrain/ci-runners/actions/([a-z]+)@\S+', st.get('uses', ''))
                if m:
                    self.assertLessEqual(set(st.get('with') or {}), action_inputs(m.group(1)), f'{name}: {jid}')
                    if m.group(1) == 'flow':
                        self.assertIn(st['with']['command'], flow.parser()._subparsers._group_actions[0].choices)

    def test_ci_calls_part_with_its_inputs(self):
        parts = load('ci.yml')['jobs']['parts']
        self.assertRegex(parts['uses'], r'\Achaobrain/ci-runners/\.github/workflows/part\.yml@\S+\Z')
        declared = load('part.yml')['on']['workflow_call']['inputs']
        self.assertEqual(set(parts['with']), set(declared))
        self.assertIs(parts['strategy']['fail-fast'], False)  # a red part cancels no other
        self.assertEqual(parts['strategy']['max-parallel'], '${{ fromJSON(needs.plan.outputs.max_parallel) }}')
        self.assertEqual(parts['name'], 'part ${{ matrix.part.id }}')  # what flow.first_runner matches

    def test_the_salvage_leg_only_on_the_lost_path_and_before_the_retry(self):
        jobs = load('part.yml')['jobs']
        self.assertEqual(jobs['first']['name'], 'first')
        self.assertEqual(jobs['first']['outputs']['uploaded'],
                         "${{ steps.part.outcome == 'success' || steps.again.outcome == 'success' }}")
        self.assertIn("needs.first.outputs.uploaded != 'true'", jobs['locate']['if'])
        self.assertIn("needs.locate.outputs.labels != ''", jobs['salvage']['if'])
        self.assertEqual(jobs['retry']['needs'], ['first', 'locate', 'salvage'])
        for j in ('locate', 'salvage', 'retry'):
            self.assertIn('!cancelled()', jobs[j]['if'], j)  # a cancelled run retries nothing (§4.8 rule 4)

    def test_every_attempt_posts_its_red_at_once_from_a_job_without_head_code(self):
        jobs = load('part.yml')['jobs']
        for jid in ('first', 'retry'):
            self.assertFalse([st for st in jobs[jid]['steps'] if (st.get('with') or {}).get('command') == 'status'], jid)
            self.assertEqual(jobs[jid]['outputs']['outcome'], '${{ steps.part.outputs.outcome }}')
        red = jobs['red']
        self.assertEqual(red['needs'], ['first', 'retry'])
        self.assertIn("needs.first.outputs.outcome == 'red' || needs.retry.outputs.outcome == 'red'", red['if'])
        self.assertIn('!cancelled()', red['if'])
        self.assertEqual(red['runs-on'], ['self-hosted', 'ci-light'])
        self.assertEqual([(st.get('uses', '').split('@')[0], st['with']['command'], st['with']['state']) for st in red['steps']],
                         [('chaobrain/ci-runners/actions/flow', 'status', 'failure')])
        # who may post, in both workflows: only jobs that run no head code
        for name in REUSABLE:
            for jid, job in load(name)['jobs'].items():
                if 'write' in (job.get('permissions') or {}).values() and 'uses' not in job:
                    self.assertFalse([st for st in job['steps'] if st.get('uses', '').startswith(
                        ('actions/checkout@', 'chaobrain/ci-runners/actions/part@'))], f'{name}: {jid}')

    def test_the_first_attempts_decision_comes_first(self):
        """A known outcome is never read as lost: where the first attempt decided, its
        decision is the retry's, whatever the lost path found (§4.10)."""
        jobs = load('part.yml')['jobs']
        r = 'needs.first.outputs.retry || needs.salvage.outputs.retry || needs.locate.outputs.retry'
        self.assertIn(f"({r} || 'none') != 'none'", jobs['retry']['if'])
        self.assertEqual(jobs['retry']['env']['RETRY'], '${{ %s }}' % r)
        self.assertEqual(jobs['retry']['timeout-minutes'], '${{ fromJSON(%s).timeout_minutes }}' % r)
        salv = [st for st in jobs['salvage']['steps'] if st.get('id') == 'retry'][0]['with']
        self.assertTrue(salv['outcome-file'].endswith('/ci-salvage-out/ci-part-${{ fromJSON(inputs.part).id }}-1/outcome.json'))
        self.assertTrue(salv['fallback-file'].endswith('/ci-salvage-in/ci-part-${{ fromJSON(inputs.part).id }}-1/outcome.json'))

    def test_each_attempt_gets_the_files_the_queue_bound_and_uploads_its_artifacts(self):
        for jid, attempt in (('first', '1'), ('retry', '2')):
            sts = load('part.yml')['jobs'][jid]['steps']
            ids = [st.get('id') for st in sts]
            down = [i for i, st in enumerate(sts) if st.get('uses', '').startswith('actions/download-artifact@')]
            self.assertEqual(len(down), 1, jid)
            self.assertLess(down[0], ids.index('part'), jid)  # the files are there before the command
            self.assertEqual(sts[down[0]]['with']['name'], 'ci-plan-files')
            part = sts[ids.index('part')]
            self.assertIn('ci-runners-files', part['env']['CIRUNNERS_FILES'])
            # the plan's queue_minutes or flow's default, which the job's bound includes
            self.assertEqual(part['with']['queue-timeout-minutes'], '${{ fromJSON(inputs.part).queue }}')
            ups = [st for st in sts if '.artifacts[' in st.get('if', '')]
            self.assertEqual(len(ups), flow.MAX_ARTIFACTS, jid)
            for i, st in enumerate(ups):
                self.assertIn(f'artifacts[{i}].name', st['if'])
                self.assertTrue(st['with']['name'].endswith(f"-${{{{ steps.part.outputs.attempt || '{attempt}' }}}}"))
                self.assertEqual(st['with']['path'], f'${{{{ runner.temp }}}}/${{{{ fromJSON(inputs.part).artifacts[{i}].path }}}}')

    def test_a_public_caller_goes_to_public_yml_and_a_private_one_never(self):
        jobs = load('ci.yml')['jobs']
        self.assertEqual(jobs['public']['if'], "github.event.repository.visibility == 'public'")
        self.assertIn("github.event.repository.visibility != 'public'", jobs['plan']['if'])
        self.assertRegex(jobs['public']['uses'], r'\Achaobrain/ci-runners/\.github/workflows/public\.yml@\S+\Z')
        self.assertEqual(set(jobs['public']['with']), set(load('public.yml')['on']['workflow_call']['inputs']))
        self.assertNotIn('plan-token', jobs['public']['with'])
        self.assertEqual(load('ci.yml')['on']['workflow_call']['outputs']['state']['value'],
                         '${{ jobs.verdict.outputs.state || jobs.public.outputs.state }}')
        # every other job of ci.yml is a private caller's: they need the plan job, which refuses a public one
        for jid, job in jobs.items():
            if jid not in ('public', 'plan'):
                self.assertIn('plan', [job['needs']] if isinstance(job['needs'], str) else job['needs'], jid)

    def test_the_plan_job_hands_on_its_outcome_and_files(self):
        sts = load('ci.yml')['jobs']['plan']['steps']
        plan = [st for st in sts if st.get('id') == 'plan'][0]
        # a plan command whose part step left no outcome is lost, not passed
        self.assertEqual(plan['with']['plan-outcome'], "${{ inputs.plan != '' && (steps.command.outputs.outcome || 'lost') || '' }}")
        self.assertEqual(plan['if'], "always() && steps.contract.outcome == 'success'")
        up = [st for st in sts if st.get('uses', '').startswith('actions/upload-artifact@')]
        self.assertEqual([u['with']['name'] for u in up], ['ci-plan-files'])
        self.assertIn("steps.plan.outputs.routed == 'true'", up[0]['if'])
        self.assertTrue(up[0]['if'].startswith('${{ !cancelled() && '))  # a failed plan part's upload skips it not
        env = [st for st in sts if st.get('id') == 'command'][0]['env']
        self.assertLessEqual({'CIRUNNERS_PLAN', 'CIRUNNERS_EVENT', 'CIRUNNERS_PR_LABELS', 'CIRUNNERS_BASE_SHA',
                              'CIRUNNERS_HEAD_SHA'}, set(env))
        self.assertFalse([k for k in env if k.startswith('CI_')])  # the part action strips CI_* names
        # the plan command gets no token but the read-only one its caller asks for (plan-token)
        self.assertEqual([k for k in env if 'token' in k.lower() + env[k].lower()], ['GH_TOKEN'])

    def test_the_parts_run_once_the_plan_is_routed_whatever_failed_after(self):
        cond = load('ci.yml')['jobs']['parts']['if']
        self.assertTrue(cond.startswith('!cancelled() && '))  # a status function: no implicit success()
        self.assertIn("needs.plan.outputs.routed == 'true'", cond)
        self.assertIn("needs.plan.outputs.run == 'true'", cond)
        self.assertIn("needs.plan.outputs.files != 'failure'", cond)
        plan = load('ci.yml')['jobs']['plan']
        self.assertEqual(plan['outputs']['files'], '${{ steps.files.outcome }}')
        self.assertEqual(plan['steps'][0]['id'], 'project')  # before anything reads it
        text = (WF / 'ci.yml').read_text()
        self.assertNotIn('github.event.repository.name', text)  # absent on a schedule
        self.assertEqual(load('ci.yml')['jobs']['parts']['with']['project'], '${{ needs.plan.outputs.project }}')

    def test_a_locate_step_that_failed_still_decides_the_retry(self):
        st = [s for s in load('part.yml')['jobs']['locate']['steps'] if s.get('id') == 'retry'][0]
        self.assertEqual(st['if'], "always() && steps.locate.outputs.labels == ''")

    def test_the_verdict_runs_unless_cancelled_or_posted(self):
        cond = load('ci.yml')['jobs']['verdict']['if']
        for c in ('always()', '!cancelled()', "needs.plan.outputs.run != 'false'"):
            self.assertIn(c, cond)
        self.assertNotIn('wait-seconds', str(load('ci.yml')['jobs']['verdict']['steps']))

    def test_the_plan_job_posts_nothing_and_hands_a_token_only_to_the_plan_command(self):
        """16.6 as amended: the plan job's token is read-only, its flow steps read (claim,
        contract) or compute (plan), and the only token a step's environment carries is
        the plan command's GH_TOKEN, and only where the caller sets plan-token."""
        job = load('ci.yml')['jobs']['plan']
        self.assertLessEqual(set(job['permissions'].values()), {'read'})
        flows = [st['with']['command'] for st in job['steps'] if st.get('uses', '').startswith('chaobrain/ci-runners/actions/flow@')]
        self.assertEqual(flows, ['claim', 'contract', 'plan'])
        uses = {st.get('uses', '').split('@')[0] for st in job['steps']} - {''}
        self.assertEqual(uses, {'chaobrain/ci-runners/actions/flow', 'actions/checkout', 'chaobrain/ci-runners/actions/part',
                                'actions/upload-artifact'})  # no claim action: it posts pending
        for st in job['steps']:
            text = str(st.get('env', {})) + str(st.get('with', {}))
            if st.get('id') == 'command':
                self.assertEqual(st['env']['GH_TOKEN'], "${{ inputs.plan-token && github.token || '' }}")
                self.assertNotIn('token', str(st.get('with', {})))
            else:
                self.assertNotIn('token', text.lower(), st.get('id'))
        self.assertNotIn('GH_TOKEN', str(load('part.yml')))  # the parts never get one

    def test_one_final_status_and_its_pending_beside_the_parts(self):
        jobs = load('ci.yml')['jobs']
        pend = jobs['pending']
        self.assertEqual(pend['needs'], 'plan')
        self.assertIn("needs.plan.outputs.routed == 'true'", pend['if'])
        self.assertIn("needs.plan.outputs.run == 'true'", pend['if'])  # never over a green the claim skipped
        self.assertEqual(pend['if'].replace(' ', ''), jobs['parts']['if'].replace(' ', ''))  # pending iff parts
        self.assertEqual([st['with']['command'] for st in pend['steps']], ['pending'])
        self.assertEqual(pend['steps'][0]['with']['seen'], "${{ needs.plan.outputs.seen || '0.0' }}")
        v = jobs['verdict']
        self.assertEqual(v['needs'], ['plan', 'pending', 'parts'])  # no pending after the final status
        post, lost, verd = v['steps']
        self.assertEqual((post['if'], lost['if'], verd['if']),
                         ("needs.plan.outputs.state != ''", "needs.plan.outputs.state == '' && needs.plan.outputs.routed != 'true'",
                          "needs.plan.outputs.state == '' && needs.plan.outputs.routed == 'true'"))
        self.assertEqual(lost['with']['state'], 'error')
        self.assertEqual((post['with']['command'], post['with']['state']), ('status', '${{ needs.plan.outputs.state }}'))
        self.assertEqual(jobs['plan']['outputs']['state'], '${{ steps.contract.outputs.state || steps.plan.outputs.state }}')
        # who posts: pending, a red attempt (part.yml) and the verdict job; nothing else
        posters = {j for j, job in jobs.items() if 'write' in (job.get('permissions') or {}).values()}
        self.assertEqual(posters, {'pending', 'parts', 'verdict', 'public'})  # public: public.yml's own three
        pub = load('public.yml')['jobs']
        self.assertEqual({j for j, job in pub.items() if 'write' in (job.get('permissions') or {}).values()},
                         {'pending', 'parts', 'verdict'})

    def test_the_retry_names_the_first_attempt(self):
        part = [st for st in load('part.yml')['jobs']['retry']['steps'] if st.get('id') == 'part'][0]['with']
        self.assertEqual(part['attempt-of'], '${{ fromJSON(env.RETRY).attempt_of }}')
        self.assertEqual(part['resume'], '${{ fromJSON(env.RETRY).resume }}')


class Public(unittest.TestCase):
    """A public caller's run (decision 18.7): public.yml and public-part.yml."""

    def test_every_job_on_a_hosted_runner_but_a_gpu_part_on_its_own_pool(self):
        for name in PUBLIC_WF:
            text = (WF / name).read_text()
            self.assertNotRegex(text, r'ci-(light|cpu|gpu2|timing|slurm)\b|\bci-gpu\b|mem-big', name)
            for jid, job in load(name)['jobs'].items():
                if 'uses' not in job:
                    self.assertIn(job['runs-on'], ('ubuntu-latest', POOL_OR_HOSTED), f'{name}: {jid}')
        jobs = load('public-part.yml')['jobs']
        self.assertNotIn('salvage', jobs)  # its tree goes with its job
        self.assertNotIn('locate', jobs)
        for jid in ('first', 'retry'):  # the hosted executor for a CPU part, the executor in the pool for a gpu one
            sts = {st.get('id'): st for st in jobs[jid]['steps']}
            self.assertEqual(sts['hosted']['if'], 'fromJSON(inputs.part).gpus == 0')
            self.assertRegex(sts['hosted']['uses'], r'\Achaobrain/ci-runners/actions/hosted@')
            self.assertEqual(sts['part']['if'], 'fromJSON(inputs.part).gpus > 0')

    def test_admit_comes_first_and_ends_the_run_where_it_refuses(self):
        jobs = load('public.yml')['jobs']
        admit = jobs['admit']
        self.assertEqual([st['with']['command'] for st in admit['steps']], ['admit'])
        self.assertEqual(admit['steps'][0]['with']['token'], '')  # it reads no API
        self.assertNotIn('if', admit)
        self.assertEqual(jobs['plan']['needs'], 'admit')
        for jid, job in jobs.items():  # each other job needs plan, so a refusal skips the run
            if jid not in ('admit', 'plan'):
                self.assertIn('plan', [job['needs']] if isinstance(job['needs'], str) else job['needs'], jid)
        self.assertIn("github.event_name != 'pull_request_target'", jobs['plan']['if'])
        self.assertIn('github.event.pull_request.head.repo.full_name == github.repository', jobs['plan']['if'])

    def test_every_flow_step_says_it_is_a_public_caller_s(self):
        for name in PUBLIC_WF:
            for jid, job in load(name)['jobs'].items():
                for st in job.get('steps') or []:
                    if 'actions/flow@' in st.get('uses', ''):
                        env = {**(job.get('env') or {}), **(st.get('env') or {})}
                        self.assertEqual(env.get('CIRUNNERS_PUBLIC'), 'true', f'{name}: {jid}')
        for name in ('ci.yml', 'part.yml'):
            self.assertNotIn('CIRUNNERS_PUBLIC', (WF / name).read_text(), name)

    def test_the_plan_command_holds_no_token(self):
        sts = {st.get('id'): st for st in load('public.yml')['jobs']['plan']['steps']}
        self.assertNotIn('GH_TOKEN', str(sts['command']))
        self.assertNotIn('uses', sts['command'])  # a plain step on the hosted runner, bounded
        self.assertEqual(sts['command']['timeout-minutes'], 30)
        self.assertNotIn('token', str(load('public-part.yml')).lower().replace("token: ''", ''))

    def test_posting_jobs_run_no_head_code(self):
        for name in PUBLIC_WF:
            for jid, job in load(name)['jobs'].items():
                if 'write' in (job.get('permissions') or {}).values() and 'uses' not in job:
                    for st in job['steps']:
                        self.assertNotIn('run', st, f'{name}: {jid}')
                        self.assertNotIn('checkout', st.get('uses', ''), f'{name}: {jid}')

    def test_the_verdict_reads_artifacts_only(self):
        sts = load('public.yml')['jobs']['verdict']['steps']
        self.assertEqual([st['with']['command'] for st in sts if 'actions/flow@' in st.get('uses', '')],
                         ['status', 'status', 'verdict'])
        self.assertNotIn('actions/verdict@', str(sts))  # the host layer's, with its salvage


class Flow(unittest.TestCase):
    def test_each_input_reaches_its_flag(self):
        text = (ROOT / 'actions' / 'flow' / 'action.yml').read_text()
        ins = action_inputs('flow') - {'command', 'token'}
        pairs = set(re.findall(r'"(--[a-z-]+)=\$IN_([A-Z_]+)"', text))
        # the fallback file is a second --outcome-file (retry reads the first that holds one)
        self.assertEqual(pairs, {('--outcome-file' if i == 'fallback-file' else f'--{i}', i.upper().replace('-', '_'))
                                 for i in ins})
        passed = {f for f, _ in pairs}
        flags = set(re.findall(r"add_argument\('(--[a-z-]+)'", (ROOT / 'ci_runners' / 'flow.py').read_text()))
        self.assertLessEqual(passed, flags)
        self.assertIn('exec sh "$GITHUB_ACTION_PATH/../../bin/ci-run" flow "$IN_COMMAND"', text)


class Stamp(unittest.TestCase):
    def tree(self, d: str) -> pathlib.Path:
        t = pathlib.Path(d) / 'tree'
        shutil.copytree(ROOT, t, ignore=shutil.ignore_patterns('.git', '__pycache__'))
        stamp.stamp(t, 'hook')  # main's state, also when this tree is a stamp PR's (operations §5 step 2)
        return t

    def files(self, t: pathlib.Path) -> dict[str, str]:
        return {str(p.relative_to(t)): p.read_text(errors='replace') for p in t.rglob('*')
                if p.is_file() and '__pycache__' not in p.parts}

    def test_main_names_v1_and_hook(self):
        # main is `hook` with `@v1`; a stamp PR's tree (operations §5 step 2) is `vX.Y.Z` with `@vX.Y.Z`
        release = (ROOT / 'actions' / 'RELEASE').read_text().strip()
        self.assertRegex(release, r'\Ahook\Z|\Av[0-9]+\.[0-9]+\.[0-9]+\Z')
        ref = 'v1' if release == 'hook' else release
        for name in REUSABLE:
            got = stamp.refs((WF / name).read_text())
            self.assertTrue(got, name)
            self.assertEqual({r for _, r in got}, {ref}, name)
        self.assertIn(('chaobrain/ci-runners/.github/workflows/part.yml', ref), stamp.refs((WF / 'ci.yml').read_text()))
        self.assertIn(('chaobrain/ci-runners/.github/workflows/public.yml', ref), stamp.refs((WF / 'ci.yml').read_text()))
        self.assertIn(('chaobrain/ci-runners/.github/workflows/public-part.yml', ref),
                      stamp.refs((WF / 'public.yml').read_text()))

    def test_a_stamped_tree_differs_only_in_its_references_and_restores(self):
        with tempfile.TemporaryDirectory() as d:
            t = self.tree(d)
            before = self.files(t)
            r = subprocess.run([sys.executable, '-B', '-m', 'ci_runners.stamp', 'v1.17.0', '--root', str(t)],
                               cwd=t, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            after = self.files(t)
            changed = {k for k in before if before[k] != after[k]}
            self.assertEqual(changed, {'actions/RELEASE', *stamp.WORKFLOWS})
            self.assertEqual(after['actions/RELEASE'], 'v1.17.0\n')
            for rel in stamp.WORKFLOWS:
                old, new = before[rel].splitlines(), after[rel].splitlines()
                self.assertEqual(len(old), len(new))
                for a, b in zip(old, new):
                    if a != b:
                        self.assertEqual(stamp.rewrite(a, 'v1.17.0'), b)
                        self.assertRegex(a, r'chaobrain/ci-runners/\S+@v1\b')
                # nothing left unpinned: every self-reference names the release, every other action a tag
                self.assertEqual({ref for _, ref in stamp.refs(after[rel])}, {'v1.17.0'}, rel)
                self.assertNotRegex(after[rel], r'chaobrain/ci-runners/\S+@(?!v1\.17\.0\b)')
                for line in new:
                    m = re.match(r'\s*(?:- )?uses: (\S+)', line)
                    if m:
                        self.assertRegex(m.group(1), r'@v[0-9][0-9.]*\Z', line)
            stamp.stamp(t, 'hook')
            self.assertEqual(self.files(t), before)

    def test_a_proof_branch_names_itself_and_keeps_the_release(self):
        with tempfile.TemporaryDirectory() as d:
            t = self.tree(d)
            self.assertEqual(sorted(stamp.stamp(t, 'hook', 'r6-proof')), sorted(stamp.WORKFLOWS))
            self.assertEqual((t / 'actions' / 'RELEASE').read_text().strip(), 'hook')
            self.assertEqual({r for _, r in stamp.refs((t / '.github/workflows/part.yml').read_text())}, {'r6-proof'})

    def test_a_bad_release_or_ref_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            t = self.tree(d)
            for rel, ref in (('v1.17', ''), ('latest', ''), ('hook', '../x'), ('hook', '-x'), ('hook', 'a b')):
                with self.assertRaises(ValueError, msg=(rel, ref)):
                    stamp.stamp(t, rel, ref)


if __name__ == '__main__':
    unittest.main()
