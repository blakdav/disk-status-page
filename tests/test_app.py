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
        with patch('app.CONFIGS', [config]), patch('app.device_for', return_value='/dev/test'), patch('app.stored', return_value={}), patch('app.collect', return_value=disk), patch('app.save'), patch('app.smart_probe', return_value=data), patch('app.record_extended_start'), patch('app.command', return_value=(0, '{}')) as command:
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


class HomepageAndAcknowledgementTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.patch = patch('app.DB', Path(self.directory.name) / 'history.sqlite3')
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.directory.cleanup)
        app.initialize()

    def disk(self, count=200, serial='test', status=0, extra=None):
        import time
        data = {'smart_status': {'passed': True}, 'ata_smart_attributes': {'table': [
            {'id': 5, 'name': 'Reallocated_Sector_Ct', 'raw': {'value': count, 'string': str(count)}}
        ]}}
        if extra:
            data['ata_smart_attributes']['table'].append(extra)
        disk = {'serial': serial, 'label': serial, 'state': 'standby',
                'observed_at': time.time(), 'smart': app.summarize_smart(data, status),
                'smart_at': time.time()}
        app.save(disk, {})
        return disk

    def test_baseline_survives_reinitialization_and_increase_realerts(self):
        self.disk()
        app.acknowledge_warning('test', 5, 200)
        app.initialize()
        result = app.homepage_status(app.snapshot())
        self.assertEqual(result['summary']['attention'], 0)
        self.assertEqual(result['summary']['known_issues'], 1)
        self.assertEqual(result['disks'][0]['health'], 'Known issue')
        self.disk(201)
        result = app.homepage_status(app.snapshot())
        self.assertEqual(result['summary']['attention'], 1)
        issue = app.snapshot()['disks'][0]['smart']['issues'][0]
        self.assertFalse(issue['acknowledged'])
        self.assertEqual(issue['increased_by'], 1)
        self.assertEqual(issue['baseline'], 200)

    def test_acknowledgement_is_per_serial_and_can_be_undone(self):
        self.disk(serial='first')
        self.disk(serial='second')
        app.acknowledge_warning('first', 5, 200)
        self.assertEqual(app.homepage_status(app.snapshot())['summary']['attention'], 1)
        app.acknowledge_warning('first', 5, None, clear=True)
        self.assertEqual(app.homepage_status(app.snapshot())['summary']['attention'], 2)

    def test_new_warning_and_smart_threshold_failure_are_not_hidden(self):
        self.disk()
        app.acknowledge_warning('test', 5, 200)
        self.disk(status=16, extra={'id': 197, 'name': 'Current_Pending_Sector', 'raw': {'value': 1}})
        result = app.snapshot()['disks'][0]
        self.assertTrue(result['needs_attention'])
        self.assertEqual(len(result['smart']['known_warnings']), 1)
        self.assertEqual(len(result['smart']['active_warnings']), 2)
        with self.assertRaises(ValueError):
            app.acknowledge_warning('test', 197, 1)

    def test_changed_count_cannot_be_acknowledged_with_old_browser_value(self):
        self.disk(201)
        with self.assertRaisesRegex(ValueError, 'changed'):
            app.acknowledge_warning('test', 5, 200)

    def test_acknowledgement_and_homepage_reads_do_not_issue_commands(self):
        self.disk()
        with patch('app.command') as command:
            app.execute_job({'action': 'acknowledge', 'serial': 'test', 'attribute_id': 5, 'expected_raw': 200})
            result = app.homepage_status(app.snapshot())
            command.assert_not_called()
        self.assertEqual(result['summary']['sleeping'], 1)
        self.assertEqual(result['summary']['attention'], 0)

    def test_stale_or_missing_state_still_needs_attention(self):
        disk = self.disk()
        app.acknowledge_warning('test', 5, 200)
        disk['observed_at'] = 1
        app.save(disk, {})
        result = app.homepage_status(app.snapshot())
        self.assertEqual(result['summary']['attention'], 1)
        self.assertEqual(result['summary']['unknown'], 1)
        self.assertEqual(result['summary']['sleeping'], 0)
        self.assertEqual(result['disks'][0]['state'], 'stale')

    def test_never_read_health_is_unknown_not_a_failed_disk(self):
        import time
        app.save({'serial': 'test', 'label': 'test', 'state': 'standby', 'observed_at': time.time()}, {})
        result = app.homepage_status(app.snapshot())
        self.assertEqual(result['summary']['health_unknown'], 1)
        self.assertEqual(result['summary']['attention'], 0)
        self.assertEqual(result['disks'][0]['health'], 'Health unknown')

    def test_self_test_failure_and_overall_failure_cannot_be_acknowledged_away(self):
        disk = self.disk()
        app.acknowledge_warning('test', 5, 200)
        disk['smart']['passed'] = False
        disk['smart']['issues'].append({'key': 'self-test', 'message': 'Self-test failed'})
        app.save(disk, {})
        self.assertTrue(app.snapshot()['disks'][0]['needs_attention'])

    def test_inventory_order_stays_fixed_during_individual_disk_updates(self):
        import time
        configs = [{'serial': 'z', 'label': 'data1'},
                   {'serial': 'a', 'label': 'data2'},
                   {'serial': 'p', 'label': 'parity1'},
                   {'serial': 'm', 'label': 'data6'}]
        with patch('app.CONFIGS', configs):
            # Simulate partial first collection, out-of-order inserts and
            # single-disk refreshes observed between full collector passes.
            for config in [configs[3], configs[0], configs[2], configs[1], configs[3], configs[0]]:
                app.save({**config, 'state': 'awake', 'observed_at': time.time()}, {})
                payload = app.snapshot()
                self.assertEqual([d['label'] for d in payload['disks']],
                                 ['data1', 'data2', 'parity1', 'data6'])
                self.assertEqual([d['label'] for d in app.homepage_status(payload)['disks']],
                                 ['data1', 'data2', 'parity1', 'data6'])

    def test_homepage_markers_keep_power_health_and_testing_separate(self):
        import time
        disk = self.disk()
        app.acknowledge_warning('test', 5, 200)
        description = app.homepage_status(app.snapshot())['disks'][0]['description']
        self.assertIn('🔵 Sleeping', description)
        self.assertIn('🟡 Known issue', description)
        disk.update(state='awake', smart_at=time.time())
        disk['smart']['test_status'] = {'value': 249}
        app.save(disk, {})
        description = app.homepage_status(app.snapshot())['disks'][0]['description']
        self.assertIn('🟢 Awake', description)
        self.assertIn('🟣 Testing', description)
        disk['state'] = 'standby'
        app.save(disk, {})
        description = app.homepage_status(app.snapshot())['disks'][0]['description']
        self.assertIn('⚪ Last read: testing', description)
        self.assertNotIn('🟣 Testing', description)

    def test_homepage_endpoint_returns_native_panel_shape(self):
        import io, json
        self.disk()
        handler = object.__new__(app.Handler)
        handler.path = '/api/homepage'
        handler.request_version = 'HTTP/1.1'
        handler.command = 'GET'
        handler.requestline = 'GET /api/homepage HTTP/1.1'
        handler.wfile = io.BytesIO()
        with patch('app.command') as command:
            handler.do_GET()
            command.assert_not_called()
        raw = handler.wfile.getvalue()
        self.assertIn(b'200', raw.split(b'\r\n')[0])
        payload = json.loads(raw.split(b'\r\n\r\n', 1)[1])
        self.assertEqual(payload['summary']['attention'], 1)
        self.assertEqual(payload['disks'][0]['label'], 'test')
        self.assertNotIn('controls_enabled', payload)


class ExtendedTestTrackingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.patch = patch('app.DB', Path(self.directory.name) / 'history.sqlite3')
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.directory.cleanup)
        app.initialize()

    def smart(self, hours=100, status='Completed without error', passed=True, running=False):
        return {'self_test_log': {'standard': {'table': [{
            'num': 1, 'type': {'value': 2, 'string': 'Extended offline'},
            'status': {'value': 15 if running else 0, 'string': status, 'passed': passed},
            'lifetime_hours': hours
        }]}}, 'test_status': {'value': 249 if running else 0}}

    def tracking(self):
        with app.connect() as db:
            return app.read_test_tracking(db, 'test')

    def save_read(self, smart, at, previous=None):
        disk = {'serial': 'test', 'label': 'test', 'state': 'awake',
                'smart': smart, 'smart_at': at, 'observed_at': at}
        app.save(disk, previous or {})
        return disk

    def test_historical_result_does_not_invent_calendar_date(self):
        self.save_read(self.smart(), 1000)
        result = self.tracking()
        self.assertEqual(result['last_result'], 'Completed without error')
        self.assertEqual(result['last_result_power_on_hours'], 100)
        self.assertEqual(result['result_source'], 'historical')
        self.assertIsNone(result['last_observed_completed_at'])
        self.assertNotIn('last_started_at', result)

    def test_app_start_and_new_result_survive_restart(self):
        smart = self.smart()
        self.save_read(smart, 1000)
        app.record_extended_start('test', smart, 1100)
        app.initialize()
        self.assertEqual(self.tracking()['last_started_at'], 1100)
        self.assertEqual(self.tracking()['last_started_source'], 'app')
        self.save_read(self.smart(hours=108), 5000)
        result = self.tracking()
        self.assertEqual(result['last_observed_completed_at'], 5000)
        self.assertTrue(result['last_result_passed'])
        self.assertNotIn('pending', result)

    def test_old_result_does_not_complete_new_test(self):
        smart = self.smart()
        self.save_read(smart, 1000)
        app.record_extended_start('test', smart, 1100)
        self.save_read(smart, 1200)
        self.assertIn('pending', self.tracking())
        self.assertIsNone(self.tracking()['last_observed_completed_at'])

    def test_observed_external_extended_test_records_observation_time(self):
        self.save_read(self.smart(status='Self-test routine in progress', passed=None, running=True), 1000)
        self.assertEqual(self.tracking()['last_started_source'], 'observed')
        self.assertEqual(self.tracking()['last_started_at'], 1000)
        self.save_read(self.smart(hours=108), 5000)
        self.assertEqual(self.tracking()['last_observed_completed_at'], 5000)

    def test_cached_sleeping_reading_does_not_advance_dates(self):
        old = self.save_read(self.smart(), 1000)
        cached = {**old, 'state': 'standby', 'observed_at': 6000}
        app.save(cached, old)
        self.assertEqual(self.tracking()['result_discovered_at'], 1000)
        self.assertIsNone(self.tracking()['last_observed_completed_at'])

    def test_short_tests_and_log_entry_renumbering_do_not_replace_long_result(self):
        smart = self.smart()
        self.save_read(smart, 1000)
        smart['self_test_log']['standard']['table'][0]['num'] = 2
        smart['self_test_log']['standard']['table'].insert(0, {
            'type': {'value': 1, 'string': 'Short offline'},
            'status': {'value': 0, 'string': 'Completed without error', 'passed': True},
            'lifetime_hours': 101})
        self.save_read(smart, 2000)
        self.assertEqual(self.tracking()['result_discovered_at'], 1000)
        self.assertEqual(self.tracking()['last_result_power_on_hours'], 100)

    def test_aborted_result_is_not_treated_as_pass(self):
        smart = self.smart()
        app.record_extended_start('test', smart, 1000)
        self.save_read(self.smart(hours=101, status='Aborted by host', passed=False), 2000)
        self.assertFalse(self.tracking()['last_result_passed'])
        self.assertEqual(self.tracking()['last_result'], 'Aborted by host')
        self.assertEqual(self.tracking()['last_observed_completed_at'], 2000)

    def test_failed_start_command_does_not_record_a_start(self):
        config = {'serial': 'test'}
        disk = {'serial': 'test', 'state': 'awake'}
        data = {'ata_smart_data': {'self_test': {'status': {'value': 0}}}}
        with patch('app.CONFIGS', [config]), patch('app.device_for', return_value='/dev/test'), patch('app.stored', return_value={}), patch('app.collect', return_value=disk), patch('app.save'), patch('app.smart_probe', return_value=data), patch('app.command', return_value=(2, '{}')), patch('app.record_extended_start') as record:
            with self.assertRaisesRegex(ValueError, 'could not be started'):
                app.execute_job({'action': 'test', 'serial': 'test', 'wake': False})
            record.assert_not_called()


