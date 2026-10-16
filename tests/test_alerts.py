import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import app

NOW = 1_800_000_000


def disk(label, serial, **extra):
    return {'label': label, 'serial': serial, 'role': 'Pool data', 'state': 'awake',
            'observed_at': NOW, 'stale': False, 'needs_attention': False,
            'unexpected_state': False, 'smart': {}, **extra}


def payload(disks, **extra):
    return {'disks': disks, 'server_time': NOW, 'poll_seconds': 30,
            'snapraid': {'state': 'idle', 'last_result': 'success', 'last_success_at': NOW},
            'backups': {'jobs': []}, 'pool': {}, **extra}


class ProblemTests(unittest.TestCase):
    def test_healthy_is_quiet(self):
        self.assertEqual(app.current_problems(payload([disk('data1', 'A')])), {})

    def test_each_kind_of_problem(self):
        problems = app.current_problems(payload(
            [disk('data1', 'A', needs_attention=True, smart={'active_warnings': ['Reallocated sectors: 8']}),
             disk('data3', 'B', role='Cold spare', expected_state='asleep', unexpected_state=True),
             disk('vault', 'C', type='nvme', capacity={'status': 'available', 'total_bytes': 100,
                                                       'used_bytes': 95, 'available_bytes': 5})],
            pool={'total_bytes': 100, 'used_bytes': 91, 'available_label': '0.10 TiB'},
            snapraid={'state': 'idle', 'last_result': 'exit-code', 'result_label': 'Failed: exit-code'},
            backups={'jobs': [{'id': 'vault-hetzner', 'name': 'Vault → Hetzner', 'problem': 'Overdue'}]}))
        self.assertEqual(problems['disk:A'][:2], ('data1 needs attention', 'Reallocated sectors: 8'))
        self.assertEqual(problems['state:B'][:2], ('data3 is awake', 'Cold spare should be asleep'))
        self.assertEqual(problems['state:B'][2], app.ALERT_STATE_DELAY)
        self.assertEqual(problems['capacity:C'][0], 'vault is 95% full')
        self.assertEqual(problems['capacity:pool'][0], 'Pool is 91% full')
        self.assertEqual(problems['sync'][1], 'Last sync failed: exit-code')
        self.assertEqual(problems['backup:vault-hetzner'][:2], ('Backup: Vault → Hetzner', 'Overdue'))

    def test_stalled_collector_is_one_alert_not_one_per_disk(self):
        disks = [disk(f'data{i}', str(i), observed_at=NOW - 3600, stale=True, needs_attention=True)
                 for i in range(5)]
        self.assertEqual(list(app.current_problems(payload(disks))), ['collector'])


class ProcessTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        p = patch('app.DB', Path(directory.name) / 'h.sqlite3')
        p.start()
        self.addCleanup(p.stop)
        app.initialize()
        self.sent = []
        self.accept = True

    def send(self, title, message, priority=0):
        self.sent.append((title, message, priority))
        return self.accept

    def run_at(self, minutes, disks):
        return app.process_alerts(payload(disks), NOW + minutes * 60, self.send)

    def bad(self, warning='Reallocated sectors: 8'):
        return [disk('data1', 'A', needs_attention=True, smart={'active_warnings': [warning]})]

    def test_waits_for_the_delay_then_alerts_once_then_resolves(self):
        self.run_at(0, self.bad())
        self.run_at(4, self.bad())
        self.assertEqual(self.sent, [])
        self.run_at(5, self.bad())
        self.run_at(6, self.bad())
        self.assertEqual(self.sent, [('data1 needs attention', 'Reallocated sectors: 8', 0)])
        self.run_at(7, [disk('data1', 'A')])
        self.assertEqual(self.sent[-1], ('Resolved: data1 needs attention', 'Reallocated sectors: 8', -1))
        self.run_at(8, [disk('data1', 'A')])
        self.assertEqual(len(self.sent), 2)

    def test_a_problem_that_clears_within_its_delay_is_never_sent(self):
        self.run_at(0, self.bad())
        self.run_at(2, [disk('data1', 'A')])
        self.run_at(10, [disk('data1', 'A')])
        self.assertEqual(self.sent, [])

    def test_changed_details_are_sent_again(self):
        self.run_at(0, self.bad())
        self.run_at(5, self.bad())
        self.run_at(6, self.bad('Reallocated sectors: 16'))
        self.assertEqual([m for _, m, _ in self.sent], ['Reallocated sectors: 8', 'Reallocated sectors: 16'])

    def test_failed_send_is_retried(self):
        self.accept = False
        self.run_at(0, self.bad())
        self.run_at(5, self.bad())
        self.accept = True
        self.run_at(6, self.bad())
        self.run_at(7, self.bad())
        self.assertEqual(len(self.sent), 2)

    def test_reminder_while_unresolved(self):
        self.run_at(0, self.bad())
        self.run_at(5, self.bad())
        self.run_at(5 + 23 * 60, self.bad())
        self.assertEqual(len(self.sent), 1)
        self.run_at(5 + 24 * 60, self.bad())
        self.assertEqual(self.sent[-1][0], '1 unresolved')
        self.assertIn('data1 needs attention: Reallocated sectors: 8', self.sent[-1][1])


if __name__ == '__main__':
    unittest.main()
