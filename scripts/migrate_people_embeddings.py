"""Resumable native embedding staging on a verified, isolated People snapshot.

Existing identities, assignments, boxes, models, receipts and decisions are never
updated. Native vectors remain in staging until a separately validated cutover.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from prepare_people_migration import save


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--migration', type=Path, required=True)
    parser.add_argument('--max-photos', type=int, default=10, help='0 processes all remaining photos')
    args = parser.parse_args()
    folder = args.migration.resolve()
    report = json.loads((folder/'migration.json').read_text())
    settings = json.loads((folder/'candidate-connection.json').read_text())
    if (settings['host'], settings['port'], settings['dbname']) != (
        '127.0.0.1', 55449, report['candidate_database']
    ) or not settings['dbname'].startswith('familyfoto_people_'):
        raise ValueError('Only the isolated migration database is permitted')
    if report['stage'] != 'preservation-verified' or report['activated']:
        raise ValueError('Verified, inactive preservation snapshot required')
    if args.max_photos < 0:
        raise ValueError('Invalid photo limit')
    cfg = json.loads((ROOT/'config/local.json').read_text(encoding='utf-8'))
    from app.library.media import safe_media_path
    from face_detect.service import create_service
    from face_detect.jobs.automatic_people_scan import decode_canonical
    from face_detect.recognition.ignored_faces import BASE
    from face_detect.recognition.people_face_quality import NOT_BLURRY
    from face_detect.recognition.quality import quality_seed
    from face_detect.migration import correspond
    profile = {'provider': 'insightface', 'device': 'cpu',
               'python': str(ROOT/'runtime/face_detect/envs/insightface/Scripts/python.exe'),
               'model_manifest': str(ROOT/'runtime/face_detect/models/buffalo_l/manifest.json')}
    provider = create_service({'face_detection': {'active_profile': 'migration', 'profiles': {'migration': profile}}})
    state = {'state': 'running', 'model_id': provider.identity, 'photos_this_run': 0,
             'profile': profile, 'started_at': time.time()}
    save(folder/'embedding-progress.json', state)
    execution_state = None
    try:
        if sys.platform == 'win32':
            import ctypes
            execution_state = ctypes.windll.kernel32.SetThreadExecutionState
            execution_state.argtypes = [ctypes.c_uint32]
            execution_state.restype = ctypes.c_uint32
            if not execution_state(0x80000001):
                raise RuntimeError('Cannot request temporary system wakefulness for migration')
        with psycopg.connect(**settings, row_factory=dict_row) as db:
            directory = Path(db.execute('SHOW data_directory').fetchone()['data_directory']).resolve()
            if directory != folder.parent/'validation-postgres':
                raise ValueError('Unexpected migration cluster')
            db.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(report['source_schema'])))
            if not db.execute('SELECT pg_try_advisory_lock(4340088) locked').fetchone()['locked']:
                raise RuntimeError('Another migration worker is active')
            db.execute('''CREATE TABLE IF NOT EXISTS native_migration_faces(
                detection_id bigint PRIMARY KEY REFERENCES face_detections(id),
                model_id text NOT NULL, state text NOT NULL CHECK(state IN ('embedded','review')),
                result jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now())''')
            db.execute('''CREATE TABLE IF NOT EXISTS native_migration_photos(
                media_id bigint PRIMARY KEY REFERENCES media(id), model_id text NOT NULL,
                state text NOT NULL CHECK(state IN ('done','review','error')),
                detail text, seconds double precision NOT NULL,
                created_at timestamptz NOT NULL DEFAULT now())''')
            if db.execute('SELECT 1 FROM native_migration_photos WHERE model_id<>%s LIMIT 1', (provider.identity,)).fetchone():
                raise ValueError('Migration already pins another model')
            query = ('SELECT d.id,d.box,a.media_id,a.width,a.height,m.sha256,m.relative_path ' + BASE
                     + " AND NOT d.ignored AND d.review_state<>'rejected'" + NOT_BLURRY)
            rows = db.execute(query+' ORDER BY a.media_id,d.id').fetchall()
            photos = {}
            for row in rows:
                photos.setdefault(row['media_id'], []).append(row)
            done = {r['media_id'] for r in db.execute('SELECT media_id FROM native_migration_photos')}
            state.update(total_photos=len(photos), completed_photos=len(done & photos.keys()),
                         eligible_faces=len(rows))
            db.commit()
            for mid, faces in photos.items():
                if mid in done:
                    continue
                if (folder/'PAUSE').exists() or (args.max_photos and state['photos_this_run'] >= args.max_photos):
                    state['state'] = 'paused'
                    break
                started = time.monotonic()
                row = faces[0]
                state['current_media_id'] = mid
                save(folder/'embedding-progress.json', state)
                image = None
                try:
                    path = safe_media_path(cfg['destination']['root'], row['relative_path'])
                    if path.stat().st_size > 128 * 1024 * 1024 or digest(path) != row['sha256']:
                        raise ValueError('Source differs from the preserved catalog or exceeds byte limit')
                    image, _ = decode_canonical(path)
                    if any(abs((f['width']/f['height'])/(image.width/image.height)-1) > .01 for f in faces):
                        raise ValueError('Canonical orientation/aspect differs; manual review required')
                    detected = provider.detect(image)
                    matched = correspond(faces, detected, image.size)
                    if digest(path) != row['sha256']:
                        raise ValueError('Source changed during inference')
                    for face in faces:
                        match = matched.get(face['id'])
                        result = ({'face': detected[match['index']], 'overlap': match['overlap'],
                                   'seed': quality_seed(detected[match['index']]), 'source_size': list(image.size),
                                   'content_hash': row['sha256']} if match else
                                  {'reason': 'No unique native face at the saved position; existing decision retained'})
                        db.execute('INSERT INTO native_migration_faces VALUES(%s,%s,%s,%s,now())',
                                   (face['id'], provider.identity, 'embedded' if match else 'review', Jsonb(result)))
                    status = 'done' if len(matched) == len(faces) else 'review'
                    detail = None
                except Exception as error:
                    db.rollback()
                    from face_detect.contracts import ProviderUnavailable, ProviderTransient
                    if isinstance(error, (ProviderUnavailable, ProviderTransient)) or not isinstance(error, (ValueError, OSError)):
                        raise
                    status, detail = 'error', str(error)[:500]
                finally:
                    if image is not None:
                        image.close()
                db.execute('INSERT INTO native_migration_photos VALUES(%s,%s,%s,%s,%s,now())',
                           (mid, provider.identity, status, detail, time.monotonic()-started))
                db.commit()
                state['photos_this_run'] += 1
                state['completed_photos'] += 1
                state['elapsed_seconds'] = round(time.time() - state['started_at'], 1)
                save(folder/'embedding-progress.json', state)
            else:
                state['state'] = 'staging-complete'
            state['face_results'] = {r['state']: r['n'] for r in db.execute(
                'SELECT state,count(*) n FROM native_migration_faces GROUP BY state')}
            state['photo_results'] = {r['state']: r['n'] for r in db.execute(
                'SELECT state,count(*) n FROM native_migration_photos GROUP BY state')}
    except Exception:
        state['state'] = 'failed'
        raise
    finally:
        provider.close()
        if execution_state is not None:
            execution_state(0x80000000)
        save(folder/'embedding-progress.json', state)
    print(json.dumps(state))


if __name__ == '__main__':
    main()
