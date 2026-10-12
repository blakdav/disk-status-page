import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import app

JOBS = [{'id': 'vault-hetzner', 'unit': 'backup-restic-vault', 'source': 'Vault',
         'destination': 'Hetzner', 'method': 'restic', 'max_age_hours': 26},
        {'id': 'restic-maintenance', 'unit': 'backup-restic-maintain', 'name': 'Hetzner prune and check',
         'source': 'Hetzner repository', 'destination': 'Prune and check', 'max_age_hours': 192}]


class BackupListTests(unittest.TestCase):
    def load(self, value):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'backups.json'
            path.write_text(json.dumps(value))
            return app.load_backup_jobs(path)

    def test_example_list_is_valid(self):
        jobs = app.load_backup_jobs(Path(app.ROOT) / 'backups.example.json')
        self.assertEqual(len(jobs), 5)

    def test_bad_lists_are_refused(self):
        good = JOBS[0]
        for bad in ({}, [good, good], [{**good, 'id': '../etc'}], [{**good, 'unit': 'a b'}],
                    [{**good, 'source': ' '}], [{**good, 'max_age_hours': 0}],
                    [{**good, 'max_age_hours': True}], [{**good, 'method': 'tar'}],
                    [{**good, 'name': ''}]):
            with self.assertRaises(ValueError, msg=bad):
                self.load(bad)
        self.assertEqual(self.load(JOBS), JOBS)


class BackupStatusTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        for target, value in (('app.BACKUP_DIR', str(self.dir)), ('app.BACKUP_JOBS', JOBS),
                              ('app.BACKUP_LIST_ERROR', None)):
            p = patch(target, value)
            p.start()
            self.addCleanup(p.stop)
        self.now = time.time()

    def write(self, job, **record):
        (self.dir / f'{job}.json').write_text(json.dumps({'boot_id': 'boot', **record}))

    def status(self, boot='boot'):
        with patch('app.interrupted', side_effect=lambda raw: raw.get('boot_id') != boot):
            return app.backup_status(self.now)

    def job(self, job_id='vault-hetzner', **kwargs):
        return next(j for j in self.status(**kwargs)['jobs'] if j['id'] == job_id)

    def test_before_first_run_is_waiting_not_a_problem(self):
        backups = self.status()
        self.assertEqual({j['state'] for j in backups['jobs']}, {'waiting'})
        self.assertTrue(app.backups_health(backups)['ok'])

    def test_missing_status_mount_is_a_problem(self):
        with patch('app.BACKUP_DIR', str(self.dir / 'absent')):
            backups = self.status()
        self.assertFalse(app.backups_health(backups)['ok'])

    def test_recent_success_is_ok_with_restic_summary(self):
        self.write('vault-hetzner', state='idle', last_result='success', finished_at=self.now - 3600,
                   last_success_at=self.now - 3600,
                   restic={'snapshot_id': 'ab12cd34', 'data_added': 1288490189, 'files_new': 12})
        job = self.job()
        self.assertEqual(job['state'], 'ok')
        self.assertEqual(job['restic']['data_added_label'], '1.2 GiB')
        self.assertTrue(job['description'].startswith('OK · '))
        self.assertTrue(job['description'].endswith('· +1.2 GiB'))
        self.assertEqual(job['name'], 'Vault → Hetzner')

    def test_old_success_is_overdue(self):
        self.write('vault-hetzner', state='idle', last_result='success', last_success_at=self.now - 27 * 3600)
        job = self.job()
        self.assertEqual((job['state'], job['problem']), ('problem', 'No success in 26 hours'))
        self.write('restic-maintenance', state='idle', last_result='success', last_success_at=self.now - 9 * 86400)
        self.assertEqual(self.job('restic-maintenance')['problem'], 'No success in 8 days')

    def test_failed_run_is_a_problem_even_after_a_recent_success(self):
        self.write('vault-hetzner', state='idle', last_result='exit-code', last_success_at=self.now - 3600)
        job = self.job()
        self.assertEqual(job['problem'], 'Last run failed (exit-code)')
        self.assertIn('last success', job['description'])
        backups = self.status()
        self.assertEqual(app.backups_health(backups)['problems'],
                         ['Vault → Hetzner: Last run failed (exit-code)'])

    def test_long_running_job_is_not_overdue(self):
        # The first PBS upload runs for days.
        self.write('vault-hetzner', state='running', started_at=self.now - 3 * 86400)
        job = self.job()
        self.assertEqual(job['state'], 'running')
        self.assertIsNone(job['problem'])

    def test_run_cut_off_by_a_reboot_is_a_problem(self):
        self.write('vault-hetzner', state='running', started_at=self.now - 600)
        self.assertEqual(self.job(boot='new-boot')['problem'], 'Interrupted by a reboot')

    def test_unreadable_record_is_a_problem(self):
        (self.dir / 'vault-hetzner.json').write_text('{not json')
        self.assertEqual(self.job()['state'], 'unknown')
        self.write('vault-hetzner', state='idle', last_success_at=self.now + 86400)
        self.assertEqual(self.job()['problem'], 'Status unavailable')

    def test_bad_list_shows_as_a_problem(self):
        with patch('app.BACKUP_LIST_ERROR', 'bad'):
            backups = self.status()
        self.assertFalse(app.backups_health(backups)['ok'])

    def test_homepage_and_health_url(self):
        import io
        self.write('vault-hetzner', state='idle', last_result='exit-code', last_success_at=self.now - 3600)
        with tempfile.TemporaryDirectory() as db, patch('app.DB', Path(db) / 'h.sqlite3'), \
                patch('app.interrupted', return_value=False):
            app.initialize()
            result = app.homepage_status(app.snapshot())
            self.assertEqual(result['backups'][0]['name'], 'Vault → Hetzner')
            self.assertEqual(result['backup_summary']['problem'], 1)
            self.assertEqual(result['backup_summary']['waiting'], 1)
            handler = object.__new__(app.Handler)
            handler.path = '/healthz/backups'
            handler.request_version = 'HTTP/1.1'
            handler.command = 'GET'
            handler.requestline = 'GET /healthz/backups HTTP/1.1'
            handler.wfile = io.BytesIO()
            handler.do_GET()
        self.assertIn(b'503', handler.wfile.getvalue().split(b'\r\n')[0])


class LabelTests(unittest.TestCase):
    def test_calendar_labels(self):
        cases = {'*-*-* 02:00:00 America/Los_Angeles': 'Daily at 2:00 AM',
                 'Sun *-*-* 12:00:00 America/Los_Angeles': 'Sundays at 12:00 PM',
                 '*-*-01 05:00:00 America/Los_Angeles': 'Monthly on day 1 at 5:00 AM',
                 'monthly': 'monthly'}
        for calendar, label in cases.items():
            self.assertEqual(app.calendar_label(calendar), label)

    def test_byte_labels(self):
        self.assertEqual(app.bytes_label(0), '0 B')
        self.assertEqual(app.bytes_label(5 * 2 ** 20), '5.0 MiB')
        self.assertEqual(app.bytes_label(3 * 2 ** 40), '3.0 TiB')
        self.assertIsNone(app.bytes_label(None))


if __name__ == '__main__':
    unittest.main()
