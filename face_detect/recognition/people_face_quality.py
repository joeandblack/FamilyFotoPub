"""Conservative, versioned quality triage for newly detected faces only."""
import math
import numpy as np
from PIL import Image
from psycopg.types.json import Jsonb
from face_detect.recognition import ignored_faces
from face_detect.recognition import people

POLICY = 'face-crop-quality-v2'
MIN_EDGE = 32
MIN_SHARPNESS = 20.0
DARK_MEAN = 35.0
DARK_P90 = 60.0
NOT_BLURRY = " AND NOT EXISTS(SELECT 1 FROM auto_people_face_quality q WHERE q.detection_id=d.id AND q.state='blurry')"


def assess(image, box):
    bounds = (max(0, math.floor(box['x_min'])), max(0, math.floor(box['y_min'])),
              min(image.width, math.ceil(box['x_max'])), min(image.height, math.ceil(box['y_max'])))
    crop = image.crop(bounds).convert('L')
    edge = min(crop.size)
    # Downsample only, never upscale a tiny detection to manufacture detail.
    crop.thumbnail((96, 96), Image.Resampling.LANCZOS)
    pixels = np.asarray(crop, dtype=np.float32)
    if min(pixels.shape) < 3:
        sharpness = 0.0
    else:
        laplacian = (pixels[:-2, 1:-1] + pixels[2:, 1:-1] + pixels[1:-1, :-2]
                     + pixels[1:-1, 2:] - 4 * pixels[1:-1, 1:-1])
        sharpness = float(laplacian.var())
    mean_luminance = float(pixels.mean()) if pixels.size else 0.0
    p90_luminance = float(np.percentile(pixels, 90)) if pixels.size else 0.0
    # Require a broadly dark crop, not just shadows around a readable face.
    too_dark = mean_luminance < DARK_MEAN and p90_luminance < DARK_P90
    reason = ('too-small' if edge < MIN_EDGE else 'too-dark' if too_dark
              else 'blurry' if sharpness < MIN_SHARPNESS else None)
    return {'policy': POLICY, 'reason': reason, 'min_edge': edge,
            'sharpness': round(sharpness, 4), 'sample_size': list(crop.size),
            'mean_luminance': round(mean_luminance, 4), 'p90_luminance': round(p90_luminance, 4)}


def visible_sql():
    return ignored_faces.BASE + """ AND m.kind='image' AND NOT d.ignored AND d.review_state<>'rejected'
        AND EXISTS(SELECT 1 FROM auto_people_face_quality q WHERE q.detection_id=d.id AND q.state='blurry')"""


def collection(db, page=1):
    from face_detect.recognition.automatic_people import guard
    guard()
    where = visible_sql()
    total = db.execute('SELECT count(*) n ' + where).fetchone()['n']
    pages = max(1, (total + 99) // 100)
    page = min(max(1, page), pages)
    items = db.execute('SELECT d.id detection_id,a.media_id,m.name, '
                       '(SELECT metrics FROM auto_people_face_quality q WHERE q.detection_id=d.id) quality '
                       + where + ' ORDER BY d.id LIMIT 100 OFFSET %s', ((page - 1) * 100,)).fetchall()
    return {'items': items, 'total': total, 'page': page, 'pages': pages, 'page_size': 100}


def release(db, did, actor):
    from face_detect.recognition import automatic_people as service
    from face_detect.recognition.grouping import unit
    from face_detect.catalog.spaces import detector_key, space_key
    service.guard()
    people._lock(db)
    people._lock_media(db, [did])
    row = db.execute('SELECT a.id analysis_id,a.media_id,a.detector_id,m.sha256 '
                     + ignored_faces.BASE + " AND d.id=%s AND m.kind='image' AND NOT d.ignored AND d.review_state<>'rejected'", (did,)).fetchone()
    if not row:
        raise LookupError('Face is no longer available')
    quality = db.execute('SELECT state FROM auto_people_face_quality WHERE detection_id=%s FOR UPDATE', (did,)).fetchone()
    if not quality:
        raise LookupError('Blurry face unavailable')
    if quality['state'] == 'released':
        return {'released': False}
    from face_detect.catalog.evidence import load_scan
    saved = load_scan(db, row['analysis_id'])
    item = next((i for i in saved if i['input']['id'] == did), None) if saved else None
    # A scan result saved before the one catalog names its model by the old ID; the catalog
    # keeps that as each detector's and space's legacy ID.
    result = item['result'] if item else {}
    detector = result.get('detector_id') or detector_key(db, result.get('model_id'))
    space = result.get('space_id') or space_key(db, result.get('model_id'))
    if not item or item['state'] != 'done' or item['input']['media_id'] != row['media_id'] or item['input']['content_hash'] != row['sha256'] or detector != row['detector_id'] or space is None:
        raise ValueError('Stored face evidence is unavailable; no changes were made')
    manifest = db.execute('SELECT manifest FROM space WHERE space_id=%s', (space,)).fetchone()
    _, norm = unit(item['vector'], manifest['manifest']['dimensions'])
    if abs(norm - 1) > .001 or item['vector'] != item['result']['vector']:
        raise ValueError('Stored face evidence differs; no changes were made')
    if db.execute('SELECT 1 FROM auto_people_members WHERE detection_id=%s', (did,)).fetchone():
        raise ValueError('Face already belongs to a group')
    existing = db.execute('SELECT vector FROM embedding WHERE detection_id=%s AND space_id=%s',
                          (did, space)).fetchone()
    if existing and existing['vector'] != item['vector']:
        raise ValueError('Stored face evidence differs; no changes were made')
    db.execute('INSERT INTO embedding(detection_id,space_id,vector) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING',
               (did, space, Jsonb(item['vector'])))
    # A manual release permits review/matching, not a low-quality seed for clustering.
    db.execute('INSERT INTO auto_people_observations VALUES(%s,%s,false)', (did, space))
    gid = db.execute('INSERT INTO auto_people_groups DEFAULT VALUES RETURNING id').fetchone()['id']
    service.put_member(db, did, gid, 'manual', space)
    db.execute("UPDATE auto_people_face_quality SET state='released',released_at=now() WHERE detection_id=%s", (did,))
    service.event(db, 'blurry-release', 'manual', actor, {'detection_id': did, 'group_id': gid})
    return {'released': True, 'group_id': gid}
