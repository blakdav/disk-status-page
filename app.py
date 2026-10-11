"""Disk Status Page: ATA monitoring and explicit disk controls and a small HTTP dashboard."""
import glob
from contextlib import contextmanager
import json
import hmac
import secrets
import queue
from urllib.parse import urlsplit
import logging
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).parent
INTERVAL = max(10, int(os.getenv('POLL_SECONDS', '60')))
SMART_INTERVAL = max(INTERVAL, int(os.getenv('SMART_SECONDS', '1800')))
DEMO = os.getenv('DEMO', 'false').lower() == 'true'
STOP = threading.Event()
DB = Path(os.getenv('DATA_DIR', '/data')) / 'history.sqlite3'
# The host's /dev. Compose mounts it at /host/dev so the container keeps its
# own /dev; udev's by-id links are relative, so they resolve inside it.
DEV_DIR = os.getenv('DEV_DIR', '/dev').rstrip('/')
# Host names the dashboard may be reached by. Disk controls answer only
# requests addressed to one of these, which stops DNS rebinding: a foreign
# page pointed at the NAS's IP still sends its own name as Host.
ALLOWED_HOSTS = {h.strip().lower() for h in os.getenv('ALLOWED_HOSTS', '').split(',') if h.strip()}
WARNINGS = {5, 187, 188, 197, 198, 199}
COUNTERS = {4, 12, 193}
# Acknowledge cumulative counters, never current pending/unreadable sectors.
ACKNOWLEDGEABLE = {5, 187, 188, 199}
CONFIGS = []
ACTION_TOKEN = ''
JOBS = queue.Queue(maxsize=1)
JOB_LOCK = threading.Lock()
JOB = None


def load_token():
    token_file = DB.parent / 'action-token'
    if not token_file.exists():
        with open(token_file, 'x', opener=lambda p, f: os.open(p, f, 0o600)) as out:
            out.write(secrets.token_urlsafe(32))
    return token_file.read_text().strip()



@contextmanager
def connect():
    connection = sqlite3.connect(DB, timeout=10)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize():
    DB.parent.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, payload TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS disks '
                   '(serial TEXT PRIMARY KEY, payload TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS events '
                   '(serial TEXT, observed_at REAL, state TEXT)')
        db.execute('CREATE INDEX IF NOT EXISTS event_time ON events(observed_at)')
        db.execute('CREATE TABLE IF NOT EXISTS acknowledgements '
                   '(serial TEXT, attribute_id INTEGER, baseline INTEGER, '
                   'acknowledged_at REAL, PRIMARY KEY(serial, attribute_id))')
        db.execute('CREATE TABLE IF NOT EXISTS extended_tests '
                   '(serial TEXT PRIMARY KEY, payload TEXT NOT NULL)')


def command(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=20)
    return result.returncode, result.stdout + result.stderr


def device_for(serial):
    # Resolve existing udev links only; discovery must never probe a disk.
    candidates = glob.glob(DEV_DIR + '/disk/by-id/ata-*_' + glob.escape(serial))
    return sorted(candidates)[0] if candidates else None


def power_state(output):
    match = re.search(r'drive state is:\s*(active/idle|standby|sleeping)', output)
    return {'active/idle': 'awake', 'standby': 'standby',
            'sleeping': 'standby'}.get(match[1], 'unknown') if match else 'unknown'


