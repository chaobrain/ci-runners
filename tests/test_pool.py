"""The machine registry, ``pool status``, the claim, the env check and ``pool run``
(decision 15.1; tasks ci-cluster-pool, ci-pool-run)."""
from __future__ import annotations

import dataclasses
import fcntl
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
import unittest.mock

from ci_runners import pool
from ci_runners import slots, slurmgate

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

REGISTRY = textwrap.dedent('''\
    schema = 1
    project = "proj"
    repository = "Owner/Proj"
    check = ["numpy"]

    [machine.hosta]
    access = "ssh"
    ssh = "alias-a"
    hostname = "host-a"
    cpus = 16
    memory_gib = 64
    gpus = 2
    gpu_model = "3090"
    gpu_mib = 24576
    p2p = true
    python = "/opt/env/bin/python"
    work = "/var/pool-work"
    slots = { lock = "/tmp/x.lock", count = 2 }
    labels = { cpu = "hosta-cpu" }
    numerics_ok = true

    [machine.hostb]
    access = "local"
    hostname = "host-b"
    cpus = 16
    memory_gib = 64
    python = "/opt/env/bin/python"
    work = "/var/pool-work"
    slots = { lock = "/tmp/x.lock", count = 1 }

    [machine.cl]
    access = "slurm"
    ssh = "alias-c"
    gate = "/fs/root/ci-runners/releases/hook/bin/ci-slurm-gate"

    [machine.cl.targets.cpu]
    partition = "part_c"
    cpus = 32
    memory_gib = 1900
    max_minutes = 90
    variant = "cpu"
    price = 0.04

    [machine.cl.targets.v100]
    partition = "part_v"
    cpus = 10
    memory_gib = 470
    max_minutes = 45
    variant = "cuda12"
    gpu_model = "v100"
    cards = 2
    max_cards = 2
    gpu_mib = 32768
    price = 1.6

    [machine.cl.targets.a40]
    partition = "part_a"
    cpus = 8
    memory_gib = 470
    max_minutes = 45
    variant = "cuda12"
    gpu_model = "a40"
    cards = 4
    max_cards = 2
    gpu_mib = 46068
    price = 4.5
''')


def reg():
    return pool.load(REGISTRY)


class Registry(unittest.TestCase):
    def test_a_registry_loads(self):
        r = reg()
        self.assertEqual(list(r.machines), ['hosta', 'hostb', 'cl'])
        self.assertEqual(r.machines['hosta'].slots, pool.Slots('/tmp/x.lock', 2))
        self.assertTrue(r.machines['cl'].billed)  # a cluster is billed unless it says otherwise
        self.assertFalse(r.machines['hosta'].billed)
        self.assertEqual(r.machines['cl'].base, '/fs/root/ci-runners')
        self.assertEqual(r.machines['cl'].targets['v100'].max_cards, 2)

    def test_malformed_entries_are_refused(self):
        for bad, why in [(REGISTRY.replace('schema = 1', 'schema = 2'), 'schema'),
                         (REGISTRY.replace('ssh = "alias-a"\n', ''), 'ssh alias'),
                         (REGISTRY.replace('"/opt/env/bin/python"', '"env/bin/python"', 1), 'absolute'),
                         (REGISTRY.replace('cpus = 16\n', 'cpus = 16\ncolour = "red"\n', 1), 'unknown keys'),
                         (REGISTRY.replace('access = "local"', 'access = "ftp"'), 'access'),
                         (REGISTRY.replace('count = 1', 'count = 0'), 'count'),
                         (REGISTRY.replace('max_cards = 2\ngpu_mib = 32768', 'gpu_mib = 32768'), 'max_cards')]:
            with self.subTest(why):
                with self.assertRaisesRegex(pool.RegistryError, why):
                    pool.load(bad)

    def test_step_2s_keys(self):
        """The project's cap, the drain copy and the shared leases (decision 15.1, step 2)."""
        text = REGISTRY.replace('slots = { lock = "/tmp/x.lock", count = 2 }\n',
                                'slots = { lock = "/tmp/x.lock", count = 2 }\nmax_slots = 1\n'
                                'drain = "/data/ci-runners/drain.json"\nleases = { dir = "/tmp/leases", parts = 1 }\n')
        m = pool.load(text).machines['hosta']
        self.assertEqual((m.max_slots, m.drain, m.leases), (1, '/data/ci-runners/drain.json', pool.LeaseDir('/tmp/leases', 1)))
        b = reg().machines['hostb']
        self.assertEqual((b.max_slots, b.drain, b.leases), (None, '', None))  # none: as step 1
        for bad, why in [(text.replace('parts = 1', 'parts = 0'), 'parts must be 1'),
                         (text.replace('parts = 1', 'parts = 2'), 'parts must be 1'),
                         (text.replace('max_slots = 1', 'max_slots = 0'), 'max_slots: from 1'),
                         (text.replace('max_slots = 1', 'max_slots = 3'), 'max_slots: from 1'),
                         (text.replace('dir = "/tmp/leases"', 'dir = "leases"'), 'absolute'),
                         (text.replace('parts = 1', 'parts = 1, x = 1'), 'unknown keys'),
                         (text.replace('drain = "/data', 'drain = "data'), 'absolute'),
                         (text.replace('gpus = 2\n', 'gpus = 0\n', 1), 'on a machine with gpus')]:
            with self.subTest(why):
                with self.assertRaisesRegex(pool.RegistryError, why):
                    pool.load(bad)
        spec = pool.host_spec(pool.load(text), m, pool.RunArgs('true', ROOT, pool.Ask(cards=1)), wait=True)
        self.assertEqual({k: spec[k] for k in ('max_slots', 'drain', 'leases', 'lease_parts', 'gpus', 'cards')},
                         {'max_slots': 1, 'drain': m.drain, 'leases': '/tmp/leases', 'lease_parts': 1, 'gpus': 2, 'cards': 1})
        import tomllib
        toks = pool.deny_tokens(tomllib.loads(text))
        self.assertIn('data/ci-runners/drain.json', toks)
        self.assertIn('tmp/leases', toks)

    def test_the_runners_key(self):
        """A host's ``runners`` (decision 15.5): absolute, private, and passed to the probe of a
        local machine (what a forced probe's caller reads)."""
        text = REGISTRY.replace('slots = { lock = "/tmp/x.lock", count = 2 }\n',
                                'slots = { lock = "/tmp/x.lock", count = 2 }\nrunners = "/data/ci/actions-runner"\n')
        self.assertEqual(pool.load(text).machines['hosta'].runners, '/data/ci/actions-runner')
        self.assertEqual(reg().machines['hostb'].runners, '')
        with self.assertRaisesRegex(pool.RegistryError, 'absolute'):
            pool.load(text.replace('"/data/ci/actions-runner"', '"actions-runner"'))
        import tomllib
        self.assertIn('data/ci/actions-runner', pool.deny_tokens(tomllib.loads(text)))
        local = CI_REGISTRY.replace('slots = { lock = "/tmp/x.lock", count = 1 }',
                                    'slots = { lock = "/tmp/x.lock", count = 1 }\nrunners = "/data/ci/actions-runner"')
        seen = []
        with unittest.mock.patch.object(pool, 'probe', lambda *a: seen.append(a) or {}):
            pool.local_probe(pool.load(local))
        self.assertEqual(seen[0][-1], '/data/ci/actions-runner')

    def test_a_registry_others_may_write_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'pool.toml')
            with open(p, 'w') as f:
                f.write(REGISTRY)
            os.chmod(p, 0o620)
            with self.assertRaisesRegex(pool.RegistryError, 'writable'):
                pool.load_file(p)
            os.chmod(p, 0o600)
            self.assertEqual(pool.load_file(p).project, 'proj')

    def test_deny_tokens_hold_the_inventory_not_the_labels(self):
        import tomllib
        toks = pool.deny_tokens(tomllib.loads(REGISTRY))
        self.assertIn('alias-a', toks)
        self.assertIn('host-a', toks)
        self.assertIn('part_v', toks)
        self.assertNotIn('hosta-cpu', toks)
        self.assertNotIn('3090', toks)


# ------------------------------------------------------------------ the claim: one slot, a CI part and a pool run


def _python_holding(lock, n, cmd, project='other'):
    """A ``ci-slot`` process (the parts' claim) running ``cmd`` while it holds a slot."""
    code = ('import sys; sys.path.insert(0, sys.argv.pop(1)); from ci_runners.slots import main; '
            'sys.exit(main(sys.argv[1:]))')
    return subprocess.Popen([sys.executable, '-I', '-c', code, ROOT, '--lock', lock, '--slots', str(n), '--project',
                             project, '--default', project, '--poll', '0.1', *cmd],
                            stderr=subprocess.PIPE, text=True)


def _wait_holding(p):
    line = p.stderr.readline()
    assert 'holding' in line, line


def _command_alive(pidf, marker):
    """Whether the ``bash -c`` command whose pid is in ``pidf`` runs: that pid, its command
    line naming ``marker`` (a path of the test's own), so a reused pid is not it."""
    try:
        with open(pidf) as f:
            pid = int(f.read())
        with open(f'/proc/{pid}/cmdline', 'rb') as f:
            return marker.encode() in f.read() and pid
    except (OSError, ValueError):
        return False


def _end_command(pidf, marker):
    """Kill the process group of a command a failing run left running (it is a session
    leader: ``supervise`` starts it in one)."""
    pid = _command_alive(pidf, marker)
    if pid:
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass


def _until(cond, timeout=20.0):
    deadline = time.time() + timeout
    while not cond():
        if time.time() > deadline:
            return False
        time.sleep(0.05)
    return True


