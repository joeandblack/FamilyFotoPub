"""Remove a permanently deleted photo's faces from every People catalog.

People data lives in the catalog schema `people`, and in any older per-model schema
(`face_native_<token>`, `auto_eval_<token>`) still in the database, which kept a shadow copy
of the photos it scanned. The permanent-delete trigger on `public.media_trash` only reaches
the empty face tables in `public`, so this does the catalogs' part inside the same
transaction as the deletion. The list of schemas is face_detect.catalog.people_schemas,
shared with the catalog code, so a schema move can't skip photo deletion.

It removes the photo's analyses and, through their cascades, its faces, vectors,
assignments, group memberships, blocks, quality and ignore records. Then it removes the
photo's work rows, migration records and shadow copy. Event history keeps face numbers
only, never a picture; Undo already refuses a face that no longer exists. Face thumbnails
are files, removed afterwards by `remove_face_thumbnails`.
"""
from pathlib import Path

from psycopg import sql

from face_detect.catalog import SCHEMA, people_schemas

# Shared with face_detect.recognition.people: serializes with People edits.
PEOPLE_LOCK = 4340032
# Rows that refer to the photo and don't cascade from its analyses.
BY_MEDIA = ('face_work', 'face_review_events', 'face_preferences', 'people_work',
            'native_migration_photos', 'native_preserved_photos', 'import_items')


def _value(row, key=None):
    if isinstance(row, dict):
        return row[key] if key else next(iter(row.values()))
    return row[0]


def _exists(db, schema, table):
    return _value(db.execute('SELECT to_regclass(%s) IS NOT NULL present',
                             (f'"{schema}"."{table}"',)).fetchone()) is True


def forget_faces(db, media_id):
    """Delete the photo's People data in every catalog. Returns {schema: [detection ids]}."""
    schemas = people_schemas(db)
    if not schemas:
        return {}
    db.execute('SELECT pg_advisory_xact_lock(%s)', (PEOPLE_LOCK,))
    # The catalog's own triggers name its tables without a schema, so each catalog is
    # cleaned with the search path set to it. The caller's path comes back afterwards.
    # On an error the caller rolls back, which resets the path too, so it's restored only
    # on success: restoring it in an aborted transaction would hide the real error.
    previous = _value(db.execute("SELECT current_setting('search_path') path").fetchone())
    removed = {}
    for schema in schemas:
        ids = _forget(db, schema, media_id)
        if ids is not None:
            removed[schema] = ids
    db.execute("SELECT set_config('search_path', %s, true)", (previous,))
    return removed


def _forget(db, schema, media_id):
    s = sql.Identifier(schema)
    db.execute("SELECT set_config('search_path', %s, true)", (f'"{schema}", public',))
    if not _exists(db, schema, 'face_analyses'):
        return None
    ids = [_value(row) for row in db.execute(sql.SQL(
        'SELECT d.id FROM {}.face_detections d JOIN {}.face_analyses a ON a.id=d.analysis_id '
        'WHERE a.media_id=%s ORDER BY d.id').format(s, s), (media_id,)).fetchall()]
    if ids and _exists(db, schema, 'native_migration_faces'):
        # The October migration's record of each face has no cascade.
        db.execute(sql.SQL('DELETE FROM {}.native_migration_faces WHERE detection_id=ANY(%s)').format(s), (ids,))
    db.execute(sql.SQL('DELETE FROM {}.face_analyses WHERE media_id=%s').format(s), (media_id,))
    for table in BY_MEDIA:
        if _exists(db, schema, table):
            db.execute(sql.SQL('DELETE FROM {}.{} WHERE media_id=%s').format(s, sql.Identifier(table)), (media_id,))
    if _exists(db, schema, 'media'):
        db.execute(sql.SQL('DELETE FROM {}.media WHERE id=%s').format(s), (media_id,))
    return ids


def remove_face_thumbnails(data_root, removed):
    """Delete the face thumbnails of removed faces. They're a rebuildable cache."""
    for schema, ids in removed.items():
        if schema == SCHEMA:
            folder = Path(data_root) / 'people/thumbnails'
        elif schema.startswith('face_native_'):
            folder = Path(data_root) / 'face_detect/profiles' / schema.removeprefix('face_native_') / 'thumbnails'
        else:
            continue
        for detection_id in ids:
            (folder / f'{int(detection_id)}.jpg').unlink(missing_ok=True)
