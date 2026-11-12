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
#: the example without the keys of decision 12.9 and 12.7, its slot files under state/: a
#: host with no host lock, which the tests below give those keys one at a time
PLAIN = re.sub(r'^(compile_cpus|default_project|host_lock) = .*\n', '', EXAMPLE, flags=re.M).replace(
    'slot_lock = "/tmp/brainspike-local-ci.lock"', 'slot_lock = "/<data disk>/<ci account>/ci-runners/state/slot.lock"').replace(
    'slot_queue = "/tmp/brainspike-local-ci.queue"', 'slot_queue = "/<data disk>/<ci account>/ci-runners/state/slot.queue"')
_FILES = re.search(r'slot_lock = "[^"]*"\nslot_queue = "[^"]*"\n', PLAIN).group()
_POOLS = r'\[hosts\.3090b\.pools\].*\n(?:.*\n){5}'  # the pools table: its header and five pools


def shared(text, host_lock='/tmp/h.lock', slot_lock='/tmp/ci.lock', slot_queue='/tmp/ci_slot.queue', lease_dir=''):
    """The example with local_accounts and its files in /tmp, as a 3090 host has them."""
    leases = f'lease_dir = "{lease_dir}"\n' if lease_dir else ''
    return text.replace('slots = 2', 'local_accounts = ["user-a"]\nslots = 2', 1).replace(
        _FILES, f'host_lock = "{host_lock}"\nslot_lock = "{slot_lock}"\nslot_queue = "{slot_queue}"\n{leases}', 1)