class Claim(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.lock = os.path.join(self.d, 'slot.lock')
        open(self.lock, 'w').close()

    def tearDown(self):
        subprocess.run(['rm', '-rf', self.d])

    def spec(self, cmd, **kw):
        s = {'lock': self.lock, 'count': 1, 'queue': '', 'project': 'proj', 'command': cmd, 'python': sys.executable,
             'memory_gib': 4, 'cards': 0, 'need_mib': 0, 'timeout_seconds': 60, 'rounds': 0,
             'queue_seconds': 30, 'poll': 0.1, 'scope': False}
        s.update(kw)
        return s

    def remote(self, spec, env=None, pre=''):
        """``pool.remote`` in a child process, its run directory under ``self.d``; its stdin
        stays open, as ``run_host``'s ssh keeps it. ``pre``: code run first (a fault)."""
        run_dir = tempfile.mkdtemp(prefix='pool-', dir=self.d)
        os.makedirs(os.path.join(run_dir, 'ws'))
        code = ('import sys; sys.path.insert(0, sys.argv.pop(1)); from ci_runners.pool import remote; '
                f'{pre + "; " if pre else ""}sys.exit(remote(sys.argv[1:]))')
        out, err = (open(os.path.join(self.d, f'{n}.{os.path.basename(run_dir)}'), 'w+') for n in ('out', 'err'))
        p = subprocess.Popen([sys.executable, '-I', '-c', code, ROOT, run_dir, json.dumps(spec)],
                             cwd=os.path.join(run_dir, 'ws'), stdin=subprocess.PIPE, stdout=out, stderr=err, text=True,
                             env=env)
        p.files = (out, err)
        return p, run_dir

    @staticmethod
    def finish(p, timeout=60):
        """Wait for ``p`` with its stdin open, then its ``(stdout, stderr)``."""
        p.wait(timeout=timeout)
        p.stdin.close()
        texts = []
        for f in p.files:
            f.seek(0)
            texts.append(f.read())
            f.close()
        return texts

    def test_a_ci_part_holding_the_one_slot_keeps_a_pool_run_out(self):
        marker = os.path.join(self.d, 'ran')
        part = _python_holding(self.lock, 1, ['sleep', '30'])
        try:
            _wait_holding(part)
            p = pool.probe(self.lock, 1, '')
            self.assertEqual(p['slots_free'], 0)
            r, run_dir = self.remote(self.spec(f'touch {marker}', rounds=2))
            out, err = self.finish(r)
            self.assertEqual(r.returncode, pool.NOT_STARTED, err)
            self.assertIn('no CI slot free', err)
            self.assertFalse(os.path.exists(marker))
            self.assertFalse(os.path.exists(run_dir))  # the run's directory goes either way
        finally:
            part.kill()
            part.wait()

    def test_a_pool_run_waits_in_line_and_runs_when_the_part_ends(self):
        part = _python_holding(self.lock, 1, ['sleep', '2'])
        _wait_holding(part)
        r, _ = self.remote(self.spec('echo ran; exit 7'))
        out, err = self.finish(r)
        part.wait()
        self.assertIn('waiting in line', err)
        self.assertIn('ran', out)
        self.assertEqual(r.returncode, 7)  # the command's code

    def test_a_ci_part_waits_while_a_pool_run_holds_the_slot(self):
        go = os.path.join(self.d, 'go')
        r, _ = self.remote(self.spec(f'while [ ! -e {go} ]; do sleep 0.1; done'))
        try:
            deadline = time.time() + 20
            while pool.probe(self.lock, 1, '')['slots_free'] and time.time() < deadline:
                time.sleep(0.1)
            self.assertEqual(pool.probe(self.lock, 1, '')['slots_free'], 0)
            q = slots.SlotQueue(self.lock, 1, poll=0.1, default_project='proj')
            rc = q.hold('other', {}, ['true'], deadline=time.time() + 1)  # returns only on its queue timeout
            self.assertEqual(rc, 4)
        finally:
            open(go, 'w').close()
            self.finish(r)

    def test_a_closed_stdin_ends_the_command_and_frees_the_slot(self):
        marker = os.path.join(self.d, 'late')
        r, _ = self.remote(self.spec(f'sleep 20; touch {marker}'))
        deadline = time.time() + 20
        while pool.probe(self.lock, 1, '')['slots_free'] and time.time() < deadline:
            time.sleep(0.1)
        t0 = time.time()
        r.stdin.close()  # the caller is gone
        r.wait(timeout=30)
        self.assertLess(time.time() - t0, 15)
        self.assertEqual(r.returncode, pool.EXIT_SIGNALLED)
        self.assertEqual(pool.probe(self.lock, 1, '')['slots_free'], 1)
        time.sleep(0.5)
        self.assertFalse(os.path.exists(marker))

    def test_the_run_gets_its_env_and_no_cards(self):
        r, _ = self.remote(self.spec('echo "[$CUDA_VISIBLE_DEVICES] $CI_PROJECT $FOO_X $CONDA_PREFIX"',
                                     variables={'FOO_X': 'kept'}))
        out, err = self.finish(r)
        self.assertEqual(r.returncode, 0, err)
        self.assertEqual(out.strip(), f'[] proj kept {os.path.dirname(os.path.dirname(sys.executable))}')

    def test_the_timeout_ends_it(self):
        r, _ = self.remote(self.spec('sleep 30', timeout_seconds=1))
        self.finish(r)
        self.assertEqual(r.returncode, pool.EXIT_TIMEOUT)

    def test_a_killed_host_side_leaves_the_slot_held_until_the_command_ends(self):
        """The supervisor is a process of its own that holds the slot: a host side killed
        mid-command (SIGKILL: no handler, no finally) never leaves a running command
        without its slot, which would let another run overbook the host."""
        started, go, done = (os.path.join(self.d, n) for n in ('started', 'go', 'done'))
        r, run_dir = self.remote(self.spec(f'touch {started}; while [ ! -e {go} ]; do sleep 0.1; done; touch {done}'))
        alive = os.path.join(run_dir, pool.ALIVE)
        try:
            self.assertTrue(_until(lambda: os.path.exists(started)))
            r.kill()
            r.wait()
            time.sleep(0.5)
            self.assertEqual(pool.probe(self.lock, 1, '')['slots_free'], 0)  # the supervisor holds it
            self.assertTrue(slots._flocked(alive))  # and its run directory counts as alive
            self.assertFalse(os.path.exists(done))
            with open(os.path.join(self.d, 'slot.queue', 'holder.1')) as f:  # the holder is the supervisor
                pid, _, project = f.read().split()
            self.assertNotEqual(int(pid), r.pid)
            os.kill(int(pid), 0)  # alive: its project counts against the cap while the command runs
            self.assertEqual(project, 'proj')
        finally:
            open(go, 'w').close()
            self.finish(r)
        self.assertTrue(_until(lambda: pool.probe(self.lock, 1, '')['slots_free'] == 1))  # freed at the command's end
        self.assertTrue(os.path.exists(done))
        self.assertFalse(slots._flocked(alive))

    def test_a_host_side_that_fails_keeps_the_directory_of_a_running_supervisor(self):
        """The host side failing after the supervisor started (here: a fault just after)
        returns, but the run's directory stays while the supervisor and its command run."""
        started, go = os.path.join(self.d, 'started'), os.path.join(self.d, 'go')
        pre = ('import ci_runners.pool as P; o = P._start_supervisor; '
               'P._start_supervisor = lambda *a: (o(*a), (_ for _ in ()).throw(RuntimeError("after")))[1]')
        r, run_dir = self.remote(self.spec(f'touch {started}; while [ ! -e {go} ]; do sleep 0.1; done; '
                                           f'echo end > {self.d}/ended'), pre=pre)
        try:
            self.assertTrue(_until(lambda: os.path.exists(started)))
            out, err = self.finish(r)
            self.assertEqual(r.returncode, pool.EXIT_FAILED, err)
            self.assertTrue(os.path.isdir(run_dir))
            self.assertTrue(slots._flocked(os.path.join(run_dir, pool.ALIVE)))
        finally:
            open(go, 'w').close()
        self.assertTrue(_until(lambda: os.path.exists(os.path.join(self.d, 'ended'))))
        self.assertTrue(_until(lambda: not slots._flocked(os.path.join(run_dir, pool.ALIVE))))

    def test_a_signal_while_the_supervisor_starts_stops_its_command(self):
        """A signal that lands inside the supervisor's Popen (its process made, not yet in
        ``started``) waits until it is known, then stops it: never a command left running
        without its slot, its directory removed under it. The fault sends it once the
        command runs (it writes ``started``), so a run that does not stop it leaves it
        running whatever the host's load."""
        started, pidf = os.path.join(self.d, 'started'), os.path.join(self.d, 'cmd.pid')
        fault = ('import os, signal, subprocess, time, ci_runners.pool as P\n'
                 'class W(subprocess.Popen):\n'
                 '    sent = False\n'
                 '    def __init__(self, *a, **k):\n'
                 '        super().__init__(*a, **k)\n'
                 '        if not W.sent:\n'
                 '            W.sent = True\n'
                 '            end = time.monotonic() + 60\n'
                 f'            while not os.path.exists({started!r}) and time.monotonic() < end:\n'
                 '                time.sleep(0.02)\n'
                 '            os.kill(os.getpid(), signal.SIGTERM)\n'
                 'P.subprocess.Popen = W\n')
        r, run_dir = self.remote(self.spec(f'echo $$ > {pidf}; touch {started}; sleep 30'), pre=f'exec({fault!r})')
        self.addCleanup(_end_command, pidf, started)
        out, err = self.finish(r, timeout=pool.STOP_GRACE + 30)
        self.assertEqual(r.returncode, pool.EXIT_SIGNALLED, err)
        self.assertTrue(os.path.exists(started), err)  # the signal came while the command ran
        self.assertFalse(_command_alive(pidf, started))  # stopped (and reaped) with its run
        self.assertEqual(pool.probe(self.lock, 1, '')['slots_free'], 1)  # the supervisor is gone
        self.assertFalse(os.path.exists(run_dir))

    def fake_systemd(self, rc=None):
        """An environment whose user manager answers (a runtime directory with a bus) and
        whose ``systemd-run`` is a fake: it logs its arguments to ``<itself>.log``, then
        runs the command after its options (holding what it was passed), or exits ``rc``."""
        b, rt = os.path.join(self.d, 'bin'), os.path.join(self.d, 'rt')
        os.makedirs(b)
        os.makedirs(rt)
        open(os.path.join(rt, 'bus'), 'w').close()
        body = (f'exit {rc}' if rc is not None else
                'while [ $# -gt 0 ]; do case "$1" in -p) shift 2;; -*) shift;; *) break;; esac; done\nexec "$@"')
        exe = os.path.join(b, 'systemd-run')
        with open(exe, 'w') as f:
            f.write(f'#!/bin/sh\nprintf "%s\\n" "$@" > "$0.log"\n{body}\n')
        os.chmod(exe, 0o755)
        return {**os.environ, 'PATH': f'{b}:{os.environ["PATH"]}', 'XDG_RUNTIME_DIR': rt}, exe + '.log'

    def test_the_supervisor_runs_in_a_scope_where_the_user_manager_answers(self):
        env, log = self.fake_systemd()
        r, run_dir = self.remote(self.spec(f'echo ran; [ -e {self.lock} ]', scope=True), env=env)
        out, err = self.finish(r)
        self.assertEqual((r.returncode, out.strip()), (0, 'ran'), err)
        with open(log) as f:
            args = f.read().splitlines()
        self.assertEqual(args[:3], ['--user', '--scope', '--quiet'])
        self.assertIn(f'--unit=ci-part-pool-proj-{os.path.basename(run_dir)}.scope', args)
        self.assertIn('MemoryMax=4G', args)  # the run's memory
        self.assertIn('ci_runners.part import supervise', ' '.join(args))

    def test_without_a_scope_the_supervisor_runs_in_a_session_of_its_own(self):
        env, log = self.fake_systemd(rc=1)
        r, _ = self.remote(self.spec('echo ran', scope=True), env=env)
        out, err = self.finish(r)
        self.assertEqual((r.returncode, out.strip()), (0, 'ran'), err)
        self.assertIn('no systemd scope (systemd-run exit 1)', err)
        self.assertTrue(os.path.exists(log))

    def test_the_project_at_its_cap_keeps_a_pool_run_waiting_though_a_slot_is_free(self):
        open(os.path.join(self.d, 'slot.2.lock'), 'w').close()
        marker = os.path.join(self.d, 'ran')
        part = _python_holding(self.lock, 2, ['sleep', '30'], project='proj')  # the project's own part
        try:
            _wait_holding(part)
            self.assertEqual(pool.probe(self.lock, 2, '')['slots_free'], 1)
            r, _ = self.remote(self.spec(f'touch {marker}', count=2, max_slots=1, rounds=3))
            out, err = self.finish(r)
            self.assertEqual(r.returncode, pool.NOT_STARTED, err)
            self.assertIn('no CI slot free on this host now', err)
            self.assertFalse(os.path.exists(marker))
            r, _ = self.remote(self.spec(f'touch {marker}', count=2, max_slots=2, rounds=3))
            out, err = self.finish(r)
            self.assertEqual(r.returncode, 0, err)
            self.assertTrue(os.path.exists(marker))
        finally:
            part.kill()
            part.wait()

    def test_a_drained_host_starts_no_pool_run(self):
        state, marker = os.path.join(self.d, 'drain.json'), os.path.join(self.d, 'ran')
        with open(state, 'w') as f:
            f.write('{"host": 100.0, "pools": {}}')
        r, run_dir = self.remote(self.spec(f'touch {marker}', drain=state))
        out, err = self.finish(r)
        self.assertEqual(r.returncode, pool.NOT_STARTED, err)
        self.assertIn('this host is drained', err)
        self.assertFalse(os.path.exists(marker))
        self.assertFalse(os.path.exists(run_dir))
        with open(state, 'w') as f:
            f.write('{"host": null, "pools": {"cards": 1.0}}')  # a pool of runners only
        r, _ = self.remote(self.spec(f'touch {marker}', drain=state))
        self.finish(r)
        self.assertEqual(r.returncode, 0)
        with open(state, 'w') as f:  # a GPU run waits for no card on a drained host either
            f.write('{"host": 100.0, "pools": {}}')
        os.remove(marker)
        r, _ = self.remote(self.spec(f'touch {marker}', drain=state, cards=1, leases=os.path.join(self.d, 'leases'),
                                     lease_parts=1, gpus=2))
        out, err = self.finish(r)
        self.assertEqual(r.returncode, pool.NOT_STARTED, err)
        self.assertIn('this host is drained', err)
        self.assertFalse(os.path.exists(marker))

    def test_a_gpu_run_takes_its_cards_by_the_parts_leases(self):
        """A GPU pool run takes a card the parts' leases give it (no CI slot), holds the
        lease for the command's life, and gives the host up when no card is to be had."""
        from ci_runners import part as part_mod
        b, leases = os.path.join(self.d, 'smi'), os.path.join(self.d, 'leases')
        os.makedirs(b)
        with open(os.path.join(b, 'nvidia-smi'), 'w') as f:
            f.write('#!/bin/sh\necho "0, 24576, 576"\necho "1, 24576, 576"\n')
        os.chmod(os.path.join(b, 'nvidia-smi'), 0o755)
        env = {**os.environ, 'PATH': f'{b}:{os.environ["PATH"]}'}
        held = part_mod.Leases(leases, shared=True)  # a part holds card 0
        self.assertEqual(held.try_take(1, [0], {0: 24000}, 0), [0])
        go = os.path.join(self.d, 'go')
        try:
            spec = self.spec(f'echo "[$CUDA_VISIBLE_DEVICES]"; while [ ! -e {go} ]; do sleep 0.1; done', cards=1,
                             need_mib=6000, leases=leases, lease_parts=1, gpus=2)
            r, _ = self.remote(spec, env=env)
            self.assertTrue(_until(lambda: slots._flocked(os.path.join(leases, 'card1.0.lock'))))
            self.assertEqual(pool.probe(self.lock, 1, '')['slots_free'], 1)  # no CI slot taken
            r2, _ = self.remote(dict(spec, rounds=2), env=env)  # both cards leased now
            out2, err2 = self.finish(r2)
            self.assertEqual(r2.returncode, pool.NOT_STARTED, err2)
            open(go, 'w').close()
            out, err = self.finish(r)
            self.assertEqual((r.returncode, out.strip()), (0, '[1]'), err)
            self.assertFalse(slots._flocked(os.path.join(leases, 'card1.0.lock')))
        finally:
            open(go, 'w').close()
            held.release()


class Probe(unittest.TestCase):
    def test_free_slots_are_read_without_taking_them(self):
        with tempfile.TemporaryDirectory() as d:
            lock = os.path.join(d, 's.lock')
            for p in (lock, os.path.join(d, 's.2.lock')):
                open(p, 'w').close()
            os.makedirs(os.path.join(d, 's.queue'))
            open(os.path.join(d, 's.queue', '000000000001.ticket'), 'w').close()
            fd = os.open(lock, os.O_RDONLY)
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                p = pool.probe(lock, 2, '')
            finally:
                os.close(fd)
            self.assertEqual((p['slots_free'], p['slots_missing'], p['queue']), (1, 0, 1))
            self.assertEqual(pool.probe(lock, 3, '')['slots_missing'], 1)

    def test_a_host_row_has_the_hoststate_fields(self):
        m = reg().machines['hosta']
        row = pool.host_row(m, {'slots_free': 1, 'queue': 2, 'load': 5.2, 'cpus': 16, 'mem_free_gib': 40,
                                'gpu_free': {'0': 20000}}, here='host-a')
        for k in ('labels', 'problems', 'queue', 'gpu_free', 'multi_gpu', 'gpu_model', 'cuda', 'cpus_free', 'billed',
                  'price', 'target', 'numerics_ok', 'start_now'):
            self.assertIn(k, row)
        self.assertEqual((row['cpus_free'], row['queue'], row['gpu_free'], row['here']), (10, 2, {0: 20000}, True))
        busy = pool.host_row(m, {'slots_free': 0, 'load': 1.0, 'cpus': 16})
        self.assertEqual(busy['cpus_free'], 0)  # no slot: no cores to place on
        self.assertEqual(pool.host_row(m, None, 'unreachable (exit 255)')['problems'], ['unreachable (exit 255)'])

    def test_a_drained_host_and_leased_cards(self):
        with tempfile.TemporaryDirectory() as d:
            lock, state, leases = os.path.join(d, 's.lock'), os.path.join(d, 'drain.json'), os.path.join(d, 'leases')
            open(lock, 'w').close()
            os.makedirs(leases)
            self.assertFalse(pool.probe(lock, 1, '', state)['drained'])  # missing: not drained
            with open(state, 'w') as f:
                f.write('{"host": 1.0, "pools": {}}')
            fd = os.open(os.path.join(leases, 'card1.0.lock'), os.O_WRONLY | os.O_CREAT)
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                p = pool.probe(lock, 1, '', state, leases, 1, 2)
            finally:
                os.close(fd)
            self.assertEqual((p['drained'], p['cards_leased']), (True, {0: 0, 1: 1}))
        m = dataclasses.replace(reg().machines['hosta'], leases=pool.LeaseDir('/tmp/l', 1))
        row = pool.host_row(m, {'slots_free': 1, 'load': 0, 'cpus': 16, 'drained': True, 'gpu_free': {'0': 20000, '1': 20000},
                                'cards_leased': {'0': 0, '1': 1}})
        self.assertEqual(row['problems'], ['drained'])
        self.assertEqual(row['gpu_free'], {0: 20000})  # card 1's one lease is held
        self.assertIn('drained', pool.fits(row, pool.Ask(), m, 0))

    @staticmethod
    def fake_proc(d, procs, mount='proc rw'):
        """A /proc with one directory per process, its cmdline the given argv, and a
        mountinfo whose /proc line ends with ``mount``."""
        proc = os.path.join(d, 'proc')
        os.makedirs(os.path.join(proc, 'self'))
        with open(os.path.join(proc, 'self', 'mountinfo'), 'w') as f:
            f.write(f'22 1 0:21 / /proc rw,nosuid shared:12 - {mount}\n'
                    '23 22 0:22 / /proc/sys/fs/binfmt_misc rw - binfmt_misc binfmt_misc rw,hidepid=2\n')
        for pid, argv in enumerate(procs, 100):
            os.makedirs(os.path.join(proc, str(pid)))
            with open(os.path.join(proc, str(pid), 'cmdline'), 'wb') as f:
                f.write(b'\0'.join(a.encode() for a in argv) + b'\0')
        os.makedirs(os.path.join(proc, '999'))  # gone before its cmdline was read
        return proc

    def test_the_runners_with_a_live_process(self):
        """Decision 15.5: a runner is alive with its listener (argv[0]) or its run-helper.sh
        (argv[1], after its shell), right under the runners' directory; nothing else counts."""
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(d, 'actions-runner')
            os.makedirs(root)
            proc = self.fake_proc(d, [
                [f'{root}/hx-gpu0/bin/Runner.Listener', 'run'],
                ['/bin/bash', f'{root}/hx-light-1/run-helper.sh'],
                ['/bin/bash', f'{root}/hx-gpu0/run-helper.sh'],  # the same runner twice
                [f'{root}/hx-part-1/bin/Runner.Worker', 'spawnclient'],  # a worker is no listener
                [f'{root}2/hx-part-2/bin/Runner.Listener', 'run'],  # another directory with the same prefix
                [f'{root}/hx-part-3/x/bin/Runner.Listener', 'run'],  # not right under it
                [f'{root}//bin/Runner.Listener', 'run'],  # no name
                ['/bin/sh', '-c', f'{root}/hx-part-4/run-helper.sh'],  # argv[2]: a command line naming it
                ['python3', f'{root}/hx-part-5/bin/Runner.Listener'],  # a listener's path at argv[1]: not one
                [f'{root}/hx-part-6/run-helper.sh'],  # a helper's path at argv[0]: not one
            ])
            for r in (root, root + '/'):
                self.assertEqual(pool.runners_alive(r, proc), {'hx-gpu0', 'hx-light-1'})
            link = os.path.join(d, 'link')  # the runners start from their real path: a linked root still counts
            os.symlink(root, link)
            real = self.fake_proc(os.path.join(d, 'r'), [[f'{os.path.realpath(root)}/hx-gpu1/bin/Runner.Listener']])
            self.assertEqual(pool.runners_alive(link, real), {'hx-gpu1'})
            st = os.stat(root)  # another account's process naming the path does not count
            with unittest.mock.patch('os.stat', lambda p, _s=os.stat: _s(p) if p == root else
                                     os.stat_result((st.st_mode, 0, 0, 0, st.st_uid + 1, 0, 0, 0, 0, 0))):
                self.assertEqual(pool.runners_alive(root, proc), set())
            self.assertEqual(pool.runners_alive(root, os.path.join(d, 'none')), None)  # no /proc: not known
            empty = self.fake_proc(os.path.join(d, 'e'), [])
            self.assertEqual(pool.runners_alive(root, empty), set())

    def test_a_proc_that_hides_other_accounts_processes(self):
        """With hidepid, only the account that owns the runners' directory can count them;
        anyone else gets None (not known), never 0 (decision 15.5: no false alarm)."""
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(d, 'actions-runner')
            os.makedirs(root)
            argv = [[f'{root}/hx-gpu0/bin/Runner.Listener', 'run']]
            for mount, ours, want in (('proc rw,hidepid=invisible', True, {'hx-gpu0'}),
                                      ('proc rw,hidepid=2', False, None), ('proc rw,hidepid=1', False, None),
                                      ('proc rw,hidepid=0', False, {'hx-gpu0'}), ('proc rw,hidepid=off', False, {'hx-gpu0'}),
                                      ('proc rw', False, {'hx-gpu0'})):
                with tempfile.TemporaryDirectory() as e:
                    proc = self.fake_proc(e, argv, mount)
                    with unittest.mock.patch('os.getuid', return_value=os.stat(root).st_uid + (0 if ours else 1)):
                        self.assertEqual(pool.runners_alive(root, proc), want, msg=mount)
            with tempfile.TemporaryDirectory() as e:
                proc = self.fake_proc(e, argv, 'proc rw,hidepid=invisible')
                self.assertIsNone(pool.runners_alive(os.path.join(d, 'missing'), proc))  # owner unknown

    def test_a_host_with_no_runner_online(self):
        """The probe's count becomes the row's ``runners_online`` (15.7: ``runners`` is the
        instances per label); 0 is a problem brainspike's plan reads, which a pool run
        ignores (it takes a slot, not a runner); unknown or not asked is none."""
        m = reg().machines['hosta']
        base = {'slots_free': 1, 'load': 0.0, 'cpus': 16}
        down = pool.host_row(m, {**base, 'runners': 0})
        self.assertEqual((down['runners_online'], down['problems']), (0, ['no runner online']))
        self.assertNotIn('runners', down)
        self.assertEqual(pool.fits(down, pool.Ask(), m, 0), '')
        self.assertEqual(pool.fits(dict(down, problems=['drained', 'no runner online']), pool.Ask(), m, 0), 'drained')
        up = pool.host_row(m, {**base, 'runners': 3})
        self.assertEqual((up['runners_online'], up['problems']), (3, []))
        for p in ({**base, 'runners': None}, base):
            row = pool.host_row(m, p)
            self.assertEqual((row.get('runners_online'), row['problems']), (None, []))
        with tempfile.TemporaryDirectory() as d:
            lock = os.path.join(d, 's.lock')
            open(lock, 'w').close()
            self.assertEqual(pool.probe(lock, 1, '', runners=os.path.join(d, 'actions-runner'))['runners'], 0)
            self.assertNotIn('runners', pool.probe(lock, 1, ''))
            with unittest.mock.patch.object(pool, 'runner_procs', lambda root: None):  # hidden: not known, not 0
                got = pool.probe(lock, 1, '', runners='/x')
                self.assertIsNone(got['runners'])
                self.assertNotIn('runner_names', got)

    def test_the_host_side_runs_the_entrys_host_python(self):
        """h100's system python is 3.10, without tomllib (decision 15.8, item 9): the status
        probe and a run's host side run the entry's ``host_python``, the system's by default."""
        self.assertEqual(reg().machines['hosta'].host_python, '/usr/bin/python3')
        text = REGISTRY.replace('python = "/opt/env/bin/python"\n',
                                'python = "/opt/env/bin/python"\nhost_python = "/opt/py/bin/python3"\n', 1)
        r = pool.load(text)
        m = r.machines['hosta']
        self.assertEqual(m.host_python, '/opt/py/bin/python3')
        tokens = pool.deny_tokens({'machine': {'hosta': {'host_python': m.host_python}}})  # inventory, as python
        self.assertEqual(tokens, pool.deny_tokens({'machine': {'hosta': {'python': m.host_python}}}))
        self.assertTrue(tokens)
        with self.assertRaisesRegex(pool.RegistryError, 'host_python must be an absolute path'):
            pool.load(text.replace('"/opt/py/bin/python3"', '"python3"'))
        seen = []

        def run(argv, **kw):
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 0, json.dumps({'slots_free': 2, 'load': 0, 'cpus': 16}) + '\n', '')
        pool.probe_host(m, run)
        self.assertEqual(seen[-1][-1], '/opt/py/bin/python3 -I -')
        launched = []
        run = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, '{"numpy": "2.0"}\n', '')  # noqa: E731

        class Proc:
            def wait(self, timeout=None):
                return 0
        with unittest.mock.patch.object(pool, 'git_dir', lambda root, dest: ''), \
                unittest.mock.patch.object(pool, 'bundle', lambda *a, **k: b''):
            rc = pool.run_host(r, m, pool.RunArgs('true', ROOT, pool.Ask()), [], run,
                               lambda argv, **kw: launched.append(argv) or Proc(), run_id='r1')
        self.assertEqual(rc, 0)
        self.assertIn(' /opt/py/bin/python3 -I -c ', launched[0][-1])

    def test_probe_host_runs_the_probe_through_ssh(self):
        m = reg().machines['hosta']
        seen = {}

        def run(argv, **kw):
            seen['argv'], seen['input'] = argv, kw['input']
            return subprocess.CompletedProcess(argv, 0, json.dumps({'slots_free': 2, 'load': 0, 'cpus': 16}) + '\n', '')
        row = pool.probe_host(m, run)
        self.assertEqual(seen['argv'][-2:], ['alias-a', '/usr/bin/python3 -I -'])
        self.assertIn("probe('/tmp/x.lock', 2, '', '', '', 1, 2, '')", seen['input'])
        self.assertEqual(row['slots_free'], 2)
        self.assertEqual(pool.probe_host(m, lambda a, **k: subprocess.CompletedProcess(a, 255, '', ''))['problems'],
                         ['unreachable (exit 255)'])


