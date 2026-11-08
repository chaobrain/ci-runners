"""The gate on a Slurm cluster and its batch script (design decision 14.5, 14.3; RUN-9c),
against a fake Slurm and a fake ssh (``tests/slurm_fake.py``)."""
from __future__ import annotations

import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from ci_runners import part, slurmgate as sg, slurmsite
from tests import slurm_fake

ROOT = pathlib.Path(__file__).resolve().parents[1]
EXAMPLE = (ROOT / 'setup' / 'slurm' / 'site.example.toml').read_text()
KEY = 'o/r/7-1/p1-1'
URL = 'https://github.com/o/r/actions/runs/7'


def request(**kw) -> dict:
    r = {'project': 'p', 'target': 'cpu', 'cards': 0, 'minutes': 10, 'queue_minutes': 10, 'memory': '1G',
         'command': 'echo hello', 'variables': {'PROJECT_VAR': 'v'}, 'run_url': URL, 'host': '3090b',
         'resume': False}
    r.update(kw)
    return r


class Cluster(unittest.TestCase):
    """A cluster account under a temporary root: its account's copy of the example site,
    the project's envs, a fake Slurm on PATH and a fake ssh to it."""
    site_text = EXAMPLE

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = pathlib.Path(self.tmp.name)
        self.base = self.d / 'root' / 'ci-runners'
        (self.base / 'etc').mkdir(parents=True)
        site = slurmsite.load(self.site_text)
        entry = slurmsite.account_entry(site, 'acct1')
        entry.update(root=str(self.d / 'root'), login_python=sys.executable)
        (self.base / 'etc' / 'account.json').write_text(json.dumps(entry))
        for v in ('cpu', 'cuda13'):
            (self.base / 'envs' / f'p-{v}' / 'bin').mkdir(parents=True)
        self.bin = slurm_fake.install(str(self.d / 'bin'))
        (self.d / 'slurm').mkdir()
        (self.d / 'ssh').mkdir()
        (self.d / 'ssh' / 'hosts.json').write_text(json.dumps({'acct1': {'base': str(self.base),
                                                                         'release': str(ROOT)}}))
        env = mock.patch.dict(os.environ, {'PATH': f'{self.bin}:/usr/bin:/bin', 'FAKE_SLURM': str(self.d / 'slurm'),
                                           'FAKE_SSH': str(self.d / 'ssh')})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.stop_jobs)

    def stop_jobs(self):
        """Cancel every job still there and wait for its runner (no process outlives a test)."""
        for p in (self.d / 'slurm' / 'jobs').glob('*.json') if (self.d / 'slurm' / 'jobs').exists() else []:
            j = json.loads(p.read_text())
            slurm_fake.scancel([str(j['id'])])
            j = json.loads(p.read_text())
            end = time.monotonic() + 15
            while j.get('runner') and slurm_fake._alive(j['runner']) and time.monotonic() < end:
                time.sleep(0.05)

    def plan(self, *behaviours):
        (self.d / 'slurm' / 'plan.json').write_text(json.dumps(list(behaviours)))

    def gate(self, command: str, stdin: bytes = b'') -> tuple[int, bytes, str]:
        out, err = io.BytesIO(), io.StringIO()
        with mock.patch('sys.stderr', err):
            rc = sg.main([str(self.base)], env={'SSH_ORIGINAL_COMMAND': command}, stdin=io.BytesIO(stdin),
                         stdout=out)
        return rc, out.getvalue(), err.getvalue()

    def gate_json(self, command: str, stdin: bytes = b'') -> dict:
        rc, out, err = self.gate(command, stdin)
        self.assertEqual(rc, 0, err)
        return json.loads(out)

    def submit(self, **kw) -> dict:
        return self.gate_json(f'ci-slurm-gate submit {KEY}', json.dumps(request(**kw)).encode())

    def wait_end(self, key: str = KEY, timeout: float = 30) -> dict:
        end = time.monotonic() + timeout
        while True:
            st = self.gate_json(f'ci-slurm-gate status {key}')
            if st['state'] not in sg.LIVE_STATES and st['state'] != 'UNKNOWN':
                return st
            self.assertLess(time.monotonic(), end, st)
            time.sleep(0.1)

    def ssh(self, *argv, **kw) -> subprocess.CompletedProcess:
        return subprocess.run([os.path.join(self.bin, 'ssh'), *argv], capture_output=True, timeout=60, **kw)


