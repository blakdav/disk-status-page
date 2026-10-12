import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import app

NVME = {'label': 'vault', 'serial': 'S7DR', 'role': 'Vault', 'type': 'nvme',
        'capacity_path': '/host/mounts/vault'}


def health_log(**overrides):
    log = {'critical_warning': 0, 'percentage_used': 2, 'available_spare': 100,
           'available_spare_threshold': 10, 'data_units_written': 77769392,
           'power_on_hours': 5806, 'unsafe_shutdowns': 169, 'media_errors': 0,
           'num_err_log_entries': 0, 'warning_temp_time': 38, 'critical_comp_time': 0}
    return {'smart_status': {'passed': True}, 'temperature': {'current': 43},
            'nvme_smart_health_information_log': {**log, **overrides}}


class NvmeSummaryTests(unittest.TestCase):
    def test_healthy_drive_reports_wear_without_warnings(self):
        result = app.summarize_nvme(health_log(), 0)
        self.assertTrue(result['passed'])
        self.assertEqual(result['issues'], [])
        self.assertEqual(result['wear']['percentage_used'], 2)
        # 77,769,392 units of 512,000 bytes: the 39.8 TB smartctl prints.
        self.assertEqual(round(result['wear']['written_bytes'] / 10 ** 12, 1), 39.8)

    def test_wear_spare_media_errors_and_critical_bits_warn(self):
        result = app.summarize_nvme(health_log(
            percentage_used=85, available_spare=10, media_errors=2, critical_warning=0b1001), 0)
        keys = {i['key'] for i in result['issues']}
        self.assertEqual(keys, {'wear', 'spare', 'media-errors', 'critical:0', 'critical:3'})
        self.assertFalse(any(i.get('acknowledgeable') for i in result['issues']))

    def test_error_log_entries_alone_do_not_warn(self):
        # Status bit 6 is smartctl's "error log has entries", often harmless.
        result = app.summarize_nvme(health_log(num_err_log_entries=40), 64)
        self.assertEqual(result['issues'], [])

    def test_failing_drive_is_failed(self):
        result = app.summarize_nvme(health_log(), 8)
        self.assertIn('health-failure', {i['key'] for i in result['issues']})


class NvmeCollectTests(unittest.TestCase):
    def test_reads_health_with_nvme_type_and_no_power_query(self):
        with patch('app.device_for', return_value='/dev/test') as find, \
                patch('app.command', return_value=(0, json.dumps(health_log()))) as run, \
                patch('app.refresh_capacity', side_effect=lambda c, d, n: d):
            disk = app.collect(NVME, {}, 10000)
        find.assert_called_once_with('S7DR', 'nvme')
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][:3], ['smartctl', '-d', 'nvme'])
        self.assertEqual(disk['state'], 'awake')
        self.assertEqual(disk['smart_at'], 10000)

    def test_recent_reading_is_not_repeated(self):
        with patch('app.device_for', return_value='/dev/test'), \
                patch('app.command') as run, \
                patch('app.refresh_capacity', side_effect=lambda c, d, n: d):
            disk = app.collect(NVME, {'smart_at': 9999, 'smart': {'passed': True}}, 10000)
        run.assert_not_called()
        self.assertEqual(disk['smart_at'], 9999)

    def test_missing_drive_is_unknown(self):
        with patch('app.device_for', return_value=None):
            disk = app.collect(NVME, {}, 10000)
        self.assertEqual(disk['state'], 'unknown')
        self.assertTrue(disk['error'])

    def test_failed_read_keeps_cached_health(self):
        with patch('app.device_for', return_value='/dev/test'), \
                patch('app.command', return_value=(2, '')), \
                patch('app.refresh_capacity', side_effect=lambda c, d, n: d):
            disk = app.collect(NVME, {'smart_at': 1, 'smart': {'passed': True}}, 10000)
        self.assertEqual(disk['smart_at'], 1)
        self.assertTrue(disk['error'])

    def test_capacity_uses_configured_mount(self):
        with patch('os.path.ismount', return_value=True) as ismount, \
                patch('app.filesystem_capacity', return_value={'status': 'available'}):
            disk = app.refresh_capacity(NVME, {'state': 'awake'}, 1)
        ismount.assert_called_with('/host/mounts/vault')
        self.assertEqual(disk['capacity']['status'], 'available')

    def test_nvme_lookup_skips_namespace_and_eui_links(self):
        with tempfile.TemporaryDirectory() as directory:
            by_id = Path(directory) / 'disk' / 'by-id'
            by_id.mkdir(parents=True)
            for name in ('nvme-Samsung_SSD_990_PRO_2TB_S7DR', 'nvme-Samsung_SSD_990_PRO_2TB_S7DR_1',
                         'nvme-eui.0025384151400cc1'):
                (by_id / name).touch()
            with patch('app.DEV_DIR', directory):
                self.assertEqual(app.device_for('S7DR', 'nvme'),
                                 str(by_id / 'nvme-Samsung_SSD_990_PRO_2TB_S7DR'))
                self.assertIsNone(app.device_for('S7DR'))


class NvmeConfigAndStatusTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.patch = patch('app.DB', Path(self.directory.name) / 'history.sqlite3')
        self.patch.start()
        self.addCleanup(self.patch.stop)
        app.initialize()

    def load(self, value):
        path = Path(self.directory.name) / 'disks.json'
        path.write_text(json.dumps(value))
        return app.load_configs(path)

    def test_bad_nvme_entries_are_refused(self):
        for bad in ({**NVME, 'type': 'sas'}, {**NVME, 'expected_state': 'asleep'},
                    {**NVME, 'allow_spindown': True}, {**NVME, 'capacity_path': '/mnt/vault'},
                    {**NVME, 'capacity_path': '/host/../etc'}):
            with self.assertRaises(ValueError, msg=bad):
                self.load([bad])
        self.assertEqual(self.load([NVME])[0]['type'], 'nvme')

    def test_disk_actions_are_refused_for_nvme(self):
        with patch('app.CONFIGS', [NVME]), patch('app.JOB', None):
            for action in ('spin-up', 'spin-down', 'health', 'test'):
                with self.assertRaises(ValueError):
                    app.submit_job(action, 'S7DR')

    def test_homepage_shows_active_and_wear_without_counting_awake(self):
        smart = app.summarize_nvme(health_log(), 0)
        app.save({**NVME, 'state': 'awake', 'observed_at': time.time(), 'smart': smart,
                  'smart_at': time.time()}, {})
        with patch('app.CONFIGS', [NVME]):
            result = app.homepage_status(app.snapshot())
        disk = result['disks'][0]
        self.assertEqual(disk['description'], 'Vault · Active · 2% worn')
        self.assertEqual(disk['health'], 'Healthy')
        self.assertEqual(result['summary']['awake'], 0)

    def test_homepage_drive_card_has_formatted_space_and_wear(self):
        smart = app.summarize_nvme(health_log(), 0)
        capacity = {'status': 'available', 'total_bytes': 2 * 2 ** 40, 'used_bytes': int(1.25 * 2 ** 40),
                    'free_bytes': int(0.75 * 2 ** 40), 'available_bytes': 22 * 2 ** 30}
        app.save({**NVME, 'state': 'awake', 'observed_at': time.time(), 'smart': smart,
                  'smart_at': time.time(), 'capacity': capacity}, {})
        with patch('app.CONFIGS', [NVME]):
            card = app.homepage_status(app.snapshot())['drives']['vault']
        self.assertEqual(card['used_label'], '1.25 TiB')
        self.assertEqual(card['free_label'], '22 GiB')
        self.assertEqual(card['used_percent_label'], '62%')
        self.assertEqual(card['wear_label'], '2%')
        self.assertEqual(card['temperature_label'], '43 °C')
        self.assertEqual(card['written_label'], '39.8 TB')
        self.assertEqual((card['health'], card['issues_label']), ('Healthy', 'None'))

    def test_drive_card_without_data_says_unavailable(self):
        with patch('app.CONFIGS', [NVME]):
            card = app.homepage_status(app.snapshot())['drives']['vault']
        self.assertEqual({card['used_label'], card['free_label'], card['wear_label']}, {'Unavailable'})

    def test_worn_drive_needs_attention(self):
        smart = app.summarize_nvme(health_log(percentage_used=90), 0)
        app.save({**NVME, 'state': 'awake', 'observed_at': time.time(), 'smart': smart}, {})
        with patch('app.CONFIGS', [NVME]):
            disk = app.homepage_status(app.snapshot())['disks'][0]
        self.assertTrue(disk['needs_attention'])
        self.assertIn('90% of rated endurance used', disk['description'])


if __name__ == '__main__':
    unittest.main()
