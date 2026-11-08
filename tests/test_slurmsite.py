"""The Slurm site config (design decision 14.6): its schema, the refusals of RUN-9b's
acceptance, the prices, the account's cluster-side copy and the deny tokens."""
from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import tempfile
import tomllib
import unittest

from ci_runners import inventory
from ci_runners import slurmsite as ss

ROOT = pathlib.Path(__file__).resolve().parents[1]
EXAMPLE = (ROOT / 'setup' / 'slurm' / 'site.example.toml').read_text()


def edit(old: str, new: str, text: str = EXAMPLE) -> str:
    assert text.count(old) == 1, old
    return text.replace(old, new)


class Load(unittest.TestCase):
    def test_the_example_loads(self):
        s = ss.load(EXAMPLE)
        self.assertEqual(s.name, 'cluster1')
        self.assertEqual(sorted(s.targets), ['a40', 'cpu', 'h100'])
        self.assertEqual(sorted(s.accounts), ['acct1'])
        self.assertTrue(s.targets['a40'].gpu)
        self.assertFalse(s.targets['cpu'].gpu)
        self.assertEqual(s.accounts['acct1'].weight, 1.0)
        self.assertFalse(s.accounts['acct1'].retired)

    def test_an_unknown_key_is_refused_at_every_level(self):
        for old, new in (('schema = 1\n', 'schema = 1\nextra = 1\n'),
                         ('max_parts = 4', 'max_parts = 4\nqos = "x"'),
                         ('cpu_on_gpu_jobs = false', 'cpu_on_gpu_jobs = false\nminimum = 1'),
                         ('cpus = 8                          # a CPU', 'gres = "x"\ncpus = 8  # a CPU'),
                         ('monthly_cap = 200.0', 'monthly_cap = 200.0\npassword = "x"')):
            with self.subTest(new=new), self.assertRaisesRegex(ss.SiteError, 'unknown keys'):
                ss.load(edit(old, new))

    def test_a_target_without_a_partition_is_refused(self):
        with self.assertRaisesRegex(ss.SiteError, r'targets\.cpu: partition is missing'):
            ss.load(edit('partition = "cpu-partition"\n', ''))

    def test_a_gpu_target_without_a_price_is_refused(self):
        with self.assertRaisesRegex(ss.SiteError, 'gpu_model a40 has no price'):
            ss.load(edit('a40 = 4.50, ', ''))
        with self.assertRaisesRegex(ss.SiteError, 'has no price'):
            ss.load(edit('gpu_model = "h100sxm"', 'gpu_model = "h100"'))

    def test_max_cards_above_cards_per_node_is_refused(self):
        with self.assertRaisesRegex(ss.SiteError, 'max_cards is above cards_per_node'):
            ss.load(edit('max_cards = 2                     # 1 where', 'max_cards = 5  # 1 where'))
        ss.load(edit('max_cards = 2                     # 1 where', 'max_cards = 4  # 1 where'))  # equal: fine

    def test_an_account_naming_an_unknown_target_is_refused(self):
        with self.assertRaisesRegex(ss.SiteError, r"accounts\.acct1: targets \['l40'\] are not targets"):
            ss.load(edit('targets = ["cpu", "a40", "h100"]', 'targets = ["cpu", "l40"]'))

    def test_gpu_and_cpu_target_keys(self):
        for k in ('cards_per_node = 4\n', 'cpus_per_card = 8\n'):
            with self.subTest(k=k), self.assertRaisesRegex(ss.SiteError, f'a GPU target needs {k.split()[0]}'):
                ss.load(edit(k, ''))
        with self.assertRaisesRegex(ss.SiteError, 'max_cards on a CPU target'):
            ss.load(edit('cpus = 8                          # a CPU', 'max_cards = 1\ncpus = 8  # a CPU'))
        with self.assertRaisesRegex(ss.SiteError, 'a CPU target needs cpus'):
            ss.load(edit('cpus = 8                          # a CPU part\'s CPUs\n', ''))
        with self.assertRaisesRegex(ss.SiteError, 'not cpus'):
            ss.load(edit('cpus_per_card = 8\n', 'cpus_per_card = 8\ncpus = 4\n'))
        for nodes in ('["node01", "node02"]', '["node[01-02]"]'):  # --nodelist would ask for every one
            with self.subTest(nodes=nodes), self.assertRaisesRegex(ss.SiteError, 'nodes is malformed'):
                ss.load(edit('nodes = ["node01"]', f'nodes = {nodes}'))
        with self.assertRaisesRegex(ss.SiteError, 'not both'):
            ss.load(edit('nodes = ["node01"]', 'nodes = ["node01"]\nexclude = ["node02"]'))

    def test_malformed_values(self):
        for old, new in (('gpu_request = "--gpus={n}"', 'gpu_request = "--gpus=2"'),
                         ('gpu_request = "--gpus={n}"', 'gpu_request = "--gpus={n} --x"'),
                         ('gpu_request = "--gpus={n}"', 'gpu_request = "--gpus={n}{n}"'),
                         ('submit_args = []', 'submit_args = ["--time=10"]'),
                         ('submit_args = []', 'submit_args = ["--qos=a b"]'),
                         ('submit_args = []', 'submit_args = ["--export=ALL"]'),
                         ('login_python = "/usr/bin/python3"', 'login_python = "python3"'),
                         ('egress = "tunnel"', 'egress = "open"'),
                         ('partition = "cpu-partition"', 'partition = "-p x"'),
                         ('root = "/shared/fs/acct1"', 'root = "/shared/../etc"'),
                         ('ssh = "cluster1-acct1"', 'ssh = "-oProxyCommand=x"'),
                         ('max_minutes = 60', 'max_minutes = 0'),
                         ('enabled = false', 'enabled = "yes"'),
                         ('targets = ["cpu", "a40", "h100"]', 'targets = ["cpu", "cpu"]'),
                         ('[targets.cpu]', '[targets.CPU]')):
            with self.subTest(new=new), self.assertRaises(ss.SiteError):
                ss.load(edit(old, new))
        ss.load(edit('submit_args = []', 'submit_args = ["--qos=normal", "--constraint=x"]'))

    def test_the_schema_and_the_proxy(self):
        with self.assertRaisesRegex(ss.SiteError, 'schema'):
            ss.load(edit('schema = 1', 'schema = 2'))
        with self.assertRaisesRegex(ss.SiteError, 'proxy'):
            ss.load(edit('egress = "tunnel"', 'egress = "proxy"'))
        ss.load(edit('egress = "tunnel"', 'egress = "proxy"\nproxy = "http://proxy.example:3128"'))
        with self.assertRaisesRegex(ss.SiteError, 'proxy'):
            ss.load(edit('egress = "tunnel"', 'egress = "push"\nproxy = "http://proxy.example:3128"'))

    def test_the_file_is_its_owners_only_and_named_after_the_site(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'cluster1.toml')
            with open(p, 'w') as f:
                f.write(EXAMPLE)
            os.chmod(p, 0o640)
            with self.assertRaisesRegex(ss.SiteError, 'readable by others'):
                ss.load_file(p)
            os.chmod(p, 0o600)
            self.assertEqual(ss.load_file(p).name, 'cluster1')
            q = os.path.join(d, 'other.toml')
            os.rename(p, q)
            with self.assertRaisesRegex(ss.SiteError, 'not its file name'):
                ss.load_file(q)
            os.makedirs(os.path.join(d, 'slurm'))
            os.rename(q, os.path.join(d, 'slurm', 'cluster1.toml'))
            open(os.path.join(d, 'slurm', 'notes.txt'), 'w').close()
            self.assertEqual(ss.site_files(d), {'cluster1': os.path.join(d, 'slurm', 'cluster1.toml')})
            self.assertEqual(ss.site_files(os.path.join(d, 'none')), {})


