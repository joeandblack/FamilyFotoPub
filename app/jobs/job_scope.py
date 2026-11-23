"""Permanent first-import ownership and prerequisites for video work."""
from psycopg.types.json import Jsonb
from datetime import datetime


def elapsed_base(db,job_id,stage):
    if stage not in ('metadata','conversion'):raise ValueError('Invalid stage')
    previous=db.execute(f'SELECT {stage}_status status FROM jobs WHERE id=%s',(job_id,)).fetchone()['status']
    if not previous or previous.get('scope')!='original-import':return 0
    return previous.get('elapsed_base',0)+max(0,(datetime.fromisoformat(previous['updated'])-datetime.fromisoformat(previous['started'])).total_seconds())


def membership(alias='m'):
    if alias not in ('m','v'):
        raise ValueError('Invalid media alias')
    if alias=='m':return 'm.first_import_job_id=%s'
    return 'EXISTS(SELECT 1 FROM media owned WHERE owned.id=v.media_id AND owned.first_import_job_id=%s)'


def pending_work(db,job_id,stage):
    if stage=='duplicates':
        from ..photos.duplicates import counts
        return bool(counts(db,job_id).get('pending'))
    if stage not in ('metadata','conversion'):raise ValueError('Invalid stage')
    pending="(v.media_id IS NULL OR v.state='pending')" if stage=='metadata' else "(lower(right(m.relative_path,4)) IN ('.avi','.flv','.mts') OR m.original_relative_path IS NOT NULL) AND (m.conversion_state IS NULL OR m.conversion_state='published') AND v.state IS NOT NULL AND v.state<>'ancillary'"
    return db.execute("SELECT EXISTS(SELECT 1 FROM media m LEFT JOIN video_metadata v ON v.media_id=m.id WHERE m.kind='video' AND "+membership()+" AND NOT EXISTS(SELECT 1 FROM media_trash t WHERE t.media_id=m.id) AND "+pending+") pending",(job_id,)).fetchone()['pending']


def original_status(status):
    # Old shared-scope snapshots remain on disk/in the migration audit column,
    # but their work and elapsed times cannot be attributed to original ownership.
    return status if status and status.get('scope')=='original-import' else None


def require_import_complete(db,job_id):
    job=db.execute('SELECT state FROM jobs WHERE id=%s',(job_id,)).fetchone()
    if not job or job['state']!='completed-review-required':
        raise RuntimeError('Finish this import before video processing')
    if db.execute("SELECT count(*) count FROM import_items WHERE job_id=%s AND state IN ('pending','error')",(job_id,)).fetchone()['count']:
        raise RuntimeError('This import has pending or failed files')


def save_status(db,job_id,stage,status):
    if stage not in ('metadata','conversion'):
        raise ValueError('Invalid stage')
    db.execute(f'UPDATE jobs SET {stage}_status=%s WHERE id=%s',(Jsonb(status),job_id))
    db.commit()
