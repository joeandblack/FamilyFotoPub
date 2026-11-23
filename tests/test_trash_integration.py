"""Real PostgreSQL lifecycle in an isolated schema; all media are disposable fixtures."""
import os
import uuid
import pytest
import psycopg
from psycopg import sql
from app import database
from app.library import api
from app.library import trash
from app.imports import importer
from app.api import app as full_app
from routers import CONFIG_MODULES, DATA_MODULES, patch_all
from app.config import load_config
from test_trash import fixture


@pytest.mark.skipif(os.environ.get('FAMILYFOTO_INTEGRATION')!='1',reason='Requires local PostgreSQL')
def test_trash_lifecycle(tmp_path,monkeypatch):
    root,config,media=fixture(tmp_path,monkeypatch)
    original=load_config()['database']
    schema='trash_test_'+uuid.uuid4().hex
    with psycopg.connect(**original) as db:
        db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    config['database']={**original,'options':f'-c search_path={schema}'}
    monkeypatch.setattr(database,'load_config',lambda:config)
    monkeypatch.setattr(trash,'DATA',tmp_path)
    monkeypatch.setattr(trash,'WORKER_LOCK',uuid.uuid4().int%(2**63-1))
    monkeypatch.setattr(importer,'IMPORT_LOCK',trash.WORKER_LOCK)
    monkeypatch.setattr(importer,'SINGLE_IMPORT_LOCK',uuid.uuid4().int%(2**63-1))
    try:
        database.migrate()
        with database.connection() as db:
            id=db.execute("INSERT INTO media(sha256,relative_path,name,kind,size) VALUES (%s,'test.jpg','test.jpg','image',25) RETURNING id",(media['sha256'],)).fetchone()['id']
        assert api.library_data()['total']==1
        with database.connection() as db:
            album=db.execute("INSERT INTO albums(name) VALUES ('Disposable album') RETURNING id").fetchone()['id']
            db.execute('INSERT INTO album_items(album_id,media_id,position,is_cover) VALUES (%s,%s,1,true)',(album,id))
        before=(root/'test.jpg').read_bytes()
        from fastapi.testclient import TestClient
        from PIL import Image
        from io import BytesIO
        config['host']='127.0.0.1'
        from app.auth import password_hash
        config.update(browser_user='fixture',browser_password_hash=password_hash('fixture'),auth_secret='fixture-secret-'*4)
        patch_all(monkeypatch,CONFIG_MODULES,'load_config',lambda:config)
        patch_all(monkeypatch,DATA_MODULES,'DATA',tmp_path)
        thumbnails=tmp_path/'thumbnails';thumbnails.mkdir()
        Image.new('RGB',(100,80),'red').save(thumbnails/(media['sha256']+'.jpg'))
        with TestClient(full_app) as client:
            assert client.post('/api/login',json={'username':'fixture','password':'fixture'}).status_code==200
            crop={'x':.1,'y':.25,'width':.5,'height':.5}
            assert client.post(f'/api/media/{id}/crop',json=crop).json()=={'crop':crop}
            assert api.library_data()['items'][0]['crop']==crop
            thumbnail=client.get(f'/api/media/{id}/thumbnail')
            assert Image.open(BytesIO(thumbnail.content)).size==(50,40)
            assert client.post(f'/api/media/{id}/crop',json={**crop,'width':2}).status_code==422
            assert client.post(f'/api/media/{id}/crop',content='null',headers={'Content-Type':'application/json'}).json()=={'crop':None}
            assert api.library_data()['items'][0]['crop'] is None
            assert (root/'test.jpg').read_bytes()==before
            monkeypatch.setattr('app.storage.disks.volume_serial',lambda drive:'TEST')
            response=client.get(f'/api/media/{id}/download')
            assert response.status_code==200 and response.content==before
            assert 'attachment;' in response.headers['content-disposition']
            assert 'test.jpg' in response.headers['content-disposition']
        for angle in (90,180,270,0):
            assert api.rotate(id)=={'rotation':angle}
            assert api.library_data()['items'][0]['rotation']==angle
            assert (root/'test.jpg').read_bytes()==before
        # A live maintenance reservation and decoder lease must not block
        # photo visibility, but still protect videos and permanent deletion.
        from fastapi import HTTPException
        from app.library.locks import lock_content
        with database.connection() as db:
            video_id=db.execute("INSERT INTO media(sha256,relative_path,name,kind,size) VALUES (%s,'clip.avi','clip.avi','video',25) RETURNING id",('b'*64,)).fetchone()['id']
        with database.connection() as maintenance:
            maintenance.execute('SELECT pg_advisory_lock(%s)',(trash.WORKER_LOCK,))
            maintenance.commit()
            (tmp_path/'video-decoder-busy.json').touch()
            with database.connection() as db:trash.change(db,id,'trash',config)
            assert api.library_data(kind='image')['total']==0
            assert api.library_data(kind='trash')['total']==1
            assert (root/'test.jpg').read_bytes()==before
            for target,action in [(video_id,'trash'),(video_id,'restore'),(id,'delete')]:
                with pytest.raises(HTTPException) as blocked:
                    with database.connection() as db:trash.change(db,target,action,config)
                assert blocked.value.status_code==409
            with database.connection() as db:trash.change(db,id,'restore',config)
            assert api.library_data(kind='image')['total']==1
            assert api.library_data(kind='trash')['total']==0
            # Both forms of same-photo serialization remain effective, even
            # though the unrelated video worker's global lock is bypassed.
            for content in (True,False):
                with database.connection() as holder:
                    if content:lock_content(holder,media['sha256'])
                    else:holder.execute('SELECT id FROM media WHERE id=%s FOR UPDATE',(id,))
                    with pytest.raises(psycopg.errors.LockNotAvailable):
                        with database.connection() as db:
                            db.execute("SET lock_timeout='100ms'")
                            trash.change(db,id,'trash',config)
            assert (root/'test.jpg').read_bytes()==before
            (tmp_path/'video-decoder-busy.json').unlink()
        with database.connection() as db:
            db.execute('DELETE FROM media WHERE id=%s',(video_id,))
        with database.connection() as worker:
            importer.acquire_import_locks(worker)
            worker.commit()
            with database.connection() as db:trash.change(db,id,'trash',config)
            assert api.library_data()['total']==0 and api.library_data(kind='trash')['total']==1
            assert (root/'test.jpg').exists()
            with database.connection() as db:trash.change(db,id,'restore',config)
            assert api.library_data()['total']==1 and api.library_data(kind='trash')['total']==0
            with database.connection() as db:trash.change(db,id,'trash',config)
            with database.connection() as db:trash.change(db,id,'delete',config)
        with database.connection() as db:
            assert not db.execute('SELECT 1 FROM album_items WHERE album_id=%s',(album,)).fetchone()
            assert db.execute('SELECT 1 FROM albums WHERE id=%s',(album,)).fetchone()
        assert not (root/'test.jpg').exists()
        assert api.library_data()['total']==api.library_data(kind='trash')['total']==0
        with pytest.raises(Exception) as rotated:api.rotate(id)
        assert rotated.value.status_code==409
        with database.connection() as db:
            assert db.execute('SELECT count(*) n FROM media').fetchone()['n']==1
        with pytest.raises(Exception) as error:api.media_file(id,'thumbnail')
        assert error.value.status_code==404
    finally:
        with psycopg.connect(**original) as db:
            db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
