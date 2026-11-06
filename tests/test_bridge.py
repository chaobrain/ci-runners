"""h100's egress bridge (design decision 11.8): only CONNECT to an allowlisted name on
port 443 reaches the tunnel, by name; a tunnel that refuses or is down gives 502."""
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from ci_runners import bridge

ALLOW = frozenset({'github.com', 'pypi.org'})


class Target(unittest.TestCase):
    def test_an_allowed_name_on_443(self):
        self.assertEqual(bridge.target(b'CONNECT github.com:443 HTTP/1.1\r\nHost: github.com:443\r\n\r\n', ALLOW),
                         'github.com')
        self.assertEqual(bridge.target(b'CONNECT PyPI.org:443 HTTP/1.0\r\n\r\n', ALLOW), 'pypi.org')

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

    def test_socks_request_by_name(self):
        self.assertEqual(bridge.socks_request('pypi.org'), b'\x05\x01\x00\x03\x08pypi.org\x01\xbb')


class FakeTunnel:
    """A SOCKS5 server that records what it is asked and answers ``rep``; on success it
    echoes what it reads, upper-cased."""

    def __init__(self, rep: int = 0):
        self.rep, self.asked, self.hold = rep, [], False

    async def handle(self, r, w):
        assert await r.readexactly(3) == b'\x05\x01\x00'
        w.write(b'\x05\x00')
        ver, cmd, _, atyp, n = await r.readexactly(5)
        name = await r.readexactly(n)
        port = int.from_bytes(await r.readexactly(2), 'big')
        self.asked.append((atyp, name.decode(), port))
        w.write(bytes([5, self.rep, 0, 1, 0, 0, 0, 0, 0, 0]))
        if self.rep == 0:
            while data := await r.read(1024):
                w.write(data.upper())
            if self.hold:  # a far end that never closes its side
                await asyncio.sleep(5)
        w.close()


class Serve(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tunnel = FakeTunnel()
        self.ts = await asyncio.start_server(lambda r, w: self.tunnel.handle(r, w), '127.0.0.1', 0)
        self.logged = []
        self.b = bridge.Bridge(ALLOW, self.ts.sockets[0].getsockname()[1], log=self.logged.append)
        self.bs = await asyncio.start_server(self.b.handle, '127.0.0.1', 0, limit=bridge.MAX_HEAD)
        self.port = self.bs.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        for s in (self.bs, self.ts):
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

    async def test_an_allowed_connect_is_piped_through_the_tunnel_by_name(self):
        status, got = await self.ask(b'CONNECT github.com:443 HTTP/1.1\r\n\r\n', b'hello')
        self.assertEqual((status, got), (200, b'HELLO'))
        for _ in range(50):  # the far end closes: the bridge logs the bytes it carried
            if any('closed, ' in line for line in self.logged):
                break
            await asyncio.sleep(0.02)
        self.assertIn('bridge: github.com:443 closed, 5 bytes down', self.logged)
        self.assertEqual(self.tunnel.asked, [(3, 'github.com', 443)])

    async def test_a_side_left_open_is_closed_after_the_linger(self):
        self.tunnel.hold = True
        r, w = await asyncio.open_connection('127.0.0.1', self.port)
        w.write(b'CONNECT github.com:443 HTTP/1.1\r\n\r\nping')
        await asyncio.wait_for(r.readuntil(b'\r\n\r\n'), 10)
        self.assertEqual(await asyncio.wait_for(r.readexactly(4), 10), b'PING')
        with mock.patch.object(bridge, 'LINGER', 0.2):
            w.write_eof()  # the client is done sending; the far end never closes its side
            self.assertEqual(await asyncio.wait_for(r.read(), 3), b'')  # the bridge closed it
            await asyncio.sleep(0.1)
        w.close()
        self.assertTrue(any('closed after' in line for line in self.logged), self.logged)
        self.assertEqual(self.b.tasks, set())

    async def test_a_refused_name_never_reaches_the_tunnel(self):
        self.assertEqual((await self.ask(b'CONNECT example.com:443 HTTP/1.1\r\n\r\n'))[0], 403)
        self.assertEqual(self.tunnel.asked, [])
        self.assertTrue(any('example.com' in line and '403' in line for line in self.logged))

    async def test_the_tunnel_refusing_gives_502(self):
        self.tunnel.rep = 2  # not allowed by the ruleset: the egress account's permitopen
        self.assertEqual((await self.ask(b'CONNECT pypi.org:443 HTTP/1.1\r\n\r\n'))[0], 502)

    async def test_the_tunnel_down_gives_502(self):
        self.ts.close()
        await self.ts.wait_closed()
        self.assertEqual((await self.ask(b'CONNECT pypi.org:443 HTTP/1.1\r\n\r\n'))[0], 502)

    async def test_a_head_too_large(self):
        head = b'CONNECT github.com:443 HTTP/1.1\r\nX: ' + b'a' * (2 * bridge.MAX_HEAD) + b'\r\n\r\n'
        self.assertEqual((await self.ask(head))[0], 431)
        self.assertEqual(self.tunnel.asked, [])

    async def test_too_many_connections(self):
        self.b.slots = asyncio.Semaphore(0)
        self.assertEqual((await self.ask(b'CONNECT github.com:443 HTTP/1.1\r\n\r\n'))[0], 503)
        self.assertEqual(self.tunnel.asked, [])


if __name__ == '__main__':
    unittest.main()
