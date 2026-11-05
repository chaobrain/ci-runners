"""The inventory schema, its loader and the lint (design §3.3, §9). Strings the lint
must catch are assembled at run time, so this file passes the lint itself."""
from __future__ import annotations

import contextlib
import io
import pathlib
import tempfile
import tomllib
import unittest

from ci_runners import inventory as inv

ROOT = pathlib.Path(__file__).resolve().parents[1]
EXAMPLE = (ROOT / 'setup' / 'inventory.example.toml').read_text()
H = '/ho' + 'me/'  # a home-directory prefix


class Load(unittest.TestCase):
    def test_the_example_loads(self):
        i = inv.load(EXAMPLE)
        self.assertEqual(i.this, '3090b')
        self.assertEqual(sorted(i.hosts), ['3090b', 'h100'])
        self.assertEqual(i.host['pools'], {'light': 2, 'part': 4, 'compile': 1})
        self.assertTrue(i.hosts['h100']['remote'])

    def test_an_unknown_schema_is_refused(self):
        for s in ('schema = 2', 'schema = "1"', ''):
            with self.assertRaises(inv.InventoryError, msg=s):
                inv.load(EXAMPLE.replace('schema = 1', s))

    def test_malformed_inventories(self):
        bad = {
            'this unknown': EXAMPLE.replace('this = "3090b"', 'this = "3090c"'),
            'unknown top key': 'extra = 1\n' + EXAMPLE,
            'unknown host key': EXAMPLE.replace('max_temp_c = 80\npools = { light', 'max_temp_c = 80\nfoo = 1\npools = { light'),
            'missing account': EXAMPLE.replace('account = "<unix account>"\n', ''),
            'relative home': EXAMPLE.replace('home = "/home/<unix account>"', 'home = "~"'),
            'slots zero': EXAMPLE.replace('slots = 2', 'slots = 0'),
            'slots bool': EXAMPLE.replace('slots = 2', 'slots = true'),
            'memory': EXAMPLE.replace('memory_max = "120G"', 'memory_max = "120GB"'),
            'gpus': EXAMPLE.replace('gpus = [0, 1]\n', 'gpus = [0, "1"]\n'),
            'cpus': EXAMPLE.replace('cpus = "0-43,88-131"', 'cpus = "0-43;rm"'),
            'pool count': EXAMPLE.replace('compile = 1 }', 'compile = 0 }'),
            'files keys': EXAMPLE.replace('slot_queue = "/tmp/<slot lock>.queue"\n\n', '\n'),
            'proxy port': EXAMPLE.replace('http_port = 10809', 'http_port = 70000'),
            'proxy no nodes': EXAMPLE.replace('nodes = ["<node 1>", "<node 2>"]', 'nodes = []'),
            'proxy unknown': EXAMPLE.replace('socks_port = 10808', 'socks_port = 10808\nnode = "x"'),
            'role': EXAMPLE.replace('[hosts.h100]', '[hosts.H100]'),
            'trusted str': EXAMPLE.replace('trusted = true', 'trusted = "yes"'),
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
        for text in ('127.0.0.1', '0.0.0.0', '192.0.2.7', '198.51.100.1', '203.0.113.9', '1.2.3', 'v1.2.3.4',
                     '256.1.1.1', '1.2.3.4.5', 'python3.12.3.1'):
            self.assertEqual(self.kinds(text), [], text)
        self.assertEqual(self.kinds(H + 'someone/x'), ['home-path'])
        self.assertEqual(self.kinds('/Use' + 'rs/someone'), ['home-path'])
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
        self.assertEqual(got, ['acct1.txt:0: denied', 'leak.md:2: denied'])

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
        self.assertGreater(len(values), 10)
        for s in values:
            self.assertIn('<', s)  # every private value is a placeholder


if __name__ == '__main__':
    unittest.main()
