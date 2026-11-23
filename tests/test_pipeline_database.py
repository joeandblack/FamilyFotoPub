"""Opt-in PostgreSQL tests use session-local temporary tables, never real media."""
import os
import uuid
from unittest.mock import Mock,patch
import pytest
from app.database import connection
from app.imports.history import history
from app.jobs.job_scope import require_import_complete
from app.video.avi import convert_queue
from app import jobs as pipeline


@pytest.mark.skipif(os.environ.get('FAMILYFOTO_INTEGRATION')!='1',reason='Requires local PostgreSQL')
def test_actual_scope_queries_and_shared_media(tmp_path):
    with connection() as db:
        for table in ('jobs','import_items','media','video_metadata','media_trash','media_adjustments'):
            db.execute(f'CREATE TEMP TABLE {table} AS SELECT * FROM public.{table} WITH NO DATA')
        db.execute('ALTER TABLE media ADD COLUMN IF NOT EXISTS first_import_job_id bigint')
        db.execute('ALTER TABLE jobs ADD COLUMN IF NOT EXISTS duplicates_status jsonb')
        db.execute('ALTER TABLE jobs ADD COLUMN IF NOT EXISTS auto_duplicates boolean DEFAULT false')
        db.execute('ALTER TABLE jobs ADD COLUMN IF NOT EXISTS auto_trash_duplicates boolean DEFAULT true')
        db.execute('CREATE TEMP TABLE photo_fingerprints(media_id bigint,version text,stored_sha256 text)')
        db.execute('CREATE TEMP TABLE photo_duplicate_items(job_id bigint,media_id bigint,state text)')
        db.execute("INSERT INTO jobs(id,source_root,state,inventory_complete,auto_metadata,auto_conversion) VALUES (1,'one','completed-review-required',true,false,false),(2,'two','paused',false,false,false)")
        db.execute("UPDATE jobs SET skipped_duplicate_files=0,skipped_duplicate_bytes=0")
        db.execute("INSERT INTO media(id,name,kind,relative_path,size) VALUES (101,'shared','video','shared.avi',10),(102,'other','video','other.avi',20),(103,'done','video','done.mp4',30)")
        db.execute("UPDATE media SET original_relative_path='done.avi',conversion_state='done' WHERE id=103")
        db.execute('UPDATE media SET first_import_job_id=CASE WHEN id=102 THEN 2 ELSE 1 END')
        db.execute("INSERT INTO video_metadata(media_id,state) VALUES (101,'done'),(102,'done'),(103,'done')")
        db.execute("INSERT INTO import_items(job_id,media_id,state,size,source_path) VALUES (1,101,'imported',10,'shared'),(1,101,'duplicate',10,'shared-again'),(2,101,'duplicate',10,'shared'),(2,102,'imported',20,'other'),(1,103,'imported',30,'done'),(2,NULL,'pending',5,'pending')")
        require_import_complete(db,1)
        with pytest.raises(RuntimeError):require_import_complete(db,2)
        before={row['id']:row for row in history(db)}
        assert before[1]['stages']['metadata']=={'done':2}
        assert before[1]['stages']['conversion']=={'pending':1,'done':1}
        converted=[]
        def convert(db,media,*args):
            converted.append(media['id'])
            db.execute("UPDATE media SET conversion_state='done' WHERE id=%s",(media['id'],));db.commit()
        status={}
        with patch('app.video.avi.convert_one',convert),patch('app.video.avi.logger',return_value=Mock()):
            convert_queue(db,{},None,Mock(),status,lambda:None,tmp_path/'PAUSE',1)
        assert converted==[101]
        assert status['state']=='completed-review-required'
        after={row['id']:row for row in history(db)}
        assert after[2]['stages']['conversion']=={'pending':1}
        assert db.execute('SELECT conversion_state FROM media WHERE id=102').fetchone()['conversion_state'] is None


