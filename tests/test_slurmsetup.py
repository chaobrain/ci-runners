"""``ci-run slurm check|plan|apply|status|uninstall`` (design decision 14.9; RUN-9e), against
a fake Slurm and a fake ssh (``tests/slurm_fake.py``): the CI key's gate, and the account
owner's own login (``--via``) for the steps the gate cannot take."""
from __future__ import annotations

import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

from types import SimpleNamespace

from ci_runners import slurmpart, slurmsetup as ss, slurmsite
from tests import slurm_fake

ROOT = pathlib.Path(__file__).resolve().parents[1]
EXAMPLE = (ROOT / 'setup' / 'slurm' / 'site.example.toml').read_text()
PUB = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI' + 'k' * 43
OTHER = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI' + 'o' * 43 + ' owner@laptop\n'
#: another account root's gate on the same login (a second install): not this one's to remove
ANOTHER = f'restrict,command="/elsewhere/ci-runners/releases/hook/bin/ci-slurm-gate" {PUB} ci-runners-slurm-hb\n'


def good_facts() -> dict:
    t = {'partition_up': True, 'test_only': True, 'test_only_text': ''}
    return {'python': [3, 12, 1], 'commands': {c: True for c in ss.COMMANDS}, 'slurm': [24, 5, 3], 'free_gib': 400.0,
            'writable': True, 'targets': {'cpu': dict(t), 'a40': dict(t), 'h100': dict(t)}, 'envs': []}


class Pure(unittest.TestCase):
    def setUp(self):
        self.site = slurmsite.load(EXAMPLE)

    def test_findings(self):
        self.assertEqual(ss.findings(self.site, 'acct1', good_facts()), [])
        cases = [(('python', [3, 10, 12]), 'login Python'), (('slurm', [20, 2, 1]), 'Slurm [20, 2, 1]'),
                 (('slurm', None), 'Slurm None'), (('writable', False), 'not writable'),
                 (('free_gib', 49.0), '49 GiB free, 50 needed')]
        for (k, v), why in cases:
            f = good_facts()
            f[k] = v
            got = ss.findings(self.site, 'acct1', f)
            self.assertEqual(len(got), 1, (k, got))
            self.assertIn(why, got[0])
        f = good_facts()
        f['commands']['rsync'] = False
        self.assertEqual(ss.findings(self.site, 'acct1', f), ['not on the login host: rsync'])
        f = good_facts()
        f['targets']['a40']['partition_up'] = False
        f['targets']['h100'].update(test_only=False, test_only_text='sbatch: error: Invalid account')
        self.assertEqual(ss.findings(self.site, 'acct1', f),
                         ['a40: partition gpu-partition-a is not up',
                          'h100: sbatch --test-only refused the request: sbatch: error: Invalid account'])
        f = good_facts()
        del f['targets']['cpu']
        self.assertIn('cpu: not checked', ss.findings(self.site, 'acct1', f)[0])
        self.assertEqual(ss.findings(self.site, 'acct1', {'root': '/x does not exist'}), ['/x does not exist'])
        f = good_facts()
        f['account'] = 'no account entry: run ci-run slurm apply'
        self.assertEqual(ss.findings(self.site, 'acct1', f), ['the account entry: no account entry: run ci-run slurm apply'])

    def test_retire(self):
        text = ss.retire_text(EXAMPLE, 'acct1')
        self.assertTrue(slurmsite.load(text).accounts['acct1'].retired)
        self.assertEqual(text.replace('retired = true\n', '', 1), EXAMPLE)  # one line, nothing else
        two = EXAMPLE + '\n[accounts.acct2]\nssh = "x"\nroot = "/r"\nslurm_account = "y"\ntargets = ["cpu"]\nretired = false\nmonthly_cap = 1.0\n'
        out = slurmsite.load(ss.retire_text(two, 'acct2'))
        self.assertEqual((out.accounts['acct1'].retired, out.accounts['acct2'].retired), (False, True))
        self.assertEqual(ss.retire_text(two, 'acct2').count('retired = '), 2)  # acct1's comment and acct2's line
        with self.assertRaisesRegex(ss.SetupError, 'no \\[accounts.acct9\\]'):
            ss.retire_text(EXAMPLE, 'acct9')

    def test_the_canary_and_its_cost(self):
        self.assertEqual([(t, c, n, round(x, 4)) for t, c, n, x in ss.canary_jobs(self.site, 'acct1')],
                         [('cpu', 0, 8, 0.024), ('a40', 2, 16, 0.45), ('h100', 2, 32, 1.6)])


