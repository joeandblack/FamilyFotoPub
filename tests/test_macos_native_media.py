"""Opt-in native ExFAT/APFS smoke test; only generated fixtures are touched."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid

import pytest
from PIL import Image
from psycopg import sql

pytestmark = pytest.mark.skipif(sys.platform != 'darwin' or not os.environ.get('FAMILYFOTO_MAC_TEST_VOLUME'), reason='Explicit native test volume required')


def test_native_import_edit_playback_and_conversion(tmp_path, monkeypatch):
    from app import config, database, api, telemetry
    from app.storage import disks
    from app.storage import config as storage_config
    from app.video import metadata as videos
    from app.video import conversions
    from app.imports import importer
    from app.service import macos_lifecycle
    from app.library import trash
    from app.storage.macos_disks import drive_for_path
    from app.photos.thumbnails import find_thumbnail
    from fastapi.testclient import TestClient
    original = config.load_config()
    schema = 'mactest_' + uuid.uuid4().hex
    with database.connection() as db:
        db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    # Automatic cleanup applies only to a freshly created, uniquely named test folder.
    with tempfile.TemporaryDirectory(prefix='.familyfoto-test-', dir=os.environ['FAMILYFOTO_MAC_TEST_VOLUME']) as scratch:
        dest = Path(scratch) / 'media'
        source = tmp_path / 'source'; source.mkdir()
        Image.new('RGB', (96, 64), (25, 130, 210)).save(source/'fixture.jpg')
        subprocess.run([original['video_tools']['ffmpeg'],'-v','error','-f','lavfi','-i',
                        'color=c=blue:s=64x48:r=10','-t','1','-c:v','mpeg4',str(source/'fixture.avi')],check=True,timeout=30)
        hashes = {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in source.iterdir()}
        def identity(path):
            drive = drive_for_path(path)
            info = disks.snapshot([drive])[drive]
            return {'root':str(path),'drive':drive,**{key:info[key] for key in ('serial','volume_serial','volume_id')}}
        data=tmp_path/'data';data.mkdir();(data/'thumbnails').mkdir()
        cfg={**original, 'source':{}, 'destination':identity(dest), 'io_mibps':0,
             'avi_conversion':{'enabled':True,'remove_destination_avi':False},
             'database':{**original['database'],'options':f'-c search_path={schema}'}}
        # Route all module-level config/data aliases to this disposable catalog.
        for name,module in list(sys.modules.items()):
            if name.startswith('app.') and module:
                if hasattr(module,'load_config'):monkeypatch.setattr(module,'load_config',lambda:cfg)
                if hasattr(module,'DATA'):monkeypatch.setattr(module,'DATA',data)
        monkeypatch.setattr(telemetry,'LOGS',tmp_path/'logs')
        monkeypatch.setattr(macos_lifecycle,'STOP',tmp_path/'STOP')
        try:
            database.migrate()
            # Exercise native identity checks, actual copy+SHA256 and thumbnails.
            importer.run(str(source))
            with database.connection() as db:
                job=db.execute('SELECT * FROM jobs').fetchone()
                assert job['state']=='completed-review-required'
                assert job['source_storage']['drive'] == drive_for_path(source)
                rows=db.execute('SELECT * FROM media ORDER BY id').fetchall()
                assert len(rows)==2
                image=next(r for r in rows if r['kind']=='image')
                video=next(r for r in rows if r['kind']=='video')
            for row in rows:
                assert hashlib.sha256((dest/row['relative_path']).read_bytes()).hexdigest()==row['sha256']
            assert find_thumbnail(data/'thumbnails',image['sha256']).is_file()
            # Isolated HTTP routes exercise actual role checks and saved edits.
            from app.auth import password_hash
            cfg['browser_user']='native-test';cfg['browser_password_hash']=password_hash('native-test-password')
            client=TestClient(api.app)
            assert client.post('/api/login',json={'username':'native-test','password':'native-test-password'}).status_code==200
            for variant in ('thumbnail','original','download'):
                assert client.get(f"/api/media/{image['id']}/{variant}").status_code==200
            assert client.post(f"/api/media/{image['id']}/rotate").json()['rotation']==90
            assert client.post(f"/api/media/{image['id']}/crop",json={'x':0,'y':0,'width':.5,'height':1}).status_code==200
            for action in ('trash','restore'):
                assert client.post(f"/api/media/{image['id']}/trash/{action}").status_code==200
            videos.run(job_id=job['id'])
            with database.connection() as db:
                result=db.execute('SELECT state FROM video_metadata WHERE media_id=%s',(video['id'],)).fetchone()
                assert result['state'] in ('done','warning')
            conversions.run(job_id=job['id'])
            with database.connection() as db:
                converted=db.execute('SELECT * FROM media WHERE id=%s',(video['id'],)).fetchone()
                assert converted['relative_path'].endswith('.mp4')
                assert converted['conversion_state']=='done'
            response=client.get(f"/api/media/{video['id']}/original",headers={'Range':'bytes=0-99'})
            assert response.status_code==206 and len(response.content)==100
            # Test permanent deletion only on the generated fixture copy.
            assert client.post(f"/api/media/{image['id']}/trash/trash").status_code==200
            deletion=client.post(f"/api/media/{image['id']}/trash/delete")
            assert deletion.status_code==200, deletion.text
            assert not (dest/image['relative_path']).exists()
            assert {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in source.iterdir()}==hashes
            client.close()
        finally:
            with database.connection() as db:
                db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
            database.close_pools()
