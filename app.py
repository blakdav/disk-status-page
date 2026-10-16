"""Disk Status Page: ATA monitoring and explicit disk controls and a small HTTP dashboard."""
import glob
from contextlib import contextmanager
import json
import hmac
import secrets
import queue
import socket
from urllib.parse import parse_qs, urlsplit
import logging
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).parent
INTERVAL = max(10, int(os.getenv('POLL_SECONDS', '60')))
SMART_INTERVAL = max(INTERVAL, int(os.getenv('SMART_SECONDS', '1800')))
DEMO = os.getenv('DEMO', 'false').lower() == 'true'
# The sync timer runs daily; no successful sync for longer than this means
# runs are failing to start or happen, and /healthz/sync turns unhealthy.
SYNC_MAX_AGE = float(os.getenv('SYNC_MAX_AGE_HOURS', '30')) * 3600
STOP = threading.Event()
DB = Path(os.getenv('DATA_DIR', '/data')) / 'history.sqlite3'
# The host's /dev. Compose mounts it at /host/dev so the container keeps its
# own /dev; udev's by-id links are relative, so they resolve inside it.
DEV_DIR = os.getenv('DEV_DIR', '/dev').rstrip('/')
# Host names the dashboard may be reached by. Disk controls answer only
# requests addressed to one of these, which stops DNS rebinding: a foreign
# page pointed at the NAS's IP still sends its own name as Host.
ALLOWED_HOSTS = {h.strip().lower() for h in os.getenv('ALLOWED_HOSTS', '').split(',') if h.strip()}
# Placed in the history folder by the host's provisioning, only after it has
# checked the storage is mounted. Its absence means /data is not the intended
# storage (for example an empty folder under an unmounted drive). Empty
# disables the check.
DATA_MARKER = os.getenv('DATA_MARKER', '.vault-marker')
WARNINGS = {5, 187, 188, 197, 198, 199}
COUNTERS = {4, 12, 193}
# Acknowledge cumulative counters, never current pending/unreadable sectors.
ACKNOWLEDGEABLE = {5, 187, 188, 199}
CONFIGS = []
# The backup jobs to show (config/backups.json, written by the NAS's
# Ansible) and where their systemd hooks' records are mounted. Optional: a
# missing or bad list only affects the Backups section.
BACKUP_JOBS = []
BACKUP_LIST_ERROR = None
BACKUP_DIR = '/host/backups'
# The Synology's log, from DSM's Log Center > Log Sending (UDP, IETF/RFC
# 5424 format). Only datagrams from these addresses are heard; empty turns
# the listener off. Log Sending forwards Log Center's events, among them
# "Internal disks woke up from hibernation." from System. DSM logs nothing
# when the disks go to sleep. Every line received is also kept, as text,
# in synology.log beside the history database.
SYNOLOGY_ADDRESSES = {a.strip() for a in os.getenv('SYNOLOGY_ADDRESS', '').split(',') if a.strip()}
SYNOLOGY_PORT = int(os.getenv('SYNOLOGY_SYSLOG_PORT', '5140'))
SYNOLOGY_LOG = DB.parent / 'synology.log'
# synology.log is renamed to synology.log.1 (replacing it) at this size.
SYNOLOGY_LOG_MAX = int(float(os.getenv('SYNOLOGY_LOG_MB', '20')) * 2 ** 20)
SYNOLOGY_LOG_LOCK = threading.Lock()
# <PRI>1 TIMESTAMP HOST APP-NAME PROCID MSGID, then structured data and MSG.
SYSLOG_HEADER = re.compile(r'<\d{1,3}>1 (\S+) (\S+) (\S+) \S+ \S+ ')
SYNOLOGY_WAKE = re.compile(r'woke up from hibernation', re.IGNORECASE)
SYNOLOGY_RETENTION = 400 * 86400
# What a disk should be doing, from its configured role (a cold spare should
# be asleep, a pool disk awake), and the power state that matches it.
EXPECTED_STATES = {'awake': 'awake', 'asleep': 'standby'}
# ata: SATA disks behind the HBA, with power states and controls. nvme: the
# NVMe drives, always active, monitored for health, wear and capacity only.
DISK_TYPES = ('ata', 'nvme')
# NVMe "percentage used" is the drive's own estimate of its rated endurance
# consumed. It can pass 100; warn well before that.
NVME_WEAR_WARNING = 80
# NVMe critical warning bits (NVMe base specification, SMART/Health log).
NVME_CRITICAL_BITS = (
    (0, 'Spare capacity below threshold'),
    (1, 'Temperature outside its safe range'),
    (2, 'Reliability degraded by media or internal errors'),
    (3, 'Drive switched to read-only'),
    (4, 'Volatile memory backup failed'),
)
ACTION_TOKEN = ''
JOBS = queue.Queue(maxsize=1)
JOB_LOCK = threading.Lock()
JOB = None


