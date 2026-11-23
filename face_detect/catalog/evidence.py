"""Store each face vector once, while retaining replay and quality evidence.

Old JSON payloads remain readable. Compaction is explicit, never a read side effect.
Metadata stays verbatim so cached receipts and matching eligibility do not change.
"""
from copy import deepcopy
import json
import math

from psycopg.types.json import Jsonb

from app.database.schema_upgrading import require_catalog_backup
from face_detect.catalog.spaces import space_key

MARKER = 'familyfoto-embedding-reference-v1'


def _same(left, right):
    # Numeric equality alone treats 1 and 1.0 as equal, but receipt hashes do not.
    return json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(right, sort_keys=True, allow_nan=False)


def reference_records(db, space):
    """Read canonical quality references, with old-installation compatibility."""
    from psycopg import sql
    canonical = db.execute("SELECT to_regclass('face_reference_evidence') present").fetchone()['present']
    table = 'face_reference_evidence' if canonical else 'native_migration_faces'
    if not db.execute('SELECT to_regclass(%s) present', (table,)).fetchone()['present']:
        return []
    return db.execute(sql.SQL("SELECT detection_id,result FROM {} WHERE space_id=%s AND state='embedded'")
                      .format(sql.Identifier(table)), (space,)).fetchall()


def _reference(did, space):
    return {'format': MARKER, 'detection_id': did, 'space_id': space}


def _vector(db, did, space, value):
    if isinstance(value, dict):
        if value != _reference(did, space):
            raise ValueError('Face vector reference differs from its evidence')
        row = db.execute('SELECT vector FROM embedding WHERE detection_id=%s AND space_id=%s',
                         (did, space)).fetchone()
        if not row:
            raise ValueError('Referenced face vector is missing')
        return row['vector']
    return value


def _persist(db, did, space, vector):
    manifest = db.execute('SELECT manifest FROM space WHERE space_id=%s', (space,)).fetchone()
    if (not manifest or not isinstance(vector, list)
            or len(vector) != manifest['manifest']['dimensions']
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in vector)):
        raise ValueError('Invalid face evidence vector')
    db.execute('INSERT INTO embedding(detection_id,space_id,vector) VALUES(%s,%s,%s) '
               'ON CONFLICT DO NOTHING', (did, space, Jsonb(vector)))
    saved = db.execute('SELECT vector FROM embedding WHERE detection_id=%s AND space_id=%s',
                       (did, space)).fetchone()
    if not saved or not _same(saved['vector'], vector):
        raise ValueError('Face evidence conflicts with the catalog vector')


def _space(db, result):
    value = result.get('space_id') or space_key(db, result.get('model_id'))
    if not value:
        raise ValueError('Face evidence has no registered embedding space')
    return value


def expand_items(db, items):
    """Reconstruct the original checkpoint exactly, including both vector fields."""
    expanded = deepcopy(items)
    for item in expanded:
        result = item.get('result', {})
        if not any(isinstance(value, dict) for value in (item.get('vector'), result.get('vector'))):
            continue
        did, space = item['input']['id'], _space(db, result)
        for owner in (item, result):
            if 'vector' in owner:
                owner['vector'] = _vector(db, did, space, owner['vector'])
    return expanded


def compact_items(db, items):
    compact = deepcopy(items)
    for item in compact:
        result = item.get('result', {})
        if 'vector' not in item and 'vector' not in result:
            continue
        did, space = item['input']['id'], _space(db, result)
        vectors = [_vector(db, did, space, owner['vector'])
                   for owner in (item, result) if 'vector' in owner]
        if any(not _same(v, vectors[0]) for v in vectors[1:]):
            raise ValueError('Repeated face evidence vectors differ')
        _persist(db, did, space, vectors[0])
        for owner in (item, result):
            if 'vector' in owner:
                owner['vector'] = _reference(did, space)
    if not _same(expand_items(db, compact), expand_items(db, items)):
        raise ValueError('Face evidence compaction changed checkpoint content')
    return compact