class Requests(Cluster):
    """A request outside the account's targets, caps or minutes is refused (RUN-9c)."""

    def refused(self, why: str, **kw) -> None:
        rc, _, err = self.gate(f'ci-slurm-gate submit {KEY}', json.dumps(request(**kw)).encode())
        self.assertEqual(rc, 2, err)
        self.assertIn(why, err)
        self.assertFalse((self.d / 'slurm' / 'jobs').exists() and any((self.d / 'slurm' / 'jobs').iterdir()))

    def test_outside_the_accounts_targets(self):
        self.refused('target l40 is not one of this account', target='l40')

    def test_outside_the_cards(self):
        self.refused('takes 1 to 2', target='a40', cards=3)
        self.refused('takes 1 to 2', target='a40', cards=0)
        self.refused('has no cards', target='cpu', cards=1)

    def test_outside_the_minutes_and_the_memory(self):
        self.refused('takes at most 60', target='h100', cards=1, minutes=61)
        self.refused('has 480G a node', memory='481G')

    def test_a_malformed_request(self):
        self.refused('unknown keys', sbatch_args=['--uid=0'])
        self.refused('variables is malformed', variables={'LD_PRELOAD': '/x'})
        self.refused('variables is malformed', variables={'PATH': '/x'})
        self.refused('variables is malformed', variables={'CI_PART_DIR': '/x'})
        self.refused('run_url is malformed', run_url=URL + '\n#SBATCH --uid=0')
        rc, _, err = self.gate(f'ci-slurm-gate submit {KEY}', b'{"project":')
        self.assertEqual(rc, 2)
        self.assertIn('not JSON', err)

    def test_an_env_that_is_not_built(self):
        shutil.rmtree(self.base / 'envs' / 'p-cuda13')
        self.refused('no env p-cuda13', target='a40', cards=1)

    def test_a_key_twice_and_a_bad_key(self):
        self.submit()
        rc, _, err = self.gate(f'ci-slurm-gate submit {KEY}', json.dumps(request()).encode())
        self.assertEqual((rc, 'has a job already' in err), (2, True), err)
        for key in ('o/r/7-1/../p1-1', '../x/7-1/p1-1', 'o/r/7/p1-1', '/o/r/7-1/p1-1', 'o/r/7-1/p1-1/x'):
            rc, _, err = self.gate(f'ci-slurm-gate submit {key}', json.dumps(request()).encode())
            self.assertEqual(rc, 2, key)
            self.assertIn('no part key', err)

    def test_the_verbs_and_no_shell(self):
        for cmd in ('', 'bash -i', 'bash check', 'ci-slurm-gate', 'ci-slurm-gate shell', 'ci-slurm-gate status',
                    'rsync --server . x'):
            rc, _, err = self.gate(cmd)
            self.assertEqual(rc, 2, cmd)


class Render(Cluster):
    """The rendered script: the site's GPU syntax, --time = timeout + 5 min, --export=NONE,
    and no flag from the request verbatim (RUN-9c)."""

    def script(self) -> str:
        return (self.base / 'work' / KEY / 'job.sh').read_text()

    def directives(self) -> dict[str, str]:
        return slurm_fake._directives(str(self.base / 'work' / KEY / 'job.sh'))

    def test_the_directives(self):
        self.plan({'pend': -1})
        self.submit(target='a40', cards=2, minutes=12.5, memory='2G',
                    command='echo "#SBATCH --uid=0"; #SBATCH --export=ALL', variables={'X': '--partition=other'})
        s = self.directives()
        self.assertEqual(s['--gpus'], '2')
        self.assertEqual(s['--time'], '18')  # ceil(12.5) + 5
        self.assertEqual(s['--export'], 'NONE')
        self.assertEqual(s['--signal'], 'B:TERM@120')
        self.assertEqual(s['--job-name'], 'ci-r-p1-1')
        self.assertEqual(s['--account'], 'lab-acct1')
        self.assertEqual(s['--partition'], 'gpu-partition-a')
        self.assertEqual(s['--nodelist'], 'node01')
        self.assertEqual(s['--cpus-per-task'], '16')
        self.assertEqual(s['--mem'], '2048M')
        self.assertEqual(s['--comment'], URL)
        self.assertEqual(s['--output'], str(self.base / 'work' / KEY / 'log'))
        text = self.script()
        self.assertNotIn('--uid', text)  # the command and the variables stay in request.json
        self.assertNotIn('other', text)
        self.assertEqual(text.count('#SBATCH --export='), 1)
        req = json.loads((self.base / 'work' / KEY / 'request.json').read_text())
        self.assertEqual(req['command'], 'echo "#SBATCH --uid=0"; #SBATCH --export=ALL')
        self.assertEqual(oct((self.base / 'work' / KEY).stat().st_mode & 0o777), '0o700')

    def test_a_cpu_target_asks_for_no_card(self):
        self.plan({'pend': -1})
        self.submit()
        s = self.directives()
        self.assertNotIn('--gpus', s)
        self.assertEqual(s['--cpus-per-task'], '8')
        self.assertEqual(s['--time'], '15')
        self.assertNotIn('--nodelist', s)