SINFO = '\n'.join(['cn1|part_c|mix|4/68/0/72|2060000|0|(null)|(null)|',
                   'gn1|part_v|mix|2/18/0/20|510000|0|gpu:tesla:2|gpu:tesla:2(IDX:0-1)|',
                   'gn2|part_v|idle|0/20/0/20|510000|0|gpu:tesla:2|gpu:tesla:0(IDX:N/A)|',
                   'gn3|part_a|down*|0/128/0/128|510000|0|gpu:tesla:4|gpu:tesla:0(IDX:N/A)|',
                   'garbage'])


class Cluster(unittest.TestCase):
    def test_parsers(self):
        nodes = pool.parse_sinfo(SINFO)
        self.assertEqual([(n.name, n.cpus_idle, n.cards_free) for n in nodes],
                         [('cn1', 68, 0), ('gn1', 18, 0), ('gn2', 20, 2), ('gn3', 128, 4)])
        self.assertEqual(pool.parse_assoc('part_c|cpu=72,node=1\npart_v|cpu=200,gres/gpu:tesla=20,node=10\n'),
                         {'part_c': {'cpu': 72}, 'part_v': {'cpu': 200, 'gpu': 20}})
        self.assertEqual(pool.parse_squeue('part_v|10|gres/gpu:tesla:1\npart_v|10|gres/gpu:2\nPART_C|32|N/A\n'),
                         {'part_v': {'cpu': 20, 'gpu': 3}, 'part_c': {'cpu': 32, 'gpu': 0}})

    def test_target_rows_start_now_within_the_association(self):
        r = reg()
        m = r.machines['cl']
        nodes = pool.parse_sinfo(SINFO)
        cpu = pool.target_row(m, m.targets['cpu'], nodes, {'part_c': {'cpu': 72}}, {})
        self.assertEqual((cpu['key'], cpu['start_now'], cpu['cpus_free'], cpu['billed']), ('cl:cpu', True, 68, True))
        full = pool.target_row(m, m.targets['cpu'], nodes, {'part_c': {'cpu': 72}}, {'part_c': {'cpu': 64}})
        self.assertEqual((full['start_now'], full['cpus_free']), (False, 8))  # 8 of the account's 72 left
        v = pool.target_row(m, m.targets['v100'], nodes, {}, {}, cards=2)
        self.assertEqual((v['start_now'], sorted(v['gpu_free'])), (True, [0, 1]))
        v_lim = pool.target_row(m, m.targets['v100'], nodes, {'part_v': {'cpu': 200, 'gpu': 20}}, {'part_v': {'gpu': 19}},
                                cards=2)
        self.assertFalse(v_lim['start_now'])
        a = pool.target_row(m, m.targets['a40'], nodes, {}, {})
        self.assertEqual((a['start_now'], a['problems']), (False, ['no usable node in its partition']))  # down

    def test_an_unreachable_cluster_starts_nothing(self):
        rows = pool.probe_cluster(reg().machines['cl'], lambda a, **k: subprocess.CompletedProcess(a, 255, '', ''))
        self.assertTrue(all(not r['start_now'] and r['problems'] and r['jobs_free'] == 0 for r in rows))

    def test_jobs_free_sums_the_nodes_within_the_association(self):
        """Decision 15.7: the jobs of the target's size each idle or mix node holds, summed,
        capped by the association's room less the account's running jobs; positive
        exactly where start_now is true."""
        m = reg().machines['cl']
        cpu, v100 = m.targets['cpu'], m.targets['v100']
        nodes = pool.parse_sinfo('\n'.join([
            'c1|part_c|mix|28/72/0/100|2060000|0|(null)|(null)|',  # 72 idle cores: two 32-core jobs
            'c2|part_c|idle|0/40/0/40|2060000|0|(null)|(null)|',  # one
            'c3|part_c|mix|60/31/0/91|2060000|0|(null)|(null)|',  # 31 cores: none
            'c4|part_c|mix|0/64/0/64|2048|1024|(null)|(null)|',  # 64 cores but 1 GiB free: one 1 GiB job, no 2 GiB one
            'c5|part_c|mix|0/64/0/64|1024|2048|(null)|(null)|',  # more memory allocated than it has: none
            'c6|part_c|drain|0/96/0/96|2060000|0|(null)|(null)|',  # not idle or mix: none
            'g1|part_v|mix|0/40/0/40|510000|0|gpu:tesla:4|gpu:tesla:0(IDX:N/A)|',  # 4 cards, 40 cores
            'g2|part_v|mix|10/30/0/40|510000|0|gpu:tesla:4|gpu:tesla:1(IDX:0)|',  # 3 cards, 30 cores
            'g3|part_v|mix|35/5/0/40|510000|0|gpu:tesla:4|gpu:tesla:0(IDX:N/A)|',  # 4 cards, 5 cores: none
            'g4|part_v|mix|20/20/0/40|510000|0|gpu:tesla:4|gpu:tesla:0(IDX:N/A)|',  # 4 cards, 20 cores
            'g5|part_v|mix|0/40/0/40|510000|0|gpu:tesla:4|gpu:tesla:3(IDX:0-2)|',  # 1 card, 40 cores
        ]))

        def jobs(t, assoc=None, used=None, **kw):
            r = pool.target_row(m, t, nodes, assoc or {}, used or {}, **kw)
            self.assertEqual(r['start_now'], r['jobs_free'] > 0)
            return r['jobs_free']
        self.assertEqual(jobs(cpu), 2 + 1 + 1)  # c1, c2, c4; no association limit
        self.assertEqual(jobs(cpu, mem_gib=2), 2 + 1)
        self.assertEqual(jobs(cpu, mem_gib=4000), 1 + 1)  # the asked memory is at most the target's 1900 GiB: c1, c2
        self.assertEqual(jobs(cpu, {'part_c': {'cpu': 72}}), 2)  # the association binds: 72 // 32
        self.assertEqual(jobs(cpu, {'part_c': {'cpu': 72}}, {'part_c': {'cpu': 32}}), 1)
        self.assertEqual(jobs(cpu, {'part_c': {'cpu': 72}}, {'part_c': {'cpu': 64}}), 0)
        self.assertEqual(jobs(cpu, {'part_c': {'cpu': 72}}, {'part_c': {'cpu': 96}}), 0)  # over the limit: none
        self.assertEqual(jobs(cpu, {'part_c': {'cpu': 1000, 'gpu': 0}}), 4)  # a CPU job takes no card
        # a GPU job takes its cards and cpus (10) per card: one card g1 4, g2 3, g4 2, g5 1;
        # two cards g1 2, g2 1, g4 1 (its 20 cores)
        self.assertEqual(jobs(v100), 4 + 3 + 2 + 1)
        self.assertEqual(jobs(v100, cards=0), 4 + 3 + 2 + 1)  # one card at least
        self.assertEqual(jobs(v100, cards=2), 2 + 1 + 1)
        self.assertEqual(jobs(v100, cards=8), 2 + 1 + 1)  # at most its max_cards
        self.assertEqual(jobs(v100, {'part_v': {'gpu': 20}}, {'part_v': {'gpu': 17}}), 3)
        self.assertEqual(jobs(v100, {'part_v': {'gpu': 20}}, {'part_v': {'gpu': 17}}, cards=2), 1)
        self.assertEqual(jobs(v100, {'part_v': {'cpu': 200}}, {'part_v': {'cpu': 180}}, cards=2), 1)
        self.assertEqual(jobs(v100, {'part_v': {'cpu': 200}}, {'part_v': {'cpu': 190}}, cards=2), 0)
        few = pool.parse_sinfo('g3|part_v|mix|35/5/0/40|510000|0|gpu:tesla:4|gpu:tesla:0(IDX:N/A)|')
        r = pool.target_row(m, v100, few, {}, {})  # free cards, too few cores for one card's 10
        self.assertEqual((r['start_now'], r['jobs_free'], r['problems']), (False, 0, []))
        self.assertEqual(pool.target_row(m, cpu, [], {}, {})['jobs_free'], 0)
        nomem = dataclasses.replace(cpu, memory_gib=0)  # the registry allows it: a job still takes 1 GiB
        self.assertEqual(jobs(nomem), 2 + 1 + 1)
        self.assertEqual(jobs(cpu, mem_gib=0), 2 + 1 + 1)

    def test_other_users_cores_count_as_well_as_the_associations_room(self):
        """Issue #155: other users hold 16 of the one node's 72 cores; the association still
        allows 72. Room for one 32-core job, not two: the node's idle cores bind."""
        m = reg().machines['cl']
        nodes = pool.parse_sinfo('cn1|part_c|mix|16/56/0/72|2060000|0|(null)|(null)|')
        r = pool.target_row(m, m.targets['cpu'], nodes, {'part_c': {'cpu': 72}}, {})
        self.assertEqual((r['jobs_free'], r['start_now'], r['cpus_free']), (1, True, 56))
        r = pool.target_row(m, m.targets['cpu'], nodes, {'part_c': {'cpu': 72}}, {'part_c': {'cpu': 32}})
        self.assertEqual((r['jobs_free'], r['start_now']), (1, True))  # 40 of the room left, 56 idle: one
        busy = pool.parse_sinfo('cn1|part_c|mix|48/24/0/72|2060000|0|(null)|(null)|')
        r = pool.target_row(m, m.targets['cpu'], busy, {'part_c': {'cpu': 72}}, {})
        self.assertEqual((r['jobs_free'], r['start_now']), (0, False))  # 24 idle cores: none, whatever the room

    def test_the_table_shows_the_jobs_a_target_starts_now(self):
        m = reg().machines['cl']
        rows = [pool.target_row(m, m.targets['cpu'], pool.parse_sinfo(SINFO), {'part_c': {'cpu': 72}}, {}),
                pool.target_row(m, m.targets['a40'], pool.parse_sinfo(SINFO), {}, {}),
                pool.host_row(reg().machines['hosta'], {'slots_free': 1, 'load': 0, 'cpus': 16})]
        lines = pool.table(rows).splitlines()
        self.assertIn(' now (2) ', lines[1])
        self.assertIn(' no ', lines[2])
        self.assertRegex(lines[3], r' now +ok ')  # a host row has no jobs_free