def summarize_smart(data, status):
    attrs = data.get('ata_smart_attributes', {}).get('table', [])
    warnings = []
    issues = []
    counters = {}
    for attr in attrs:
        raw = attr.get('raw', {}).get('value', 0)
        if attr.get('id') in WARNINGS and raw:
            message = f"{attr['name']}: {attr.get('raw', {}).get('string', raw)}"
            warnings.append(message)
            issues.append({'key': f"attribute:{attr['id']}", 'attribute_id': attr['id'],
                           'raw': raw, 'message': message,
                           'acknowledgeable': attr['id'] in ACKNOWLEDGEABLE})
        if attr.get('id') in COUNTERS:
            counters[attr['name']] = raw
    passed = data.get('smart_status', {}).get('passed')
    errors = data.get('ata_smart_error_log', {}).get('summary', {}).get('count', 0)
    if errors:
        message = f'{errors} recorded SMART errors (may be historical)'
        warnings.append(message)
        issues.append({'key': 'error-log', 'message': message})
    # The self-test count includes successful tests; inspect individual results.
    for test in data.get('ata_smart_self_test_log', {}).get('standard', {}).get('table', []):
        result = test.get('status', {})
        if result.get('passed') is False:
            message = 'Self-test: ' + result.get('string', 'failed')
            warnings.append(message)
            issues.append({'key': 'self-test', 'message': message})
    # Keep threshold failures separate from raw counts: acknowledging a count
    # must never suppress SMART's own current/past attribute failure flags.
    for bit, key, message in (
        (3, 'health-failure', 'SMART overall health failure'),
        (4, 'threshold', 'A SMART attribute is at or below its failure threshold'),
        (5, 'past-threshold', 'A SMART attribute previously crossed its failure threshold'),
        (6, 'error-log-flag', 'SMART error log contains recorded errors'),
        (7, 'self-test-flag', 'SMART self-test log contains recorded failures'),
    ):
        if status & (1 << bit):
            warnings.append(message)
            issues.append({'key': key, 'message': message})
    return {'passed': passed, 'temperature': data.get('temperature', {}).get('current'),
            'warnings': warnings, 'issues': issues, 'counters': counters,
            'error_log': data.get('ata_smart_error_log', {}),
            'self_test_log': data.get('ata_smart_self_test_log', {}),
            'attributes': attrs,
            'test_status': data.get('ata_smart_data', {}).get('self_test', {}).get('status', {})}


def collect(config, previous, now, force_smart=False):
    disk = {**previous, **config, 'observed_at': now, 'error': None}
    path = device_for(config['serial'])
    disk['device'] = path
    disk['state'] = 'unknown'
    if not path:
        disk['error'] = 'Disk not found by serial; check device access and udev links.'
        return disk
    try:
        code, output = command(['hdparm', '-C', path])
        disk['state'] = power_state(output) if code == 0 else 'unknown'
        if disk['state'] == 'unknown':
            disk['error'] = 'Power-state query failed or returned an unsupported response.'
            return disk
        # Fail closed: no SMART query unless the power-state query succeeded.
        if disk['state'] == 'awake' and (force_smart or (previous.get('smart', {}).get('test_status', {}).get('value', 0) & 0xf0 == 0xf0) or now - previous.get('smart_at', 0) >= SMART_INTERVAL):
            code, output = command(['smartctl', '-d', config.get('smart_type', 'ata'),
                                    '-n', 'standby,3,5', '-a', '-j', path])
            # Explicit type avoids autodetection; standby recheck handles races.
            if code == 3:
                disk['state'] = 'standby'
            elif code & 7:
                disk['error'] = 'SMART read skipped or failed; cached values retained.'
            else:
                data = json.loads(output)
                disk['smart'] = summarize_smart(data, code)
                disk['smart_at'] = now
        real = Path(os.path.realpath(path)).name
        stat = Path('/sys/class/block') / real / 'stat'
        if stat.exists():
            values = stat.read_text().split()
            disk['io'] = {'reads': int(values[0]), 'writes': int(values[4])}
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        disk['error'] = str(exc)
    return refresh_capacity(config, disk, now)


def latest_extended_entry(smart):
    # SMART log order is newest first; the log entry number changes whenever
    # another test is added, so it must not be part of the identity.
    table = smart.get('self_test_log', {}).get('standard', {}).get('table', [])
    for entry in table:
        test_type = entry.get('type', {})
        if test_type.get('value') in (2, 130) or 'extended' in test_type.get('string', '').lower():
            return entry
    return None


def extended_entry_key(entry):
    if not entry:
        return None
    return json.dumps({k: entry.get(k) for k in
                       ('type', 'status', 'lifetime_hours', 'lba')}, sort_keys=True)


def entry_in_progress(entry):
    status = entry.get('status', {})
    return (status.get('value') in (15, 240)
            or 'in progress' in status.get('string', '').lower())


def write_test_tracking(db, serial, tracking):
    db.execute('INSERT INTO extended_tests VALUES (?, ?) '
               'ON CONFLICT(serial) DO UPDATE SET payload = excluded.payload',
               (serial, json.dumps(tracking)))


def read_test_tracking(db, serial):
    row = db.execute('SELECT payload FROM extended_tests WHERE serial = ?', (serial,)).fetchone()
    return json.loads(row[0]) if row else {}


def record_extended_start(serial, before_smart, requested_at):
    with connect() as db:
        tracking = read_test_tracking(db, serial)
        # Import any prior result without inventing its calendar date.
        tracking = observe_extended_test(tracking, before_smart, requested_at)
        tracking.update(last_started_at=requested_at, last_started_source='app',
                        pending={'started_at': requested_at, 'source': 'app',
                                 'baseline_key': extended_entry_key(latest_extended_entry(before_smart))})
        write_test_tracking(db, serial, tracking)