class GresSite(Cluster):
    site_text = EXAMPLE.replace('gpu_request = "--gpus={n}"', 'gpu_request = "--gres=gpu:{n}"').replace(
        'submit_args = []', 'submit_args = ["--qos=lab-qos"]')

    def test_the_sites_own_gpu_syntax_and_arguments(self):
        account = json.loads((self.base / 'etc' / 'account.json').read_text())
        text = sg.render(account, request(target='a40', cards=2), KEY, '/x', str(ROOT))
        self.assertIn('#SBATCH --gres=gpu:2\n', text)
        self.assertNotIn('--gpus', text)
        self.assertIn('#SBATCH --qos=lab-qos\n', text)


class Stage(Cluster):
    """``stage`` writes only under ``work/`` (RUN-9c)."""

    def rsync(self, src: str, dest: str, verb: str = 'stage', *extra) -> subprocess.CompletedProcess:
        return subprocess.run(['rsync', '-a', '--delete', *extra, '-e', os.path.join(self.bin, 'ssh'),
                               f'--rsync-path=ci-slurm-gate {verb}', src, dest], capture_output=True, text=True,
                              timeout=60)

    def setUp(self):
        super().setUp()
        self.src = self.d / 'src'
        (self.src / 'pkg').mkdir(parents=True)
        (self.src / 'pkg' / 'a.py').write_text('x = 1\n')
        self.outside = self.d / 'root' / 'outside'
        self.outside.mkdir()

    def written_outside_work(self) -> list[str]:
        out = []
        for top, _dirs, files in os.walk(self.d / 'root'):
            for n in files:
                p = pathlib.Path(top, n)
                if not str(p).startswith(str(self.base / 'work') + os.sep) and p.parent != self.base / 'etc':
                    out.append(str(p))
        return out

    def test_the_workspace_lands_in_its_part(self):
        r = self.rsync(f'{self.src}/', f'acct1:{KEY}/ws/')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.base / 'work' / KEY / 'ws' / 'pkg' / 'a.py').read_text(), 'x = 1\n')
        self.assertEqual(self.written_outside_work(), [])
        r = self.rsync(f'{self.src}/', f'acct1:{KEY}/tmp/')
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_anything_else_is_refused(self):
        for dest in (f'acct1:{KEY}/record/', f'acct1:{KEY}/', 'acct1:../etc/', 'acct1:/tmp/x/ws/',
                     f'acct1:{KEY}/../../../../etc/ws/', 'acct1:o/r/7-1/../../x-1/ws/', f'acct1:{KEY}/part/'):
            r = self.rsync(f'{self.src}/', dest)
            self.assertNotEqual(r.returncode, 0, dest)
        r = self.rsync(f'{self.src}/', f'acct1:{KEY}/ws/', 'stage', '--remove-source-files')
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue((self.src / 'pkg' / 'a.py').exists())
        self.assertEqual(self.written_outside_work(), [])
        with self.assertRaisesRegex(sg.GateError, 'only --sender'):
            sg.rsync_argv(str(self.base), 'fetch', ['--server', '-logDtpre.iLsfxCIvu', '.', f'{KEY}/tmp/'])
        with self.assertRaisesRegex(sg.GateError, 'no --sender'):
            sg.rsync_argv(str(self.base), 'stage', ['--server', '--sender', '-l', '.', f'{KEY}/ws/'])
        with self.assertRaisesRegex(sg.GateError, 'not allowed'):
            sg.rsync_argv(str(self.base), 'stage', ['--server', '-l', '--server', '.', f'{KEY}/ws/'])

    def test_a_link_on_the_way_is_refused(self):
        (self.base / 'work' / 'o').mkdir(parents=True)
        os.symlink(self.outside, self.base / 'work' / 'o' / 'r')
        r = self.rsync(f'{self.src}/', f'acct1:{KEY}/ws/')
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(list(self.outside.iterdir()), [])
        os.unlink(self.base / 'work' / 'o' / 'r')
        (self.base / 'work' / KEY).mkdir(parents=True)
        os.symlink(self.outside, self.base / 'work' / KEY / 'ws')
        r = self.rsync(f'{self.src}/', f'acct1:{KEY}/ws/')
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(list(self.outside.iterdir()), [])


