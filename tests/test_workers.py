"""Plan, retry and execution of the optional workers request."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from ci_runners import flow, hosted, part, presence, route
from tests import test_part_slots as slots_fixture
from tests import test_route as route_fixture

ROOT = Path(__file__).resolve().parents[1]


class Plan(unittest.TestCase):
    def test_positive_json_integer(self):
        def load(value):
            return route.load_plan({'schema': 1, 'parts': [route_fixture.part(workers=value)]})

        self.assertEqual(load(4)[0]['workers'], 4)
        for value in (True, False, 1.0, 1.5, '4', None, 0, -1):
            with self.subTest(value=value), self.assertRaisesRegex(route.PlanError, 'workers is a positive integer'):
                load(value)

    def test_workers_does_not_affect_route_or_admission(self):
        base = route_fixture.part(cores=6)
        rows = route_fixture.rows(route_fixture.host(ci_cpu=route_fixture.R(1),
                                                   **{'ci-cpu-6': route_fixture.R(1)}))
        routed = route.route([base], rows)[0]
        with_workers = flow.entry(route.route([{**base, 'workers': 3}], rows)[0])
        self.assertEqual(with_workers.pop('workers'), 3)
        self.assertEqual(with_workers, flow.entry(routed))

    def test_retry_uses_the_original_part_even_without_an_artifact(self):
        p = flow.entry(route.route([route_fixture.part(workers=3)], None)[0])
        retry = flow.decide(p, None, '', None, '', 1, {})
        self.assertEqual(retry['first'], 'lost')
        self.assertEqual(retry['labels'], ['self-hosted', 'ci-cpu'])
        expr = '$' + '{{ fromJSON(inputs.part).workers }}'
        for workflow, attempts in (('part', 2), ('public-part', 4)):
            source = (ROOT / '.github' / 'workflows' / f'{workflow}.yml').read_text(encoding='utf-8')
            for i, section in enumerate(source.split('command: $' + '{{ fromJSON(inputs.part).command }}')[1:]):
                with self.subTest(workflow=workflow, call=i):
                    self.assertEqual(section.splitlines()[1].strip(), f'workers: {expr}')
            self.assertEqual(source.count(f'workers: {expr}'), attempts)

    def test_public_and_slurm_are_final_even_without_an_artifact(self):
        for needs in ('cpu', 'gpu'):
            p = {'id': 'p', 'needs': needs, 'timeout_minutes': 50, 'workers': 3}
            env = {'CIRUNNERS_PUBLIC': 'true'}
            self.assertIsNone(flow.decide(p, None, '', None, '', 1, env))
            self.assertIsNotNone(flow.decide({k: v for k, v in p.items() if k != 'workers'},
                                             None, '', None, '', 1, env))
        p = {'needs': 'cpu', 'where': 'slurm', 'workers': 3}
        self.assertIsNone(flow.decide(p, None, '', None, '', 1, {}))


class Executor(slots_fixture.SlotHost):
    def test_child_inherits_the_checked_pin_and_worker_override(self):
        cmd = ('printf "%s:%s" "$CI_WORKERS" "$CI_CPUS" > "$CI_PART_DIR/value"; '
               'grep Cpus_allowed_list /proc/self/status > "$CI_PART_DIR/affinity"')
        _, requested = self.go(self.runner(slot='0'), command=cmd, workers=1, id='requested')
        self.assertEqual(requested['outcome'], 'pass')
        self.assertEqual(self.work(requested, 'value'), '1:0')
        self.assertEqual(self.work(requested, 'affinity').split()[-1], '0')
        self.assertEqual(requested['workers'], {'requested': 1, 'effective': 1, 'command_started': True, 'cpus': '0'})
        _, default = self.go(self.runner(slot='0'), command=cmd, id='default')
        self.assertEqual(self.work(default, 'value'), '3:0')
        self.assertNotIn('workers', default)

    def test_slot_limit_is_checked_on_each_attempt(self):
        runner = self.runner(slot='0')
        first_slot = part.RunnerSlot(0, '0-1', 3, '1G', 1)
        with mock.patch.object(part, 'runner_slot', return_value=(first_slot, '')):
            first = self.go(runner, workers=2)[1]
        self.assertEqual(first['workers']['effective'], 2)
        # Retry on a different slot rejects the original count even though its cpuset is wide enough.
        retry_slot = part.RunnerSlot(1, '0-1', 1, '1G', 1)
        with mock.patch.object(part, 'runner_slot', return_value=(retry_slot, '')):
            _, out = self.go(runner, workers=2, attempt_of=first['record'])
        self.assertEqual((out['outcome'], out['attempt']), ('unavailable', 2))
        self.assertIn('slot worker limit is 1', out['reason'])
        self.assertFalse(out['workers']['command_started'])
        self.assertFalse((Path(self.path(out)) / 'work' / 'ran').exists())

    def test_unslotted_private_runner_can_retry_but_does_not_count_for_drain(self):
        for slot in (None, '99'):
            with self.subTest(slot=slot):
                _, out = self.go(self.runner(slot=slot), workers=1, id=f'c{slot}')
                self.assertEqual((out['outcome'], out['next']), ('unavailable', 'fallback'))
                self.assertEqual(out['workers'], {'requested': 1, 'command_started': False})
                self.assertFalse((Path(self.path(out)) / 'work' / 'ran').exists())
                self.assertEqual(presence.part_end(out, {'class': 'part'}, ''), '')

    def test_public_gpu_and_slurm_never_start_or_retry(self):
        for i, (cls, name) in enumerate((('gpu', 'hb-pub-gpu0-1'), ('slurm', 'hb-slurm-1'))):
            with self.subTest(cls=cls):
                env = {**self.runner(cls), 'RUNNER_NAME': name}
                _, out = self.go(env, id=f'p{i}', workers=1)
                self.assertEqual((out['outcome'], out['next']), ('unavailable', 'none'))
                path = self.path(out)
                rec = json.loads(Path(path, 'record.json').read_text())
                ex = json.loads(Path(path, 'exit.json').read_text())
                self.assertEqual(part.finish(path, rec, ex)['next'], 'none')
                self.assertFalse(out['workers']['command_started'])
                self.assertFalse(Path(path, 'work', 'ran').exists())


class Supervisor(unittest.TestCase):
    def test_narrowed_affinity_is_checked_after_pinning_without_starting_command(self):
        for allowed in ({0}, OSError('not readable')):
            with self.subTest(allowed=allowed), tempfile.TemporaryDirectory() as d:
                def actual(_pid):
                    self.assertTrue(pin.called)
                    if isinstance(allowed, OSError):
                        raise allowed
                    return allowed

                with mock.patch.object(part.os, 'sched_setaffinity') as pin, \
                     mock.patch.object(part.os, 'sched_getaffinity', side_effect=actual), \
                     mock.patch.object(part.signal, 'signal'), mock.patch.object(part.os, 'setsid'), \
                     mock.patch.object(part.subprocess, 'Popen') as popen:
                    part.supervise(d, 10, ['must-not-start'], worker_spec=(2, 3, [0, 1]))
                    pin.assert_called_once_with(0, [0, 1])
                    popen.assert_not_called()
                ex = json.loads(Path(d, 'exit.json').read_text())
                out = part.finish(d, {'class': 'part'}, ex)
                self.assertEqual((out['outcome'], out['next']), ('unavailable', 'fallback'))
                self.assertFalse(out['workers']['command_started'])
                self.assertEqual(presence.part_end(out, {'class': 'part'}, ''), '')

    def test_hosted_worker_request_has_an_unavailable_artifact(self):
        with tempfile.TemporaryDirectory() as d:
            spawn = mock.Mock()
            env = {'RUNNER_ENVIRONMENT': 'github-hosted', 'RUNNER_TEMP': d,
                   'GITHUB_OUTPUT': os.path.join(d, 'output')}
            hosted.run(['--id', 'c', '--command', 'true', '--timeout-minutes', '1', '--workers', '1'], env, popen=spawn)
            spawn.assert_not_called()
            doc = json.loads(Path(d, 'ci-runners', 'c', 'outcome.json').read_text())
            self.assertEqual((doc['outcome'], doc['next']), ('unavailable', 'none'))
            self.assertEqual(doc['workers'], {'requested': 1, 'command_started': False})
            self.assertIn('outcome=unavailable', Path(env['GITHUB_OUTPUT']).read_text())
