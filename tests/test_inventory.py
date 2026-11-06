"""The inventory schema, its loader and the lint (design §3.3, §9). Strings the lint
must catch are assembled at run time, so this file passes the lint itself."""
from __future__ import annotations

import contextlib
import io
import pathlib
import tempfile
import re
import tomllib
import unittest

from ci_runners import inventory as inv

ROOT = pathlib.Path(__file__).resolve().parents[1]
EXAMPLE = (ROOT / 'setup' / 'inventory.example.toml').read_text()
H = '/ho' + 'me/'  # a home-directory prefix


class Load(unittest.TestCase):
    def test_the_example_loads(self):
        i = inv.load(EXAMPLE)
        self.assertEqual(i.this, 'h100')
        self.assertEqual(sorted(i.hosts), ['h100'])
        self.assertEqual(i.host['pools']['h100'], {'per_card': True, 'labels': ['h100']})
        self.assertNotIn('host_lock', i.host['files'])

    def test_the_retired_keys_are_accepted_and_ignored(self):
        """Decision 11.12: h100's inventory may still hold the first path's keys."""
        old = EXAMPLE.replace('slots = 2', 'ssh = "<alias>"\ntrusted = true\nremote = false\ncpu_slowdown = 2.7\n'
                              'slots = 2', 1).replace('slot_lock = ', 'host_lock = "/tmp/h.lock"\nslot_lock = ', 1)
        old += '[hosts.h100.proxy]\nclient = "/x"\n'
        i = inv.load(old)
        self.assertEqual(i.host['cpu_slowdown'], 2.7)
        self.assertEqual(inv.RETIRED_KEYS, {'ssh', 'trusted', 'remote', 'cpu_slowdown', 'proxy'})
        with self.assertRaises(inv.InventoryError):  # only those: another key is still unknown
            inv.load(EXAMPLE.replace('slots = 2', 'slowdown = 2.7\nslots = 2', 1))
        with self.assertRaises(inv.InventoryError):
            inv.load(EXAMPLE.replace('slot_lock = ', 'other_lock = "/tmp/o.lock"\nslot_lock = ', 1))

    def test_pools_as_tables(self):
        """RUN-5: a pool may give its labels, or have one runner per card."""
        base = re.sub(r'\[hosts\.h100\.pools\].*\n(?:.*\n){3}', '', EXAMPLE).replace(
            'max_temp_c = 80\n', 'max_temp_c = 80\npools = { @ }\n')
        self.assertIn('@', base)
        good = ('light = 2', 'light = { count = 2 }', 'light = { count = 1, labels = ["h100-light"] }',
                'h100 = { per_card = true }', 'h100 = { per_card = true, labels = ["h100", "x-1"] }')
        for g in good:
            inv.load(base.replace('@', g))
        bad = ('light = 0', 'light = {}', 'light = { labels = ["a"] }', 'light = { count = 0 }',
               'light = { count = 1, per_card = true }', 'light = { count = 1, labels = [] }',
               'light = { count = 1, labels = ["A"] }', 'light = { count = 1, labels = ["a b"] }',
               'light = { count = 1, labels = ["a", "a"] }', 'light = { count = 1, labels = "a" }',
               'light = { count = 1, extra = 1 }', 'h100 = { per_card = 1 }', 'light = { count = true }',
               'light = { count = 1, labels = ["light"] }', 'light = { count = 1, labels = ["self-hosted"] }',
               'h100 = { per_card = true, labels = ["gpu"] }')  # a bare class or GitHub's own label
        for b in bad:
            with self.assertRaises(inv.InventoryError, msg=b):
                inv.load(base.replace('@', b))
        with self.assertRaisesRegex(inv.InventoryError, 'without gpus'):
            inv.load(base.replace('@', 'h100 = { per_card = true }').replace('gpus = [0, 1, 2, 3, 4]', 'gpus = []', 1))
        with self.assertRaises(inv.InventoryError):
            inv.load(base.replace('{ @ }', '{}'))

    def test_python_and_orgsync(self):
        for bad in ('python = "python3"', 'python = ""', 'python = 3', 'orgsync = "yes"'):
            key = bad.split(' ')[0]
            text = re.sub(rf'^(# )?{key} = .*$', bad, EXAMPLE, count=1, flags=re.M)
            with self.assertRaises(inv.InventoryError, msg=bad):
                inv.load(text)

    def test_an_unknown_schema_is_refused(self):
        for s in ('schema = 2', 'schema = "1"', ''):
            with self.assertRaises(inv.InventoryError, msg=s):
                inv.load(EXAMPLE.replace('schema = 1', s))

    def test_malformed_inventories(self):
        bad = {
            'this unknown': EXAMPLE.replace('this = "h100"', 'this = "h200"'),
            'unknown top key': 'extra = 1\n' + EXAMPLE,
            'unknown host key': EXAMPLE.replace('max_temp_c = 80\n', 'max_temp_c = 80\nfoo = 1\n'),
            'missing account': EXAMPLE.replace('account = "<shared account>"\n', ''),
            'relative home': EXAMPLE.replace('home = "/home/<shared account>"', 'home = "~"'),
            'slots zero': EXAMPLE.replace('slots = 2', 'slots = 0'),
            'slots bool': EXAMPLE.replace('slots = 2', 'slots = true'),
            'memory': EXAMPLE.replace('memory_max = "512G"', 'memory_max = "512GB"'),
            'gpus': EXAMPLE.replace('gpus = [0, 1, 2, 3, 4]\n', 'gpus = [0, "1"]\n'),
            'cpus': EXAMPLE.replace('cpus = "0-43,88-131"', 'cpus = "0-43;rm"'),
            'pool count': EXAMPLE.replace('light = { count = 2,', 'light = { count = 0,'),
            'files keys': re.sub(r'slot_queue = .*\n', '', EXAMPLE),
            'role': EXAMPLE.replace('[hosts.h100]', '[hosts.H100]'),
            'not toml': '[[',
        }
        for what, text in bad.items():
            with self.assertRaises(inv.InventoryError, msg=what):
                inv.load(text)


