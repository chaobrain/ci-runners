"""The executor's ``slurm`` path (design decision 14.3, 14.4; RUN-9d), against a fake ssh and
a fake Slurm (``tests/slurm_fake.py``): the end mapping, the pending bound and the next
target, the budget, TERM, the lost driver and the reaper, the record's ``slurm`` block."""
from __future__ import annotations

import json
import os
import pathlib
import signal
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from ci_runners import inventory, outcome, part, slurmpart as sp, slurmsite
from tests import slurm_fake
from tests.test_slurmgate import Cluster, EXAMPLE

ROOT = pathlib.Path(__file__).resolve().parents[1]
SITE = (EXAMPLE.replace('pending_minutes = 3               # a target', 'pending_minutes = 0.02  # a target')
        .replace('pending_minutes = 3               # optional', 'pending_minutes = 0.02  # optional')
        .replace('enabled = false', 'enabled = ["p"]')
        .replace('enabled = ["project-a"]', 'enabled = ["p"]')
        .replace('root = "/shared/fs/acct1"', 'root = "/shared/fs/acct1"'))
ACCT2 = ('\n[accounts.acct2]\nssh = "cluster1-acct2"\nroot = "/shared/fs/acct2"\nslurm_account = "lab-acct2"\n'
         'targets = ["cpu", "a40"]\nmonthly_cap = 200.0\n')


def ledger_row(**kw) -> dict:
    r = {'JobID': '9', 'JobName': 'x', 'Partition': 'gpu-partition-b', 'State': 'COMPLETED', 'ExitCode': '0:0',
         'ElapsedRaw': '3600', 'NodeList': 'n', 'AllocTRES': 'billing=16,cpu=16,gres/gpu=2,node=1',
         'Start': '2026-11-08T01:00:00', 'End': '2026-11-08T02:00:00', 'TimelimitRaw': '60'}
    r.update(kw)
    return r


