import copy

import pytest
from psycopg.types.json import Jsonb

from app import database
from face_detect.catalog import evidence
from face_detect.recognition import people_face_quality as quality
from test_people_face_quality import reject
from test_automatic_people import automatic, pool, env
from test_unexamined_photos import disposable_migrations


def fixture_backup_gate(db, current, pending):
    import re
    assert re.fullmatch(r'peopletest_[0-9a-f]{32}', db.execute('SELECT current_schema() s').fetchone()['s'])
    assert current == {1} and pending == {2}


def test_compact_scan_round_trip_and_blurry_release(automatic):
    with database.connection() as db:
        did = automatic[0]
        meta = reject(db, did)
        items = evidence.load_scan(db, meta['id'])
        original = copy.deepcopy(items)
        compact = evidence.compact_items(db, items)
        assert items == original
        assert compact[0]['vector']['format'] == evidence.MARKER
        assert evidence.expand_items(db, compact) == original
        assert evidence.compact_items(db, compact) == compact
        db.execute('UPDATE auto_people_scan_results SET items=%s WHERE analysis_id=%s',
                   (Jsonb(compact), meta['id']))
        assert evidence.load_scan(db, meta['id']) == original
        assert quality.release(db, did, 'test')['released']
        assert quality.release(db, did, 'test') == {'released': False}


def test_conflicting_or_missing_vector_is_not_silently_accepted(automatic):
    with database.connection() as db:
        meta = reject(db, automatic[0])
        items = evidence.load_scan(db, meta['id'])
        bad = copy.deepcopy(items)
        bad[0]['result']['vector'] = [0., 1.]
        with pytest.raises(ValueError, match='Repeated'):
            evidence.compact_items(db, bad)
        compact = evidence.compact_items(db, items)
        bad = copy.deepcopy(compact)
        bad[0]['vector']['detection_id'] += 1
        with pytest.raises(ValueError, match='reference differs'):
            evidence.expand_items(db, bad)
        db.execute('DELETE FROM embedding WHERE detection_id=%s', (automatic[0],))
        with pytest.raises(ValueError, match='missing'):
            evidence.expand_items(db, compact)


def test_catalog_migration_preserves_metadata_and_is_atomic(automatic):
    with database.connection() as db:
        did = automatic[0]
        meta = reject(db, did)
        items = evidence.load_scan(db, meta['id'])
        result = {'face': {'vector': [1., 0.], 'warnings': [], 'box': {'probability': .9}},
                  'source': 'migration'}
        db.execute("INSERT INTO native_migration_faces(detection_id,space_id,state,result) VALUES(%s,%s,'embedded',%s)",
                   (did, 'synthetic-auto', Jsonb(result)))
        report = evidence.compact_catalog(db, before_upgrade=fixture_backup_gate)
        assert report == {'scan_results': 1, 'reference_records': 1}
        assert evidence.load_scan(db, meta['id']) == items
        saved = evidence.reference_records(db, 'synthetic-auto')[0]['result']
        assert saved['source'] == result['source']
        assert saved['face']['box'] == result['face']['box']
        assert db.execute("SELECT to_regclass('native_migration_faces') t").fetchone()['t'] is None
        assert quality.release(db, did, 'test')['released']


def test_compaction_failure_rolls_back_everything(automatic):
    with database.connection() as db:
        meta = reject(db, automatic[0])
        items = evidence.load_scan(db, meta['id'])
        db.execute('INSERT INTO embedding(detection_id,space_id,vector) VALUES(%s,%s,%s)',
                   (automatic[0], 'synthetic-auto', Jsonb([0., 1.])))
        with pytest.raises(ValueError, match='conflicts'):
            with db.transaction():
                evidence.compact_catalog(db, before_upgrade=fixture_backup_gate)
        assert db.execute("SELECT to_regclass('native_migration_faces') t").fetchone()['t']
        assert not db.execute("SELECT to_regclass('face_reference_evidence') t").fetchone()['t']
        assert evidence.load_scan(db, meta['id']) == items


def test_reference_evidence_is_authoritative_backup_state():
    from app.backup.policy import classify_table, CATALOG
    assert classify_table('people', 'face_reference_evidence') == CATALOG


def test_numeric_equivalence_must_not_change_receipt_serialization():
    assert not evidence._same([1, 0], [1.0, 0.0])
    assert not evidence._same([-0.0], [0.0])
    assert evidence._same({'b': 2, 'a': 1}, {'a': 1, 'b': 2})


def test_empty_scan_remains_a_cached_result(automatic):
    with database.connection() as db:
        aid = db.execute('SELECT analysis_id FROM face_detections WHERE id=%s',
                         (automatic[0],)).fetchone()['analysis_id']
        db.execute('INSERT INTO auto_people_scan_results VALUES(%s,%s)', (aid, Jsonb([])))
        assert evidence.compact_items(db, []) == []
        assert evidence.load_scan(db, aid) == []


def test_reference_evidence_cascades_with_face(automatic):
    with database.connection() as db:
        did = automatic[0]
        db.execute("INSERT INTO native_migration_faces(detection_id,space_id,state,result) VALUES(%s,%s,'review','{}')",
                   (did, 'synthetic-auto'))
        evidence.compact_catalog(db, before_upgrade=fixture_backup_gate)
        db.execute('DELETE FROM face_detections WHERE id=%s', (did,))
        assert not db.execute('SELECT 1 FROM face_reference_evidence').fetchall()


def test_compaction_requires_verified_backup_by_default(automatic):
    with database.connection() as db:
        for supplied in ({}, {'before_upgrade': None}):
            with pytest.raises(RuntimeError, match='verified offline backup'):
                evidence.compact_catalog(db, **supplied)
        assert db.execute("SELECT to_regclass('native_migration_faces') t").fetchone()['t']
        assert not db.execute("SELECT to_regclass('face_reference_evidence') t").fetchone()['t']
