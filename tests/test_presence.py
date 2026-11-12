"""Presence (design decision 16.4): ours by unit and cgroup, the causes, idle, the two-tick
rule, the never-stop rules, enable and disable, the runner unit's ExecCondition and the
black-hole self-drain. ``systemctl`` and ``nvidia-smi`` are stubbed: no unit is touched."""
from __future__ import annotations

import contextlib
import dataclasses
import datetime
import io
import json
import os
import pathlib
import re
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from ci_runners import drain as drain_mod
from ci_runners import hostload, inventory, pool, presence, setup
from tests.test_part import Host

ROOT = pathlib.Path(__file__).resolve().parents[1]
UID = 4242
MGR = f'/user.slice/user-{UID}.slice/user@{UID}.service'
RUNNER_CG = f'0::{MGR}/ci.slice/ci-runner@hz-part-1.service\n'
PART_CG = f'0::{MGR}/ci.slice/ci-p.slice/ci-part-hz-gpu0-unit-77-1.scope\n'
SESSION_CG = f'0::/user.slice/user-{UID}.slice/session-7.scope\n'  # the same account, logged in
OTHER_CG = '0::/user.slice/user-65534.slice/user@65534.service/ci.slice/ci-runner@x.service\n'  # another manager's
#: cgroup v1 (a shared account's host): the systemd tree is the name=systemd line
V1_OURS = (f'11:cpuacct,cpu:/user.slice\n3:memory:/user.slice\n'
           f'1:name=systemd:{MGR}/ci.slice/ci-p.slice/ci-part-hz-part-1-unit-9-1.scope\n')
V1_THEIRS = (f'11:cpuacct,cpu:/user.slice\n3:memory:/user.slice\n'
             f'1:name=systemd:/user.slice/user-{UID}.slice/session-c3.scope\n')
SMI_Q = '--query-gpu=index,pci.bus_id,temperature.gpu,memory.used'
SMI_APPS = '--query-compute-apps=pid,gpu_bus_id,used_memory'


class Ours(unittest.TestCase):
    """*Ours* by systemd unit and cgroup, never by Unix account (16.4)."""

    def test_cgroup_path_v2_v1_and_hybrid(self):
        self.assertEqual(hostload.cgroup_path(RUNNER_CG), f'{MGR}/ci.slice/ci-runner@hz-part-1.service')
        self.assertEqual(hostload.cgroup_path(V1_OURS), f'{MGR}/ci.slice/ci-p.slice/ci-part-hz-part-1-unit-9-1.scope')
        hybrid = '1:name=systemd:/a/b.service\n0::/a/b.service\n'
        self.assertEqual(hostload.cgroup_path(hybrid), '/a/b.service')
        self.assertEqual(hostload.cgroup_path('1:name=systemd:/x.scope\n0::/\n'), '/x.scope')  # an empty unified line
        self.assertEqual(hostload.cgroup_path(''), '')

    def test_ours_by_unit_not_by_account(self):
        def mine(text):
            return hostload.ours_cgroup(hostload.cgroup_path(text), UID)
        self.assertTrue(mine(RUNNER_CG))
        self.assertTrue(mine(PART_CG))
        self.assertTrue(mine(f'0::{MGR}/app.slice/ci-presence.service\n'))  # another ci- unit
        self.assertFalse(mine(SESSION_CG))  # the same uid, a login: not ours
        self.assertFalse(mine(f'0::{MGR}/app.slice/run-r1.scope\n'))  # a unit of the account that is not ci-
        self.assertFalse(mine(f'0::{MGR}/ci.slice\n'))  # a slice holds no process of a unit of ours
        self.assertFalse(mine(OTHER_CG))  # another account's manager
        self.assertFalse(mine(f'0::/system.slice/ci-runner@x.service\n'))  # no user manager at all

    def test_ours_on_cgroup_v1_and_a_shared_account(self):
        """h100: every process is the shared account's and visible; the unit tells them apart."""
        files = {'/proc/10/cgroup': V1_OURS, '/proc/11/cgroup': V1_THEIRS}
        self.assertTrue(hostload.ours(10, UID, files.get))
        self.assertFalse(hostload.ours(11, UID, files.get))

    def test_a_process_we_cannot_see_is_not_ours(self):
        self.assertFalse(hostload.ours(12, UID, lambda p: ''))  # gone, or ProtectProc=invisible

    def test_the_busy_rules_card_reading_counts_what_is_not_ours(self):
        """The load record's card memory now follows ours (16.4): a visible process of a
        login session is not ours (as on h100), a part's is."""
        me = os.getuid()  # the default reading: ours by this account's own manager
        files = {'/proc/meminfo': '', '/proc/stat': '', '/proc/900/cgroup': SESSION_CG.replace(str(UID), str(me)),
                 '/proc/901/cgroup': PART_CG.replace(str(UID), str(me))}
        smi = {'--query-gpu=index,pci.bus_id': '0, 00000000:01:00.0\n',
               '--query-compute-apps=pid,gpu_bus_id,used_memory': '900, 00000000:01:00.0, 3000\n'
                                                                    '901, 00000000:01:00.0, 5000\n'}
        run = lambda argv, **_kw: subprocess.CompletedProcess(argv, 0, smi.get(argv[1], ''), '')  # noqa: E731
        r = hostload.raw('', read=lambda p: files.get(p, ''), run=run, hz=100, now=lambda: 1.0)
        self.assertEqual(r['gpu'], {'0': 3000})


class Load(unittest.TestCase):
    """A slot's CPUs kept busy by processes that are not ours: one load sample."""

    def test_the_readings(self):
        stat = 'cpu  9 9 9 9 9 9 9 9\ncpu0 100 0 0 50 0 0 0 0 0 0\ncpu1 10 10 10 50 7 1 1 1 0 0\nintr 1\n'
        self.assertEqual(hostload.cpu_jiffies(stat), {0: 100, 1: 33})  # iowait (7) is idle
        self.assertEqual(hostload.proc_ticks('5 (a b) S ' + ' '.join(['0'] * 10) + ' 30 12 0 0 0 0 0 0 777 rest'),
                         (777.0, 42))
        self.assertIsNone(hostload.proc_ticks('garbage'))
        self.assertEqual(hostload.allowed_cpus('Name: x\nCpus_allowed_list:\t0-2,8\n'), frozenset({0, 1, 2, 8}))
        self.assertEqual(hostload.cpu_set('nonsense'), frozenset())

    def test_foreign_cores_take_off_our_share(self):
        a = {'t': 0.0, 'cpu': {0: 0, 1: 0, 2: 0, 3: 0}, 'ours': {7: (5.0, 0, frozenset({0, 1})),
                                                               8: (6.0, 0, frozenset({0, 1, 2, 3}))}}
        b = {'t': 1.0, 'cpu': {0: 100, 1: 100, 2: 100, 3: 0}, 'ours': {7: (5.0, 100, frozenset({0, 1})),
                                                                     8: (6.0, 100, frozenset({0, 1, 2, 3}))}}
        s = hostload.load_between(a, b, 100)
        self.assertEqual(s['cpu'], {0: 1.0, 1: 1.0, 2: 1.0, 3: 0.0})
        # slot {0,1}: 2 busy cores, ours 1 (pid 7 pinned there) and half of pid 8's one core
        self.assertAlmostEqual(hostload.foreign_cores(s, frozenset({0, 1})), 0.5)
        # slot {2,3}: pid 7 is pinned elsewhere and adds nothing; a quarter of 8 per CPU
        self.assertAlmostEqual(hostload.foreign_cores(s, frozenset({2, 3})), 0.5)
        b['ours'][7] = (9.0, 100, frozenset({0, 1}))  # a new process on the same pid: not the same
        self.assertEqual(len(hostload.load_between(a, b, 100)['ours']), 1)
        self.assertIsNone(hostload.load_between(a, a, 100))

    def test_the_snapshot_reads_only_our_processes(self):
        files = {'/proc/stat': 'cpu0 1 0 0 0 0 0 0 0\n', '/proc/5/cgroup': RUNNER_CG, '/proc/6/cgroup': SESSION_CG,
                 '/proc/5/stat': '5 (x) S ' + ' '.join(['0'] * 10) + ' 3 4 0 0 0 0 0 0 9',
                 '/proc/5/status': 'Cpus_allowed_list:\t0-1\n', '/proc/6/stat': '6 (y) S ' + ' '.join(['0'] * 20)}
        snap = hostload.load_snapshot(UID, read=lambda p: files.get(p, ''), listdir=lambda p: ['5', '6', 'self'],
                                      now=lambda: 3.0)
        self.assertEqual(snap, {'t': 3.0, 'cpu': {0: 1}, 'ours': {5: (9.0, 7, frozenset({0, 1}))}, 'units_usec': None})
        # where our manager's cgroup can be read (v2): every unit of ours together
        files.update({'/proc/self/cgroup': f'0::{MGR}/app.slice/ci-presence.service\n',
                      f'/sys/fs/cgroup{MGR}/ci.slice/cpu.stat': 'usage_usec 5000000\n',
                      f'/sys/fs/cgroup{MGR}/app.slice/ci-envrefresh.service/cpu.stat': 'usage_usec 1000000\n',
                      f'/sys/fs/cgroup{MGR}/app.slice/other.service/cpu.stat': 'usage_usec 9000000\n'})
        dirs = {'/proc': ['5', '6'], f'/sys/fs/cgroup{MGR}/app.slice': ['ci-envrefresh.service', 'other.service']}
        snap = hostload.load_snapshot(UID, read=lambda p: files.get(p, ''), listdir=dirs.__getitem__, now=lambda: 3.0)
        self.assertEqual(snap['units_usec'], 6000000.0)

    def test_our_short_lived_work_is_not_foreign(self):
        """A compiler of ours that starts and ends within the sample is in no process list;
        the units' own CPU time caps what counts as foreign."""
        a = {'t': 0.0, 'cpu': {0: 0, 1: 0}, 'ours': {}, 'units_usec': 0.0}
        b = {'t': 1.0, 'cpu': {0: 100, 1: 100}, 'ours': {}, 'units_usec': 1.5e6}
        s = hostload.load_between(a, b, 100)
        self.assertAlmostEqual(s['foreign'], 0.5)
        self.assertAlmostEqual(hostload.foreign_cores(s, frozenset({0, 1})), 0.5)
        self.assertIsNone(hostload.load_between({**a, 'units_usec': None}, b, 100)['foreign'])
        self.assertAlmostEqual(hostload.foreign_cores({**s, 'foreign': None}, frozenset({0, 1})), 2.0)


