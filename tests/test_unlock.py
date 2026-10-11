import io
import json
import unittest
from email.message import Message
from unittest.mock import patch
import app


class UnlockTests(unittest.TestCase):
    def request(self, origin='https://nas.test', marker='DiskStatusPage', demo=False):
        handler = object.__new__(app.Handler)
        handler.path = '/api/unlock'
        handler.request_version = 'HTTP/1.1'
        handler.command = 'POST'
        handler.requestline = 'POST /api/unlock HTTP/1.1'
        handler.wfile = io.BytesIO()
        handler.rfile = io.BytesIO(b'{}')
        handler.headers = Message()
        for name, value in {'Host': 'nas.test', 'Origin': origin,
                            'Content-Type': 'application/json',
                            'Content-Length': '2', 'X-Requested-With': marker}.items():
            handler.headers[name] = value
        with patch('app.ACTION_TOKEN', 'internal-token'), patch('app.DEMO', demo):
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