def row(key, **kw):
    r = {'key': key, 'access': 'ssh', 'labels': {}, 'problems': [], 'queue': 0, 'gpu_free': {}, 'multi_gpu': False,
         'gpu_model': '', 'cuda': '', 'cpus_free': 16, 'billed': False, 'price': 0.0, 'target': '', 'numerics_ok': True,
         'start_now': True, 'slots_free': 1, 'slots': 2, 'mem_free_gib': 60, 'here': False}
    r.update(kw)
    return r


class Pick(unittest.TestCase):
    def keys(self, rows, ask, **kw):
        now, later, skipped = pool.pick(rows, ask, reg(), **kw)
        return [r['key'] for r in now], [r['key'] for r in later], skipped

    def test_free_hosts_first_then_the_cheapest_target_that_starts_now(self):
        rows = [row('cl:cpu', billed=True, price=0.04, target='cpu', cpus_free=68), row('hosta', cpus_free=4),
                row('hostb', cpus_free=12)]
        self.assertEqual(self.keys(rows, pool.Ask(cpus=4))[0], ['hostb', 'hosta', 'cl:cpu'])

    def test_the_callers_own_host_is_skipped_unless_asked(self):
        rows = [row('hostb', here=True)]
        now, _, skipped = self.keys(rows, pool.Ask())
        self.assertEqual(now, [])
        self.assertIn('own host', skipped[0])
        self.assertEqual(self.keys(rows, pool.Ask(), here_ok=True)[0], ['hostb'])

    def test_a_host_without_a_free_slot_is_where_it_waits_in_line(self):
        rows = [row('hosta', slots_free=0, cpus_free=0, queue=3), row('hostb', slots_free=0, cpus_free=0, queue=1),
                row('cl:cpu', billed=True, target='cpu', start_now=False)]
        now, later, skipped = self.keys(rows, pool.Ask())
        self.assertEqual((now, later), ([], ['hostb', 'hosta']))
        self.assertIn('cl:cpu: cannot start it now', skipped)

    def test_cards_by_model_count_and_p2p(self):
        rows = [row('hosta', gpu_model='3090', multi_gpu=True, gpu_free={0: 20000, 1: 3000}),
                row('cl:v100', billed=True, price=1.6, target='v100', gpu_model='v100', multi_gpu=True,
                    gpu_free={0: 32768, 1: 32768}),
                row('cl:a40', billed=True, price=4.5, target='a40', gpu_model='a40', multi_gpu=True,
                    gpu_free={0: 1, 1: 1}),
                row('cl:cpu', billed=True, target='cpu')]
        now, _, skipped = self.keys(rows, pool.Ask(cards=1))
        self.assertEqual(now, ['cl:v100', 'cl:a40'])  # a host's cards are its parts' leases
        self.assertIn('leased to its parts', skipped[0])
        self.assertEqual(self.keys(rows, pool.Ask(cards=2))[0], ['cl:v100', 'cl:a40'])
        self.assertEqual(self.keys(rows, pool.Ask(cards=1, gpu_model='a40'))[0], ['cl:a40'])
        self.assertEqual(self.keys(rows, pool.Ask(cards=1, gpu_model='a4'))[0], [])  # a model, not a prefix
        _, _, skipped = self.keys(rows, pool.Ask(cards=0))
        self.assertIn('cl:v100: a GPU target, and no card asked', skipped)

    def test_a_hosts_cards_by_its_leases_where_the_registry_shares_them(self):
        r = reg()
        r = dataclasses.replace(r, machines={**r.machines, 'hosta': dataclasses.replace(
            r.machines['hosta'], leases=pool.LeaseDir('/tmp/l', 1))})
        rows = [row('hosta', gpu_model='3090', multi_gpu=True, slots_free=0, cpus_free=0, gpu_free={1: 20000}),
                row('cl:v100', billed=True, price=1.6, target='v100', gpu_model='v100', multi_gpu=True,
                    gpu_free={0: 32768, 1: 32768})]
        now, later, _ = pool.pick(rows, pool.Ask(cards=1), r)
        self.assertEqual([x['key'] for x in now], ['hosta', 'cl:v100'])  # free, though its CI slots are full
        now, later, _ = pool.pick(rows, pool.Ask(cards=2), r)
        self.assertEqual(([x['key'] for x in now], [x['key'] for x in later]), (['cl:v100'], ['hosta']))

    def test_sizes_beyond_a_machine_are_skipped(self):
        _, _, skipped = self.keys([row('hosta')], pool.Ask(cpus=64))
        self.assertEqual(skipped, ['hosta: 16 cores, 64 asked'])
        _, _, skipped = self.keys([row('cl:cpu', billed=True, target='cpu')], pool.Ask(memory_gib=4000))
        self.assertEqual(skipped, ['cl:cpu: 1900 GiB a node, 4000 asked'])

    def test_on_keeps_one_machine_or_target(self):
        rows = [row('hosta'), row('cl:cpu', billed=True, target='cpu')]
        self.assertEqual(self.keys(rows, pool.Ask(), only='cl')[0], ['cl:cpu'])
        self.assertEqual(self.keys(rows, pool.Ask(), only='hosta')[0], ['hosta'])

    def test_gpu_argument(self):
        self.assertEqual(pool.parse_gpu('v100:2'), ('v100', 2))
        self.assertEqual(pool.parse_gpu('any'), ('', 1))
        with self.assertRaises(ValueError):
            pool.parse_gpu('v100:x')


class EnvCheck(unittest.TestCase):
    def test_diff(self):
        self.assertEqual(pool.env_diff({'a': '1.0', 'b': '2.0@abc'}, {'a': '1.0', 'b': '2.0@abc'}), [])
        self.assertEqual(pool.env_diff({'a': '1.0'}, {'a': '1.1'}), ['a: 1.0 here, 1.1 there'])
        self.assertEqual(pool.env_diff({'b': '2.0@abc'}, {'b': '2.0@def'}), ['b: 2.0@abc here, 2.0@def there'])
        self.assertEqual(pool.env_diff({'b': '2.0@abc'}, {'b': '2.0'}), [])  # editable on one side only: the version
        self.assertEqual(pool.env_diff({'c': None}, {'c': '1'}), ['c: missing here, 1 there'])

    def test_fingerprint_reads_this_env(self):
        fp = pool.fingerprint([], sys.executable, ['pip', 'no-such-dist-xyz'])
        self.assertIsNone(fp['no-such-dist-xyz'])
        self.assertTrue(fp['pip'] is None or fp['pip'][0].isdigit())

    def test_a_mismatch_refuses_unless_warned(self):
        fps = iter([{'numpy': '2.0'}, {'numpy': '2.1'}] * 2)
        orig = pool.fingerprint
        pool.fingerprint = lambda *a, **k: next(fps)
        try:
            a = pool.RunArgs('true', '.', pool.Ask())
            self.assertFalse(pool._env_ok(reg(), a, [], '/x/python', None))
            a.env_mismatch = 'warn'
            self.assertTrue(pool._env_ok(reg(), a, [], '/x/python', None))
        finally:
            pool.fingerprint = orig

    def test_forwarded_variables(self):
        self.assertEqual(pool.forwarded({'JAX_ENABLE_X64': '1', 'XLA_FLAGS': 'x', 'HOME': '/h', 'CI_TEST_TIMEOUT': '9',
                                         'GH_TOKEN': 's'}), {'JAX_ENABLE_X64': '1', 'XLA_FLAGS': 'x', 'CI_TEST_TIMEOUT': '9'})


class Exit(unittest.TestCase):
    def test_exit_codes(self):
        self.assertEqual(pool._exit_code({'rc': 3}), 3)
        self.assertEqual(pool._exit_code({'rc': -9}), 137)
        self.assertEqual(pool._exit_code({'rc': 0, 'timed_out': True}), pool.EXIT_TIMEOUT)
        self.assertEqual(pool._exit_code({'rc': -9, 'oom_kills': 1}), pool.EXIT_OOM)
        self.assertEqual(pool._exit_code({'rc': None, 'signalled': True}), pool.EXIT_SIGNALLED)