INV = '''schema = 1
this = "hz"
[hosts.hz]
account = "acct-x"
home = "{t}"
data = "{data}"
slots = 3
memory_max = "120G"
disk_min_gib = 10
gpus = [0, 1]
max_temp_c = 80
cpus = "0-7"
compile_cpus = "4-7"
pools = {{ light = {{ count = 1, labels = ["ci-light", "hz-light"] }}, part = {part}, gpu = {{ per_card = true }}, multi = 1, compile = 1 }}
files = {{ slot_lock = "{slot}", slot_queue = "{t}/s.queue" }}
{extra}
'''
NAMES = ['hz-light-1', 'hz-part-1', 'hz-part-2', 'hz-gpu0', 'hz-gpu1', 'hz-multi-1', 'hz-compile-1']


class Fixture(unittest.TestCase):
    """A host with every class of runner, its systemctl, nvidia-smi and /proc stubbed."""
    part = '2'
    extra = 'presence = { enabled = true }'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = pathlib.Path(self.tmp.name)
        self.data = self.t / 'data'
        self.etc = self.t / 'etc'  # no pool.toml unless a test writes one: no capacity file
        self.etc.mkdir()
        (self.data / 'actions-runner').mkdir(parents=True)
        self.slot = self.t / 's.lock'
        for p in (self.slot, self.t / 's.2.lock', self.t / 's.3.lock'):
            p.write_text('')
        self.load_inv()
        self.units = {f'ci-runner@{n}.service': ('active', 'success') for n in NAMES}
        self.scope_list: list[str] = []
        self.pub_fail = False
        self.show_fail = False
        self.procs: dict[int, dict[str, str]] = {}
        self.files = {'/proc/sys/kernel/random/boot_id': 'B1\n'}
        self.smi = {SMI_Q: '0, 00000000:01:00.0, 40, 300\n1, 00000000:4D:00.0, 41, 300\n', SMI_APPS: ''}
        self.locks = ''
        self.calls: list[list[str]] = []
        self.load_sample: dict | None = None
        self.disk = 100.0
        self.access: list = []
        self.now = time.time()
        self.listdir_hook = None

    def tearDown(self):
        self.tmp.cleanup()

    def load_inv(self, extra=None, part=None):
        self.inv = inventory.load(INV.format(t=self.t, data=self.data, slot=self.slot, part=part or self.part,
                                             extra=self.extra if extra is None else extra))

    # the stubs

    def sysrun(self, argv, **_kw):
        self.calls.append(list(argv))
        if argv[0] == 'nvidia-smi':
            return subprocess.CompletedProcess(argv, 0, self.smi.get(argv[1], ''), '')
        cmd = argv[2:]
        if cmd[0] == 'show' and self.show_fail:
            return subprocess.CompletedProcess(argv, 1, '', 'Failed to connect to bus')
        if cmd[0] == 'show':
            units = [a for a in cmd[1:] if not a.startswith('--')]
            text = '\n\n'.join(f'ActiveState={self.units.get(u, ("inactive", "success"))[0]}\n'
                               f'Result={self.units.get(u, ("inactive", "success"))[1]}\nId={u}' for u in units)
            return subprocess.CompletedProcess(argv, 0, text + '\n', '')
        if cmd[0] == 'list-units' and cmd[-1] == 'ci-pub-runner@*':  # the public runners' units (18.4)
            if self.pub_fail:
                return subprocess.CompletedProcess(argv, 1, '', 'Failed to connect to bus')
            return subprocess.CompletedProcess(argv, 0, ''.join(f'{u} loaded {a} x x\n' for u, (a, _) in self.units.items()
                                                                if u.startswith('ci-pub-runner@')), '')
        if cmd[0] == 'list-units':
            return subprocess.CompletedProcess(argv, 0, ''.join(f'{s} loaded active running x\n' for s in self.scope_list), '')
        if cmd[:1] == ['--no-block'] and cmd[1] in ('stop', 'start'):
            self.units[cmd[2]] = ('inactive', 'success') if cmd[1] == 'stop' else ('active', 'success')
            return subprocess.CompletedProcess(argv, 0, '', '')
        if cmd[0] == 'is-active':
            return subprocess.CompletedProcess(argv, 3, 'inactive\n', '')
        raise AssertionError(f'unexpected command {argv}')

    def read(self, path):
        m = re.fullmatch(r'/proc/(\d+)/(cmdline|cgroup|stat|status)', path)
        if m:
            return self.procs.get(int(m.group(1)), {}).get(m.group(2), '')
        return self.files.get(path, '')

    def listdir(self, path):
        if self.listdir_hook:
            self.listdir_hook()
        if path == '/proc':
            return [str(p) for p in self.procs] + ['self']
        raise OSError(path)

    def probe(self):
        return presence.Probe(self.inv, run=self.sysrun, read=self.read, listdir=self.listdir, uid=UID,
                              load=lambda: self.load_sample, disk_free=lambda p: self.disk,
                              own_access=lambda h, uid: self.access, locks=lambda: self.locks, etc=str(self.etc))

    # helpers

    def job(self, name, pid=None):
        """A Runner.Worker of runner ``name``: a job on it."""
        pid = pid or 1000 + len(self.procs)
        root = self.data / 'actions-runner'
        self.procs[pid] = {'cmdline': f'{root}/{name}/bin/Runner.Worker\0spawnclient\0', 'cgroup': RUNNER_CG}
        return pid

    def hold_slot(self, path, pid, cgroup):
        st = os.stat(path)
        self.procs.setdefault(pid, {})['cgroup'] = cgroup
        self.locks += f'{len(self.locks)}: FLOCK  ADVISORY  WRITE {pid} {os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino} 0 EOF\n'

    def tick(self, dry_run=False):
        out = io.StringIO()
        lines: list[str] = []
        with contextlib.redirect_stdout(out):
            code = presence.tick(self.inv, self.probe(), dry_run=dry_run, now=lambda: self.now, out=lines.append)
        self.now += 20
        return code, lines, out.getvalue()

    def causes(self):
        p = self.probe()
        rs = presence.runners(self.inv)
        return presence.causes(self.inv, rs, p.facts(rs), self.units)

    def actions(self, verb):
        return [c[-1] for c in self.calls if c[:4] == ['systemctl', '--user', '--no-block', verb]]

    def log(self):
        out = []
        for f in sorted((self.data / 'ci-runners' / 'logs').glob('presence-*.log')):
            out += [json.loads(x) for x in f.read_text().splitlines()]
        return out


