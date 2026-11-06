"""h100's egress bridge (design decision 11.8, §5.3): an HTTP CONNECT proxy on 127.0.0.1
that passes allowlisted names on port 443 to the ssh tunnel's SOCKS5 port.

The runner app, git, pip and conda all speak HTTP CONNECT; not all of them speak SOCKS.
The name goes to the tunnel as a SOCKS5 domain name, so it resolves on the egress host:
local DNS answers for blocked names cannot be trusted. Anything but ``CONNECT
<allowed name>:443`` gets 403; a tunnel that refuses or is down gives 502. One journal
line per connection: the name and the result. ``ci-bridge.service`` runs it.

OpenSSH's dynamic forwarding may answer a SOCKS request before the egress host has
opened the channel, so a name the egress account refuses, or a link ssh has not yet
found dead, can show as a tunnel that closes at once: the log reads ``closed, 0 bytes
down`` right after ``open``.
"""
from __future__ import annotations

import asyncio
import sys
from collections.abc import Collection

from ci_runners.inventory import NAME

MAX_HEAD = 8192        # bytes of request head
HEAD_TIMEOUT = 30      # s to send the head
SOCKS_TIMEOUT = 30     # s for the tunnel to answer
MAX_CONNECTIONS = 256
LINGER = 300           # s the other direction may run on after one side has finished
PORT = 443
CHUNK = 1 << 16


class Refused(Exception):
    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status


def target(head: bytes, allow: Collection[str]) -> str:
    """The allowed name a request head asks for. Raises Refused. Pure.

    Only ``CONNECT <name>:443 HTTP/1.x`` passes, with ``<name>``, lower-cased, on
    ``allow`` exactly and a DNS name (no address literal, no trailing dot, no user part)."""
    line = head.split(b'\r\n', 1)[0]
    try:
        method, authority, version = line.decode('ascii').split(' ')
    except (UnicodeDecodeError, ValueError):
        raise Refused(400, 'malformed request line') from None
    if method != 'CONNECT':
        raise Refused(403, f'method {method[:16]!r} is not CONNECT')
    if version not in ('HTTP/1.1', 'HTTP/1.0'):
        raise Refused(400, 'not HTTP/1.x')
    host, sep, port = authority.rpartition(':')
    host = host.lower()
    if not sep or port != str(PORT):
        raise Refused(403, f'port {port[:8]!r} is not {PORT}')
    if not NAME.match(host) or host not in allow:
        raise Refused(403, f'{host[:80]!r} is not on the allowlist')
    return host


def socks_request(host: str, port: int = PORT) -> bytes:
    """A SOCKS5 CONNECT by domain name (RFC 1928, ATYP 3). Pure."""
    name = host.encode('ascii')
    if not 0 < len(name) < 256:
        raise ValueError('name length')
    return b'\x05\x01\x00\x03' + bytes([len(name)]) + name + port.to_bytes(2, 'big')