class Tree(unittest.TestCase):
    def test_tracked_and_untracked_files_but_not_ignored_ones(self):
        with tempfile.TemporaryDirectory() as d:
            git = lambda *a: subprocess.run(['git', '-C', d, *a], check=True, capture_output=True)  # noqa: E731
            git('init', '-q')
            for name, text in [('a.py', 'a'), ('.gitignore', 'build/\n'), ('build/x', 'x'), ('new_test.py', 'n')]:
                os.makedirs(os.path.dirname(os.path.join(d, name)), exist_ok=True)
                with open(os.path.join(d, name), 'w') as f:
                    f.write(text)
            git('add', 'a.py', '.gitignore')
            files = pool.tree_files(d)
            self.assertEqual(files, ['.gitignore', 'a.py', 'new_test.py'])
            import tarfile
            names = tarfile.open(fileobj=io.BytesIO(pool.bundle(d, files, os.path.join(ROOT, 'ci_runners')))).getnames()
            self.assertIn('ws/new_test.py', names)
            self.assertIn('pkg/ci_runners/pool.py', names)
            self.assertNotIn('ws/build/x', names)


    def test_the_staged_git_sees_the_callers_merge_base_files_and_status(self):
        with tempfile.TemporaryDirectory() as d:
            src, ws = os.path.join(d, 'src'), os.path.join(d, 'out')
            os.makedirs(src)

            def git(*a, cwd=src):
                return subprocess.run(['git', '-C', cwd, '-c', 'user.name=t', '-c', 'user.email=t@t', *a], check=True,
                                      capture_output=True, text=True).stdout.strip()
            git('init', '-q')
            for i in range(3):  # main's history
                with open(os.path.join(src, f'm{i}.py'), 'w') as f:
                    f.write(str(i))
                git('add', '.')
                git('commit', '-q', '-m', f'm{i}')
            base = git('rev-parse', 'HEAD')
            git('update-ref', 'refs/remotes/origin/main', base)
            git('checkout', '-q', '-b', 'topic')
            for i in range(2):  # the branch's commits
                with open(os.path.join(src, f't{i}.py'), 'w') as f:
                    f.write(str(i))
                git('add', '.')
                git('commit', '-q', '-m', f't{i}')
            with open(os.path.join(src, 'm0.py'), 'w') as f:
                f.write('changed')  # a local change
            with open(os.path.join(src, 'staged.py'), 'w') as f:
                f.write('s')
            git('add', 'staged.py')  # added, not committed
            with open(os.path.join(src, 'loose.py'), 'w') as f:
                f.write('u')  # untracked
            with tempfile.TemporaryDirectory() as g:
                data = pool.bundle(src, pool.tree_files(src), git=pool.git_dir(src, g))
            import tarfile
            with tarfile.open(fileobj=io.BytesIO(data)) as tar:
                tar.extractall(ws, filter='tar')
            there = os.path.join(ws, 'ws')
            self.assertEqual(git('merge-base', 'origin/main', 'HEAD', cwd=there), base)
            self.assertEqual(git('rev-parse', 'HEAD', cwd=there), git('rev-parse', 'HEAD'))
            self.assertEqual(git('ls-files', cwd=there), git('ls-files'))
            self.assertEqual(git('status', '--porcelain', cwd=there), git('status', '--porcelain'))
            self.assertEqual(git('diff', '--name-only', 'origin/main', cwd=there), git('diff', '--name-only', 'origin/main'))

class FakeGate:
    """The gate's verbs as ``OwnerGate`` calls them, over a scripted job."""

    def __init__(self, states, log=b'line 1\nline 2\n', exit=None, fail_on=None):
        self.states, self.log, self.exit, self.fail_on = list(states), log, exit or {'rc': 0}, fail_on
        self.calls = []

    def __call__(self, argv, **kw):
        if argv[0] == 'rsync':
            self.calls.append(('stage', argv[-1]))
            return subprocess.CompletedProcess(argv, 0, b'', b'')
        cmd = argv[-1]
        if 'fingerprint' in cmd or 'importlib.metadata' in cmd:
            return subprocess.CompletedProcess(argv, 0, '{"numpy": "1"}\n', '')
        words = cmd.split("'")[1].split()  # SSH_ORIGINAL_COMMAND='ci-slurm-gate VERB ARGS'
        verb = words[1]
        self.calls.append((verb, kw.get('input')))
        if verb == self.fail_on:
            raise KeyboardInterrupt
        if verb == 'submit':
            return subprocess.CompletedProcess(argv, 0, b'{"job": 42}', b'')
        if verb == 'status':
            st = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            done = st not in ('PENDING', 'RUNNING')
            return subprocess.CompletedProcess(argv, 0, json.dumps({'state': st, 'log_size': 0 if st == 'PENDING' else
                                                                    len(self.log), 'exit': self.exit if done else None,
                                                                    'reason': 'Resources'}).encode(), b'')
        if verb == 'log':
            return subprocess.CompletedProcess(argv, 0, self.log[int(words[3]):], b'')
        if verb == 'cancel':
            return subprocess.CompletedProcess(argv, 0, b'{}', b'')
        raise AssertionError(verb)


class ClusterRun(unittest.TestCase):
    def setUp(self):
        self.r = reg()
        self.m = self.r.machines['cl']
        self.root = tempfile.mkdtemp()
        with open(os.path.join(self.root, 'x.py'), 'w') as f:
            f.write('x')
        self.orig = pool.fingerprint
        pool.fingerprint = lambda *a, **k: {'numpy': '1'}

    def tearDown(self):
        pool.fingerprint = self.orig
        subprocess.run(['rm', '-rf', self.root])

    def go(self, gate, target='cpu', **kw):
        a = pool.RunArgs('pytest -q', self.root, pool.Ask(cards=kw.pop('cards', 0)), timeout_min=kw.pop('timeout', 20),
                         queue_min=kw.pop('queue', 0.0))
        out = io.BytesIO()
        rc = pool.run_cluster(self.r, self.m, self.m.targets[target], a, ['x.py'], gate, sleep=lambda s: None, out=out)
        return rc, out.getvalue(), [c[0] for c in gate.calls]

    def test_a_job_runs_its_log_is_relayed_and_its_code_returned(self):
        g = FakeGate(['PENDING', 'RUNNING', 'COMPLETED'], exit={'rc': 1})
        a = pool.RunArgs('pytest -q', self.root, pool.Ask(), queue_min=5)
        out = io.BytesIO()
        rc = pool.run_cluster(self.r, self.m, self.m.targets['cpu'], a, ['x.py'], g, sleep=lambda s: None, out=out)
        self.assertEqual(rc, 1)
        self.assertEqual(out.getvalue(), b'line 1\nline 2\n')
        verbs = [c[0] for c in g.calls]
        self.assertEqual(verbs[:2], ['stage', 'submit'])
        self.assertNotIn('cancel', verbs)

    def test_the_request_passes_the_gates_checks_and_the_key_its_pattern(self):
        g = FakeGate(['COMPLETED'])
        self.go(g, target='v100', cards=1, queue=1)
        req = json.loads(dict(g.calls)['submit'])
        for k, (ok, required) in slurmgate.REQUEST.items():
            self.assertTrue(k in req or not required, k)
            if k in req:
                self.assertTrue(ok(req[k]), k)
        self.assertEqual((req['target'], req['cards'], req['host']), ('v100', 1, 'pool'))
        key = pool.run_key('Owner/Proj', 1_800_000_000.5, 1234)
        self.assertRegex(key, slurmgate.KEY)
        self.assertEqual(key, 'owner/proj/1800000000500-1/pool-1234-1')

    def test_a_job_not_started_within_the_bound_is_withdrawn(self):
        g = FakeGate(['PENDING'])
        rc, _, verbs = self.go(g, queue=0.0)
        self.assertEqual(rc, pool.NOT_STARTED)
        self.assertEqual(verbs[-1], 'cancel')

    def test_a_submit_that_fails_after_sbatch_still_cancels(self):
        g = FakeGate(['RUNNING'], fail_on='submit')
        with self.assertRaises(KeyboardInterrupt):
            self.go(g, queue=5)
        self.assertEqual([c[0] for c in g.calls][-1], 'cancel')

    def test_a_failed_status_read_is_tried_again(self):
        g = FakeGate(['RUNNING', 'COMPLETED'], exit={'rc': 0})
        real, fails = g.__call__, [1]

        def flaky(argv, **kw):
            if 'status' in str(argv[-1]) and fails:
                fails.pop()
                return subprocess.CompletedProcess(argv, 1, b'', b'ci-slurm-gate: refused: squeue timed out')
            return real(argv, **kw)
        a = pool.RunArgs('pytest -q', self.root, pool.Ask(), queue_min=5)
        rc = pool.run_cluster(self.r, self.m, self.m.targets['cpu'], a, ['x.py'], flaky, sleep=lambda s: None,
                              out=io.BytesIO())
        self.assertEqual(rc, 0)

    def test_an_interrupted_run_cancels_its_job(self):
        g = FakeGate(['RUNNING'], fail_on='log')
        with self.assertRaises(KeyboardInterrupt):
            self.go(g, queue=5)
        self.assertEqual(g.calls[-1][0], 'cancel')

    def test_a_timeout_past_the_targets_bound_is_refused(self):
        g = FakeGate(['COMPLETED'])
        rc, _, verbs = self.go(g, target='v100', cards=1, timeout=60)
        self.assertEqual((rc, verbs), (pool.EXIT_USAGE, []))

    def test_the_gate_command_is_the_forced_commands_form(self):
        g = pool.OwnerGate(self.m)
        cmd = g._cmd(['status', 'owner/proj/1-1/pool-1-1'])
        self.assertEqual(cmd, "SSH_ORIGINAL_COMMAND='ci-slurm-gate status owner/proj/1-1/pool-1-1' exec "
                              "/fs/root/ci-runners/releases/hook/bin/ci-slurm-gate")


class Bounds(unittest.TestCase):
    def setUp(self):  # a checkout to stage (the mutants' copies of this tree have no .git)
        self.root = tempfile.mkdtemp()
        subprocess.run(['git', 'init', '-q', self.root], check=True)
        open(os.path.join(self.root, 'x.py'), 'w').close()

    def tearDown(self):
        subprocess.run(['rm', '-rf', self.root])

    def test_a_free_now_host_is_given_up_soon_the_chosen_one_waited_on(self):
        r, a = reg(), pool.RunArgs('true', self.root, pool.Ask())
        m = r.machines['hosta']
        quick, long = pool.host_spec(r, m, a, wait=False), pool.host_spec(r, m, a, wait=True)
        self.assertEqual((quick['rounds'], long['rounds']), (pool.FREE_NOW_ROUNDS, 0))
        self.assertEqual(long['queue_seconds'], pool.LONG_WAIT_MIN * 60)
        rows = [row('hosta', cpus_free=12), row('hostb', slots_free=0, cpus_free=0)]
        calls = []
        orig = pool.run_host
        pool.run_host = lambda reg_, m, *x, wait, **k: calls.append((m.key, wait)) or pool.NOT_STARTED
        try:
            rc = pool.pool_run(r, a, rows=rows)
        finally:
            pool.run_host = orig
        self.assertEqual((rc, calls), (pool.EXIT_NO_ROOM, [('hosta', False), ('hostb', True)]))

    def test_a_run_ends_before_its_directory_counts_as_stale(self):
        self.assertLess(pool.MAX_TIMEOUT_MIN + pool.LONG_WAIT_MIN + 60, pool.STALE_DAYS * 24 * 60)
        orig = pool.run_host
        pool.run_host = lambda *x, **k: 0
        try:
            for minutes, rc in ((pool.MAX_TIMEOUT_MIN + 1, pool.EXIT_USAGE), (0, pool.EXIT_USAGE),
                                (pool.MAX_TIMEOUT_MIN, 0)):
                a = pool.RunArgs('true', self.root, pool.Ask(), timeout_min=minutes)
                self.assertEqual(pool.pool_run(reg(), a, rows=[row('hosta')]), rc, minutes)
        finally:
            pool.run_host = orig

    def test_a_stale_directory_is_removed_unless_its_run_is_alive(self):
        with tempfile.TemporaryDirectory() as work:
            old = time.time() - (pool.STALE_DAYS * 86400 + 60)
            dirs = {n: os.path.join(work, n) for n in ('pool-gone', 'pool-alive', 'pool-new', 'other')}
            for p in dirs.values():
                os.makedirs(p)
            fd = pool._hold_alive(dirs['pool-alive'])  # a supervisor that outlived its host side
            for n in ('pool-gone', 'pool-alive', 'other'):
                os.utime(dirs[n], (old, old))
            try:
                pool._remove_stale(work, '', time.time())
            finally:
                os.close(fd)
            self.assertEqual(sorted(os.listdir(work)), ['other', 'pool-alive', 'pool-new'])


class PoolRun(unittest.TestCase):
    def test_no_room_anywhere_says_so(self):
        r = reg()
        rows = [row('hosta', slots_free=0, cpus_free=0)]
        a = pool.RunArgs('true', ROOT, pool.Ask(), no_wait=True)
        self.assertEqual(pool.pool_run(r, a, rows=rows), pool.EXIT_NO_ROOM)

    def test_it_falls_through_to_the_next_machine_when_one_has_no_room(self):
        r = reg()
        rows = [row('hosta', cpus_free=12), row('cl:cpu', billed=True, target='cpu')]
        tried = []
        orig_h, orig_c = pool.run_host, pool.run_cluster
        pool.run_host = lambda reg_, m, *a, **k: tried.append(m.key) or pool.NOT_STARTED
        pool.run_cluster = lambda reg_, m, t, *a, **k: tried.append(f'{m.key}:{t.name}') or 5
        try:
            rc = pool.pool_run(r, pool.RunArgs('true', ROOT, pool.Ask()), rows=rows)
        finally:
            pool.run_host, pool.run_cluster = orig_h, orig_c
        self.assertEqual((rc, tried), (5, ['hosta', 'cl:cpu']))

    def test_a_commands_own_75_is_its_code_not_a_retry(self):
        r = reg()
        rows = [row('hosta', cpus_free=12), row('cl:cpu', billed=True, target='cpu')]
        tried = []
        orig = pool.run_host
        pool.run_host = lambda reg_, m, *a, **k: tried.append(m.key) or 75
        try:
            rc = pool.pool_run(r, pool.RunArgs('true', ROOT, pool.Ask()), rows=rows)
        finally:
            pool.run_host = orig
        self.assertEqual((rc, tried), (75, ['hosta']))


# ------------------------------------------------------------------ a CI account's registry (decision 15.1, item 8)

with open(os.path.join(ROOT, 'setup', 'slurm', 'site.example.toml')) as _f:
    SITE = _f.read()
CI_REGISTRY = REGISTRY.replace(
    'access = "slurm"\nssh = "alias-c"\ngate = "/fs/root/ci-runners/releases/hook/bin/ci-slurm-gate"\n',
    'access = "slurm"\nsite = "cluster1"\naccount = "acct1"\n').replace(
    'labels = { cpu = "hosta-cpu" }\n', 'labels = { cpu = "hosta-cpu" }\nprobe = "forced"\n')
