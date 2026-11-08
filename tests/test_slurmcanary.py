"""``ci-run slurm canary`` (design decision 14.9, 14.10; RUN-9e): one short job per target
through the installed gate, against a fake Slurm and a fake ssh; what it records per
target, and the P2P hold it sets and lifts."""
from __future__ import annotations

import json
import os
import pathlib
import sys
import time
import unittest

from ci_runners import slurmcanary as sc, slurmpart, slurmsetup as ss
from tests.test_slurmsetup import Site


class Canary(Site):
    def setUp(self):
        super().setUp()
        self.apply()
        for v, driver in (('cpu', None), ('cuda13', '580.65.06')):
            b = self.base / 'envs' / f'p-{v}' / 'bin'
            b.mkdir(parents=True)
            os.symlink(sys.executable, b / 'python')
            if driver:
                (b / 'nvidia-smi').write_text(f'#!/bin/sh\necho {driver}\necho {driver}\n')
                (b / 'nvidia-smi').chmod(0o755)
        self.targets = self.data / 'ci-runners' / 'state' / 'slurm' / 'cluster1' / 'targets.json'

    def canary(self, *targets, imports=('json',), wait=1.0) -> tuple[int, str, dict]:
        ctx, out = self.ctx()
        rc = sc.canary(ctx, 'cluster1', 'acct1', 'p', targets=list(targets), imports=list(imports), wait=wait, poll=0.2)
        return rc, out.getvalue(), json.loads(self.targets.read_text())

    def test_a_cpu_target_passes_and_its_record_holds_latency_cost_and_the_round_trip(self):
        rc, said, held = self.canary('cpu')
        self.assertEqual(rc, 0, said)
        rec = held['cpu']['canary']
        self.assertEqual((rec['ok'], rec['state'], rec['exit_code'], rec['cards'], rec['why']),
                         (True, 'COMPLETED', '0:0', 0, []))
        self.assertIsInstance(rec['latency'], float)
        self.assertGreaterEqual(rec['latency'], -2)
        self.assertAlmostEqual(rec['cost'], 8 * 0.06 * rec['elapsed'] / 3600, places=4)
        self.assertEqual(rec['found']['imports'], {'json': 'ok'})
        self.assertIn('cpu: pass (job 1001, 0 cards on fake01', said)
        job = json.loads((self.d / 'slurm' / 'jobs' / '1001.json').read_text())
        self.assertEqual((job['sb']['--time'], job['sb']['--job-name']), ('8', 'ci-canary-cpu-1'))  # 3 min and the margin

    def test_a_failed_p2p_self_test_holds_the_target_at_one_card_and_a_pass_lifts_it(self):
        rc, said, held = self.canary('a40')
        self.assertEqual(rc, 1)
        self.assertEqual((held['a40']['max_cards'], held['a40']['canary']['p2p']), (1, True))
        self.assertTrue(held['a40']['canary']['why'][0].startswith('p2p: '), said)
        site = self.site()
        self.assertEqual(slurmpart.max_cards(str(self.data), site, 'a40'), 1)
        self.site_file.write_text(self.site_file.read_text().replace('max_cards = 2                     # 1 where',
                                                                     'max_cards = 1  # 1 where'))
        self.apply()  # the account's cluster-side entry follows the site config
        rc, said, held = self.canary('a40')
        self.assertEqual(rc, 0, said)
        self.assertEqual(set(held['a40']), {'canary'})  # the hold is lifted
        self.assertEqual(held['a40']['canary']['found']['driver'], '580.65.06')

    def test_findings_fail_the_canary(self):
        drv = self.base / 'envs' / 'p-cuda13' / 'bin' / 'nvidia-smi'
        drv.write_text('#!/bin/sh\necho 570.12\n')
        self.site_file.write_text(self.site_file.read_text().replace('max_cards = 2                     # 1 where',
                                                                     'max_cards = 1  # 1 where'))
        self.apply()
        rc, said, held = self.canary('a40', imports=('json', 'no_such_module'))
        self.assertEqual(rc, 1)
        self.assertEqual(held['a40']['canary']['why'], ["import no_such_module: ModuleNotFoundError",
                                                        "driver 570.12 is below the variant's floor 580"])
        self.assertNotIn('max_cards', held['a40'])

    def test_a_job_pending_past_the_wait_is_cancelled(self):
        (self.d / 'slurm' / 'plan.json').write_text(json.dumps([{'pend': -1, 'reason': 'Priority'}]))
        t0 = time.monotonic()
        rc, said, held = self.canary('cpu', wait=0.02)
        self.assertLess(time.monotonic() - t0, 30)
        self.assertEqual(rc, 1)
        self.assertEqual(held['cpu']['canary']['why'], ['pending past 0.02 min: Priority'])
        job = slurm_job(self.d, 1001)
        self.assertEqual(job['state'], 'CANCELLED')

    def test_only_the_accounts_targets(self):
        ctx, _ = self.ctx()
        with self.assertRaisesRegex(ss.SetupError, 'not a target of acct1'):
            sc.canary(ctx, 'cluster1', 'acct1', 'p', targets=['l40'])


class Check(unittest.TestCase):
    def test_the_node_side_check(self):
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            ws, res = os.path.join(d, 'ws'), os.path.join(d, 'res')
            os.makedirs(ws)
            os.makedirs(res)
            env = {'GITHUB_WORKSPACE': ws, 'CI_RESULTS_DIR': res}

            def smi(argv, **kw):
                return subprocess.CompletedProcess(argv, 0, '580.1\n', '')
            self.assertEqual(sc.check(['--cards', '1', '--floor', '580'], env=env, run=smi), 1)  # no marker staged
            out = json.loads(pathlib.Path(res, 'canary.json').read_text())
            self.assertEqual((out['why'], out['driver']), (["the staged marker: FileNotFoundError"], '580.1'))
            with open(os.path.join(ws, sc.MARKER), 'w') as f:
                f.write('t')
            self.assertEqual(sc.check(['--cards', '1', '--floor', '580'], env=env, run=smi), 0)
            self.assertEqual(pathlib.Path(res, sc.MARKER).read_text(), 't')

            def none(argv, **kw):
                raise FileNotFoundError(argv[0])
            self.assertEqual(sc.check(['--cards', '1', '--floor', '580'], env=env, run=none), 1)
            self.assertEqual(json.loads(pathlib.Path(res, 'canary.json').read_text())['why'], ['nvidia-smi: no driver version'])
            self.assertEqual(sc.check(['--floor', '580'], env=env, run=none), 0)  # a CPU target asks no driver


def slurm_job(d, jid: int) -> dict:
    from tests import slurm_fake
    return slurm_fake.refresh(json.loads((d / 'slurm' / 'jobs' / f'{jid}.json').read_text()))


if __name__ == '__main__':
    unittest.main()