class Runners(Fixture):
    def test_the_runners_are_setups_with_their_resources(self):
        rs = presence.runners(self.inv)
        self.assertEqual([r.name for r in rs], [n for n, _, _ in setup.runners(self.inv)])
        by = {r.name: r for r in rs}
        self.assertEqual((by['hz-gpu1'].pool, by['hz-gpu1'].cards), ('gpu', (1,)))
        self.assertEqual(by['hz-multi-1'].cards, (0, 1))  # a multi runner leases every card
        self.assertEqual(by['hz-part-1'].cpus, frozenset(range(8)))  # no slot: the host's cpus
        self.assertEqual((by['hz-light-1'].cards, by['hz-light-1'].cpus), ((), frozenset()))
        self.load_inv(part='{ count = 2, slot = { cpus = ["0-1", "2-3"], workers = 2, memory = "8G", speed = 1.0 } }')
        by = {r.name: r for r in presence.runners(self.inv)}
        self.assertEqual((by['hz-part-2'].slot, by['hz-part-2'].cpus), (1, frozenset({2, 3})))

    def test_the_home_light_runner_is_the_only_one_never_stopped(self):
        rs = {r.name: r for r in presence.runners(self.inv)}
        self.assertFalse(presence.never_stopped(rs['hz-light-1']))  # carries ci-light
        home = presence.Runner('hz-home-1', 'home', 'light', ('hz-light',), (), None, frozenset())
        self.assertTrue(presence.never_stopped(home))
        self.assertFalse(presence.never_stopped(presence.Runner('x', 'part', 'part', ('hz-cpu',), (), None, frozenset())))


class Causes(Fixture):
    def test_no_cause_on_a_quiet_host(self):
        self.assertEqual(self.causes(), {n: [] for n in NAMES})

    def test_a_drained_host_pool_or_unreadable_state(self):
        drain_mod.change(str(self.data), True, 'gpu')
        why = self.causes()
        self.assertEqual((why['hz-gpu0'], why['hz-gpu1'], why['hz-part-1']), (['drained: pool gpu'],) * 2 + ([],))
        drain_mod.change(str(self.data), True)
        self.assertTrue(all(w[0] == 'drained: host' for w in self.causes().values()))  # light runners too
        pathlib.Path(drain_mod.state_path(str(self.data))).write_text('{')
        self.assertEqual(self.causes()['hz-part-1'], [drain_mod.UNREADABLE])

    def test_the_hosts_health_gate(self):
        self.disk = 3.0
        self.assertTrue(self.causes()['hz-light-1'][0].startswith('health: 3 GiB free'))
        self.disk = 100.0
        self.assertEqual(self.causes()['hz-light-1'], [])  # local_accounts unset: the access is not read
        self.inv.host['local_accounts'] = ['someone']
        self.access = [('/tmp/x', 'uid 4242 rw')]
        self.assertIn('shared files', self.causes()['hz-light-1'][0])
        self.access = []
        self.assertEqual(self.causes()['hz-light-1'], [])

    def test_a_card_held_by_a_process_that_is_not_ours(self):
        self.smi[SMI_APPS] = '900, 00000000:4D:00.0, 100\n901, 00000000:01:00.0, 6000\n'
        self.procs[900] = {'cgroup': SESSION_CG}  # visible, but not ours (a shared account's co-user)
        self.procs[901] = {'cgroup': PART_CG}  # our own part on card 0
        why = self.causes()
        self.assertEqual(why['hz-gpu1'], ['card 1: held by a process that is not ours'])
        self.assertEqual(why['hz-gpu0'], [])  # ours: its own runner is busy, not held
        self.assertEqual(why['hz-multi-1'], ['card 1: held by a process that is not ours'])
        self.assertEqual(why['hz-part-1'], [])
        self.procs[900] = {}  # hidden (ProtectProc=invisible): not ours either
        self.assertEqual(self.causes()['hz-gpu1'], ['card 1: held by a process that is not ours'])

    def test_a_cards_memory_that_is_not_ours(self):
        """Memory used past ours, of at least busy.gpu_mib: a process the host does not list."""
        self.smi[SMI_Q] = '0, 00000000:01:00.0, 40, 2400\n1, 00000000:4D:00.0, 41, 2047\n'
        why = self.causes()
        self.assertEqual((why['hz-gpu0'], why['hz-gpu1']), (['card 0: 2400 MiB that are not ours'], []))
        self.smi[SMI_APPS] = '901, 00000000:01:00.0, 2000\n'
        self.procs[901] = {'cgroup': PART_CG}
        self.assertEqual(self.causes()['hz-gpu0'], [])  # 400 MiB besides ours
        self.load_inv(extra='presence = { enabled = true }\nbusy = { gpu_mib = 300 }')
        self.assertEqual(self.causes()['hz-gpu0'], ['card 0: 400 MiB that are not ours'])

    def test_the_card_health_gate_on_a_per_card_host(self):
        self.smi[SMI_Q] = '0, 00000000:01:00.0, 80, 0\n'  # card 0 at the limit, card 1 unread
        why = self.causes()
        self.assertEqual(why['hz-gpu0'], ['health gate: card 0 at or above 80 C or unread'])
        self.assertEqual(why['hz-gpu1'], ['health gate: card 1 at or above 80 C or unread'])
        self.assertEqual(len(why['hz-multi-1']), 2)
        h = dict(self.inv.host, pools={'multi': 1})
        self.assertFalse(presence.per_card(h))  # without a per-card pool the part's gate does not refuse (§5.5)

    def test_a_slot_taken_by_something_that_is_not_ours(self):
        self.hold_slot(self.slot, 700, SESSION_CG)  # a local run's ci_slot.sh hold
        why = self.causes()
        # the two free slots: one runner of each pool first, so the timing runner is not starved
        self.assertEqual((why['hz-part-1'], why['hz-compile-1']), ([], []))
        self.assertEqual(why['hz-part-2'], ['slot: no free CI slot (1 of 3 held by others, 0 by our parts)'])
        self.hold_slot(self.t / 's.2.lock', 701, '')  # a holder we cannot see: not ours
        why = self.causes()
        self.assertEqual(why['hz-part-1'], [])
        self.assertEqual(why['hz-compile-1'], ['slot: no free CI slot (2 of 3 held by others, 0 by our parts)'])

    def test_no_more_part_runners_online_than_free_slots(self):
        """16.2: our busy part runners hold their slots; the online idle ones keep theirs first."""
        self.job('hz-part-1')
        self.hold_slot(self.slot, 702, PART_CG)  # its part's hold: ours, not counted as theirs
        self.hold_slot(self.t / 's.2.lock', 703, SESSION_CG)  # a local run's
        self.units['ci-runner@hz-part-2.service'] = ('inactive', 'success')
        why = self.causes()
        self.assertEqual(why['hz-part-1'], [])  # busy: it holds its slot
        self.assertEqual(why['hz-compile-1'], [])  # online: the one free slot is its
        self.assertEqual(why['hz-part-2'], ['slot: no free CI slot (1 of 3 held by others, 1 by our parts)'])

    def test_a_nightly_part_takes_every_slot(self):
        self.load_inv(extra='presence = { enabled = true }')
        inv_text = INV.format(t=self.t, data=self.data, slot=self.slot, part='2', extra='presence = { enabled = true }')
        self.inv = inventory.load(inv_text.replace('compile = 1 }', 'compile = 1, nightly = 1 }'))
        self.units['ci-runner@hz-nightly-1.service'] = ('active', 'success')
        self.assertEqual(self.causes()['hz-part-1'], [])
        self.job('hz-nightly-1')
        why = self.causes()
        self.assertTrue(why['hz-part-1'] and why['hz-part-2'] and why['hz-compile-1'])
        self.assertEqual(why['hz-nightly-1'], [])

    def test_a_timing_part_fences_the_runners_on_its_cpus(self):
        self.job('hz-compile-1')
        why = self.causes()
        self.assertEqual(why['hz-part-1'], ['timing: a timing part fences CPUs 4-7'])
        self.assertEqual((why['hz-gpu0'], why['hz-light-1'], why['hz-compile-1']), ([], [], []))
        self.load_inv(part='{ count = 2, slot = { cpus = ["0-1", "2-3"], workers = 2, memory = "8G", speed = 1.0 } }')
        self.assertEqual(self.causes()['hz-part-1'], [])  # its slot is off the compile CPUs

    def test_a_slots_cpus_kept_busy_by_others_with_hysteresis(self):
        self.load_inv(part='{ count = 2, slot = { cpus = ["0-1", "2-3"], workers = 2, memory = "8G", speed = 1.0 } }')
        self.load_sample = {'cpu': {0: 1.0, 1: 0.0, 2: 0.4, 3: 0.0}, 'ours': []}
        why = self.causes()
        self.assertEqual(why['hz-part-1'], ['load: 1.0 cores that are not ours on its 2 CPUs'])
        self.assertEqual(why['hz-part-2'], [])  # 0.4 < half of 2
        rs = presence.runners(self.inv)
        f = self.probe().facts(rs)
        f.load = {'cpu': {2: 0.6}, 'ours': []}
        self.assertEqual(presence.causes(self.inv, rs, f, self.units)['hz-part-2'], [])
        held = presence.causes(self.inv, rs, f, self.units, held_load={'hz-part-2'})['hz-part-2']
        self.assertEqual(held, ['load: 0.6 cores that are not ours on its 2 CPUs'])  # held: clears below a quarter
        f.load = {'cpu': {2: 0.4}, 'ours': []}
        self.assertEqual(presence.causes(self.inv, rs, f, self.units, held_load={'hz-part-2'})['hz-part-2'], [])

    def test_the_home_light_runner_has_no_cause_even_drained(self):
        self.inv = inventory.load(INV.format(t=self.t, data=self.data, slot=self.slot, part='2',
                                             extra='presence = { enabled = true }').replace(
            'light = { count = 1, labels = ["ci-light", "hz-light"] }', 'light = 1'))
        drain_mod.change(str(self.data), True)
        why = self.causes()
        self.assertEqual(why['hz-light-1'], [])
        self.assertEqual(why['hz-part-1'], ['drained: host'])


