import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import app


class SyncStatusTests(unittest.TestCase):
    def status(self, record, boot='boot-a'):
        with patch('app.Path.read_text', side_effect=[json.dumps(record), boot]):
            return app.snapraid_status()

    def test_running_keeps_previous_finished_result(self):
        status = self.status({'state': 'running', 'boot_id': 'boot-a',
                              'started_at': 100, 'finished_at': 50,
                              'last_result': 'success'})
        self.assertEqual(status['state_label'], 'Running')
        self.assertEqual(status['result_label'], 'Succeeded')
        self.assertEqual(status['finished_at'], 50)

    def test_reboot_does_not_claim_still_running(self):
        status = self.status({'state': 'running', 'boot_id': 'old'}, 'new')
        self.assertEqual(status['state'], 'interrupted')

    def test_failure_is_not_success(self):
        status = self.status({'state': 'idle', 'last_result': 'exit-code',
                              'finished_at': 50})
        self.assertEqual(status['result_label'], 'Failed: exit-code')

    def test_missing_invalid_and_future_data_are_unknown(self):
        for text in ('invalid', '[]', '{"state":"idle","finished_at":1e99}'):
            with self.subTest(text=text), patch('app.Path.read_text', return_value=text):
                self.assertEqual(app.snapraid_status()['state'], 'unknown')
        with patch('app.Path.read_text', side_effect=FileNotFoundError):
            self.assertEqual(app.snapraid_status()['state'], 'unknown')

    def test_unreadable_boot_cannot_claim_running(self):
        with patch('app.Path.read_text', side_effect=[
                '{"state":"running","boot_id":"old"}', PermissionError()]):
            self.assertEqual(app.snapraid_status()['state'], 'unknown')

    def test_missing_history_is_distinct_from_missing_mount(self):
        for mounted, expected in [(False, 'Status mount missing'),
                                  (True, 'Awaiting first recorded sync')]:
            with patch('app.Path.read_text', side_effect=FileNotFoundError), patch('app.os.path.ismount', return_value=mounted):
                self.assertEqual(app.snapraid_status()['state_label'], expected)
