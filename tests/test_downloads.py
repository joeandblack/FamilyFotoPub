from pathlib import Path
import io
import json
import os
import tracemalloc
import zipfile
import pytest
from app import database
from app.library import downloads
from test_albums import catalog
from test_uploads import env
from test_unexamined_photos import disposable_migrations


@pytest.mark.parametrize('extension',['MTS','avi','FLV'])
def test_converted_download_names(extension):
    from app.library.names import download_name
    media={'id':7,'name':f'family.trip.{extension}','kind':'video',
           'original_relative_path':f'original.{extension}','relative_path':'converted.mp4'}
    assert download_name(media)=='family.trip.mp4'
    assert downloads.filename(media)=='family.trip [7].mp4'
    assert media['name']==f'family.trip.{extension}'
    assert download_name({**media,'original_relative_path':None})==media['name']
    assert download_name({**media,'relative_path':f'original.{extension}'})==media['name']
    assert download_name({**media,'kind':'image'})==media['name']

@pytest.fixture
def prepared(catalog,monkeypatch):
    client,ids,job,config=catalog
    monkeypatch.setattr(downloads,'load_config',lambda:config)
    config['destination'].update(drive='fixture',volume_serial='fixture')
    monkeypatch.setattr(downloads,'volume_serial',lambda _: 'fixture')
    return client,ids,config

def prepare(c,ids):
    response=c.get('/api/downloads/prepare',params={'ids':','.join(map(str,ids))})
    assert response.status_code==200,response.text
    return response.json()['url']


def test_single_and_zip_use_converted_extension_preserving_catalog(prepared,monkeypatch):
    from urllib.parse import unquote
    c,ids,config=prepared;mid=ids[1]
    monkeypatch.setattr('app.storage.disks.volume_serial',lambda _: 'fixture')
    raw=b'fixture converted MP4 bytes'
    (Path(config['destination']['root'])/'converted.mp4').write_bytes(raw)
    with database.connection() as db:
        db.execute("UPDATE media SET name=%s,original_relative_path=relative_path,relative_path='converted.mp4',conversion_state='done',size=%s WHERE id=%s",('holiday.MTS',len(raw),mid))
        before=db.execute('SELECT name,sha256,original_relative_path FROM media WHERE id=%s',(mid,)).fetchone()
    for extra,expected in [('', 'holiday.mp4'),('?disambiguate=true',f'holiday [{mid}].mp4')]:
        response=c.get(f'/api/media/{mid}/download'+extra)
        assert response.status_code==200
        assert expected in unquote(response.headers['content-disposition'])
        assert response.headers['content-type']=='video/mp4'
        assert response.content==raw
    response=c.get(prepare(c,ids[:2]))
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.read(f'holiday [{mid}].mp4')==raw
        assert archive.read(f'same [{ids[0]}].jpg')==b'album fixture 0'
    with database.connection() as db:
        assert db.execute('SELECT name,sha256,original_relative_path FROM media WHERE id=%s',(mid,)).fetchone()==before
    assert (Path(config['destination']['root'])/before['original_relative_path']).read_bytes()==b'album fixture 1'

def test_archive_contains_every_original_repeated_names_mixed_and_trash(prepared):
    c,ids,config=prepared
    for mid in ids:assert c.post(f'/api/media/{mid}/trash/trash').status_code==200
    url=prepare(c,ids+[ids[0]])
    for _ in range(2):
        response=c.get(url);assert response.status_code==200
        assert len(response.content)==int(response.headers['content-length'])
        assert 'attachment;' in response.headers['content-disposition']
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            assert archive.testzip() is None
            assert archive.namelist()==[f'same [{mid}].jpg' for mid in ids]
            for index,mid in enumerate(ids):assert archive.read(f'same [{mid}].jpg')==f'album fixture {index}'.encode()
    with database.connection() as db:assert db.execute('SELECT count(*) n FROM media_trash').fetchone()['n']==3

def test_snapshot_names_permissions_expiry_and_missing(prepared,monkeypatch):
    c,ids,config=prepared
    with database.connection() as db:db.execute('UPDATE media SET name=%s WHERE id=%s',('..\\odd/<name>?.mp4',ids[1]))
    url=prepare(c,ids[:2]);response=c.get(url)
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert len(archive.namelist())==2
        assert all('/' not in n and '\\' not in n for n in archive.namelist())
    from app.auth import password_hash
    config['browser_accounts']['guest']={'role':'guest','password_hash':password_hash('fixture')}
    c.post('/api/login',json={'username':'guest','password':'fixture'})
    assert c.get(url).status_code==403
    own=prepare(c,ids[:2]);assert c.get(own).status_code==200
    assert c.get(own+'x').status_code==403
    monkeypatch.setattr(downloads.time,'time',lambda:9999999999)
    # Token checking directly avoids expiring the test login cookie at the same time.
    with pytest.raises(Exception) as error:downloads.token_ids(own.split('token=')[1],{'username':'guest','role':'guest'})
    assert error.value.status_code==403

