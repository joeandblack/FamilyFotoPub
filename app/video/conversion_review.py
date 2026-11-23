"""Explicit playback review; only the managed retained original may be deleted."""
from fastapi import HTTPException
from psycopg.types.json import Jsonb
from .avi import verify
from .conversion_performance import REVIEW_MESSAGE
from ..library.media import safe_media_path
from ..library.locks import lock_content
from ..telemetry import utcnow
from ..viewer import media_visible
from .metadata import readonly_monitor


def finish(db, job_id, media_id, delete_original, config):
    row = db.execute('SELECT sha256 FROM media WHERE id=%s AND ' + media_visible('media'), (media_id,)).fetchone()
    if not row:
        raise HTTPException(404, 'Video not found')
    lock_content(db, row['sha256'])
    media = db.execute('SELECT m.*,v.metadata FROM media m JOIN video_metadata v ON v.media_id=m.id WHERE m.id=%s FOR UPDATE OF m,v', (media_id,)).fetchone()
    if not media or media['kind'] != 'video' or media['first_import_job_id'] != job_id:
        raise HTTPException(409, 'Video does not belong to this import')
    if db.execute('SELECT 1 FROM media_trash WHERE media_id=%s', (media_id,)).fetchone():
        raise HTTPException(409, 'Video is in Recently Deleted')
    validation = dict((media['metadata'] or {}).get('conversion_validation') or {})
    if validation.get('original_deleted_at'):
        return {'ok': True, 'media_id': media_id}
    if validation.get('playback') not in ('review-required','user-reviewed') or media['conversion_state'] not in ('done','warning'):
        raise HTTPException(409, 'Video has no reviewable retained original')
    if validation.get('original_delete_started_at') and not delete_original:
        raise HTTPException(409, 'Original deletion already started; retry the delete action')
    if delete_original:
        root = config['destination']['root']
        target = safe_media_path(root, media['relative_path'])
        original = safe_media_path(root, media['original_relative_path'] or '')
        if original == target or not media['original_relative_path'] or original.suffix.lower() not in ('.mts', '.flv'):
            raise HTTPException(409, 'No separate retained MTS / FLV original')
        shared = db.execute('SELECT 1 FROM media WHERE id<>%s AND (relative_path=%s OR original_relative_path=%s) LIMIT 1', (media_id, media['original_relative_path'], media['original_relative_path'])).fetchone()
        if shared:
            raise HTTPException(409, 'Retained original is shared with another catalog item')
        monitor = readonly_monitor(config)
        try:
            verify(target, media['stored_sha256'], media['size'], monitor.check)
            if original.exists():
                verify(original, media['sha256'], media['original_size'], monitor.check)
            elif not validation.get('original_delete_started_at'):
                raise OSError('Retained original is missing; nothing changed')
            # Durable intent allows a retry after unlink succeeds but the final commit fails.
            validation.setdefault('original_delete_started_at', utcnow())
            db.execute("UPDATE video_metadata SET metadata=jsonb_set(coalesce(metadata,'{}'::jsonb),'{conversion_validation}',%s) WHERE media_id=%s", (Jsonb(validation), media_id))
            db.commit()
            monitor.check()
            original.unlink(missing_ok=True)
            validation['original_deleted_at'] = utcnow()
        finally:
            monitor.close()
    validation.update(playback='user-reviewed', reviewed_at=utcnow(), original_action='delete' if delete_original else 'keep')
    db.execute("UPDATE video_metadata SET metadata=jsonb_set(coalesce(metadata,'{}'::jsonb),'{conversion_validation}',%s) WHERE media_id=%s", (Jsonb(validation), media_id))
    # Do not erase unrelated conversion errors.
    db.execute("UPDATE media SET conversion_state='done',conversion_error=NULL WHERE id=%s AND conversion_error=%s", (media_id, REVIEW_MESSAGE))
    return {'ok': True, 'media_id': media_id}