class Deny(unittest.TestCase):
    DOC = {'schema': 1, 'this': 'role-a', 'hosts': {'role-a': {
        'ssh': 'secret-alias', 'account': 'acct1', 'home': H + 'acct1', 'memory_max': '120G',
        'files': {'host_lock': '/tmp/public.lock', 'slot_lock': '/tmp/p.lock', 'slot_queue': '/tmp/q'},
        'proxy': {'nodes': ['node.example-a', 'nd'], 'no_proxy': 'localhost, .corp-intra ,127.0.0.1'},
        'slots': 2}}}
    POLICY = {'actors': ['MemberOne'], 'bot': 'github-actions[bot]',
              'repositories': {'chaobrain/x': {'id': 5, 'visibility': 'private'}}}

    def test_deny_tokens(self):
        got = inv.deny_tokens(self.DOC, self.POLICY)
        self.assertEqual(got, {'secret-alias', 'acct1', 'home/acct1', 'node.example-a', 'corp-intra', '127.0.0.1',
                               'memberone'})
        # public keys, table keys, numbers, short and common words are not tokens
        for t in ('role-a', '120g', '/tmp/public.lock', 'chaobrain/x', 'github-actions[bot]', 'nd', 'localhost'):
            self.assertNotIn(inv.normalise(t), got)

    def test_labels_are_public_and_the_python_is_not(self):
        doc = {'hosts': {'h': {'python': '/opt/private-env/bin/python3',
                               'pools': {'light': {'count': 2, 'labels': ['h100-light']}}}}}
        self.assertEqual(inv.deny_tokens(doc), {'opt/private-env/bin/python3'})

    def test_tokens_split_as_the_lint_reads_words(self):
        doc = {'nodes': ['hk+node01', 'node=sg02', 'Tokyo#3x', '\u9999\u6e2f01']}
        got = inv.deny_tokens(doc)
        self.assertEqual(got, {'node01', 'node', 'sg02', 'tokyo', '\u9999\u6e2f01'})
        for line in ('via hk+node01', 'node=sg02', '\u9999\u6e2f01 up'):
            self.assertIn('denied', [f.kind for f in inv.scan(line, {inv.hash_token(t) for t in got})], line)
        long = '/'.join(f'p{i}' for i in range(20))
        (tok,) = inv.deny_tokens({'path': long})
        self.assertEqual(tok, '/'.join(f'p{i}' for i in range(inv.MAX_PARTS)))
        self.assertEqual([f.kind for f in inv.scan(f'x {long} y', {inv.hash_token(tok)})], ['denied'])

    def test_hash_is_salted_and_normalised(self):
        import hashlib
        self.assertNotEqual(inv.hash_token('acct1'), hashlib.sha256(b'acct1').hexdigest())
        self.assertEqual(inv.hash_token('/ACCT1/'), inv.hash_token('acct1'))

    def test_read_deny(self):
        h = inv.hash_token('x')
        self.assertEqual(inv.read_deny(f'# comment\n{h}  # x\n\n'), {h})
        with self.assertRaises(ValueError):
            inv.read_deny('not-a-hash\n')


