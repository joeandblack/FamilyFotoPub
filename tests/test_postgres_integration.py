"""Opt-in PostgreSQL test uses a disposable isolated schema and D: fixtures."""
import os
import json
import uuid
from pathlib import Path
import pytest
from PIL import Image
from psycopg import sql
from app import database
from app.imports import importer
from app.config import load_config

@pytest.mark.skipif(os.environ.get('FAMILYFOTO_INTEGRATION')!='1',reason='Requires installed local PostgreSQL')
def test_postgres_import_resume_and_duplicates(tmp_path,monkeypatch):
    from app import telemetry
    monkeypatch.setattr(telemetry,'LOGS',tmp_path/'logs')
    config=load_config()
    schema='fftest_'+uuid.uuid4().hex
    with database.connection() as db:
        db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    original=dict(config['database'])
    source=tmp_path/'source';source.mkdir()
    destination=tmp_path/'destination';destination.mkdir()
    data=tmp_path/'data';data.mkdir();(data/'thumbnails').mkdir()
    from app.auth import password_hash
    config.pop('browser_password',None)
    config={**config,'auth_secret':'fixture-secret-'*4,'host':'0.0.0.0','browser_user':'fixture-admin','browser_password_hash':password_hash('fixture-password'),'browser_accounts':{},
            'source':{'root':str(source)},'destination':{'root':str(destination)},'io_mibps':0,
            'database':{**original,'options':f'-c search_path={schema}'}}
    try:
        Image.new('RGB',(50,40),(0,100,50)).save(source/'original.jpg')
        (source/'duplicate.JPG').write_bytes((source/'original.jpg').read_bytes())
        (source/'clip.mp4').write_bytes(b'test video payload')
        (source/'notes.unknown').write_text('retain original')
        # NTFS epoch timestamp previously overflowed PostgreSQL bigint.
        os.utime(source/'notes.unknown',ns=(-11644473599999999900,-11644473599999999900))
        # Only NTFS stores the 1601 epoch exactly; other filesystems clamp it.
        if os.name == 'nt':
            assert (source/'notes.unknown').stat().st_mtime_ns == -11644473599999999900
        originals={p.name:(p.read_bytes(),p.stat().st_mtime_ns) for p in source.iterdir()}
        class FakeMonitor:
            def __init__(self,config):self.thread=True
            def prepare(self):pass
            def check(self):pass
            def close(self):pass
        monkeypatch.setattr(database,'load_config',lambda:config)
        monkeypatch.setattr(importer,'load_config',lambda:config)
        monkeypatch.setattr(importer,'Monitor',FakeMonitor)
        monkeypatch.setattr('app.storage.config.enroll_source',lambda c,p:{**c['source'],'root':str(p)})
        monkeypatch.setattr(importer,'DATA',data)
        monkeypatch.setattr(importer,'IMPORT_LOCK',uuid.uuid4().int % (2**63 - 1))
        monkeypatch.setattr(importer,'SINGLE_IMPORT_LOCK',uuid.uuid4().int % (2**63 - 1))
        database.migrate()
        (data/'PAUSE').touch()
        importer.run()
        with database.connection() as db:
            assert db.execute('SELECT state FROM jobs').fetchone()['state']=='paused'
            original_start=db.execute('SELECT started_at FROM jobs').fetchone()['started_at']
            assert db.execute('SELECT count(*) count FROM media').fetchone()['count']==0
        (data/'PAUSE').unlink()
        importer.run()
        importer.run()
        with database.connection() as db:
            assert db.execute('SELECT started_at FROM jobs').fetchone()['started_at']==original_start
        assert json.loads((data/'import-status.json').read_text())['started_at']==original_start.isoformat()
        assert (data/'performance.json').is_file()
        assert json.loads((data/'performance.json').read_text())['calls']['progress_reporting']>=1
        from fastapi.testclient import TestClient
        from app import api
        from routers import CONFIG_MODULES, DATA_MODULES, patch_all
        patch_all(monkeypatch,CONFIG_MODULES,'load_config',lambda:config)
        patch_all(monkeypatch,DATA_MODULES,'DATA',data)
        with TestClient(api.app) as client:
            assert client.post('/api/login',json={'username':'fixture-admin','password':'fixture-password'}).status_code==200
            response=client.get('/api/media')
            assert response.status_code==200
            assert response.json()['total']==2
            assert response.json()['page_size']==1000
            review=client.get('/api/issues').json()
            assert len(review['imports'])==1
            batch=review['imports'][0]
            assert batch['source_root']==str(source)
            assert batch['errors']==[]
            assert [(g['extension'],g['count']) for g in batch['unsupported']]==[('.unknown',1)]
            assert '*.txt' in client.get('/api/import/ignore-patterns').json()['patterns']
            assert client.get('/api/media?kind=image&year=unknown').json()['total']==1
            assert client.get('/api/media?search=clip').json()['total']==1
            assert client.post('/api/videos/pause').status_code==200
            assert (data/'VIDEO-PAUSE').is_file()
            (data/'VIDEO-PAUSE').unlink()
        with database.connection() as db:
            assert db.execute('SELECT count(*) count FROM media').fetchone()['count']==2
            counts={r['state']:r['count'] for r in db.execute('SELECT state,count(*) count FROM import_items GROUP BY state').fetchall()}
            assert counts=={'imported':2,'duplicate':1,'unsupported':1}
            assert db.execute('SELECT state FROM jobs').fetchone()['state']=='completed-review-required'
        assert originals=={p.name:(p.read_bytes(),p.stat().st_mtime_ns) for p in source.iterdir()}
        from app.video import metadata as videos
        monkeypatch.setattr(videos,'IMPORT_LOCK',importer.IMPORT_LOCK)
        monkeypatch.setattr(videos,'DATA',data)
        monkeypatch.setattr(videos,'load_config',lambda:config)
        monkeypatch.setattr(videos,'tool_paths',lambda:{})
        monkeypatch.setattr(videos,'background_priority',lambda:None)
        monkeypatch.setattr(videos,'readonly_monitor',lambda c:FakeMonitor(c))
        monkeypatch.setattr(videos,'extract',lambda *a:({'duration_seconds':1,'width':640,'height':480,'poster':False},None,'Test decoder warning'))
        (data/'post-import-review.json').write_text(json.dumps({'state':'passed'}))
        (data/'thumbnail-migration.json').write_text(json.dumps({'state':'completed'}))
        with database.connection() as db:
            job_id=db.execute('SELECT id FROM jobs').fetchone()['id']
            assert all(row['first_import_job_id']==job_id for row in db.execute('SELECT first_import_job_id FROM media').fetchall())
        videos.run(max_items=1,job_id=job_id)
        with database.connection() as db:
            assert db.execute('SELECT state FROM video_metadata').fetchone()['state']=='warning'
            assert db.execute('SELECT state FROM jobs').fetchone()['state']=='completed-review-required'
            assert db.execute("SELECT count(*) count FROM import_items WHERE state='error'").fetchone()['count']==0
        assert originals=={p.name:(p.read_bytes(),p.stat().st_mtime_ns) for p in source.iterdir()}
        with database.connection() as db:
            clip=db.execute("SELECT * FROM media WHERE kind='video'").fetchone()
            clip_item_id=db.execute('SELECT id FROM import_items WHERE media_id=%s',(clip['id'],)).fetchone()['id']
        target=destination/clip['relative_path']
        target.unlink()
        with database.connection() as db:
            db.execute("UPDATE import_items SET state='pending' WHERE id=%s",(clip_item_id,))
        importer.run()
        assert target.read_bytes()==originals['clip.mp4'][0]
        with database.connection() as db:
            assert db.execute('SELECT first_import_job_id FROM media WHERE id=%s',(clip['id'],)).fetchone()['first_import_job_id']==job_id
            assert db.execute('SELECT count(*) count FROM media').fetchone()['count']==2
            assert db.execute('SELECT state FROM jobs').fetchone()['state']=='completed-review-required'
            # Recovery records a duplicate audit row without a media link.
            db.execute("UPDATE import_items SET state='pending' WHERE id=%s",(clip_item_id,))
        target.write_bytes(b'corrupt destination fixture')
        with pytest.raises(OSError,match='incorrect checksum; not overwritten'):
            importer.run()
        assert target.read_bytes()==b'corrupt destination fixture'
        assert originals=={p.name:(p.read_bytes(),p.stat().st_mtime_ns) for p in source.iterdir()}
        with database.connection() as db:
            assert db.execute('SELECT state FROM jobs').fetchone()['state']=='paused-error'
        # A user-deleted duplicate must not recreate the destination or stop the batch.
        target.unlink()
        with database.connection() as db:
            db.execute('INSERT INTO media_trash(media_id,deleting_at,deleted_at) VALUES(%s,now(),now())',(clip['id'],))
        importer.run()
        assert not target.exists()
        with database.connection() as db:
            assert db.execute('SELECT state FROM jobs').fetchone()['state']=='completed-review-required'
            item=db.execute('SELECT state,media_id FROM import_items WHERE id=%s',(clip_item_id,)).fetchone()
            assert item=={'state':'duplicate','media_id':None}
            assert db.execute('SELECT first_import_job_id FROM media WHERE id=%s',(clip['id'],)).fetchone()['first_import_job_id']==job_id
        assert originals=={p.name:(p.read_bytes(),p.stat().st_mtime_ns) for p in source.iterdir()}
    finally:
        import psycopg
        with psycopg.connect(**original) as db:
            db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