def load_configs(path):
    """Read the disk list (written by the NAS's Ansible) and refuse a bad one."""
    configs = json.loads(Path(path).read_text())
    if not isinstance(configs, list) or not configs:
        raise ValueError('the disk list must be a non-empty JSON array')
    serials = set()
    for config in configs:
        if not isinstance(config, dict):
            raise ValueError('each disk must be a JSON object')
        for key in ('label', 'serial', 'role'):
            if not isinstance(config.get(key), str) or not config[key].strip():
                raise ValueError(f'each disk needs a non-empty "{key}"')
        # An empty or duplicate serial would match the wrong disk.
        if config['serial'] in serials:
            raise ValueError(f'serial {config["serial"]} is listed twice')
        serials.add(config['serial'])
        if config.get('expected_state') not in (None, *EXPECTED_STATES):
            raise ValueError(f'{config["label"]}: expected_state must be "awake" or "asleep"')
        if not isinstance(config.get('allow_spindown', False), bool):
            raise ValueError(f'{config["label"]}: allow_spindown must be true or false')
        if config.get('type', 'ata') not in DISK_TYPES:
            raise ValueError(f'{config["label"]}: type must be "ata" or "nvme"')
        # NVMe drives never sleep and take no disk controls.
        if config.get('type') == 'nvme' and (config.get('expected_state') or config.get('allow_spindown')):
            raise ValueError(f'{config["label"]}: an NVMe drive has no expected power state or spin-down')
        # Only a path under the container's read-only host mounts.
        if 'capacity_path' in config and not (isinstance(config['capacity_path'], str) and
                                              re.fullmatch(r'/host/[A-Za-z0-9_/-]+', config['capacity_path'])):
            raise ValueError(f'{config["label"]}: capacity_path must be a path under /host/')
    return configs


def load_backup_jobs(path):
    """Read the backup list and refuse a bad one."""
    jobs = json.loads(Path(path).read_text())
    if not isinstance(jobs, list):
        raise ValueError('the backup list must be a JSON array')
    ids = set()
    for job in jobs:
        if not isinstance(job, dict):
            raise ValueError('each backup job must be a JSON object')
        # ids name status files and units name timer files: no paths.
        for key in ('id', 'unit'):
            if not isinstance(job.get(key), str) or not re.fullmatch(r'[a-z0-9-]+', job[key]):
                raise ValueError(f'each backup job needs a "{key}" of lowercase letters, digits and dashes')
        if job['id'] in ids:
            raise ValueError(f'backup job {job["id"]} is listed twice')
        ids.add(job['id'])
        for key in ('source', 'destination'):
            if not isinstance(job.get(key), str) or not job[key].strip():
                raise ValueError(f'{job["id"]}: needs a non-empty "{key}"')
        if 'name' in job and (not isinstance(job['name'], str) or not job['name'].strip()):
            raise ValueError(f'{job["id"]}: "name" must be non-empty text')
        age = job.get('max_age_hours')
        if isinstance(age, bool) or not isinstance(age, (int, float)) or age <= 0:
            raise ValueError(f'{job["id"]}: max_age_hours must be a positive number')
        if job.get('method') not in (None, 'restic', 'rsync'):
            raise ValueError(f'{job["id"]}: method must be "restic" or "rsync"')
    return jobs


def require_data_marker():
    if DATA_MARKER and not (DB.parent / DATA_MARKER).is_file():
        raise SystemExit(f'{DB.parent / DATA_MARKER} is missing: the history storage is '
                         'not mounted or not provisioned. Refusing to write history; '
                         'Docker will retry.')


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
        db.execute('CREATE TABLE IF NOT EXISTS synology_wakes (woke_at REAL PRIMARY KEY)')


def command(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=20)
    return result.returncode, result.stdout + result.stderr


def device_for(serial, kind='ata'):
    # Resolve existing udev links only; discovery must never probe a disk.
    # NVMe links also exist as <model>_<serial>_1 (namespace 1), which this
    # pattern skips, as it does the eui.* links.
    candidates = glob.glob(f'{DEV_DIR}/disk/by-id/{kind}-*_' + glob.escape(serial))
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


def summarize_nvme(data, status):
    log = data.get('nvme_smart_health_information_log', {})
    issues = []

    def issue(key, message):
        issues.append({'key': key, 'message': message})

    critical = log.get('critical_warning') or 0
    for bit, message in NVME_CRITICAL_BITS:
        if critical & (1 << bit):
            issue(f'critical:{bit}', message)
    passed = data.get('smart_status', {}).get('passed')
    if status & 8 or passed is False:
        issue('health-failure', 'SMART overall health failure')
    used = log.get('percentage_used')
    if isinstance(used, int) and used >= NVME_WEAR_WARNING:
        issue('wear', f'{used}% of rated endurance used')
    spare, threshold = log.get('available_spare'), log.get('available_spare_threshold')
    if isinstance(spare, int) and isinstance(threshold, int) and spare <= threshold:
        issue('spare', f'Available spare {spare}% is at or below its {threshold}% threshold')
    if log.get('media_errors'):
        issue('media-errors', f"{log['media_errors']} media and data integrity errors")
    # The error information log is deliberately not a warning: many drives
    # record harmless entries there (unsupported commands from tools).
    units = log.get('data_units_written')
    return {'passed': passed, 'temperature': data.get('temperature', {}).get('current'),
            'warnings': [i['message'] for i in issues], 'issues': issues,
            'wear': {'percentage_used': used, 'available_spare': spare,
                     'available_spare_threshold': threshold,
                     # One data unit is 1,000 512-byte blocks.
                     'written_bytes': units * 512000 if isinstance(units, int) else None,
                     'power_on_hours': log.get('power_on_hours'),
                     'unsafe_shutdowns': log.get('unsafe_shutdowns'),
                     'media_errors': log.get('media_errors'),
                     'error_log_entries': log.get('num_err_log_entries'),
                     'warning_temperature_minutes': log.get('warning_temp_time'),
                     'critical_temperature_minutes': log.get('critical_comp_time')}}