def load_scan(db, analysis_id):
    row = db.execute('SELECT items FROM auto_people_scan_results WHERE analysis_id=%s',
                     (analysis_id,)).fetchone()
    return expand_items(db, row['items']) if row else None


def compact_migration_result(db, did, space, result):
    """Keep reference geometry/provenance, remove only a duplicate vector array."""
    value = deepcopy(result)
    face = value.get('face')
    if isinstance(face, dict) and 'vector' in face:
        vector = _vector(db, did, space, face['vector'])
        _persist(db, did, space, vector)
        face['vector'] = _reference(did, space)
        restored = deepcopy(value)
        restored['face']['vector'] = vector
        original = deepcopy(result)
        original['face']['vector'] = _vector(db, did, space, original['face']['vector'])
        if not _same(restored, original):
            raise ValueError('Migration quality evidence changed')
    return value


def compact_catalog(db, *, before_upgrade=require_catalog_backup):
    """Offline transaction body, after caller has backed up and stopped all writers.

    Do not commit here: any inconsistent vector or round-trip failure must roll back
    the whole operation. Deploy compatible readers before using the compact format.
    """
    from psycopg import sql
    versions = {row['version'] for row in db.execute('SELECT version FROM catalog_versions').fetchall()}
    if versions != {1}:
        raise ValueError('Unsupported People catalog version for evidence compaction')
    (before_upgrade or require_catalog_backup)(db, versions, {2})
    if db.execute("SELECT to_regclass('face_reference_evidence') present").fetchone()['present']:
        raise ValueError('Reference evidence migration already applied')
    db.execute('LOCK TABLE auto_people_scan_results, native_migration_faces, embedding, '
               'face_detections, face_analyses IN ACCESS EXCLUSIVE MODE')
    db.execute('''CREATE TABLE face_reference_evidence(
        detection_id bigint PRIMARY KEY REFERENCES face_detections(id) ON DELETE CASCADE,
        space_id text NOT NULL REFERENCES space(space_id),
        state text NOT NULL CHECK(state IN ('embedded','review')),
        result jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now())''')
    scans = references = 0
    # Iterate by key instead of fetching the entire old multi-hundred-MB payload.
    for table, key in (('auto_people_scan_results', 'analysis_id'), ('native_migration_faces', 'detection_id')):
        last = 0
        while True:
            batch = db.execute(sql.SQL('SELECT * FROM {} WHERE {}>%s ORDER BY {} LIMIT 20').format(
                sql.Identifier(table), sql.Identifier(key), sql.Identifier(key)), (last,)).fetchall()
            if not batch:
                break
            for row in batch:
                last = row[key]
                if table == 'auto_people_scan_results':
                    for item in row['items']:
                        owner = db.execute('SELECT analysis_id FROM face_detections WHERE id=%s',
                                           (item['input']['id'],)).fetchone()
                        if not owner or owner['analysis_id'] != last:
                            raise ValueError('Cached face belongs to a different analysis')
                    value = compact_items(db, row['items'])
                    db.execute('UPDATE auto_people_scan_results SET items=%s WHERE analysis_id=%s',
                               (Jsonb(value), last))
                    if not _same(load_scan(db, last), expand_items(db, row['items'])):
                        raise ValueError('Stored checkpoint changed during compaction')
                    scans += 1
                else:
                    value = compact_migration_result(db, last, row['space_id'], row['result'])
                    db.execute('INSERT INTO face_reference_evidence VALUES(%s,%s,%s,%s,%s)',
                               (last, row['space_id'], row['state'], Jsonb(value), row['created_at']))
                    stored = db.execute('SELECT result FROM face_reference_evidence WHERE detection_id=%s',
                                        (last,)).fetchone()['result']
                    if stored != value:
                        raise ValueError('Stored reference evidence changed during migration')
                    references += 1
    # RESTRICT refuses unexpected dependencies; never cascade into application data.
    db.execute('DROP TABLE native_migration_faces RESTRICT')
    db.execute('INSERT INTO catalog_versions(version) VALUES(2)')
    return {'scan_results': scans, 'reference_records': references}
