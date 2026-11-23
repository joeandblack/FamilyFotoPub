import os
import uuid
import pytest
from app import database
from app.library import locks as media_locks
from app.imports import importer


def test_content_key_signed_and_collision_safe():
    assert media_locks.content_key('ffffffff'+'0'*56)==-1
    assert media_locks.content_key('7fffffff'+'0'*56)==2**31-1
    assert media_locks.content_key('12345678'+'0'*56)==media_locks.content_key('12345678'+'f'*56)


@pytest.mark.skipif(os.environ.get('FAMILYFOTO_INTEGRATION')!='1',reason='Requires local PostgreSQL')
def test_import_trash_lock_protocol(monkeypatch):
    import psycopg
    worker_key=uuid.uuid4().int%(2**63-1)
    monkeypatch.setattr(importer,'IMPORT_LOCK',worker_key)
    monkeypatch.setattr(importer,'SINGLE_IMPORT_LOCK',uuid.uuid4().int%(2**63-1))
    monkeypatch.setattr(media_locks,'MEDIA_LOCK_NAMESPACE',uuid.uuid4().int%(2**31-1))
    sha='1'*64
    with database.connection() as worker:
        importer.acquire_import_locks(worker)
        key=media_locks.lock_content(worker,sha)
        worker.commit()
        with database.connection() as other:
            with pytest.raises(RuntimeError,match='importer'):
                importer.acquire_import_locks(other)
        with database.connection() as deletion:
            assert deletion.execute('SELECT pg_try_advisory_lock_shared(%s) ok',(worker_key,)).fetchone()['ok']
            deletion.execute("SET lock_timeout='100ms'")
            with pytest.raises(psycopg.errors.LockNotAvailable):
                media_locks.lock_content(deletion,sha)
            deletion.rollback()
            # A different picture remains available while this one is importing.
            other_key=media_locks.lock_content(deletion,'2'*64)
            media_locks.unlock_content(deletion,other_key)
            media_locks.unlock_content(worker,key)
            worker.commit()
            media_locks.lock_content(deletion,sha)
            deletion.commit()  # Durable deletion intent does not release protection.
            assert not worker.execute('SELECT pg_try_advisory_lock(%s,%s) ok',(media_locks.MEDIA_LOCK_NAMESPACE,key)).fetchone()['ok']
            with database.connection() as maintenance:
                assert not maintenance.execute('SELECT pg_try_advisory_lock(%s) ok',(worker_key,)).fetchone()['ok']
        # Request connection closed: importer can continue with this hash.
        media_locks.lock_content(worker,sha)
    with database.connection() as maintenance:
        assert maintenance.execute('SELECT pg_try_advisory_lock(%s) ok',(worker_key,)).fetchone()['ok']
        with database.connection() as worker:
            with pytest.raises(RuntimeError,match='maintenance'):
                importer.acquire_import_locks(worker)