CAPACITY = (SINFO.replace('part_c', 'cpu-partition') + '\n===\n' + 'cpu-partition|cpu=72\n' + '===\n'
            + 'cpu-partition|32|N/A\n')


def inventory_text(this, slurm=8):
    """A host's inventory (decision 15.7): a pool with the default labels, two with their
    own, a per-card pool (which also names card 0's label: its runner carries it once), and
    a slurm pool that also carries the label both hosts share."""
    return textwrap.dedent(f'''\
        schema = 1
        this = "{this}"
        [hosts.{this}]
        account = "acct-{this}"
        home = "/srv/ci/{this}"
        data = "/srv/ci/{this}/data"
        slots = 2
        memory_max = "64G"
        disk_min_gib = 50
        gpus = [0, 3]
        max_temp_c = 80
        files = {{ slot_lock = "/tmp/s.lock", slot_queue = "/tmp/s.queue" }}
        [hosts.{this}.pools]
        light = 2
        part = {{ count = 3, labels = ["{this}-cpu"] }}
        cards = {{ per_card = true, class = "gpu", labels = ["{this}-gpu", "{this}-gpu0"] }}
        slurm = {{ count = {slurm}, labels = ["{this}-slurm", "shared-slurm"] }}
        ''')


def runners_of(this, slurm=8):
    return {'light': 2, this: 2, f'{this}-cpu': 3, f'{this}-gpu': 2, f'{this}-gpu0': 2, f'{this}-gpu3': 1,
            f'{this}-slurm': slurm, 'shared-slurm': slurm}


class CIAccount(unittest.TestCase):
    """A CI account's registry: the cluster through its CI key and the gate's ``capacity``,
    the other host through a key forced to ``ci-run pool probe``; both for pool status only."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.etc = os.path.join(self.tmp.name, 'ci-runners', 'etc')
        os.makedirs(os.path.join(self.etc, 'slurm', 'keys'))
        os.makedirs(os.path.join(self.etc, 'pool', 'keys'))
        self.site = os.path.join(self.etc, 'slurm', 'cluster1.toml')
        with open(self.site, 'w') as f:
            f.write(SITE)
        os.chmod(self.site, 0o600)
        self.key = os.path.join(self.etc, 'pool', 'keys', 'hosta')
        open(self.key, 'w').close()
        self.reg = pool.load(CI_REGISTRY)

    def test_the_entries_load_and_serve_status_only(self):
        m, h = self.reg.machines['cl'], self.reg.machines['hosta']
        self.assertEqual((m.site, m.account, m.ssh, m.gate, h.probe), ('cluster1', 'acct1', '', '', 'forced'))
        self.assertEqual([k for k, x in self.reg.machines.items() if x.status_only], ['hosta', 'cl'])
        self.assertFalse(reg().machines['cl'].status_only)
        rows = [row('hosta'), row('cl:cpu', target='cpu', access='slurm', billed=True), row('hostb')]
        for only in ('', 'hosta', 'cl'):
            now, later, skipped = pool.pick(rows, pool.Ask(), self.reg, only=only)
            self.assertNotIn('hosta', [r['key'] for r in now + later])
            self.assertNotIn('cl:cpu', [r['key'] for r in now + later])
        now, _, skipped = pool.pick(rows, pool.Ask(), self.reg)
        self.assertEqual([r['key'] for r in now], ['hostb'])
        self.assertIn("hosta: a CI account's entry, for pool status only", skipped)
        self.assertIn("cl:cpu: a CI account's entry, for pool status only", skipped)

    def test_malformed_entries_are_refused(self):
        two = 'site = "cluster1"\naccount = "acct1"\n'
        for bad, why in [(CI_REGISTRY.replace('account = "acct1"\n', ''), 'site and account'),
                         (CI_REGISTRY.replace('site = "cluster1"\n', ''), 'site and account'),
                         (CI_REGISTRY.replace(two, two + 'ssh = "alias-c"\n'), 'site and account'),
                         (CI_REGISTRY.replace(two, two + 'gate = "/g"\n'), 'site and account'),
                         (CI_REGISTRY.replace('site = "cluster1"', 'site = "Cluster 1"'), 'site and account'),
                         (CI_REGISTRY.replace('account = "acct1"', 'account = "acct 1"'), 'site and account'),
                         (CI_REGISTRY.replace('access = "local"\n', 'access = "local"\n' + two), 'site and account'),
                         (CI_REGISTRY.replace(two, ''), 'ssh alias'),
                         (CI_REGISTRY.replace(two, 'ssh = "alias-c"\n'), 'gate is missing'),
                         (CI_REGISTRY.replace('probe = "forced"', 'probe = "source"'), 'probe is'),
                         (CI_REGISTRY.replace('access = "local"\n', 'access = "local"\nprobe = "forced"\n'), 'probe is')]:
            with self.subTest(why=why):
                with self.assertRaisesRegex(pool.RegistryError, why):
                    pool.load(bad)

    def test_the_forced_probe_sends_no_code_and_no_stdin_through_its_key(self):
        m = self.reg.machines['hosta']
        seen = {}

        def run(argv, **kw):
            seen['argv'], seen['kw'] = argv, kw
            return subprocess.CompletedProcess(argv, 0, json.dumps({'slots_free': 1, 'load': 0, 'cpus': 16}) + '\n', '')
        got = pool.probe_host(m, run, etc=self.etc)
        self.assertEqual((got['slots_free'], got['problems']), (1, []))
        self.assertEqual(seen['argv'][-2:], ['alias-a', 'ci-run pool probe'])
        self.assertEqual(seen['argv'][seen['argv'].index('-i') + 1], self.key)
        self.assertIn('IdentitiesOnly=yes', seen['argv'])
        self.assertIs(seen['kw']['stdin'], subprocess.DEVNULL)
        self.assertEqual(seen['kw']['timeout'], pool.HOST_TIMEOUT)
        self.assertNotIn('input', seen['kw'])
        self.assertEqual(pool.probe_host(m, lambda a, **k: subprocess.CompletedProcess(a, 255, '', ''),
                                         etc=self.etc)['problems'], ['unreachable (exit 255)'])
        os.unlink(self.key)
        self.assertEqual(pool.probe_host(m, run, etc=self.etc)['problems'], ['no key etc/pool/keys/hosta'])

    def test_a_shared_account_names_its_aliases_in_its_own_ssh_config(self):
        """etc/pool/ssh_config, where it is a file, is the forced probe's ssh configuration (15.8)."""
        m = self.reg.machines['hosta']
        seen = []

        def run(argv, **kw):
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 0, json.dumps({'slots_free': 1, 'load': 0, 'cpus': 16}) + '\n', '')
        pool.probe_host(m, run, etc=self.etc)
        self.assertNotIn('-F', seen[-1])  # none there: the account's own
        cfg = os.path.join(self.etc, 'pool', 'ssh_config')
        os.mkdir(cfg)
        pool.probe_host(m, run, etc=self.etc)
        self.assertNotIn('-F', seen[-1])  # not a file
        os.rmdir(cfg)
        open(cfg, 'w').close()
        pool.probe_host(m, run, etc=self.etc)
        self.assertEqual(seen[-1][seen[-1].index('-F') + 1], cfg)
        self.assertEqual(seen[-1][-2:], ['alias-a', 'ci-run pool probe'])

    def test_the_cluster_through_the_gates_capacity(self):
        m = self.reg.machines['cl']
        m = dataclasses.replace(m, targets={'cpu': dataclasses.replace(m.targets['cpu'], partition='cpu-partition')})
        seen = []

        def run(argv, rc=0, out=CAPACITY, **kw):
            seen.append(argv)
            return subprocess.CompletedProcess(argv, rc, out.encode(), b'secret-host: refused')
        timeouts = []
        rows = pool.probe_cluster(m, lambda a, **k: timeouts.append(k['timeout']) or run(a, **k), etc=self.etc)
        self.assertEqual(timeouts, [pool.CLUSTER_TIMEOUT])
        self.assertEqual(seen[0][-3:], ['cluster1-acct1', 'ci-slurm-gate', 'capacity'])
        self.assertEqual(seen[0][seen[0].index('-i') + 1], os.path.join(self.etc, 'slurm', 'keys', 'acct1'))
        self.assertTrue(seen[0][seen[0].index('-o') + 1].startswith('BatchMode'))
        self.assertEqual((rows[0]['key'], rows[0]['problems'], rows[0]['start_now'], rows[0]['cpus_free']),
                         ('cl:cpu', [], True, 40))  # 72 less the account's running 32
        for rc, out, why in [(255, '', 'cluster unreachable'), (2, '', 'the gate refused capacity'),
                             (0, 'only one part', 'cluster unreachable')]:
            got = pool.probe_cluster(m, lambda a, **k: run(a, rc, out), etc=self.etc)
            self.assertTrue(got[0]['problems'][0].startswith(why), (rc, got[0]['problems']))
            self.assertFalse(got[0]['start_now'])
            self.assertNotIn('secret-host', got[0]['problems'][0])

        def slow(argv, **kw):
            raise subprocess.TimeoutExpired(argv, 90)
        self.assertEqual(pool.probe_cluster(m, slow, etc=self.etc)[0]['problems'], ['cluster unreachable'])

        def missing(argv, **kw):
            raise FileNotFoundError('ssh')
        self.assertEqual(pool.probe_cluster(m, missing, etc=self.etc)[0]['problems'], ['cluster unreachable'])
        self.assertEqual(pool.probe_cluster(dataclasses.replace(m, account='acct9'), run, etc=self.etc)[0]['problems'],
                         ['no account acct9 in site cluster1'])
        self.assertEqual(pool.probe_cluster(dataclasses.replace(m, site='other'), run, etc=self.etc)[0]['problems'],
                         ['no site config etc/slurm/other.toml'])
        os.chmod(self.site, 0o644)
        self.assertEqual(pool.probe_cluster(m, run, etc=self.etc)[0]['problems'], ['site config: invalid'])

    def test_status_probes_every_machine_at_once(self):
        def run(argv, **kw):
            time.sleep(0.6)
            return subprocess.CompletedProcess(argv, 255, b'' if 'ci-slurm-gate' in argv else '', b'')
        t0 = time.monotonic()
        rows = pool.status(self.reg, run, here='', etc=self.etc)
        self.assertLess(time.monotonic() - t0, 1.5)  # three machines of 0.6 s each: at once
        self.assertEqual([r['key'] for r in rows], ['hosta', 'hostb', 'cl:cpu', 'cl:v100', 'cl:a40'])
        self.assertEqual(rows[0]['problems'], ['unreachable (exit 255)'])  # the forced probe, through its key
        self.assertEqual(rows[2]['problems'], ['cluster unreachable'])
        self.assertEqual((pool.HOST_TIMEOUT, pool.CLUSTER_TIMEOUT), (60, 90))  # inside the plan step's 120 s

    def write_registry(self, text):
        path = os.path.join(self.etc, 'pool.toml')
        with open(path, 'w') as f:
            f.write(text)
        os.chmod(path, 0o600)

    def local_lock(self):
        lock = os.path.join(self.tmp.name, 'x.lock')
        open(lock, 'w').close()
        self.write_registry(CI_REGISTRY.replace('lock = "/tmp/x.lock", count = 1', f'lock = "{lock}", count = 1'))

    def test_pool_probe_prints_the_local_machines_probe(self):
        self.local_lock()
        out = io.StringIO()
        with unittest.mock.patch('sys.stdout', out):
            self.assertEqual(pool.main(['probe'], self.etc), 0)
        p = json.loads(out.getvalue())
        self.assertEqual((p['slots_free'], p['slots_missing'], p['drained']), (1, 0, False))
        self.assertNotIn('cards_leased', p)
        self.assertNotIn('runner_labels', p)  # no inventory beside the registry
        with open(os.path.join(self.etc, 'inventory.toml'), 'w') as f:
            f.write(inventory_text('hx'))
        out = io.StringIO()
        with unittest.mock.patch('sys.stdout', out):
            self.assertEqual(pool.main(['probe'], self.etc), 0)
        self.assertEqual(json.loads(out.getvalue())['runner_labels'], runners_of('hx'))  # the etc's inventory
        for text in (CI_REGISTRY.replace('access = "local"', 'access = "ssh"\nssh = "b"'),
                     CI_REGISTRY.replace('access = "ssh"\nssh = "alias-a"', 'access = "local"').replace(
                         'probe = "forced"\n', '')):
            self.write_registry(text)
            err = io.StringIO()
            with unittest.mock.patch('sys.stderr', err):
                self.assertEqual(pool.main(['probe'], self.etc), pool.EXIT_USAGE)
            self.assertIn('local machines in the registry, not one', err.getvalue())

    def test_a_host_row_counts_its_runners_per_label(self):
        """Decision 15.7: the local row's ``runners`` come from the inventory beside the
        registry, each instance once per label it carries; the other host's from its own
        inventory, through the forced probe; a label both carry is counted on each."""
        self.assertIsNone(pool.runner_labels(self.etc))  # no inventory
        with open(os.path.join(self.etc, 'inventory.toml'), 'w') as f:
            f.write('schema = 1\n')
        self.assertIsNone(pool.runner_labels(self.etc))  # one that does not load
        with open(os.path.join(self.etc, 'inventory.toml'), 'w') as f:
            f.write(inventory_text('hx'))
        self.assertEqual(pool.runner_labels(self.etc), runners_of('hx'))
        other = os.path.join(self.tmp.name, 'other', 'ci-runners', 'etc')  # the other host's CI account
        os.makedirs(other)
        with open(os.path.join(other, 'inventory.toml'), 'w') as f:
            f.write(inventory_text('hy', slurm=4))
        lock = os.path.join(self.tmp.name, 'x.lock')
        open(lock, 'w').close()
        theirs = pool.local_probe(pool.load(CI_REGISTRY.replace('lock = "/tmp/x.lock", count = 1',
                                                                f'lock = "{lock}", count = 1')), other)
        self.assertEqual(theirs['runner_labels'], runners_of('hy', slurm=4))

        def answer(p):
            return lambda argv, **kw: subprocess.CompletedProcess(argv, 0, json.dumps(p) + '\n', '')
        local = pool.probe_host(self.reg.machines['hostb'], answer({'slots_free': 1, 'load': 0}), etc=self.etc)
        forced = pool.probe_host(self.reg.machines['hosta'], answer(theirs), etc=self.etc)
        self.assertEqual(local['runners'], runners_of('hx'))
        self.assertEqual(forced['runners'], runners_of('hy', slurm=4))  # its own, not the caller's
        self.assertEqual((local['runners']['shared-slurm'], forced['runners']['shared-slurm']), (8, 4))
        os.unlink(os.path.join(self.etc, 'inventory.toml'))
        self.assertNotIn('runners', pool.probe_host(self.reg.machines['hostb'], answer({'slots_free': 1}), etc=self.etc))
        self.assertNotIn('runner_labels', pool.local_probe(pool.load(CI_REGISTRY.replace(
            'lock = "/tmp/x.lock", count = 1', f'lock = "{lock}", count = 1')), self.etc))
        m = self.reg.machines['hosta']
        for bad in ({'a': True}, {'a': -1}, {'a': '1'}, ['a'], 3):  # a malformed answer gives none
            self.assertNotIn('runners', pool.host_row(m, {'slots_free': 1, 'runner_labels': bad}), msg=repr(bad))
        self.assertEqual(pool.host_row(m, {'slots_free': 1, 'runner_labels': {}})['runners'], {})
        self.assertEqual(pool.host_row(m, {'slots_free': 1, 'runner_labels': {'a': 0}})['runners'], {'a': 0})

    def test_a_forced_probes_leases_hold_its_cards_whatever_the_callers_entry(self):
        m = self.reg.machines['hosta']
        self.assertIsNone(m.leases)
        got = pool.host_row(m, {'slots_free': 1, 'load': 0, 'gpu_free': {'0': 20000, '1': 20000},
                                'cards_leased': {'0': 1, '1': 0}})
        self.assertEqual(got['gpu_free'], {1: 20000})  # card 0's one lease is held there

    def test_the_cluster_entry_needs_the_registry_in_its_data_tree(self):
        m = self.reg.machines['cl']
        seen = []

        def run(argv, **kw):
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 0, CAPACITY.encode(), b'')
        pool.probe_cluster(m, run, etc=self.etc)
        cp = next(a for a in seen[0] if a.startswith('ControlPath='))
        self.assertTrue(cp.startswith('ControlPath=' + os.path.join(self.tmp.name, 'ci-runners', 'state', 'slurm',
                                                                    'cluster1', 'cm')), cp)
        other = os.path.join(self.tmp.name, 'elsewhere')
        shutil.copytree(self.etc, other)
        self.assertEqual(pool.probe_cluster(m, run, etc=other)[0]['problems'],
                         ['the registry is not in <data>/ci-runners/etc'])
        with open(self.site, 'wb') as f:
            f.write(b'\xff\xfe not text')
        self.assertEqual(pool.probe_cluster(m, run, etc=self.etc)[0]['problems'], ['site config: invalid'])

    def test_the_etc_defaults_to_the_accounts(self):
        with unittest.mock.patch.dict(os.environ, {'CI_RUNNERS_ETC': self.etc}):
            self.assertEqual(pool.probe_key('', 'hosta'), self.key)
        self.assertEqual(pool.probe_key('/e', 'hosta'), '/e/pool/keys/hosta')

    def test_pool_run_refuses_a_ci_accounts_entry(self):
        err = io.StringIO()
        with unittest.mock.patch('sys.stderr', err):
            for only in ('hosta', 'cl', 'cl:cpu'):
                self.assertEqual(pool.pool_run(self.reg, pool.RunArgs('true', ROOT, pool.Ask()), only=only, rows=[]),
                                 pool.EXIT_USAGE)
        self.assertIn("cl:cpu: a CI account's entry, for pool status only", err.getvalue())

    def test_the_ssh_cluster_probe_reads_only_a_good_answer(self):
        m = reg().machines['cl']
        bad = pool.probe_cluster(m, lambda a, **k: subprocess.CompletedProcess(a, 1, CAPACITY, ''))
        self.assertEqual(bad[0]['problems'], ['cluster unreachable'])
        good = pool.probe_cluster(m, lambda a, **k: subprocess.CompletedProcess(a, 0, CAPACITY, ''))
        self.assertNotEqual(good[0]['problems'], ['cluster unreachable'])

    def test_pool_probe_reads_the_drain_the_queue_and_the_leases_its_registry_names(self):
        lock = os.path.join(self.tmp.name, 'x.lock')
        open(lock, 'w').close()
        queue = os.path.join(self.tmp.name, 'q')
        os.mkdir(queue)
        open(os.path.join(queue, 'a.ticket'), 'w').close()
        drain = os.path.join(self.tmp.name, 'drain.json')
        with open(drain, 'w') as f:
            json.dump({'host': 1, 'pools': {}}, f)
        leases = os.path.join(self.tmp.name, 'leases')
        os.mkdir(leases)
        text = CI_REGISTRY.replace('slots = { lock = "/tmp/x.lock", count = 1 }',
                                   f'slots = {{ lock = "{lock}", count = 1, queue = "{queue}" }}\n'
                                   f'drain = "{drain}"\ngpus = 2\ngpu_model = "3090"\n'
                                   f'leases = {{ dir = "{leases}", parts = 1 }}')
        fd = os.open(os.path.join(leases, 'card1.0.lock'), os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            p = pool.local_probe(pool.load(text))
        finally:
            os.close(fd)
        self.assertEqual((p['drained'], p['queue'], p['cards_leased']), (True, 1, {0: 0, 1: 1}))

    def test_pool_status_reads_the_etc_it_is_given(self):
        self.local_lock()
        seen = {}

        def status(reg_, **kw):
            seen.update(kw)
            return []
        with unittest.mock.patch.object(pool, 'status', status), unittest.mock.patch('sys.stdout', io.StringIO()):
            self.assertEqual(pool.main(['status', '--json'], self.etc), 0)
        self.assertEqual(seen.pop('etc'), self.etc)  # the keys and the site config beside the registry
        self.assertEqual(sorted(seen), ['cached', 'on_rows'])  # the capacity file's writer and its rows (16.5)

    def test_the_forced_command_takes_no_argument_and_reads_no_stdin(self):
        self.local_lock()
        env = dict(os.environ, CI_RUNNERS_ETC=self.etc, CI_RUNNERS_PYTHON=sys.executable,
                   SSH_ORIGINAL_COMMAND='ci-run pool run -- rm -rf /')
        ci_run = os.path.join(ROOT, 'bin', 'ci-run')
        r = subprocess.run([ci_run, 'pool', 'probe'], input='import os; os._exit(9)\n', capture_output=True, text=True,
                           env=env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)['slots_free'], 1)
        r = subprocess.run([ci_run, 'pool', 'probe', 'x'], capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 2)