class Load(unittest.TestCase):
    def test_the_example_loads(self):
        i = inv.load(EXAMPLE)
        self.assertEqual(i.this, '3090b')
        self.assertEqual(sorted(i.hosts), ['3090b'])
        self.assertEqual(i.host['pools']['cards'], {'per_card': True, 'class': 'gpu', 'labels': ['3090b-gpu']})
        self.assertEqual(i.host['files']['host_lock'], '/tmp/brainspike-host.lock')
        self.assertNotIn('host_lock', inv.load(PLAIN).host['files'])
        for gone in ('watchdog_log', 'watchdog_notice'):  # decision 13.4: unknown keys now
            with self.assertRaisesRegex(inv.InventoryError, 'unknown keys', msg=gone):
                inv.load(EXAMPLE.replace('slots = 2', f'{gone} = "/x"\nslots = 2', 1))
        with self.assertRaisesRegex(inv.InventoryError, 'class'):  # and the h100 class
            inv.load(EXAMPLE.replace('multi = { count = 1, labels = ["3090b-multi"] }', 'h100 = { per_card = true }'))

    def test_the_retired_keys_are_accepted_and_ignored(self):
        """Decision 11.12: h100's inventory may still hold the first path's keys."""
        old = PLAIN.replace('slots = 2', 'ssh = "<alias>"\ntrusted = true\nremote = false\ncpu_slowdown = 2.7\n'
                              'slots = 2', 1).replace('slot_lock = ', 'host_lock = "/tmp/h.lock"\nslot_lock = ', 1)
        old += '[hosts.3090b.proxy]\nclient = "/x"\n'
        i = inv.load(old)
        self.assertEqual(i.host['cpu_slowdown'], 2.7)
        self.assertEqual(inv.RETIRED_KEYS, {'ssh', 'trusted', 'remote', 'cpu_slowdown', 'proxy'})
        with self.assertRaises(inv.InventoryError):  # only those: another key is still unknown
            inv.load(PLAIN.replace('slots = 2', 'slowdown = 2.7\nslots = 2', 1))
        with self.assertRaises(inv.InventoryError):
            inv.load(PLAIN.replace('slot_lock = ', 'other_lock = "/tmp/o.lock"\nslot_lock = ', 1))

    def test_the_3090_keys_of_decision_12_9(self):
        """A host lock in ``files`` and a ``default_project`` (a project name) load."""
        text = PLAIN.replace('slots = 2', 'default_project = "brainspike"\nslots = 2', 1).replace(
            'slot_lock = ', 'host_lock = "/tmp/h.lock"\nslot_lock = ', 1)
        i = inv.load(text)
        self.assertEqual(i.host['default_project'], 'brainspike')
        self.assertEqual(i.host['files']['host_lock'], '/tmp/h.lock')
        for bad in ('"Brainspike"', '"a b"', '""', '1'):
            with self.assertRaises(inv.InventoryError, msg=bad):
                inv.load(PLAIN.replace('slots = 2', f'default_project = {bad}\nslots = 2', 1))

    def test_the_local_accounts_and_the_push_key(self):
        """Decision 12.9, §5.3: the accounts that share the lock files, and the push key's line."""
        key = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI' + 'A' * 43
        i = inv.load(shared(PLAIN).replace('local_accounts = ["user-a"]', f'local_accounts = ["user-a", "user_b"]\n'
                                             f'push_key = "{key} c"', 1))
        self.assertEqual(i.host['local_accounts'], ['user-a', 'user_b'])
        self.assertEqual(i.host['push_key'], key + ' c')
        for bad in ('[]', '["a", "a"]', '["A"]', '["a b"]', '"a"', '[1]'):
            with self.assertRaises(inv.InventoryError, msg=bad):
                inv.load(shared(PLAIN).replace('local_accounts = ["user-a"]', f'local_accounts = {bad}', 1))
        for bad in (key[:-1], 'ssh-rsa ' + key[12:], key + ' a b', key + '\\n' + key, ''):
            with self.assertRaises(inv.InventoryError, msg=bad):
                inv.load(PLAIN.replace('slots = 2', f'push_key = "{bad}"\nslots = 2', 1))
        i = inv.load(PLAIN.replace('slots = 2', f'probe_key = "{key} 3090a"\nslots = 2', 1))  # 15.1, item 8
        self.assertEqual(i.host['probe_key'], key + ' 3090a')
        for bad in (key[:-1], 'ssh-rsa ' + key[12:], ''):
            with self.assertRaises(inv.InventoryError, msg=bad):
                inv.load(PLAIN.replace('slots = 2', f'probe_key = "{bad}"\nslots = 2', 1))
        other = key[:-1] + 'B'
        i = inv.load(PLAIN.replace('slots = 2', f'probe_key = ["{key} 3090a", "{other} 3090b"]\nslots = 2', 1))  # 15.8
        self.assertEqual(inv.probe_keys(i.host['probe_key']), [key + ' 3090a', other + ' 3090b'])
        self.assertEqual(inv.probe_keys(key), [key])
        for bad in ('[]', f'["{key}", "{key[:-1]}"]', f'["{key}", 1]', f'["{key}", ""]', '1'):
            with self.assertRaises(inv.InventoryError, msg=bad):
                inv.load(PLAIN.replace('slots = 2', f'probe_key = {bad}\nslots = 2', 1))
        self.assertEqual(inv.deny_tokens({'hosts': {'h': {'local_accounts': ['acct7'], 'push_key': key}}}),
                         {'acct7', inv.normalise(key[12:])})  # the type word is the code's: not a token

    def test_shared_files_are_plain_names_in_tmp(self):
        """B1: with local_accounts, root's step makes every ``files`` path root-owned with ACL
        entries, so each must be a plain name directly in /tmp."""
        self.assertEqual(inv.load(shared(PLAIN)).host['files']['slot_queue'], '/tmp/ci_slot.queue')
        for bad in ('/tmp/../etc/shadow', '/etc/shadow', '/tmp/x/../q', '/tmp//q', '/tmp/./q', '/tmp/a/q',
                    H + 'u/q', '/tmp/', '/tmp/.q', '/tmp/a b'):
            for key in ('host_lock', 'slot_lock', 'slot_queue', 'lease_dir'):
                with self.assertRaises(inv.InventoryError, msg=(key, bad)):
                    inv.load(shared(PLAIN, **{key: bad}))
        # the GPU leases shared with the pool runs (decision 15.1): optional, a plain name in /tmp too
        self.assertEqual(inv.load(shared(PLAIN, lease_dir='/tmp/ci-leases')).host['files']['lease_dir'], '/tmp/ci-leases')
        self.assertEqual(inv.load(PLAIN).host['files']['slot_queue'][:1], '/')  # without them: any absolute path
        self.assertFalse(inv.shared_path('/tmp/a/../b'))
        self.assertTrue(inv.shared_path('/tmp/a.b-c_d'))

    def test_the_keys_of_decision_16_are_accepted_and_ignored(self):
        """Decision 16.3 (R1): a pool's ``slot`` and ``audience`` and a host's ``presence``
        load, as the example documents them (commented), checked for shape; without them
        the inventory loads as before."""
        new = re.sub(r'^part = .*\n', '', EXAMPLE, flags=re.M)
        new = re.sub(r'^# (part = .*slot = .*)$', r'\1', new, flags=re.M)
        new = re.sub(r'^# (presence = [^#]*)#.*$', r'\1', new, flags=re.M)
        old, i = inv.load(EXAMPLE), inv.load(new)
        self.assertEqual(i.host['presence'], {'enabled': False, 'tick_seconds': 20, 'infra_failures': 3})
        self.assertEqual(i.host['pools']['part']['slot']['cpus'], ['16-19', '20-23', '24-27'])
        self.assertEqual(i.host['pools']['part']['audience'], 'private')
        self.assertNotIn('presence', old.host)
        self.assertEqual({k: v for k, v in i.host.items() if k not in ('presence', 'pools')},
                         {k: v for k, v in old.host.items() if k != 'pools'})
        self.assertEqual(inv.pool_labels(i, 'part'), inv.pool_labels(old, 'part'))
        self.assertEqual(inv.deny_tokens({'hosts': {'h': {'pools': {'p': {'audience': 'public', 'slot': {
            'cpus': ['0-3'], 'memory': '120G', 'isa': 'x86-64-v3'}}}}}}), set())  # public words, not inventory
        base = re.sub(_POOLS, '', PLAIN).replace('max_temp_c = 80\n', 'max_temp_c = 80\npools = { @ }\n')
        slot = 'workers = 2, memory = "40G", speed = 1.5'
        good = (f'part = {{ count = 2, slot = {{ cpus = ["0-3", "4-5,8"], {slot} }} }}',
                f'part = {{ count = 1, slot = {{ cpus = ["0"], workers = 1, memory = "1T", speed = 2, isa = "avx2" }} }}',
                f'cards = {{ per_card = true, class = "gpu", slot = {{ cpus = ["0-3", "4-7"], {slot} }} }}',
                'light = { count = 1, audience = "private" }')
        for g in good:
            inv.load(base.replace('@', g))
        bad = (f'part = {{ count = 2, slot = {{ cpus = ["0-3"], {slot} }} }}',  # one cpuset a runner
               f'cards = {{ per_card = true, class = "gpu", slot = {{ cpus = ["0-3"], {slot} }} }}',
               f'part = {{ count = 2, slot = {{ cpus = ["0-3", "3-5"], {slot} }} }}',  # a CPU in two
               f'part = {{ count = 1, slot = {{ cpus = ["0-3,2"], {slot} }} }}',
               f'part = {{ count = 1, slot = {{ cpus = ["3-0"], {slot} }} }}',
               f'part = {{ count = 1, slot = {{ cpus = ["0-99999"], {slot} }} }}',
               f'part = {{ count = 1, slot = {{ cpus = ["0;1"], {slot} }} }}',
               f'part = {{ count = 1, slot = {{ cpus = "0-3", {slot} }} }}',
               f'part = {{ count = 1, slot = {{ cpus = [], {slot} }} }}',
               f'part = {{ count = 1, slot = {{ cpus = [0], {slot} }} }}',
               'part = { count = 1, slot = { cpus = ["0"], memory = "40G", speed = 1.0 } }',  # no workers
               'part = { count = 1, slot = { cpus = ["0"], workers = 1, speed = 1.0 } }',
               'part = { count = 1, slot = { cpus = ["0"], workers = 1, memory = "40G" } }',
               'part = { count = 1, slot = { workers = 1, memory = "40G", speed = 1.0 } }',
               f'part = {{ count = 1, slot = {{ cpus = ["0"], {slot}, cpu = 1 }} }}',  # a typo
               'part = { count = 1, slot = { cpus = ["0"], workers = 0, memory = "40G", speed = 1.0 } }',
               'part = { count = 1, slot = { cpus = ["0"], workers = true, memory = "40G", speed = 1.0 } }',
               'part = { count = 1, slot = { cpus = ["0"], workers = 1, memory = "40GB", speed = 1.0 } }',
               'part = { count = 1, slot = { cpus = ["0"], workers = 1, memory = "40G", speed = 0 } }',
               'part = { count = 1, slot = { cpus = ["0"], workers = 1, memory = "40G", speed = "1" } }',
               f'part = {{ count = 1, slot = {{ cpus = ["0"], {slot}, isa = "AVX2" }} }}',
               f'part = {{ count = 1, slot = {{ cpus = ["0"], {slot}, isa = 2 }} }}',
               'part = { count = 1, slot = ["0"] }', 'light = { count = 1, audience = "everyone" }',
               'light = { count = 1, audience = true }', 'light = { count = 1, audiences = "public" }',
               'light = { count = 1, audience = "public" }')  # the public pool is GPU-only (18.7)
        for b in bad:
            with self.assertRaises(inv.InventoryError, msg=b):
                inv.load(base.replace('@', b))
        for b in ('{ enabled = "no" }', '{ enable = false }', '{ tick_seconds = 0 }', '{ infra_failures = 1.5 }',
                  '{ tick_seconds = true }', 'false'):
            with self.assertRaises(inv.InventoryError, msg=b):
                inv.load(PLAIN.replace('slots = 2', f'presence = {b}\nslots = 2', 1))
        for g in ('{}', '{ enabled = true }', '{ tick_seconds = 5, infra_failures = 1 }'):
            inv.load(PLAIN.replace('slots = 2', f'presence = {g}\nslots = 2', 1))
        self.assertEqual(inv.cpu_ranges('0-3,8'), [(0, 3), (8, 8)])

    def test_the_keys_of_decision_18_are_accepted_and_ignored(self):
        """C2 (decision 18.7, 16.3's rule): a host's ``isolation`` and its public pool load, as
        the example documents them (commented), checked for shape; the public pool is out of
        ``pools``, so the host's runners, labels and pools are those without it."""
        from ci_runners import presence, setup
        new = re.sub(r'^# (isolation = "[^"]*")\s.*$', r'\1', EXAMPLE, flags=re.M)
        new = re.sub(r'^# (pub-gpu = .*)$', r'\1', new, flags=re.M)
        old, i = inv.load(EXAMPLE), inv.load(new)
        self.assertEqual(i.host['isolation'], 'ns-subuid')
        self.assertEqual(inv.public_pool(i), ('pub-gpu', {'per_card': True, 'class': 'gpu', 'audience': 'public',
                                                          'labels': ['ci-pub-gpu', '3090b-pub-gpu']}))
        self.assertIsNone(inv.public_pool(old))
        self.assertEqual(i.host['pools'], old.host['pools'])
        self.assertEqual({k: v for k, v in i.host.items() if k != 'isolation'}, dict(old.host))
        self.assertEqual(setup.runners(i), setup.runners(old))
        self.assertEqual(setup._runners(i), setup._runners(old))
        self.assertEqual(presence.runners(i, 28), presence.runners(old, 28))
        for cls in ('light', 'part', 'gpu', 'multi', 'compile'):
            self.assertEqual(inv.pool_labels(i, cls), inv.pool_labels(old, cls), cls)
            self.assertEqual(inv.pool_of(i.host, cls), inv.pool_of(old.host, cls), cls)
        self.assertEqual(inv.deny_tokens({'hosts': {'h': {'isolation': 'ns-subuid'}}}), set())  # a public word
        base = new.replace('isolation = "ns-subuid"', 'isolation = "@"')
        for g in ('account', 'ns-subuid'):
            self.assertEqual(inv.load(base.replace('@', g)).host['isolation'], g)
        for b in ('ns-uid', 'none', 'microvm', '', 'NS-SUBUID', 'account ', '1'):
            with self.assertRaises(inv.InventoryError, msg=b):
                inv.load(base.replace('@', b))
        with self.assertRaises(inv.InventoryError):  # not a string
            inv.load(base.replace('"@"', '1'))
        pub = 'pub-gpu = { per_card = true, class = "gpu", audience = "public", labels = ["ci-pub-gpu", "3090b-pub-gpu"] }'
        good = (pub, pub.replace(', "3090b-pub-gpu"', ''),
                'gpu2 = { per_card = true, audience = "public", class = "gpu", labels = ["ci-pub-gpu"] }')
        for g in good:
            self.assertIsNotNone(inv.public_pool(inv.load(new.replace(pub, g))), g)
        bad = {
            'light': 'pub = { count = 1, class = "light", audience = "public", labels = ["ci-pub-gpu"] }',
            'part': 'pub = { count = 1, class = "part", audience = "public", labels = ["ci-pub-gpu"] }',
            'multi': 'pub = { count = 1, class = "multi", audience = "public", labels = ["ci-pub-gpu"] }',
            'not per card': 'pub = { count = 1, class = "gpu", audience = "public", labels = ["ci-pub-gpu"] }',
            'per card false': 'pub = { per_card = false, class = "gpu", audience = "public", labels = ["ci-pub-gpu"] }',
            'a slot': pub.replace(' }', ', slot = { cpus = ["0-3", "4-7"], workers = 4, memory = "40G", speed = 1.0 } }'),
            'no labels': 'pub-gpu = { per_card = true, class = "gpu", audience = "public" }',
            'no ci-pub-gpu': pub.replace('"ci-pub-gpu", ', ''),
            'a private label': pub.replace('"3090b-pub-gpu"', '"ci-gpu"'),
            'another host label': pub.replace('"3090b-pub-gpu"', '"3090a-pub-gpu"'),
            'the host gpu label': pub.replace('"3090b-pub-gpu"', '"3090b-gpu"'),
            'two public pools': pub + '\n' + pub.replace('pub-gpu =', 'pub-two ='),
        }
        for what, b in bad.items():
            with self.assertRaises(inv.InventoryError, msg=what):
                inv.load(new.replace(pub, b))
        for what in ('light', 'part', 'multi'):  # refused for its class, whatever else it holds
            with self.assertRaisesRegex(inv.InventoryError, 'a public pool is of class gpu only', msg=what):
                inv.load(new.replace(pub, bad[what]))
        with self.assertRaises(inv.InventoryError):  # the host's own gpu pool is needed: it shares its cards
            inv.load(new.replace('cards = { per_card = true, class = "gpu", labels = ["3090b-gpu"] }  # runners 3090b-gpu<N>\n', ''))
        with self.assertRaises(inv.InventoryError):  # and the host's isolation
            inv.load(new.replace('isolation = "ns-subuid"', ''))
        for pool in ('ci-pub-gpu', 'x-pub-gpu'):  # nor as a pool's default label, <pool>, <role> (§5.1)
            with self.assertRaises(inv.InventoryError, msg=pool):
                inv.load(EXAMPLE.replace('multi = { count = 1, labels = ["3090b-multi"] }', f'{pool} = {{ count = 1, class = "multi" }}'))
        with self.assertRaises(inv.InventoryError):
            inv.load(EXAMPLE.replace('"3090b"', '"h-pub-gpu"').replace('3090b.', 'h-pub-gpu.').replace(
                'multi = { count = 1, labels = ["3090b-multi"] }', 'multi = 1'))
        inv.load(EXAMPLE.replace('multi = { count = 1, labels = ["3090b-multi"] }', 'multi = 1'))
        for label in ('ci-pub-gpu', 'ci-pub-cpu', '3090b-pub-gpu', '3090a-pub-gpu'):  # never on a private pool
            with self.assertRaises(inv.InventoryError, msg=label):
                inv.load(EXAMPLE.replace('labels = ["3090b-gpu"]', f'labels = ["3090b-gpu", "{label}"]'))
            with self.assertRaises(inv.InventoryError, msg=label):
                inv.load(EXAMPLE.replace('labels = ["3090b-light"]', f'labels = ["{label}"]'))

    def test_pools_as_tables(self):
        """RUN-5: a pool may give its labels, or have one runner per card."""
        base = re.sub(_POOLS, '', PLAIN).replace(
            'max_temp_c = 80\n', 'max_temp_c = 80\npools = { @ }\n')
        self.assertIn('@', base)
        good = ('light = 2', 'light = { count = 2 }', 'light = { count = 1, labels = ["hx-light"] }',
                'cards = { per_card = true, class = "gpu" }', 'cards = { per_card = true, class = "gpu", labels = ["hx-gpu", "x-1"] }')
        for g in good:
            inv.load(base.replace('@', g))
        bad = ('light = 0', 'light = {}', 'light = { labels = ["a"] }', 'light = { count = 0 }',
               'light = { count = 1, per_card = true }', 'light = { count = 1, labels = [] }',
               'light = { count = 1, labels = ["A"] }', 'light = { count = 1, labels = ["a b"] }',
               'light = { count = 1, labels = ["a", "a"] }', 'light = { count = 1, labels = "a" }',
               'light = { count = 1, extra = 1 }', 'cards = { per_card = 1, class = "gpu" }', 'light = { count = true }',
               'light = { count = 1, labels = ["light"] }', 'light = { count = 1, labels = ["self-hosted"] }',
               'cards = { per_card = true, class = "gpu", labels = ["gpu"] }')  # a bare class or GitHub's own label
        for b in bad:
            with self.assertRaises(inv.InventoryError, msg=b):
                inv.load(base.replace('@', b))
        with self.assertRaisesRegex(inv.InventoryError, 'without gpus'):
            inv.load(base.replace('@', 'cards = { per_card = true, class = "gpu" }').replace('gpus = [0, 1]', 'gpus = []', 1))
        with self.assertRaises(inv.InventoryError):
            inv.load(base.replace('{ @ }', '{}'))

    def test_pools_name_their_class(self):
        """Decision 12.5: a pool's class is its key, or its ``class`` where it names one; each
        class has one pool per host; only gpu pools run a runner per card."""
        base = re.sub(_POOLS, '', PLAIN).replace(
            'max_temp_c = 80\n', 'max_temp_c = 80\ncompile_cpus = "4-15"\npools = { @ }\n')
        i = inv.load(base.replace('@', 'light = 1, cards = { per_card = true, class = "gpu", labels = ["hx-gpu"] }, '
                                       'multi = { count = 1, labels = ["hx-multi"] }, compile = 1, nightly = 1'))
        self.assertEqual(i.host['compile_cpus'], '4-15')
        self.assertEqual((inv.pool_class(i.host, 'cards'), inv.pool_class(i.host, 'multi')), ('gpu', 'multi'))
        self.assertEqual((inv.pool_of(i.host, 'gpu'), inv.pool_of(i.host, 'cards'), inv.pool_of(i.host, 'part')),
                         ('cards', None, None))
        self.assertEqual((inv.pool_labels(i, 'gpu'), inv.pool_labels(i, 'multi'), inv.pool_labels(i, 'light')),
                         (['hx-gpu'], ['hx-multi'], None))
        bad = ('cards = 1',  # no class of that name
               'cards = { count = 1, class = "gpus" }', 'cards = { count = 1, class = 1 }',
               'cards = { per_card = true, class = "multi" }', 'multi = { per_card = true }',
               'part = 1, cpu = { count = 1, class = "part" }',  # two pools of one class
               'multi = { count = 1, labels = ["multi"] }')  # a bare class name as a label
        for b in bad:
            with self.assertRaises(inv.InventoryError, msg=b):
                inv.load(base.replace('@', b))
        with self.assertRaises(inv.InventoryError):
            inv.load(base.replace('@', 'light = 1').replace('compile_cpus = "4-15"', 'compile_cpus = "4-15;x"'))

    def test_peer_labels(self):
        """Decision 13.3: the other host's pool per class; labels, so public."""
        self.assertEqual(inv.peer_labels(inv.load(EXAMPLE), 'multi'), ['3090a-multi'])
        base = re.sub(r'^peer_labels = .*\n', '', EXAMPLE, flags=re.M)
        i = inv.load(base.replace('slots = 2', 'peer_labels = { part = ["hz-cpu"], gpu = ["hz-gpu", "x-1"] }\n'
                                     'slots = 2', 1))
        self.assertEqual((inv.peer_labels(i, 'part'), inv.peer_labels(i, 'gpu'), inv.peer_labels(i, 'compile')),
                         (['hz-cpu'], ['hz-gpu', 'x-1'], None))
        self.assertIsNone(inv.peer_labels(inv.load(base), 'part'))
        for bad in ('{ cpu = ["hz-cpu"] }', '{ h100 = ["hz-gpu"] }', '{ part = [] }', '{ part = "hz-cpu" }',
                    '{ part = ["HZ"] }', '{ part = ["part"] }', '{ part = ["self-hosted"] }', '{ part = ["a", "a"] }',
                    '["hz-cpu"]'):
            with self.assertRaises(inv.InventoryError, msg=bad):
                inv.load(base.replace('slots = 2', f'peer_labels = {bad}\nslots = 2', 1))
        self.assertEqual(inv.deny_tokens({'hosts': {'h': {'peer_labels': {'part': ['secretish-label']}}}}), set())

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
            'this unknown': EXAMPLE.replace('this = "3090b"', 'this = "3090c"'),
            'unknown top key': 'extra = 1\n' + EXAMPLE,
            'unknown host key': EXAMPLE.replace('max_temp_c = 80\n', 'max_temp_c = 80\nfoo = 1\n'),
            'missing account': EXAMPLE.replace('account = "<ci account>"\n', ''),
            'relative home': EXAMPLE.replace('home = "/home/<ci account>"', 'home = "~"'),
            'slots zero': EXAMPLE.replace('slots = 2', 'slots = 0'),
            'slots bool': EXAMPLE.replace('slots = 2', 'slots = true'),
            'memory': EXAMPLE.replace('memory_max = "120G"', 'memory_max = "120GB"'),
            'gpus': EXAMPLE.replace('gpus = [0, 1]\n', 'gpus = [0, "1"]\n'),
            'cpus': EXAMPLE.replace('cpus = "0-27"', 'cpus = "0-27;rm"'),
            'pool count': EXAMPLE.replace('light = { count = 2,', 'light = { count = 0,'),
            'files keys': re.sub(r'slot_queue = .*\n', '', EXAMPLE),
            'role': EXAMPLE.replace('[hosts.3090b]', '[hosts.X3090b]'),
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

    def test_the_default_project_is_public(self):
        """A project name is the repository's, which the design names: not a token."""
        self.assertEqual(inv.deny_tokens({'hosts': {'h': {'default_project': 'brainspike', 'account': 'acct9'}}}),
                         {'acct9'})

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

    def test_a_hosts_uid_or_disk_numbers(self):
        """The lint rules a host's uid and its disks' device numbers (RUN-8 review): a
        fixture uses a fake one, of FAKE_UIDS or a major of FAKE_MAJORS. The literals are
        built here, so that this file holds none."""
        uid, major = 1000 + 9, 250 + 9
        for text in (f'0::/user.slice/user-{uid}.slice/user@{uid}.service/app.slice/x.service',
                     f'/tmp/systemd-private-0a-user@{uid}.service-Xy/tmp', f'uid={uid}', f'uid: {uid}', f'Uid = {uid}',
                     f'XDG_RUNTIME_DIR=/run/user/{uid}', f'uid {uid}', f'the uid {uid} owns it'):
            self.assertEqual(self.kinds(text), ['uid'], text)
        for text in ('user@4242.service', 'user@999.service', 'user@0.service', 'uid=65534', 'user@65535.service',
                     'useruser@1500', 'pid 1500', 'the uid', 'port: 8080', 'user-123456.slice', '/run/user/4242',
                     '/run/user/$(id -u)', 'uid 999', 'fluid 1500'):
            self.assertEqual(self.kinds(text), [], text)
        for text in (f'600 22 {major}:2 /tmp/systemd-private-x/tmp /tmp rw - ext4 /dev/x rw',
                     f"HOST = '22 1 {major}:2 / / rw,relatime shared:1 - ext4 /dev/x rw'",
                     f'1: FLOCK  ADVISORY  WRITE 900 {major:x}:02:1234 0 EOF',
                     f'2: POSIX  ADVISORY  READ  901 {major:02x}:2:1 0 EOF'):
            self.assertEqual(self.kinds(text), ['device'], text)
        for text in ('600 22 250:2 /tmp/x /tmp rw', '23 22 0:5 / /proc rw - proc proc rw', '1: FLOCK  ADVISORY  WRITE 9 fa:02:1 0 EOF',
                     '1: FLOCK  ADVISORY  WRITE 9 00:2a:1 0 EOF', 'at 12:30 /x', 'v1.2 3 4:5 /x'):
            self.assertEqual(self.kinds(text), [], text)

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
        self.assertGreaterEqual(len(values), 4)  # account, home, data, python
        for s in values:
            self.assertIn('<', s)  # every private value is a placeholder


if __name__ == '__main__':
    unittest.main()
