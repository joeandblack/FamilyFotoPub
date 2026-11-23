from psycopg.types.json import Jsonb
from test_automatic_people import automatic,pool,env,observations,setup_groups
from face_detect.recognition import automatic_people as auto
from app import database
from face_detect.recognition.people_embedding_cache import EmbeddingCache


def test_cached_cohort_reads_mutable_visibility_and_vector_versions(automatic,monkeypatch):
    with database.connection() as db:
        setup_groups(db,automatic)
        expected=auto.cohort(db,'synthetic-auto')
        cache=EmbeddingCache();monkeypatch.setattr(auto,'embedding_cache',cache)
        first=auto.cohort(db,'synthetic-auto')
        assert [{**r,'vector':list(r['vector'])} for r in first]==expected
        vector=first[0]['vector']
        assert auto.cohort(db,'synthetic-auto')[0]['vector'] is vector
        did=automatic[0]
        db.execute('UPDATE embedding SET vector=%s WHERE detection_id=%s AND space_id=%s',(Jsonb([0.,1.]),did,'synthetic-auto'))
        assert list(next(r for r in auto.cohort(db,'synthetic-auto') if r['id']==did)['vector'])==[0.,1.]
        db.execute('UPDATE face_detections SET ignored=true WHERE id=%s',(did,))
        assert did not in {r['id'] for r in auto.cohort(db,'synthetic-auto')}
        db.execute('UPDATE face_detections SET ignored=false WHERE id=%s',(did,))
        assert did in {r['id'] for r in auto.cohort(db,'synthetic-auto')}
        db.execute('DELETE FROM embedding WHERE detection_id=%s',(did,))
        assert did not in {r['id'] for r in auto.cohort(db,'synthetic-auto')}


def test_eviction_never_drops_results_and_metadata_is_fresh(automatic):
    with database.connection() as db:
        setup_groups(db,automatic)
        cache=EmbeddingCache(max_bytes=16)
        first=cache.records(db,'synthetic-auto')
        assert len(first)==len(automatic) and cache.bytes<=16
        assert len(cache.records(db,'synthetic-auto'))==len(first)
        db.execute('UPDATE auto_people_observations SET seed=false WHERE detection_id=%s',(automatic[0],))
        assert next(r for r in cache.records(db,'synthetic-auto') if r['detection_id']==automatic[0])['seed'] is False
        assert cache.records(db,'other-model')==[] and not cache.entries


def test_cache_does_not_reuse_rolled_back_vectors(automatic):
    class Rollback(Exception):pass
    import pytest
    with database.connection() as db:
        setup_groups(db,automatic);cache=EmbeddingCache()
        original={r['detection_id']:list(r['vector']) for r in cache.records(db,'synthetic-auto')}
        with pytest.raises(Rollback):
            with db.transaction():
                db.execute('UPDATE embedding SET vector=%s WHERE detection_id=%s',(Jsonb([.6,.8]),automatic[0]))
                assert list(cache.records(db,'synthetic-auto')[0]['vector'])==[.6,.8]
                raise Rollback()
        assert {r['detection_id']:list(r['vector']) for r in cache.records(db,'synthetic-auto')}==original
