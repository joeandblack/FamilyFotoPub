import pytest
import psycopg
from app import database
from app.library.api import library_data
from app.imports.history import history
from test_uploads import env,test_upload_import_duplicates_search_and_cleanup
from test_unexamined_photos import disposable_migrations
from test_import_modes import seed,create_job


def test_cleanup_keeps_counts_originals_and_only_first_membership(env):
    _,config,_=env
    owner,backup=seed(config);later=create_job(config,backup)
    migration=(database.ROOT/'databases'/'024_single_import_policy.sql').read_text()
    with database.connection() as db:
        media=db.execute('SELECT * FROM media').fetchone()
        db.execute("INSERT INTO import_items(job_id,source_path,size,mtime_ns,state,media_id) VALUES(%s,'backup/renamed.mp4',%s,0,'duplicate',%s)",(later,media['size'],media['id']))
        db.execute('ALTER TABLE jobs ADD COLUMN import_mode text')
        db.execute('DELETE FROM schema_versions WHERE version=24')
        before=history(db)
        db.execute(migration)
        assert history(db)==before
        assert db.execute('SELECT * FROM media').fetchone()==media
        assert db.execute("SELECT media_id FROM import_items WHERE job_id=%s",(later,)).fetchone()['media_id'] is None
        db.execute(migration)  # Idempotent, with unchanged counts.
        assert history(db)==before
    assert library_data(import_job=str(owner))['total']==1
    assert library_data(import_job=str(later))['total']==0
    assert library_data()['total']==1


def test_cleanup_refuses_to_remove_only_reference(env):
    _,config,_=env
    owner,_=seed(config)
    with database.connection() as db:
        db.execute("UPDATE import_items SET state='duplicate' WHERE job_id=%s",(owner,))
        db.execute('ALTER TABLE jobs ADD COLUMN import_mode text')
        db.execute('DELETE FROM schema_versions WHERE version=24');db.commit()
        with pytest.raises(psycopg.errors.RaiseException,match='retained original'):
            db.execute((database.ROOT/'databases'/'024_single_import_policy.sql').read_text())
        db.rollback()
        assert db.execute('SELECT media_id FROM import_items').fetchone()['media_id'] is not None
