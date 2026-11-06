"""The egress proxy (design decision 11.10, §5.3): an HTTP CONNECT proxy on the egress
host's 127.0.0.1 that connects allowlisted names on port 443 directly.

h100's ``ci-tunnel.service`` forwards its local port here over ssh (``-L``), and the
egress account's key opens that one forward only; the runner app, git, pip and conda
all speak HTTP CONNECT. ``ci-egress.service``, a system unit of the egress host, runs
``ci-run egress --port P --allow FILE`` as a dynamic user of its own.

The allowlist (``setup/egress/allow``, installed root-owned) holds one entry per line:
a name, which matches itself only, or a suffix ``.example.com``, which matches every
name below it and not ``example.com`` itself. The Actions service's names change
(``pipelinesghubeus<N>``, ``run-actions-<N>-...``, storage accounts), so it needs
suffixes; an ``authorized_keys`` ``permitopen`` takes literal names only, so the
allowlist lives here, where no co-user of h100's account can change it.

Anything but ``CONNECT <allowed name>:443`` gets 403. The proxy resolves the name itself
and connects only to global addresses: a name that resolves to none gets 403, a
connection that fails gets 502. It answers 200 only once its connection is open. The
journal gets a line when a connection is refused, and two when it is passed: when it
opens and when it closes.
"""
from __future__ import annotations

import argparse
import asyncio
import ipaddress
import socket
import sys
from collections.abc import Callable, Collection

from ci_runners.inventory import NAME

MAX_HEAD = 8192        # bytes of request head
HEAD_TIMEOUT = 30      # s to send the head
CONNECT_TIMEOUT = 30   # s to resolve the name and open the connection, all addresses together
MAX_CONNECTIONS = 256
LINGER = 300           # s the other direction may run on after one side has finished
PORT = 443
CHUNK = 1 << 16


class Refused(Exception):
    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status


def _entry_ok(entry: str) -> bool:
    """An allowlist entry: a DNS name (two labels or more, not all digits), or ``.`` and
    such a name. Pure."""
    name = entry[1:] if entry.startswith('.') else entry
    return bool(NAME.match(name)) and not name.replace('.', '').isdigit()


def allowed(host: str, allow: Collection[str]) -> bool:
    """Whether ``host`` is on ``allow``: an exact entry, or a suffix entry (``.x.y``) that
    it lies strictly below. Pure."""
    if host in allow:
        return True
    return any(e.startswith('.') and host.endswith(e) and len(host) > len(e) for e in allow)


def load_allow(text: str) -> frozenset[str]:
    """The allowlist file: one entry per line, ``#`` comments, blank lines skipped.
    Raises ValueError on a bad entry or an empty list. Pure."""
    out = set()
    for n, line in enumerate(text.splitlines(), 1):
        entry = line.split('#', 1)[0].strip().lower()
        if not entry:
            continue
        if not _entry_ok(entry):
            raise ValueError(f'allowlist line {n}: {entry[:80]!r} is not a name or a .suffix')
        out.add(entry)
    if not out:
        raise ValueError('the allowlist is empty')
    return frozenset(out)


def target(head: bytes, allow: Collection[str]) -> str:
    """The allowed name a request head asks for. Raises Refused. Pure.

    Only ``CONNECT <name>:443 HTTP/1.x`` passes, with ``<name>``, lower-cased, a DNS name
    (no address literal, no trailing dot, no user part) on ``allow``."""
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
    if not NAME.match(host) or host.replace('.', '').isdigit() or not allowed(host, allow):
        raise Refused(403, f'{host[:80]!r} is not on the allowlist')
    return host


def global_addresses(infos: list) -> list[str]:
    """The global addresses among ``getaddrinfo`` results, in order, once each. Pure."""
    out = []
    for *_, sockaddr in infos:
        a = sockaddr[0]
        try:
            if ipaddress.ip_address(a).is_global and a not in out:
                out.append(a)
        except ValueError:
            continue
    return out


async def _resolve(host: str) -> list:
    # absolute: a resolver's search list never extends an allowed name into another one
    return await asyncio.get_running_loop().getaddrinfo(host + '.', PORT, type=socket.SOCK_STREAM)


async def _open(host: str, resolve: Callable = _resolve) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """A connection to ``host:443`` on a global address. Raises Refused: 403 when the name
    resolves to no global address, 502 when it does not resolve or no address answers."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + CONNECT_TIMEOUT
    try:
        infos = await asyncio.wait_for(resolve(host), CONNECT_TIMEOUT)
    except (OSError, asyncio.TimeoutError):
        raise Refused(502, f'{host} does not resolve') from None
    addrs = global_addresses(infos)
    if not addrs:
        raise Refused(403, f'{host} resolves to no global address')
    for a in addrs:
        left = deadline - loop.time()
        if left <= 0:
            break
        try:
            return await asyncio.wait_for(asyncio.open_connection(a, PORT), left)
        except (OSError, asyncio.TimeoutError):
            continue
    raise Refused(502, f'{host}:{PORT} does not answer')


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
    """The proxy. ``opener`` opens the connection to an allowed name (``_open``; tests
    give another)."""

    def __init__(self, allow: Collection[str], log=None, opener: Callable | None = None) -> None:
        self.allow = frozenset(allow)
        self.opener = opener or _open
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
                ur, uw = await self.opener(host)
            except Refused as e:
                await self._answer(w, e.status, str(e))
                return
            except Exception as e:  # never leave a client without an answer
                await self._answer(w, 502, f'{type(e).__name__}')
                return
            self.log(f'egress: {host}:{PORT} open')
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
            self.log(f'egress: {host}:{PORT} closed, {done} bytes down' if done >= 0 else
                     f'egress: {host}:{PORT} closed after {LINGER} s with one side open')

    async def _answer(self, w: asyncio.StreamWriter, status: int, reason: str) -> None:
        self.log(f'egress: refused {status}: {reason}')
        try:
            w.write(f'HTTP/1.1 {status} {_PHRASE.get(status, "Refused")}\r\nContent-Length: 0\r\n'
                    'Connection: close\r\n\r\n'.encode())
            await w.drain()
        except OSError:
            pass
        w.close()


_PHRASE = {400: 'Bad Request', 403: 'Forbidden', 408: 'Request Timeout', 431: 'Request Header Fields Too Large',
           502: 'Bad Gateway', 503: 'Service Unavailable'}


async def serve(port: int, allow: Collection[str]) -> None:
    bridge = Bridge(allow)
    server = await asyncio.start_server(bridge.handle, '127.0.0.1', port, limit=MAX_HEAD)
    print(f'egress: listening on 127.0.0.1:{port}, {len(bridge.allow)} entries', file=sys.stderr, flush=True)
    async with server:
        await server.serve_forever()


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog='ci-run egress')
    p.add_argument('--port', type=int, required=True, help='the port on 127.0.0.1')
    p.add_argument('--allow', required=True, help='the allowlist file')
    a = p.parse_args(argv)
    if not 0 < a.port < 65536:
        print('ci-run egress: --port must be 1 to 65535', file=sys.stderr)
        return 2
    try:
        with open(a.allow, encoding='utf-8') as f:
            allow = load_allow(f.read())
    except (OSError, UnicodeDecodeError, ValueError) as e:
        print(f'ci-run egress: {a.allow}: {e}', file=sys.stderr)
        return 2
    asyncio.run(serve(a.port, allow))
    return 0
