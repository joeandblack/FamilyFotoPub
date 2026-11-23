"""Read-only details for the issue counters in the import overview."""
from .ignore import IGNORE_SQL_PATTERN
from ..photos.duplicates import ELIGIBLE, MEMBER
from ..jobs.job_scope import original_status
from ..photos.fingerprint import readable_warning


def diagnostics(db, job_id):
    job = db.execute('SELECT * FROM jobs WHERE id=%s', (job_id,)).fetchone()
    if not job:
        return None
    items = []
    def add(stage, severity, rows):
        for row in rows:
            if stage == 'Photo duplicate scan':
                row = dict(row, error=readable_warning(row.get('error')))
            items.append(dict(stage=stage, severity=severity, **row))
    add('Import', 'error', db.execute("SELECT source_path name,error FROM import_items WHERE job_id=%s AND state='error' AND source_path !~ %s", (job_id, IGNORE_SQL_PATTERN)).fetchall())
    add('Video processing', 'warning', db.execute("SELECT m.name,v.error FROM media m JOIN video_metadata v ON v.media_id=m.id WHERE m.first_import_job_id=%s AND v.state='warning' AND m.conversion_state IS DISTINCT FROM 'duplicate' AND NOT EXISTS(SELECT 1 FROM media_trash t WHERE t.media_id=m.id)", (job_id,)).fetchall())
    add('Video conversion', 'warning', db.execute("SELECT m.name,m.conversion_error error FROM media m WHERE m.first_import_job_id=%s AND m.conversion_state='warning' AND NOT EXISTS(SELECT 1 FROM media_trash t WHERE t.media_id=m.id) AND NOT EXISTS(SELECT 1 FROM video_metadata v WHERE v.media_id=m.id AND v.state='ancillary')", (job_id,)).fetchall())
    add('Photo duplicate scan', 'warning', db.execute(f"SELECT m.name,q.error FROM photo_duplicate_items q JOIN media m ON m.id=q.media_id WHERE q.job_id=%s AND q.state='warning' AND {ELIGIBLE} AND {MEMBER}", (job_id, job_id, job_id)).fetchall())
    for stage, key in [('Import', None), ('Video processing', 'metadata'), ('Video conversion', 'conversion'), ('Photo duplicate scan', 'duplicates')]:
        status = job if key is None else (original_status(job.get(key + '_status')) if key in ('metadata','conversion') else job.get(key + '_status')) or {}
        if status.get('error') and not any(i['stage']==stage and i['severity']=='error' for i in items):
            add(stage, 'error', [dict(name='', error=status['error'])])
    return items