class Jobs(Cluster):
    """The batch job end to end on the fake Slurm: the command in its explicit
    environment, the log, the results fetched back, cancel, the memory cap, the P2P
    self-test before two cards."""

    def test_a_job_runs_its_command_and_its_results_come_back(self):
        cmd = ('echo "out $CI_PROJECT $PROJECT_VAR $CUDA_DEVICE_ORDER"; test ! -e "$HOME/.never"; '
               'echo r > "$CI_RESULTS_DIR/r.xml"; echo t > "$RUNNER_TEMP/t.txt"; echo "path $PATH"; '
               'echo "tmp $TMPDIR"; env | grep -c "^FAKE_SLURM=" || true; test -d "$TMPDIR" && pwd')
        j = self.submit(command=cmd)
        st = self.wait_end()
        self.assertEqual((st['job'], st['state'], st['exit']['rc']), (j['job'], 'COMPLETED', 0))
        log = b''
        while True:
            rc, chunk, _ = self.gate(f'ci-slurm-gate log {KEY} {len(log)}')
            if not chunk:
                break
            log += chunk
        text = log.decode()
        self.assertIn('out p v PCI_BUS_ID', text)
        self.assertIn(f'path {self.base}/envs/p-cpu/bin:', text)
        self.assertIn('\n0\n', text)  # --export=NONE: nothing of the submitting environment
        self.assertIn(f'{self.base}/work/{KEY}/ws', text)
        tmp = text.split('tmp ', 1)[1].split('\n', 1)[0]
        self.assertFalse(os.path.exists(tmp))  # the node's 700 directory, gone after the job
        dst = self.d / 'back'
        dst.mkdir()
        for sub in ('part', 'tmp'):
            r = subprocess.run(['rsync', '-a', '-e', os.path.join(self.bin, 'ssh'), '--rsync-path=ci-slurm-gate fetch',
                                f'acct1:{KEY}/{sub}/', f'{dst}/{sub}/'], capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((dst / 'part' / 'results' / 'r.xml').read_text(), 'r\n')
        self.assertEqual((dst / 'tmp' / 't.txt').read_text(), 't\n')
        r = subprocess.run(['rsync', '-a', '-e', os.path.join(self.bin, 'ssh'), '--rsync-path=ci-slurm-gate fetch',
                            f'acct1:{KEY}/record/', f'{dst}/record/'], capture_output=True, text=True, timeout=60)
        self.assertNotEqual(r.returncode, 0)

    def test_a_failing_command_and_the_inner_timeout(self):
        self.submit(command='exit 3')
        st = self.wait_end()
        self.assertEqual((st['state'], st['exit']['rc'], st['exit']['timed_out']), ('FAILED', 3, False))
        self.assertEqual(part.finish('/r/7-1/p1-1', {'attempt': 1}, st['exit'])['outcome'], 'red')

    def test_the_memory_cap_ends_a_part_oom(self):
        self.submit(memory='64M', command='python3 -c "import time; b = b\'x\' * (160 << 20); time.sleep(30)" & wait')
        t0 = time.monotonic()
        st = self.wait_end()
        self.assertLess(time.monotonic() - t0, 20)  # killed, not timed out
        self.assertGreaterEqual(st['exit']['oom_kills'], 1)
        self.assertEqual(part.finish('/r/7-1/p1-1', {'attempt': 1}, st['exit'])['outcome'], 'oom')

    def test_under_the_cap_it_passes(self):
        self.submit(memory='64M', command='python3 -c "b = b\'x\' * (8 << 20)"')
        st = self.wait_end()
        self.assertEqual((st['exit']['rc'], st['exit']['oom_kills']), (0, 0))

    def test_two_cards_run_the_p2p_self_test_first(self):
        self.submit(target='a40', cards=2, command='echo never')
        job = json.loads((self.base / 'work' / KEY / 'job.json').read_text())['job']
        st = self.wait_end()
        self.assertEqual(st['state'], 'COMPLETED')
        self.assertIn('p2p', st['exit'])  # the self-test did not pass on the fake's cards
        self.assertIsNone(st['exit']['rc'])
        out = part.finish('/r/7-1/p1-1', {'attempt': 1}, st['exit'])
        self.assertEqual((out['outcome'], out['next']), ('unavailable', 'none'))
        self.assertGreater(job, 0)

    def test_one_card_runs_no_self_test(self):
        self.submit(target='a40', cards=1, command='echo one')
        st = self.wait_end()
        self.assertNotIn('p2p', st['exit'])
        self.assertEqual(st['exit']['rc'], 0)

    def test_pending_status_and_cancel(self):
        self.plan({'pend': -1, 'reason': 'AssocGrpNodeLimit'})
        self.submit()
        st = self.gate_json(f'ci-slurm-gate status {KEY}')
        self.assertEqual((st['state'], st['reason'], st['exit']), ('PENDING', 'AssocGrpNodeLimit', None))
        self.assertEqual(self.gate_json(f'ci-slurm-gate cancel {KEY}')['cancelled'], True)
        st = self.wait_end()
        self.assertEqual((st['state'], st['by']), ('CANCELLED', 'us'))

    def test_a_running_job_cancelled_by_us_or_by_another_user(self):
        self.submit(command='sleep 30')
        end = time.monotonic() + 15
        while self.gate_json(f'ci-slurm-gate status {KEY}')['state'] != 'RUNNING':
            self.assertLess(time.monotonic(), end)
            time.sleep(0.05)
        time.sleep(0.5)  # the supervisor is up
        self.gate_json(f'ci-slurm-gate cancel {KEY}')
        st = self.wait_end()
        self.assertEqual((st['state'], st['by'], st['exit']['signalled']), ('CANCELLED', 'us', True))
        key2 = 'o/r/7-1/p2-1'
        self.plan({}, {'other_cancel': 1.0})
        self.gate_json(f'ci-slurm-gate submit {key2}', json.dumps(request(command='sleep 30')).encode())
        st = self.wait_end(key2)
        self.assertEqual((st['state'], st['by']), ('CANCELLED', 'other'))

    def test_a_job_that_never_runs(self):
        self.plan({'end': 'NODE_FAIL'})
        self.submit()
        st = self.wait_end()
        self.assertEqual((st['state'], st['exit']), ('NODE_FAIL', None))


class Ledger(Cluster):
    def test_the_months_jobs(self):
        (self.d / 'slurm' / 'ledger.json').write_text(json.dumps([{
            'JobID': '5', 'JobName': 'mine', 'Partition': 'gpu-partition-b', 'State': 'COMPLETED', 'ExitCode': '0:0',
            'ElapsedRaw': '3600', 'NodeList': 'n', 'AllocTRES': 'cpu=16,gres/gpu=2', 'Start': 'x', 'End': 'y',
            'TimelimitRaw': '60'}]))
        self.plan({'pend': -1})
        self.submit()
        led = self.gate_json('ci-slurm-gate ledger 2026-11')
        self.assertEqual(led['month'], '2026-11')
        self.assertEqual(sorted(j['JobName'] for j in led['jobs']), ['ci-r-p1-1', 'mine'])
        self.assertEqual(self.gate('ci-slurm-gate ledger 2026-13')[0], 2)
        self.assertEqual(self.gate('ci-slurm-gate ledger 11-2026')[0], 2)
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, '', '')
        sg.ledger('', run, now=__import__('datetime').datetime(2026, 12, 3))
        self.assertEqual(calls[0][calls[0].index('-S') + 1], '2026-12-01T00:00:00')


