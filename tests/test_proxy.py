"""The CI proxy check's state machine (design §5.3: switch node after 3 failures)."""
from __future__ import annotations

import http.server
import socket
import threading
import unittest

from ci_runners import proxy

NODES = ['n1', 'n2', 'n3']


class Step(unittest.TestCase):
    def test_three_failures_switch_to_the_next_node(self):
        s = {}
        for _ in range(2):
            s, sw = proxy.step(s, False, NODES)
            self.assertIsNone(sw)
        s, sw = proxy.step(s, False, NODES)
        self.assertEqual((s, sw), ({'node': 'n2', 'failures': 0}, 'n2'))
        s, _ = proxy.step({'node': 'n3', 'failures': 2}, False, NODES)
        self.assertEqual(s['node'], 'n1')  # wraps around

    def test_a_success_resets(self):
        self.assertEqual(proxy.step({'node': 'n2', 'failures': 2}, True, NODES), ({'node': 'n2', 'failures': 0}, None))
        self.assertEqual(proxy.step({'node': 'gone', 'failures': 9}, True, NODES)[0]['node'], 'n1')


class Probe(unittest.TestCase):
    """``probe`` against a local stand-in for the proxy (plain HTTP: the proxy gets the
    absolute URL)."""

    def serve(self, status):
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(status)
                self.end_headers()

            def log_message(self, *_a):
                pass
        srv = http.server.HTTPServer(('127.0.0.1', 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv.server_address[1]

    def test_any_http_answer_means_the_proxy_works(self):
        for status in (200, 403, 429):
            self.assertTrue(proxy.probe(self.serve(status), timeout=5, url='http://example.invalid/zen'), status)

    def test_no_answer_means_it_does_not(self):
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            port = s.getsockname()[1]  # bound, not listening: refused
            self.assertFalse(proxy.probe(port, timeout=5, url='http://example.invalid/zen'))


if __name__ == '__main__':
    unittest.main()
