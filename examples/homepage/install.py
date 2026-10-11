"""Merge Disk Status Page styling into Homepage config, preserving other customizations."""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import urllib.request

BASE = 'https://raw.githubusercontent.com/blakdav/disk-status-page/main/examples/homepage/'


def merge(existing, addition):
    begin = '/* BEGIN DISK STATUS PAGE */'
    end = '/* END DISK STATUS PAGE */'
    block = begin + '\n' + addition.rstrip() + '\n' + end
    if existing.count(begin) != existing.count(end) or existing.count(begin) > 1:
        raise ValueError('Ambiguous existing Disk Status Page markers; no changes made')
    if begin in existing:
        first = existing.index(begin)
        last = existing.index(end) + len(end)
        if first > last:
            raise ValueError('Invalid existing markers')
        return existing[:first] + block + existing[last:]
    return existing.rstrip() + '\n\n' + block + '\n'


def install(directory, fetch):
    if not directory.is_dir():
        raise ValueError('Homepage config directory does not exist')
    pending = []
    # Fetch and validate both files before modifying either existing file.
    for name in ('custom.js', 'custom.css'):
        path = directory / name
        existing = path.read_text() if path.exists() else ''
        updated = merge(existing, fetch(name))
        pending.append((path, existing, updated))
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    for path, existing, updated in pending:
        if existing == updated:
            continue
        if path.exists():
            backup = path.with_name(path.name + '.before-disk-status-' + stamp)
            backup.write_text(existing)
            print('Backup:', backup)
        path.write_text(updated)
        print('Updated:', path)
    print('Add the service IDs from the example, then reload Homepage.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config-dir', type=Path, default=Path('/opt/docker/homepage'))
    args = parser.parse_args()
    def fetch(name):
        with urllib.request.urlopen(BASE + name, timeout=30) as response:
            return response.read().decode('utf-8')
    install(args.config_dir, fetch)