class Reap(Cluster):
    def test_a_job_past_its_deadline_is_cancelled_and_old_parts_go(self):
        self.plan({'pend': -1}, {'pend': -1})
        self.submit(minutes=1)  # its deadline: 10 min queue + 1 + 5
        key2 = 'o/r/8-1/p1-1'
        self.gate_json(f'ci-slurm-gate submit {key2}', json.dumps(request(queue_minutes=0, minutes=1)).encode())
        old = self.base / 'work' / 'o' / 'r' / '1-1' / 'gone-1'
        old.mkdir(parents=True)
        (old / 'log').write_text('x')
        t = time.time() - 4 * 86400
        os.utime(old / 'log', (t, t))
        os.utime(old, (t, t))
        res = sg.reap(str(self.base), now=time.time() + 7 * 60)  # past key2's deadline (0 + 1 + 5 min), not KEY's
        self.assertEqual(res, {'cancelled': [1002], 'removed': 1})
        self.assertFalse((self.base / 'work' / 'o' / 'r' / '1-1').exists())
        self.assertEqual(self.gate_json(f'ci-slurm-gate status {key2}')['state'], 'CANCELLED')
        self.assertEqual(self.gate_json(f'ci-slurm-gate status {KEY}')['state'], 'PENDING')
        self.assertTrue((self.base / 'work' / KEY).exists())  # a live job's part stays, however old
        res = sg.reap(str(self.base), now=time.time() + 4 * 86400)
        self.assertEqual(res['removed'], 1)  # key2's, its job gone; KEY's job still pends
        self.assertTrue((self.base / 'work' / KEY).exists())