@pytest.mark.skipif(os.environ.get('FAMILYFOTO_INTEGRATION')!='1',reason='Requires local PostgreSQL')
def test_reservation_excludes_another_process_between_stages(tmp_path,monkeypatch):
    # Exercise real session locks without contending with a live media worker.
    for name in ('LOCK','HANDOFF_LOCK','WORKER_LOCK'):
        monkeypatch.setattr(pipeline,name,uuid.uuid4().int % (2**63-1))
    with patch.object(pipeline,'DATA',tmp_path),connection() as first,connection() as second:
        pipeline.idle(first)
        first.execute('SELECT pg_advisory_unlock(%s)',(pipeline.WORKER_LOCK,));first.commit()
        with pytest.raises(RuntimeError,match='active'):pipeline.idle(second)


@pytest.mark.skipif(os.environ.get('FAMILYFOTO_INTEGRATION')!='1',reason='Requires local PostgreSQL')
@pytest.mark.parametrize('poster_error',[None,'Poster unavailable: Decoder produced no poster frame'])
def test_metadata_scope_pause_and_resume(tmp_path,monkeypatch,poster_error):
    import hashlib
    import json
    from contextlib import contextmanager
    from app.video import metadata as videos
    monkeypatch.setattr(videos,'IMPORT_LOCK',uuid.uuid4().int % (2**63-1))
    for name,value in [('post-import-review.json',{'state':'passed'}),('thumbnail-migration.json',{'state':'completed'})]:
        (tmp_path/name).write_text(json.dumps(value))
    payload=b'fixture video bytes';sha=hashlib.sha256(payload).hexdigest()
    (tmp_path/'one.mp4').write_bytes(payload)
    with connection() as db:
        for table in ('jobs','import_items','media','video_metadata','media_trash'):
            db.execute(f'CREATE TEMP TABLE {table} AS SELECT * FROM public.{table} WITH NO DATA')
        db.execute('ALTER TABLE media ADD COLUMN IF NOT EXISTS first_import_job_id bigint')
        db.execute('CREATE UNIQUE INDEX ON video_metadata(media_id)')
        db.execute("ALTER TABLE video_metadata ALTER COLUMN state SET DEFAULT 'pending'")
        db.execute("INSERT INTO jobs(id,state) VALUES (1,'completed-review-required'),(2,'paused')")
        db.execute("INSERT INTO media(id,name,kind,relative_path,size,sha256) VALUES (101,'one.mp4','video','one.mp4',%s,%s),(102,'unread.mp4','video','unread.mp4',1,'other')",(len(payload),sha))
        db.execute("INSERT INTO import_items(job_id,media_id,state) VALUES (1,101,'imported'),(2,102,'imported')")
        db.execute('UPDATE media SET first_import_job_id=CASE WHEN id=101 THEN 1 ELSE 2 END')
        db.commit()
        @contextmanager
        def scoped_connection():yield db
        monkeypatch.setattr(videos,'connection',scoped_connection)
        monkeypatch.setattr(videos,'DATA',tmp_path)
        monkeypatch.setattr(videos,'migrate',lambda:None)
        monkeypatch.setattr(videos,'load_config',lambda:{'destination':{'root':str(tmp_path)}})
        monkeypatch.setattr(videos,'tool_paths',lambda:{})
        monkeypatch.setattr(videos,'background_priority',lambda:None)
        monkeypatch.setattr(videos,'readonly_monitor',lambda config:Mock())
        monkeypatch.setattr(videos,'logger',lambda name:Mock())
        with patch.object(videos,'extract',return_value=({'duration_seconds':1,'poster':poster_error is None},None,poster_error)) as extract:
            (tmp_path/'VIDEO-PAUSE').touch();videos.run(job_id=1)
            extract.assert_not_called()
            assert json.loads((tmp_path/'video-status.json').read_text())['state']=='paused'
            (tmp_path/'VIDEO-PAUSE').unlink();videos.run(job_id=1)
            extract.assert_called_once()
            videos.run(job_id=1);extract.assert_called_once()
        assert db.execute('SELECT media_id,state FROM video_metadata').fetchall()==[{'media_id':101,'state':'warning' if poster_error else 'done'}]
        saved=db.execute('SELECT metadata_status FROM jobs WHERE id=1').fetchone()['metadata_status']
        assert saved['job_id']==1 and saved['counts']=={('warning' if poster_error else 'done'):1}
        assert db.execute('SELECT metadata_status FROM jobs WHERE id=2').fetchone()['metadata_status'] is None
