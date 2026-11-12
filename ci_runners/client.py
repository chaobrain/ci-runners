"""One keep-alive HTTPS connection per host, through the host's ``https_proxy`` (issue #242).

``urllib`` opens a new connection, and through the egress a new tunnel and TLS handshake, for
every request: about 0.25 s to the API and about 1 s to the artifact storage, per request.
A ``Client`` keeps one connection per host and reuses it, so a verdict that lists the run's
artifacts, fetches each part's zip and posts its status pays each handshake once.

``Client.api`` is ``github.api`` over the kept connection: the same headers, 30 s per call,
3 tries, no retry of a refusal (HTTP < 500 but 429), the same errors. ``Client.artifact``
fetches an artifact's zip: the API answers with a redirect to the storage's signed URL, which
gets no token; at most ``limit`` bytes are read, and a larger body is refused. A connection
the server closed between two requests is opened again once, as a browser does.
"""
from __future__ import annotations

import base64
import http.client
import json
import os
import ssl
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping

from ci_runners import github

#: the largest JSON answer read (a page of 100 artifacts is about 60 KB)
MAX_JSON = 4 << 20
HEADERS = {'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28',
           'User-Agent': 'ci-runners'}


class TooLarge(ValueError):
    pass


class Client:
    """Keep-alive connections, one per ``(host, port)``. Not thread-safe: one per thread."""

    def __init__(self, env: Mapping[str, str] | None = None, timeout: float = github.CALL_BOUND,
                 tries: int = github.TRIES, pause: float = 5, connect: Callable | None = None):
        env = os.environ if env is None else env
        # as urllib reads them: the lowercase name first
        self._proxy = env.get('https_proxy') or env.get('HTTPS_PROXY') or ''
        self._no_proxy = env.get('no_proxy') or env.get('NO_PROXY') or ''
        self._timeout, self._tries, self._pause = timeout, tries, pause
        self._connect = connect or self._open
        self._conns: dict[tuple[str, int], http.client.HTTPConnection] = {}
        self.opened = 0  # connections opened: what the reuse saves shows here

    def close(self) -> None:
        for c in self._conns.values():
            c.close()
        self._conns.clear()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _open(self, host: str, port: int) -> http.client.HTTPConnection:
        ctx = ssl.create_default_context()
        proxy = self._proxy
        if proxy and not urllib.request.proxy_bypass_environment(host, {'no': self._no_proxy}):
            p = urllib.parse.urlsplit(proxy if '://' in proxy else f'http://{proxy}')
            if p.scheme != 'http' or not p.hostname:
                raise OSError(f'https_proxy {p.scheme}://…: only an http:// proxy is supported')
            c = http.client.HTTPSConnection(p.hostname, p.port or 80, timeout=self._timeout, context=ctx)
            tunnel = {}
            if p.username is not None:
                cred = f'{urllib.parse.unquote(p.username)}:{urllib.parse.unquote(p.password or "")}'
                tunnel['Proxy-Authorization'] = 'Basic ' + base64.b64encode(cred.encode()).decode()
            c.set_tunnel(host, port, tunnel)
            return c
        return http.client.HTTPSConnection(host, port, timeout=self._timeout, context=ctx)

    def _once(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None,
              limit: int) -> tuple[int, Mapping[str, str], bytes]:
        """One request: its status, headers and at most ``limit`` bytes of its body."""
        u = urllib.parse.urlsplit(url)
        if u.scheme != 'https' or not u.hostname:
            raise _Refused(f'{u.scheme}://{u.hostname}: not an https URL')
        key = (u.hostname, u.port or 443)
        path = (u.path or '/') + (f'?{u.query}' if u.query else '')
        for fresh in (False, True):
            c = self._conns.get(key)
            reused = c is not None
            if c is None:
                c = self._conns[key] = self._connect(*key)
                self.opened += 1
            try:
                c.request(method, path, body=body, headers=dict(headers))
                r = c.getresponse()
            except (http.client.RemoteDisconnected, http.client.BadStatusLine, ConnectionResetError,
                    BrokenPipeError) as e:
                self._drop(key)
                if reused and not fresh:
                    continue  # the server closed the kept connection: once on a new one
                raise OSError(type(e).__name__) from None
            except BaseException:
                self._drop(key)
                raise
            try:
                size = r.getheader('Content-Length')
                if size is not None and size.isdigit() and int(size) > limit:
                    raise TooLarge(f'{url.split("?")[0]}: larger than {limit} bytes')
                data = r.read(limit + 1)
                if len(data) > limit:
                    raise TooLarge(f'{url.split("?")[0]}: larger than {limit} bytes')
                if not r.isclosed():
                    r.read()  # the rest (nothing): the connection can be reused
            except BaseException:
                self._drop(key)  # a body left unread poisons the connection
                raise
            if r.will_close:
                self._drop(key)
            return r.status, {k.lower(): v for k, v in r.getheaders()}, data
        raise AssertionError('unreachable')

    def _drop(self, key: tuple[str, int]) -> None:
        c = self._conns.pop(key, None)
        if c is not None:
            c.close()

    def _tried(self, what: str, attempt: Callable[[], object]) -> object:
        """``attempt()`` up to ``tries`` times, ``pause`` apart; a refusal (raised as
        ``_Refused``) is not tried again."""
        last = ''
        for i in range(self._tries):
            try:
                return attempt()
            except _Refused as e:
                last = str(e)
                break
            except _Status as e:
                last = str(e)
            except TooLarge:
                raise
            except (OSError, ValueError, http.client.HTTPException) as e:
                last = type(e).__name__
            if i + 1 < self._tries:
                time.sleep(self._pause)
        raise github.ApiError(f'{what}: {last}')

    def api(self, method: str, url: str, token: str, body: dict | None = None) -> object:
        """``github.api`` over the kept connection: the answer's JSON."""
        data = json.dumps(body).encode() if body is not None else None
        headers = {**HEADERS, 'Authorization': f'Bearer {token}'}
        if data is not None:
            headers['Content-Type'] = 'application/json'

        def attempt() -> object:
            status, _, got = self._once(method, url, headers, data, MAX_JSON)
            _check(status)
            return json.loads(got or b'null')
        return self._tried(f'{method} {url.split("?")[0]}', attempt)

    def artifact(self, url: str, token: str, limit: int) -> bytes:
        """An artifact's zip, at most ``limit`` bytes: ``url`` is the API's
        ``/actions/artifacts/<id>/zip``; its redirect is followed without the token."""
        headers = {**HEADERS, 'Authorization': f'Bearer {token}'}

        def attempt() -> bytes:
            status, got_headers, data = self._once('GET', url, headers, None, limit)
            if status == 200:  # an API that answers with the zip itself
                return data
            if status not in REDIRECTS:
                _check(status)
                raise _Status(f'HTTP {status}')
            where = got_headers.get('location', '')
            if urllib.parse.urlsplit(where).scheme != 'https':
                raise _Refused('the redirect is not to an https URL')
            status, _, data = self._once('GET', where, {'User-Agent': HEADERS['User-Agent']}, None, limit)
            if status != 200:
                _check(status)
                raise _Status(f'storage HTTP {status}')
            return data
        return self._tried(f'GET {url}', attempt)


class _Refused(Exception):
    pass


class _Status(Exception):
    pass


REDIRECTS = frozenset({301, 302, 303, 307, 308})


def _check(status: int) -> None:
    """Nothing for a 2xx; else the error: a refusal (HTTP < 500 but 429) is not tried
    again."""
    if 200 <= status < 300:
        return
    if status < 500 and status != 429:
        raise _Refused(f'HTTP {status}')
    raise _Status(f'HTTP {status}')
