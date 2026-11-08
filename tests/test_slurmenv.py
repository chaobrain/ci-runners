"""``ci-run slurm env`` (design decision 14.9, D4; RUN-9e): a project's env built on the login
host through the owner's login, its egress a reverse forward to the driver behind a relay
that takes only the build's secret, and nothing listening after the build."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import pathlib
import socket
import sys
import threading
import time
import unittest
from unittest import mock

from ci_runners import envbuild, slurmenv, slurmlogin, slurmsetup as ss
from tests.test_envbuild import HEAD, Api
from tests.test_slurmsetup import Site

SPEC = 'schema = 1\npython = "3.12"\nrequirements = ["requirements.txt"]\n[conda]\npackages = ["nest-simulator=3.10"]\n'
#: a conda and the python it creates: each asks its proxy (https_proxy) for a tunnel, as
#: pip and conda would, and logs what it was asked to do
PROBE = r'''
import base64, os, socket, sys, urllib.parse
def probe(host):
    url = os.environ.get('https_proxy')
    if not url:
        return 'direct'
    u = urllib.parse.urlsplit(url)
    s = socket.create_connection((u.hostname, u.port), timeout=10)
    auth = base64.b64encode(f'{u.username}:{u.password}'.encode()).decode()
    s.sendall(f'CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\nProxy-Authorization: Basic {auth}\r\n\r\n'.encode())
    return s.recv(200).split(b'\r\n')[0].decode()
'''
CONDA = r'''
args = sys.argv[1:]
d = args[args.index('--prefix') + 1]
if 'no-python' in args:  # a corrupt package, reported, yet exit 0
    os.makedirs(d)
    sys.exit(0)
os.makedirs(os.path.join(d, 'bin'))
py = os.path.join(d, 'bin', 'python')
with open(py, 'w') as f:
    f.write(PYTHON)
os.chmod(py, 0o755)
got = probe('conda.anaconda.org')
with open(os.path.join(d, 'conda.log'), 'w') as f:
    f.write(' '.join(args) + '\n' + got + '\n' + f'nice {os.nice(0)}\n')
    f.write(f'home {os.environ["HOME"]} {sorted(os.listdir(os.environ["HOME"]))}\n')
    f.write(f'pip {os.environ.get("PIP_CONFIG_FILE")} {os.environ.get("PYTHONNOUSERSITE")}\n')
sys.exit(0 if got.endswith(('200 Connection established', 'direct')) else 1)
'''
PY = r'''
got = probe('pypi.org')
d = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
reqs = [open(a).read() for a in sys.argv if a.endswith('.txt')]
specs = [a for a in sys.argv[4:] if not a.startswith('-') and not a.endswith('.txt')]
with open(os.path.join(d, 'pip.log'), 'a') as f:
    f.write(' '.join(sys.argv[1:4]) + '\n' + got + '\n' + ''.join(reqs) + f'install {" ".join(specs)}\n')
sys.exit(0 if got.endswith(('200 Connection established', 'direct')) and 'fail-me' not in specs else 1)
'''


def fake_conda() -> str:
    python = f'#!{sys.executable}\n' + PROBE + PY
    return f'#!{sys.executable}\n' + PROBE + f'PYTHON = {python!r}\n' + CONDA


class Egress:
    """The driver's egress forward: an HTTP proxy that records each request head and
    answers 200."""

    def __init__(self):
        self.srv = socket.create_server(('127.0.0.1', 0))
        self.port, self.heads = self.srv.getsockname()[1], []
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            with c:
                head = b''
                while b'\r\n\r\n' not in head and (chunk := c.recv(4096)):
                    head += chunk
                self.heads.append(head.decode())
                c.sendall(b'HTTP/1.1 200 Connection established\r\n\r\n')

    def close(self):
        self.srv.close()


class Relay(unittest.TestCase):
    def test_only_the_builds_secret_passes_and_the_header_is_dropped(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 's.sock')
            got = []
            up = socket.socket(socket.AF_UNIX)
            up.bind(path)
            up.listen(4)
            self.addCleanup(up.close)

            def serve():
                c, _ = up.accept()
                with c:
                    got.append(c.recv(4096))
                    c.sendall(b'HTTP/1.1 200 Connection established\r\n\r\nhello')
            threading.Thread(target=serve, daemon=True).start()
            r = slurmlogin.Relay('s3cret', path)
            self.addCleanup(r.close)

            def ask(auth):
                with socket.create_connection(('127.0.0.1', r.port), timeout=10) as s:
                    s.sendall(b'CONNECT pypi.org:443 HTTP/1.1\r\nHost: pypi.org:443\r\n' + auth + b'\r\n')
                    out = b''
                    while chunk := s.recv(4096):
                        out += chunk
                    return out
            good = 'Basic ' + base64.b64encode(b'ci:s3cret').decode()
            wrong = 'Basic ' + base64.b64encode(b'ci:other').decode()
            self.assertTrue(ask(b'').startswith(b'HTTP/1.1 407'))
            self.assertTrue(ask(f'Proxy-Authorization: {wrong}\r\n'.encode()).startswith(b'HTTP/1.1 407'))
            self.assertTrue(ask(f'Proxy-Authorization: {good}\r\nProxy-Authorization: {good}\r\n'.encode())
                            .startswith(b'HTTP/1.1 407'))  # one header, not two
            self.assertEqual(got, [])
            self.assertEqual(ask(f'Proxy-Authorization: {good}\r\n'.encode()),
                             b'HTTP/1.1 200 Connection established\r\n\r\nhello')
            self.assertEqual(got, [b'CONNECT pypi.org:443 HTTP/1.1\r\nHost: pypi.org:443\r\n\r\n'])
            self.assertEqual(r.srv.getsockname()[0], '127.0.0.1')  # the login host's loopback only
            r.close()
            with self.assertRaises(OSError):
                socket.create_connection(('127.0.0.1', r.port), timeout=5)

    def relay(self):
        import tempfile
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        path = os.path.join(d.name, 's.sock')
        got = []
        up = socket.socket(socket.AF_UNIX)
        up.bind(path)
        up.listen(8)
        self.addCleanup(up.close)

        def serve():
            while True:
                try:
                    c, _ = up.accept()
                except OSError:
                    return
                got.append(c)  # a tunnel that stays open
        threading.Thread(target=serve, daemon=True).start()
        r = slurmlogin.Relay('s3cret', path)
        self.addCleanup(r.close)
        return r, got

    def test_only_connect_passes_and_a_long_head_is_refused(self):
        r, got = self.relay()
        good = 'Basic ' + base64.b64encode(b'ci:s3cret').decode()
        with socket.create_connection(('127.0.0.1', r.port), timeout=10) as s:
            s.sendall(f'GET http://pypi.org/ HTTP/1.1\r\nHost: pypi.org\r\nProxy-Authorization: {good}\r\n\r\n'
                      'GET http://pypi.org/x HTTP/1.1\r\nProxy-Authorization: x\r\n\r\n'.encode())
            self.assertTrue(s.recv(4096).startswith(b'HTTP/1.1 405'))  # a plain request could keep the connection
            self.assertEqual(s.recv(4096), b'')
        with socket.create_connection(('127.0.0.1', r.port), timeout=10) as s:
            s.sendall(b'CONNECT pypi.org:443 HTTP/1.1\r\nX: ' + b'a' * slurmlogin.RELAY_HEAD)
            self.assertEqual(s.recv(4096), b'')  # closed, nothing passed
        self.assertEqual(got, [])

    def test_another_users_connection_is_closed_before_it_takes_a_slot(self):
        r, got = self.relay()
        with socket.create_connection(('127.0.0.1', r.port), timeout=10) as s:
            mine = (s.getsockname()[1], r.port)
            for _ in range(100):
                if len(r.live) == 1:
                    break
                time.sleep(0.05)
            c = next(iter(r.live))
            self.assertEqual(slurmlogin._peer_uid(c), os.getuid())  # by /proc/net/tcp
            self.assertEqual((c.getpeername()[1], c.getsockname()[1]), mine)
            with mock.patch('builtins.open', side_effect=OSError('no /proc')):
                self.assertIsNone(slurmlogin._peer_uid(c))  # unknown: the header check still holds
            client, ours = mine
            proc = ('  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid\n'
                    f'   0: 0100007F:{client:04X} 0100007F:{ours + 1:04X} 01 0:0 00:0 0 1001\n'  # the port to another
                    f'   1: 0100007F:{ours:04X} 0100007F:{client:04X} 01 0:0 00:0 0 1002\n'  # the relay's end
                    f'   2: 0100007F:{client:04X} 0100007F:{ours:04X} 01 0:0 00:0 0 1003\n')
            with mock.patch('builtins.open', mock.mock_open(read_data=proc)):
                self.assertEqual(slurmlogin._peer_uid(c), 1003)
        with mock.patch.object(slurmlogin, '_peer_uid', return_value=os.getuid() + 1), \
                mock.patch.object(slurmlogin, 'RELAY_CONNS', 1):
            r, got = self.relay()
            with socket.create_connection(('127.0.0.1', r.port), timeout=10) as s:
                self.assertEqual(s.recv(4096), b'')
            self.assertEqual(r.slots._value, 1)  # it took no slot

    def test_idle_and_surplus_connections_are_closed_and_close_ends_every_tunnel(self):
        good = 'Basic ' + base64.b64encode(b'ci:s3cret').decode()
        with mock.patch.object(slurmlogin, 'RELAY_CONNS', 2), mock.patch.object(slurmlogin, 'RELAY_IDLE', 2.0):
            r, got = self.relay()
            idle = [socket.create_connection(('127.0.0.1', r.port), timeout=10) for _ in range(2)]
            time.sleep(0.3)
            with socket.create_connection(('127.0.0.1', r.port), timeout=10) as s:
                t0 = time.monotonic()
                self.assertEqual(s.recv(4096), b'')  # beyond the cap: closed at once
                self.assertLess(time.monotonic() - t0, 1.0)
            for s in idle:
                with s:
                    self.assertEqual(s.recv(4096), b'')  # no head within RELAY_IDLE: closed
            with socket.create_connection(('127.0.0.1', r.port), timeout=10) as s:
                t0 = time.monotonic()
                s.setblocking(False)
                closed = False
                while time.monotonic() - t0 < 6 and not closed:  # a byte at a time, each well within the limit
                    try:
                        s.send(b'C')
                        closed = s.recv(1) == b''
                    except BlockingIOError:
                        pass
                    except OSError:
                        closed = True
                    time.sleep(0.4)
                self.assertTrue(closed)
                self.assertLess(time.monotonic() - t0, 3.5)  # one deadline for the whole head
            time.sleep(0.3)
            tunnel = socket.create_connection(('127.0.0.1', r.port), timeout=10)
            self.addCleanup(tunnel.close)
            tunnel.sendall(f'CONNECT pypi.org:443 HTTP/1.1\r\nProxy-Authorization: {good}\r\n\r\n'.encode())
            for _ in range(100):
                if got:
                    break
                time.sleep(0.05)
            self.assertEqual(len(got), 1)
            time.sleep(2.5)
            self.assertEqual(len(r.live), 2)  # a tunnel has no idle limit: both its sockets live
            r.close()
            self.assertEqual(tunnel.recv(4096), b'')  # close ends the tunnel too
            for _ in range(100):
                if not r.live:
                    break
                time.sleep(0.05)
            self.assertEqual(r.live, set())


class Env(Site):
    def setUp(self):
        super().setUp()
        self.apply()
        self.egress = Egress()
        self.addCleanup(self.egress.close)
        (self.etc / 'projects').mkdir()
        (self.etc / 'projects' / 'p.toml').write_text('name = "p"\nrepository = "org/repo"\ncontext = "p/ci"\n[hosts.hb]\nmemory_max = "2G"\n')
        self.api = Api({HEAD: {'.github/ci-runners.toml': SPEC, 'requirements.txt': 'numpy\n'}})
        self.envs = self.base / 'envs'

    def conda(self):
        c = self.base / 'conda' / 'bin' / 'conda'
        c.parent.mkdir(parents=True)
        c.write_text(fake_conda())
        c.chmod(0o755)
        (self.base / 'conda' / slurmlogin.CONDA_DONE).touch()

    def env(self, **kw) -> tuple[int, str]:
        ctx, out = self.ctx()
        kw.setdefault('tunnel_port', self.egress.port)
        self.n = getattr(self, 'n', 0) + 1  # a build a second apart from the last
        with mock.patch.dict(os.environ, {'GH_TOKEN': 'tok'}), \
                mock.patch.object(envbuild, '_stamp', return_value=f'20261108T1200{self.n:02d}Z'):
            rc = slurmenv.env(ctx, 'cluster1', 'acct1', 'p', 'owner', call=self.api, **kw)
        return rc, out.getvalue()

    def test_a_build_through_the_tunnel_links_each_variant_and_leaves_nothing_listening(self):
        self.conda()
        rc, said = self.env()
        self.assertEqual(rc, 0, said)
        build = next(n for n in os.listdir(self.envs) if n.startswith('p-2'))
        self.assertEqual({n: os.readlink(self.envs / n) for n in ('p-cpu', 'p-cuda13')},
                         {'p-cpu': build, 'p-cuda13': build})  # the account's targets' variants
        d = self.envs / build
        conda_log = (d / 'conda.log').read_text()
        self.assertIn('--override-channels -c conda-forge python=3.12 pip nest-simulator=3.10', conda_log)
        self.assertIn('200 Connection established', conda_log)  # its egress: relay, socket, the driver's forward
        self.assertIn(f'nice {min(19, os.nice(0) + 10)}\n', conda_log)  # the steps run at nice 10
        self.assertIn('-m pip install\nHTTP/1.1 200 Connection established\nnumpy\n', (d / 'pip.log').read_text())
        heads = self.egress.heads
        self.assertEqual([h.split('\r\n')[0] for h in heads], ['CONNECT conda.anaconda.org:443 HTTP/1.1',
                                                               'CONNECT pypi.org:443 HTTP/1.1'])
        self.assertFalse(any('roxy-Authorization' in h for h in heads))  # the secret stays on the login host
        rec = json.loads((d / 'ci-env.json').read_text())
        self.assertEqual((rec['repository'], rec['sha'], rec['stamp']), ('org/repo', HEAD, build[2:]))
        self.assertEqual(os.listdir(self.base / 'run'), [])  # no socket, no requirement files left
        self.assertEqual((self.d / 'ssh' / 'forwarded').read_text().count('1'), 2)
        kept = json.loads((self.data / 'ci-runners' / 'state' / 'slurm' / 'cluster1' / 'env-acct1-p.json').read_text())
        self.assertEqual((kept['spec'], kept['files'], kept['builds']),
                         (SPEC, {'requirements.txt': 'numpy\n'}, {build: ['cpu', 'cuda13']}))
        calls = (self.d / 'ssh' / 'calls.log').read_text()
        self.assertNotIn('tok', calls)  # the token is in no argv and no ssh child's environment
        self.assertNotIn(self.base.name + '/run/egress', ''.join(h for h in heads))

    def test_the_accounts_own_conda_and_pip_settings_do_not_reach_the_build(self):
        self.conda()
        (self.home / '.condarc').write_text('custom_channels:\n  conda-forge: https://mirror.example/cloud\n')
        (self.home / '.config' / 'pip').mkdir(parents=True)
        (self.home / '.config' / 'pip' / 'pip.conf').write_text('[global]\nindex-url = https://mirror.example/simple\n')
        rc, said = self.env()
        self.assertEqual(rc, 0, said)
        log = (self.envs / self.builds()[0] / 'conda.log').read_text()
        home = next(x for x in log.splitlines() if x.startswith('home ')).split()[1]
        self.assertEqual(os.path.dirname(home), str(self.base / 'run'))  # its own, empty
        self.assertIn(f'home {home} []\n', log)
        self.assertIn(f'pip {os.devnull} 1\n', log)
        self.assertEqual(os.listdir(self.base / 'run'), [])  # and removed after

    def builds(self):
        return sorted(n for n in os.listdir(self.envs) if n.startswith('p-2'))

    def links(self):
        return {n: os.readlink(self.envs / n) for n in ('p-cpu', 'p-cuda13') if os.path.lexists(self.envs / n)}

    def test_two_variants_get_two_builds_with_their_own_install(self):
        self.conda()
        self.api.files[HEAD]['.github/ci-runners.toml'] = (
            SPEC.replace('requirements = ', 'install = ["base-only"]\nrequirements = ')
            + '[variant.cpu]\ninstall = ["cpu-only"]\n[variant.cuda13]\ninstall = ["jax-cuda13-plugin"]\n')
        rc, said = self.env()
        self.assertEqual(rc, 0, said)
        b = self.builds()
        self.assertEqual([n[len('p-20261108T120001Z'):] for n in b], ['-cpu', '-cuda13'])  # no base build: none uses it
        self.assertEqual(self.links(), {'p-cpu': b[0], 'p-cuda13': b[1]})
        for n, spec in zip(b, ('cpu-only', 'jax-cuda13-plugin')):
            self.assertIn(f'numpy\ninstall {spec}\n', (self.envs / n / 'pip.log').read_text())
            rec = json.loads((self.envs / n / 'ci-env.json').read_text())
            self.assertEqual((rec['variant'], rec['install']), (n.rsplit('-', 1)[1], [spec]))
        kept = json.loads((self.data / 'ci-runners' / 'state' / 'slurm' / 'cluster1' / 'env-acct1-p.json').read_text())
        self.assertEqual(kept['builds'], {b[0]: ['cpu'], b[1]: ['cuda13']})
        self.assertIn(f'cpu -> {b[0]}; cuda13 -> {b[1]}', said)

    def test_a_variant_the_spec_does_not_name_links_the_base_build(self):
        self.conda()
        self.api.files[HEAD]['.github/ci-runners.toml'] = SPEC + '[variant.cuda13]\ninstall = ["jax-cuda13-plugin"]\n'
        rc, said = self.env()
        self.assertEqual(rc, 0, said)
        b = self.builds()
        self.assertEqual(b, ['p-20261108T120001Z', 'p-20261108T120001Z-cuda13'])
        self.assertEqual(self.links(), {'p-cpu': b[0], 'p-cuda13': b[1]})
        self.assertIn('numpy\ninstall \n', (self.envs / b[0] / 'pip.log').read_text())  # the base's install

    def test_a_failed_variant_build_leaves_every_link_as_it_was(self):
        self.conda()
        self.env()
        before, builds = self.links(), self.builds()
        self.api.files[HEAD]['.github/ci-runners.toml'] = SPEC + '[variant.cuda13]\ninstall = ["fail-me"]\n'
        with self.assertRaisesRegex(ss.SetupError, 'build on owner'):
            self.env()  # the base build passes first; the variant's fails
        self.assertEqual((self.links(), self.builds()), (before, builds))  # and this run's builds are gone

    def test_old_builds_are_kept_per_variant(self):
        self.conda()
        spec = SPEC + '[variant.cuda13]\ninstall = ["jax-cuda13-plugin"]\n'
        self.api.files[HEAD]['.github/ci-runners.toml'] = spec
        for _ in range(4):
            self.assertEqual(self.env()[0], 0)
        b = self.builds()
        self.assertEqual(len(b), 8)  # within the grace: nothing removed
        old = time.time() - slurmenv.grace(self.site(), 'acct1') - 60
        for n in b:
            if (self.envs / n / slurmlogin.UNLINKED).exists():
                os.utime(self.envs / n / slurmlogin.UNLINKED, (old, old))
        rc, said = self.env()
        left = self.builds()
        self.assertEqual(sum(1 for n in left if n.endswith('-cuda13')), 3)  # the newest 3 of each kind
        self.assertEqual(sum(1 for n in left if not n.endswith('-cuda13')), 3)
        self.assertIn("removed ['p-20261108T120001Z', 'p-20261108T120001Z-cuda13', 'p-20261108T120002Z', "
                      "'p-20261108T120002Z-cuda13']", said)

    def test_a_failed_step_leaves_the_links_and_removes_its_build(self):
        self.conda()
        self.env()
        before = {n: os.readlink(self.envs / n) for n in ('p-cpu', 'p-cuda13')}
        self.egress.close()  # the driver's egress is gone: conda's tunnel fails
        with self.assertRaisesRegex(ss.SetupError, 'build on owner: exit 2'):
            self.env(variants=['cpu'])
        self.assertEqual({n: os.readlink(self.envs / n) for n in ('p-cpu', 'p-cuda13')}, before)
        self.assertEqual(sorted(n for n in os.listdir(self.envs) if n.startswith('p-2')), [before['p-cpu']])
        self.assertEqual(os.listdir(self.base / 'run'), [])

    def test_old_builds_are_removed_but_linked_ones_and_those_unlinked_within_the_grace(self):
        self.conda()
        old = time.time() - slurmenv.grace(self.site(), 'acct1') - 60
        for stamp in ('20260101T000000Z', '20260102T000000Z', '20260103T000000Z', '20260104T000000Z',
                      '20260105T000000Z'):
            (self.envs / f'p-{stamp}').mkdir()
            if stamp != '20260103T000000Z':  # unlinked for longer than any job runs, but this one
                (self.envs / f'p-{stamp}' / slurmlogin.UNLINKED).touch()
                os.utime(self.envs / f'p-{stamp}' / slurmlogin.UNLINKED, (old, old))
        os.symlink('p-20260101T000000Z', self.envs / 'p-other')  # a variant this account no longer builds
        rc, said = self.env()
        left = sorted(n for n in os.listdir(self.envs) if n.startswith('p-2'))
        self.assertEqual(left[:4], ['p-20260101T000000Z', 'p-20260103T000000Z', 'p-20260104T000000Z',
                                    'p-20260105T000000Z'])
        self.assertEqual(len(left), 5)
        self.assertIn("removed ['p-20260102T000000Z']", said)
        self.assertTrue((self.envs / 'p-20260103T000000Z' / slurmlogin.UNLINKED).exists())  # its grace starts now
        self.assertFalse((self.envs / left[-1] / slurmlogin.UNLINKED).exists())  # the new build is linked
        self.assertGreaterEqual(slurmenv.grace(self.site(), 'acct1'), 60 * 60)

    def test_without_conda_the_pinned_installer_goes_first(self):
        script = (f'#!/bin/sh\nmkdir -p "$3/bin"\ncat > "$3/bin/conda" <<\'EOF\'\n{fake_conda()}\nEOF\n'
                  'chmod 755 "$3/bin/conda"\n')
        sha = hashlib.sha256(script.encode()).hexdigest()
        fetched = []

        def opener(url, timeout):
            fetched.append(url)
            return io.BytesIO(script.encode())
        partial = self.base / 'conda' / 'bin' / 'conda'  # an install that stopped half way
        partial.parent.mkdir(parents=True)
        partial.write_text('#!/bin/sh\nexit 1\n')
        partial.chmod(0o755)
        (self.base / 'conda' / 'stale').touch()
        with mock.patch.object(slurmenv, 'MINIFORGE', ('Mf.sh', 'https://x/Mf.sh', sha)):
            rc, said = self.env(opener=opener)
            self.assertEqual(rc, 0, said)
            self.assertTrue(os.access(self.base / 'conda' / 'bin' / 'conda', os.X_OK))
            self.assertEqual(sorted(os.listdir(self.base / 'conda')), [slurmlogin.CONDA_DONE, 'bin'])  # redone afresh
            self.assertEqual(fetched, ['https://x/Mf.sh'])
            self.assertFalse((self.base / 'run' / 'Mf.sh').exists())
            self.env(opener=opener)
            self.assertEqual(len(fetched), 1)  # the conda is there now
            slurmenv.installer(str(self.data / 'ci-runners' / 'cache'), opener)
            self.assertEqual(len(fetched), 1)  # and the installer cached
        with mock.patch.object(slurmenv, 'MINIFORGE', ('Mf.sh', 'https://x/Mf.sh', '0' * 64)):
            with self.assertRaisesRegex(ss.SetupError, 'is not the pinned'):
                slurmenv.installer(str(self.data / 'ci-runners' / 'cache'), opener)
            self.assertFalse((self.data / 'ci-runners' / 'cache' / 'Mf.sh').exists())

    def test_egress_modes_and_refusals(self):
        self.conda()
        with self.assertRaisesRegex(ss.SetupError, 'needs this host\'s egress forward'):
            self.env(tunnel_port=0)
        with self.assertRaisesRegex(ss.SetupError, 'not a variant'):
            self.env(variants=['rocm'])
        self.site_file.write_text(self.site_file.read_text().replace('egress = "tunnel"', 'egress = "direct"'))
        rc, said = self.env(tunnel_port=0, variants=['cpu'])
        build = os.readlink(self.envs / 'p-cpu')
        self.assertFalse(os.path.lexists(self.envs / 'p-cuda13'))  # only the variant asked for
        self.assertIn('direct', (self.envs / build / 'conda.log').read_text())
        self.assertFalse((self.d / 'ssh' / 'forwarded').exists())
        self.site_file.write_text(self.site_file.read_text().replace(
            'egress = "direct"', f'egress = "proxy"\nproxy = "http://127.0.0.1:{self.egress.port}"'))
        self.env(tunnel_port=0)
        build = os.readlink(self.envs / 'p-cpu')
        self.assertIn('200 Connection established', (self.envs / build / 'conda.log').read_text())
        self.assertFalse((self.d / 'ssh' / 'forwarded').exists())  # the site's proxy, not a forward
        self.assertEqual(len(self.egress.heads), 2)
        self.site_file.write_text(self.site_file.read_text().replace('egress = "proxy"', 'egress = "push"').replace(
            f'proxy = "http://127.0.0.1:{self.egress.port}"', ''))
        with self.assertRaisesRegex(ss.SetupError, 'D4 b'):
            self.env()
        self.site_file.write_text(self.site_file.read_text().replace('egress = "push"', 'egress = "tunnel"'))
        with mock.patch.object(slurmenv, 'SOCKET_MAX', 20), self.assertRaisesRegex(ss.SetupError, 'Unix socket path'):
            self.env()

    def test_builds_of_two_projects_run_one_at_a_time(self):
        import fcntl
        inst = self.base / 'run' / 'Mf.sh'
        inst.write_text('#!/bin/sh\nmkdir -p "$3/bin"\nprintf "#!/bin/sh\\n" > "$3/bin/conda"\nchmod 755 "$3/bin/conda"\n')
        done = []
        with open(self.envs / slurmlogin.BUILD_LOCK, 'a') as held:
            fcntl.flock(held, fcntl.LOCK_EX)  # another project's build: conda and its package cache are shared
            t = threading.Thread(target=lambda: done.append(slurmlogin.build(
                str(self.base), 'q', '20261108T120000Z', [one(['cpu'], [['mkdir', str(self.envs / 'q-20261108T120000Z')]])],
                {}, {}, 'direct', installer=str(inst))))
            t.start()
            time.sleep(1.0)
            self.assertFalse((self.base / 'conda').exists())  # it waits for the lock: no install
            self.assertFalse((self.envs / 'q-20261108T120000Z').exists())  # and no step
        t.join(30)
        self.assertEqual(done[0]['links'], ['q-cpu'])
        self.assertTrue((self.base / 'conda' / slurmlogin.CONDA_DONE).exists())

    def test_a_conda_create_that_leaves_no_python_fails_the_build(self):
        self.conda()
        d = self.envs / 'p-20261108T120000Z'
        conda = str(self.base / 'conda' / 'bin' / 'conda')
        mark = self.base / 'run' / 'next-step'
        steps = [[conda, 'create', '--yes', '--prefix', str(d), 'python=3.12', 'no-python'], ['touch', str(mark)]]
        with self.assertRaisesRegex(slurmlogin.LoginError, 'conda create exited 0 but left no .*/bin/python'):
            slurmlogin.build(str(self.base), 'p', '20261108T120000Z', [one(['cpu'], steps)], {}, {}, 'direct')
        self.assertFalse(mark.exists())  # no later step runs
        self.assertFalse(os.path.lexists(d))
        slurmlogin.build(str(self.base), 'p', '20261108T120001Z',  # a create that makes its python goes on
                         [one(['cpu'], [[conda, 'create', '--prefix', str(self.envs / 'p-20261108T120001Z')],
                                        ['touch', str(mark)]])], {}, {}, 'direct')
        self.assertTrue(mark.exists())

    def test_an_upstream_without_a_commit_fails_the_build(self):
        self.conda()
        with self.assertRaisesRegex(slurmlogin.LoginError, 'brainstate: no commit checked out'):
            slurmlogin.build(str(self.base), 'p', '20261108T120000Z', [one(['cpu'])], {}, {'upstream_names': ['brainstate']},
                             'direct')
        self.assertFalse(os.path.lexists(self.envs / 'p-20261108T120000Z'))
        self.assertFalse(os.path.lexists(self.envs / 'p-cpu'))

    def test_a_requirement_file_stays_in_its_directory(self):
        self.conda()
        with self.assertRaisesRegex(slurmlogin.LoginError, 'outside the requirements'):
            slurmlogin.build(str(self.base), 'p', '20261108T120000Z', [one(['cpu'])], {'../../etc/python': 'x'}, {},
                             'direct')
        for builds in ([], [one(['cpu']), one(['cuda13'])], [one(['cpu']), one(['cpu'], variant='cuda13')],
                       [one(['../x'])], [one(['cpu'], variant='../x')]):
            with self.assertRaisesRegex(slurmlogin.LoginError, 'the builds are malformed', msg=builds):
                slurmlogin.build(str(self.base), 'p', '20261108T120000Z', builds, {}, {}, 'direct')
        self.assertEqual((self.base / 'etc' / 'python').read_text(), f'{sys.executable}\n')
        self.assertEqual(os.listdir(self.base / 'run'), [])

    def test_refresh_rebuilds_only_when_an_upstream_moved(self):
        ctx, out = self.ctx()
        with self.assertRaisesRegex(ss.SetupError, 'no build of p kept'):
            slurmenv.env(ctx, 'cluster1', 'acct1', 'p', 'owner', refresh=True, tunnel_port=1)
        state = self.data / 'ci-runners' / 'state' / 'slurm' / 'cluster1'
        spec = SPEC + '[[upstream]]\nrepository = "chaobrain/brainstate"\nref = "origin/main"\n'
        at_a = {'build': 'b', 'sha': HEAD, 'commits': {'brainstate': 'a' * 40}}
        (state / 'env-acct1-p.json').write_text(json.dumps({'sha': HEAD, 'spec': spec, 'files': {'requirements.txt': 'x\n'},
                                                            'variants': {'cpu': at_a, 'cuda13': at_a}}))
        sent = []
        with mock.patch.object(envbuild, 'head_of', return_value='a' * 40), \
                mock.patch.object(envbuild, '_token', side_effect=AssertionError('a refresh holds no token')):
            self.assertEqual(slurmenv.env(ctx, 'cluster1', 'acct1', 'p', 'owner', refresh=True, tunnel_port=1,
                                          opener=no_network), 0)
        self.assertIn('up to date', out.getvalue())
        with mock.patch.object(envbuild, 'head_of', return_value='b' * 40), \
                mock.patch.object(envbuild, '_token', side_effect=AssertionError('a refresh holds no token')), \
                mock.patch.object(ss, 'login_call', side_effect=lambda site, via, call, args, **kw:
                                  sent.append((call, args)) or ({'conda': True} if call == 'conda_state' else
                                                                {'commits': {}, 'builds': {'b': []}, 'links': [], 'removed': []})):
            slurmenv.env(ctx, 'cluster1', 'acct1', 'p', 'owner', refresh=True, tunnel_port=1, opener=no_network)
        self.assertIn('moved: brainstate; rebuilding cpu, cuda13', out.getvalue())
        build = sent[-1][1]
        self.assertEqual((build['files'], build['record']['sha']), ({'requirements.txt': 'x\n'}, HEAD))
        self.assertTrue(any(s[:3] == ['git', 'clone', '--quiet'] for s in build['builds'][0]['steps']))

    def refresh(self, kept, head, **kw):
        """A refresh with ``kept`` variants and every upstream's branch at ``head``: what it
        says and the builds it sends."""
        ctx, out = self.ctx()
        state = self.data / 'ci-runners' / 'state' / 'slurm' / 'cluster1'
        spec = SPEC + '[[upstream]]\nrepository = "chaobrain/brainstate"\nref = "origin/main"\n'
        (state / 'env-acct1-p.json').write_text(json.dumps({'sha': HEAD, 'spec': spec, 'files': {'requirements.txt': 'x\n'},
                                                            'variants': kept}))
        sent = []
        with mock.patch.object(envbuild, 'head_of', return_value=head), \
                mock.patch.object(ss, 'login_call', side_effect=lambda site, via, call, args, **_:
                                  sent.append(args) or ({'conda': True} if call == 'conda_state' else
                                                        {'commits': {'brainstate': head}, 'builds': {'b2': args['builds'][0]['links']},
                                                         'links': [], 'removed': []})):
            slurmenv.env(ctx, 'cluster1', 'acct1', 'p', 'owner', refresh=True, tunnel_port=1, opener=no_network, **kw)
        return out.getvalue(), [b['links'] for a in sent if 'builds' in a for b in a['builds']]

    def test_refresh_after_a_subset_run_rebuilds_each_variant_that_is_behind(self):
        a, b = 'a' * 40, 'b' * 40
        cur = {'build': 'b1', 'sha': HEAD, 'commits': {'brainstate': b}}
        said, sent = self.refresh({'cpu': cur, 'cuda13': {**cur, 'commits': {'brainstate': a}}}, b)
        self.assertIn('moved: brainstate; rebuilding cuda13', said)  # cpu was rebuilt alone at b, cuda13 is at a
        self.assertEqual(sent, [['cuda13']])
        said, sent = self.refresh({'cpu': cur, 'cuda13': {**cur, 'sha': '1' * 40}}, b)
        self.assertIn('moved: none; rebuilding cuda13', said)  # built from an older spec
        said, sent = self.refresh({'cpu': cur}, b)
        self.assertIn('rebuilding cuda13', said)  # never built
        said, sent = self.refresh({'cpu': cur, 'cuda13': cur}, b)
        self.assertIn('up to date', said)
        self.assertEqual(sent, [])
        said, sent = self.refresh({'cpu': cur, 'cuda13': {**cur, 'commits': {'brainstate': a}}}, b, variants=['cpu'])
        self.assertIn('up to date', said)  # only the variant asked for

    def test_a_subset_run_keeps_the_other_variants_builds_in_the_kept_file(self):
        self.conda()
        self.assertEqual(self.env()[0], 0)
        first = self.builds()[0]
        self.assertEqual(self.env(variants=['cpu'])[0], 0)
        kept = json.loads((self.data / 'ci-runners' / 'state' / 'slurm' / 'cluster1' / 'env-acct1-p.json').read_text())
        second = self.builds()[1]
        self.assertEqual({v: e['build'] for v, e in kept['variants'].items()}, {'cpu': second, 'cuda13': first})
        self.assertEqual(kept['variants']['cuda13']['sha'], HEAD)

    def git_steps(self, d, how):
        """Steps that check an upstream ``u`` out in build ``d``: a fresh commit (``how``, its
        message) or a clone of ``how``'s."""
        src = str(self.envs / d / 'src' / 'u')
        if how.startswith('/'):
            return [['git', 'clone', '-q', how, src]]
        return [['git', 'init', '-q', src],
                ['git', '-C', src, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '--allow-empty', '-m', how]]

    def test_builds_whose_upstream_commits_differ_fail_and_leave_the_links(self):
        self.conda()
        self.env()
        before, builds = self.links(), self.builds()
        stamp = '20261108T130000Z'
        first, second = f'p-{stamp}', f'p-{stamp}-cuda13'
        with self.assertRaisesRegex(slurmlogin.LoginError, 'upstream moved during the run: u; run it again'):
            slurmlogin.build(str(self.base), 'p', stamp,
                             [one(['cpu'], self.git_steps(first, 'one')),
                              one(['cuda13'], self.git_steps(second, 'two'), variant='cuda13')],
                             {}, {'upstream_names': ['u']}, 'direct')
        self.assertEqual((self.links(), self.builds()), (before, builds))
        r = slurmlogin.build(str(self.base), 'p', stamp,
                             [one(['cpu'], self.git_steps(first, 'one')),
                              one(['cuda13'], self.git_steps(second, str(self.envs / first / 'src' / 'u')), variant='cuda13')],
                             {}, {'upstream_names': ['u']}, 'direct')
        self.assertEqual(self.links(), {'p-cpu': first, 'p-cuda13': second})  # one commit set: it passes
        got = json.loads((self.envs / second / 'ci-env.json').read_text())['commits']
        self.assertEqual(r['commits'], got)
        self.assertRegex(got['u'], '^[0-9a-f]{40}$')

    def test_a_link_path_that_is_not_a_link_moves_no_link(self):
        self.conda()
        self.env()
        before, builds = self.links(), self.builds()
        os.unlink(self.envs / 'p-cuda13')
        stamp = '20261108T130000Z'
        ran = self.envs / 'ran'
        (self.envs / 'p-cuda13').mkdir()
        with self.assertRaisesRegex(slurmlogin.LoginError, 'p-cuda13 is not a link'):
            slurmlogin.build(str(self.base), 'p', stamp, [one(['cpu', 'cuda13'], [['touch', str(ran)]])], {}, {}, 'direct')
        self.assertFalse(ran.exists())  # refused before any build
        (self.envs / 'p-cuda13').rmdir()
        with self.assertRaisesRegex(slurmlogin.LoginError, 'p-cuda13 is not a link'):
            slurmlogin.build(str(self.base), 'p', stamp,
                             [one(['cpu'], [['mkdir', str(self.envs / f'p-{stamp}')]]),
                              one(['cuda13'], [['mkdir', str(self.envs / f'p-{stamp}-cuda13'), str(self.envs / 'p-cuda13')]],
                                  variant='cuda13')], {}, {}, 'direct')  # it turns up during the build
        self.assertEqual((os.readlink(self.envs / 'p-cpu'), self.builds()), (before['p-cpu'], builds))

    def test_a_variant_no_link_holds_keeps_no_build_past_the_grace(self):
        self.conda()
        old = time.time() - slurmenv.grace(self.site(), 'acct1') - 60
        for day in range(1, 6):
            d = self.envs / f'p-2026010{day}T000000Z-cuda12'  # a variant the account no longer runs
            d.mkdir()
            if day != 5:
                (d / slurmlogin.UNLINKED).touch()
                os.utime(d / slurmlogin.UNLINKED, (old, old))
        rc, said = self.env()
        self.assertEqual(rc, 0, said)
        self.assertEqual([n for n in self.builds() if n.endswith('-cuda12')], ['p-20260105T000000Z-cuda12'])  # its grace
        self.assertIn("removed ['p-20260101T000000Z-cuda12', 'p-20260102T000000Z-cuda12', 'p-20260103T000000Z-cuda12', "
                      "'p-20260104T000000Z-cuda12']", said)


def one(links, steps=(), variant=''):
    """A ``slurmlogin.build`` entry."""
    return {'variant': variant, 'links': list(links), 'steps': list(steps), 'install': []}


def no_network(url, timeout):
    raise AssertionError(f'{url}: no network in a test')


if __name__ == '__main__':
    unittest.main()