DEV = '\ndev_slots = { lock = "/tmp/dev.lock", cpus = ["8-11", "12-15"], memory_gib = 16 }\n'
INV_DEV = '''schema = 1
this = "hz"
[hosts.hz]
account = "acct-x"
home = "{t}"
data = "{t}/data"
slots = 2
memory_max = "120G"
disk_min_gib = 10
gpus = []
max_temp_c = 80
cpus = "0-7"
compile_cpus = "{compile}"
pools = {{ light = {{ count = 1 }}, part = {{ count = 2, slot = {{ cpus = ["0-3", "4-7"], workers = 2, memory = "8G", speed = 1 }} }} }}
files = {{ slot_lock = "{t}/s.lock", slot_queue = "{t}/s.queue" }}
'''


def reg_dev(extra=DEV):
    """The registry with ``extra`` in hostb's entry (a local host of 16 CPUs and 64 GiB)."""
    return pool.load(REGISTRY.replace('slots = { lock = "/tmp/x.lock", count = 1 }\n',
                                      'slots = { lock = "/tmp/x.lock", count = 1 }' + extra))


class DevSlotsRegistry(unittest.TestCase):
    """Decision 16.8: the registry's ``dev_slots``, outside every CI slot."""

    def test_a_host_names_its_dev_slots(self):
        m = reg_dev().machines['hostb']
        self.assertEqual(m.dev_slots, pool.DevSlots('/tmp/dev.lock', ('8-11', '12-15'), 16))
        self.assertIsNone(reg().machines['hostb'].dev_slots)  # none named: 15.1's claim
        self.assertIsNone(reg().machines['hosta'].dev_slots)

    def test_malformed_dev_slots_are_refused(self):
        for extra, why in (
                ('\ndev_slots = { lock = "/tmp/dev.lock", cpus = ["8-11", "11-15"], memory_gib = 16 }\n', 'overlap'),
                ('\ndev_slots = { lock = "/tmp/dev.lock", cpus = ["8-16"], memory_gib = 16 }\n', 'beyond'),
                ('\ndev_slots = { lock = "/tmp/dev.lock", cpus = ["8-11"], memory_gib = 65 }\n', 'memory_gib'),
                ('\ndev_slots = { lock = "/tmp/dev.lock", cpus = ["8-11"], memory_gib = 0 }\n', 'memory_gib'),
                ('\ndev_slots = { lock = "/tmp/dev.lock", cpus = [], memory_gib = 4 }\n', 'cpusets'),
                ('\ndev_slots = { lock = "/tmp/dev.lock", cpus = ["8-x"], memory_gib = 4 }\n', 'not a cpuset'),
                ('\ndev_slots = { lock = "/tmp/dev.lock", cpus = ["11-8"], memory_gib = 4 }\n', 'not a cpuset'),
                ('\ndev_slots = { lock = "/tmp/x.lock", cpus = ["8-11"], memory_gib = 4 }\n', "CI slots'"),
                ('\ndev_slots = { lock = "/tmp/./x.lock", queue = "/tmp/dev.queue", cpus = ["8-11"], memory_gib = 4 }\n',
                 "CI slots'"),
                ('\ndev_slots = { lock = "/tmp/dev.lock", queue = "/tmp/x.queue", cpus = ["8-11"], memory_gib = 4 }\n',
                 "CI slots'"),
                ('\ndev_slots = { lock = "tmp/dev.lock", cpus = ["8-11"], memory_gib = 4 }\n', 'absolute'),
                ('\ndev_slots = { lock = "/tmp/dev.lock", cpus = ["8-11"], memory_gib = 4, share = 1 }\n', 'unknown'),
                ('\ndev_slots = { cpus = ["8-11"], memory_gib = 4 }\n', 'lock is missing')):
            with self.assertRaises(pool.RegistryError, msg=extra) as e:
                reg_dev(extra)
            self.assertIn(why, str(e.exception), extra)
        two_ci = REGISTRY.replace('slots = { lock = "/tmp/x.lock", count = 1 }\n',
                                  'slots = { lock = "/tmp/x.lock", count = 2 }' + DEV.replace('/tmp/dev.lock', '/tmp/x.2.lock'))
        with self.assertRaises(pool.RegistryError):
            pool.load(two_ci)

    def test_dev_slots_on_a_cluster_or_a_status_only_entry_are_refused(self):
        with self.assertRaises(pool.RegistryError) as e:
            pool.load(REGISTRY.replace('gate = "/fs/root/ci-runners/releases/hook/bin/ci-slurm-gate"\n',
                                       'gate = "/fs/root/ci-runners/releases/hook/bin/ci-slurm-gate"' + DEV))
        self.assertIn('host only', str(e.exception))
        with self.assertRaises(pool.RegistryError) as e:
            pool.load(REGISTRY.replace('labels = { cpu = "hosta-cpu" }\n', 'labels = { cpu = "hosta-cpu" }\nprobe = "forced"'
                                       + DEV))
        self.assertIn('pool status only', str(e.exception))

    def test_an_overlap_with_the_inventorys_ci_cpusets(self):
        from ci_runners import inventory
        inv = inventory.load(INV_DEV.format(t='/tmp/t', compile='8-9'))
        got = pool.dev_overlaps(['8-11', '12-15'], inv)
        self.assertEqual(got, ["dev slot 1 (CPUs 8-11) meets the inventory's compile_cpus (CPUs [8, 9])"])
        self.assertEqual(pool.dev_overlaps(['10-11', '12-15'], inv), [])
        self.assertEqual([n for n, _ in pool.ci_cpusets(inv)],
                         ['cpus', 'compile_cpus', 'pools.part.slot.cpus[0]', 'pools.part.slot.cpus[1]'])
        self.assertEqual(pool.dev_overlaps(['3'], inventory.load(
            INV_DEV.format(t='/tmp/t', compile='8-9').replace('cpus = "0-7"\n', ''))),
            ["dev slot 1 (CPUs 3) meets the inventory's pools.part.slot.cpus[0] (CPUs [3])"])

    def test_a_pool_whose_parts_are_pinned_to_nothing_meets_every_dev_slot(self):
        from ci_runners import inventory
        base = INV_DEV.format(t='/tmp/t', compile='8-9')
        gpu = base.replace('light = { count = 1 }', 'light = { count = 1 }, gpu = { count = 1 }')
        self.assertEqual(pool.ci_unpinned(inventory.load(gpu)), [])  # the host's cpus pin its parts
        bare = inventory.load(gpu.replace('cpus = "0-7"\n', ''))
        self.assertEqual(pool.ci_unpinned(bare), ['gpu'])  # light runs no part; part has its slots
        self.assertEqual(pool.dev_overlaps(['12-15'], bare),
                         ["pool gpu's parts are pinned to no cpuset (no slot.cpus, no host cpus): they may run on a "
                          "dev slot's CPUs"])
        comp = inventory.load(base.replace('light = { count = 1 }', 'light = { count = 1 }, compile = { count = 1 }'))
        self.assertEqual(pool.ci_unpinned(comp), [])  # compile_cpus pins it
        self.assertEqual(pool.ci_unpinned(inventory.load(
            base.replace('compile_cpus = "8-9"\n', '').replace('light = { count = 1 }',
                                                                'light = { count = 1 }, compile = { count = 1 }'))),
            ['compile'])  # a compile part is not pinned by the host's cpus

    def test_the_host_spec_names_the_dev_slots_for_a_cpu_run_only(self):
        r = reg_dev()
        a = pool.RunArgs('true', '/x', pool.Ask(cpus=4, memory_gib=8))
        spec = pool.host_spec(r, r.machines['hostb'], a, wait=False)
        self.assertEqual(spec['dev'], {'lock': '/tmp/dev.lock', 'cpus': ['8-11', '12-15'], 'queue': '', 'memory_gib': 16,
                                       'inventory': ''})
        self.assertIsNone(pool.host_spec(r, r.machines['hostb'], dataclasses.replace(a, ask=pool.Ask(cards=1)), True)['dev'])
        self.assertIsNone(pool.host_spec(reg(), reg().machines['hostb'], a, True)['dev'])