async def _socks(host: str, socks_port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """A connection to ``host:443`` through the tunnel. Raises Refused(502)."""
    try:
        r, w = await asyncio.wait_for(asyncio.open_connection('127.0.0.1', socks_port), SOCKS_TIMEOUT)
    except (OSError, asyncio.TimeoutError):
        raise Refused(502, 'the tunnel is down') from None
    try:
        w.write(b'\x05\x01\x00')
        if await asyncio.wait_for(r.readexactly(2), SOCKS_TIMEOUT) != b'\x05\x00':
            raise Refused(502, 'the tunnel refused the greeting')
        w.write(socks_request(host))
        ver, rep, _, atyp = await asyncio.wait_for(r.readexactly(4), SOCKS_TIMEOUT)
        if ver != 5 or rep != 0:
            raise Refused(502, f'the tunnel refused {host} (reply {rep})')
        if atyp == 1:
            n = 4
        elif atyp == 4:
            n = 16
        elif atyp == 3:
            n = (await asyncio.wait_for(r.readexactly(1), SOCKS_TIMEOUT))[0]
        else:
            raise Refused(502, 'the tunnel sent a bad address type')
        await asyncio.wait_for(r.readexactly(n + 2), SOCKS_TIMEOUT)
    except (OSError, asyncio.IncompleteReadError, asyncio.TimeoutError):
        w.close()
        raise Refused(502, f'the tunnel dropped {host}') from None
    except Refused:
        w.close()
        raise
    return r, w


async def _pipe(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> int:
    """Copy until end of file, then half-close; the bytes copied."""
    n = 0
    try:
        while data := await r.read(CHUNK):
            n += len(data)
            w.write(data)
            await w.drain()
        if w.can_write_eof():
            w.write_eof()
    except OSError:
        w.close()
    return n


class Bridge:
    def __init__(self, allow: Collection[str], socks_port: int, log=None) -> None:
        self.allow = frozenset(allow)
        self.socks_port = socks_port
        self.slots = asyncio.Semaphore(MAX_CONNECTIONS)
        # the open connections' tasks: the stream protocol drops its reference once the
        # client's side is lost, and the loop holds tasks only weakly
        self.tasks: set[asyncio.Task] = set()
        self.log = log or (lambda line: print(line, file=sys.stderr, flush=True))

    async def handle(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            await self._handle(r, w)
        finally:
            self.tasks.discard(task)

    async def _handle(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        if self.slots.locked():
            await self._answer(w, 503, 'too many connections')
            return
        async with self.slots:
            try:
                try:
                    head = await asyncio.wait_for(r.readuntil(b'\r\n\r\n'), HEAD_TIMEOUT)
                except asyncio.LimitOverrunError:
                    raise Refused(431, 'request head too large') from None
                except (asyncio.IncompleteReadError, asyncio.TimeoutError, OSError):
                    raise Refused(408, 'no request head') from None
                host = target(head, self.allow)
                ur, uw = await _socks(host, self.socks_port)
            except Refused as e:
                await self._answer(w, e.status, str(e))
                return
            except Exception as e:  # never leave a client without an answer
                await self._answer(w, 502, f'{type(e).__name__}')
                return
            self.log(f'bridge: {host}:{PORT} open')
            w.write(b'HTTP/1.1 200 Connection established\r\n\r\n')
            up, down = asyncio.ensure_future(_pipe(r, uw)), asyncio.ensure_future(_pipe(ur, w))
            try:
                _, pending = await asyncio.wait((up, down), return_when=asyncio.FIRST_COMPLETED)
                if pending:  # one side is done: the other gets LINGER to finish
                    await asyncio.wait(pending, timeout=LINGER)
            finally:
                for t in (up, down):
                    t.cancel()
                uw.close()
                w.close()
            done = down.result() if down.done() and not down.cancelled() else -1
            self.log(f'bridge: {host}:{PORT} closed, {done} bytes down' if done >= 0 else
                     f'bridge: {host}:{PORT} closed after {LINGER} s with one side open')

    async def _answer(self, w: asyncio.StreamWriter, status: int, reason: str) -> None:
        self.log(f'bridge: refused {status}: {reason}')
        try:
            w.write(f'HTTP/1.1 {status} {_PHRASE.get(status, "Refused")}\r\nContent-Length: 0\r\n'
                    'Connection: close\r\n\r\n'.encode())
            await w.drain()
        except OSError:
            pass
        w.close()


_PHRASE = {400: 'Bad Request', 403: 'Forbidden', 408: 'Request Timeout', 431: 'Request Header Fields Too Large',
           502: 'Bad Gateway', 503: 'Service Unavailable'}


async def serve(http_port: int, socks_port: int, allow: Collection[str]) -> None:
    bridge = Bridge(allow, socks_port)
    server = await asyncio.start_server(bridge.handle, '127.0.0.1', http_port, limit=MAX_HEAD)
    print(f'bridge: listening on 127.0.0.1:{http_port}, {len(bridge.allow)} names, tunnel 127.0.0.1:{socks_port}',
          file=sys.stderr, flush=True)
    async with server:
        await server.serve_forever()


def main(host: dict) -> int:
    t = host.get('tunnel')
    if not t:
        print('ci-run bridge: this host has no tunnel', file=sys.stderr)
        return 2
    asyncio.run(serve(t['http_port'], t['socks_port'], t['allow']))
    return 0
