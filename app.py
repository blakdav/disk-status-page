"""Disk Status Page: read-only ATA monitoring and a small HTTP dashboard."""
import glob
from contextlib import contextmanager
import json
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
WARNINGS = {5, 187, 188, 197, 198, 199}
COUNTERS = {4, 12, 193}


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
        db.execute('CREATE TABLE IF NOT EXISTS disks '
                   '(serial TEXT PRIMARY KEY, payload TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS events '
                   '(serial TEXT, observed_at REAL, state TEXT)')
        db.execute('CREATE INDEX IF NOT EXISTS event_time ON events(observed_at)')


def command(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=20)
    return result.returncode, result.stdout + result.stderr


def device_for(serial):
    # Resolve existing udev links only; discovery must never probe a disk.
    candidates = glob.glob('/dev/disk/by-id/ata-*_' + glob.escape(serial))
    return sorted(candidates)[0] if candidates else None


def power_state(output):
    match = re.search(r'drive state is:\s*(active/idle|standby|sleeping)', output)
    return {'active/idle': 'awake', 'standby': 'standby',
            'sleeping': 'standby'}.get(match[1], 'unknown') if match else 'unknown'


def summarize_smart(data, status):
    attrs = data.get('ata_smart_attributes', {}).get('table', [])
    warnings = []
    counters = {}
    for attr in attrs:
        raw = attr.get('raw', {}).get('value', 0)
        if attr.get('id') in WARNINGS and raw:
            warnings.append(f"{attr['name']}: {attr.get('raw', {}).get('string', raw)}")
        if attr.get('id') in COUNTERS:
            counters[attr['name']] = raw
    passed = data.get('smart_status', {}).get('passed')
    errors = data.get('ata_smart_error_log', {}).get('summary', {}).get('count', 0)
    if errors:
        warnings.append(f'{errors} recorded SMART errors (may be historical)')
    # The self-test count includes successful tests; inspect individual results.
    for test in data.get('ata_smart_self_test_log', {}).get('standard', {}).get('table', []):
        result = test.get('status', {})
        if result.get('passed') is False:
            warnings.append('Self-test: ' + result.get('string', 'failed'))
    if status & 0b11111000:
        warnings.append('SMART reports a health, attribute, error-log or self-test warning')
    return {'passed': passed, 'temperature': data.get('temperature', {}).get('current'),
            'warnings': warnings, 'counters': counters,
            'error_log': data.get('ata_smart_error_log', {}),
            'self_test_log': data.get('ata_smart_self_test_log', {}),
            'attributes': attrs}


def collect(config, previous, now):
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
        if disk['state'] == 'awake' and now - previous.get('smart_at', 0) >= SMART_INTERVAL:
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
    return disk


def save(disk, previous):
    # A first sample, restart gap, or unknown sample does not prove a transition.
    contiguous = (previous.get('state') in ('awake', 'standby')
                  and disk['observed_at'] - previous.get('observed_at', 0) < INTERVAL * 2.5)
    changed = contiguous and disk['state'] in ('awake', 'standby') and disk['state'] != previous['state']
    with connect() as db:
        if changed:
            key = 'last_up' if disk['state'] == 'awake' else 'last_down'
            disk[key] = disk['observed_at']
            db.execute('INSERT INTO events VALUES (?, ?, ?)',
                       (disk['serial'], disk['observed_at'], disk['state']))
        db.execute('INSERT OR REPLACE INTO disks VALUES (?, ?)',
                   (disk['serial'], json.dumps(disk)))
        db.execute('DELETE FROM events WHERE observed_at < ?', (time.time() - 365 * 86400,))


def stored():
    with connect() as db:
        return {serial: json.loads(payload) for serial, payload in db.execute('SELECT * FROM disks')}


def snapshot():
    disks = stored()
    with connect() as db:
        events = [{'serial': s, 'observed_at': t, 'state': state}
                  for s, t, state in db.execute(
                      'SELECT * FROM events ORDER BY observed_at DESC LIMIT 100')]
    return {'demo': DEMO, 'poll_seconds': INTERVAL, 'smart_seconds': SMART_INTERVAL,
            'server_time': time.time(), 'disks': list(disks.values()), 'events': events}


def run_collector(configs):
    while not STOP.is_set():
        started = time.monotonic()
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
        STOP.wait(max(1, INTERVAL - (time.monotonic() - started)))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split('?')[0]
        if path in ('/api/status', '/healthz'):
            payload = snapshot()
            if path == '/healthz':
                fresh = any(payload['server_time'] - d['observed_at'] < INTERVAL * 3
                            for d in payload['disks'])
                self.send_response(200 if fresh else 503)
                body = json.dumps({'collector_recent': fresh}).encode()
            else:
                self.send_response(200)
                body = json.dumps(payload).encode()
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
    def worker():
        try:
            run_collector(configs)
        except Exception:
            logging.exception('Collector stopped; health endpoint will become unhealthy')
    threading.Thread(target=worker, daemon=True).start()
    ThreadingHTTPServer(('0.0.0.0', int(os.getenv('PORT', '8080'))), Handler).serve_forever()