def collect_nvme(config, previous, now, force_smart=False):
    # NVMe drives have no standby to protect, so there is no power-state
    # check; "awake" here means present and answering.
    disk = {**previous, **config, 'observed_at': now, 'error': None}
    path = device_for(config['serial'], 'nvme')
    disk['device'] = path
    disk['state'] = 'unknown'
    if not path:
        disk['error'] = 'Drive not found by serial; check device access and udev links.'
        return disk
    disk['state'] = 'awake'
    try:
        if force_smart or now - previous.get('smart_at', 0) >= SMART_INTERVAL:
            code, output = command(['smartctl', '-d', 'nvme', '-a', '-j', path])
            if code & 7:
                disk['error'] = 'SMART read failed; cached values retained.'
            else:
                disk['smart'] = summarize_nvme(json.loads(output), code)
                disk['smart_at'] = now
        stat = Path('/sys/class/block') / Path(os.path.realpath(path)).name / 'stat'
        if stat.exists():
            values = stat.read_text().split()
            disk['io'] = {'reads': int(values[0]), 'writes': int(values[4])}
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        disk['error'] = str(exc)
    return refresh_capacity(config, disk, now)


def collect(config, previous, now, force_smart=False):
    if config.get('type') == 'nvme':
        return collect_nvme(config, previous, now, force_smart)
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
    # Only a current, known power state can contradict the expected one.
    expected = EXPECTED_STATES.get(disk.get('expected_state'))
    disk['unexpected_state'] = bool(expected and not disk['stale']
                                    and disk.get('state') in ('awake', 'standby')
                                    and disk['state'] != expected)
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


def read_lifecycle(path):
    """A systemd hook record (state, times, result), or ValueError if it is
    malformed. Raises FileNotFoundError before the first recorded run."""
    raw = json.loads(Path(path).read_text())
    if not isinstance(raw, dict) or raw.get('state') not in ('running', 'idle'):
        raise ValueError('Invalid lifecycle record')
    for key in ('started_at', 'finished_at', 'last_success_at'):
        value = raw.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or
                                  not 0 < value <= time.time() + 5):
            raise ValueError('Invalid lifecycle timestamp')
    return raw


def interrupted(raw):
    """A record still saying running from before the NAS last booted."""
    boot = Path('/host/boot_id').read_text().strip()
    return not boot or boot != raw.get('boot_id')


def pacific(value, year=True):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.fromtimestamp(value, ZoneInfo('America/Los_Angeles')).strftime(
        '%b %d, %Y %I:%M %p %Z' if year else '%b %-d, %-I:%M %p')


