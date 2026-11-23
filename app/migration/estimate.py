"""Read-only, explicitly approximate full-rehash downtime planning."""
from contextlib import closing
import math
from pathlib import Path
import sqlite3

from ..backup.media_tree import file_digest
from ..backup.encrypted import read_bounded
import json


def rehash_estimate(media_bytes, files, *, slow_mib_per_second=50, fast_mib_per_second=200):
    if (type(media_bytes) is not int or media_bytes < 0 or type(files) is not int or files < 0
            or not 0 < slow_mib_per_second <= fast_mib_per_second):
        raise ValueError('Nonnegative media totals and positive throughput assumptions are required')
    total = media_bytes * 2
    return {'kind': 'offline-cutover-estimate', 'media_bytes': media_bytes, 'files': files,
            'rehash_bytes': total, 'copies_read': 2,
            'estimated_rehash_seconds_min': math.ceil(total / (fast_mib_per_second * 1024 ** 2)),
            'estimated_rehash_seconds_max': math.ceil(total / (slow_mib_per_second * 1024 ** 2)),
            'assumed_mib_per_second': [slow_mib_per_second, fast_mib_per_second],
            'measured': False, 'includes_catalog_backup': False,
            'message': 'Both complete media copies are read while the app is offline. '
                       'This is a planning range, not a deadline; catalog backup, small files, '
                       'USB contention and slow disks add time. The old media is retained.'}


def plan_disk_cutover(directory):
    directory = Path(directory).absolute()
    ready = json.loads(read_bounded(directory / 'READY.json', 65536))
    inventory = directory / 'inventory/inventory.sqlite3'
    if ready.get('state') != 'ready-for-cutover-review' or file_digest(inventory) != ready.get('inventory_sha256'):
        raise ValueError('A completed unchanged disk preparation is required')
    with closing(sqlite3.connect(inventory.as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('PRAGMA trusted_schema=OFF')
        files, size = db.execute("SELECT count(*),coalesce(sum(size),0) FROM entries WHERE kind='file'").fetchone()
    return rehash_estimate(size, files)