class Pure(unittest.TestCase):
    """The choice, the prices of a ledger, the budget and the end mapping."""

    def setUp(self):
        self.site = slurmsite.load(SITE + ACCT2)

    def test_the_least_spent_account_relative_to_its_weight_first_and_round_robin_on_ties(self):
        s = self.site
        self.assertEqual(sp.order(s, 'a40', {'acct1': 5.0, 'acct2': 1.0}, 0), ['acct2', 'acct1'])
        self.assertEqual(sp.order(s, 'a40', {'acct1': 0.0, 'acct2': 0.0}, 0), ['acct1', 'acct2'])
        self.assertEqual(sp.order(s, 'a40', {'acct1': 0.0, 'acct2': 0.0}, 1), ['acct2', 'acct1'])
        self.assertEqual(sp.order(s, 'h100', {}, 0), ['acct1'])  # acct2 has no h100
        heavy = slurmsite.load(SITE + ACCT2.replace('monthly_cap = 200.0', 'weight = 4.0\nmonthly_cap = 200.0'))
        self.assertEqual(sp.order(heavy, 'a40', {'acct1': 2.0, 'acct2': 4.0}, 0), ['acct2', 'acct1'])
        self.assertEqual(sp.order(s, 'a40', {'acct1': 200.0, 'acct2': 0.0}, 0), ['acct2'])  # over its own cap
        retired = slurmsite.load(SITE + ACCT2.replace('monthly_cap = 200.0', 'monthly_cap = 200.0\nretired = true'))
        self.assertEqual(sp.order(retired, 'a40', {}, 1), ['acct1'])

    def test_a_ledger_is_priced_by_the_sites_table(self):
        s = self.site
        self.assertAlmostEqual(sp.row_cost(s, ledger_row())[0], 32.0)  # 2 h100 cards, 1 h, no CPU
        self.assertAlmostEqual(sp.row_cost(s, ledger_row(Partition='cpu-partition', AllocTRES='cpu=8'))[0], 0.48)
        self.assertAlmostEqual(sp.row_cost(s, ledger_row(Partition='other', AllocTRES='cpu=1,gres/gpu:a=1'))[0], 16.0)
        cost, live = sp.row_cost(s, ledger_row(State='RUNNING', ElapsedRaw='60', TimelimitRaw='120'))
        self.assertEqual((round(cost, 6), live), (64.0, True))  # a running job at its whole limit
        self.assertEqual(sp.row_cost(s, ledger_row(State='CANCELLED by 5', ElapsedRaw='0'))[0], 0.0)
        month, day = sp.spend(s, [ledger_row(), ledger_row(Start='2026-11-01T00:00:00')], '2026-11-08')
        self.assertEqual((month, day), (64.0, 32.0))
        pending = ledger_row(State='PENDING', ElapsedRaw='0', AllocTRES='', ReqTRES='cpu=16,gres/gpu=2', TimelimitRaw='30')
        self.assertEqual(sp.row_cost(s, pending), (16.0, True))  # what it asks for, at its whole limit
        self.assertEqual(sp.row_cost(s, dict(pending, State='CANCELLED by 5'))[0], 0.0)  # never ran

    def test_ledgers_are_cached_and_a_silent_accounts_last_rows_still_count(self):
        class G:
            def __init__(self, rows):
                self.rows, self.calls = rows, 0

            def json(self, verb, month):
                self.calls += 1
                if self.rows is None:
                    raise sp.Unreachable('down')
                return {'jobs': self.rows}
        import tempfile
        with tempfile.TemporaryDirectory() as cache:
            a, b = G([{'JobID': '1'}]), G([{'JobID': '2'}])
            self.assertEqual(sp.ledgers({'a': a, 'b': b}, cache, 1000.0, '2026-11'),
                             ({'a': [{'JobID': '1'}], 'b': [{'JobID': '2'}]}, set()))
            sp.ledgers({'a': a}, cache, 1000.0 + sp.LEDGER_TTL - 1, '2026-11')
            self.assertEqual(a.calls, 1)  # cached
            a.rows = [{'JobID': '3'}]
            self.assertEqual(sp.ledgers({'a': a}, cache, 1000.0 + sp.LEDGER_TTL, '2026-11')[0], {'a': [{'JobID': '3'}]})
            sp.stale_ledger(cache, 'a')
            sp.ledgers({'a': a}, cache, 1000.0 + sp.LEDGER_TTL, '2026-11')
            self.assertEqual(a.calls, 3)  # a submit makes it stale
            b.rows = a.rows = None  # silent: the last rows of the month, however old; none of another month
            self.assertEqual(sp.ledgers({'a': a, 'b': b}, cache, 1e9, '2026-11'),
                             ({'a': [{'JobID': '3'}], 'b': [{'JobID': '2'}]}, {'a', 'b'}))
            self.assertEqual(sp.ledgers({'a': a}, cache, 1e9, '2026-12'), ({}, {'a'}))

    def test_the_staged_git_config_holds_no_credentials(self):
        text = ('[core]\n\tbare = false\n[remote "origin"]\n\turl = https://github.com/o/r\n'
                '[http "https://github.com/"]\n\textraheader = AUTHORIZATION: basic c2VjcmV0\n'
                '[includeIf "gitdir:/w/.git"]\n\tpath = /rt/git-credentials-1.config\n[branch "main"]\n\tremote = origin\n')
        self.assertEqual(sp.git_config(text), '[core]\n\tbare = false\n[remote "origin"]\n\turl = https://github.com/o/r\n'
                                              '[http "https://github.com/"]\n[branch "main"]\n\tremote = origin\n')
        self.assertEqual(sp.git_config('[http]\n\tExtraHeader=x\n'), '[http]\n')
        import tempfile
        calls = []

        class G:
            host, alias = 'h', 'a'

            def sync(self, verb, src, dst, excludes=(), delete=True):
                cfg = pathlib.Path(src) / '.git' / 'config'
                calls.append((verb, dst, tuple(excludes), delete, cfg.read_text() if cfg.exists() else None))
        with tempfile.TemporaryDirectory() as t:
            ws = pathlib.Path(t) / 'ws'
            (ws / '.git').mkdir(parents=True)
            (ws / '.git' / 'config').write_text(text)
            d = object.__new__(sp.Driver)
            d.plan, d.key, d.record = {'workspace': str(ws), 'temp': ''}, 'k', t
            d.stage(G())
        self.assertEqual(calls[0][:4], ('stage', 'h:k/ws/', ('/.git/config',), True))  # never the checkout's own
        self.assertEqual(calls[1][1:], ('h:k/ws/', (), False, sp.git_config(text)))
        self.assertEqual(len(calls), 2)

    def test_cancel_job(self):
        class G:
            def __init__(self, states):
                self.states, self.calls = list(states), []

            def json(self, verb, key, timeout=0):
                self.calls.append(verb)
                if verb == 'cancel':
                    return {'cancelled': True}
                st = self.states.pop(0)
                if st is None or st.startswith('!'):
                    raise sp.Refused(st[1:] if st else 'k has no job')
                return {'state': st}
        g = G([None])
        self.assertEqual((sp.cancel_job(g, 'k'), g.calls), ({'state': 'GONE'}, ['status']))  # nothing to cancel
        self.assertEqual(sp.cancel_job(G(['!no part k']), 'k'), {'state': 'GONE'})
        with self.assertRaisesRegex(sp.Refused, 'squeue: TimeoutExpired'):  # no answer: not gone
            sp.cancel_job(G(['!squeue: TimeoutExpired']), 'k')
        g = G(['RUNNING'])
        self.assertEqual((sp.cancel_job(g, 'k'), g.calls), (None, ['status', 'cancel']))  # its end not seen yet
        g = G(['CANCELLED'])
        self.assertEqual((sp.cancel_job(g, 'k'), g.calls), ({'state': 'CANCELLED'}, ['status']))

    def test_the_budget(self):
        s = self.site
        self.assertEqual(sp.budget_refusal(s, 'acct1', 'a40', 1, 55, {'acct1': (0.0, 0.0)}), '')
        self.assertIn("site's daily cap", sp.budget_refusal(s, 'acct1', 'h100', 2, 55, {'acct1': (0.0, 0.0)}))
        self.assertIn("site's daily cap", sp.budget_refusal(s, 'acct1', 'a40', 1, 55, {'acct1': (26.0, 26.0)}))
        self.assertIn("site's monthly cap", sp.budget_refusal(s, 'acct1', 'a40', 1, 55, {'acct1': (190.0, 0.0),
                                                                                        'acct2': (408.0, 0.0)}))
        self.assertIn("account acct1's monthly cap", sp.budget_refusal(s, 'acct1', 'a40', 1, 55, {'acct1': (196.0, 0.0)}))
        self.assertEqual(sp.budget_refusal(s, 'acct2', 'a40', 1, 55, {'acct1': (196.0, 0.0)}), '')

    def test_the_end_mapping(self):
        """Each row of 14.4's table, from the gate's last status, through finish()."""
        rec = {'attempt': 1, 'retries': 1, 'class': 'slurm', 'host': 'hb', 'pool_labels': ['hb-slurm'],
               'fallback': ['self-hosted', '3090a-slurm']}

        def end(st, stopping=False):
            ex = {'end_epoch': 1.0, **sp.end_evidence(st, stopping=stopping)}
            out = part.finish('/d/o/r/1-1/x-1', rec, ex)
            return out['outcome'], out['next'], out['reason']
        ok = {'rc': 0, 'timed_out': False, 'oom_kills': 0, 'signalled': False}
        self.assertEqual(end({'state': 'COMPLETED', 'exit': ok})[:2], ('pass', 'none'))
        self.assertEqual(end({'state': 'FAILED', 'exit': {**ok, 'rc': 3}})[:3], ('red', 'none', 'exit 3'))
        self.assertEqual(end({'state': 'FAILED', 'exit': {**ok, 'rc': 143, 'timed_out': True,
                                                           'signalled': False}})[:2], ('red', 'none'))
        o = end({'state': 'COMPLETED', 'exit': {**ok, 'rc': -9, 'oom_kills': 1}})
        self.assertEqual(o[:2], ('oom', 'retry'))
        o = end({'state': 'TIMEOUT', 'exit': None})
        self.assertEqual(o[:2], ('red', 'none'))
        self.assertIn('time limit', o[2])
        o = end({'state': 'FAILED', 'exit': {**ok, 'rc': 143, 'signalled': True}})  # the warning TERM
        self.assertEqual(o[:2], ('red', 'none'))
        self.assertIn('warning signal', o[2])
        o = end({'state': 'NODE_FAIL', 'node': 'n1', 'exit': None})
        self.assertEqual(o, ('unavailable', 'retry', 'node fail: NODE_FAIL on n1'))
        self.assertEqual(end({'state': 'BOOT_FAIL', 'exit': None})[:2], ('unavailable', 'retry'))
        o = end({'state': 'CANCELLED', 'by': 'other', 'exit': {**ok, 'rc': 143, 'signalled': True}})
        self.assertEqual(o[:2], ('unavailable', 'fallback'))
        self.assertEqual(end({'state': 'CANCELLED', 'by': '', 'exit': None})[:2], ('unavailable', 'fallback'))
        self.assertEqual(end({'state': 'OUT_OF_MEMORY', 'exit': None})[:2], ('unavailable', 'fallback'))
        self.assertEqual(end({'state': 'FAILED', 'exit': None})[:2], ('unavailable', 'fallback'))
        o = end({'state': 'COMPLETED', 'exit': {'rc': None, 'p2p': 'card 0 -> 1 read zeros', 'signalled': False}})
        self.assertEqual(o, ('unavailable', 'none', 'p2p: card 0 -> 1 read zeros'))
        self.assertEqual(end({'state': 'CANCELLED', 'by': 'us', 'exit': None})[:1], ('stopped',))
        self.assertEqual(end({'state': 'RUNNING', 'exit': None}, stopping=True)[:1], ('stopped',))
        # a node failure on the last allowed attempt is final
        out = part.finish('/d/o/r/1-1/x-2', {**rec, 'attempt': 2}, sp.end_evidence({'state': 'NODE_FAIL'}, stopping=False))
        self.assertEqual(out['next'], 'none')
        self.assertEqual(outcome.hint('unavailable', retry_here=True), outcome.Hint('retry', where='same-host'))

    def test_ssh_facts_need_both_the_host_and_the_user(self):
        import subprocess as sp_
        out = {'rc': 0, 'text': b'user labuser\nhostname login7\nport 22\n'}
        g = sp.Gate('/e', '/d', self.site, 'acct1', run=lambda argv, **kw: sp_.CompletedProcess(argv, out['rc'], out['text'], b''))
        self.assertEqual(g.ssh_facts(), {'user': 'labuser', 'hostname': 'login7'})
        out['text'] = b'hostname login7\nport 22\n'
        self.assertIsNone(g.ssh_facts())  # no user to redact: fail closed
        out.update(rc=255, text=b'user labuser\nhostname login7\n')
        self.assertIsNone(g.ssh_facts())

        def hangs(argv, **kw):
            raise sp_.TimeoutExpired(argv, kw.get('timeout'))
        g.run = hangs
        self.assertIsNone(g.ssh_facts())  # an ssh -G that hangs: no facts, and no error that names the alias
        g.run = lambda argv, **kw: sp_.CompletedProcess(argv, 0, b'user u1\nhostname h1\nproxyjump j@jump.example\n', b'')
        self.assertEqual(g.ssh_facts(), {'user': 'u1', 'hostname': 'h1', 'proxyjump': 'j@jump.example'})

    def test_the_gates_and_sshs_error_text_is_redacted(self):
        import io
        import tempfile
        d = object.__new__(sp.Driver)
        d.out, d.log_tail, d.clock, d.block = io.StringIO(), '', time.time, {}
        d.log_decoder = sp.codecs.getincrementaldecoder('utf-8')('replace')
        d.redact = slurmsite.Redactor(self.site)
        d.say('cpu@acct1: acct1: ssh: connect to host cluster1-acct1 port 22: Connection refused')
        self.assertEqual(d.out.getvalue(), 'ci-runners: cpu@<account>: <account>: ssh: connect to host <host> port 22: '
                                           'Connection refused\n')
        with tempfile.TemporaryDirectory() as t:
            d.record = t
            d.finish({'rc': None, 'unavailable': 'sbatch refused it: Invalid account lab-acct1 or partition cpu-partition',
                      'red': 'on node01', 'p2p': '/shared/fs/acct1/x'})
            ex = json.loads((pathlib.Path(t) / 'exit.json').read_text())
        self.assertEqual((ex['unavailable'], ex['red'], ex['p2p']),
                         ('sbatch refused it: Invalid account <account> or partition <partition>', 'on <node>', '<root>/x'))

    def test_an_unexpected_error_ends_the_part_unavailable_and_its_traceback_stays_here(self):
        import contextlib
        import io
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(err):
                self.assertEqual(sp.drive(t), 0)  # no plan: the driver cannot even start
            ex = json.loads((pathlib.Path(t) / 'exit.json').read_text())
            self.assertEqual((ex['rc'], ex['unavailable']), (None, 'driver: KeyError'))
            self.assertIn('Traceback', (pathlib.Path(t) / 'driver-error.txt').read_text())
            self.assertEqual(err.getvalue(), '')
        with tempfile.TemporaryDirectory() as t:
            site = pathlib.Path(t) / 'cluster1.toml'
            site.write_text(SITE)
            site.chmod(0o600)
            (pathlib.Path(t) / 'slurm-plan.json').write_text(json.dumps({'site_file': str(site), 'key': 'k'}))
            err = io.StringIO()
            with mock.patch.object(sp.Driver, 'drive', side_effect=ValueError('at /shared/fs/acct1')), \
                    contextlib.redirect_stderr(err), contextlib.redirect_stdout(err):
                self.assertEqual(sp.drive(t), 0)
            ex = json.loads((pathlib.Path(t) / 'exit.json').read_text())
            self.assertEqual(ex['unavailable'], 'driver: ValueError')
            self.assertIn('/shared/fs/acct1', (pathlib.Path(t) / 'driver-error.txt').read_text())  # this host's own
            self.assertEqual(err.getvalue(), '')
            self.assertTrue((pathlib.Path(t) / 'slurm.json').exists())  # the driver's own finish

    def test_the_job_log_is_redacted_by_whole_lines(self):
        import io
        d = object.__new__(sp.Driver)
        d.out, d.log_tail = io.StringIO(), ''
        d.redact = slurmsite.Redactor(self.site)
        d.write_log('a cpu-part')
        self.assertEqual(d.out.getvalue(), '')  # held: the line may go on
        d.write_log('ition b\nc lab-')
        self.assertEqual(d.out.getvalue(), 'a <partition> b\n')
        long = 'x ' * (sp.LOG_LINE_MAX // 2) + 'lab-'
        d.write_log(long)
        self.assertEqual(len(d.log_tail), sp.LOG_KEEP)  # a line too long goes out but its end
        d.write_log('acct1 end', end=True)
        self.assertTrue(d.out.getvalue().endswith('x <account> end'), d.out.getvalue()[-40:])
        self.assertEqual(d.log_tail, '')
        # no blank: the cut moves to the start of the value it would split
        d.out = io.StringIO()
        text = 'y' * (sp.LOG_LINE_MAX + 1) + '/shared/fs/acct1' + 'z' * (sp.LOG_KEEP - 8)
        d.write_log(text)
        self.assertEqual(d.log_tail, '/shared/fs/acct1' + 'z' * (sp.LOG_KEEP - 8))
        d.write_log('\n')
        self.assertEqual(d.out.getvalue(), 'y' * (sp.LOG_LINE_MAX + 1) + '<root>' + 'z' * (sp.LOG_KEEP - 8) + '\n')
        d.out = io.StringIO()
        d.write_log('q' * (sp.LOG_LINE_MAX + sp.LOG_KEEP))
        self.assertEqual((len(d.out.getvalue()), len(d.log_tail)), (sp.LOG_LINE_MAX, sp.LOG_KEEP))  # nothing to move

    def test_a_gates_error_text_is_redacted_before_it_is_cut(self):
        import subprocess as sp_
        red = slurmsite.Redactor(self.site)
        res = {}
        g = sp.Gate('/e', '/d', self.site, 'acct1', redact=red,
                    run=lambda argv, **kw: sp_.CompletedProcess(argv, res['rc'], b'', res['err']))
        g.ssh_argv = lambda: ['ssh']
        for rc, exc, keep in ((2, sp.Refused, 300), (255, sp.Unreachable, 200)):
            res.update(rc=rc, err=(' lab-acct1' + 'y' * (keep - 4)).encode())  # a cut at its end splits the name
            with self.assertRaises(exc) as e:
                g.call('status', 'k')
            self.assertNotIn('acct1', str(e.exception).replace('acct1:', ''), rc)
            self.assertIn('unt>', str(e.exception))  # the placeholder was cut, not the name
        g.run = lambda argv, **kw: sp_.CompletedProcess(argv, 23, b'', (' lab-acct1' + 'y' * 196).encode())
        with self.assertRaises(sp.Refused) as e:
            g.sync('stage', '/a/', 'h:k/')
        self.assertNotIn('cct1', str(e.exception))
        g.run = lambda argv, **kw: sp_.CompletedProcess(argv, 255, b'', (' lab-acct1' + 'y' * 196).encode())
        with self.assertRaises(sp.Unreachable) as e:
            g.sync('stage', '/a/', 'h:k/')
        self.assertNotIn('cct1', str(e.exception).replace('acct1:', ''))
        d = object.__new__(sp.Driver)
        d.plan, d.site, d.run, d.redact = {'etc': '/e', 'data': '/d', 'ssh': 'ssh', 'rsync': 'rsync'}, self.site, None, red
        self.assertIs(d.gate('acct1').redact, red)  # the driver's gates redact with its redactor

    def test_the_class_row(self):
        s, data = self.site, '/nonexistent'
        ok, why = sp.row(data, s, ['a40', 'cpu', 'h100', 'l40'], project='p', gpus=1, minutes=90, lock='none')
        self.assertEqual(ok, ['a40'])
        self.assertIn('cpu: gpus 1, a CPU target', why)
        self.assertIn('h100: timeout-minutes 90 above its 60', why)
        self.assertIn('l40: no target', why)
        self.assertEqual(sp.row(data, s, ['a40'], project='q', gpus=1, minutes=5, lock='none'),
                         ([], 'a40: not enabled for q (14.10)'))
        self.assertEqual(sp.row(data, s, ['a40'], project='p', gpus=3, minutes=5, lock='none')[0], [])
        self.assertEqual(sp.row(data, s, ['a40'], project='p', gpus=0, minutes=5, lock='none')[0], [])
        self.assertEqual(sp.row(data, s, ['cpu'], project='p', gpus=0, minutes=5, lock='s')[0], [])
        self.assertEqual(sp.row(data, s, ['cpu'], project='p', gpus=0, minutes=120, lock='none')[0], ['cpu'])


class Driver(Cluster):
    """A 3090 host's slurm pool driving the cluster account of ``Cluster`` (and a second
    account), through the executor."""
    site_text = SITE

    def setUp(self):
        super().setUp()
        d = self.d
        self.data, self.etc, self.ws, self.temp = d / 'data', d / 'data' / 'ci-runners' / 'etc', d / 'ws', d / 'rt'
        for x in (self.etc / 'projects', self.etc / 'slurm' / 'keys', self.ws, self.temp / '_runner_file_commands'):
            x.mkdir(parents=True)
        (self.ws / 'src.txt').write_text('src\n')
        (self.ws / '.git').mkdir()
        (self.ws / '.git' / 'config').write_text('[core]\n\tbare = false\n[http "https://github.com/"]\n'
                                                 '\textraheader = AUTHORIZATION: basic c2VjcmV0\n')
        (self.temp / 'git-credentials-1.config').write_text('[http]\n\textraheader = AUTHORIZATION: basic c2VjcmV0\n')
        (self.temp / 'input.txt').write_text('in\n')
        (self.temp / '_runner_file_commands' / 'set_output').write_text('never staged\n')
        site = self.etc / 'slurm' / 'cluster1.toml'
        site.write_text(SITE + ACCT2)
        site.chmod(0o600)
        for a in ('acct1', 'acct2'):
            (self.etc / 'slurm' / 'keys' / a).write_text('key\n')
        # acct2: a second cluster account of its own
        self.base2 = d / 'root2' / 'ci-runners'
        (self.base2 / 'etc').mkdir(parents=True)
        e2 = slurmsite.account_entry(slurmsite.load(SITE + ACCT2), 'acct2')
        e2.update(root=str(d / 'root2'), login_python=sys.executable)
        (self.base2 / 'etc' / 'account.json').write_text(json.dumps(e2))
        for v in ('cpu', 'cuda13'):
            (self.base2 / 'envs' / f'p-{v}' / 'bin').mkdir(parents=True)
        self.hosts({})
        inv = f'''schema = 1
this = "hb"
[hosts.hb]
account = "acct-x"
home = "{d}"
data = "{self.data}"
slots = 1
memory_max = "120G"
disk_min_gib = 0
gpus = [0, 1]
max_temp_c = 80
pools = {{ light = 1, slurm = {{ count = 2, labels = ["hb-slurm"] }} }}
files = {{ slot_lock = "{d}/s.lock", slot_queue = "{d}/s.queue" }}
'''
        self.inv = inventory.load(inv)
        (self.etc / 'projects' / 'p.toml').write_text(
            'name = "p"\nrepository = "Org/Repo"\ncontext = "p/ci"\nmemory = "1G"\n'
            '[fallback]\nslurm = ["self-hosted", "3090a-slurm"]\n'
            '[hosts.hb]\nmemory_max = "2G"\nvariables = { P_HOST = "hb" }\n')
        self.out = d / 'github_output'
        self.env = {'GITHUB_REPOSITORY': 'org/repo', 'GITHUB_RUN_ID': '77', 'GITHUB_RUN_ATTEMPT': '1',
                    'GITHUB_SHA': 'abc', 'GITHUB_WORKSPACE': str(self.ws), 'RUNNER_TEMP': str(self.temp),
                    'RUNNER_NAME': 'hb-slurm-1', 'CI_RUNNER_CLASS': 'slurm', 'GITHUB_OUTPUT': str(self.out),
                    'PATH': '/usr/bin:/bin', 'HOME': str(d), 'SECRET_TOKEN': 'never'}
        self.log = d / 'driver.log'

    def hosts(self, down: dict) -> None:
        h = {'cluster1-acct1': {'base': str(self.base), 'release': str(ROOT), 'down': down.get('acct1', False)},
             'cluster1-acct2': {'base': str(self.base2), 'release': str(ROOT), 'down': down.get('acct2', False)}}
        (self.d / 'ssh' / 'hosts.json').write_text(json.dumps(h))

    def executor(self) -> part.Executor:
        log = open(self.log, 'ab')
        self.addCleanup(log.close)

        def spawn(argv, **kw):
            return subprocess.Popen(argv, stdout=log, stderr=log, **kw)
        return part.Executor(self.inv, str(self.etc), self.env, release=str(ROOT), scope=False, poll=0.1, spawn=spawn,
                             slurm_tools={'ssh': os.path.join(self.bin, 'ssh'), 'rsync': '/usr/bin/rsync'},
                             slurm_env={'PATH': os.environ['PATH'], 'FAKE_SLURM': os.environ['FAKE_SLURM'],
                                        'FAKE_SSH': os.environ['FAKE_SSH']})

    def execute(self, ex: part.Executor | None = None, **kw) -> tuple[int, dict]:
        kw.setdefault('project', 'p')
        kw.setdefault('id', 'unit')
        kw.setdefault('timeout_minutes', 1)
        kw.setdefault('queue_timeout_minutes', 0.5)
        kw.setdefault('command', 'true')
        kw.setdefault('target', 'cpu')
        kw.setdefault('lock', '')
        return (ex or self.executor()).execute(part.Inputs(**kw))

    def record(self, out: dict) -> pathlib.Path:
        return self.data / 'ci-runners' / 'state' / 'parts' / out['record'].split(':', 1)[1]

    def block(self, out: dict) -> dict:
        return json.loads((self.record(out) / 'record.json').read_text())['slurm']

    def jobs(self) -> list[dict]:
        d = self.d / 'slurm' / 'jobs'
        return [slurm_fake.refresh(json.loads(p.read_text())) for p in sorted(d.glob('*.json'))] if d.exists() else []


class Ends(Driver):
    def test_a_passing_part_its_results_come_back_and_its_record_names_job_target_account_and_cost(self):
        cmd = ('cat src.txt; cat "$RUNNER_TEMP/input.txt"; test ! -e "$RUNNER_TEMP/_runner_file_commands" || exit 9; '
               'echo "host $CI_RUNNERS_HOST $P_HOST $GITHUB_SHA ${SECRET_TOKEN:-none}"; '
               'echo r > "$CI_RESULTS_DIR/r.xml"; echo t > "$RUNNER_TEMP/made.txt"')
        code, out = self.execute(command=cmd)
        self.assertEqual((code, out['outcome'], out['next']), (0, 'pass', 'none'), self.log.read_text())
        rec = self.record(out)
        self.assertEqual((rec / 'work' / 'results' / 'r.xml').read_text(), 'r\n')  # CI_PART_DIR, locally
        self.assertEqual((self.temp / 'made.txt').read_text(), 't\n')  # RUNNER_TEMP, locally
        self.assertEqual((self.temp / '_runner_file_commands' / 'set_output').read_text(), 'never staged\n')
        log = (rec / 'job.log').read_text()
        self.assertIn('src\nin\nhost hb hb abc none\n', log)
        self.assertIn('host hb hb abc none', self.log.read_text())  # relayed to the job's log
        b = self.block(out)
        self.assertEqual((b['site'], b['account'], b['target'], b['partition'], b['job'], b['state']),
                         ('cluster1', 'acct1', 'cpu', 'cpu-partition', 1001, 'COMPLETED'))
        self.assertEqual((b['cards'], b['cpus'], b['node']), (0, 8, 'fake01'))
        self.assertAlmostEqual(b['cost'], 8 * 0.06 * b['elapsed'] / 3600)
        self.assertIn('hb → cpu, job 1001, ', out['slurm'])  # not its site, account or node
        summary = (rec / 'summary.md').read_text()
        self.assertIn('| cluster | hb → cpu, job 1001, ', summary)
        self.assertIn('cost', summary)
        r = json.loads((rec / 'record.json').read_text())
        self.assertEqual(r['class'], 'slurm')
        self.assertEqual(r['run_epoch'], b['run_epoch'])  # the run starts with the job, not the drive
        self.assertGreaterEqual(r['run_epoch'], r['start_epoch'])
        self.assertEqual(b['open'], {})
        ws = self.base / 'work' / 'org' / 'repo' / '77-1' / 'unit-1' / 'ws'
        self.assertEqual((ws / '.git' / 'config').read_text(), '[core]\n\tbare = false\n[http "https://github.com/"]\n')
        self.assertEqual((ws / 'src.txt').read_text(), 'src\n')
        self.assertFalse((ws.parent / 'tmp' / 'git-credentials-1.config').exists())
        self.assertTrue((ws.parent / 'tmp' / 'input.txt').exists())
        cache = json.loads((self.data / 'ci-runners' / 'state' / 'slurm' / 'cluster1' / 'ledger-acct1.json').read_text())
        self.assertEqual(cache['at'], 0)  # read again by the next part: the job counts from its submission

    def private_site(self, **ssh):
        """The site config names the accounts' real roots, as a real one does, and acct1's
        ssh config ``ssh`` (a login host and user)."""
        site = self.etc / 'slurm' / 'cluster1.toml'
        site.write_text(site.read_text().replace('/shared/fs/acct1', str(self.d / 'root'))
                        .replace('/shared/fs/acct2', str(self.d / 'root2')))
        h = json.loads((self.d / 'ssh' / 'hosts.json').read_text())
        h['cluster1-acct1'].update(ssh)
        (self.d / 'ssh' / 'hosts.json').write_text(json.dumps(h))

    def test_what_a_part_shows_on_github_names_no_inventory(self):
        self.private_site(hostname='login7.example.org', user='labuser')
        self.env['GITHUB_STEP_SUMMARY'] = str(self.d / 'step_summary.md')
        (self.d / 'slurm' / 'nodes.json').write_text('["other[1-2]"]')  # its node: only from the job's status
        cmd = ('echo "dir $CI_PART_DIR"; echo "fake01 labuser@LOGIN7.example.org lab-acct1 cluster1-acct1 cluster1:cpu"; '
               'printf "x cpu-part"; sleep 1; printf "ition y\\nlast lab-acct1"; exit 3')
        code, out = self.execute(command=cmd)
        log = self.log.read_text()
        self.assertIn('dir <root>/ci-runners/', log)
        self.assertIn('<node> <user>@<host> <account> <host> <site>:cpu\n', log)  # the target stays
        self.assertIn('x <partition> y\n', log)  # one name, split across two reads of the job's log
        self.assertIn('last <account>', log)  # and the log's last line, without its newline
        summary = (self.record(out) / 'summary.md').read_text()
        art = self.temp / 'ci-runners' / 'unit'  # the part's uploaded artifacts
        shown = (json.dumps(out) + summary + (self.d / 'step_summary.md').read_text()
                 + (art / 'outcome.json').read_text() + (art / 'summary.md').read_text())
        self.assertIn('hb → cpu, job 1001, ', shown)  # the target, job and cost stay
        for x in (str(self.d / 'root'), 'fake01', 'labuser', 'login7', 'lab-acct1', 'cluster1-acct1', 'cpu-partition'):
            self.assertNotIn(x.lower(), log.lower())
            self.assertNotIn(x, shown)
        for x in ('cluster1', 'acct1'):  # our own names for the site and the account
            self.assertNotIn(x, shown + log.replace('ci-runners', ''))
        self.assertIn('fake01 labuser@', (self.record(out) / 'job.log').read_text())  # this host's own copy: as it was
        self.assertEqual(self.block(out)['node'], 'fake01')

    def test_without_its_ssh_config_a_part_runs_nowhere(self):
        self.private_site(no_config=True)
        code, out = self.execute()
        self.assertEqual(out['outcome'], 'unavailable')
        self.assertIn('ssh -G <account>: no host and user to redact', out['reason'])
        self.assertEqual(self.jobs(), [])

    def test_a_red_part(self):
        code, out = self.execute(command='exit 3')
        self.assertEqual((code, out['outcome'], out['reason'], out['next']), (1, 'red', 'exit 3', 'none'))

    def test_the_inner_timeout_is_red(self):
        code, out = self.execute(command='sleep 60', timeout_minutes=0.03)
        self.assertEqual((out['outcome'], out['reason']), ('red', 'the inner timeout ended the command'))

    def test_the_memory_cap_is_oom_and_retries_with_resume(self):
        code, out = self.execute(command='python3 -c "import time; b = b\'x\' * (160 << 20); time.sleep(30)" & wait',
                                 memory='64M')
        self.assertEqual((out['outcome'], out['next'], out['resume']), ('oom', 'retry', True))
        self.assertEqual(out['retry-on'], ['self-hosted', 'hb-slurm'])

    def test_the_jobs_time_limit_is_red(self):
        self.plan({'end': 'TIMEOUT'})
        code, out = self.execute()
        self.assertEqual((code, out['outcome'], out['next']), (1, 'red', 'none'))
        self.assertIn('time limit', out['reason'])

    def test_a_node_failure_retries_on_this_pool(self):
        self.plan({'end': 'NODE_FAIL'})
        code, out = self.execute()
        self.assertEqual((code, out['outcome'], out['next']), (0, 'unavailable', 'retry'))
        self.assertEqual(out['retry-on'], ['self-hosted', 'hb-slurm'])
        self.assertEqual(out['reason'], 'node fail: NODE_FAIL on <node>')  # the node the job ran on, redacted

    def test_a_cancel_by_another_user_falls_back(self):
        self.plan({'other_cancel': 0.5})
        code, out = self.execute(command='sleep 30')
        self.assertEqual((out['outcome'], out['next'], out['retry-on']),
                         ('unavailable', 'fallback', ['self-hosted', '3090a-slurm']))
        self.assertIn('cancelled by another user', out['reason'])

    def test_a_kill_by_the_node_falls_back(self):
        self.plan({'end': 'OUT_OF_MEMORY'})
        code, out = self.execute()
        self.assertEqual((out['outcome'], out['next']), ('unavailable', 'fallback'))

    def test_a_failed_p2p_self_test_holds_the_target_at_one_card(self):
        code, out = self.execute(target='a40', gpus=2, command='echo never')
        self.assertEqual((out['outcome'], out['next']), ('unavailable', 'none'))
        self.assertTrue(out['reason'].startswith('p2p: '), out['reason'])
        held = json.loads((self.data / 'ci-runners' / 'state' / 'slurm' / 'cluster1' / 'targets.json').read_text())
        self.assertEqual(held['a40']['max_cards'], 1)
        code, out = self.execute(id='again', target='a40', gpus=2)
        self.assertEqual(out['outcome'], 'unavailable')
        self.assertIn('it takes 1 to 1', out['reason'])
        self.assertEqual(len(self.jobs()), 1)  # refused before a job

    def test_refusals_before_a_job(self):
        for kw, why in ((dict(target='h100', gpus=1, timeout_minutes=61), 'above its 60'),
                        (dict(target='l40'), 'no target'), (dict(target='cpu', lock='s'), 'lock s'),
                        (dict(target='other:cpu'), 'no site other')):
            code, out = self.execute(id=f'r{len(why)}', **kw)
            self.assertEqual(out['outcome'], 'unavailable', kw)
            self.assertIn(why, out['reason'])
        with self.assertRaisesRegex(part.Refused, 'names its target'):
            self.execute(id='none', target='')
        self.assertEqual(self.jobs(), [])
        code, out = self.execute(id='nosite', target='l40')
        self.assertIn('l40: no target of site <site>', out['reason'])  # the executor's own reason, redacted
        site = self.etc / 'slurm' / 'cluster1.toml'
        site.chmod(0o644)
        code, out = self.execute(id='badsite', target='cpu')
        self.assertEqual(out['reason'], 'site config: invalid (python -m ci_runners.slurmsite check)')
        self.assertIn('readable by others', (self.record(out) / 'site-error.txt').read_text())  # the details: here


class Pending(Driver):
    def test_a_pending_job_is_cancelled_at_its_bound_and_the_next_account_then_target_tried(self):
        self.plan({'pend': -1, 'reason': 'AssocGrpNodeLimit'}, {'pend': -1, 'reason': 'Resources'}, {})
        code, out = self.execute(target='a40,h100', gpus=1, command='echo ok')
        self.assertEqual(out['outcome'], 'pass', self.log.read_text())
        jobs = self.jobs()
        self.assertEqual([(j['state'], j['sb']['--account'], j['sb']['--partition']) for j in jobs],
                         [('CANCELLED', 'lab-acct1', 'gpu-partition-a'), ('CANCELLED', 'lab-acct2', 'gpu-partition-a'),
                          ('COMPLETED', 'lab-acct1', 'gpu-partition-b')])
        b = self.block(out)
        self.assertEqual([(t['account'], t['target']) for t in b['tried']],
                         [('acct1', 'a40'), ('acct2', 'a40'), ('acct1', 'h100')])
        self.assertIn('AssocGrpNodeLimit', b['tried'][0]['why'])
        self.assertEqual((b['target'], b['job']), ('h100', 1003))

    def test_past_the_queue_timeout_the_part_is_unavailable_pending(self):
        self.plan({'pend': -1, 'reason': 'Priority'}, {'pend': -1, 'reason': 'Priority'})
        t0 = time.monotonic()
        code, out = self.execute(target='cpu', queue_timeout_minutes=0.03)
        self.assertLess(time.monotonic() - t0, 20)
        self.assertEqual((out['outcome'], out['next']), ('unavailable', 'fallback'))
        self.assertTrue(out['reason'].startswith('pending: '), out['reason'])
        self.assertIn('Priority', out['reason'])
        self.assertTrue(all(j['state'] == 'CANCELLED' for j in self.jobs()))

    def test_a_pending_reason_that_names_nodes_outside_the_config_is_redacted(self):
        (self.d / 'slurm' / 'nodes.json').write_text(json.dumps(['fake[01-02]', 'zq[17-20]']))
        self.plan({'pend': -1, 'reason': 'ReqNodeNotAvail, UnavailableNodes:zq[19-20]'},
                  {'pend': -1, 'reason': 'ReqNodeNotAvail, UnavailableNodes:zq19'})
        code, out = self.execute(target='cpu', queue_timeout_minutes=0.03)
        self.assertEqual(out['outcome'], 'unavailable')
        self.assertIn('UnavailableNodes:<node>', out['reason'])
        log = self.log.read_text()
        self.assertIn('UnavailableNodes:<node>', log)
        self.assertNotIn('zq', log + out['reason'])
        cached = json.loads((self.data / 'ci-runners' / 'state' / 'slurm' / 'cluster1' / 'nodes.json').read_text())
        self.assertEqual(cached['nodes'], ['fake[01-02]', 'zq[17-20]'])

    def test_without_the_clusters_node_list_no_part_runs(self):
        self.hosts({'acct1': True, 'acct2': True})
        code, out = self.execute(target='cpu')
        self.assertEqual(out['outcome'], 'unavailable')
        self.assertIn("the cluster's node list is unknown", out['reason'])
        self.hosts({})
        self.assertEqual(self.execute(id='up', target='cpu')[1]['outcome'], 'pass')  # cached now
        (self.d / 'slurm' / 'nodes.json').write_text(json.dumps(['other[1-2]']))
        self.hosts({'acct1': True, 'acct2': True})
        code, out = self.execute(id='down', target='cpu')
        self.assertNotIn('node list', out['reason'])  # the cached lists, however old: the budget decides
        cache = self.data / 'ci-runners' / 'state' / 'slurm' / 'cluster1' / 'nodes.json'
        c = json.loads(cache.read_text())
        c['at'] -= sp.NODES_TTL
        cache.write_text(json.dumps(c))
        self.hosts({})
        self.execute(id='again', target='cpu')
        self.assertEqual(json.loads(cache.read_text())['nodes'], ['other[1-2]'])  # read again past its TTL

    def test_past_the_queue_timeout_no_next_account_is_tried(self):
        self.plan({'pend': -1, 'reason': 'Priority'}, {'pend': -1, 'reason': 'Priority'})
        code, out = self.execute(target='cpu', queue_timeout_minutes=0.005)  # below the target's pending bound
        self.assertEqual(out['outcome'], 'unavailable')
        self.assertEqual(len(self.jobs()), 1)

    def test_tied_accounts_take_turns(self):
        site = self.etc / 'slurm' / 'cluster1.toml'
        site.write_text(site.read_text().replace('cpu_core_hour = 0.06', 'cpu_core_hour = 0.0'))  # spend ties at 0
        accounts = []
        for k in range(3):
            code, out = self.execute(id=f't{k}', target='cpu')
            accounts.append(self.block(out)['account'])
        self.assertEqual(accounts, ['acct1', 'acct2', 'acct1'])

    def test_a_retired_accounts_spend_still_counts(self):
        site = self.etc / 'slurm' / 'cluster1.toml'
        site.write_text(site.read_text() + 'retired = true\n')  # acct2, the last table
        cache = self.data / 'ci-runners' / 'state' / 'slurm' / 'cluster1'
        cache.mkdir(parents=True)
        (cache / 'ledger-acct2.json').write_text(json.dumps({'month': time.strftime('%Y-%m'), 'at': 0, 'jobs': [
            ledger_row(Start=time.strftime('%Y-%m-%dT00:00:00'))]}))  # 32 today, over the site's daily 30
        code, out = self.execute(target='cpu')
        self.assertEqual(out['outcome'], 'unavailable')
        self.assertIn("budget: the site's daily cap 30", out['reason'])
        self.assertEqual(self.jobs(), [])

    def test_a_budget_refusal_is_unavailable_budget(self):
        # each account's sacct shows the row: 2 x 14.90 of the daily 30; an a40 card for 6 min is 0.45 more
        (self.d / 'slurm' / 'ledger.json').write_text(json.dumps([ledger_row(
            Start=time.strftime('%Y-%m-%dT00:00:00'), ElapsedRaw='1676')]))
        code, out = self.execute(target='a40', gpus=1)
        self.assertEqual((out['outcome'], out['next']), ('unavailable', 'fallback'))
        self.assertTrue(out['reason'].startswith('budget: '), out['reason'])
        self.assertNotIn(';', out['reason'])  # the first refusal, not every account's
        self.assertEqual(self.jobs(), [])
        code, out = self.execute(id='cheap', target='cpu')  # 8 cores for 6 min (0.05) still fit
        self.assertEqual(out['outcome'], 'pass')

    def test_a_hosts_cluster_parts_are_bounded(self):
        import fcntl
        d = self.data / 'ci-runners' / 'state' / 'slurm' / 'cluster1' / 'parts'
        d.mkdir(parents=True)
        held = []
        for k in range(4):  # the site's max_parts, all held
            fd = os.open(d / f'{k}.lock', os.O_WRONLY | os.O_CREAT, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            held.append(fd)
        try:
            code, out = self.execute(queue_timeout_minutes=0.02)
        finally:
            for fd in held:
                os.close(fd)
        self.assertEqual((out['outcome'], out['reason']), ('unavailable', 'queue timeout: 4 cluster parts run from this host'))
        self.assertEqual(self.jobs(), [])

    def test_an_unreachable_account_is_skipped(self):
        code, out = self.execute(id='first', target='cpu')  # both answer: their ledgers are cached
        self.assertEqual(self.block(out)['account'], 'acct1')
        self.hosts({'acct1': True})
        code, out = self.execute(target='cpu')
        self.assertEqual(out['outcome'], 'pass')
        self.assertEqual(self.block(out)['account'], 'acct2')
        self.hosts({'acct1': True, 'acct2': True})
        code, out = self.execute(id='nowhere', target='cpu')
        self.assertEqual((out['outcome'], out['next']), ('unavailable', 'fallback'))
        self.assertIn('did not answer', out['reason'])


    def test_an_account_of_unknown_spend_stops_every_part(self):
        self.hosts({'acct1': True})  # silent, and no ledger of it cached: the site's caps cannot be checked
        code, out = self.execute(target='cpu')
        self.assertEqual((out['outcome'], out['next']), ('unavailable', 'fallback'))
        self.assertTrue(out['reason'].startswith('budget: the spend of <account> is unknown'), out['reason'])
        self.assertEqual(self.jobs(), [])


class Stop(Driver):
    """The executor runs in this (the main) thread; a watcher acts once the job runs."""

    def when_running(self, act) -> threading.Thread:
        def watch():
            end = time.monotonic() + 30
            while not any(j['state'] == 'RUNNING' for j in self.jobs()) and time.monotonic() < end:
                time.sleep(0.05)
            time.sleep(0.5)  # the cluster supervisor is up
            act()
        t = threading.Thread(target=watch, daemon=True)
        t.start()
        return t

    def test_term_cancels_the_job_and_ends_stopped(self):
        ex = self.executor()
        t = self.when_running(lambda: os.kill(ex.proc.pid, signal.SIGTERM))  # the scope's stop reaches the driver
        code, out = self.execute(ex, command='sleep 60')
        t.join(10)
        self.assertEqual((code, out['outcome'], out['next']), (0, 'stopped', 'none'))
        self.assertEqual(self.jobs()[0]['state'], 'CANCELLED')
        self.assertEqual(self.jobs()[0]['cancelled_by'], os.getuid())
        b = self.block(out)
        self.assertEqual((b['state'], b['open']), ('CANCELLED', {}))  # its end seen, not only the scancel sent

    def test_a_cancel_the_login_host_does_not_take_is_left_to_the_reaper(self):
        ex = self.executor()

        def act():
            self.hosts({'acct1': True})
            os.kill(ex.proc.pid, signal.SIGTERM)
        t = self.when_running(act)
        with mock.patch.object(sp, 'CANCEL_WAIT', 1):
            code, out = self.execute(ex, command='sleep 60')
        t.join(10)
        self.assertEqual(out['outcome'], 'stopped')
        rec = self.record(out)
        self.assertTrue((rec / 'exit.json').exists())
        self.assertEqual(json.loads((rec / 'slurm.json').read_text())['open'], {'acct1': 1001})
        self.assertEqual(self.jobs()[0]['state'], 'RUNNING')
        self.hosts({})
        ssh = os.path.join(self.bin, 'ssh')
        end = time.monotonic() + 20
        while json.loads((rec / 'slurm.json').read_text())['open']:
            self.assertLess(time.monotonic(), end)
            lines = sp.reap(str(self.data), str(self.etc), ssh=ssh)
            time.sleep(0.2)
        self.assertTrue(any('job 1001 (acct1) ended (CANCELLED), its driver gone' in x for x in lines), lines)
        self.assertEqual(self.jobs()[0]['state'], 'CANCELLED')
        self.assertFalse(any('job 1001' in x for x in sp.reap(str(self.data), str(self.etc), ssh=ssh)))

    def test_a_lost_driver_leaves_the_job_to_the_reaper(self):
        t = self.when_running(lambda: self.hosts({'acct1': True}))  # the login host goes away mid-part
        with mock.patch.object(sp, 'LOST_AFTER', 1):
            code, out = self.execute(command='sleep 60')
        t.join(10)
        self.assertEqual((out['outcome'], out['next']), ('lost', 'retry'))
        rec = self.record(out)
        self.assertFalse((rec / 'exit.json').exists())
        b = json.loads((rec / 'slurm.json').read_text())
        self.assertEqual((b['job'], b['account']), (1001, 'acct1'))
        self.assertIn('did not answer', b['lost'])
        self.assertEqual(self.jobs()[0]['state'], 'RUNNING')
        (rec / 'outcome.json').unlink()  # as if the runner had stopped first: the verdict's salvage reads it
        r = json.loads((rec / 'record.json').read_text())
        r.pop('slurm')  # and the executor before its end: salvage reads the block from slurm.json
        (rec / 'record.json').write_text(json.dumps(r))
        _, done = part.salvage(self.inv, out['record'], self.env)
        self.assertEqual(done['outcome'], 'lost')
        self.assertIn('hb → cpu, job 1001', done['slurm'])
        self.hosts({})
        nested = rec / 'work' / 'x'  # a block below a record (results the job wrote): not the reaper's
        nested.mkdir(parents=True)
        for f in ('slurm.json', 'slurm-plan.json'):
            (nested / f).write_text((rec / f).read_text())
        ssh = os.path.join(self.bin, 'ssh')
        lines = sp.reap(str(self.data), str(self.etc), ssh=ssh)
        self.assertIn('slurm reap: org/repo/77-1/unit-1: job 1001 (acct1) cancelled, its driver gone; its end not seen yet',
                      lines)
        self.assertEqual(sum('job 1001' in x for x in lines), 1, lines)
        end = time.monotonic() + 15
        while self.jobs()[0]['state'] != 'CANCELLED':
            self.assertLess(time.monotonic(), end)
            time.sleep(0.05)
        lines = sp.reap(str(self.data), str(self.etc), ssh=ssh)
        self.assertIn('slurm reap: org/repo/77-1/unit-1: job 1001 (acct1) ended (CANCELLED), its driver gone', lines)
        self.assertEqual(json.loads((rec / 'slurm.json').read_text())['open'], {})
        lines = sp.reap(str(self.data), str(self.etc), ssh=ssh)
        self.assertFalse(any('job 1001' in x for x in lines), lines)  # once

    def test_the_reaper_leaves_a_live_drivers_job(self):
        ex = self.executor()
        seen: list = []

        def act():
            seen.append(sp.reap(str(self.data), str(self.etc), ssh=os.path.join(self.bin, 'ssh')))
            seen.append(self.jobs()[0]['state'])
            os.kill(ex.proc.pid, signal.SIGTERM)
        t = self.when_running(act)
        self.execute(ex, command='sleep 60')
        t.join(10)
        lines, state = seen
        self.assertFalse(any('job 1001' in x for x in lines), lines)
        self.assertTrue(any('cluster1:acct1: 0 cancelled past their deadline' in x for x in lines), lines)
        self.assertEqual(state, 'RUNNING')


class Sites(unittest.TestCase):
    def test_a_target_names_its_site_where_there_are_several(self):
        import tempfile
        with tempfile.TemporaryDirectory() as etc:
            os.makedirs(os.path.join(etc, 'slurm'))
            self.assertEqual(sp.site_path(etc, 'cpu'), ('', '0 site configs here: name the target <site>:cpu'))
            open(os.path.join(etc, 'slurm', 'c1.toml'), 'w').close()
            self.assertEqual(sp.site_path(etc, 'cpu'), (os.path.join(etc, 'slurm', 'c1.toml'), 'cpu'))
            self.assertEqual(sp.site_path(etc, 'c1:cpu'), (os.path.join(etc, 'slurm', 'c1.toml'), 'cpu'))
            open(os.path.join(etc, 'slurm', 'c2.toml'), 'w').close()
            self.assertEqual(sp.site_path(etc, 'cpu')[0], '')
            self.assertEqual(sp.site_path(etc, 'c2:cpu'), (os.path.join(etc, 'slurm', 'c2.toml'), 'cpu'))
            self.assertEqual(sp.site_path(etc, 'c3:cpu'), ('', 'no site c3'))


if __name__ == '__main__':
    unittest.main()