def bytes_label(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    for unit, power in (('TiB', 4), ('GiB', 3), ('MiB', 2), ('KiB', 1)):
        if value >= 2 ** (10 * power):
            return f'{value / 2 ** (10 * power):.1f} {unit}'
    return f'{value:.0f} B'


def snapraid_status():
    """Read service lifecycle metadata only; never query the array."""
    result = {'state': 'unknown', 'state_label': 'Not recorded',
              'last_result': None, 'result_label': 'Not recorded',
              'finished_at': None, 'finished_label': 'Not recorded'}
    try:
        raw = read_lifecycle('/host/snapraid/sync.json')
        result.update(raw)
        if raw['state'] == 'running':
            if interrupted(raw):
                result.update(state='interrupted', state_label='Interrupted by reboot')
            else:
                result['state_label'] = 'Running'
        else:
            result['state_label'] = 'Idle'
        last = raw.get('last_result')
        result['result_label'] = ('Succeeded' if last == 'success' else
                                  'Not recorded' if last is None else f'Failed: {last}')
        if raw.get('finished_at'):
            result['finished_label'] = pacific(raw['finished_at'])
    except FileNotFoundError:
        mounted = os.path.ismount('/host/snapraid')
        result.update(state='unknown', awaiting_first=mounted,
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


def calendar_label(calendar):
    """Plain words for the OnCalendar forms the NAS uses; anything else
    is shown as written."""
    match = re.fullmatch(r'(?:(Mon|Tue|Wed|Thu|Fri|Sat|Sun) )?\*-\*-(\*|\d{2}) (\d{2}):(\d{2}):\d{2}(?: \S+)?', calendar)
    if not match or (match[1] and match[2] != '*'):
        return calendar
    hour, minute = int(match[3]), match[4]
    at = f'{(hour % 12) or 12}:{minute} {"AM" if hour < 12 else "PM"}'
    days = {'Mon': 'Mondays', 'Tue': 'Tuesdays', 'Wed': 'Wednesdays', 'Thu': 'Thursdays',
            'Fri': 'Fridays', 'Sat': 'Saturdays', 'Sun': 'Sundays'}
    if match[1]:
        return f'{days[match[1]]} at {at}'
    if match[2] == '*':
        return f'Daily at {at}'
    return f'Monthly on day {int(match[2])} at {at}'


def timer_label(unit):
    try:
        text = Path(f'/host/systemd/{unit}.timer').read_text()
    except OSError:
        return 'Timer file unavailable'
    calendars = re.findall(r'^OnCalendar=(.+)$', text, re.MULTILINE)
    return '; '.join(calendar_label(c.strip()) for c in calendars) or 'Not scheduled'


def age_label(hours):
    return f'{hours / 24:g} days' if hours >= 48 and hours % 24 == 0 else f'{hours:g} hours'


def backup_job_status(job, mounted, now):
    entry = {key: job.get(key) for key in ('id', 'source', 'destination', 'method', 'max_age_hours')}
    entry.update(name=job.get('name') or f"{job['source']} → {job['destination']}",
                 schedule_label=timer_label(job['unit']), problem=None, restic=None,
                 max_age_label=age_label(job['max_age_hours']),
                 result_label='Not recorded', last_success_label='Never',
                 started_at=None, finished_at=None, last_success_at=None, last_result=None)
    try:
        raw = read_lifecycle(f"{BACKUP_DIR}/{job['id']}.json")
    except FileNotFoundError:
        # Only the first run creates the record; a missing mount is a fault.
        entry.update(state='waiting' if mounted else 'unknown',
                     state_label='Awaiting first run' if mounted else 'Status mount missing',
                     problem=None if mounted else 'Status mount missing')
        entry['description'] = entry['state_label']
        return entry
    except (OSError, ValueError, TypeError, OverflowError):
        entry.update(state='unknown', state_label='Status unavailable', problem='Status unavailable')
        entry['description'] = entry['state_label']
        return entry
    entry.update({key: raw.get(key) for key in ('started_at', 'finished_at', 'last_success_at', 'last_result')})
    if isinstance(raw.get('restic'), dict):
        restic = dict(raw['restic'])
        restic['data_added_label'] = bytes_label(restic.get('data_added'))
        entry['restic'] = restic
    last = raw.get('last_result')
    entry['result_label'] = ('Succeeded' if last == 'success' else
                             'Not recorded' if last is None else f'Failed ({last})')
    success = raw.get('last_success_at')
    if success:
        entry['last_success_label'] = pacific(success, year=False)
    try:
        lost = raw['state'] == 'running' and interrupted(raw)
    except OSError:
        lost = False
    age = job['max_age_hours']
    if lost:
        entry.update(state='problem', state_label='Interrupted by a reboot',
                     problem='Interrupted by a reboot')
    elif raw['state'] == 'running':
        # A run in progress is never overdue: the first PBS upload takes days.
        entry.update(state='running', state_label='Running since ' + pacific(raw['started_at'], year=False)
                     if raw.get('started_at') else 'Running')
    elif last not in (None, 'success'):
        entry.update(state='problem', state_label='Last run failed', problem=f'Last run failed ({last})')
    elif not success or now - success > age * 3600:
        entry.update(state='problem', state_label='Overdue', problem=f'No success in {age_label(age)}')
    else:
        entry.update(state='ok', state_label='OK')
    if entry['state'] == 'running':
        entry['description'] = entry['state_label']
    elif entry['problem']:
        entry['description'] = entry['problem'] + (f" · last success {entry['last_success_label']}" if success else '')
    else:
        added = (entry['restic'] or {}).get('data_added_label')
        entry['description'] = f"OK · {entry['last_success_label']}" + (f' · +{added}' if added else '')
    return entry


def backup_status(now):
    if BACKUP_LIST_ERROR:
        return {'configured': False, 'error': BACKUP_LIST_ERROR, 'jobs': []}
    mounted = os.path.isdir(BACKUP_DIR)
    return {'configured': bool(BACKUP_JOBS), 'error': None,
            'jobs': [backup_job_status(job, mounted, now) for job in BACKUP_JOBS]}


def backups_health(backups):
    """Healthy when every job ran recently and its last run succeeded, or it
    is running, or has not had its first run yet."""
    problems = [f"{job['name']}: {job['problem']}" for job in backups['jobs'] if job['problem']]
    if backups.get('error'):
        problems.append('Backup list unreadable: ' + backups['error'])
    return {'ok': not problems, 'problems': problems}


def parse_syslog(line):
    """{logged_at, host, app, message} for an RFC 5424 line, or None."""
    match = SYSLOG_HEADER.match(line)
    if not match:
        return None
    try:
        logged_at = datetime.fromisoformat(match[1]).timestamp()
    except ValueError:
        return None
    rest = line[match.end():]
    # Structured data: "-", or [elements] whose values escape "]" as "\]".
    if rest.startswith('-'):
        i = 1
    else:
        i = 0
        while i < len(rest) and rest[i] == '[':
            i += 1
            while i < len(rest) and rest[i] != ']':
                i += 2 if rest[i] == '\\' else 1
            i += 1
    # DSM starts the message with a UTF-8 byte order mark, as RFC 5424 allows.
    message = rest[i:].removeprefix(' ').removeprefix('\ufeff')
    return {'logged_at': logged_at, 'host': match[2], 'app': match[3], 'message': message}


def printable(text):
    return ''.join(c if c.isprintable() else ' ' for c in text)[:2000]


def append_synology_log(text):
    with SYNOLOGY_LOG_LOCK:
        try:
            if SYNOLOGY_LOG.stat().st_size >= SYNOLOGY_LOG_MAX:
                SYNOLOGY_LOG.replace(SYNOLOGY_LOG.with_name(SYNOLOGY_LOG.name + '.1'))
        except FileNotFoundError:
            pass
        with SYNOLOGY_LOG.open('a', encoding='utf-8') as log:
            log.write(text + '\n')


def record_synology_line(line, now):
    """Keep one forwarded line, and store its wake if it reports one. True
    if it was an RFC 5424 line."""
    line = line.strip()
    if not line:
        return False
    event = parse_syslog(line)
    if not event or event['logged_at'] > now + 300:
        # Kept verbatim with the time it arrived, e.g. when Log Sending is
        # set to the BSD format by mistake.
        append_synology_log(f"{datetime.fromtimestamp(now).astimezone().isoformat(timespec='seconds')} "
                            f"(unparsed) {printable(line)}")
        return False
    stamp = datetime.fromtimestamp(event['logged_at']).astimezone().isoformat(timespec='seconds')
    append_synology_log(f"{stamp} {printable(event['host'])} {printable(event['app'])}: {printable(event['message'])}")
    with connect() as db:
        db.execute("INSERT INTO metadata VALUES ('synology', ?) "
                   "ON CONFLICT(name) DO UPDATE SET payload=excluded.payload",
                   (json.dumps({'last_line_at': event['logged_at'], 'last_received_at': now}),))
        if event['app'] == 'System' and SYNOLOGY_WAKE.search(event['message']):
            # A resent line is ignored: one wake per moment.
            db.execute('INSERT OR IGNORE INTO synology_wakes (woke_at) VALUES (?)', (event['logged_at'],))
            db.execute('DELETE FROM synology_wakes WHERE woke_at < ?', (now - SYNOLOGY_RETENTION,))
    return True


def run_synology_listener(sock):
    refused = set()
    while not STOP.is_set():
        try:
            data, (host, _) = sock.recvfrom(8192)
        except OSError:
            if sock.fileno() == -1:
                return
            continue
        if host not in SYNOLOGY_ADDRESSES:
            if host not in refused and len(refused) < 50:
                refused.add(host)
                logging.warning('Ignoring syslog from %s: not in SYNOLOGY_ADDRESS', host)
            continue
        for line in data.decode('utf-8', 'replace').splitlines():
            try:
                record_synology_line(line, time.time())
            except (OSError, sqlite3.Error, ValueError, OverflowError):
                logging.exception('Could not record Synology log line')


def synology_log_tail(lines=100):
    """The newest lines of synology.log, newest first."""
    try:
        with SYNOLOGY_LOG.open('rb') as log:
            log.seek(max(0, log.seek(0, os.SEEK_END) - 64 * 1024))
            text = log.read().decode('utf-8', 'replace')
    except FileNotFoundError:
        return []
    return text.splitlines()[-lines:][::-1] if text else []


def synology_log_bytes():
    """Both log files, oldest line first."""
    parts = []
    for path in (SYNOLOGY_LOG.with_name(SYNOLOGY_LOG.name + '.1'), SYNOLOGY_LOG):
        try:
            parts.append(path.read_bytes())
        except FileNotFoundError:
            pass
    return b''.join(parts)


def synology_log_page(query='', offset=0, limit=500):
    """A page of the whole log, newest first, optionally only the lines
    containing query (case-insensitive)."""
    lines = synology_log_bytes().decode('utf-8', 'replace').splitlines()[::-1]
    total = len(lines)
    if query:
        needle = query.casefold()
        lines = [line for line in lines if needle in line.casefold()]
    offset = max(0, offset)
    limit = min(max(1, limit), 2000)
    return {'total': total, 'matched': len(lines), 'offset': offset,
            'lines': lines[offset:offset + limit]}


def synology_status(now):
    if not SYNOLOGY_ADDRESSES:
        return {'configured': False}
    with connect() as db:
        rows = db.execute('SELECT woke_at FROM synology_wakes ORDER BY woke_at DESC LIMIT 30').fetchall()
        day, week = (db.execute('SELECT COUNT(*) FROM synology_wakes WHERE woke_at > ?',
                                (now - span,)).fetchone()[0] for span in (86400, 7 * 86400))
        meta = db.execute("SELECT payload FROM metadata WHERE name='synology'").fetchone()
    meta = json.loads(meta[0]) if meta else {}
    wakes = [{'woke_at': woke, 'woke_label': pacific(woke, year=False)} for (woke,) in rows]
    return {'configured': True, 'wakes': wakes, 'wakes_24h': day, 'wakes_7d': week,
            'last_line_at': meta.get('last_line_at'),
            'last_wake_label': wakes[0]['woke_label'] if wakes else 'None recorded',
            'log': synology_log_tail()}


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
    path = config.get('capacity_path') or '/host/disks/' + label
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
    # The current config wins over what was stored with the last observation,
    # so a role change shows as soon as the app restarts with the new list.
    if CONFIGS:
        disks = {c['serial']: {**disks.get(c['serial'], {
            'state': 'unknown', 'observed_at': 0,
            'error': 'Awaiting first observation'}), **c} for c in CONFIGS}
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
            'snapraid': snapraid_status(), 'backups': backup_status(now),
            'synology': synology_status(now), 'job': job_snapshot(), 'controls_enabled': bool(ACTION_TOKEN and ALLOWED_HOSTS) and not DEMO}


def collector_freshness(payload):
    observations = [d.get('observed_at', 0) for d in payload['disks']]
    interval = payload.get('poll_seconds', INTERVAL)
    now = payload['server_time']
    missing = not observations or any(not t for t in observations)
    future = any(t > now + 5 for t in observations)
    age = max((now - t for t in observations), default=0)
    state = ('stale' if missing or future or age > interval * 3 else
             'delayed' if age > interval * 2 else 'current')
    labels = {'current': 'Current', 'delayed': 'Delayed', 'stale': 'Stale'}
    if missing:
        labels['stale'] = 'Awaiting data'
    return {'state': state, 'label': labels[state],
            'oldest_observation_age_seconds': max(0, age) if not missing else None}


def disks_health(payload):
    """Healthy only when every disk is current, fine, and in its expected state."""
    problems = []
    freshness = collector_freshness(payload)
    if freshness['state'] != 'current':
        problems.append(f"Collector data {freshness['label'].lower()}")
    for disk in payload['disks']:
        if disk.get('needs_attention'):
            problems.append(f"{disk['label']} needs attention")
        elif disk.get('unexpected_state'):
            problems.append(f"{disk['label']} should be {disk['expected_state']}")
    return {'ok': not problems, 'problems': problems}


def sync_health(sync, now):
    """Healthy while a sync runs, before the first recorded one, or after a
    recent success; unhealthy on failure, interruption, unreadable status, or
    no success within SYNC_MAX_AGE."""
    state = sync.get('state')
    problems = []
    if state == 'running' or sync.get('awaiting_first'):
        pass
    elif state in ('interrupted', 'unknown'):
        problems.append(sync.get('state_label') or 'Sync status unavailable')
    elif sync.get('last_result') != 'success':
        problems.append('Last sync ' + (sync.get('result_label') or 'not recorded').lower())
    elif now - (sync.get('last_success_at') or 0) > SYNC_MAX_AGE:
        problems.append(f'No successful sync in {SYNC_MAX_AGE / 3600:g} hours')
    return {'ok': not problems, 'problems': problems}


def size_label(value):
    if not isinstance(value, (int, float)):
        return 'Unavailable'
    return f'{value / 2 ** 40:.2f} TiB' if value >= 2 ** 40 else f'{value / 2 ** 30:.0f} GiB'


def drive_card(disk):
    """One NVMe drive as ready-made text for a Homepage Custom API card,
    which can show fields but not compute or format them."""
    smart = disk.get('smart') or {}
    wear = smart.get('wear') or {}
    capacity = disk.get('capacity') or {}
    measured = capacity.get('status') == 'available'
    total, used = capacity.get('total_bytes'), capacity.get('used_bytes')
    # Free is what df calls Avail: space a new file can actually use.
    return {'used_label': size_label(used) if measured else 'Unavailable',
            'free_label': size_label(capacity.get('available_bytes')) if measured else 'Unavailable',
            'total_label': size_label(total) if measured else 'Unavailable',
            'used_percent_label': f'{used / total:.0%}' if measured and total else 'Unavailable',
            'wear_label': (f"{wear['percentage_used']}%" if wear.get('percentage_used') is not None
                           else 'Unavailable'),
            'temperature_label': (f"{smart['temperature']} °C" if smart.get('temperature') is not None
                                  else 'Unavailable'),
            'written_label': (f"{wear['written_bytes'] / 10 ** 12:.1f} TB" if wear.get('written_bytes') is not None
                              else 'Unavailable')}


def synology_card(synology):
    """The Synology's wakes as ready-made text for a Homepage card."""
    if not synology.get('configured'):
        return {'configured': False, 'description': 'Not set up'}
    return {'configured': True,
            'description': (f"Last woke {synology['last_wake_label']}" if synology['wakes']
                            else 'No wakes recorded yet'),
            'last_wake_label': synology['last_wake_label'],
            'wakes_24h': synology['wakes_24h'], 'wakes_7d': synology['wakes_7d']}


def homepage_status(payload):
    summary = {'total': len(payload['disks']), 'awake': 0, 'sleeping': 0,
               'unknown': 0, 'attention': 0, 'known_issues': 0, 'health_unknown': 0,
               'tests_running': 0, 'stale': 0, 'unexpected_state': 0}
    disks = []
    drives = {}
    for disk in payload['disks']:
        state = 'stale' if disk.get('stale') else disk.get('state', 'unknown')
        nvme = disk.get('type') == 'nvme'
        state_label = {'awake': 'Active' if nvme else 'Awake', 'standby': 'Sleeping',
                       'unknown': 'Unknown', 'stale': 'Stale'}.get(state, 'Unknown')
        # Awake and sleeping count spinning disks; an active NVMe drive is neither.
        if not (nvme and state == 'awake'):
            summary[{'awake': 'awake', 'standby': 'sleeping'}.get(state, 'unknown')] += 1
        summary['stale'] += int(bool(disk.get('stale')))
        summary['unexpected_state'] += int(bool(disk.get('unexpected_state')))
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
        # Homepage stays quiet for healthy disks and acknowledged warnings.
        # Keep health/testing details in structured fields and the full dashboard.
        attention = []
        if disk.get('error'):
            attention.append(disk['error'])
        if smart.get('passed') is False:
            attention.append('SMART health failed')
        attention.extend(smart.get('active_warnings') or [])
        if disk['needs_attention'] and not attention and state not in ('stale', 'unknown'):
            attention.append('Needs attention')
        attention = list(dict.fromkeys(attention))
        # e.g. "Cold spare · Sleeping", or "Cold spare · Awake (expected asleep)".
        description = state_label
        if disk.get('unexpected_state'):
            description += f" (expected {disk['expected_state']})"
        wear = smart.get('wear') or {}
        if nvme and wear.get('percentage_used') is not None:
            description += f" · {wear['percentage_used']}% worn"
        if disk.get('role'):
            description = f"{disk['role']} · {description}"
        if attention:
            description += ' · ' + '; '.join(attention)
        disks.append({'label': disk['label'], 'serial': disk['serial'],
                      'role': disk.get('role'), 'expected_state': disk.get('expected_state'),
                      'unexpected_state': bool(disk.get('unexpected_state')),
                      'state': state, 'health': health, 'description': description,
                      'unacknowledged_issues': attention,
                      'temperature': smart.get('temperature'),
                      'health_read_at': disk.get('smart_at'),
                      'observed_at': disk.get('observed_at'),
                      'extended_test': disk.get('extended_test', {}),
                      'capacity': disk.get('capacity', {}),
                      'type': disk.get('type', 'ata'), 'wear': wear,
                      'needs_attention': disk['needs_attention']})
        if nvme:
            drives[disk['label']] = {**drive_card(disk), 'health': health,
                                     'issues_label': '; '.join(attention) or 'None'}
    backups = payload.get('backups') or {'jobs': []}
    backup_summary = {'total': len(backups['jobs']),
                      **{state: sum(j['state'] == state for j in backups['jobs'])
                         for state in ('ok', 'running', 'waiting', 'problem')}}
    backup_summary['problem'] += sum(j['state'] == 'unknown' for j in backups['jobs'])
    return {'demo': payload['demo'], 'summary': summary, 'disks': disks, 'drives': drives,
            'backups': [{'name': j['name'], 'state': j['state'], 'description': j['description']}
                        for j in backups['jobs']],
            'backup_summary': backup_summary,
            'synology': synology_card(payload.get('synology') or {'configured': False}),
            'backups_health': backups_health(backups),
            'snapraid': payload.get('snapraid', {}),
            'schedules': payload.get('schedules', {}), 'pool': payload.get('pool', {}),
            'freshness': collector_freshness(payload),
            'disks_health': disks_health(payload),
            'sync_health': sync_health(payload.get('snapraid', {}), payload['server_time']),
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
    if action != 'refresh' and any(c['serial'] == serial and c.get('type') == 'nvme' for c in CONFIGS):
        raise ValueError('NVMe drives take no disk actions; Refresh disks reads their health')
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
                if DEMO and config.get('type') == 'nvme':
                    now = time.time()
                    disk = {**old, **config, 'observed_at': now, 'state': 'awake', 'error': None,
                            'smart_at': now, 'capacity': {'status': 'available', 'observed_at': now,
                                                          'total_bytes': 2 * 10 ** 12, 'used_bytes': 12 * 10 ** 11,
                                                          'free_bytes': 8 * 10 ** 11, 'available_bytes': 8 * 10 ** 11},
                            'smart': summarize_nvme({'smart_status': {'passed': True}, 'temperature': {'current': 43},
                                                     'nvme_smart_health_information_log': {
                                                         'percentage_used': 2 + i, 'available_spare': 100,
                                                         'available_spare_threshold': 10, 'data_units_written': 77769392,
                                                         'power_on_hours': 5806, 'media_errors': 0}}, 0)}
                elif DEMO:
                    now = time.time()
                    disk = {**old, **config, 'observed_at': now, 'state': 'standby' if config.get('expected_state') == 'asleep' else 'awake',
                            'error': None, 'smart_at': now - (7200 if config.get('expected_state') == 'asleep' else 0),
                            'smart': {'passed': True, 'temperature': 32 + i, 'warnings': [],
                                      'counters': {'Start_Stop_Count': 120 + i, 'Load_Cycle_Count': 800 + i}}}
                else:
                    disk = collect(config, old, time.time())
                save(disk, old)
            if not DEMO:
                refresh_pool_capacity(list(stored().values()), time.time())
            next_poll = time.monotonic() + INTERVAL


# /healthz: the collector is running (Docker's healthcheck). /healthz/disks,
# /healthz/sync and /healthz/backups: for Homepage's status dots, which are green for an HTTP
# status of 403 or below and red above it.
HEALTH_PATHS = ('/healthz', '/healthz/disks', '/healthz/sync', '/healthz/backups')


STATIC_FILES = {'/': ('index.html', 'text/html'), '/app.js': ('app.js', 'text/javascript'),
                '/style.css': ('style.css', 'text/css'),
                '/synology-log': ('synology-log.html', 'text/html'),
                '/synology-log.js': ('synology-log.js', 'text/javascript')}


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

    def health(self, path, payload):
        """(healthy, body) for a health URL, or None for any other path."""
        if path == '/healthz':
            freshness = collector_freshness(payload)
            fresh = freshness['state'] == 'current'
            return fresh, {'collector_recent': fresh, 'freshness': freshness}
        if path == '/healthz/disks':
            result = disks_health(payload)
            return result['ok'], result
        if path == '/healthz/sync':
            result = sync_health(payload['snapraid'], payload['server_time'])
            return result['ok'], result
        if path == '/healthz/backups':
            result = backups_health(payload['backups'])
            return result['ok'], result
        return None

    def do_HEAD(self):
        path = self.path.split('?')[0]
        if path not in HEALTH_PATHS:
            self.send_error(405)
            return
        healthy, _ = self.health(path, snapshot())
        self.send_response(200 if healthy else 503)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_GET(self):
        path = self.path.split('?')[0]
        if path in ('/api/status', '/api/homepage', *HEALTH_PATHS):
            payload = snapshot()
            if path in HEALTH_PATHS:
                healthy, result = self.health(path, payload)
                self.send_response(200 if healthy else 503)
                body = json.dumps(result).encode()
            else:
                self.send_response(200)
                if path == '/api/status':
                    # The same verdicts as the health URLs, for the overview.
                    payload['health'] = {'disks': disks_health(payload),
                                         'sync': sync_health(payload.get('snapraid', {}), payload['server_time']),
                                         'backups': backups_health(payload.get('backups') or {'jobs': []})}
                body = json.dumps(homepage_status(payload) if path == '/api/homepage' else payload).encode()
            mime = 'application/json'
        elif path == '/api/synology-log' and SYNOLOGY_ADDRESSES:
            params = parse_qs(urlsplit(self.path).query)
            try:
                offset = int(params.get('offset', ['0'])[0])
                limit = int(params.get('limit', ['500'])[0])
            except ValueError:
                self.respond_json(400, {'error': 'offset and limit must be numbers'})
                return
            body = json.dumps(synology_log_page(params.get('q', [''])[0][:200], offset, limit)).encode()
            mime = 'application/json'
            self.send_response(200)
        elif path == '/synology.log' and SYNOLOGY_ADDRESSES:
            # The whole log, both files, newest line last.
            body = synology_log_bytes()
            mime = 'text/plain'
            self.send_response(200)
        elif path in STATIC_FILES:
            body = (ROOT / 'static' / STATIC_FILES[path][0]).read_bytes()
            mime = STATIC_FILES[path][1]
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
    # The disk list belongs to the NAS, not the image: Ansible writes it from
    # its pool lists and Compose mounts it. Demo mode uses placeholder disks.
    disks_file = os.getenv('DISKS_FILE') or (
        str(ROOT / 'disks.example.json') if DEMO else '/config/disks.json')
    try:
        configs = load_configs(disks_file)
    except (OSError, ValueError) as exc:
        raise SystemExit(f'Cannot use disk list {disks_file}: {exc}')
    backups_file = os.getenv('BACKUPS_FILE') or (
        str(ROOT / 'backups.example.json') if DEMO else '/config/backups.json')
    try:
        BACKUP_JOBS = load_backup_jobs(backups_file)
    except FileNotFoundError:
        logging.info('No backup list at %s; the Backups section stays empty', backups_file)
    except (OSError, ValueError) as exc:
        BACKUP_LIST_ERROR = str(exc)
        logging.error('Cannot use backup list %s: %s', backups_file, exc)
    # Before anything writes to /data. Demo history is disposable.
    if not DEMO:
        require_data_marker()
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
    if SYNOLOGY_ADDRESSES and not DEMO:
        listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        listener.bind(('0.0.0.0', SYNOLOGY_PORT))
        threading.Thread(target=run_synology_listener, args=(listener,), daemon=True).start()
        logging.info('Listening for the Synology\'s log on UDP %d', SYNOLOGY_PORT)
    ThreadingHTTPServer(('0.0.0.0', int(os.getenv('PORT', '8080'))), Handler).serve_forever()
