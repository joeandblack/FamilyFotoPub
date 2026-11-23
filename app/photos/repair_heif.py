"""Explicit, resumable HEIF derivative repair; never modifies original files."""
import argparse
import json
import subprocess
import sys
from ..config import DATA, ROOT, load_config
from ..database import dedicated_connection as connection
from ..storage.disks import Monitor
from ..library.media import digest, safe_media_path, TrafficLimit
from ..library.locks import MEDIA_LOCK_NAMESPACE, content_key
from ..jobs import idle
from .thumbnails import thumbnail_path, find_thumbnail
from ..telemetry import write_json, utcnow


def repair(ids=None):
    config = load_config()
    report = {'started': utcnow(), 'repaired': [], 'skipped': [], 'failed': [], 'state': 'running'}
    report_path = DATA/'heif-repair.json'
    with connection() as db:
        idle(db)
        if db.execute("SELECT id FROM upload_sessions WHERE state IN ('uploading','queued','importing','cancelling')").fetchone():
            raise RuntimeError('Upload active; repair deferred')
        rows = db.execute("SELECT m.* FROM media m WHERE lower(relative_path) ~ '\\.(heic|heif)$' AND left(m.name,2)<>'._' AND NOT EXISTS(SELECT 1 FROM media_trash t WHERE t.media_id=m.id)" + (' AND m.id=ANY(%s)' if ids else '') + ' ORDER BY m.id', (ids,) if ids else ()).fetchall()
        db.commit()  # Only session advisory locks remain; no long table transaction.
        report['total'] = len(rows)
        monitor = Monitor(config, roles=('destination',))
        if not monitor.marker.exists():
            raise RuntimeError('Existing destination marker required; no disk enrollment during repair')
        monitor.prepare()
        try:
            for row in rows:
                # A new transfer takes priority. No upload is paused or cancelled.
                if db.execute("SELECT id FROM upload_sessions WHERE state IN ('uploading','queued','importing','cancelling')").fetchone():
                    report['state'] = 'deferred-for-upload'
                    break
                db.commit()
                thumb = thumbnail_path(DATA/'thumbnails', row['sha256'])
                preview = thumbnail_path(DATA/'previews', row['sha256'])
                if not row['metadata_error'] and find_thumbnail(DATA/'thumbnails', row['sha256']).is_file() and preview.is_file():
                    report['skipped'].append(row['id'])
                    continue
                key = content_key(row['sha256'])
                db.execute('SELECT pg_advisory_lock_shared(%s,%s)', (MEDIA_LOCK_NAMESPACE,key))
                try:
                    if db.execute('SELECT 1 FROM media_trash WHERE media_id=%s', (row['id'],)).fetchone():
                        report['skipped'].append(row['id'])
                        continue
                    source = safe_media_path(config['destination']['root'], row['relative_path'])
                    before = source.stat()
                    limit = TrafficLimit(config.get('io_mibps', 40))
                    if before.st_size != row['size'] or digest(source, monitor.check, limit) != row['sha256']:
                        raise OSError('Original size/hash differs from catalog; repair refused')
                    request = {'database':config['database'], 'owner':db.execute('SELECT pg_backend_pid()').fetchone()['pg_backend_pid'],
                               'media_id':row['id'], 'sha':row['sha256'], 'source':str(source), 'thumbnail':str(thumb), 'preview':str(preview)}
                    result = subprocess.run([sys.executable, '-m', 'app.photos.heif', '--guarded'], cwd=ROOT,
                        input=json.dumps(request), capture_output=True, text=True, timeout=45, check=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                    metadata = json.loads(result.stdout)
                    after = source.stat()
                    if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns or digest(source, monitor.check, limit) != row['sha256']:
                        raise OSError('Original changed during repair; catalog not updated')
                    monitor.check()
                    db.execute('UPDATE media SET captured_at=coalesce(captured_at,%s),width=coalesce(width,%s),height=coalesce(height,%s),metadata_error=NULL WHERE id=%s',
                        (metadata['captured_at'], metadata['width'], metadata['height'], row['id']))
                    db.commit()
                    report['repaired'].append(row['id'])
                except Exception as error:
                    db.rollback()
                    detail = (error.stderr[-1500:] if isinstance(error, subprocess.CalledProcessError) else str(error))
                    report['failed'].append({'id': row['id'], 'error': detail})
                finally:
                    db.execute('SELECT pg_advisory_unlock_shared(%s,%s)', (MEDIA_LOCK_NAMESPACE,key))
                    db.commit()
                    report['updated'] = utcnow()
                    write_json(report_path, report)
                if (len(report['repaired']) + len(report['failed'])) % 25 == 0:
                    print(json.dumps({k: len(report[k]) for k in ('repaired','skipped','failed')}), flush=True)
            else:
                report['state'] = 'completed'
        finally:
            monitor.close()
            report['finished'] = utcnow()
            write_json(report_path, report)
    print(json.dumps({'state': report['state'], 'total': report['total'], **{k: len(report[k]) for k in ('repaired','skipped','failed')}}), flush=True)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument('--ids', nargs='+', type=int)
    scope.add_argument('--all-heif', action='store_true')
    args = parser.parse_args()
    repair(args.ids)
