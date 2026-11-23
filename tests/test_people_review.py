"""Independent concurrency regression checks using isolated synthetic data."""
from concurrent.futures import ThreadPoolExecutor
import threading
import pytest
from test_people_service import pool
from test_uploads import env
from app import database
from face_detect.recognition import people

def concurrent_reject(ids, operation):
    started=threading.Event()
    def attempt():
        with database.connection() as db:
            db.execute("SET LOCAL statement_timeout='5s'")
            started.set()
            return operation(db)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with database.connection() as owner:
            media=people.eligible(owner,[ids[0]])[0]['media_id']
            owner.execute('SELECT id FROM media WHERE id=%s FOR UPDATE',(media,))
            owner.execute("UPDATE face_detections SET review_state='rejected' WHERE id=%s",(ids[0],))
            future=executor.submit(attempt)
            assert started.wait(2)
            # A correctly ordered writer must wait for the source media lock.
            with pytest.raises(TimeoutError): future.result(timeout=.2)
        return future.result(timeout=5)

def test_apply_serializes_with_source_review(pool):
    _,ids=pool
    with database.connection() as db:
        preview=people.preview(db,{'action':'assign','name':'Synthetic','detection_ids':[ids[0]]},'admin')
    def attempt(db):
        with pytest.raises(RuntimeError,match='changed|hidden|superseded'):
            people.apply(db,preview['token'],'admin')
    concurrent_reject(ids,attempt)
    with database.connection() as db:
        assert db.execute('SELECT count(*) n FROM people_assignments').fetchone()['n']==0

def test_apply_and_optout_use_consistent_lock_order(pool):
    _,ids=pool
    with database.connection() as db:
        preview=people.preview(db,{'action':'assign','name':'Synthetic','detection_ids':[ids[0]]},'admin')
        media=people.eligible(db,[ids[0]])[0]['media_id']
    started=threading.Event()
    def attempt():
        with database.connection() as db:
            db.execute("SET LOCAL statement_timeout='5s'")
            started.set()
            with pytest.raises(RuntimeError,match='changed|hidden|superseded|expired'):
                people.apply(db,preview['token'],'admin')
    with ThreadPoolExecutor(max_workers=1) as executor:
        with database.connection() as owner:
            owner.execute("SET LOCAL statement_timeout='5s'")
            owner.execute('SELECT id FROM media WHERE id=%s FOR UPDATE',(media,))
            future=executor.submit(attempt)
            assert started.wait(2)
            with pytest.raises(TimeoutError):future.result(timeout=.2)
            # Cascades and trigger cleanup must not wait for a preview lock held
            # by the correction that is waiting for this media reservation.
            owner.execute('DELETE FROM face_analyses WHERE media_id=%s',(media,))
        future.result(timeout=5)
    with database.connection() as db:
        assert db.execute('SELECT count(*) n FROM people_assignments').fetchone()['n']==0
