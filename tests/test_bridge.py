"""The egress proxy (design decision 11.10): only CONNECT to a name on the allowlist (exact,
or strictly below a suffix entry) on port 443 is connected, directly and only to a global
address; it answers 200 only once that connection is open, and 502 when it fails."""
from __future__ import annotations

import asyncio
import pathlib
import socket
import unittest
from unittest import mock

from ci_runners import bridge

ROOT = pathlib.Path(__file__).resolve().parents[1]
ALLOW = frozenset({'github.com', 'pypi.org', '.actions.githubusercontent.com'})
GLOBAL6 = '2606:4700:4700::1111'  # a global address (no IPv4 literal: the inventory lint)


def info(*addrs):
    """``getaddrinfo`` results for ``addrs``."""
    return [(socket.AF_INET6 if ':' in a else socket.AF_INET, socket.SOCK_STREAM, 6, '', (a, 443)) for a in addrs]


class Target(unittest.TestCase):
    def test_an_allowed_name_on_443(self):
        self.assertEqual(bridge.target(b'CONNECT github.com:443 HTTP/1.1\r\nHost: github.com:443\r\n\r\n', ALLOW),
                         'github.com')
        self.assertEqual(bridge.target(b'CONNECT PyPI.org:443 HTTP/1.0\r\n\r\n', ALLOW), 'pypi.org')

    def test_a_suffix_entry_matches_only_names_below_it(self):
        for name in ('x.actions.githubusercontent.com', 'pipelinesghubeus7.actions.githubusercontent.com',
                     'a.b.actions.githubusercontent.com'):
            self.assertEqual(bridge.target(f'CONNECT {name}:443 HTTP/1.1\r\n\r\n'.encode(), ALLOW), name)
        for name in ('actions.githubusercontent.com', 'evilactions.githubusercontent.com',
                     'githubusercontent.com', 'x.actions.githubusercontent.com.evil.org'):
            with self.assertRaises(bridge.Refused, msg=name) as cm:
                bridge.target(f'CONNECT {name}:443 HTTP/1.1\r\n\r\n'.encode(), ALLOW)
            self.assertEqual(cm.exception.status, 403)
        self.assertTrue(bridge.allowed('x.y.org', {'x.y.org'}))
        self.assertFalse(bridge.allowed('w.x.y.org', {'x.y.org'}))  # an exact entry is not a suffix

    def test_refusals(self):
        for head, status in ((b'CONNECT github.com:80 HTTP/1.1\r\n\r\n', 403),
                             (b'CONNECT github.com HTTP/1.1\r\n\r\n', 403),
                             (b'CONNECT github.com:0443 HTTP/1.1\r\n\r\n', 403),
                             (b'GET http://github.com/ HTTP/1.1\r\n\r\n', 403),
                             (b'GET github.com:443 HTTP/1.1\r\n\r\n', 403),
                             (b'CONNECT example.com:443 HTTP/1.1\r\n\r\n', 403),
                             (b'CONNECT evil.github.com:443 HTTP/1.1\r\n\r\n', 403),
                             (b'CONNECT github.com.:443 HTTP/1.1\r\n\r\n', 403),
                             (b'CONNECT user@github.com:443 HTTP/1.1\r\n\r\n', 403),
                             (b'CONNECT [::1]:443 HTTP/1.1\r\n\r\n', 403),
                             (b'CONNECT github.com:443 HTTP/2\r\n\r\n', 400),
                             (b'CONNECT  github.com:443 HTTP/1.1\r\n\r\n', 400),
                             (b'CONNECT g\xc3\xa9.com:443 HTTP/1.1\r\n\r\n', 400)):
            with self.assertRaises(bridge.Refused, msg=head) as cm:
                bridge.target(head, ALLOW)
            self.assertEqual(cm.exception.status, status, head)

    def test_a_name_on_the_list_must_still_be_a_name(self):
        with self.assertRaises(bridge.Refused):
            bridge.target(b'CONNECT a_b.org:443 HTTP/1.1\r\n\r\n', {'a_b.org'})
        with self.assertRaises(bridge.Refused):  # an address under a suffix entry is no name
            bridge.target(b'CONNECT 1.2.3:443 HTTP/1.1\r\n\r\n', {'.2.3'})