class FreshnessTests(unittest.TestCase):
    def payload(self, times):
        return {'server_time': 1000, 'poll_seconds': 30,
                'disks': [{'observed_at': t} for t in times]}

    def test_current_delayed_and_stale_thresholds(self):
        self.assertEqual(app.collector_freshness(self.payload([970, 940]))['state'], 'current')
        self.assertEqual(app.collector_freshness(self.payload([970, 939]))['state'], 'delayed')
        self.assertEqual(app.collector_freshness(self.payload([970, 909]))['state'], 'stale')

    def test_one_stuck_disk_is_not_hidden_by_other_fresh_disks(self):
        result = app.collector_freshness(self.payload([999, 999, 800]))
        self.assertEqual(result['state'], 'stale')
        self.assertEqual(result['oldest_observation_age_seconds'], 200)

    def test_no_data_missing_disk_and_clock_anomaly_are_not_green(self):
        for times in [[], [0, 999], [1100]]:
            self.assertEqual(app.collector_freshness(self.payload(times))['state'], 'stale')

    def test_health_get_and_head_fail_when_observations_are_stale(self):
        import io
        for method in ['GET', 'HEAD']:
            for times, expected in [([999], 200), ([930], 503), ([800], 503), ([], 503)]:
                handler = object.__new__(app.Handler)
                handler.path = '/healthz'
                handler.request_version = 'HTTP/1.1'
                handler.command = method
                handler.requestline = method + ' /healthz HTTP/1.1'
                handler.wfile = io.BytesIO()
                with patch('app.snapshot', return_value=self.payload(times)), patch('app.command') as command:
                    getattr(handler, 'do_' + method)()
                    command.assert_not_called()
                response = handler.wfile.getvalue()
                self.assertIn(str(expected).encode(), response.split(b'\r\n')[0])
                self.assertIn(b'Cache-Control: no-store', response)
                if method == 'HEAD':
                    self.assertEqual(response.split(b'\r\n\r\n', 1)[1], b'')


if __name__ == '__main__':
    unittest.main()
