"""The sandbox prototype's id maps and group modes (experiments/sandbox; design 18, items 4 and 5)."""
from __future__ import annotations

import importlib.util
import pathlib
import stat
import types
import unittest

HERE = pathlib.Path(__file__).resolve().parents[1] / 'experiments' / 'sandbox'


def _load(name):
    spec = importlib.util.spec_from_file_location(f'experiment_{name}', HERE / f'{name}.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sandbox = _load('sandbox')
check = _load('check')

# an account 'acct' (uid and gid 5000) with a subordinate range, a GPU group's line and another account's lines
SUBUID = 'other:100000:65536\nacct:300000:65536\n'
SUBGID = 'other:100000:65536\n# a comment\nacct:300000:65536\nbroken line\nacct:x:1\nacct:3000:1\n5000:3100:0\n'


def plan(groups, *, mapping='subuid', subgid=SUBGID, gpu_gid=3000, gid=5000):
    return sandbox.id_plan(mapping, groups, 'acct', 5000, gid, SUBUID, subgid, gpu_gid=gpu_gid)


def node(gid, mode):
    return types.SimpleNamespace(st_gid=gid, st_mode=stat.S_IFCHR | mode, st_uid=0)


class Lines(unittest.TestCase):
    def test_every_line_of_the_account_by_name_or_uid(self):
        self.assertEqual(sandbox.subid_lines(SUBGID, 'acct', 5000), [(300000, 65536), (3000, 1)])
        self.assertEqual(sandbox.subid_lines('5000:7000:1\nacct:8000:2\n', 'acct', 5000), [(7000, 1), (8000, 2)])
        self.assertEqual(sandbox.subid_lines(SUBGID, 'nobody', 1), [])

    def test_the_range_is_the_first_line_large_enough_for_the_job(self):
        self.assertEqual(sandbox.subordinate_range([(3000, 1), (300000, 65536)], 'f'), (300000, 65536))
        self.assertEqual(sandbox.subordinate_range([(1, 1001)], 'f'), (1, 1001))
        with self.assertRaises(sandbox.Cannot):
            sandbox.subordinate_range([(3000, 1), (1, 1000)], 'f')


class Plan(unittest.TestCase):
    def test_gpu_maps_the_gpu_group_beyond_every_other_inside_id_and_holds_it_alone(self):
        p = plan('gpu')
        self.assertEqual(p['uid_map'], ['0', '5000', '1', '1', '300000', '65536'])
        self.assertEqual(p['gid_map'], ['0', '5000', '1', '1', '300000', '65536', '65537', '3000', '1'])
        self.assertEqual(p['job_groups'], [65537])
        self.assertEqual(p['gpu_inside'], 65537)
        self.assertEqual((p['job_kernel_uid'], p['job_kernel_gid']), (300999, 300999))
        self.assertEqual(sandbox._groups_arg(p['job_groups']), ['--groups=65537'])

    def test_the_inside_id_follows_a_smaller_range(self):
        p = plan('gpu', subgid='acct:300000:2000\nacct:3000:1\n')
        self.assertEqual(p['gid_map'], ['0', '5000', '1', '1', '300000', '2000', '2001', '3000', '1'])
        self.assertEqual(p['uid_map'][-1], '2000')  # the pair has one size
        self.assertEqual(p['job_groups'], [2001])

    def test_gpu_without_the_line_names_the_line_to_add(self):
        with self.assertRaises(sandbox.Cannot) as c:
            plan('gpu', subgid='acct:300000:65536\n')
        self.assertIn('"acct:3000:1"', str(c.exception))
        with self.assertRaises(sandbox.Cannot):
            plan('gpu', subgid='other:3000:1\nacct:300000:65536\n')  # another account's line
        with self.assertRaises(sandbox.Cannot):
            plan('gpu', gpu_gid=None)
        with self.assertRaises(sandbox.Cannot):
            plan('gpu', gpu_gid=5000)  # the account's primary group
        with self.assertRaises(sandbox.Cannot):
            plan('gpu', gpu_gid=300010, subgid='acct:300000:65536\n')  # inside the range

    def test_kept_and_none_map_no_gpu_group(self):
        for groups, arg in (('kept', ['--keep-groups']), ('none', ['--clear-groups'])):
            p = plan(groups)
            self.assertEqual(p['gid_map'], ['0', '5000', '1', '1', '300000', '65536'])
            self.assertIsNone(p['gpu_inside'])
            self.assertEqual(sandbox._groups_arg(p['job_groups']), arg)

    def test_single_keeps_the_groups_only(self):
        p = plan('kept', mapping='single')
        self.assertEqual((p['uid_map'], p['gid_map'], p['setgroups']), (['0', '5000', '1'], ['0', '5000', '1'], 'deny'))
        for groups in ('gpu', 'none'):
            with self.assertRaises(sandbox.Cannot):
                plan(groups, mapping='single')

    def test_the_gpu_group_is_the_card_nodes_group(self):
        self.assertEqual(sandbox.gpu_group(4, node(3000, 0o660), node(3000, 0o660)), 3000)
        self.assertEqual(sandbox.gpu_group(4, node(3000, 0o660), node(0, 0o666)), 3000)
        for n, ctl in ((node(0, 0o666), node(0, 0o666)), (node(3000, 0o600), node(3000, 0o660)),
                       (node(3000, 0o660), node(3001, 0o660))):
            with self.assertRaises(sandbox.Cannot):
                sandbox.gpu_group(4, n, ctl)


class Identity(unittest.TestCase):
    SUB = [(300000, 65536)]

    def ids(self, uid, gid, groups):
        return {'uid': [uid] * 4, 'gid': [gid] * 4, 'groups': groups}

    def test_check_8_holds_only_for_a_subordinate_pair_with_the_gpu_group_alone(self):
        held = check.identity_held
        self.assertTrue(held(self.ids(300999, 300999, [3000]), 5000, self.SUB, self.SUB, [3000]))
        self.assertTrue(held(self.ids(300999, 300999, []), 5000, self.SUB, self.SUB, []))
        self.assertFalse(held(self.ids(300999, 300999, [3000, 4000, 5000]), 5000, self.SUB, self.SUB, [3000]))
        self.assertFalse(held(self.ids(300999, 300999, []), 5000, self.SUB, self.SUB, [3000]))
        self.assertFalse(held(self.ids(5000, 5000, [3000]), 5000, self.SUB, self.SUB, [3000]))  # the account
        self.assertFalse(held(self.ids(300999, 5000, [3000]), 5000, self.SUB, self.SUB, [3000]))  # its gid
        self.assertFalse(held(None, 5000, self.SUB, self.SUB, []))

    def test_could_open_by_the_files_bits(self):
        f = types.SimpleNamespace(st_uid=5000, st_gid=4000, st_mode=stat.S_IFREG | 0o640)
        self.assertFalse(check.could_open(self.ids(300999, 300999, [3000]), f))
        self.assertTrue(check.could_open(self.ids(300999, 300999, [3000, 4000]), f))
        self.assertTrue(check.could_open(self.ids(300999, 4000, []), f))
        self.assertTrue(check.could_open(self.ids(5000, 5000, []), f))
        self.assertTrue(check.could_open(self.ids(300999, 300999, []), types.SimpleNamespace(
            st_uid=5000, st_gid=4000, st_mode=stat.S_IFREG | 0o644)))


if __name__ == '__main__':
    unittest.main()