class DevSlotsPick(unittest.TestCase):
    def test_a_dev_row_is_free_by_its_dev_slots_not_its_ci_slots(self):
        r = reg_dev()
        full = row('hostb', access='local', slots_free=0, cpus_free=0, queue=4, dev_slots=2, dev_free=1, dev_queue=0)
        now, later, _ = pool.pick([full], pool.Ask(cpus=4, memory_gib=8), r)
        self.assertEqual([x['key'] for x in now], ['hostb'])
        busy = dict(full, slots_free=2, cpus_free=16, dev_free=0, dev_queue=1)
        now, later, _ = pool.pick([busy], pool.Ask(cpus=4, memory_gib=8), r)
        self.assertEqual(([x['key'] for x in now], [x['key'] for x in later]), ([], ['hostb']))
        self.assertFalse(pool.free_now(busy, pool.Ask(cpus=4, memory_gib=8), 0))
        self.assertFalse(pool.free_now(dict(full, mem_free_gib=4), pool.Ask(cpus=4, memory_gib=8), 0))

    def test_a_run_larger_than_a_dev_slot_is_skipped(self):
        r = reg_dev()
        rows = [row('hostb', access='local', dev_slots=2, dev_free=2, dev_queue=0)]
        _, _, skipped = pool.pick(rows, pool.Ask(cpus=5, memory_gib=8), r)
        self.assertEqual(skipped, ['hostb: 4 cores the smallest dev slot, 5 asked'])
        _, _, skipped = pool.pick(rows, pool.Ask(cpus=4, memory_gib=17), r)
        self.assertEqual(skipped, ['hostb: 16 GiB a dev slot, 17 asked'])
        uneven = reg_dev(DEV.replace('"12-15"', '"12-13"'))  # the run may get the smaller one
        _, _, skipped = pool.pick(rows, pool.Ask(cpus=3, memory_gib=8), uneven)
        self.assertEqual(skipped, ['hostb: 2 cores the smallest dev slot, 3 asked'])
        now, _, _ = pool.pick(rows, pool.Ask(cpus=2, memory_gib=8), uneven)
        self.assertEqual([x['key'] for x in now], ['hostb'])

    def test_waiting_in_line_by_the_dev_queue(self):
        r = reg_dev()
        r = dataclasses.replace(r, machines={**r.machines, 'hosta': dataclasses.replace(r.machines['hosta'],
                                                                                         dev_slots=r.machines['hostb'].dev_slots)})
        rows = [row('hosta', dev_slots=2, dev_free=0, dev_queue=3, queue=0), row('hostb', access='local', dev_slots=2,
                                                                                 dev_free=0, dev_queue=1, queue=9)]
        _, later, _ = pool.pick(rows, pool.Ask(cpus=2, memory_gib=4), r)
        self.assertEqual([x['key'] for x in later], ['hostb', 'hosta'])
        rows = [row('hosta', dev_slots=2, dev_free=1, dev_queue=0, cpus_free=16),
                row('hostb', access='local', dev_slots=2, dev_free=2, dev_queue=0, cpus_free=0)]
        now, _, _ = pool.pick(rows, pool.Ask(cpus=2, memory_gib=4), r)
        self.assertEqual([x['key'] for x in now], ['hostb', 'hosta'])  # by the free dev slots, not the CI cores

    def test_a_pool_run_on_a_dev_host_waits_by_its_dev_queue(self):
        r = reg_dev()
        rows = [row('hostb', access='local', slots_free=2, cpus_free=16, queue=7, dev_slots=2, dev_free=0, dev_queue=3)]
        calls, said = [], []
        orig, orig_say = pool.run_host, pool._say
        pool.run_host = lambda reg_, m, a, *x, wait, **k: calls.append((m.key, wait, pool.host_spec(reg_, m, a, wait)['dev']['lock'])) or 0
        pool._say = said.append
        try:
            self.assertEqual(pool.pool_run(r, pool.RunArgs('true', ROOT, pool.Ask(cpus=2), no_wait=True), rows=rows),
                             pool.EXIT_NO_ROOM)  # its CI slots are free, its dev slots are not: no room
            self.assertEqual(calls, [])
            self.assertEqual(pool.pool_run(r, pool.RunArgs('true', ROOT, pool.Ask(cpus=2)), rows=rows), 0)
            self.assertEqual(pool.pool_run(r, pool.RunArgs('true', ROOT, pool.Ask(cpus=2)), only='hostb', rows=rows), 0)
        finally:
            pool.run_host, pool._say = orig, orig_say
        self.assertEqual(calls, [('hostb', True, '/tmp/dev.lock')] * 2)
        self.assertIn('no machine free now; waiting in line on hostb (dev queue 3)', said)

    def test_the_row_and_the_table_show_the_dev_slots(self):
        m = reg_dev().machines['hostb']
        p = {'slots_free': 0, 'queue': 2, 'load': 1.0, 'cpus': 16, 'mem_free_gib': 30, 'dev_free': 1, 'dev_queue': 0}
        got = pool.host_row(m, p)
        self.assertEqual((got['slots_free'], got['cpus_free'], got['dev_slots'], got['dev_free'], got['dev_queue']),
                         (0, 0, 2, 1, 0))  # the CI figures stay the CI slots'
        self.assertEqual(pool.host_row(m, None, 'unreachable')['dev_free'], 0)
        self.assertNotIn('dev_slots', pool.host_row(reg().machines['hostb'], p))
        self.assertIn('0/1 dev 1/2', pool.table([dict(got, key='hostb', access='local')]))

    def test_the_probe_reads_the_dev_slots_without_taking_them(self):
        with tempfile.TemporaryDirectory() as d:
            ci, dev = os.path.join(d, 's.lock'), os.path.join(d, 'dev.lock')
            open(ci, 'w').close()
            q = slots.SlotQueue(dev, 2, None, 0.1, default_project='proj')
            q.take_ticket('proj', 'pool run')  # a ticket as a waiting run writes it
            open(os.path.join(d, 'dev.queue', '000000000009.wait'), 'w').close()
            got = pool.probe(ci, 1, '', dev_lock=dev, dev_count=2)
            self.assertEqual((got['slots_free'], got['dev_free'], got['dev_queue']), (1, 2, 2))  # missing: free
            fd = os.open(os.path.join(d, 'dev.2.lock'), os.O_CREAT | os.O_WRONLY)
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                self.assertEqual(pool.probe(ci, 1, '', dev_lock=dev, dev_count=2)['dev_free'], 1)
            finally:
                os.close(fd)
            self.assertNotIn('dev_free', pool.probe(ci, 1, ''))


    def test_the_probe_of_a_host_reads_its_dev_slots(self):
        with tempfile.TemporaryDirectory() as d:
            m = dataclasses.replace(reg().machines['hosta'], slots=pool.Slots(os.path.join(d, 's.lock'), 1),
                                    dev_slots=pool.DevSlots(os.path.join(d, 'dev.lock'), ('8-11', '12-15'), 16))
            open(m.slots.lock, 'w').close()
            seen = {}

            def run(argv, **kw):  # the probe's source, run here for real
                seen['input'] = kw['input']
                return subprocess.run([sys.executable, '-I', '-'], input=kw['input'], capture_output=True, text=True)
            got = pool.probe_host(m, run)
            self.assertIn(f", dev_lock={m.dev_slots.lock!r}, dev_count=2, dev_queue=''", seen['input'])
            self.assertEqual((got['slots_free'], got['dev_slots'], got['dev_free'], got['dev_queue']), (1, 2, 2, 0))
            pool.probe_host(reg().machines['hosta'], run)
            self.assertNotIn('dev_lock=', seen['input'].strip().splitlines()[-1])  # the call: none without dev_slots


class DevSlotsClaim(unittest.TestCase):
    """R7's acceptance: a pool run on a host with ``dev_slots`` takes no CI slot."""
    spec, remote, finish = Claim.spec, Claim.remote, staticmethod(Claim.finish)

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.lock = os.path.join(self.d, 'slot.lock')
        open(self.lock, 'w').close()
        self.dev = os.path.join(self.d, 'dev.lock')
        self.inv = os.path.join(self.d, 'inventory.toml')
        with open(self.inv, 'w') as f:  # CI's cpusets leave CPU 0 to the dev slot
            f.write(INV_DEV.format(t=self.d, compile='8-9').replace('"0-7"', '"1-7"').replace('"0-3"', '"1-3"'))

    def tearDown(self):
        subprocess.run(['rm', '-rf', self.d])

    def dev_spec(self, cmd, cpus=('0',), **kw):
        return self.spec(cmd, dev={'lock': self.dev, 'cpus': list(cpus), 'queue': '', 'memory_gib': 8,
                                   'inventory': self.inv}, **kw)

    def test_a_pool_run_on_dev_slots_takes_no_ci_slot(self):
        started, go = os.path.join(self.d, 'started'), os.path.join(self.d, 'go')
        part = _python_holding(self.lock, 1, ['sleep', '30'])  # every CI slot is a part's
        try:
            _wait_holding(part)
            r, _ = self.remote(self.dev_spec(f'grep Cpus_allowed_list /proc/self/status; touch {started}; '
                                             f'while [ ! -e {go} ]; do sleep 0.1; done'))
            for _ in range(300):
                if os.path.exists(started) or r.poll() is not None:
                    break
                time.sleep(0.05)
            self.assertTrue(os.path.exists(started))
            during_ci = pool.probe(self.lock, 1, '')
            during_dev = pool.probe(self.lock, 1, '', dev_lock=self.dev, dev_count=1)
            queue = os.listdir(os.path.join(self.d, 'slot.queue')) if os.path.isdir(os.path.join(self.d, 'slot.queue')) else []
            open(go, 'w').close()
            out, err = self.finish(r)
        finally:
            part.kill()
            part.wait()
            part.stderr.close()
        self.assertEqual(r.returncode, 0, err)
        self.assertIn('holding dev slot 1 of 1 (CPUs 0)', err)
        self.assertNotIn('CI slot', err)
        self.assertRegex(out, r'Cpus_allowed_list:\s*0\n')  # pinned to its dev slot's cpuset
        self.assertEqual(during_ci['slots_free'], 0)  # the part's alone: the run took none and waited for none
        self.assertEqual(during_dev['dev_free'], 0)  # its own
        self.assertEqual([n for n in queue if n.endswith(('.ticket', '.wait'))], [])  # nor waited in its line

    def test_dev_slots_full_wait_and_never_fall_back_to_a_ci_slot(self):
        marker = os.path.join(self.d, 'ran')
        holder = _python_holding(self.dev, 1, ['sleep', '30'])
        try:
            _wait_holding(holder)
            r, _ = self.remote(self.dev_spec(f'touch {marker}', rounds=2))
            out, err = self.finish(r)
        finally:
            holder.kill()
            holder.wait()
            holder.stderr.close()
        self.assertEqual(r.returncode, pool.NOT_STARTED, err)
        self.assertIn('no dev slot free on this host now', err)
        self.assertFalse(os.path.exists(marker))
        self.assertEqual(pool.probe(self.lock, 1, '')['slots_free'], 1)

    def test_a_dev_slot_that_meets_a_ci_slot_is_refused_where_the_inventory_reads(self):
        marker = os.path.join(self.d, 'ran')
        with open(self.inv, 'w') as f:
            f.write(INV_DEV.format(t=self.d, compile='8-9'))
        r, _ = self.remote(self.dev_spec(f'touch {marker}', cpus=('2-3',)))
        out, err = self.finish(r)
        self.assertEqual(r.returncode, pool.NOT_STARTED, err)
        self.assertIn("refused: dev slot 1 (CPUs 2-3) meets the inventory's cpus", err)
        self.assertFalse(os.path.exists(marker))
        self.assertFalse(os.path.exists(self.dev))  # nothing taken
        with open(self.inv, 'w') as f:
            f.write('not an inventory')
        r, _ = self.remote(self.dev_spec(f'touch {marker}', cpus=('0',)))
        out, err = self.finish(r)
        self.assertEqual(r.returncode, pool.NOT_STARTED, err)  # named, and not an inventory: refused
        self.assertIn(f'refused: the inventory named cannot be read: {self.inv}', err)
        self.assertFalse(os.path.exists(marker))

    def test_the_default_inventory_is_checked_where_it_reads_and_said_where_not(self):
        marker = os.path.join(self.d, 'ran')
        etc = os.path.join(self.d, 'etc')
        os.makedirs(etc)
        env = dict(os.environ, CI_RUNNERS_ETC=etc)
        spec = self.dev_spec(f'touch {marker}', cpus=('2-3',))
        spec['dev']['inventory'] = ''
        r, _ = self.remote(spec, env=env)
        out, err = self.finish(r)
        self.assertEqual(r.returncode, 0, err)  # none there (another account's host side): run, and said
        self.assertIn(f"dev slots: overlap with the CI slots not checked: {os.path.join(etc, 'inventory.toml')}", err)
        self.assertTrue(os.path.exists(marker))
        os.remove(marker)
        with open(os.path.join(etc, 'inventory.toml'), 'w') as f:
            f.write(INV_DEV.format(t=self.d, compile='8-9'))
        r, _ = self.remote(spec, env=env)
        out, err = self.finish(r)
        self.assertEqual(r.returncode, pool.NOT_STARTED, err)
        self.assertIn("refused: dev slot 1 (CPUs 2-3) meets the inventory's cpus", err)
        self.assertFalse(os.path.exists(marker))

    def test_a_run_above_the_dev_slots_memory_is_refused(self):
        marker = os.path.join(self.d, 'ran')
        r, _ = self.remote(self.dev_spec(f'touch {marker}', memory_gib=9))
        out, err = self.finish(r)
        self.assertEqual(r.returncode, pool.NOT_STARTED, err)
        self.assertIn('9 GiB asked, a dev slot holds 8', err)
        self.assertFalse(os.path.exists(marker))

    def test_a_host_without_dev_slots_keeps_the_ci_slot_claim(self):
        r, _ = self.remote(self.spec('echo ran'))
        out, err = self.finish(r)
        self.assertEqual(r.returncode, 0, err)
        self.assertIn('holding CI slot 1 of 1', err)


if __name__ == '__main__':
    unittest.main()