class Targets(unittest.TestCase):
    def setUp(self):
        self.s = ss.load(EXAMPLE)

    def test_enabled_for_a_project(self):
        t = self.s.targets
        self.assertTrue(t['cpu'].enabled_for('any'))
        self.assertTrue(t['a40'].enabled_for('project-a'))
        self.assertFalse(t['a40'].enabled_for('project-b'))
        self.assertFalse(t['h100'].enabled_for('project-a'))
        s = ss.load(edit('enabled = false\n', ''))
        self.assertFalse(s.targets['h100'].enabled_for('project-a'))  # absent: not enabled

    def test_cpus_and_the_pending_bound(self):
        self.assertEqual(self.s.targets['a40'].cpus(2), 16)
        self.assertEqual(self.s.targets['cpu'].cpus(0), 8)
        self.assertEqual(self.s.pending_minutes('cpu'), 3)
        s = ss.load(edit('pending_minutes = 3               # optional', 'pending_minutes = 7  # optional'))
        self.assertEqual(s.pending_minutes('a40'), 7)
        self.assertEqual(s.pending_minutes('h100'), 3)

    def test_prices_a_gpu_job_pays_its_cards_and_a_cpu_job_its_cores(self):
        s = self.s
        self.assertAlmostEqual(s.hourly('a40', 2, 16), 9.0)
        self.assertAlmostEqual(s.hourly('cpu', 0, 8), 0.48)
        both = ss.load(edit('cpu_on_gpu_jobs = false', 'cpu_on_gpu_jobs = true'))
        self.assertAlmostEqual(both.hourly('a40', 2, 16), 9.0 + 16 * 0.06)
        # the worst case: the job's whole --time, the timeout and the margin
        self.assertAlmostEqual(s.worst_cost('h100', 2, 55), 2 * 16.0 * 60 / 60)
        self.assertAlmostEqual(s.worst_cost('cpu', 0, 25), 8 * 0.06 * 30 / 60)
        self.assertEqual(ss.TIME_MARGIN, 5)


