import os
import socket
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
import app

WAKE = ('2026-10-13T15:19:22-05:00 DiskStation scemd[11897]: event_disk_hibernation_handler.c:47 '
        'The internal disks wake up, hibernate from [Oct 12 11:35:23]')
OTHER = '2026-10-13T15:19:22-05:00 DiskStation scemd[11897]: disk/disk_config_single.c:352 /dev/sda apply wd idle off'


def ts(text):
    return datetime.fromisoformat(text).timestamp()


class ParseTests(unittest.TestCase):
    def test_wake_line(self):
        logged, wake = app.parse_synology_line(WAKE)
        self.assertEqual(logged, ts('2026-10-13T15:19:22-05:00'))
        # The bracket is Pacific (SYNOLOGY_TZ), unlike the line's -05:00 stamp.
        self.assertEqual(wake, (logged, ts('2026-10-12T11:35:23-07:00')))

    def test_other_scemd_lines_count_but_are_not_wakes(self):
        self.assertEqual(app.parse_synology_line(OTHER), (ts('2026-10-13T15:19:22-05:00'), None))
        self.assertEqual(app.parse_synology_line('<30>' + OTHER)[1], None)

    def test_foreign_lines_are_ignored(self):
        for line in ('', 'garbage', OTHER.replace('scemd', 'sshd'),
                     '2026-13-40T99:00:00-05:00 DiskStation scemd[1]: x'):
            self.assertIsNone(app.parse_synology_line(line), line)

    def test_sleep_from_the_previous_year(self):
        line = WAKE.replace('2026-10-13', '2027-01-01').replace('Oct 12', 'Dec 31')
        logged, (woke, slept) = app.parse_synology_line(line)
        self.assertEqual(slept, ts('2026-12-31T11:35:23-08:00'))

    def test_sleep_after_the_wake_is_unknown(self):
        line = WAKE.replace('Oct 12 11:35:23', 'Oct 13 23:00:00')
        self.assertIsNone(app.parse_synology_line(line)[1][1])


class StoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        for target, value in (('app.DB', Path(directory.name) / 'h.sqlite3'),
                              ('app.SYNOLOGY_ADDRESSES', {'127.0.0.1'})):
            p = patch(target, value)
            p.start()
            self.addCleanup(p.stop)
        app.initialize()
        self.now = ts('2026-10-13T16:00:00-05:00')

    def test_wakes_are_stored_once_and_counted(self):
        for _ in range(2):
            self.assertTrue(app.record_synology_line(WAKE, self.now))
        self.assertTrue(app.record_synology_line(OTHER, self.now))
        status = app.synology_status(self.now)
        self.assertEqual((status['wakes_24h'], status['wakes_7d'], len(status['wakes'])), (1, 1, 1))
        self.assertEqual(status['last_asleep_label'], '1d 1h')
        self.assertEqual(status['last_line_at'], ts('2026-10-13T15:19:22-05:00'))
        card = app.synology_card(status)
        self.assertEqual(card['description'], 'Woke Oct 13, 1:19 PM after 1d 1h asleep')

    def test_future_lines_are_refused(self):
        self.assertFalse(app.record_synology_line(WAKE, self.now - 3600))
        self.assertEqual(app.synology_status(self.now)['wakes'], [])

    def test_unconfigured(self):
        with patch('app.SYNOLOGY_ADDRESSES', set()):
            self.assertEqual(app.synology_status(self.now), {'configured': False})
        self.assertEqual(app.synology_card({'configured': False})['description'], 'Not set up')

    def test_listener_hears_only_the_synology(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        self.addCleanup(sock.close)
        threading.Thread(target=app.run_synology_listener, args=(sock,), daemon=True).start()
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(sender.close)
        with patch('app.SYNOLOGY_ADDRESSES', {'127.0.0.2'}):
            sender.sendto(WAKE.encode(), ('127.0.0.1', port))
            time.sleep(0.3)
        self.assertEqual(app.synology_status(self.now)['wakes'], [])
        sender.sendto((WAKE + '\n').encode(), ('127.0.0.1', port))
        deadline = time.time() + 3
        while time.time() < deadline and not app.synology_status(self.now)['wakes']:
            time.sleep(0.05)
        self.assertEqual(len(app.synology_status(self.now)['wakes']), 1)


if __name__ == '__main__':
    unittest.main()