class Check(Cluster):
    def test_the_facts(self):
        facts = self.gate_json('ci-slurm-gate check')
        self.assertEqual(facts['slurm'], [24, 5, 3])
        self.assertEqual(facts['python'][:2], list(sys.version_info[:2]))
        self.assertTrue(all(facts['commands'][c] for c in ('sbatch', 'squeue', 'sacct', 'scancel', 'sinfo')))
        self.assertTrue(facts['writable'])
        self.assertEqual(sorted(facts['targets']), ['a40', 'cpu', 'h100'])
        self.assertTrue(all(t['test_only'] and t['partition_up'] for t in facts['targets'].values()))
        self.assertEqual(facts['envs'], ['p-cpu', 'p-cuda13'])
        self.assertEqual(list((self.base / 'work').iterdir()) if (self.base / 'work').exists() else [], [])

    def test_a_wrong_gpu_syntax_and_a_down_partition_show(self):
        account = json.loads((self.base / 'etc' / 'account.json').read_text())
        account['gpu_request'] = '--gres=gpu:{n}'
        (self.base / 'etc' / 'account.json').write_text(json.dumps(account))
        (self.d / 'slurm' / 'down.json').write_text('["gpu-partition-b"]')
        t = self.gate_json('ci-slurm-gate check')['targets']
        self.assertEqual((t['a40']['test_only'], t['cpu']['test_only']), (False, True))
        self.assertEqual((t['h100']['partition_up'], t['cpu']['partition_up']), (False, True))


class Wrapper(Cluster):
    def test_the_forced_command_runs_the_gate_of_the_hook_release(self):
        rel = self.base / 'releases' / 'v9.9.9'
        rel.mkdir(parents=True)
        for n in ('bin', 'ci_runners'):
            shutil.copytree(ROOT / n, rel / n)
        os.symlink('v9.9.9', self.base / 'releases' / 'hook')
        py = self.d / 'py.sh'  # the interpreter etc/python names, which leaves a mark
        py.write_text(f'#!/bin/sh\ntouch {self.d}/mark\nexec {sys.executable} "$@"\n')
        py.chmod(0o755)
        (self.base / 'etc' / 'python').write_text(f'{py}\n')
        gate = self.base / 'releases' / 'hook' / 'bin' / 'ci-slurm-gate'
        r = subprocess.run([str(gate)], capture_output=True, text=True, timeout=60,
                           env=dict(os.environ, SSH_ORIGINAL_COMMAND='ci-slurm-gate check'))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('"targets"', r.stdout)
        self.assertTrue((self.d / 'mark').exists())
        r = subprocess.run([str(gate)], capture_output=True, text=True, timeout=60, env=dict(os.environ))
        self.assertEqual(r.returncode, 2)
        self.assertIn('usage', r.stderr)


if __name__ == '__main__':
    unittest.main()