class Tick(Fixture):
    def test_two_idle_ticks_with_a_cause_stop_two_without_start(self):
        drain_mod.change(str(self.data), True, 'gpu')
        self.tick()
        self.assertEqual(self.actions('stop'), [])  # one tick: not yet
        self.tick()
        self.assertEqual(self.actions('stop'), ['ci-runner@hz-gpu0.service', 'ci-runner@hz-gpu1.service'])
        for _ in range(3):  # still drained: they stay down
            self.tick()
        self.assertEqual(self.actions('start'), [])
        drain_mod.change(str(self.data), False, 'gpu')
        self.tick()
        self.assertEqual(self.actions('start'), [])
        self.tick()
        self.assertEqual(self.actions('start'), ['ci-runner@hz-gpu0.service', 'ci-runner@hz-gpu1.service'])
        log = [x for x in self.log() if x.get('runner') == 'hz-gpu0']
        self.assertEqual([(x['action'], x['cause']) for x in log],
                         [('stop', 'drained: pool gpu'), ('start', 'no cause')])
        st = json.loads(pathlib.Path(presence.state_path(str(self.data))).read_text())
        self.assertEqual(st['counts']['hz-gpu0'], {'stop': 1, 'start': 1})

    def test_a_tick_between_resets_the_count(self):
        drain_mod.change(str(self.data), True, 'gpu')
        self.tick()
        drain_mod.change(str(self.data), False, 'gpu')
        self.tick()
        drain_mod.change(str(self.data), True, 'gpu')
        self.tick()
        self.assertEqual(self.actions('stop'), [])
        self.tick()
        self.assertEqual(len(self.actions('stop')), 2)

    def test_a_busy_runner_is_never_stopped(self):
        drain_mod.change(str(self.data), True)
        self.job('hz-part-1')
        self.scope_list = ['ci-part-hz-gpu0-unit-77-1.scope']
        for _ in range(3):
            self.tick()
        stopped = self.actions('stop')
        self.assertNotIn('ci-runner@hz-part-1.service', stopped)  # a Runner.Worker below it
        self.assertNotIn('ci-runner@hz-gpu0.service', stopped)  # a part scope of it
        self.assertIn('ci-runner@hz-part-2.service', stopped)
        self.assertIn('ci-runner@hz-light-1.service', stopped)  # light runners stop on a drain
        self.assertFalse([x for x in self.log() if x.get('action') == 'kept'])  # never even planned

    def test_idle_unknown_stops_nothing(self):
        drain_mod.change(str(self.data), True)
        self.listdir = lambda p: (_ for _ in ()).throw(OSError(p))
        for _ in range(3):
            self.tick()
        self.assertEqual(self.actions('stop'), [])

    def test_a_job_assigned_between_the_look_and_the_stop_is_not_killed(self):
        """The race of 16.4: the tick saw the runner idle, then a Runner.Worker appeared;
        the look just before the stop finds it and keeps the runner."""
        drain_mod.change(str(self.data), True, 'compile')
        self.tick()
        looks = []

        def hook():
            looks.append(1)
            if len(looks) == 2:  # the second listing of /proc: the one just before the stop
                self.job('hz-compile-1')
        self.listdir_hook = hook
        self.tick()
        self.assertEqual(self.actions('stop'), [])
        self.assertEqual([x['action'] for x in self.log() if x.get('runner') == 'hz-compile-1'], ['kept'])

    def test_the_home_light_runner_is_never_stopped(self):
        self.inv = inventory.load(INV.format(t=self.t, data=self.data, slot=self.slot, part='2',
                                             extra='presence = { enabled = true }').replace(
            'light = { count = 1, labels = ["ci-light", "hz-light"] }', 'light = 1'))
        drain_mod.change(str(self.data), True)
        for _ in range(3):
            self.tick()
        self.assertNotIn('ci-runner@hz-light-1.service', self.actions('stop'))
        self.assertIn('ci-runner@hz-part-1.service', self.actions('stop'))

    def test_a_runner_stopped_by_hand_is_left_alone(self):
        self.units['ci-runner@hz-part-1.service'] = ('inactive', 'success')
        for _ in range(3):
            self.tick()
        self.assertEqual(self.actions('start'), [])

    def test_a_runner_its_condition_held_back_is_started(self):
        self.units['ci-runner@hz-part-1.service'] = ('inactive', 'exec-condition')
        self.tick()
        self.tick()
        self.assertEqual(self.actions('start'), ['ci-runner@hz-part-1.service'])

    def test_disabled_starts_what_it_stopped_at_once_and_nothing_else(self):
        drain_mod.change(str(self.data), True, 'gpu')
        self.tick()
        self.tick()
        self.assertEqual(len(self.actions('stop')), 2)
        self.units['ci-runner@hz-part-1.service'] = ('inactive', 'success')  # stopped by hand
        self.units['ci-runner@hz-multi-1.service'] = ('inactive', 'exec-condition')
        self.load_inv(extra='presence = { enabled = false }')
        self.calls.clear()
        self.tick()  # one tick, the drain still on
        self.assertEqual(sorted(self.actions('start')), ['ci-runner@hz-gpu0.service', 'ci-runner@hz-gpu1.service',
                                                         'ci-runner@hz-multi-1.service'])
        self.assertEqual(self.actions('stop'), [])
        self.assertFalse([c for c in self.calls if c[0] == 'nvidia-smi'])  # nothing else read
        self.assertEqual(drain_mod.read(str(self.data))['pools'].keys(), {'gpu'})  # nor changed
        self.assertEqual(self.units['ci-runner@hz-part-1.service'][0], 'inactive')

    def test_no_table_also_starts_what_it_stopped(self):
        drain_mod.change(str(self.data), True, 'gpu')
        self.tick()
        self.tick()
        self.load_inv(extra='')
        self.tick()
        self.assertEqual(sorted(self.actions('start')), ['ci-runner@hz-gpu0.service', 'ci-runner@hz-gpu1.service'])
        self.calls.clear()
        self.tick()
        # then nothing but the units' read, and the public runners' (stopped while presence is off, 18.5)
        self.assertEqual([c[2] for c in self.calls], ['list-units', 'show'])
        self.assertFalse(pathlib.Path(presence.state_path(str(self.data))).exists() and
                         json.loads(pathlib.Path(presence.state_path(str(self.data))).read_text())['runners'])

    def test_an_unreadable_state_takes_down_runners_for_presences(self):
        """So that a rollback still brings back what presence stopped."""
        p = pathlib.Path(presence.state_path(str(self.data)))
        p.parent.mkdir(parents=True)
        p.write_text('{not json')
        self.units['ci-runner@hz-part-1.service'] = ('inactive', 'success')
        self.load_inv(extra='presence = { enabled = false }')
        self.tick()
        self.assertEqual(self.actions('start'), ['ci-runner@hz-part-1.service'])

    def test_the_dry_run_changes_nothing(self):
        drain_mod.change(str(self.data), True, 'gpu')
        self.units['ci-runner@hz-part-1.service'] = ('inactive', 'exec-condition')
        self.load_inv(extra='')  # not enabled: the dry run reads the host as if it were
        code, lines, _ = self.tick(dry_run=True)
        self.assertEqual(code, 0)
        text = '\n'.join(lines)
        self.assertIn('not enabled on this host; dry run', text)
        self.assertIn('hz-gpu0: active, idle; stop; drained: pool gpu', text)
        self.assertIn('hz-part-1: inactive (exec-condition), idle; start', text)
        self.assertIn('hz-light-1: active, idle; keep', text)
        self.assertEqual((self.actions('stop'), self.actions('start')), ([], []))
        self.assertFalse(pathlib.Path(presence.state_path(str(self.data))).exists())
        self.assertEqual(self.log(), [])

    def test_a_tick_that_dies_after_a_stop_still_owns_the_runner(self):
        """The stop's mark is on disk before the stop, so the rollback brings the runner back."""
        drain_mod.change(str(self.data), True, 'compile')
        self.tick()
        orig = self.sysrun

        def dies(argv, **kw):
            r = orig(argv, **kw)
            if argv[2:4] == ['--no-block', 'stop']:
                raise RuntimeError('the tick dies here')
            return r
        self.sysrun = dies
        with self.assertRaises(RuntimeError):
            self.tick()
        self.sysrun = orig
        self.load_inv(extra='presence = { enabled = false }')
        self.tick()
        self.assertEqual(self.actions('start'), ['ci-runner@hz-compile-1.service'])

    def test_one_tick_at_a_time(self):
        import fcntl
        drain_mod.change(str(self.data), True)
        p = pathlib.Path(presence.state_path(str(self.data)) + '.lock')
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, 'w') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            for _ in range(3):
                code, lines, _ = self.tick()
            self.assertEqual((code, lines[-1]), (0, 'presence: another tick runs; nothing done'))
        self.assertEqual(self.calls, [])

    def test_the_state_is_written_only_by_a_tick_that_acts(self):
        self.tick()
        st = json.loads(pathlib.Path(presence.state_path(str(self.data))).read_text())
        self.assertEqual(sorted(st['runners']), sorted(NAMES))
        self.assertEqual(oct(os.stat(presence.state_path(str(self.data))).st_mode & 0o777), '0o600')

    def test_the_units_unread_do_nothing(self):
        orig = self.sysrun
        self.sysrun = lambda argv, **kw: subprocess.CompletedProcess(argv, 1, '', '') if argv[2] == 'show' \
            else orig(argv, **kw)
        code, lines, _ = self.tick()
        self.assertEqual(code, 1)
        self.assertIn('cannot be read', lines[0])

    def test_the_capacity_file_is_rewritten_after_the_ticks_actions(self):
        """16.5: each enabled tick rewrites the host's own part of the capacity file, after its
        stops and starts; nothing without a registry or in a dry run; a failure is logged and
        the tick goes on."""
        order = []
        orig = self.sysrun
        self.sysrun = lambda argv, **kw: (order.append('stop' if 'stop' in argv else 'systemctl'), orig(argv, **kw))[1]

        def record(reg, etc):
            order.append(('record', reg, etc))
        with mock.patch.object(pool, 'record_local', record), mock.patch.object(pool, 'load_file', lambda p: p):
            self.tick()
            self.assertNotIn('record', [o[0] for o in order if isinstance(o, tuple)])  # no pool.toml
            (self.etc / 'pool.toml').write_text('')
            self.tick(dry_run=True)
            self.assertEqual([o for o in order if isinstance(o, tuple)], [])
            drain_mod.change(str(self.data), True, 'gpu')
            self.tick()
            order.clear()
            self.tick()  # the second tick with a cause stops the gpu runners
            want = ('record', str(self.etc / 'pool.toml'), str(self.etc))
            self.assertEqual(order[-1], want)
            self.assertIn('stop', order[:-1])
            self.assertEqual(order.count(want), 1)

        def broken(reg, etc):
            raise RuntimeError('capacity.lock busy')
        with mock.patch.object(pool, 'record_local', broken), mock.patch.object(pool, 'load_file', lambda p: p):
            code, _, out = self.tick()
        self.assertEqual(code, 0)
        self.assertIn('"capacity": "not written"', out)
        self.assertIn('RuntimeError: capacity.lock busy', [x.get('error') for x in self.log()])
        self.assertTrue(presence.read_state(str(self.data))[1])

