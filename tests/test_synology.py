import socket
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
import app

# As DSM's Log Sending sends them (IETF format), BOM included.
LOGIN = ('<14>1 2026-10-15T21:49:07-07:00 DiskStation Connection - - [synolog@6574 event_id="0x0001" '
         'synotype="Connection" username="blake" event="User [blake\\] from [192.0.2.3\\] logged in '
         'successfully via [SSH\\]." arg_4="SSH"][meta sequenceId="1"] ﻿User [blake] from [192.0.2.3] '
         'logged in successfully via [SSH].')
WAKE = ('<14>1 2026-10-15T21:17:27-07:00 DiskStation System - - [meta sequenceId="1"] '
        '﻿Internal disks woke up from hibernation.')


def ts(text):
    return datetime.fromisoformat(text).timestamp()


class ParseTests(unittest.TestCase):
    def test_structured_data_with_escaped_brackets(self):
        event = app.parse_syslog(LOGIN)
        self.assertEqual(event, {'logged_at': ts('2026-10-15T21:49:07-07:00'), 'host': 'DiskStation',
                                 'app': 'Connection',
                                 'message': 'User [blake] from [192.0.2.3] logged in successfully via [SSH].'})

    def test_no_structured_data(self):
        event = app.parse_syslog('<14>1 2026-10-15T21:17:27-07:00 DiskStation System - - - hello')
        self.assertEqual(event['message'], 'hello')

    def test_other_formats_are_not_parsed(self):
        for line in ('', 'garbage', '<14>Oct 15 21:17:27 DiskStation System: hello',
                     '<14>1 not-a-time DiskStation System - - - hello'):
            self.assertIsNone(app.parse_syslog(line), line)


class StoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        for target, value in (('app.DB', self.dir / 'h.sqlite3'),
                              ('app.SYNOLOGY_LOG', self.dir / 'synology.log'),
                              ('app.SYNOLOGY_ADDRESSES', {'127.0.0.1'})):
            p = patch(target, value)
            p.start()
            self.addCleanup(p.stop)
        app.initialize()
        self.now = ts('2026-10-15T22:00:00-07:00')

    def test_wakes_are_stored_once_and_counted(self):
        for _ in range(2):
            self.assertTrue(app.record_synology_line(WAKE, self.now))
        self.assertTrue(app.record_synology_line(LOGIN, self.now))
        status = app.synology_status(self.now)
        self.assertEqual((status['wakes_24h'], status['wakes_7d'], len(status['wakes'])), (1, 1, 1))
        self.assertEqual(status['last_wake_label'], 'Oct 15, 9:17 PM')
        self.assertEqual(status['last_line_at'], ts('2026-10-15T21:49:07-07:00'))
        self.assertEqual(app.synology_card(status)['description'], 'Last woke Oct 15, 9:17 PM')

    def test_every_line_is_kept_in_the_log(self):
        app.record_synology_line(WAKE, self.now)
        app.record_synology_line(LOGIN, self.now)
        app.record_synology_line('<14>Oct 15 bsd \x1b[31mline', self.now)
        log = app.synology_status(self.now)['log']
        self.assertEqual(len(log), 3)
        self.assertIn('(unparsed) <14>Oct 15 bsd  [31mline', log[0])
        self.assertIn('DiskStation Connection: User [blake]', log[1])
        self.assertIn('DiskStation System: Internal disks woke up from hibernation.', log[2])
        self.assertNotIn('﻿', (self.dir / 'synology.log').read_text())

    def test_log_rotates_at_its_size_limit(self):
        with patch('app.SYNOLOGY_LOG_MAX', 200):
            for _ in range(5):
                app.record_synology_line(LOGIN, self.now)
        self.assertTrue((self.dir / 'synology.log.1').exists())
        self.assertLess((self.dir / 'synology.log').stat().st_size, 400)

    def test_wake_from_another_program_is_not_a_wake(self):
        app.record_synology_line(WAKE.replace(' System ', ' Connection '), self.now)
        self.assertEqual(app.synology_status(self.now)['wakes'], [])

    def test_future_lines_are_not_wakes(self):
        self.assertFalse(app.record_synology_line(WAKE, self.now - 3600))
        self.assertEqual(app.synology_status(self.now)['wakes'], [])

    def test_unconfigured(self):
        with patch('app.SYNOLOGY_ADDRESSES', set()):
            self.assertEqual(app.synology_status(self.now), {'configured': False})
        self.assertEqual(app.synology_card({'configured': False})['description'], 'Not set up')

    def test_listener_hears_only_the_synology(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(('127.0.0.1', 0))
        self.addCleanup(sock.close)
        port = sock.getsockname()[1]
        threading.Thread(target=app.run_synology_listener, args=(sock,), daemon=True).start()
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(sender.close)
        # The listener uses the real clock, so the line must be recent.
        self.now = time.time()
        stamp = datetime.fromtimestamp(self.now - 60).astimezone().isoformat(timespec='seconds')
        wake = WAKE.replace('2026-10-15T21:17:27-07:00', stamp).encode()
        with patch('app.SYNOLOGY_ADDRESSES', {'127.0.0.2'}):
            sender.sendto(wake, ('127.0.0.1', port))
            time.sleep(0.3)
        self.assertEqual(app.synology_status(self.now)['wakes'], [])
        sender.sendto(wake, ('127.0.0.1', port))
        deadline = time.time() + 3
        while time.time() < deadline and not app.synology_status(self.now)['wakes']:
            time.sleep(0.05)
        self.assertEqual(len(app.synology_status(self.now)['wakes']), 1)


if __name__ == '__main__':
    unittest.main()
