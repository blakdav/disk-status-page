import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import app


class CollectorTests(unittest.TestCase):
    def test_sleeping_disk_never_reads_smart(self):
        with patch('app.device_for', return_value='/dev/test'), patch('app.command', return_value=(0, 'drive state is: standby')) as run:
            disk = app.collect({'serial': 'test'}, {'smart_at': 1, 'smart': {'passed': True}}, 10000)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(disk['state'], 'standby')
        self.assertEqual(disk['smart_at'], 1)

    def test_unknown_state_never_reads_smart(self):
        with patch('app.device_for', return_value='/dev/test'), patch('app.command', return_value=(0, 'unsupported')) as run:
            disk = app.collect({'serial': 'test'}, {}, 10000)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(disk['state'], 'unknown')

    def test_race_to_standby_retains_cached_smart(self):
        with patch('app.device_for', return_value='/dev/test'), patch('app.command', side_effect=[(0, 'drive state is: active/idle'), (3, '{}')]) as run:
            disk = app.collect({'serial': 'test'}, {'smart_at': 1}, 10000)
        self.assertIn('standby,3,5', run.call_args.args[0])
        self.assertEqual(disk['state'], 'standby')
        self.assertEqual(disk['smart_at'], 1)

    def test_smart_failure_bits_preserve_health_result(self):
        result = app.summarize_smart({'smart_status': {'passed': False}}, 8)
        self.assertFalse(result['passed'])
        self.assertTrue(result['warnings'])

    def test_history_survives_reopen_and_does_not_infer_gap(self):
        with tempfile.TemporaryDirectory() as directory, patch('app.DB', Path(directory) / 'test.sqlite3'):
            app.initialize()
            import time
            now = time.time()
            first = {'serial': 'test', 'state': 'standby', 'observed_at': now}
            app.save(first, {})
            second = {**first, 'state': 'awake', 'observed_at': now + 60}
            app.save(second, first)
            self.assertEqual(app.stored()['test']['last_up'], now + 60)
            gap = {**second, 'state': 'standby', 'observed_at': now + 10000}
            app.save(gap, second)
            self.assertNotIn('last_down', app.stored()['test'])
            self.assertEqual(len(app.snapshot()['events']), 1)


class HttpTests(unittest.TestCase):
    def test_static_and_api_responses_without_network(self):
        import io
        from http.server import BaseHTTPRequestHandler
        import json
        for path in ['/', '/app.js', '/style.css', '/api/status', '/healthz', '/missing']:
            handler = object.__new__(app.Handler)
            handler.path = path
            handler.request_version = 'HTTP/1.1'
            handler.command = 'GET'
            handler.requestline = 'GET ' + path + ' HTTP/1.1'
            handler.wfile = io.BytesIO()
            payload = {'server_time': 100, 'disks': [{'observed_at': 100}]}
            with patch('app.snapshot', return_value=payload):
                handler.do_GET()
            response = handler.wfile.getvalue()
            self.assertIn(b'404' if path == '/missing' else b'200', response.split(b'\r\n')[0])
            if path == '/api/status':
                self.assertEqual(json.loads(response.split(b'\r\n\r\n', 1)[1]), payload)

    def test_unsupported_smart_power_check_keeps_cache(self):
        with patch('app.device_for', return_value='/dev/test'), patch('app.command', side_effect=[(0, 'drive state is: active/idle'), (5, '{}')]):
            disk = app.collect({'serial': 'test'}, {'smart_at': 1}, 10000)
        self.assertEqual(disk['smart_at'], 1)
        self.assertTrue(disk['error'])

if __name__ == '__main__':
    unittest.main()