class Race(Host):
    def test_the_executors_refusal_still_fires_after_a_tick(self):
        """The resource changes between a tick and the job's start: the part's own gate
        refuses (the backstop of 16.4)."""
        inv = inventory.load(self.inv_text + 'presence = { enabled = true }\n')
        units = {'ci-runner@hb-light-1.service': ('active', 'success'), 'ci-runner@hb-part-1.service': ('active', 'success')}
        rs = presence.runners(inv)
        probe = presence.Probe(inv, run=lambda argv, **kw: subprocess.CompletedProcess(argv, 0, '', ''),
                               read=lambda p: '', listdir=lambda p: [], uid=UID, load=lambda: None,
                               disk_free=lambda p: 100.0, locks=lambda: '')
        self.assertEqual(presence.causes(inv, rs, probe.facts(rs), units)['hb-part-1'], [])  # online: no cause
        drain_mod.change(str(self.data), True)  # then the host drains, before the job starts
        code, out = self.execute()
        self.assertEqual((out['outcome'], out['reason']), ('unavailable', 'drained: host'))


class Check(Fixture):
    def check(self, name, probe=None):
        lines = []
        return presence.check(self.inv, probe or self.probe(), name, out=lines.append), lines

    def test_a_cause_holds_the_start(self):
        drain_mod.change(str(self.data), True, 'gpu')
        self.units['ci-runner@hz-gpu0.service'] = ('activating', '')
        self.assertEqual(self.check('hz-gpu0'), (presence.HOLD, ['presence: hz-gpu0 held back: drained: pool gpu']))
        self.assertEqual(self.check('hz-part-1')[0], 0)
        self.assertEqual(self.check('no-such-runner')[0], 0)

    def test_a_light_runners_start_reads_no_card(self):
        self.assertEqual(self.check('hz-light-1')[0], 0)
        self.assertFalse([c for c in self.calls if c[0] == 'nvidia-smi'])
        self.smi[SMI_Q] = '0, 00000000:01:00.0, 90, 0\n1, 00000000:4D:00.0, 41, 0\n'
        self.assertEqual(self.check('hz-gpu0')[0], presence.HOLD)
        self.assertEqual(self.check('hz-gpu1')[0], 0)

    def test_disabled_or_never_stopped_or_an_error_lets_it_start(self):
        drain_mod.change(str(self.data), True)
        self.load_inv(extra='presence = { enabled = false }')
        self.assertEqual(self.check('hz-gpu0')[0], 0)
        self.load_inv(extra='presence = { enabled = true }')
        broken = self.probe()
        broken.facts = lambda rs, **kw: 1 / 0
        code, lines = self.check('hz-gpu0', broken)
        self.assertEqual(code, 0)
        self.assertIn('ZeroDivisionError', lines[0])
        self.inv = inventory.load(INV.format(t=self.t, data=self.data, slot=self.slot, part='2',
                                             extra='presence = { enabled = true }').replace(
            'light = { count = 1, labels = ["ci-light", "hz-light"] }', 'light = 1'))
        self.assertEqual(self.check('hz-light-1')[0], 0)

    def test_an_unread_inventory_lets_it_start(self):
        def broken():
            raise OSError('no inventory')
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(presence.main(['check', 'hz-gpu0'], broken), 0)
            self.assertEqual(presence.main(['what'], broken), 2)

    def test_the_runner_units_condition_skips_only_on_77(self):
        """The rendered ExecCondition= line, run by sh as systemd would (``$$`` is ``$``):
        77 skips the start; 0, 1 (a traceback) or 2 (an older release's unknown command) start."""
        tmpl = (ROOT / 'setup' / 'units' / 'ci-runner@.service').read_text()
        fake = self.t / 'releases' / 'hook' / 'bin' / 'ci-run'
        fake.parent.mkdir(parents=True)
        line = next(x for x in setup.render(tmpl, {'CI_DATA': str(self.t / 'd'), 'CI_ETC': '/e', 'CI_PYTHON': '/p',
                                                   'PROXY_ORDER': '', 'HOOKS_ORDER': '', 'HOOKS_CHECK': ''}).splitlines()
                    if x.startswith('ExecCondition='))
        argv = line[len('ExecCondition='):].replace('$$', '$').replace(f'{self.t}/d/ci-runners/releases', f'{self.t}/releases')
        m = re.fullmatch(r"/bin/sh -c '(.*)' presence %i", argv)
        self.assertIsNotNone(m, argv)
        for rc, want in ((77, 1), (0, 0), (1, 0), (2, 0)):
            fake.write_text(f'#!/bin/sh\n[ "$CI_RUNNERS_ETC $1 $2 $3" = "/e presence check hz-gpu0" ] || exit 9\nexit {rc}\n')
            fake.chmod(0o755)
            r = subprocess.run(['/bin/sh', '-c', m.group(1), 'presence', 'hz-gpu0'], capture_output=True, timeout=30)
            self.assertEqual(r.returncode, want, rc)

