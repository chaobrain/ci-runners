"""The kept connections (issue #242): ci_runners/client.py against fake connections."""
from __future__ import annotations

import base64
import http.client
import json
import unittest
from unittest import mock

from ci_runners import client, github


class Resp:
    def __init__(self, status, body=b'', headers=None, close=False):
        self.status, self._body, self._closed = status, body, False
        self._headers = {'Content-Length': str(len(body)), **(headers or {})}
        self.will_close = close

    def getheader(self, name, default=None):
        return {k.lower(): v for k, v in self._headers.items()}.get(name.lower(), default)

    def getheaders(self):
        return list(self._headers.items())

    def read(self, n=None):
        out = self._body if n is None else self._body[:n]
        self._body = self._body[len(out):]
        self._closed = not self._body
        return out

    def isclosed(self):
        return self._closed


class Conn:
    def __init__(self, net, host, port):
        self.net, self.host, self.port, self.closed = net, host, port, False

    def request(self, method, path, body=None, headers=None):
        self.net.requests.append((self.host, method, path, dict(headers or {}), body, self))
        got = self.net.answers[self.host].pop(0)
        if isinstance(got, BaseException):
            raise got
        self.pending = got

    def getresponse(self):
        return self.pending

    def close(self):
        self.closed = True


class Net:
    """Answers by host, in order; each connection made is kept in ``made``."""

    def __init__(self, **answers):
        self.answers = {h.replace('_', '.'): list(a) for h, a in answers.items()}
        self.requests, self.made = [], []

    def connect(self, host, port):
        c = Conn(self, host, port)
        self.made.append(c)
        return c


def zip_redirect(where='https://store.example/blob?sig=x'):
    return Resp(302, b'', {'Location': where})


