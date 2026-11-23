"""Catalog-only trash; explicit, guarded deletion of destination copies only."""
import json
from fastapi import HTTPException
from ..config import DATA
from ..storage.disks import Monitor, match_disk, volume_serial
from .media import safe_media_path, digest, TrafficLimit
from .locks import lock_content
from ..photos.thumbnails import cache_files, remove_cached_media
from .face_cleanup import forget_faces, remove_face_thumbnails
from ..viewer import media_visible
from ..video import staging

WORKER_LOCK=4340002


def change(db, media_id, action, config, *, deleted_by=None):
    if action not in ('trash', 'restore', 'delete'):
        raise HTTPException(400, 'Unknown trash action')
    def maintenance_guard():
        # Session scope survives permanent-deletion intent commits.
        if not db.execute('SELECT pg_try_advisory_lock_shared(%s) available',(WORKER_LOCK,)).fetchone()['available']:
            raise HTTPException(409, 'Another operation briefly reserved media access. Deletion stopped safely; retry the remaining items when no operation is running.')
        if (DATA/'video-decoder-busy.json').exists():
            raise HTTPException(409, 'A decoder lease requires completion or review')
    def photo_visibility(media):
        return media['kind']=='image' and action in ('trash','restore')
    media=db.execute('SELECT * FROM media WHERE id=%s AND '+media_visible('media'),(media_id,)).fetchone()
    if not media:
        raise HTTPException(404, 'Media not found')
    guarded=not photo_visibility(media)
    if guarded:
        maintenance_guard()
    lock_content(db,media['sha256'])
    media=db.execute('SELECT * FROM media WHERE id=%s AND '+media_visible('media')+' FOR UPDATE',(media_id,)).fetchone()
    if not media:
        raise HTTPException(404, 'Media not found')
    # Only confirmed photos can bypass maintenance; recheck after locking.
    if not guarded and not photo_visibility(media):
        maintenance_guard()
    row=db.execute('SELECT * FROM media_trash WHERE media_id=%s',(media_id,)).fetchone()
    if row and row['deleted_at']:
        raise HTTPException(410, 'Item permanently deleted')
    if action=='trash':
        db.execute('INSERT INTO media_trash(media_id,deleted_by) VALUES (%s,%s) ON CONFLICT DO NOTHING',
                   (media_id,deleted_by))
    elif action=='restore':
        if row and row['deleting_at']:
            raise HTTPException(409, 'Deletion already started; restore is unavailable')
        db.execute('DELETE FROM media_trash WHERE media_id=%s',(media_id,))
        db.execute('UPDATE photo_duplicate_trash SET restored_at=now() WHERE media_id=%s AND restored_at IS NULL',(media_id,))
    else:
        if not row:
            raise HTTPException(409, 'Move the item to Trash first')
        delete_files(db, media, config)
    return {'ok':True}


def delete_files(db, media, config):
    # Caller holds shared worker and per-content session locks across the intent commit.
    staging.files(DATA, media['id'])  # Fail before deleting media if scratch paths are unsafe.
    cache_files(DATA, media['sha256'])  # Reject unsafe paths before deleting originals.
    destination=config['destination']
    monitor=Monitor(config,roles=('destination',))
    monitor.verify()
    marker=json.loads(monitor.marker.read_text(encoding='utf-8'))
    match_disk(destination,marker)
    library_id=marker['library_id']
    def guard():
        if volume_serial(destination['drive'])!=destination['volume_serial'].upper():
            raise OSError('Destination volume changed')
        current=json.loads(monitor.marker.read_text(encoding='utf-8'))
        if current.get('library_id')!=library_id:
            raise OSError('Destination library changed')
    candidates=[(media['relative_path'],media.get('stored_sha256') or media['sha256'])]
    if media.get('original_relative_path') and media['original_relative_path']!=media['relative_path']:
        candidates.append((media['original_relative_path'],media['sha256']))
    if media.get('conversion_duplicate_of'):
        canonical=db.execute('SELECT relative_path,original_relative_path FROM media WHERE id=%s',(media['conversion_duplicate_of'],)).fetchone()
        if not canonical or any(relative in (canonical['relative_path'],canonical['original_relative_path']) for relative,_ in candidates):
            raise OSError('Duplicate shares a retained media path; deletion stopped for review')
    paths=[]
    intent=db.execute('SELECT deleting_at FROM media_trash WHERE media_id=%s',(media['id'],)).fetchone()['deleting_at']
    for relative,sha in candidates:
        path=safe_media_path(destination['root'],relative)
        guard()
        if path.exists():
            if digest(path,guard,TrafficLimit(config['io_mibps']))!=sha:
                raise OSError('Checksum mismatch; deletion stopped for review')
            paths.append(path)
        elif relative==media['relative_path'] and not intent:
            raise OSError('Active destination copy missing; review required before deletion')
    db.execute('UPDATE media_trash SET deleting_at=coalesce(deleting_at,now()) WHERE media_id=%s',(media['id'],))
    db.commit()
    for path in paths:
        guard()
        path.unlink()
    staging.cleanup(DATA, media['id'], guard)
    remove_cached_media(DATA, media['sha256'])
    removed=forget_faces(db, media['id'])
    db.execute('UPDATE media_trash SET deleted_at=now() WHERE media_id=%s',(media['id'],))
    # Before the caller commits: if the commit fails, the faces stay without thumbnails, which
    # are a rebuildable cache, and retrying the deletion removes the faces.
    remove_face_thumbnails(DATA, removed)