class Site(unittest.TestCase):
    """A driver host's etc, data and keys; a cluster account's empty root and its owner's
    home, a fake Slurm on PATH and a fake ssh to the CI key's gate and to the owner's login."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = self.d = pathlib.Path(self.tmp.name)
        self.root, self.home, self.etc, self.data = d / 'root', d / 'home', d / 'etc', d / 'data'
        self.base = self.root / 'ci-runners'
        for x in (self.root, self.home / '.ssh', self.etc / 'slurm' / 'keys', self.data, d / 'slurm', d / 'ssh'):
            x.mkdir(parents=True)
        self.keys = self.home / '.ssh' / 'authorized_keys'
        self.keys.write_text(OTHER + f'restrict,command="{self.base}/releases/hook/bin/ci-slurm-gate" {PUB} '
                                     'ci-runners-slurm-ha\n' + ANOTHER)
        self.keys.chmod(0o644)  # sshd takes it so; uninstall keeps its mode
        self.site_file = self.etc / 'slurm' / 'cluster1.toml'
        self.site_file.write_text(EXAMPLE.replace('root = "/shared/fs/acct1"', f'root = "{self.root}"')
                                  .replace('login_python = "/usr/bin/python3"', f'login_python = "{sys.executable}"'))
        self.site_file.chmod(0o600)
        key = self.etc / 'slurm' / 'keys' / 'acct1'
        key.write_text('private\n')
        key.chmod(0o600)
        (self.etc / 'slurm' / 'keys' / 'acct1.pub').write_text(PUB + ' ci@hb\n')
        self.bin = slurm_fake.install(str(d / 'bin'))
        (d / 'ssh' / 'hosts.json').write_text(json.dumps({
            'cluster1-acct1': {'base': str(self.base), 'release': str(self.base / 'releases' / 'hook')},
            'owner': {'login': True, 'home': str(self.home)}}))
        env = mock.patch.dict(os.environ, {'PATH': f'{self.bin}:/usr/bin:/bin', 'FAKE_SLURM': str(d / 'slurm'),
                                           'FAKE_SSH': str(d / 'ssh')})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self.stop_jobs)

    def stop_jobs(self):
        jobs = self.d / 'slurm' / 'jobs'
        for p in jobs.glob('*.json') if jobs.exists() else []:
            slurm_fake.scancel([str(json.loads(p.read_text())['id'])])

    def ctx(self) -> tuple[ss.Ctx, io.StringIO]:
        out = io.StringIO()
        return ss.Ctx(str(self.etc), str(self.data), 'hb', ssh=os.path.join(self.bin, 'ssh'), out=out), out

    def site(self) -> slurmsite.Site:
        return slurmsite.load_file(str(self.site_file))

    def plan_file(self) -> str:
        ctx, out = self.ctx()
        self.assertEqual(ss.plan(ctx, 'cluster1', 'acct1'), 0)
        path = self.d / 'plan.txt'
        path.write_text(out.getvalue())
        return str(path)

    def apply(self) -> str:
        ctx, out = self.ctx()
        self.assertEqual(ss.apply(ctx, 'cluster1', 'acct1', self.plan_file(), 'owner'), 0)
        return out.getvalue()

    def job(self, name: str, account: str = 'lab-acct1') -> int:
        script = self.d / f'{name}.sh'
        script.write_text(f'#!/bin/sh\n#SBATCH --job-name={name}\n#SBATCH --account={account}\n'
                          '#SBATCH --partition=cpu-partition\n#SBATCH --time=10\nsleep 60\n')
        with mock.patch('sys.stdout', io.StringIO()) as out:
            slurm_fake.sbatch([str(script)])
        return int(out.getvalue())


class Plan(Site):
    def test_the_plan_lists_every_change_and_reads_no_remote_state(self):
        ctx, out = self.ctx()
        with mock.patch.object(ctx, 'run', side_effect=AssertionError('no remote call')):
            ss.plan(ctx, 'cluster1', 'acct1')
        text, b = out.getvalue(), str(self.base)
        tag = ROOT.resolve().name
        for line in (f'   {b}', f'   {b}/releases', f'   {b}/work', f'   {b}/run',
                     f'2. release {tag}, copied from this host to {b}/releases/{tag}',
                     f'{b}/releases/hook -> {tag}', f'   {b}/etc/python, mode 600: {sys.executable}',
                     f'   restrict,command="{b}/releases/hook/bin/ci-slurm-gate" {PUB} ci-runners-slurm-hb',
                     'variants: cpu, cuda13', '   a40: 2 cards, 16 CPUs, up to 0.45', '   in all up to 2.07'):
            self.assertIn(line, text)
        entry = json.dumps(slurmsite.account_entry(self.site(), 'acct1'), indent=1, sort_keys=True)
        self.assertIn('\n'.join(f'   {x}' for x in entry.splitlines()), text)
        self.assertNotIn('private', text)

    def test_a_release_named_hook_is_refused(self):
        hook = self.d / 'releases' / 'hook'
        hook.parent.mkdir()
        hook.symlink_to(ROOT)
        text = ss.plan_text(self.site(), 'acct1', release=str(hook), key=PUB, driver='hb')  # its real name counts
        self.assertIn(f'release {ROOT.resolve().name},', text)
        named = self.d / 'h' / 'hook'
        named.parent.mkdir()
        named.mkdir()
        with self.assertRaisesRegex(ss.SetupError, "release 'hook'"):
            ss.plan_text(self.site(), 'acct1', release=str(named), key=PUB, driver='hb')

    def test_the_ci_key_must_be_the_owners_only_and_ed25519(self):
        key = self.etc / 'slurm' / 'keys' / 'acct1'
        ctx, _ = self.ctx()
        key.chmod(0o640)
        with self.assertRaisesRegex(ss.SetupError, 'mode 600'):
            ss.plan(ctx, 'cluster1', 'acct1')
        key.chmod(0o600)
        (self.etc / 'slurm' / 'keys' / 'acct1.pub').write_text('ssh-rsa AAAAB3Nza x\n')
        with self.assertRaisesRegex(ss.SetupError, 'not an ssh-ed25519'):
            ss.plan(ctx, 'cluster1', 'acct1')
        key.unlink()
        with self.assertRaisesRegex(ss.SetupError, 'no CI key'):
            ss.plan(ctx, 'cluster1', 'acct1')


class Install(Site):
    def test_check_before_an_install_goes_through_the_owners_login(self):
        hosts = json.loads((self.d / 'ssh' / 'hosts.json').read_text())
        hosts['owner']['motd'] = 'Welcome {"not": "ours"}\n'  # what a login's rc files print is passed over
        (self.d / 'ssh' / 'hosts.json').write_text(json.dumps(hosts))
        ctx, out = self.ctx()
        with self.assertRaisesRegex(ss.SetupError, 'before an install, check through --via'):
            ss.check(ctx, 'cluster1', 'acct1')
        self.assertEqual(ss.check(ctx, 'cluster1', 'acct1', 'owner'), 0, out.getvalue())
        self.assertIn('0 finding(s)', out.getvalue())
        self.assertEqual(os.listdir(self.root), [])  # its temporary tree is gone
        tried = {t['--partition']: (t.get('--gpus'), t['--time']) for t in
                 json.loads((self.d / 'slurm' / 'test_only.json').read_text())}
        self.assertEqual(tried, {'cpu-partition': (None, '125'), 'gpu-partition-a': ('2', '125'),
                                 'gpu-partition-b': ('2', '65')})  # each target's largest part: its limits
        (self.d / 'slurm' / 'down.json').write_text(json.dumps(['gpu-partition-b']))
        ctx, out = self.ctx()
        self.assertEqual(ss.check(ctx, 'cluster1', 'acct1', 'owner'), 1)
        self.assertIn('finding: h100: partition gpu-partition-b is not up', out.getvalue())
        self.site_file.write_text(self.site_file.read_text().replace('gpu_request = "--gpus={n}"',
                                                                     'gpu_request = "--gres=gpu:{n}"'))
        ctx, out = self.ctx()
        self.assertEqual(ss.check(ctx, 'cluster1', 'acct1', 'owner'), 1)
        self.assertIn('finding: a40: sbatch --test-only refused the request', out.getvalue())

    def test_apply_makes_exactly_the_plan_and_the_gate_then_answers(self):
        self.base.mkdir(0o755)  # one there before: made 700
        self.base.chmod(0o755)
        said = self.apply()
        self.assertIn('the owner installs the key line of step 4', said)
        for x in ('', *ss.slurmlogin.DIRS):
            self.assertEqual((self.base / x).stat().st_mode & 0o777, 0o700, x)
        tag = ROOT.resolve().name
        rel = self.base / 'releases' / tag
        self.assertEqual(os.readlink(self.base / 'releases' / 'hook'), tag)
        files = ss.release_files(str(ROOT))
        self.assertEqual(sorted(str(p.relative_to(rel)) for p in rel.rglob('*') if p.is_file()), sorted(files))
        self.assertEqual(ss.release_digest(str(rel), files), ss.release_digest(str(ROOT), files))
        self.assertTrue(all(p.stat().st_mode & 0o077 == 0 for p in rel.rglob('*')))
        self.assertTrue(os.access(rel / 'bin' / 'ci-slurm-gate', os.X_OK))
        self.assertEqual(json.loads((self.base / 'etc' / 'account.json').read_text()),
                         slurmsite.account_entry(self.site(), 'acct1'))
        self.assertEqual((self.base / 'etc' / 'python').read_text(), f'{sys.executable}\n')
        self.assertEqual((self.base / 'etc' / 'account.json').stat().st_mode & 0o777, 0o600)
        rec = json.loads((self.data / 'ci-runners' / 'state' / 'slurm' / 'cluster1' / 'install-acct1.json').read_text())
        self.assertEqual(rec['release'], tag)
        self.assertEqual(self.keys.read_text().count(f'{self.base}/'), 1)  # authorized_keys is the owner's step
        ctx, out = self.ctx()
        self.assertEqual(ss.check(ctx, 'cluster1', 'acct1'), 0, out.getvalue())  # the CI key's gate, installed
        self.assertIn('envs []', out.getvalue())
        (rel / 'stale.txt').write_text('x')
        self.apply()  # again: the same tree
        self.assertFalse((rel / 'stale.txt').exists())
        self.assertEqual(os.readlink(self.base / 'releases' / 'hook'), tag)

    def test_apply_refuses_a_stale_plan_and_a_retired_account(self):
        plan = self.plan_file()
        self.site_file.write_text(self.site_file.read_text().replace('max_cards = 2                     # 1 where',
                                                                     'max_cards = 1  # 1 where'))
        ctx, _ = self.ctx()
        with self.assertRaisesRegex(ss.SetupError, 'the plan is stale'):
            ss.apply(ctx, 'cluster1', 'acct1', plan, 'owner')
        self.assertEqual(os.listdir(self.root), [])
        self.site_file.write_text(ss.retire_text(self.site_file.read_text(), 'acct1'))
        with self.assertRaisesRegex(ss.SetupError, 'retired'):
            ss.apply(ctx, 'cluster1', 'acct1', self.plan_file(), 'owner')
        with self.assertRaisesRegex(ss.SetupError, 'not an ssh alias'):
            ss.login_call(self.site(), '-oProxyCommand=x', 'prepare', {}, ssh='ssh', run=None)

    def test_a_refusal_on_the_login_host_is_reported(self):
        (self.root).chmod(0o500)
        self.addCleanup(self.root.chmod, 0o700)
        ctx, _ = self.ctx()
        with self.assertRaisesRegex(ss.SetupError, 'prepare on owner: refused: .*Permission denied'):
            ss.apply(ctx, 'cluster1', 'acct1', self.plan_file(), 'owner')


class Status(Site):
    def test_spend_live_jobs_and_held_targets(self):
        self.apply()
        (self.d / 'slurm' / 'plan.json').write_text(json.dumps([{'pend': -1}, {'pend': -1}]))
        self.job('ci-repo-unit-1')
        self.job('mine')
        (self.d / 'slurm' / 'ledger.json').write_text(json.dumps([{
            'JobID': '9', 'JobName': 'ci-old', 'Partition': 'gpu-partition-b', 'State': 'COMPLETED', 'ExitCode': '0:0',
            'ElapsedRaw': '3600', 'AllocTRES': 'cpu=16,gres/gpu=1', 'Start': '2026-01-01T00:00:00', 'TimelimitRaw': '60'}]))
        slurmpart.hold_one_card(str(self.data), 'cluster1', 'a40', 'p2p: copies read zeros')
        ctx, out = self.ctx()
        self.assertEqual(ss.status(ctx, 'cluster1'), 0)
        text = out.getvalue()
        self.assertIn('acct1: release ', text)
        self.assertIn('month 16.', text)  # one h100 card-hour, and the two pending jobs at their limits
        self.assertIn('1 ci- job(s) live\n  job 1001 ci-repo-unit-1 PENDING', text)
        self.assertNotIn('mine', text)
        self.assertIn('site cluster1: month 16.', text)
        self.assertIn('target a40: held at 1 card(s): p2p: copies read zeros', text)
        self.site_file.write_text(ss.retire_text(self.site_file.read_text(), 'acct1'))
        ctx, out = self.ctx()
        ss.status(ctx, 'cluster1')  # its gate is no longer asked; its cached rows still count
        self.assertIn('acct1: retired; month 16.', out.getvalue())
        self.assertIn('site cluster1: month 16.', out.getvalue())


class Uninstall(Site):
    def test_refused_while_a_ci_job_is_there_then_cancel_removes_everything_of_ours(self):
        self.apply()
        (self.d / 'slurm' / 'plan.json').write_text(json.dumps([{'pend': -1}, {'pend': -1}, {'pend': -1}]))
        ours, mine, elsewhere = self.job('ci-repo-unit-1'), self.job('mine'), self.job('ci-x-1', account='lab-other')
        ctx, _ = self.ctx()
        with self.assertRaisesRegex(ss.SetupError, f'ci- jobs of the account are there: {ours} '):
            ss.uninstall(ctx, 'cluster1', 'acct1', 'owner', cancel=False)
        self.assertTrue(self.base.exists())
        self.assertFalse(self.site().accounts['acct1'].retired)
        ctx, out = self.ctx()
        self.assertEqual(ss.uninstall(ctx, 'cluster1', 'acct1', 'owner', cancel=True), 0)
        states = {j['id']: slurm_fake.refresh(j)['state'] for j in
                  (json.loads(p.read_text()) for p in (self.d / 'slurm' / 'jobs').glob('*.json'))}
        self.assertEqual(states, {ours: 'CANCELLED', mine: 'PENDING', elsewhere: 'PENDING'})
        self.assertEqual(self.keys.read_text(), OTHER + ANOTHER)  # this gate's line gone, the others kept
        self.assertEqual(self.keys.stat().st_mode & 0o777, 0o644)
        self.assertFalse(self.base.exists())
        self.assertTrue(self.root.exists())
        self.assertTrue(self.site().accounts['acct1'].retired)
        self.assertEqual(self.site_file.stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.data / 'ci-runners' / 'state' / 'slurm' / 'cluster1' / 'install-acct1.json').exists())
        self.assertIn('1 ci- job(s) cancelled, 1 key line(s) removed, the tree removed', out.getvalue())


class Guards(Site):
    """The login host's steps refuse what is not theirs."""

    def test_uninstall_refuses_a_link_for_authorized_keys_or_the_tree_and_a_tree_not_ours(self):
        self.apply()
        real = self.home / 'keys.real'
        self.keys.rename(real)
        self.keys.symlink_to(real)
        ctx, _ = self.ctx()
        with self.assertRaisesRegex(ss.SetupError, 'is a link: edit it by hand'):
            ss.uninstall(ctx, 'cluster1', 'acct1', 'owner', cancel=False)
        self.assertIn('ci-slurm-gate', real.read_text())
        self.assertTrue(self.base.exists())
        self.assertTrue(os.path.islink(self.keys))
        gate = f'{self.base}/{ss.GATE}'
        with mock.patch.dict(os.environ, {'HOME': str(self.home)}):
            with self.assertRaisesRegex(ss.slurmlogin.LoginError, 'not a ci-runners tree'):
                ss.slurmlogin.uninstall(str(self.root), 'lab-acct1', gate)
            other = self.d / 'other' / 'ci-runners'
            other.parent.mkdir()
            other.symlink_to(self.base)
            with self.assertRaisesRegex(ss.slurmlogin.LoginError, 'not a directory of this account'):
                ss.slurmlogin.uninstall(str(other), 'lab-acct1', gate)
        self.assertTrue(self.base.exists())

    def test_install_refuses_a_hook_that_is_not_a_link(self):
        self.apply()
        hook = self.base / 'releases' / 'hook'
        hook.unlink()
        hook.mkdir()
        with self.assertRaisesRegex(ss.slurmlogin.LoginError, 'is not a link'):
            ss.slurmlogin.install(str(self.base), ROOT.resolve().name, {})
        with self.assertRaisesRegex(ss.slurmlogin.LoginError, 'not a plain name'):
            ss.slurmlogin.install(str(self.base), ROOT.resolve().name, {'../x': ''})


class Cli(Site):
    def test_ci_run_slurm(self):
        inv = SimpleNamespace(this='hb', host={'data': str(self.data)})  # what main reads of it
        with mock.patch('sys.stdout', io.StringIO()) as out:
            self.assertEqual(ss.main(['plan', 'cluster1', 'acct1'], inv, str(self.etc)), 0)
        self.assertIn('ci-runners-slurm-hb', out.getvalue())
        with mock.patch('sys.stderr', io.StringIO()) as err:
            self.assertEqual(ss.main(['plan', 'cluster9', 'acct1'], inv, str(self.etc)), 2)
        self.assertIn('no site config etc/slurm/cluster9.toml', err.getvalue())
        with mock.patch('sys.stderr', io.StringIO()), self.assertRaises(SystemExit):
            ss.main(['apply', 'cluster1', 'acct1', '--plan', 'x'], inv, str(self.etc))  # --via is required


if __name__ == '__main__':
    unittest.main()