def observe_extended_test(tracking, smart, observed_at):
    tracking = dict(tracking)
    entry = latest_extended_entry(smart)
    if not entry:
        return tracking
    key = extended_entry_key(entry)
    previous_key = tracking.get('latest_log_key')
    if entry_in_progress(entry):
        # Only an explicitly extended log entry establishes an external long
        # test; generic execution status can also describe a short test.
        pending = tracking.get('pending')
        if not pending:
            tracking.update(last_started_at=observed_at, last_started_source='observed',
                            pending={'started_at': observed_at, 'source': 'observed',
                                     'baseline_key': previous_key})
        tracking['latest_log_key'] = key
        return tracking
    pending = tracking.get('pending')
    new_result = key != previous_key
    if not new_result:
        return tracking
    # A result already present before the request is not that test's result.
    if pending and key == pending.get('baseline_key'):
        return tracking
    correlated = bool(pending)
    tracking.update(latest_log_key=key,
                    last_result=entry.get('status', {}).get('string', 'Unknown result'),
                    last_result_passed=entry.get('status', {}).get('passed'),
                    last_result_power_on_hours=entry.get('lifetime_hours'),
                    result_discovered_at=observed_at,
                    result_source='observed' if correlated or previous_key is not None else 'historical',
                    last_observed_completed_at=observed_at if correlated or previous_key is not None else None)
    # Completed includes aborted/failed attempts; passed remains separate.
    if correlated:
        tracking.pop('pending', None)
    return tracking


def save(disk, previous):
    # A first sample, restart gap, or unknown sample does not prove a transition.
    contiguous = (previous.get('state') in ('awake', 'standby')
                  and disk['observed_at'] - previous.get('observed_at', 0) < INTERVAL * 2.5)
    changed = contiguous and disk['state'] in ('awake', 'standby') and disk['state'] != previous['state']
    with connect() as db:
        # Cached readings on sleeping disks must not advance test timestamps.
        tracking = read_test_tracking(db, disk['serial'])
        if disk.get('smart') and (not tracking or disk.get('smart_at') != previous.get('smart_at')):
            tracking = observe_extended_test(tracking, disk['smart'], disk.get('smart_at') or disk['observed_at'])
            write_test_tracking(db, disk['serial'], tracking)
        if changed:
            key = 'last_up' if disk['state'] == 'awake' else 'last_down'
            disk[key] = disk['observed_at']
            db.execute('INSERT INTO events VALUES (?, ?, ?)',
                       (disk['serial'], disk['observed_at'], disk['state']))
        db.execute('INSERT INTO disks VALUES (?, ?) '
                   'ON CONFLICT(serial) DO UPDATE SET payload = excluded.payload',
                   (disk['serial'], json.dumps(disk)))
        db.execute('DELETE FROM events WHERE observed_at < ?', (time.time() - 365 * 86400,))


def stored():
    with connect() as db:
        return {serial: json.loads(payload) for serial, payload in db.execute('SELECT * FROM disks ORDER BY serial')}


def warning_issues(smart):
    if 'issues' in smart:
        return smart['issues']
    # Older caches remain readable without waking sleeping disks on upgrade.
    # Preserve unclassified legacy warnings rather than guessing them away.
    attributes = smart.get('attributes', [])
    result = []
    for index, message in enumerate(smart.get('warnings', [])):
        attr = next((a for a in attributes if message.startswith(a.get('name', '') + ':')), None)
        if attr:
            result.append({'key': f"attribute:{attr['id']}", 'attribute_id': attr['id'],
                           'raw': attr.get('raw', {}).get('value'), 'message': message,
                           'acknowledgeable': attr['id'] in ACKNOWLEDGEABLE})
        else:
            result.append({'key': f'legacy:{index}', 'message': message})
    return result


def decorate_disk(disk, acknowledgements, now):
    disk = dict(disk)
    smart = dict(disk.get('smart') or {})
    issues = []
    for original in warning_issues(smart):
        issue = dict(original)
        baseline = acknowledgements.get((disk['serial'], issue.get('attribute_id')))
        raw = issue.get('raw')
        issue['acknowledged'] = bool(issue.get('acknowledgeable') and baseline
                                     and isinstance(raw, int) and raw <= baseline['baseline'])
        if baseline:
            issue['baseline'] = baseline['baseline']
            issue['acknowledged_at'] = baseline['acknowledged_at']
            issue['increased_by'] = max(0, raw - baseline['baseline']) if isinstance(raw, int) else 0
        issues.append(issue)
    smart['issues'] = issues
    smart['active_warnings'] = [i['message'] for i in issues if not i['acknowledged']]
    smart['known_warnings'] = [i['message'] for i in issues if i['acknowledged']]
    disk['smart'] = smart
    disk['stale'] = now - disk.get('observed_at', 0) > INTERVAL * 2.5
    disk['needs_attention'] = bool(disk['stale'] or disk.get('error')
                                   or disk.get('state') not in ('awake', 'standby')
                                   or smart.get('passed') is False or smart['active_warnings'])
    disk['health_unknown'] = smart.get('passed') is None
    return disk