class Scan(unittest.TestCase):
    DENY = frozenset(inv.hash_token(t) for t in ('secret-alias', 'acct1', 'node.example-a', 'home/acct1'))

    def kinds(self, text):
        return [f.kind for f in inv.scan(text, self.DENY)]

    def test_denied_tokens_anywhere_in_a_word(self):
        for text in ('ssh secret-alias true', 'user@SECRET-ALIAS:22', 'scp acct1@h:/x', 'a ' + H + 'acct1/data b',
                     'proxy node.example-a:443', '~acct1', 'x_acct1_y'):
            self.assertIn('denied', self.kinds(text), text)
        for text in ('secret-aliases', 'acct10', 'xacct1', 'node.example', 'example-a', 'secret alias'):
            self.assertNotIn('denied', self.kinds(text), text)

    def test_patterns(self):
        ip = '.'.join(['10', '6', '8', '1'])
        self.assertEqual(self.kinds(f'host {ip} up'), ['ipv4'])
        self.assertEqual(self.kinds(f'user@{ip}:22'), ['ipv4'])
        self.assertEqual(self.kinds(f'host at {ip}.'), ['ipv4'])  # before a full stop
        for text in ('127.0.0.1', '0.0.0.0', '192.0.2.7', '198.51.100.1', '203.0.113.9', '1.2.3', 'v1.2.3.4',
                     '256.1.1.1', '1.2.3.4.5', 'python3.12.3.1'):
            self.assertEqual(self.kinds(text), [], text)
        self.assertEqual(self.kinds(H + 'someone/x'), ['home-path'])
        self.assertEqual(self.kinds('/Use' + 'rs/someone'), ['home-path'])
        for name in ('runner-x', 'runner.x', 'runners', '\u00e9t\u00e9'):
            self.assertEqual(self.kinds(H + name), ['home-path'], name)
        for text in (H + 'runner/work', H + '<user>/data', '~/data/actions-runner', '/home', H):
            self.assertEqual(self.kinds(text), [], text)
        self.assertEqual(self.kinds('t=' + 'gh' + 'p_' + 'A' * 36), ['github-token'])
        self.assertEqual(self.kinds('github' + '_pat_' + 'B' * 30), ['github-token'])
        self.assertEqual(self.kinds('-----BEGIN ' + 'OPENSSH PRIVATE KEY-----'), ['private-key'])
        self.assertEqual(self.kinds('-----BEGIN PUBLIC KEY-----'), [])

    def test_one_finding_per_line_and_kind_with_its_line_number(self):
        got = inv.scan(f'ok\nacct1 acct1 {H}a {H}b\n', self.DENY, 'f')
        self.assertEqual([(f.line, f.kind) for f in got], [(2, 'denied'), (2, 'home-path')])
        self.assertEqual(str(got[0]), 'f:2: denied')  # never the text

    def test_lint_scans_contents_and_names_and_skips_binaries(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d)
            (p / 'clean.txt').write_text('nothing here\n')
            (p / 'leak.md').write_text('line\nssh secret-alias\n')
            (p / 'acct1.txt').write_text('\n')
            (p / 'blob.bin').write_bytes(b'\0secret-alias')
            got = sorted(str(f) for f in inv.lint(d, self.DENY))
        self.assertEqual(got, ['<the name of tracked file 1>:0: denied', 'leak.md:2: denied'])  # never the name

    def test_cli(self):
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            p = pathlib.Path(d)
            (p / 'inv.toml').write_text(EXAMPLE)
            self.assertEqual(inv.main(['check', str(p / 'inv.toml')]), 0)
            (p / 'leak.md').write_text('secret-alias\n')
            (p / 'deny').write_text('\n'.join(sorted(self.DENY)) + '\n')
            self.assertEqual(inv.main(['lint', d, '--deny', str(p / 'deny')]), 1)
            (p / 'leak.md').write_text('clean\n')
            self.assertEqual(inv.main(['lint', d, '--deny', str(p / 'deny')]), 0)
            self.assertEqual(inv.main([]), 2)
            self.assertEqual(inv.main(['bogus']), 2)

    def test_the_example_is_all_placeholders(self):
        doc = tomllib.loads(EXAMPLE)
        values = list(inv._strings(doc))
        self.assertGreater(len(values), 5)
        for s in values:
            self.assertIn('<', s)  # every private value is a placeholder


if __name__ == '__main__':
    unittest.main()