class Kept(unittest.TestCase):
    def client(self, net, **kw):
        return client.Client(env={}, connect=net.connect, pause=0, **kw)

    def test_one_connection_per_host_for_every_request(self):
        net = Net(api_example=[Resp(200, b'{"artifacts": []}'), zip_redirect(), zip_redirect(), Resp(201, b'{}')],
                  store_example=[Resp(200, b'zip1'), Resp(200, b'zip2')])
        with self.client(net) as c:
            self.assertEqual(c.api('GET', 'https://api.example/repos/o/r/actions/runs/1/artifacts?page=1', 't'),
                             {'artifacts': []})
            self.assertEqual(c.artifact('https://api.example/repos/o/r/actions/artifacts/5/zip', 't', 10), b'zip1')
            self.assertEqual(c.artifact('https://api.example/repos/o/r/actions/artifacts/6/zip', 't', 10), b'zip2')
            self.assertEqual(c.api('POST', 'https://api.example/repos/o/r/statuses/x', 't', {'state': 'success'}), {})
            self.assertEqual(c.opened, 2)
        self.assertEqual([(c.host, c.port) for c in net.made], [('api.example', 443), ('store.example', 443)])
        self.assertTrue(all(c.closed for c in net.made))  # closed with the client
        paths = [(h, m, p) for h, m, p, *_ in net.requests]
        self.assertEqual(paths, [('api.example', 'GET', '/repos/o/r/actions/runs/1/artifacts?page=1'),
                                 ('api.example', 'GET', '/repos/o/r/actions/artifacts/5/zip'),
                                 ('store.example', 'GET', '/blob?sig=x'),
                                 ('api.example', 'GET', '/repos/o/r/actions/artifacts/6/zip'),
                                 ('store.example', 'GET', '/blob?sig=x'),
                                 ('api.example', 'POST', '/repos/o/r/statuses/x')])
        api_headers = net.requests[0][3]
        self.assertEqual(api_headers['Authorization'], 'Bearer t')
        self.assertEqual(api_headers['X-GitHub-Api-Version'], '2022-11-28')
        for host, _, _, headers, _, _ in net.requests:
            if host == 'store.example':
                self.assertNotIn('Authorization', headers)  # the signed URL gets no token
        self.assertEqual(json.loads(net.requests[-1][4]), {'state': 'success'})
        self.assertEqual(net.requests[-1][3]['Content-Type'], 'application/json')

    def test_a_kept_connection_the_server_closed_is_opened_again_once(self):
        net = Net(api_example=[Resp(200, b'{}'), http.client.RemoteDisconnected('gone'), Resp(200, b'[1]')])
        c = self.client(net, tries=1)  # not a try of its own: one try is enough
        c.api('GET', 'https://api.example/a', 't')
        self.assertEqual(c.api('GET', 'https://api.example/b', 't'), [1])
        self.assertEqual(c.opened, 2)
        self.assertTrue(net.made[0].closed)
        # on a new connection, the same end is a failed try, not a free one
        net = Net(api_example=[ConnectionResetError(), ConnectionResetError(), BrokenPipeError()])
        with self.assertRaisesRegex(github.ApiError, r'GET https://api.example/a: OSError'):
            self.client(net).api('GET', 'https://api.example/a', 't')
        self.assertEqual(len(net.made), 3)

    def test_a_connection_that_says_it_closes_is_not_kept(self):
        net = Net(api_example=[Resp(200, b'{}', close=True), Resp(200, b'{}')])
        c = self.client(net)
        c.api('GET', 'https://api.example/a', 't')
        c.api('GET', 'https://api.example/b', 't')
        self.assertEqual(c.opened, 2)

    def test_refusals_are_not_tried_again_and_5xx_are(self):
        net = Net(api_example=[Resp(403, b'{}'), Resp(502), Resp(429), Resp(200, b'{"ok": 1}')])
        c = self.client(net)
        with self.assertRaisesRegex(github.ApiError, r'\AGET https://api.example/a: HTTP 403\Z'):
            c.api('GET', 'https://api.example/a?x=1', 't')
        self.assertEqual(c.api('GET', 'https://api.example/a', 't'), {'ok': 1})  # 502, 429, then the answer
        net = Net(api_example=[Resp(500)] * 3)
        with mock.patch('time.sleep') as slept, self.assertRaisesRegex(github.ApiError, 'HTTP 500'):
            client.Client(env={}, connect=net.connect, pause=5).api('GET', 'https://api.example/a', 't')
        self.assertEqual([a.args for a in slept.call_args_list], [(5,), (5,)])  # 3 tries, 2 pauses
        net = Net(api_example=[Resp(302, b'', {'Location': 'https://x/'})])
        with self.assertRaisesRegex(github.ApiError, 'HTTP 302'):  # the API's JSON is never a redirect
            self.client(net).api('GET', 'https://api.example/a', 't')
        net = Net(api_example=[Resp(200, b'not json') for _ in range(3)])
        with self.assertRaisesRegex(github.ApiError, 'JSONDecodeError'):
            self.client(net).api('GET', 'https://api.example/a', 't')

    def test_the_zip_is_bounded_and_its_redirect_checked(self):
        unsized = Resp(200, b'12345678901')
        unsized._headers.pop('Content-Length')  # chunked: only the read finds it too large
        for answer in (unsized, Resp(200, b'1', {'Content-Length': '11'})):  # refused before the read
            net = Net(api_example=[zip_redirect()], store_example=[answer])
            with self.assertRaisesRegex(client.TooLarge, 'larger than 10 bytes'):
                self.client(net).artifact('https://api.example/z', 't', 10)
            self.assertTrue(net.made[1].closed)  # a body left unread: the connection goes
        self.assertEqual(answer._body, b'1')  # not read at all
        net = Net(api_example=[zip_redirect('http://store.example/blob')])
        with self.assertRaisesRegex(github.ApiError, 'not to an https URL'):
            self.client(net).artifact('https://api.example/z', 't', 10)
        self.assertEqual(len(net.requests), 1)  # refused at once, nothing fetched
        net = Net(api_example=[Resp(200, b'zip')])  # an API that answers with the zip itself
        self.assertEqual(self.client(net).artifact('https://api.example/z', 't', 10), b'zip')
        net = Net(api_example=[zip_redirect()] * 2, store_example=[Resp(404), Resp(503), Resp(200, b'z')])
        with self.assertRaisesRegex(github.ApiError, 'HTTP 404'):
            self.client(net).artifact('https://api.example/z', 't', 10)
        net = Net(api_example=[zip_redirect()] * 2, store_example=[Resp(503), Resp(200, b'z')])
        self.assertEqual(self.client(net).artifact('https://api.example/z', 't', 10), b'z')
        net = Net(api_example=[Resp(410)])
        with self.assertRaisesRegex(github.ApiError, 'HTTP 410'):
            self.client(net).artifact('https://api.example/z', 't', 10)
        net = Net(api_example=[Resp(304)] * 3)
        with self.assertRaisesRegex(github.ApiError, 'HTTP 304'):
            self.client(net).artifact('https://api.example/z', 't', 10)
        with self.assertRaisesRegex(github.ApiError, 'not an https URL'):
            self.client(Net()).api('GET', 'http://api.example/a', 't')

    def test_a_pool_keeps_a_client_per_thread(self):
        import threading
        made, lock = [], threading.Lock()

        class One:
            def __init__(self):
                with lock:
                    made.append(self)
                self.closed, self.urls = False, []

            def artifact(self, url, token, limit):
                self.urls.append(url)
                return url.encode()

            def close(self):
                self.closed = True
        pool = client.Pool(One)
        self.assertEqual(pool.artifact('https://a/1', 't', 9), b'https://a/1')
        self.assertEqual(pool.artifact('https://a/2', 't', 9), b'https://a/2')  # the same thread: the same client
        t = threading.Thread(target=pool.artifact, args=('https://a/3', 't', 9))
        t.start()
        t.join()
        self.assertEqual([c.urls for c in made], [['https://a/1', 'https://a/2'], ['https://a/3']])
        self.assertEqual(pool.clients, made)
        pool.close()
        self.assertTrue(all(c.closed for c in made))
        with mock.patch.object(client, 'Client', One):
            client.Pool().artifact('https://a/4', 't', 9)  # Client by default, looked up when made
        self.assertEqual(made[-1].urls, ['https://a/4'])

    def test_each_host_s_seconds_are_summed(self):
        net = Net(api_example=[Resp(200, b'{}'), zip_redirect()], store_example=[Resp(200, b'z')])
        c = self.client(net)
        with mock.patch('time.monotonic', side_effect=[0.0, 0.25, 1.0, 1.5, 2.0, 3.0]):
            c.api('GET', 'https://api.example/a', 't')
            c.artifact('https://api.example/z', 't', 10)
        self.assertEqual(c.seconds, {'api.example': 0.75, 'store.example': 1.0})

    def test_the_proxy_tunnels_and_no_proxy_bypasses_it(self):
        c = client.Client(env={'https_proxy': 'http://127.0.0.1:3128', 'HTTPS_PROXY': 'http://other:1',
                               'no_proxy': 'localhost,mirror.example'})
        conn = c._open('api.github.com', 443)
        self.assertEqual((conn.host, conn.port), ('127.0.0.1', 3128))
        self.assertEqual((conn._tunnel_host, conn._tunnel_port), ('api.github.com', 443))
        self.assertNotIn('Proxy-Authorization', conn._tunnel_headers)
        direct = c._open('mirror.example', 443)
        self.assertEqual((direct.host, direct.port, direct._tunnel_host), ('mirror.example', 443, None))
        c = client.Client(env={'HTTPS_PROXY': 'user:p%40ss@proxy.example:8080'})
        conn = c._open('api.github.com', 443)
        self.assertEqual((conn.host, conn.port), ('proxy.example', 8080))
        self.assertEqual(conn._tunnel_headers['Proxy-Authorization'],
                         'Basic ' + base64.b64encode(b'user:p@ss').decode())
        plain = client.Client(env={})._open('api.github.com', 443)
        self.assertEqual((plain.host, plain._tunnel_host), ('api.github.com', None))
        with self.assertRaisesRegex(OSError, 'only an http:// proxy'):
            client.Client(env={'https_proxy': 'socks5://127.0.0.1:1080'})._open('api.github.com', 443)


class Cause(unittest.TestCase):
    """Issue #250: a failed call's ``ApiError`` carries its short cause beside the whole."""

    def test_the_cause_of_each_end(self):
        url = 'https://api.example/repos/o/r/actions/runs/1/artifacts'
        for answers, cause in (([Resp(403, b'{}')], 'HTTP 403'), ([Resp(502, b'')] * 3, 'HTTP 502'),
                               ([TimeoutError()] * 3, 'TimeoutError')):
            c = client.Client(env={}, connect=Net(api_example=answers).connect, pause=0)
            with self.assertRaises(github.ApiError) as cm:
                c.api('GET', url + '?page=1', 't')
            self.assertEqual((cm.exception.cause, str(cm.exception)), (cause, f'GET {url}: {cause}'))
        net = Net(api_example=[zip_redirect()] * 3, store_example=[Resp(502, b'')] * 3)
        with self.assertRaises(github.ApiError) as cm:
            client.Client(env={}, connect=net.connect, pause=0).artifact(
                'https://api.example/repos/o/r/actions/artifacts/5/zip', 't', 10)
        self.assertEqual(cm.exception.cause, 'HTTP 502')  # the storage's answer, without its signed URL


if __name__ == '__main__':
    unittest.main()
