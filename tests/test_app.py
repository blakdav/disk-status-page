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

class ActionTests(unittest.TestCase):
    def test_refresh_does_not_wake_sleeping_disks(self):
        with patch('app.device_for', return_value='/dev/test'), patch('app.command', return_value=(0, 'drive state is: standby')) as run:
            disk = app.collect({'serial': 'test'}, {}, 10000, force_smart=True)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(disk['state'], 'standby')

    def test_duplicate_actions_and_unknown_serial_are_rejected(self):
        with patch('app.CONFIGS', [{'serial': 'known'}]), patch('app.JOB', None), patch('app.JOBS'):
            with self.assertRaises(ValueError):
                app.submit_job('spin-up', 'unknown')
            app.submit_job('spin-up', 'known')
            with self.assertRaises(ValueError):
                app.submit_job('spin-down', 'known')

    def test_mount_guard_checks_partitions_and_fails_without_host_info(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            disk = root / 'sdb'
            part = disk / 'sdb1'
            part.mkdir(parents=True)
            for node, dev in [(disk, '8:16'), (part, '8:17')]:
                (node / 'dev').write_text(dev)
                (node / 'holders').mkdir()
            (part / 'partition').write_text('1')
            mounts = root / 'mountinfo'
            swaps = root / 'swaps'
            swaps.write_text('Filename Type Size Used Priority\n')
            mounts.write_text('1 0 8:17 / /mnt/data rw - xfs /dev/sdb1 rw\n')
            original = Path
            def paths(value):
                return {'/sys/class/block': root,
                        '/host/mountinfo': mounts, '/host/swaps': swaps}.get(value, original(value))
            with patch('app.Path', side_effect=paths):
                with self.assertRaisesRegex(ValueError, 'mounted'):
                    app.verify_unmounted({'allow_spindown': True}, '/dev/sdb')
                mounts.write_text('1 0 8:1 / / rw - ext4 /dev/sda1 rw\n')
                app.verify_unmounted({'allow_spindown': True}, '/dev/sdb')
                (part / 'holders' / 'dm-0').touch()
                with self.assertRaisesRegex(ValueError, 'holders'):
                    app.verify_unmounted({'allow_spindown': True}, '/dev/sdb')
                (part / 'holders' / 'dm-0').unlink()
                mounts.unlink()
                with self.assertRaises(FileNotFoundError):
                    app.verify_unmounted({'allow_spindown': True}, '/dev/sdb')

    def test_pool_disk_cannot_be_spun_down(self):
        with self.assertRaisesRegex(ValueError, 'spare'):
            app.verify_unmounted({'allow_spindown': False}, '/dev/test')

    def test_missing_confirmation_does_not_wake_disk(self):
        config = {'serial': 'test'}
        before = {'serial': 'test', 'state': 'standby'}
        with patch('app.CONFIGS', [config]), patch('app.device_for', return_value='/dev/test'), patch('app.stored', return_value={}), patch('app.collect', return_value=before), patch('app.save'), patch('app.wake_disk') as wake:
            with self.assertRaisesRegex(ValueError, 'confirm'):
                app.execute_job({'action': 'health', 'serial': 'test', 'wake': False})
            wake.assert_not_called()

    def test_running_self_test_blocks_standby(self):
        config = {'serial': 'test', 'allow_spindown': True}
        before = {'serial': 'test', 'state': 'awake'}
        data = {'ata_smart_data': {'self_test': {'status': {'value': 249}}}}
        with patch('app.CONFIGS', [config]), patch('app.device_for', return_value='/dev/test'), patch('app.stored', return_value={}), patch('app.collect', return_value=before), patch('app.save'), patch('app.verify_unmounted'), patch('app.smart_probe', return_value=data), patch('app.command') as command:
            with self.assertRaisesRegex(ValueError, 'running'):
                app.execute_job({'action': 'spin-down', 'serial': 'test'})
            command.assert_not_called()

    def test_spin_up_updates_observed_state_and_history(self):
        import time
        now = time.time()
        config = {'serial': 'test', 'label': 'test'}
        before = {**config, 'state': 'standby', 'observed_at': now}
        after = {**config, 'state': 'awake', 'observed_at': now + 1}
        with tempfile.TemporaryDirectory() as directory, patch('app.DB', Path(directory) / 'db'):
            app.initialize()
            with patch('app.CONFIGS', [config]), patch('app.device_for', return_value='/dev/test'), patch('app.collect', side_effect=[before, after]), patch('app.wake_disk') as wake:
                app.execute_job({'action': 'spin-up', 'serial': 'test'})
            wake.assert_called_once_with('/dev/test')
            self.assertEqual(app.stored()['test']['state'], 'awake')
            self.assertEqual(app.snapshot()['events'][0]['state'], 'awake')

    def test_extended_test_uses_standby_guard_and_no_force_flag(self):
        config = {'serial': 'test'}
        disk = {'serial': 'test', 'state': 'awake'}
        data = {'ata_smart_data': {'self_test': {'status': {'value': 0}}}}
        with patch('app.CONFIGS', [config]), patch('app.device_for', return_value='/dev/test'), patch('app.stored', return_value={}), patch('app.collect', return_value=disk), patch('app.save'), patch('app.smart_probe', return_value=data), patch('app.command', return_value=(0, '{}')) as command:
            message = app.execute_job({'action': 'test', 'serial': 'test', 'wake': False})
        args = command.call_args.args[0]
        self.assertIn('standby,3,5', args)
        self.assertIn('long', args)
        self.assertNotIn('-t force', ' '.join(args))
        self.assertIn('background', message)

    def test_action_authentication_and_cross_origin_rejection(self):
        import io
        import json
        from email.message import Message
        for token, origin, expected in [('bad', 'https://nas.test', 403),
                                         ('secret', 'https://evil.test', 403),
                                         ('secret', 'https://nas.test', 202)]:
            handler = object.__new__(app.Handler)
            handler.path = '/api/action'
            handler.request_version = 'HTTP/1.1'
            handler.command = 'POST'
            handler.requestline = 'POST /api/action HTTP/1.1'
            handler.wfile = io.BytesIO()
            raw = json.dumps({'action': 'refresh'}).encode()
            handler.rfile = io.BytesIO(raw)
            handler.headers = Message()
            for key, value in {'Host': 'nas.test', 'Origin': origin,
                               'X-Action-Token': token, 'Content-Type': 'application/json',
                               'Content-Length': str(len(raw))}.items():
                handler.headers[key] = value
            with patch('app.ACTION_TOKEN', 'secret'), patch('app.DEMO', False), patch('app.submit_job', return_value={'state': 'queued'}) as submit:
                handler.do_POST()
                self.assertEqual(submit.call_count, int(expected == 202))
            self.assertIn(str(expected).encode(), handler.wfile.getvalue().split(b'\r\n')[0])

    def test_action_token_persists_with_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory, patch('app.DB', Path(directory) / 'history.sqlite3'):
            token = app.load_token()
            self.assertEqual(token, app.load_token())
            self.assertEqual((Path(directory) / 'action-token').stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