def acknowledge_warning(serial, attribute_id, expected_raw, clear=False):
    if isinstance(attribute_id, bool) or attribute_id not in ACKNOWLEDGEABLE:
        raise ValueError('Only cumulative warning counters can be acknowledged')
    if clear:
        with connect() as db:
            db.execute('DELETE FROM acknowledgements WHERE serial = ? AND attribute_id = ?',
                       (serial, attribute_id))
        return 'Acknowledgement removed; disk information updated.'
    disk = stored().get(serial)
    if not disk:
        raise ValueError('No collected information for this disk')
    issue = next((i for i in warning_issues(disk.get('smart') or {})
                  if i.get('attribute_id') == attribute_id), None)
    if (not issue or not isinstance(issue.get('raw'), int)
            or isinstance(expected_raw, bool) or not isinstance(expected_raw, int)
            or issue['raw'] != expected_raw):
        raise ValueError('Warning changed or is unavailable; refresh and review it first')
    with connect() as db:
        db.execute('INSERT OR REPLACE INTO acknowledgements VALUES (?, ?, ?, ?)',
                   (serial, attribute_id, issue['raw'], time.time()))
    return 'Current value acknowledged; increases will need attention again.'


def snapraid_status():
    """Read service lifecycle metadata only; never query the array."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    result = {'state': 'unknown', 'state_label': 'Not recorded',
              'last_result': None, 'result_label': 'Not recorded',
              'finished_at': None, 'finished_label': 'Not recorded'}
    try:
        raw = json.loads(Path('/host/snapraid/sync.json').read_text())
        if not isinstance(raw, dict) or raw.get('state') not in ('running', 'idle'):
            raise ValueError('Invalid sync record')
        for key in ('started_at', 'finished_at', 'last_success_at'):
            value = raw.get(key)
            if value is not None and (not isinstance(value, (int, float)) or
                                      not 0 < value <= time.time() + 5):
                raise ValueError('Invalid sync timestamp')
        result.update(raw)
        if raw['state'] == 'running':
            boot = Path('/host/boot_id').read_text().strip()
            if not boot or boot != raw.get('boot_id'):
                result.update(state='interrupted', state_label='Interrupted by reboot')
            else:
                result['state_label'] = 'Running'
        else:
            result['state_label'] = 'Idle'
        last = raw.get('last_result')
        result['result_label'] = ('Succeeded' if last == 'success' else
                                  'Not recorded' if last is None else f'Failed: {last}')
        if raw.get('finished_at'):
            result['finished_label'] = datetime.fromtimestamp(
                raw['finished_at'], ZoneInfo('America/Los_Angeles')).strftime('%b %d, %Y %I:%M %p %Z')
    except FileNotFoundError:
        mounted = os.path.ismount('/host/snapraid')
        result.update(state='unknown',
                      state_label='Awaiting first recorded sync' if mounted else 'Status mount missing',
                      detail='History begins with the next service run.' if mounted else
                      'Add the SnapRAID status bind mount to Compose and recreate the container.')
    except (OSError, ValueError, TypeError, OverflowError):
        # Preserve historical information but never report an unverified run.
        result.update(state='unknown', state_label='Status unavailable')
    return result


def snapraid_schedules():
    schedules = {}
    for kind in ('sync', 'scrub'):
        try:
            text = Path(f'/host/systemd/snapraid-{kind}.timer').read_text()
            calendars = re.findall(r'^OnCalendar=(.+)$', text, re.MULTILINE)
            labels = {'*-*-* 03:30:00 America/Los_Angeles': 'Daily at 3:30 AM Pacific',
                      '*-*-01 05:00:00 America/Los_Angeles': 'First of each month at 5:00 AM Pacific'}
            schedules[kind] = {'calendar': calendars,
                               'label': '; '.join(labels.get(c, c) for c in calendars) or 'Not configured'}
        except OSError:
            schedules[kind] = {'calendar': [], 'label': 'Timer file unavailable (check Compose mounts)'}
    return schedules


def filesystem_capacity(path, now):
    # Reject an absent mount: never accidentally report the container OS disk.
    if not os.path.ismount(path):
        return {'status': 'unmounted'}
    try:
        stat = os.statvfs(path)
        return {'status': 'available', 'observed_at': now,
                'total_bytes': stat.f_blocks * stat.f_frsize,
                'used_bytes': (stat.f_blocks - stat.f_bfree) * stat.f_frsize,
                'free_bytes': stat.f_bfree * stat.f_frsize,
                'available_bytes': stat.f_bavail * stat.f_frsize}
    except OSError as exc:
        return {'status': 'unavailable', 'error': str(exc)}


def refresh_capacity(config, disk, now):
    label = config.get('label', '')
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', label):
        return disk
    path = '/host/disks/' + label
    if not os.path.ismount(path):
        disk['capacity'] = {'status': 'unmounted'}
    elif disk['state'] == 'awake':
        disk['capacity'] = filesystem_capacity(path, now)
    # Sleeping disks retain their previous capacity without filesystem calls.
    return disk


def pool_capacity(disks, now):
    with connect() as db:
        row = db.execute("SELECT payload FROM metadata WHERE name='pool_capacity'").fetchone()
    result = json.loads(row[0]) if row else {'status': 'not_collected'}
    if not os.path.ismount('/host/pool'):
        result = {'status': 'unavailable', 'error': 'Pool bind mount missing. Add /mnt/pool:/host/pool:ro to Compose and recreate the container.'}
    result['cached'] = now - result.get('observed_at', 0) > INTERVAL * 2
    for key in ('total', 'used', 'free', 'available'):
        value = result.get(key + '_bytes')
        result[key + '_label'] = f'{value / 2 ** 40:.2f} TiB' if value is not None else 'Unavailable'
    return result


def refresh_pool_capacity(disks, now):
    branches = [d for d in disks if d.get('role') == 'Pool data']
    # Conservatively skip mergerfs statfs while a configured member is asleep.
    if not branches or any(d.get('state') != 'awake' for d in branches):
        return
    result = filesystem_capacity('/host/pool', now)
    with connect() as db:
        db.execute("INSERT INTO metadata VALUES ('pool_capacity', ?) "
                   "ON CONFLICT(name) DO UPDATE SET payload=excluded.payload", (json.dumps(result),))


def snapshot():
    disks = stored()
    now = time.time()
    # UI order comes from inventory, never SQLite row/update order.
    if CONFIGS:
        disks = {c['serial']: disks.get(c['serial'], {
            **c, 'state': 'unknown', 'observed_at': 0,
            'error': 'Awaiting first observation'}) for c in CONFIGS}
    with connect() as db:
        extended_tests = {s: json.loads(p) for s, p in db.execute('SELECT * FROM extended_tests')}
        acknowledgements = {(s, a): {"baseline": b, "acknowledged_at": t}
                            for s, a, b, t in db.execute("SELECT * FROM acknowledgements")}
        events = [{'serial': s, 'observed_at': t, 'state': state}
                  for s, t, state in db.execute(
                      'SELECT * FROM events ORDER BY observed_at DESC LIMIT 100')]
    for serial, disk in disks.items():
        disk['extended_test'] = extended_tests.get(serial, {})
    return {'demo': DEMO, 'poll_seconds': INTERVAL, 'smart_seconds': SMART_INTERVAL,
            'server_time': now, 'disks': [decorate_disk(d, acknowledgements, now) for d in disks.values()], 'events': events,
            'schedules': snapraid_schedules(), 'pool': pool_capacity(list(disks.values()), now),
            'snapraid': snapraid_status(), 'job': job_snapshot(), 'controls_enabled': bool(ACTION_TOKEN and ALLOWED_HOSTS) and not DEMO}


def collector_freshness(payload):
    observations = [d.get('observed_at', 0) for d in payload['disks']]
    interval = payload.get('poll_seconds', INTERVAL)
    now = payload['server_time']
    missing = not observations or any(not t for t in observations)
    future = any(t > now + 5 for t in observations)
    age = max((now - t for t in observations), default=0)
    state = ('stale' if missing or future or age > interval * 3 else
             'delayed' if age > interval * 2 else 'current')
    labels = {'current': '🟢 Current', 'delayed': '🟠 Delayed', 'stale': '🔴 Stale'}
    if missing:
        labels['stale'] = '🔴 Awaiting data'
    return {'state': state, 'label': labels[state],
            'oldest_observation_age_seconds': max(0, age) if not missing else None}


def homepage_status(payload):
    summary = {'total': len(payload['disks']), 'awake': 0, 'sleeping': 0,
               'unknown': 0, 'attention': 0, 'known_issues': 0, 'health_unknown': 0,
               'tests_running': 0, 'stale': 0}
    disks = []
    for disk in payload['disks']:
        state = 'stale' if disk.get('stale') else disk.get('state', 'unknown')
        state_label = {'awake': 'Awake', 'standby': 'Sleeping', 'unknown': 'Unknown', 'stale': 'Stale'}.get(state, 'Unknown')
        summary[{'awake': 'awake', 'standby': 'sleeping'}.get(state, 'unknown')] += 1
        summary['stale'] += int(bool(disk.get('stale')))
        summary['attention'] += int(disk['needs_attention'])
        smart = disk.get('smart') or {}
        summary['known_issues'] += int(bool(smart.get('known_warnings')))
        summary['health_unknown'] += int(disk['health_unknown'])
        value = smart.get('test_status', {}).get('value')
        running = isinstance(value, int) and value & 0xf0 == 0xf0
        summary['tests_running'] += int(running and not disk.get('stale') and state == 'awake')
        health = ('Needs attention' if disk['needs_attention'] else
                  'Health unknown' if disk['health_unknown'] else
                  'Known issue' if smart.get('known_warnings') else 'Healthy')
        description = f'{state_label} · {health}'
        if running:
            current = state == 'awake' and not disk.get('stale')
            description += ' · ' + ('Testing' if current else 'Last read: testing')
        disks.append({'label': disk['label'], 'serial': disk['serial'],
                      'state': state, 'health': health, 'description': description,
                      'temperature': smart.get('temperature'),
                      'health_read_at': disk.get('smart_at'),
                      'observed_at': disk.get('observed_at'),
                      'extended_test': disk.get('extended_test', {}),
                      'capacity': disk.get('capacity', {}),
                      'needs_attention': disk['needs_attention']})
    return {'demo': payload['demo'], 'summary': summary, 'disks': disks,
            'snapraid': payload.get('snapraid', {}),
            'schedules': payload.get('schedules', {}), 'pool': payload.get('pool', {}),
            'freshness': collector_freshness(payload),
            'generated_at': payload['server_time']}


def job_snapshot():
    with JOB_LOCK:
        return dict(JOB) if JOB else None


def submit_job(action, serial=None, wake=False, attribute_id=None, expected_raw=None):
    global JOB
    if action not in ('refresh', 'spin-up', 'spin-down', 'health', 'test', 'acknowledge', 'unacknowledge'):
        raise ValueError('Unknown action')
    if action != 'refresh' and serial not in {c['serial'] for c in CONFIGS}:
        raise ValueError('Unknown disk serial')
    with JOB_LOCK:
        if JOB and JOB['state'] in ('queued', 'running'):
            raise ValueError('An action is already running; please wait')
        JOB = {'id': secrets.token_hex(8), 'action': action, 'serial': serial,
               'state': 'queued', 'message': 'Queued', 'wake': wake,
               'attribute_id': attribute_id, 'expected_raw': expected_raw}
        JOBS.put_nowait(dict(JOB))
        return dict(JOB)


def verify_unmounted(config, path):
    if not config.get('allow_spindown', False):
        raise ValueError('Spin-down is enabled only for configured spare disks')
    # Container mount tables cannot establish whether the HOST uses a disk.
    real = Path('/sys/class/block') / Path(os.path.realpath(path)).name
    nodes = [real] + [p for p in real.iterdir() if (p / 'partition').exists()]
    devices = {p.joinpath('dev').read_text().strip() for p in nodes}
    for p in nodes:
        if any(p.joinpath('holders').iterdir()):
            raise ValueError('Disk has active kernel holders; spin-down refused')
    mounts = Path('/host/mountinfo').read_text().splitlines()
    if not mounts:
        raise ValueError('Host mount information is unavailable')
    if any(line.split()[2] in devices for line in mounts):
        raise ValueError('Disk or a partition is mounted on the NAS')
    for line in Path('/host/swaps').read_text().splitlines()[1:]:
        # /proc/swaps names host paths; host device nodes live under DEV_DIR.
        info = os.stat(re.sub(r'^/dev(?=/)', DEV_DIR, line.split()[0]))
        if f'{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}' in devices:
            raise ValueError('Disk is used for swap')


def host_allowed(header):
    # Host may carry a port; IPv6 literals are bracketed. Anything else
    # (userinfo, paths, empty) is refused.
    match = re.fullmatch(r'(\[[0-9a-fA-F:.]+\]|[A-Za-z0-9.-]+)(:\d+)?', header or '')
    return bool(match) and match[1].lower().strip('[]') in ALLOWED_HOSTS


def wake_disk(path):
    code, output = command(['dd', 'if=' + path, 'of=/dev/null', 'bs=4096',
                            'count=1', 'iflag=direct', 'status=none'])
    if code:
        raise ValueError('Disk wake failed: ' + output[:300])


def smart_probe(config, path):
    code, output = command(['smartctl', '-d', config.get('smart_type', 'ata'),
                            '-n', 'standby,3,5', '-a', '-j', path])
    if code == 3:
        return None
    if code & 7:
        raise ValueError('Cannot safely read SMART/test status')
    return json.loads(output)


def test_running(data):
    status = data.get('ata_smart_data', {}).get('self_test', {}).get('status', {})
    value = status.get('value')
    if not isinstance(value, int):
        raise ValueError('Self-test status unavailable; action refused')
    return value & 0xf0 == 0xf0


def execute_job(job):
    if job['action'] in ('acknowledge', 'unacknowledge'):
        return acknowledge_warning(job['serial'], job.get('attribute_id'),
                                   job.get('expected_raw'), job['action'] == 'unacknowledge')
    if job['action'] == 'refresh':
        errors = []
        for config in CONFIGS:
            previous = stored().get(config['serial'], {})
            disk = collect(config, previous, time.time(), force_smart=True)
            save(disk, previous)
            if disk.get('error'):
                errors.append(config['label'] + ': ' + disk['error'])
        if errors:
            raise ValueError('; '.join(errors))
        return 'Updated disk states and awake-disk health; sleeping disks were skipped.'
    config = next(c for c in CONFIGS if c['serial'] == job['serial'])
    path = device_for(config['serial'])
    if not path:
        raise ValueError('Disk not found')
    previous = stored().get(config['serial'], {})
    # Take a real pre-action observation rather than relying on cached state.
    before = collect(config, previous, time.time())
    save(before, previous)
    if before['state'] == 'unknown':
        raise ValueError(before.get('error') or 'Power state unknown')
    action = job['action']
    try:
        if action == 'spin-up':
            wake_disk(path)
        elif action == 'spin-down':
            verify_unmounted(config, path)
            if before['state'] == 'awake':
                data = smart_probe(config, path)
                if data is not None and test_running(data):
                    raise ValueError('Self-test is running; spin-down refused')
            # Recheck mounts immediately before sending standby.
            verify_unmounted(config, path)
            code, output = command(['hdparm', '-y', path])
            if code:
                raise ValueError('Standby request failed: ' + output[:300])
        elif action in ('health', 'test'):
            if before['state'] == 'standby':
                if not job['wake']:
                    raise ValueError('Disk is asleep; confirm waking it to continue')
                wake_disk(path)
            if action == 'test':
                data = smart_probe(config, path)
                if data is None:
                    raise ValueError('Disk returned to standby; retry')
                if test_running(data):
                    raise ValueError('A SMART self-test is already running')
                requested_at = time.time()
                code, output = command(['smartctl', '-d', config.get('smart_type', 'ata'),
                                        '-n', 'standby,3,5', '-t', 'long', '-j', path])
                if code & 7:
                    raise ValueError('Extended self-test could not be started')
                record_extended_start(config['serial'], summarize_smart(data, 0), requested_at)
    finally:
        # Always return observed results, including after a failed command.
        after = collect(config, before, time.time(), force_smart=True)
        save(after, before)
    if after.get('error'):
        raise ValueError(after['error'])
    expected = {'spin-up': 'awake', 'spin-down': 'standby'}.get(action)
    if expected and after['state'] != expected:
        raise ValueError('Requested state was not observed; disk may be in use')
    return ('Extended self-test requested; the drive runs it in the background. '
            'Refresh health to see progress/results.' if action == 'test'
            else 'Action finished; disk information updated.')


def run_collector(configs):
    global JOB
    next_poll = 0
    while not STOP.is_set():
        try:
            job = JOBS.get(timeout=max(0, next_poll - time.monotonic()))
        except queue.Empty:
            job = None
        if job:
            with JOB_LOCK:
                JOB.update(state='running', message='Running')
            try:
                message = execute_job(job)
                state = 'done'
            except Exception as exc:
                logging.exception('Disk action failed')
                message, state = str(exc), 'failed'
            with JOB_LOCK:
                JOB.update(state=state, message=message)
            JOBS.task_done()
        else:
            previous = stored()
            for i, config in enumerate(configs):
                old = previous.get(config['serial'], {})
                if DEMO:
                    now = time.time()
                    disk = {**old, **config, 'observed_at': now, 'state': 'awake' if i < 3 else 'standby',
                            'error': None, 'smart_at': now - (7200 if i >= 3 else 0),
                            'smart': {'passed': True, 'temperature': 32 + i, 'warnings': [],
                                      'counters': {'Start_Stop_Count': 120 + i, 'Load_Cycle_Count': 800 + i}}}
                else:
                    disk = collect(config, old, time.time())
                save(disk, old)
            if not DEMO:
                refresh_pool_capacity(list(stored().values()), time.time())
            next_poll = time.monotonic() + INTERVAL


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path not in ('/api/action', '/api/unlock'):
            self.send_error(404)
            return
        # JSON plus a custom header prevents form-based cross-origin requests;
        # no CORS support is provided. Also reject foreign browser origins.
        origin = self.headers.get('Origin')
        same_origin = (host_allowed(self.headers.get('Host'))
                       and (not origin or urlsplit(origin).netloc == self.headers.get('Host')))
        if self.path == '/api/unlock':
            # This is a deliberate-click guard, not user authentication.
            # Browsers cannot obtain the capability through a cross-origin POST.
            if (DEMO or not ACTION_TOKEN or not same_origin
                    or self.headers.get('Sec-Fetch-Site') == 'cross-site'
                    or self.headers.get('Content-Type') != 'application/json'
                    or self.headers.get('X-Requested-With') != 'DiskStatusPage'):
                self.respond_json(403, {'error': 'Unlock from the dashboard'})
                return
            self.respond_json(200, {'token': ACTION_TOKEN})
            return
        supplied = self.headers.get('X-Action-Token', '')
        if (DEMO or not ACTION_TOKEN or not same_origin
                or not hmac.compare_digest(supplied, ACTION_TOKEN)):
            self.respond_json(403, {'error': 'Choose Unlock controls on the dashboard'})
            return
        try:
            if self.headers.get('Content-Type') != 'application/json':
                raise ValueError('JSON required')
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 4096:
                raise ValueError('Invalid request size')
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError('Object required')
            job = submit_job(body.get('action'), body.get('serial'), body.get('wake') is True,
                             body.get('attribute_id'), body.get('expected_raw'))
            self.respond_json(202, job)
        except (ValueError, TypeError) as exc:
            self.respond_json(400, {'error': str(exc)})

    def respond_json(self, code, value):
        body = json.dumps(value).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        if self.path.split('?')[0] != '/healthz':
            self.send_error(405)
            return
        freshness = collector_freshness(snapshot())
        self.send_response(200 if freshness['state'] == 'current' else 503)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_GET(self):
        path = self.path.split('?')[0]
        if path in ('/api/status', '/api/homepage', '/healthz'):
            payload = snapshot()
            if path == '/healthz':
                freshness = collector_freshness(payload)
                fresh = freshness['state'] == 'current'
                self.send_response(200 if fresh else 503)
                body = json.dumps({'collector_recent': fresh, 'freshness': freshness}).encode()
            else:
                self.send_response(200)
                body = json.dumps(homepage_status(payload) if path == '/api/homepage' else payload).encode()
            mime = 'application/json'
        elif path in ('/', '/app.js', '/style.css'):
            file = ROOT / 'static' / ('index.html' if path == '/' else path[1:])
            body = file.read_bytes()
            mime = {'/': 'text/html', '/app.js': 'text/javascript', '/style.css': 'text/css'}[path]
            self.send_response(200)
        else:
            self.send_error(404)
            return
        self.send_header('Content-Type', mime + '; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; frame-ancestors 'none'")
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    configs = json.loads(Path(os.getenv('DISKS_FILE', str(ROOT / 'disks.json'))).read_text())
    initialize()
    CONFIGS = configs
    ACTION_TOKEN = '' if DEMO else load_token()
    if not ALLOWED_HOSTS and not DEMO:
        logging.warning('ALLOWED_HOSTS is not set; disk controls stay disabled')
    def worker():
        try:
            run_collector(configs)
        except Exception:
            logging.exception('Collector stopped; health endpoint will become unhealthy')
    threading.Thread(target=worker, daemon=True).start()
    ThreadingHTTPServer(('0.0.0.0', int(os.getenv('PORT', '8080'))), Handler).serve_forever()
