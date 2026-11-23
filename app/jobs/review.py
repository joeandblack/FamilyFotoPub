"""Read-only post-import review; reports are local runtime data, not source control."""
import json
from pathlib import Path, PurePosixPath
import time

from PIL import Image
from ..config import DATA, load_config
from ..database import connection
from ..storage.disks import Monitor
from ..imports.importer import IMPORT_LOCK
from ..library.media import TrafficLimit, digest
from ..telemetry import utcnow, write_json
from ..imports.ignore import IGNORE_SQL_PATTERN


def thumbnail_file(sha):
    flat = DATA/'thumbnails'/f'{sha}.jpg'
    return flat if flat.is_file() else DATA/'thumbnails'/sha[:2]/sha[2:4]/f'{sha}.jpg'


def check_catalog(db, config, report):
    """Queue and catalog consistency; returns the media rows to check on disk."""
    job = db.execute('SELECT * FROM jobs WHERE source_root=%s', (str(Path(config['source']['root']).resolve()),)).fetchone()
    if job is None:
        raise RuntimeError('Configured batch is not cataloged')
    counts = db.execute("SELECT CASE WHEN source_path ~ %s THEN 'ignored' ELSE state END state,count(*) count,sum(size) bytes FROM import_items WHERE job_id=%s GROUP BY 1", (IGNORE_SQL_PATTERN,job['id'])).fetchall()
    if job['state'] != 'completed-review-required' or any(r['state'] in ('pending', 'error') for r in counts):
        raise RuntimeError('Batch is not ready for review')
    ignored_bytes=sum(int(r['bytes']) for r in counts if r['state']=='ignored')
    counts=[r for r in counts if r['state']!='ignored']
    report['counts'] = {r['state']: r['count'] for r in counts}
    report['bytes_by_state'] = {r['state']: int(r['bytes']) for r in counts}
    from ..imports.modes import add_skipped
    add_skipped(job,report['counts'],report['bytes_by_state'])
    if sum(report['bytes_by_state'].values()) != job['total_bytes']-ignored_bytes:
        report['faults'].append({'type': 'inventory-byte-total'})
    broken = db.execute("SELECT i.id FROM import_items i LEFT JOIN media m ON m.id=i.media_id WHERE i.job_id=%s AND ((i.state='imported' AND (m.id IS NULL OR coalesce(m.original_size,m.size)<>i.size)) OR (i.state IN ('duplicate','unsupported') AND i.media_id IS NOT NULL))", (job['id'],)).fetchall()
    report['faults'].extend({'type': 'queue-link', **r} for r in broken)
    report['intentional_deletions'] = db.execute('SELECT media_id,deleted_at::text FROM media_trash WHERE deleted_at IS NOT NULL ORDER BY media_id').fetchall()
    incomplete = db.execute('SELECT media_id FROM media_trash WHERE deleting_at IS NOT NULL AND deleted_at IS NULL').fetchall()
    report['faults'].extend({'type':'incomplete-user-deletion',**r} for r in incomplete)
    rows = db.execute('SELECT m.* FROM media m WHERE NOT EXISTS(SELECT 1 FROM media_trash t WHERE t.media_id=m.id AND t.deleting_at IS NOT NULL) ORDER BY m.id').fetchall()
    report['catalog_count'] = len(rows)
    orphaned = db.execute("SELECT m.id FROM media m WHERE NOT EXISTS(SELECT 1 FROM import_items i WHERE i.media_id=m.id AND i.state IN ('imported','duplicate'))").fetchall()
    report['faults'].extend({'type': 'orphan-catalog', **r} for r in orphaned)
    report['unsupported_extensions'] = {}
    for item in db.execute("SELECT source_path FROM import_items WHERE job_id=%s AND state='unsupported' AND source_path !~ %s", (job['id'],IGNORE_SQL_PATTERN)).fetchall():
        ext = Path(item['source_path']).suffix.lower() or '(none)'
        report['unsupported_extensions'][ext] = report['unsupported_extensions'].get(ext, 0) + 1
    db.commit()
    return rows


