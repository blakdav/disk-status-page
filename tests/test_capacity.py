import unittest
from types import SimpleNamespace
from unittest.mock import patch
import app

class CapacityTests(unittest.TestCase):
    def test_reserved_space_is_not_available(self):
        stat = SimpleNamespace(f_blocks=100, f_bfree=40, f_bavail=30, f_frsize=4096)
        with patch('app.os.path.ismount', return_value=True), patch('app.os.statvfs', return_value=stat):
            c = app.filesystem_capacity('/test', 10)
        self.assertEqual(c['used_bytes'], 60 * 4096)
        self.assertEqual(c['free_bytes'], 40 * 4096)
        self.assertEqual(c['available_bytes'], 30 * 4096)

    def test_unmounted_path_never_reports_os_capacity(self):
        with patch('app.os.path.ismount', return_value=False), patch('app.os.statvfs') as stat:
            self.assertEqual(app.filesystem_capacity('/test', 1)['status'], 'unmounted')
            stat.assert_not_called()

    def test_sleeping_disk_retains_measurement(self):
        disk = {'state': 'standby', 'capacity': {'observed_at': 1}}
        with patch('app.os.path.ismount', return_value=True), patch('app.os.statvfs') as stat:
            app.refresh_capacity({'label': 'data1'}, disk, 10)
            stat.assert_not_called()
        self.assertEqual(disk['capacity']['observed_at'], 1)

    def test_pool_not_sampled_with_sleeping_member(self):
        with patch('app.filesystem_capacity') as sample:
            app.refresh_pool_capacity([{'role': 'Pool data', 'state': 'standby'}], 10)
            sample.assert_not_called()

    def test_schedules_preserve_unknown_calendar(self):
        with patch('app.Path.read_text', return_value='[Timer]\nOnCalendar=weekly\n'):
            self.assertEqual(app.snapraid_schedules()['sync']['label'], 'weekly')