class AllowFile(unittest.TestCase):
    def test_entries_comments_and_blanks(self):
        self.assertEqual(bridge.load_allow('# head\n\nGitHub.com\n  .blob.core.windows.net  # storage\npypi.org\n'),
                         {'github.com', '.blob.core.windows.net', 'pypi.org'})

    def test_a_bad_entry_or_an_empty_list_refuses(self):
        for text in ('github.com\nnot a name\n', 'localhost\n', '.com\n', '1.2.3\n', 'github.com:443\n',
                     '..github.com\n', '*.github.com\n', '# only comments\n\n', ''):
            with self.assertRaises(ValueError, msg=text):
                bridge.load_allow(text)

    def test_the_repository_allowlist_covers_what_the_runners_use(self):
        allow = bridge.load_allow((ROOT / 'setup' / 'egress' / 'allow').read_text())
        for name in ('github.com', 'api.github.com', 'codeload.github.com', 'broker.actions.githubusercontent.com',
                     'launch.actions.githubusercontent.com', 'run.actions.githubusercontent.com',
                     'token.actions.githubusercontent.com', 'tokenghub.actions.githubusercontent.com',
                     'pipelinesghubeus22.actions.githubusercontent.com',
                     'run-actions-3-azure-eastus.actions.githubusercontent.com',
                     'results-receiver.actions.githubusercontent.com', 'artifactcache.actions.githubusercontent.com',
                     'productionresultssa0.blob.core.windows.net', 'pkg.github.com', 'objects.githubusercontent.com',
                     'pypi.org', 'files.pythonhosted.org', 'conda.anaconda.org'):
            self.assertTrue(bridge.allowed(name, allow), name)

    def test_main_refuses_a_bad_file(self):
        import tempfile
        with tempfile.NamedTemporaryFile('w', suffix='.allow') as f:
            f.write('github.com\nbad entry\n')
            f.flush()
            with mock.patch('sys.stderr'):
                self.assertEqual(bridge.main(['--port', '1', '--allow', f.name]), 2)
                self.assertEqual(bridge.main(['--port', '0', '--allow', f.name]), 2)
                self.assertEqual(bridge.main(['--port', '1', '--allow', f.name + '.absent']), 2)


class Open(unittest.IsolatedAsyncioTestCase):
    async def test_only_global_addresses(self):
        self.assertEqual(bridge.global_addresses(info('192.0.2.1', 'fd00::5', '::1', GLOBAL6, GLOBAL6)), [GLOBAL6])

        async def resolve(host):
            return info('192.0.2.1', 'fd00::5', '::1')
        with mock.patch('asyncio.open_connection') as oc:
            with self.assertRaises(bridge.Refused) as cm:
                await bridge._open('github.com', resolve)
            oc.assert_not_called()  # nothing is connected to a local address
        self.assertEqual(cm.exception.status, 403)

    async def test_a_global_address_is_connected(self):
        async def resolve(host):
            return info('fd00::5', GLOBAL6)
        conn = (mock.Mock(), mock.Mock())

        async def opened(host, port):
            self.assertEqual((host, port), (GLOBAL6, 443))
            return conn
        with mock.patch('asyncio.open_connection', side_effect=opened):
            self.assertIs(await bridge._open('github.com', resolve), conn)

    async def test_a_failing_connection_or_resolution_gives_502(self):
        async def resolve(host):
            return info(GLOBAL6)

        async def refused(host, port):
            raise ConnectionRefusedError()
        with mock.patch('asyncio.open_connection', side_effect=refused):
            with self.assertRaises(bridge.Refused) as cm:
                await bridge._open('github.com', resolve)
        self.assertEqual(cm.exception.status, 502)

        async def unresolved(host):
            raise socket.gaierror('no name')
        with self.assertRaises(bridge.Refused) as cm:
            await bridge._open('github.com', unresolved)
        self.assertEqual(cm.exception.status, 502)


class FarEnd:
    """An upstream that echoes what it reads, upper-cased."""

    def __init__(self):
        self.hold = False

    async def handle(self, r, w):
        while data := await r.read(1024):
            w.write(data.upper())
        if self.hold:  # a far end that never closes its side
            await asyncio.sleep(5)
        w.close()