def start_monitor(monitor, config):
    # Use only an already-enrolled marker; review must not create one.
    monitor.verify()
    marker = json.loads(monitor.marker.read_text(encoding='utf-8'))
    from ..storage.disks import match_disk
    match_disk(config['destination'], marker)
    monitor.marker_id = marker['library_id']
    monitor.last_ok = time.monotonic()
    import threading
    monitor.thread = threading.Thread(target=monitor.run, daemon=True)
    monitor.thread.start()


def check_files(rows, root, monitor, report, output):
    """Catalog path, size and thumbnail presence for every media row."""
    begun = time.monotonic()
    report['checked'] = 0
    report['thumbnail_missing'] = []
    report['image_metadata_warnings'] = 0
    for media in rows:
        monitor.check()
        relative = PurePosixPath(media['relative_path'])
        stored_sha=media.get('stored_sha256') or media['sha256']
        expected = f"{stored_sha[:2]}/{stored_sha[2:4]}/{stored_sha}"
        if relative.is_absolute() or '..' in relative.parts or str(relative.with_suffix('')) != expected:
            report['faults'].append({'type': 'invalid-catalog-path', 'id': media['id']})
            continue
        path = root / media['relative_path']
        try:
            actual = path.stat().st_size
            if actual != media['size']:
                report['faults'].append({'type': 'wrong-size', 'id': media['id'], 'expected': media['size'], 'actual': actual})
        except FileNotFoundError:
            report['faults'].append({'type': 'missing', 'id': media['id'], 'relative_path': media['relative_path']})
        if media['kind'] == 'image':
            if media['metadata_error']:
                report['image_metadata_warnings'] += 1
            elif not thumbnail_file(media['sha256']).is_file():
                report['thumbnail_missing'].append(media['id'])
        report['checked'] += 1
        if report['checked'] % 500 == 0:
            write_json(output, report)
            print(f"Checked {report['checked']} / {len(rows)}", flush=True)
    report['existence_size_seconds'] = round(time.monotonic()-begun, 2)


def pick_samples(rows):
    """Samples spread over the catalog plus all previously missing/recovered media."""
    selected = {m['id']: m for m in rows[::max(1, len(rows)//12)]}
    recovery = DATA/'recovery-audit.json'
    recovered = json.loads(recovery.read_text())['missing'] if recovery.exists() else []
    recovered_ids = {r['id'] for r in recovered}
    selected.update({m['id']: m for m in rows if m['id'] in recovered_ids})
    if rows:
        selected[rows[-1]['id']] = rows[-1]
    return selected, recovered_ids


def verify_samples(selected, recovered_ids, root, monitor, report, output):
    limit = TrafficLimit(20)
    for media in selected.values():
        actual = digest(root/media['relative_path'], monitor.check, limit)
        result = {'id': media['id'], 'bytes': media['size'], 'match': actual == (media.get('stored_sha256') or media['sha256']), 'recovered': media['id'] in recovered_ids}
        report['checksums'].append(result)
        if not result['match']:
            report['faults'].append({'type': 'checksum', 'id': media['id']})
        if media['kind'] == 'image' and not media['metadata_error']:
            with Image.open(thumbnail_file(media['sha256'])) as image:
                image.verify()
        write_json(output, report)


def run():
    config = load_config()
    report = {'started': utcnow(), 'state': 'checking', 'faults': [], 'checksums': []}
    output = DATA / 'post-import-review.json'
    monitor = Monitor(config, roles=('destination',))
    with connection() as db:
        if not db.execute('SELECT pg_try_advisory_lock(%s) locked', (IMPORT_LOCK,)).fetchone()['locked']:
            raise RuntimeError('Importer is active; review deferred')
        rows = check_catalog(db, config, report)
        try:
            start_monitor(monitor, config)
            root = Path(config['destination']['root'])
            check_files(rows, root, monitor, report, output)
            selected, recovered_ids = pick_samples(rows)
            verify_samples(selected, recovered_ids, root, monitor, report, output)
            report['state'] = 'integrity-fault' if report['faults'] or report['thumbnail_missing'] else 'integrity-passed-browser-tests-pending'
        except Exception as error:
            report['state'] = 'integrity-fault'
            report['faults'].append({'type': type(error).__name__, 'reason': str(error)})
        finally:
            if hasattr(monitor, 'thread'):
                monitor.close()
            report['finished'] = utcnow()
            write_json(output, report)
    print(json.dumps(report, default=str), flush=True)
    return report


if __name__ == '__main__':
    run()