PUB_POOL = 'compile = 1, pub-gpu = { per_card = true, class = "gpu", audience = "public", labels = ["ci-pub-gpu", "hz-pub-gpu"] } }'
PUB0, PUB_OTHER = 'ci-pub-runner@hz-pub-gpu0-17.service', 'ci-pub-runner@hy-pub-gpu0-3.service'


class Public(Fixture):
    """The public runners (decisions 18.4, 18.5, 18.7): their audience, its withdrawal within
    one tick, the card a public runner holds, and the row presence writes for the issuer."""
    extra = 'presence = { enabled = true }\nisolation = "account"'

    def setUp(self):
        super().setUp()
        self.load_inv()
        self.record()

    def load_inv(self, extra=None, part=None, public=True):
        text = INV.format(t=self.t, data=self.data, slot=self.slot, part=part or self.part,
                          extra=self.extra if extra is None else extra)
        self.inv = inventory.load(text.replace('compile = 1 }', PUB_POOL) if public else text)

    def record(self, **kw):
        """The conformance check's record (18.5): passed, now, in this boot and kernel."""
        doc = {'passed': True, 'failed': [], 'time': self.now, 'boot_id': 'B1', 'kernel': os.uname().release,
               'isolation': 'account', 'groups': 'gpu', 'versions': {'ci-runners': 'v1.0.0'}, **kw}
        path = self.data / 'ci-runners' / 'state' / presence.CHECK_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({k: v for k, v in doc.items() if v is not None}))

    def row(self):
        return json.loads((self.data / 'ci-runners' / 'state' / presence.PUBLIC_FILE).read_text())

    def test_a_withdrawn_audience_stops_the_public_runners_within_one_tick(self):
        self.units[PUB0] = ('active', 'success')
        self.tick()
        self.assertEqual(self.actions('stop'), [])  # the audience holds: it runs on
        self.assertEqual(self.row()['offered'], True)
        self.record(passed=False, failed=['egress'])
        self.tick()  # one tick, busy or not, no second look
        self.assertEqual(self.actions('stop'), [PUB0])
        self.assertEqual([(x['action'], x['cause']) for x in self.log() if x.get('runner') == PUB0],
                         [('stop', 'public audience withdrawn: the conformance check failed (egress)')])
        self.assertEqual(self.row()['offered'], False)
        self.assertEqual(self.row()['cards'], [])

    def test_each_reason_withdraws_it(self):
        old = self.now - presence.CHECK_MAX_AGE - 1
        cases = [({'passed': False, 'failed': []}, 'the conformance check failed (its record)'),
                 ({'failed': ['gpu', 'mounts']}, 'the conformance check failed (gpu, mounts)'),
                 ({'failed': None}, 'the conformance check failed (its record)'),
                 ({'passed': 'true'}, 'the conformance check failed (its record)'),
                 ({'time': old}, 'the conformance check is older than 26 h'),
                 ({'time': self.now + 3600}, 'the conformance check is older than 26 h'),
                 ({'time': True}, 'the conformance check is older than 26 h'),
                 ({'time': None}, 'the conformance check is older than 26 h'),
                 ({'boot_id': 'B0'}, 'the conformance check ran in another boot or kernel'),
                 ({'kernel': '1.0.0'}, 'the conformance check ran in another boot or kernel'),
                 ({'isolation': 'ns-subuid'}, "the conformance check proved 'ns-subuid', not the inventory's 'account'"),
                 ({'groups': 'gpu,ci'}, 'the job keeps more than the GPU group (mode (a) is required, 18.4)'),
                 ({'groups': None}, 'the job keeps more than the GPU group (mode (a) is required, 18.4)')]
        for kw, why in cases:
            with self.subTest(kw=kw):
                self.units[PUB0] = ('active', 'success')
                self.calls.clear()
                self.record(**kw)
                self.tick()
                self.assertEqual(self.actions('stop'), [PUB0])
                self.assertEqual(self.log()[-1]['cause'], f'public audience withdrawn: {why}')
                self.assertEqual(self.row()['why'], why)
        self.units[PUB0] = ('active', 'success')
        self.calls.clear()
        (self.data / 'ci-runners' / 'state' / presence.CHECK_FILE).unlink()
        self.tick()
        self.assertEqual(self.actions('stop'), [PUB0])
        self.assertEqual(self.row()['check'], 'none')
        # a check of a moment ago, within a clock's skew, holds
        self.record(time=self.now + 200)
        self.assertEqual(presence.check_result(json.loads((self.data / 'ci-runners' / 'state' /
                                                           presence.CHECK_FILE).read_text()),
                                               isolation='account', boot='B1', kernel=os.uname().release,
                                               now=self.now), ('passed', ''))

    def test_the_inventory_or_presence_off_withdraws_it(self):
        for extra, public, why in (('presence = { enabled = true }\nisolation = "account"', False,
                                    'the inventory names no public pool'),
                                   ('presence = { enabled = false }\nisolation = "account"', True, 'presence is off'),
                                   ('isolation = "account"', True, 'presence is off')):
            with self.subTest(why=why):
                self.load_inv(extra=extra, public=public)
                self.units[PUB0] = ('active', 'success')
                self.calls.clear()
                self.tick()
                self.assertEqual(self.actions('stop'), [PUB0])
                self.assertEqual(self.log()[-1]['cause'], f'public audience withdrawn: {why}')
        inv = dataclasses.replace(self.inv, hosts={'hz': {k: v for k, v in self.inv.host.items() if k != 'isolation'}})
        self.assertEqual(presence.public_audience(inv, True, ('passed', '')), 'the inventory names no isolation')
        self.load_inv()
        self.assertEqual(presence.public_audience(self.inv, True, ('passed', '')), '')
        self.assertEqual(presence.public_audience(self.inv, True, ('stale', 'old')), 'old')

    def test_unread_units_never_keep_them_up(self):
        self.record(passed=False, failed=['mounts'])
        self.pub_fail = True  # the public units cannot be listed: every unit of their pattern
        self.tick()
        self.assertEqual(self.actions('stop'), ['ci-pub-runner@*'])
        self.assertEqual(self.log()[-1]['cause'], 'public audience withdrawn: the conformance check failed (mounts)')
        self.pub_fail = False
        self.calls.clear()
        self.units[PUB0] = ('active', 'success')
        self.show_fail = True  # the private runners' units cannot be read: the tick ends, after the stop
        self.assertEqual(self.tick()[0], 1)
        self.assertEqual(self.actions('stop'), [PUB0])
        self.show_fail = False
        self.load_inv(extra='presence = { enabled = false }\nisolation = "account"')
        self.units[PUB0] = ('active', 'success')
        self.calls.clear()
        self.show_fail = True  # and in the rollback too
        self.tick()
        self.assertEqual(self.actions('stop'), [PUB0])

    def test_an_unread_boot_id_is_no_boot(self):
        doc = {'passed': True, 'failed': [], 'time': self.now, 'boot_id': '', 'kernel': 'k', 'isolation': 'account',
               'groups': 'gpu'}
        self.assertEqual(presence.check_result(doc, isolation='account', boot='', kernel='k', now=self.now)[0], 'stale')
        self.assertEqual(presence.check_result({**doc, 'boot_id': 'B1'}, isolation='account', boot='B1', kernel='k',
                                               now=self.now), ('passed', ''))

    def test_a_dry_run_stops_nothing(self):
        self.record(passed=False)
        self.units[PUB0] = ('active', 'success')
        self.tick(dry_run=True)
        self.assertEqual(self.actions('stop'), [])
        self.assertFalse((self.data / 'ci-runners' / 'state' / presence.PUBLIC_FILE).exists())

    def test_a_public_runner_holds_its_card_against_the_private_runners(self):
        self.units[PUB0] = ('active', 'success')
        why = self.causes()
        held = 'public: card 0 is held by a public runner'
        self.assertEqual(why['hz-gpu0'], [held])
        self.assertEqual(why['hz-multi-1'], [held])  # every card
        self.assertEqual(why['hz-gpu1'], [])
        self.assertEqual(why['hz-part-1'], [])
        self.units[PUB0] = ('inactive', 'success')
        self.assertEqual(self.causes()['hz-gpu0'], [])
        self.units[PUB0] = ('failed', 'exit-code')
        self.assertEqual(self.causes()['hz-gpu0'], [])
        self.units[PUB0] = ('deactivating', 'success')  # still holding its lease
        self.assertEqual(self.causes()['hz-gpu0'], [held])
        self.assertEqual(self.check_of('hz-gpu0'), presence.HOLD)  # its ExecCondition= keeps it down
        self.assertEqual(self.check_of('hz-gpu1'), 0)

    def test_an_unread_or_unknown_unit_holds_every_card(self):
        unread = 'public: the public runners cannot be read'
        self.pub_fail = True
        why = self.causes()
        self.assertEqual((why['hz-gpu0'], why['hz-gpu1'], why['hz-multi-1']), ([unread], [unread], [unread]))
        self.assertEqual(why['hz-part-1'], [])  # no card, nothing read
        self.pub_fail = False
        for unit in (PUB_OTHER, 'ci-pub-runner@hz-pub-gpu-1.service', 'ci-pub-runner@hz-pub-gpu0.service',
                     'ci-pub-runner@hz-gpu0-1.service'):
            with self.subTest(unit=unit):
                self.units = {u: v for u, v in self.units.items() if not u.startswith('ci-pub-')}
                self.units[unit] = ('active', 'success')
                self.assertEqual(self.causes()['hz-gpu1'], [unread])
        self.assertEqual(presence.public_card('ci-pub-runner@hz-pub-gpu1-9', 'hz'), 1)
        self.assertEqual(presence.public_card('ci-pub-runner@hz-pub-gpu12-9.service', 'hz'), 12)
        self.assertIsNone(presence.public_card('ci-runner@hz-pub-gpu1-9.service', 'hz'))
        self.assertIsNone(presence.public_card('ci-pub-runner@hz-pub-gpu1-9.service.x', 'hz'))
        self.assertEqual(presence.public_cards([], 'hz'), set())
        self.assertIsNone(presence.public_cards(None, 'hz'))

    def test_a_light_runner_s_start_reads_no_public_unit(self):
        self.pub_fail = True
        self.assertEqual(self.check_of('hz-light-1'), 0)
        self.assertEqual(self.check_of('hz-part-1'), 0)
        self.assertFalse([c for c in self.calls if c[-1] == 'ci-pub-runner@*'])

    def test_the_row_offers_the_free_cards_only(self):
        self.units[PUB0] = ('active', 'success')
        self.tick()
        r = self.row()
        self.assertEqual({k: r[k] for k in ('offered', 'cards', 'running', 'check', 'isolation', 'proven', 'groups', 'why')},
                         {'offered': True, 'cards': [1], 'running': [0], 'check': 'passed', 'isolation': 'account',
                          'proven': 'account', 'groups': 'gpu', 'why': ''})
        self.assertEqual(r['age_s'], 0)
        self.assertEqual(r['time'], self.now - 20)
        self.job('hz-gpu1')  # a private job on card 1
        self.tick()
        self.assertEqual(self.row()['cards'], [])
        self.procs.clear()
        self.job('hz-multi-1')  # a two-card part holds every card
        self.units[PUB0] = ('inactive', 'success')
        self.tick()
        self.assertEqual(self.row()['cards'], [])
        self.procs.clear()
        self.tick()
        self.assertEqual(self.row()['cards'], [0, 1])
        self.smi[SMI_Q] = '0, 00000000:01:00.0, 90, 0\n1, 00000000:4D:00.0, 41, 0\n'  # card 0 too hot
        self.tick()
        self.assertEqual(self.row()['cards'], [1])
        self.listdir_hook = lambda: (_ for _ in ()).throw(OSError('no /proc'))  # idle not known: none
        self.tick()
        self.assertEqual(self.row()['cards'], [])
        self.listdir_hook = None
        self.pub_fail = True
        self.tick()
        self.assertEqual((self.row()['cards'], self.row()['running']), ([], None))

    def test_no_row_without_a_public_pool(self):
        self.load_inv(public=False)
        self.tick()
        self.assertFalse((self.data / 'ci-runners' / 'state' / presence.PUBLIC_FILE).exists())

    def test_a_row_that_cannot_be_written_is_logged(self):
        with mock.patch.object(presence, 'write_public', side_effect=OSError('disk full')):
            self.assertEqual(self.tick()[0], 0)
        self.assertEqual(self.log()[-1], {**self.log()[-1], 'public': 'not written', 'error': 'OSError: disk full'})

    def test_the_capacity_row_carries_it_fresh(self):
        self.assertIsNone(pool.public_of(dataclasses.replace(self.inv, public={})))
        stale = pool.public_of(self.inv, now=self.now)
        self.assertEqual((stale['offered'], stale['cards'], stale['check']), (False, [], 'none'))
        self.tick()
        t = self.now - 20
        fresh = pool.public_of(self.inv, now=t + 30)
        self.assertEqual({k: fresh[k] for k in ('offered', 'cards', 'check', 'isolation', 'age_s', 'why')},
                         {'offered': True, 'cards': [0, 1], 'check': 'passed', 'isolation': 'account', 'age_s': 0, 'why': ''})
        self.assertFalse(pool.public_of(self.inv, now=t + pool.PUBLIC_MAX_AGE + 1)['offered'])
        self.assertFalse(pool.public_of(self.inv, now=t - 60)['offered'])  # from the future
        path = self.data / 'ci-runners' / 'state' / presence.PUBLIC_FILE
        for bad in ({'cards': [0, '1']}, {'cards': 1}, {'offered': 'yes'}):
            with self.subTest(bad=bad):
                path.write_text(json.dumps({**self.row(), **bad}))
                got = pool.public_of(self.inv, now=t + 1)
                self.assertEqual((got['offered'], got['cards']), (False, []))
        path.write_text(json.dumps({**self.row(), 'offered': True}))
        etc = self.t / 'etc'
        with mock.patch.object(pool, '_inventory', return_value=self.inv), \
                mock.patch.object(pool, '_runners_of', return_value=[]), \
                mock.patch.object(pool, 'public_of', return_value={'offered': True}) as pub:
            self.assertEqual(pool._with_inventory({}, str(etc))['public'], {'offered': True})
            pub.return_value = None
            self.assertNotIn('public', pool._with_inventory({}, str(etc)))

    def check_of(self, name):
        return presence.check(self.inv, self.probe(), name, out=lambda _: None)


