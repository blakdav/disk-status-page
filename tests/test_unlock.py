import io
import json
import unittest
from email.message import Message
from unittest.mock import patch
import app


class UnlockTests(unittest.TestCase):
    def request(self, origin='https://nas.test', marker='DiskStatusPage', demo=False,
                host='nas.test', allowed=('nas.test',)):
        handler = object.__new__(app.Handler)
        handler.path = '/api/unlock'
        handler.request_version = 'HTTP/1.1'
        handler.command = 'POST'
        handler.requestline = 'POST /api/unlock HTTP/1.1'
        handler.wfile = io.BytesIO()
        handler.rfile = io.BytesIO(b'{}')
        handler.headers = Message()
        for name, value in {'Host': host, 'Origin': origin,
                            'Content-Type': 'application/json',
                            'Content-Length': '2', 'X-Requested-With': marker}.items():
            handler.headers[name] = value
        with patch('app.ACTION_TOKEN', 'internal-token'), patch('app.DEMO', demo), \
                patch('app.ALLOWED_HOSTS', set(allowed)):
            handler.do_POST()
        headers, body = handler.wfile.getvalue().split(b'\r\n\r\n', 1)
        return headers, json.loads(body)

    def test_same_origin_button_unlocks_without_password(self):
        headers, body = self.request()
        self.assertIn(b'200', headers.split(b'\r\n')[0])
        self.assertIn(b'Cache-Control: no-store', headers)
        self.assertEqual(body['token'], 'internal-token')

    def test_cross_origin_cannot_unlock(self):
        headers, body = self.request(origin='https://other.test')
        self.assertIn(b'403', headers.split(b'\r\n')[0])
        self.assertNotIn('token', body)

    def test_simple_form_cannot_unlock(self):
        headers, body = self.request(marker='')
        self.assertIn(b'403', headers.split(b'\r\n')[0])

    def test_demo_cannot_unlock(self):
        headers, body = self.request(demo=True)
        self.assertIn(b'403', headers.split(b'\r\n')[0])

    def test_dns_rebinding_cannot_unlock(self):
        # A rebound page sends matching Origin and Host, both its own name.
        headers, body = self.request(origin='http://evil.test:8088', host='evil.test:8088')
        self.assertIn(b'403', headers.split(b'\r\n')[0])
        self.assertNotIn('token', body)

    def test_allowed_host_with_port_unlocks(self):
        headers, body = self.request(origin='http://nas.test:8088', host='nas.test:8088')
        self.assertIn(b'200', headers.split(b'\r\n')[0])

    def test_no_allowed_hosts_cannot_unlock(self):
        headers, body = self.request(allowed=())
        self.assertIn(b'403', headers.split(b'\r\n')[0])

    def test_host_header_parsing(self):
        with patch('app.ALLOWED_HOSTS', {'172.23.24.16', 'nas.test', '::1'}):
            for header, expected in [('172.23.24.16:8088', True), ('NAS.test', True),
                                     ('[::1]:8088', True), ('evil.test', False),
                                     ('evil@nas.test', False), ('nas.test/x', False),
                                     ('', False), (None, False)]:
                self.assertEqual(app.host_allowed(header), expected, header)
