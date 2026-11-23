"""Rebuild the video posters a backup leaves out; originals and video metadata are unchanged.

Only videos whose metadata recorded a poster are rebuilt. Videos that never had one (audio,
sidecars, decoder warnings) are left as they are, so a repair never turns a reviewed warning
into something else. Each original is checked against the catalog first, as the video job does.
"""
from ..library.locks import lock_content, unlock_content
from ..library.media import TrafficLimit, digest, safe_media_path
from ..photos.thumbnails import find_thumbnail, thumbnail_path
from . import metadata as videos

QUERY = ("SELECT m.* FROM media m JOIN video_metadata v ON v.media_id=m.id "
         "WHERE m.kind='video' AND v.state='done' AND v.metadata->>'poster'='true' AND left(m.name,1)<>'.' "
         "AND NOT EXISTS(SELECT 1 FROM media_trash t WHERE t.media_id=m.id AND t.deleting_at IS NOT NULL)")


def videos_with_posters(db, ids=None):
    return db.execute(QUERY + (' AND m.id=ANY(%s)' if ids else '') + ' ORDER BY m.id',
                      (ids,) if ids else ()).fetchall()


def ready_tools():
    """The decoders, refused while an earlier decoder still needs review."""
    if (videos.DATA/'video-decoder-busy.json').exists():
        raise RuntimeError('Previous decoder needs review; see docs/VIDEO.md')
    return videos.tool_paths()


def rebuild_missing_poster(db, row, config, tools, guard=lambda: None):
    """Caller reserves maintenance; the content lock excludes deletion/edits."""
    key = lock_content(db, row['sha256'])
    try:
        if db.execute('SELECT 1 FROM media_trash WHERE media_id=%s AND deleting_at IS NOT NULL', (row['id'],)).fetchone():
            return 'excluded'
        if find_thumbnail(videos.DATA/'thumbnails', row['sha256']).is_file():
            return 'exists'
        source = safe_media_path(config['destination']['root'], row['relative_path'])
        before = source.stat()
        if before.st_size != row['size'] or digest(source, guard, TrafficLimit(0)) != (row.get('stored_sha256') or row['sha256']):
            raise OSError('Original size/hash differs from catalog; poster repair refused')
        metadata, _, warning = videos.extract(source, row['sha256'], tools, guard)
        after = source.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            thumbnail_path(videos.DATA/'thumbnails', row['sha256']).unlink(missing_ok=True)
            raise OSError('Original changed during poster repair; repair refused')
        if warning or not metadata.get('poster'):
            raise ValueError(warning or 'No poster produced')
        return 'rebuilt'
    finally:
        unlock_content(db, key)