class Entry(unittest.TestCase):
    def test_the_cluster_side_copy_holds_the_account_and_its_targets_only(self):
        two = EXAMPLE + ('\n[accounts.acct2]\nssh = "cluster1-acct2"\nroot = "/shared/fs/acct2"\n'
                         'slurm_account = "lab-acct2"\ntargets = ["cpu"]\nmonthly_cap = 50.0\n')
        s = ss.load(two)
        e = ss.account_entry(s, 'acct2')
        self.assertEqual(e['slurm_account'], 'lab-acct2')
        self.assertEqual(e['root'], '/shared/fs/acct2')
        self.assertEqual(sorted(e['targets']), ['cpu'])
        self.assertNotIn('lab-acct1', json.dumps(e))
        self.assertNotIn('enabled', e['targets']['cpu'])  # the driver's to decide
        e1 = ss.account_entry(s, 'acct1')
        self.assertEqual(e1['targets']['a40']['nodes'], ['node01'])
        self.assertEqual(e1['gpu_request'], '--gpus={n}')
        self.assertEqual(e1['login_python'], '/usr/bin/python3')
        json.dumps(e1)


class Deny(unittest.TestCase):
    def test_the_sites_names_are_denied_and_the_public_values_not(self):
        doc = tomllib.loads(edit('submit_args = []', 'submit_args = ["--qos=lab-qos"]').replace(
            '"/usr/bin/python3"', '"/shared/fs/conda/bin/python3"'))
        toks = ss.deny_tokens(doc)
        for t in ('cpu-partition', 'gpu-partition-a', 'node01', 'cluster1-acct1', 'shared/fs/acct1', 'lab-acct1',
                  'lab-qos', 'shared/fs/conda/bin/python3'):
            self.assertIn(t, toks)
        for t in ('cluster1', 'gpus', 'tunnel', 'h100sxm', 'cuda13', '480g', 'project-a', 'acct1', 'h100', 'a40'):
            self.assertNotIn(t, toks)
        self.assertNotIn('usr/bin/python3', ss.deny_tokens(tomllib.loads(EXAMPLE)))  # a system interpreter

    def test_the_cli_prints_hashes_the_lint_finds(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'cluster1.toml')
            with open(p, 'w') as f:
                f.write(EXAMPLE)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(ss.main(['deny', p]), 0)
            deny = inventory.read_deny(out.getvalue())
            self.assertIn(inventory.hash_token('gpu-partition-b'), deny)
            self.assertEqual([f.kind for f in inventory.scan('#SBATCH --partition=gpu-partition-b', deny)], ['denied'])
            self.assertEqual(inventory.scan('#SBATCH --gpus=2', deny), [])
            os.chmod(p, 0o600)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(ss.main(['entry', p, 'acct1']), 0)
            self.assertEqual(json.loads(out.getvalue())['account'], 'acct1')
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(ss.main(['check', os.path.join(d, 'missing.toml')]), 1)
                self.assertEqual(ss.main(['entry', p, 'nobody']), 2)