def test_preflight_rejects_partial_unavailable_and_busy(prepared,tmp_path):
    c,ids,config=prepared
    for value in ['',str(ids[0]),'1,0','1,true','1,'*1001]:assert c.get('/api/downloads/prepare',params={'ids':value}).status_code==422
    assert c.get('/api/downloads/prepare',params={'ids':f'{ids[0]},999999'}).status_code==404
    url=prepare(c,ids)
    (tmp_path/'destination'/'1.jpg').unlink()
    assert c.get('/api/downloads/prepare',params={'ids':','.join(map(str,ids))}).status_code==503
    assert c.get(url).status_code==503
    (tmp_path/'destination'/'1.jpg').write_bytes(b'restored fixture')
    with database.connection() as db:
        sha=db.execute('SELECT sha256 FROM media WHERE id=%s',(ids[0],)).fetchone()['sha256']
        db.execute('SELECT pg_advisory_lock(%s,%s)',(downloads.MEDIA_LOCK_NAMESPACE,downloads.content_key(sha)));db.commit()
        assert c.get('/api/downloads/prepare',params={'ids':','.join(map(str,ids))}).status_code==409
    with database.connection() as db:db.execute('INSERT INTO media_trash(media_id,deleting_at) VALUES (%s,now())',(ids[1],))
    assert c.get('/api/downloads/prepare',params={'ids':','.join(map(str,ids))}).status_code==409
    c.cookies.clear();assert c.get(url).status_code==401

def test_large_stream_is_bounded_and_cancellation_releases_resources(prepared,tmp_path):
    c,ids,config=prepared
    with (tmp_path/'destination'/'0.jpg').open('wb') as file:
        for _ in range(512):file.write(b'A'*65536)
    archive=downloads.Archive(ids)
    tracemalloc.start();total=0
    for chunk in archive.chunks():
        total+=len(chunk);assert len(chunk)<=65536
    _,peak=tracemalloc.get_traced_memory();tracemalloc.stop()
    assert total==archive.length and peak<2*1024*1024
    archive.close();assert all(file.closed for _,file,_ in archive.files)
    # Disconnect an ASGI stream while the response is being sent.
    async def cancel():
        import anyio
        archive=downloads.Archive(ids);response=downloads.ArchiveResponse(archive)
        sent=anyio.Event()
        async def receive():await sent.wait();return {'type':'http.disconnect'}
        async def send(message):
            if message['type']=='http.response.body':sent.set();await anyio.sleep(.01)
        await response({'type':'http','asgi':{'spec_version':'2.0'}},receive,send)
        assert archive.closed and all(file.closed for _,file,_ in archive.files)
    import anyio
    anyio.run(cancel)
    # A subsequent request can reacquire all locks and read the same selection.
    archive=downloads.Archive(ids);archive.close()

def test_midstream_failure_cannot_finalize_successful_zip(prepared):
    _,ids,_=prepared;archive=downloads.Archive(ids)
    class Failing:
        def read(self,n):raise OSError('Fixture disk disconnected')
    name,file,size=archive.files[0];archive.files[0]=(name,Failing(),size)
    iterator=archive.chunks();assert next(iterator).startswith(b'PK')
    with pytest.raises(OSError):next(iterator)
    archive.close();assert file.closed


def test_browser_archive_downloads(catalog,monkeypatch,tmp_path):
    import os
    import json
    import subprocess
    import threading
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    from urllib.parse import urlsplit
    if os.environ.get('FAMILYFOTO_BROWSER')!='1':pytest.skip('Requires installed Windows Chrome')
    c,ids,job,config=catalog
    # This fixture must not proxy its synthetic login into the live People service.
    monkeypatch.setattr('face_detect.api.people_preview_gateway.settings',lambda:None)
    monkeypatch.setattr(downloads,'load_config',lambda:config)
    config['destination'].update(drive='fixture',volume_serial='fixture')
    monkeypatch.setattr('app.storage.disks.volume_serial',lambda _: 'fixture')
    monkeypatch.setattr(downloads,'volume_serial',lambda _: 'fixture')
    root=Path(os.environ.get('FAMILYFOTO_WEB_ROOT','web')).resolve()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def handle_request(self):
            path=urlsplit(self.path).path
            if path=='/fixture/unavailable':
                (Path(config['destination']['root'])/'2.jpg').unlink()
                body=b'{}';status=200;mime='application/json'
            elif path=='/' or path.startswith('/web/'):
                file=root/('index.html' if path=='/' else path[5:])
                assert file.resolve().is_relative_to(root)
                body=file.read_bytes();status=200;mime='text/html' if path=='/' else 'text/css' if file.suffix=='.css' else 'text/javascript'
            else:
                data=self.rfile.read(int(self.headers.get('Content-Length',0)))
                response=c.request(self.command,self.path,content=data,headers={'Content-Type':'application/json'})
                body=response.content;status=response.status_code;mime=response.headers.get('content-type','application/octet-stream')
            self.send_response(status)
            if path.startswith('/api/downloads/archive') or path.endswith('/download'):
                self.send_header('Content-Disposition',response.headers.get('content-disposition','inline'))
            self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(body)));self.end_headers()
            try:self.wfile.write(body)
            except (BrokenPipeError,ConnectionAbortedError,ConnectionResetError):pass
        do_GET=do_POST=do_PATCH=do_DELETE=handle_request
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    fixture=tmp_path/'browser-fixture.json';fixture.write_text(json.dumps({'url':f'http://127.0.0.1:{server.server_port}/','ids':ids,'job':job}))
    try:
        result=subprocess.run(['node','tests/downloads-browser.mjs',str(fixture)],capture_output=True,text=True,timeout=120)
        (tmp_path/'browser-result.txt').write_text(result.stdout+result.stderr)
        assert result.returncode==0,result.stdout+result.stderr
        report=json.loads(result.stdout.strip().splitlines()[-1])
        for item in report['archives']:
            with zipfile.ZipFile(item['path']) as archive:
                assert archive.testzip() is None
                assert len(archive.namelist())==len(item['ids'])
                for mid in item['ids']:assert archive.read(f'same [{mid}].jpg')==f'album fixture {ids.index(mid)}'.encode()
        for item in report['singles']:assert Path(item['path']).read_bytes()==f"album fixture {ids.index(item['id'])}".encode()
        print(result.stdout)
    finally:server.shutdown();server.server_close();worker.join()
