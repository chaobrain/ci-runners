"""The drain switch (design decision 13.1), where a refused part goes (13.3), and a
retry on another host (13.3)."""
from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
import pathlib
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from ci_runners import drain, inventory, part
from tests.test_part import Host

PEER = 'peer_labels = { part = ["hz-cpu"], light = ["hz-light"] }\n'


class State(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = self.tmp.name
        self.host = {'pools': {'light': 1, 'part': 1, 'cards': {'per_card': True, 'class': 'gpu'}}}

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_is_not_drained(self):
        st = drain.read(self.data)
        self.assertEqual(st, {'host': None, 'pools': {}})
        for cls in ('light', 'part', 'gpu', 'multi'):
            self.assertEqual(drain.refusal(st, self.host, cls), '', cls)

    def test_on_and_off_for_the_host_and_a_pool(self):
        st = drain.change(self.data, True, 'cards', now=lambda: 100.0)
        self.assertEqual(st, {'host': None, 'pools': {'cards': 100.0}})
        self.assertEqual(drain.read(self.data), st)  # a fresh read: it outlives the process that set it
        self.assertEqual(drain.refusal(st, self.host, 'gpu'), 'drained: pool cards')
        self.assertEqual(drain.refusal(st, self.host, 'part'), '')  # only that pool
        st = drain.change(self.data, True, now=lambda: 200.0)
        for cls in ('light', 'part', 'gpu'):
            self.assertEqual(drain.refusal(drain.read(self.data), self.host, cls), 'drained: host', cls)
        self.assertEqual(drain.change(self.data, True, now=lambda: 300.0)['host'], 200.0)  # the first time stays
        st = drain.change(self.data, False, 'cards')
        self.assertEqual(st, {'host': 200.0, 'pools': {}})  # the host stays drained
        drain.change(self.data, True, 'part', now=lambda: 1.0)
        self.assertEqual(drain.change(self.data, False), {'host': None, 'pools': {}})  # off: everything
        self.assertEqual(drain.read(self.data), {'host': None, 'pools': {}})

    def test_unreadable_or_malformed_drains_the_host(self):
        path = pathlib.Path(drain.state_path(self.data))
        path.parent.mkdir(parents=True)
        for text in ('', '{', '[]', '{"host": null}', '{"host": "x", "pools": {}}', '{"host": true, "pools": {}}',
                     '{"host": null, "pools": {"a": "b"}}', '{"host": null, "pools": [], "x": 1}'):
            path.write_text(text)
            self.assertIsNone(drain.read(self.data), text)
            self.assertEqual(drain.refusal(drain.read(self.data), self.host, 'part'), drain.UNREADABLE, text)
        path.unlink()
        os.mkfifo(path)  # never blocks, never counts as a state
        self.assertIsNone(drain.read(self.data))
        path.unlink()
        path.symlink_to('/dev/null')
        self.assertIsNone(drain.read(self.data))
        path.unlink()
        path.write_text('{"host": null, "pools": {}}')
        self.assertEqual(drain.refusal(drain.read(self.data), self.host, 'part'), '')
        path.write_text('{')
        for on, pool in ((True, ''), (True, 'part'), (False, 'part')):  # each would undrain the host
            with self.assertRaisesRegex(ValueError, 'drain.json is unreadable.*drain off'):
                drain.change(self.data, on, pool)
            self.assertEqual(path.read_text(), '{', (on, pool))
        self.assertEqual(drain.change(self.data, False), {'host': None, 'pools': {}})  # off rewrites it

    def test_each_change_writes_the_pool_runs_copy(self):
        """Decision 15.1: other accounts cannot read state/ (mode 700); each change writes a
        world-readable copy, which the pool's ``drained`` reads as ``read`` does."""
        from ci_runners import pool
        public = drain.public_path(self.data)
        self.assertFalse(pool.drained(public))  # missing: not drained
        old = os.umask(0o077)
        try:
            drain.change(self.data, True, 'cards', now=lambda: 100.0)
            self.assertEqual(os.stat(public).st_mode & 0o777, 0o644)
            self.assertFalse(pool.drained(public))  # a drained pool of runners drains no pool run
            drain.change(self.data, True, now=lambda: 200.0)
        finally:
            os.umask(old)
        with open(public) as f:
            self.assertEqual(json.load(f), drain.read(self.data))
        self.assertTrue(pool.drained(public))
        drain.change(self.data, False)
        self.assertFalse(pool.drained(public))

    def test_publish_brings_the_copy_up_to_the_state(self):
        """A host drained before the copy existed (an older release) is drained for the pool
        runs at the next part or ``drain status``; an unreadable state publishes a drained host."""
        from ci_runners import pool
        public = pathlib.Path(drain.public_path(self.data))
        drain.publish(self.data)  # no state directory: nothing written
        self.assertFalse(public.exists())
        drain.change(self.data, True, now=lambda: 100.0)
        public.unlink()  # drained by a release without the copy
        self.assertFalse(pool.drained(str(public)))
        drain.publish(self.data)
        self.assertTrue(pool.drained(str(public)))
        self.assertEqual(os.stat(public).st_mode & 0o777, 0o644)
        pathlib.Path(drain.state_path(self.data)).write_text('{')  # unreadable: drained, the copy kept as it is
        before = public.stat().st_mtime_ns
        drain.publish(self.data, now=lambda: 300.0)
        self.assertEqual(public.stat().st_mtime_ns, before)
        drain.change(self.data, False)
        public.write_text('{"host": null, "pools": {}}\n')
        pathlib.Path(drain.state_path(self.data)).write_text('[]')
        drain.publish(self.data, now=lambda: 300.0)
        self.assertEqual(json.loads(public.read_text()), {'host': 300.0, 'pools': {}})
        pathlib.Path(drain.state_path(self.data)).write_text('{"host": null, "pools": {"cards": 5}}')
        drain.publish(self.data)
        self.assertEqual(json.loads(public.read_text()), {'host': None, 'pools': {'cards': 5}})
        self.assertFalse(pool.drained(str(public)))

    def test_the_pool_reads_a_state_as_read_does(self):
        from ci_runners import pool
        path = pathlib.Path(self.data, 'd.json')
        for text in ('', '{', '[]', '{"host": null}', '{"host": "x", "pools": {}}', '{"host": true, "pools": {}}',
                     '{"host": null, "pools": {"a": "b"}}', '{"host": null, "pools": [], "x": 1}',
                     '{"host": null, "pools": {}}', '{"host": 5, "pools": {}}', '{"host": null, "pools": {"a": 1}}'):
            path.write_text(text)
            st = drain.read_path(str(path))
            self.assertEqual(pool.drained(str(path)), st is None or st['host'] is not None, text)
        path.chmod(0)
        if os.geteuid() != 0:
            self.assertTrue(pool.drained(str(path)))  # unreadable: drained
        path.unlink()
        os.mkfifo(path)
        self.assertTrue(pool.drained(str(path)))  # never blocks
        path.unlink()
        path.symlink_to('/dev/null')
        self.assertTrue(pool.drained(str(path)))

    def test_a_pool_by_key_or_class(self):
        self.assertEqual((drain.pool_key(self.host, 'cards'), drain.pool_key(self.host, 'gpu')), ('cards', 'cards'))
        with self.assertRaisesRegex(ValueError, 'no pool or class'):
            drain.pool_key(self.host, 'multi')

    def test_status(self):
        text = drain.status_text({'host': None, 'pools': {'cards': 0.0}}, ['ci-part-a.scope'])
        self.assertIn('host: not drained\n', text)
        self.assertIn('pool cards: drained since ', text)
        self.assertIn('running part scopes: 1\n  ci-part-a.scope\n', text)
        self.assertIn('host: drained (drain state unreadable', drain.status_text(None, []))
        fake = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, 'ci-part-x.scope loaded active running x\n', '')
        self.assertEqual(drain.running_scopes(fake), ['ci-part-x.scope'])
        def broken(*_a, **_k):
            raise OSError('no user manager')
        self.assertEqual(drain.running_scopes(broken), [])

    def test_ci_run_drain(self):
        """``ci-run drain`` reaches drain.main with the inventory's host."""
        etc = pathlib.Path(self.data, 'etc')
        etc.mkdir()
        (etc / 'inventory.toml').write_text(
            f'schema = 1\nthis = "h"\n[hosts.h]\naccount = "a"\nhome = "{self.data}"\ndata = "{self.data}"\nslots = 1\n'
            'memory_max = "1G"\ndisk_min_gib = 0\ngpus = []\nmax_temp_c = 80\npools = { part = 1 }\n'
            f'files = {{ slot_lock = "{self.data}/s.lock", slot_queue = "{self.data}/q" }}\n')
        from ci_runners import cli
        with mock.patch.dict(os.environ, {'CI_RUNNERS_ETC': str(etc)}), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['drain', 'on', '--pool', 'part']), 0)
        self.assertEqual(set(drain.read(self.data)['pools']), {'part'})

    def test_the_cli(self):
        inv = inventory.Inventory('h', {'h': {'data': self.data, **self.host}})
        fake = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, '', '')
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(drain.main(['on', '--pool', 'gpu'], inv, fake), 0)
            self.assertEqual(drain.main(['status'], inv, fake), 0)
            self.assertEqual(drain.main(['on', '--pool', 'nope'], inv, fake), 2)
            self.assertEqual(drain.main(['on', 'x'], inv, fake), 2)
            self.assertEqual(drain.main(['status', 'x'], inv, fake), 2)
            self.assertEqual(drain.main([], inv, fake), 2)
        self.assertEqual(set(drain.read(self.data)['pools']), {'cards'})
        self.assertIn('pool cards: drained since', out.getvalue())
        pathlib.Path(drain.state_path(self.data)).write_text('{')
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.assertEqual(drain.main(['on', '--pool', 'part'], inv, fake), 1)
        self.assertIn('ci-run drain: ', err.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(drain.main(['off'], inv, fake), 0)
        self.assertEqual(drain.read(self.data), {'host': None, 'pools': {}})


class Gate(Host):
    """``part`` on a drained host or pool (13.1), with and without a peer pool (13.3)."""

    def setUp(self):
        super().setUp()
        self.inv_text = self.inv_text.replace('max_temp_c = 80\n', 'max_temp_c = 80\n' + PEER)
        self.inv = inventory.load(self.inv_text)

    def ran(self, out):
        return pathlib.Path(self.path(out), 'work', 'ran').exists()

    def test_a_drained_host_refuses_to_the_peer_pool(self):
        from ci_runners import pool
        drain.change(str(self.data), True)
        os.unlink(drain.public_path(str(self.data)))  # drained by a release without the pool runs' copy
        code, out = self.execute(command='touch "$CI_PART_DIR/ran"')
        self.assertTrue(pool.drained(drain.public_path(str(self.data))))  # the part published it
        self.assertEqual((code, out['outcome'], out['reason'], out['next']), (0, 'unavailable', 'drained: host', 'fallback'))
        self.assertEqual(out['retry-on'], ['self-hosted', 'hz-cpu'])  # the peer's pool, not the registry's fallback
        self.assertFalse(self.ran(out))
        _, out = self.execute({**self.env, 'CI_RUNNER_CLASS': 'light'}, id='l')
        self.assertEqual((out['reason'], out['retry-on']), ('drained: host', ['self-hosted', 'hz-light']))

    def test_the_gate_refuses_before_the_slot(self):
        """The first read is before anything is taken: a drained part does not queue for a
        slot another holds."""
        drain.change(str(self.data), True)
        fd = os.open(self.slot, os.O_WRONLY)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            t0 = time.monotonic()
            _, out = self.execute(queue_timeout_minutes=0.05)
        finally:
            os.close(fd)
        self.assertEqual(out['reason'], 'drained: host')
        self.assertLess(time.monotonic() - t0, 2.5)

    def test_a_drained_pool_refuses_only_its_class(self):
        drain.change(str(self.data), True, 'light')
        _, out = self.execute({**self.env, 'CI_RUNNER_CLASS': 'light'}, id='l')
        self.assertEqual((out['outcome'], out['reason']), ('unavailable', 'drained: pool light'))
        _, out = self.execute(id='p')
        self.assertEqual(out['outcome'], 'pass')

    def test_no_peer_pool_is_nowhere_to_go(self):
        self.inv = inventory.load(self.inv_text.replace(PEER, ''))
        drain.change(str(self.data), True)
        _, out = self.execute()
        self.assertEqual((out['outcome'], out['next'], out['retry-on']), ('unavailable', 'none', []))

    def test_the_last_attempt_goes_nowhere(self):
        drain.change(str(self.data), True)
        _, out = self.execute(attempt_of='hz:org/repo/77-1/unit-1')
        self.assertEqual((out['outcome'], out['next'], out['retry-on'], out['attempt']), ('unavailable', 'none', [], 2))

    def test_an_unreadable_state_refuses(self):
        p = pathlib.Path(drain.state_path(str(self.data)))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('not json')
        _, out = self.execute()
        self.assertEqual((out['outcome'], out['reason']), ('unavailable', drain.UNREADABLE))

    def test_drained_while_it_waited_for_its_slot(self):
        """The second read, once the slot is held: the part ends before its command and lets
        its slot go. (The executor sets signal handlers, so it runs in the main thread.)"""
        fd = os.open(self.slot, os.O_WRONLY)
        fcntl.flock(fd, fcntl.LOCK_EX)

        def later():
            time.sleep(0.5)
            drain.change(str(self.data), True, 'part')
            os.close(fd)
        t = threading.Thread(target=later)
        t.start()
        code, out = self.execute(command='touch "$CI_PART_DIR/ran"', queue_timeout_minutes=0.5)
        t.join(30)
        self.assertEqual((out['outcome'], out['reason'], out['retry-on']),
                         ('unavailable', 'drained: pool part', ['self-hosted', 'hz-cpu']))
        self.assertFalse(self.ran(out))
        fd = os.open(self.slot, os.O_WRONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # the slot was let go
        finally:
            os.close(fd)

    def test_a_running_part_finishes(self):
        """A part whose command has started is not touched by a drain."""
        flag = pathlib.Path(self.tmp.name) / 'go'

        def later():
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and not list((self.data / 'ci-runners/state/parts').rglob('scope.json')):
                time.sleep(0.05)
            drain.change(str(self.data), True)
            flag.write_text('')
        t = threading.Thread(target=later)
        t.start()
        _, out = self.execute(command=f'while [ ! -e {flag} ]; do sleep 0.05; done; touch "$CI_PART_DIR/ran"')
        t.join(30)
        self.assertEqual(out['outcome'], 'pass')
        self.assertTrue(self.ran(out))
        self.assertEqual(drain.read(str(self.data))['host'] is not None, True)


class AcrossHosts(Host):
    """``attempt-of`` naming another host's record (decision 13.3; v1.5.0 notes item 5)."""

    def test_another_hosts_record_gives_the_attempt_by_name(self):
        code, out = self.execute(id='cpu.x-1', attempt_of='hz:org/repo/77-1/cpu.x-1-1')
        self.assertEqual((code, out['outcome'], out['attempt']), (0, 'pass', 2))
        self.assertEqual(out['record'], 'hb:org/repo/77-1/cpu.x-1-2')
        rec = json.loads(pathlib.Path(self.path(out), 'record.json').read_text())
        self.assertEqual(rec['attempt'], 2)

    def test_another_parts_record_is_refused(self):
        for ref in ('hz:org/repo/77-1/other-1', 'hz:org/repo/77-1/unit', 'hz:org/repo/unit-1', 'hz:unit-1',
                    'hz:org/repo/77-1/unit-0', 'HZ:org/repo/77-1/unit-1', 'hz:org/repo/77-1/unit-1/x'):
            with self.assertRaises(part.Refused, msg=ref):
                self.execute(attempt_of=ref, id='unit')

    def test_this_hosts_record_is_read_as_before(self):
        _, first = self.execute(command='exit 0', id='u')
        _, out = self.execute(attempt_of=first['record'], id='u')
        self.assertEqual(out['attempt'], 2)
        with self.assertRaises(part.Refused):  # a path out of this host's records
            self.execute(attempt_of='hb:../../x/u-1', id='u')


if __name__ == '__main__':
    unittest.main()