class Redact(unittest.TestCase):
    """What a part shows on GitHub: the site's inventory as placeholders (RUN-9h)."""

    def site(self) -> ss.Site:
        text = edit('submit_args = []', 'submit_args = ["--qos=labqos"]')
        text = edit('login_python = "/usr/bin/python3"', 'login_python = "/opt/labpy/bin/python3"\n'
                    'proxy = "http://proxy.lab.example:3128"', text).replace('egress = "tunnel"', 'egress = "proxy"')
        text = edit('partition = "gpu-partition-b"', 'partition = "h100sxm"\nexclude = ["zq[08-10,3]"]', text)
        return ss.load(edit('partition = "cpu-partition"', 'partition = "cpu"', text))

    def test_each_kind_of_inventory_has_its_placeholder(self):
        r = ss.Redactor(self.site())
        r.add_ssh({'hostname': 'login7.lab.example', 'user': 'labuser'})
        cases = [
            ('/shared/fs/acct1/ci-runners/envs/p-cpu', '<root>/ci-runners/envs/p-cpu'),
            ('/shared/fs/other', '<home>/other'),
            ('cd /shared/fs/acct10', 'cd <root>0'),  # a long value: wherever it occurs
            ('x%2Fshared%2Ffs%2Facct1%2Fy', 'x<root>%2Fy'),  # URL-encoded
            ('home%2Flabuser %2flabuser \\nlabuser \\tlabuser', 'home%2F<user> %2f<user> \\n<user> \\t<user>'),
            ('\x1b[32mlabuser\x1b[0m xlab-acct1', '<user> x<account>'),  # no escape hides a value's start
            ('node01.hpc.lab.example login7.lab.example.', '<node> <host>.'),  # with its domain
            ('ReqNodeNotAvail, UnavailableNodes:node[01-02]', 'ReqNodeNotAvail, UnavailableNodes:<node>'),
            ('test[1-2] node[02-03]', 'test[1-2] node[02-03]'),  # names no known node
            ('-A lab-acct1 -p gpu-partition-a -w node01', '-A <account> -p <partition> -w <node>'),
            ('labuser@login7.lab.example via cluster1-acct1', '<user>@<host> via <host>'),
            ('LabUser on NODE01', '<user> on <node>'),  # in any case
            ('node011 xnode01 node01x', 'node011 xnode01 node01x'),
            ('zq08 zq09 zq10 zq3 zq[08-10,3] zq11', '<node> <node> <node> <node> <node> zq11'),
            ('/opt/labpy/bin/python3 http://proxy.lab.example:3128 --qos=labqos', '<python> <proxy> <submit-arg>'),
            ('qos labqos', 'qos <submit-arg>'),
            # our own names say nothing: the site, its targets, accounts, variants and GPU models
            ('cluster1:a40 acct1 cuda13 h100sxm cpu', 'cluster1:a40 acct1 cuda13 h100sxm cpu'),
        ]
        for text, want in cases:
            self.assertEqual(r(text), want, text)

    def test_proxyjump_hosts_and_users(self):
        r = ss.Redactor()
        r.add_ssh({'hostname': 'login9.lab.example', 'user': 'labuser', 'proxyjump': 'jumper@gate.lab.example:2222,ssh://hop2.example,[fd00::7]:22'})
        self.assertEqual(r('login9.lab.example gate.lab.example hop2.example fd00::7 jumper'), '<host> <host> <host> <host> <user>')
        r = ss.Redactor()
        r.add_ssh({'hostname': 'login7', 'user': 'labuser', 'proxyjump': 'none'})
        self.assertEqual(r('none'), 'none')

    def test_a_cut_never_splits_a_value(self):
        r = ss.Redactor(self.site())
        text = 'aaaa lab-acct1 bbbb'
        for cut in range(len(text) + 1):
            c = r.safe_cut(text, cut)
            self.assertEqual(r(text[:c]) + r(text[c:]), r(text), cut)
        self.assertEqual(r.safe_cut(text, 7), 5)
        self.assertEqual(r.safe_cut('x \x1b[32mz', 5), 2)  # nor an escape sequence
        self.assertEqual(r.safe_cut('x node[01-02] y', 9), 2)  # nor a host list

    def test_the_nodes_a_job_ran_on_are_added_as_seen(self):
        r = ss.Redactor(self.site())
        self.assertEqual(r('on c07 and c08'), 'on c07 and c08')
        r.add_nodes('c[07-08]')
        self.assertEqual(r('on c07 and c08, c[07-08]'), 'on <node> and <node>, <node>')
        for x in ('None assigned', '', 'a b'):
            r.add_nodes(x)
        self.assertEqual(r('None assigned'), 'None assigned')

    def test_a_short_or_common_value_is_kept_as_the_deny_list_keeps_it(self):
        r = ss.Redactor()
        r.add('gpu', '<partition>')
        r.add('home', '<home>')
        r.add('/data', '<root>')
        self.assertEqual(r('gpu home /data'), 'gpu home <root>')
        self.assertEqual(ss.Redactor()('anything'), 'anything')

    def test_a_host_list(self):
        self.assertEqual(ss.hostlist('zq[01-03,7],cpu1'), ['zq01', 'zq02', 'zq03', 'zq7', 'cpu1'])
        self.assertEqual(ss.hostlist('a[1-2]-ib'), ['a1-ib', 'a2-ib'])
        for bad in ('None assigned', '', 'a[x-2]', 'a[1-]', '(null)'):
            self.assertEqual(ss.hostlist(bad), [], bad)
        self.assertEqual(len(ss.hostlist('n[1-99999]')), ss.HOSTLIST_MAX)
        self.assertEqual(len(ss.hostlist('n[1-3000],m[1-3000]')), ss.HOSTLIST_MAX)


if __name__ == '__main__':
    unittest.main()