class Serve(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.far = FarEnd()
        self.fs = await asyncio.start_server(lambda r, w: self.far.handle(r, w), '127.0.0.1', 0)
        self.asked, self.gate, self.fail = [], None, None
        self.logged = []
        self.b = bridge.Bridge(ALLOW, log=self.logged.append, opener=self.opener)
        self.bs = await asyncio.start_server(self.b.handle, '127.0.0.1', 0, limit=bridge.MAX_HEAD)
        self.port = self.bs.sockets[0].getsockname()[1]

    async def opener(self, host):
        self.asked.append(host)
        if self.gate is not None:
            await self.gate.wait()
        if self.fail is not None:
            raise self.fail
        return await asyncio.open_connection('127.0.0.1', self.fs.sockets[0].getsockname()[1])

    async def asyncTearDown(self):
        for s in (self.bs, self.fs):
            s.close()
            await s.wait_closed()

    async def ask(self, head: bytes, then: bytes = b''):
        r, w = await asyncio.open_connection('127.0.0.1', self.port)
        w.write(head)
        status = await asyncio.wait_for(r.readuntil(b'\r\n\r\n'), 10)
        got = b''
        if then:
            w.write(then)
            got = await asyncio.wait_for(r.readexactly(len(then)), 10)
        w.close()
        return int(status.split(b' ')[1]), got

    async def test_an_allowed_connect_is_piped(self):
        status, got = await self.ask(b'CONNECT x.actions.githubusercontent.com:443 HTTP/1.1\r\n\r\n', b'hello')
        self.assertEqual((status, got), (200, b'HELLO'))
        for _ in range(50):  # the far end closes: the proxy logs the bytes it carried
            if any('closed, ' in line for line in self.logged):
                break
            await asyncio.sleep(0.02)
        self.assertIn('egress: x.actions.githubusercontent.com:443 closed, 5 bytes down', self.logged)
        self.assertEqual(self.asked, ['x.actions.githubusercontent.com'])

    async def test_200_only_once_the_connection_is_open(self):
        self.gate = asyncio.Event()
        r, w = await asyncio.open_connection('127.0.0.1', self.port)
        w.write(b'CONNECT github.com:443 HTTP/1.1\r\n\r\n')
        with self.assertRaises(asyncio.TimeoutError):  # nothing while the connection is not open
            await asyncio.wait_for(r.read(1), 0.3)
        self.gate.set()
        self.assertIn(b' 200 ', await asyncio.wait_for(r.readuntil(b'\r\n\r\n'), 10))
        w.close()

    async def test_a_failed_connection_gives_502_and_never_200(self):
        self.fail = bridge.Refused(502, 'github.com:443 does not answer')
        self.assertEqual((await self.ask(b'CONNECT github.com:443 HTTP/1.1\r\n\r\n'))[0], 502)
        self.fail = bridge.Refused(403, 'github.com resolves to no global address')
        self.assertEqual((await self.ask(b'CONNECT github.com:443 HTTP/1.1\r\n\r\n'))[0], 403)
        self.fail = RuntimeError('anything else')
        self.assertEqual((await self.ask(b'CONNECT github.com:443 HTTP/1.1\r\n\r\n'))[0], 502)
        self.assertFalse(any(' open' in line for line in self.logged), self.logged)

    async def test_a_side_left_open_is_closed_after_the_linger(self):
        self.far.hold = True
        r, w = await asyncio.open_connection('127.0.0.1', self.port)
        w.write(b'CONNECT github.com:443 HTTP/1.1\r\n\r\nping')
        await asyncio.wait_for(r.readuntil(b'\r\n\r\n'), 10)
        self.assertEqual(await asyncio.wait_for(r.readexactly(4), 10), b'PING')
        with mock.patch.object(bridge, 'LINGER', 0.2):
            w.write_eof()  # the client is done sending; the far end never closes its side
            self.assertEqual(await asyncio.wait_for(r.read(), 3), b'')  # the proxy closed it
            await asyncio.sleep(0.1)
        w.close()
        self.assertTrue(any('closed after' in line for line in self.logged), self.logged)
        self.assertEqual(self.b.tasks, set())

    async def test_a_head_that_never_comes(self):
        with mock.patch.object(bridge, 'HEAD_TIMEOUT', 0.2):
            r, w = await asyncio.open_connection('127.0.0.1', self.port)
            w.write(b'CONNECT github.com:443 HTTP/1.1\r\n')  # and nothing more
            status = await asyncio.wait_for(r.readuntil(b'\r\n\r\n'), 10)
            w.close()
        self.assertEqual(int(status.split(b' ')[1]), 408)
        self.assertEqual(self.asked, [])

    async def test_a_refused_name_is_never_connected(self):
        for name in (b'example.com', b'actions.githubusercontent.com'):
            self.assertEqual((await self.ask(b'CONNECT ' + name + b':443 HTTP/1.1\r\n\r\n'))[0], 403)
        self.assertEqual(self.asked, [])
        self.assertTrue(any('example.com' in line and '403' in line for line in self.logged))

    async def test_a_head_too_large(self):
        head = b'CONNECT github.com:443 HTTP/1.1\r\nX: ' + b'a' * (2 * bridge.MAX_HEAD) + b'\r\n\r\n'
        self.assertEqual((await self.ask(head))[0], 431)
        self.assertEqual(self.asked, [])

    async def test_too_many_connections(self):
        self.b.slots = asyncio.Semaphore(0)
        self.assertEqual((await self.ask(b'CONNECT github.com:443 HTTP/1.1\r\n\r\n'))[0], 503)
        self.assertEqual(self.asked, [])


if __name__ == '__main__':
    unittest.main()
