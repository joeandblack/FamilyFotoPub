"""Remove People data left behind by photos that were permanently deleted before the fix.

Until the permanent-delete fix, deleting a photo for good left its faces, vectors and face
thumbnails in the People catalog, and its shadow copy there. The app already hid them.
This finds such photos and removes their People data with the same code the delete path
now uses (app/library/face_cleanup.py), one photo per transaction.

Without --apply it only reports counts and changes nothing. With --apply it needs the
FamilyFoto web server stopped, because it takes the installation's writer lease.

    python scripts/forget_deleted_photo_faces.py --config <local.json> --data <data folder>
    python scripts/forget_deleted_photo_faces.py --config <local.json> --data <data folder> --apply
"""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

LEFTOVERS = '''SELECT t.media_id FROM public.media_trash t WHERE t.deleted_at IS NOT NULL
    AND (EXISTS (SELECT 1 FROM {s}.face_analyses a WHERE a.media_id=t.media_id)
      OR EXISTS (SELECT 1 FROM {s}.media m WHERE m.id=t.media_id)
      OR EXISTS (SELECT 1 FROM {s}.import_items i WHERE i.media_id=t.media_id))
    ORDER BY t.media_id'''


def leftovers(db, schemas):
    from psycopg import sql
    from app.library.face_cleanup import _exists
    found = {}
    for schema in schemas:
        if not all(_exists(db, schema, table) for table in ('face_analyses', 'face_detections', 'media', 'import_items')):
            continue
        s = sql.Identifier(schema)
        ids = [row['media_id'] for row in db.execute(sql.SQL(LEFTOVERS).format(s=s)).fetchall()]
        faces = db.execute(sql.SQL('SELECT count(*) n FROM {}.face_detections d JOIN {}.face_analyses a '
                                   'ON a.id=d.analysis_id WHERE a.media_id=ANY(%s)').format(s, s), (ids,)).fetchone()['n']
        found[schema] = {'photos': ids, 'faces': faces}
    return found


def report(found, data):
    out = {}
    for schema, item in found.items():
        token = schema.removeprefix('face_native_')
        out[schema[:16] + '…'] = {'photos': len(item['photos']), 'faces': item['faces'],
                                  'thumbnail_folder_present': (data / 'face_detect/profiles' / token / 'thumbnails').is_dir()}
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    cfg = json.loads(args.config.read_text(encoding='utf-8'))
    data = args.data.resolve()
    os.environ['FAMILYFOTO_DATA_DIR'] = str(data)
    import psycopg
    from psycopg.rows import dict_row
    from app.library.face_cleanup import forget_faces, people_schemas, remove_face_thumbnails

    with psycopg.connect(**cfg['database'], row_factory=dict_row, connect_timeout=5) as db:
        db.execute('SET default_transaction_read_only = on')
        found = leftovers(db, people_schemas(db))
    print(json.dumps({'found': report(found, data)}, indent=2))
    if not args.apply:
        return 0

    from app import database
    photos = sorted({media_id for item in found.values() for media_id in item['photos']})
    faces = 0
    with database.connection(cfg['database']) as db:
        for media_id in photos:
            # Only a photo whose deletion finished; a photo restored meanwhile is skipped.
            done = db.execute('SELECT 1 FROM public.media_trash WHERE media_id=%s AND deleted_at IS NOT NULL FOR SHARE',
                              (media_id,)).fetchone()
            if not done:
                db.rollback()
                continue
            removed = forget_faces(db, media_id)
            db.commit()
            remove_face_thumbnails(data, removed)
            faces += sum(len(ids) for ids in removed.values())
        db.execute('SET TRANSACTION READ ONLY')
        left = leftovers(db, people_schemas(db))
    print(json.dumps({'removed': {'photos': len(photos), 'faces': faces}, 'left': report(left, data)}, indent=2))
    return 0 if all(not item['photos'] for item in left.values()) else 1


if __name__ == '__main__':
    sys.exit(main())