def _iso(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).isoformat(timespec='seconds')


class BlackHole(Fixture):
    def record(self, rid, outcome=None, reason='', cls='part', boot='B1', age=60, executor=None, start=0.0):
        d = self.data / 'ci-runners' / 'state' / 'parts' / 'org' / 'repo' / '77-1' / f'{rid}-1'
        d.mkdir(parents=True)
        (d / 'record.json').write_text(json.dumps({'class': cls, 'boot_start': boot, 'executor': executor or os.getpid(),
                                                   'start_epoch': start}))
        if outcome:
            (d / 'outcome.json').write_text(json.dumps({'outcome': outcome, 'reason': reason}))
        t = self.now - age
        for p in [*d.iterdir(), d]:
            os.utime(p, (t, t))

    def hook_line(self, reason, age=60, accepted=False, **kw):
        t = self.now - age
        logs = self.data / 'ci-runners' / 'logs'
        logs.mkdir(parents=True, exist_ok=True)
        with open(logs / f'hook-{datetime.datetime.fromtimestamp(t, datetime.timezone.utc):%Y-%m-%d}.log', 'a') as f:
            f.write(json.dumps({'accepted': accepted, 'reason': reason, 'runner': 'hz-part-1', 'time': _iso(t), **kw}) + '\n')

    def ends(self):
        return presence.infra_ends(str(self.data), self.now, boot='B1')

    def test_what_counts(self):
        self.record('a', 'unavailable', 'low disk: 1 GiB free under the data directory')
        self.record('o', 'unavailable', 'queue timeout: no CI slot')  # the host was busy
        self.record('p', 'unavailable', 'gpus 2: the gpu runners take 1 card (design §4.8)')  # the caller's request
        self.record('q', executor=os.getpid(), start=0.0)
        (self.data / 'ci-runners/state/parts/org/repo/77-1/q-1/record.json').write_text('{"executor": 1e999}')  # garbage
        self.record('b', 'unavailable', 'busy: cpu 17 >= 14 cores for 10 min')
        self.record('c', 'unavailable', 'drained: host')
        self.record('d', 'unavailable', 'project queue full')
        self.record('e', 'unavailable', 'pending: AssocGrpCpuLimit', cls='slurm')  # the cluster's
        self.record('f', 'lost', 'the part ended without an exit status')
        self.record('g', 'lost', '', boot='B0')  # the host rebooted: not a black hole
        self.record('h', 'red', 'exit 1')
        self.record('i', 'unavailable', 'low disk: 1 GiB free', age=16 * 60)  # outside the window
        self.record('j', executor=os.getpid(), start=0.0)  # never finished, its executor gone: lost
        self.record('k', executor=os.getpid(), start=self.now)  # never finished, its executor runs
        self.record('m', 'unavailable', drain_mod.UNREADABLE)
        self.record('n', executor=os.getpid(), start=0.0)
        (self.data / 'ci-runners/state/parts/org/repo/77-1/n-1/exit.json').write_text('{}')  # its supervisor ended
        self.hook_line('stale')
        self.hook_line('policy')
        self.hook_line('actor')  # a refusal by the policy: not the host's fault
        self.hook_line('error', accepted=True)
        self.hook_line('timeout', hook='job-completed')
        with open(next((self.data / 'ci-runners' / 'logs').glob('hook-*.log')), 'a') as f:
            f.write('[' * 100000 + '\n')  # a line too deep to parse: skipped
        got = sorted((e['why'], e.get('record', e.get('runner'))) for e in self.ends())
        self.assertEqual(got, [('hook: policy', 'hz-part-1'), ('hook: stale', 'hz-part-1'),
                               ('lost', 'org/repo/77-1/f-1'), ('lost', 'org/repo/77-1/j-1'),
                               ('unavailable: low disk: 1 GiB free under the data directory', 'org/repo/77-1/a-1')])

    def test_three_ends_in_15_min_drain_the_host_and_it_stays_drained(self):
        self.record('a', 'unavailable', 'low disk: 1 GiB free')
        self.record('b', 'lost')
        self.tick()
        self.assertIsNone(drain_mod.read(str(self.data))['host'])  # two: not yet
        self.hook_line('timeout')
        self.tick()
        self.assertIsNotNone(drain_mod.read(str(self.data))['host'])
        log = self.log()
        self.assertEqual(sorted(x['infra'] for x in log if 'infra' in x),
                         ['hook: timeout', 'lost', 'unavailable: low disk: 1 GiB free'])  # each end once
        self.assertEqual([x['drain'] for x in log if 'drain' in x], ['drained: infra'])
        self.now += 20 * 60  # the ends age out: nothing undrains by itself
        self.tick()
        self.assertIsNotNone(drain_mod.read(str(self.data))['host'])

    def test_an_undrain_counts_afresh(self):
        for rid in 'abc':
            self.record(rid, 'lost')
        self.tick()
        self.assertIsNotNone(drain_mod.read(str(self.data))['host'])
        for _ in range(3):  # the hook kept refusing while the host was drained (it reads no drain)
            self.hook_line('stale', age=5)
        self.tick()
        drain_mod.change(str(self.data), False)  # the operator, once fixed
        self.tick()
        self.tick()
        self.assertIsNone(drain_mod.read(str(self.data))['host'])  # the old ends do not drain it again
        self.assertEqual(len([x for x in self.log() if 'drain' in x]), 1)

    def test_an_error_in_the_count_never_stops_the_controller(self):
        drain_mod.change(str(self.data), True, 'compile')
        orig = presence.infra_ends
        presence.infra_ends = lambda *a, **kw: 1 / 0
        try:
            self.tick()
            self.tick()
        finally:
            presence.infra_ends = orig
        self.assertEqual(self.actions('stop'), ['ci-runner@hz-compile-1.service'])
        self.assertIn('ZeroDivisionError', [x for x in self.log() if 'error' in x][0]['error'])

    def test_a_state_from_before_the_count_is_read(self):
        """The first PR's state has no count: it is kept (an unread one would start the
        runners stopped by hand), and the count starts afresh."""
        p = pathlib.Path(presence.state_path(str(self.data)))
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps({'runners': {'hz-gpu0': {'stopped': 1.0}}, 'counts': {}}))
        st, ok = presence.read_state(str(self.data))
        self.assertTrue(ok)
        self.assertEqual((st['runners'], st['infra']['seen']), ({'hz-gpu0': {'stopped': 1.0}}, {}))

    def test_the_limit_is_the_inventorys(self):
        self.load_inv(extra='presence = { enabled = true, infra_failures = 1 }')
        self.record('a', 'lost')
        self.tick()
        self.assertIsNotNone(drain_mod.read(str(self.data))['host'])

    def test_disabled_counts_nothing(self):
        self.load_inv(extra='presence = { enabled = false }')
        for rid in 'abc':
            self.record(rid, 'lost')
        self.tick()
        self.assertIsNone(drain_mod.read(str(self.data))['host'])

    def test_the_dry_run_does_not_drain(self):
        for rid in 'abc':
            self.record(rid, 'lost')
        _, lines, _ = self.tick(dry_run=True)
        self.assertIsNone(drain_mod.read(str(self.data))['host'])
        self.assertIn('infrastructure ends in the last 15 min: 3 (the host drains at 3)', '\n'.join(lines))


class Cli(unittest.TestCase):
    def test_usage(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(presence.main(['--now'], lambda: None), 2)
        self.assertIn('usage: ci-run presence', err.getvalue())


if __name__ == '__main__':
    unittest.main()
