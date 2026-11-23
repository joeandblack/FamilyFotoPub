import asyncio
import hashlib
import io
import json
import os
import uuid
from unittest.mock import Mock
import pytest
from PIL import Image
from psycopg import sql
from fastapi.testclient import TestClient
from app import uploads, database, jobs as pipeline, api
from app.auth import api as auth_api
from app.imports import importer
from routers import CONFIG_MODULES, DATA_MODULES


def test_empty_dispatcher_does_not_reserve_media_worker(env,monkeypatch):
    monkeypatch.setattr(pipeline,'idle',lambda db:pytest.fail('Empty poll must not reserve media access'))
    uploads.dispatch_once()


def test_fresh_library_opens_without_media_storage(env,tmp_path,monkeypatch):
    from app.storage import config as storage_config
    client,config,_=env
    config['source']={};config['destination']={}
    path=tmp_path/'local.json';path.write_text(json.dumps(config))
    monkeypatch.setattr(storage_config,'CONFIG_PATH',path)
    for route in ('/api/status','/api/storage/settings','/api/media','/api/albums'):
        response=client.get(route)
        assert response.status_code==200,(route,response.text)
    assert client.get('/api/storage/settings').json()['destination']==''
    response=client.post('/api/import/start',json={'source':str(tmp_path)})
    assert response.status_code==409,response.text
    response=client.post('/api/uploads',json={})
    assert response.status_code in (400,409),response.text


def test_first_upload_without_server_source(env):
    client,config,_=env
    config['source']={}
    content=image_bytes()
    session,_=start(client,['Holiday/photo.jpg'],content)
    transfer(client,session,content)
    response=client.post(f"/api/uploads/{session['id']}/finalize")
    assert response.status_code==200,response.text
    # The upload dispatch uses its staging files, not a configured server source.
    uploads.dispatch_once()
    with database.connection() as db:
        assert db.execute('SELECT count(*) n FROM media').fetchone()['n']==1


@pytest.mark.parametrize('path',['/a/b.jpg','../a.jpg','a/../b.jpg','a//b.jpg','a/./b.jpg','C:/a.jpg','C:foo/a.jpg','//server/a.jpg','a\\b.jpg','a/\x00.jpg','a/\n.jpg','file.jpg'])
def test_invalid_relative_paths(path):
    with pytest.raises(ValueError):uploads.relative_path(path)


def test_mac_unicode_and_reserved_names_are_data_not_disk_paths():
    assert uploads.relative_path('Holiday/日本/e\u0301té/CON.jpg')=='Holiday/日本/e\u0301té/CON.jpg'
    value={'files':[{'path':'Holiday/日本/CON.jpg','size':3},{'path':'Holiday/.DS_Store','size':10},{'path':'Holiday/_note.xyz','size':1}]}
    _,files,total,_,_=uploads.manifest(value,uploads.DEFAULTS)
    assert total==3 and [f['disposition'] for f in files]==['accepted','ignored','unsupported']


@pytest.fixture
def env(tmp_path,monkeypatch):
    if os.environ.get('FAMILYFOTO_INTEGRATION')!='1':pytest.skip('Requires local PostgreSQL')
    from app.config import load_config
    from app import telemetry
    from app.storage import config as storage_config
    from app.storage import disks
    config=load_config();schema='uploadtest_'+uuid.uuid4().hex
    original=database.connection
    with original() as db:db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    source=tmp_path/'source';source.mkdir();destination=tmp_path/'destination';destination.mkdir();data=tmp_path/'data';data.mkdir();(data/'thumbnails').mkdir()
    from app.auth import password_hash
    config.pop('browser_password',None)
    config={**config,'auth_secret':'fixture-secret-'*4,'host':'0.0.0.0','browser_user':'admin','browser_password_hash':password_hash('fixture'),'browser_accounts':{'reader':{'role':'regular','password_hash':password_hash('fixture')}},'source':{'root':str(source)},'destination':{'root':str(destination)},'io_mibps':0,
            'uploads':{'chunk_bytes':64,'reserve_bytes':1},'database':{**config['database'],'options':f'-c search_path={schema}'}}
    class Monitor:
        def __init__(self,*a,**kw):self.thread=True
        def verify(self):pass
        def prepare(self):pass
        def check(self):pass
        def close(self):pass
    for module in (database,uploads,importer,storage_config,*CONFIG_MODULES):
        monkeypatch.setattr(module,'load_config',lambda:config)
    monkeypatch.setattr('app.config.load_config',lambda:config)
    for module in (uploads,importer,pipeline,*DATA_MODULES):monkeypatch.setattr(module,'DATA',data)
    monkeypatch.setattr(telemetry,'LOGS',tmp_path/'logs')
    monkeypatch.setattr(importer,'Monitor',Monitor);monkeypatch.setattr(disks,'Monitor',Monitor)
    for module,names in [(uploads,['QUOTA_LOCK','DISPATCH_LOCK']),(pipeline,['LOCK','HANDOFF_LOCK','WORKER_LOCK']),(importer,['IMPORT_LOCK','SINGLE_IMPORT_LOCK'])]:
        for name in names:monkeypatch.setattr(module,name,uuid.uuid4().int%(2**63-1))
    try:
        database.migrate()
    except Exception:
        # Setup may be refused while a real worker holds migration reservations.
        # Clean only the unique fixture schema created above, even before yield.
        with database.connection() as db:db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
        raise
    client=TestClient(api.app)
    client.post('/api/login',json={'username':'admin','password':'fixture'})
    client.headers['X-FamilyFoto-Upload']='1'
    try:yield client,config,data
    finally:
        client.close()
        with database.connection() as db:db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def image_bytes():
    stream=io.BytesIO();Image.new('RGB',(10,10),(20,70,130)).save(stream,format='JPEG');return stream.getvalue()


def start(client,paths,content,key=None):
    payload={'request_key':key or str(uuid.uuid4()),'files':[{'path':p,'size':len(content),'modified':123} for p in paths]}
    response=client.post('/api/uploads',json=payload)
    assert response.status_code==200,response.text
    return response.json(),payload


def transfer(client,session,content,start=0):
    for f in session['files']:
        if f['disposition']!='accepted':continue
        for offset in range(start,len(content),64):
            chunk=content[offset:offset+64]
            r=client.put(f"/api/uploads/{session['id']}/files/{f['file_id']}?offset={offset}",content=chunk,headers={'X-Content-SHA256':hashlib.sha256(chunk).hexdigest()})
            assert r.status_code==200,r.text
        r=client.post(f"/api/uploads/{session['id']}/files/{f['file_id']}/verify",json={'sha256':hashlib.sha256(content).hexdigest()})
        assert r.status_code==200,r.text


def test_upload_import_duplicates_search_and_cleanup(env):
    client,config,data=env;content=image_bytes()
    session,payload=start(client,['Holiday/日本/e\u0301té.jpg','Holiday/CON.jpg','Holiday/.DS_Store','Holiday/notes.xyz'],content)
    assert client.post('/api/uploads',json=payload).json()['id']==session['id']
    assert client.post(f"/api/uploads/{session['id']}/finalize").status_code==409
    transfer(client,session,content)
    finalized=client.post(f"/api/uploads/{session['id']}/finalize").json()
    assert client.post(f"/api/uploads/{session['id']}/finalize").json()['job_id']==finalized['job_id']
    # Simulates a fresh dispatcher observing a persisted queued job.
    uploads.dispatch_once()
    status=client.get('/api/uploads/'+session['id']).json();assert status['state']=='done',status
    with database.connection() as db:
        media=db.execute('SELECT * FROM media').fetchall();assert len(media)==1
        assert media[0]['first_import_job_id']==finalized['job_id'] and '\\' not in media[0]['relative_path']
        assert sorted(r['state'] for r in db.execute('SELECT state FROM import_items').fetchall())==['duplicate','imported','unsupported']
    assert client.get('/api/media',params={'search':'日本/'}).json()['total']==1
    assert client.get('/api/import/history').json()['imports'][0]['source_kind']=='upload'
    uploads.dispatch_once();assert not uploads.folder(session['id']).exists()
    # A second upload counts duplicates without claiming library membership.
    second,_=start(client,['Another/renamed.jpg'],content);transfer(client,second,content)
    client.post('/api/uploads/'+second['id']+'/finalize');uploads.dispatch_once()
    with database.connection() as db:
        assert db.execute('SELECT count(*) n FROM media').fetchone()['n']==1
        assert db.execute('SELECT first_import_job_id FROM media').fetchone()['first_import_job_id']==finalized['job_id']
        assert db.execute("SELECT count(*) n FROM import_items WHERE state='duplicate' AND media_id IS NOT NULL").fetchone()['n']==0
    assert client.get('/api/media',params={'search':'Another/'}).json()['total']==0
    uploads.dispatch_once()
    assert client.get('/api/uploads/'+second['id']).json()['staging_cleaned']


def test_chunk_reconnect_content_integrity_cancel_and_permissions(env,monkeypatch):
    client,config,data=env;content=image_bytes();session,_=start(client,['Mac/nested/clip.jpg'],content)
    sid=session['id'];route=f'/api/uploads/{sid}/files/0'
    chunk=content[:64];headers={'X-Content-SHA256':hashlib.sha256(chunk).hexdigest()}
    assert client.put(route+'?offset=0',content=chunk,headers=headers).json()['received']==64
    assert client.put(route+'?offset=0',content=chunk,headers=headers).status_code==409
    assert client.get('/api/uploads/'+sid).json()['files'][0]['received']==64
    assert client.put(route+'?offset=64',content=b'bad',headers=headers).status_code==409
    assert uploads.file_path(sid,0,'Mac/nested/clip.jpg').stat().st_size==64
    transfer(client,session,content,start=64)
    assert client.post(route+'/verify',json={'sha256':'0'*64}).status_code==409
    assert client.post(route+'/reset').status_code==200
    assert client.get('/api/uploads/'+sid).json()['files'][0]['received']==0
    # An administrator can view another person's session but never write into it.
    with database.connection() as db:db.execute("UPDATE upload_sessions SET owner_name='another-admin' WHERE id=%s",(sid,))
    assert client.get('/api/uploads/'+sid).json()['owner']=='another-admin'
    assert client.put(route+'?offset=0',content=chunk,headers=headers).status_code==409
    with database.connection() as db:db.execute("UPDATE upload_sessions SET owner_name='admin' WHERE id=%s",(sid,))
    client.headers.pop('X-FamilyFoto-Upload')
    assert client.post('/api/uploads/'+sid+'/cancel').status_code==403
    client.headers['X-FamilyFoto-Upload']='1'
    assert client.post('/api/uploads/'+sid+'/cancel',headers={'Origin':'http://evil.invalid'}).status_code==403
    client.post('/api/login',json={'username':'reader','password':'fixture'})
    # A regular account may upload, but sees and writes only its own sessions.
    assert client.get('/api/uploads').json()['sessions']==[]
    assert client.put(route+'?offset=0',content=chunk,headers=headers).status_code==409
    client.post('/api/login',json={'username':'admin','password':'fixture'})
    assert client.post('/api/uploads/'+sid+'/cancel').json()['state']=='cancelled'
    assert client.post('/api/uploads/'+sid+'/finalize').status_code==409
    uploads.dispatch_once()
    with database.connection() as db:assert db.execute('SELECT cleaned_at FROM upload_sessions WHERE id=%s',(sid,)).fetchone()['cleaned_at'] is None


def test_limits_disk_full_and_restart_pause(env,monkeypatch):
    client,config,data=env;content=image_bytes()
    config['uploads']['max_files']=1
    assert client.post('/api/uploads',json={'request_key':str(uuid.uuid4()),'files':[{'path':'a/1.jpg','size':1},{'path':'a/2.jpg','size':1}]}).status_code==409
    config['uploads']['max_files']=5000;config['uploads']['max_file_bytes']=1
    assert client.post('/api/uploads',json={'request_key':str(uuid.uuid4()),'files':[{'path':'a/1.jpg','size':2}]}).status_code==409
    config['uploads']['max_file_bytes']=10000;config['uploads']['staging_bytes']=10
    assert client.post('/api/uploads',json={'request_key':str(uuid.uuid4()),'files':[{'path':'a/1.jpg','size':20}]}).status_code==409
    config['uploads']['staging_bytes']=10000
    session,_=start(client,['Mac/a.jpg'],content)
    with monkeypatch.context() as m:
        m.setattr(uploads.shutil,'disk_usage',lambda p:Mock(free=0))
        r=client.put(f"/api/uploads/{session['id']}/files/0?offset=0",content=b'x',headers={'X-Content-SHA256':hashlib.sha256(b'x').hexdigest()})
        assert r.status_code==409 and 'full' in r.text
    transfer(client,session,content);client.post('/api/uploads/'+session['id']+'/finalize').json()
    with monkeypatch.context() as m:
        m.setattr(pipeline,'idle',Mock(side_effect=RuntimeError('active')))
        uploads.dispatch_once()
    assert client.get('/api/uploads/'+session['id']).json()['state']=='queued'
    with database.connection() as db:db.execute("UPDATE upload_sessions SET state='importing' WHERE id=%s",(session['id'],))
    uploads.dispatch_once()
    # A persisted handoff is recovered by the server without browser requests.
    assert client.get('/api/uploads/'+session['id']).json()['state']=='done'
    assert client.get('/api/uploads/'+session['id']).json()['state']=='done'


def test_cancel_queued_expiry_and_stream_failure(env,monkeypatch):
    client,config,data=env;content=image_bytes();session,_=start(client,['Mac/a.jpg'],content)
    sid=session['id']
    async def broken():
        yield b'abc'
        raise OSError('connection lost')
    with pytest.raises(OSError,match='lost'):
        asyncio.run(uploads.chunk('admin',sid,0,0,hashlib.sha256(b'abc').hexdigest(),broken()))
    assert client.get('/api/uploads/'+sid).json()['files'][0]['received']==0
    assert uploads.file_path(sid,0,'Mac/a.jpg').stat().st_size==0
    transfer(client,session,content);client.post('/api/uploads/'+sid+'/finalize')
    client.post('/api/uploads/'+sid+'/cancel');uploads.dispatch_once()
    with database.connection() as db:
        assert db.execute('SELECT count(*) n FROM media').fetchone()['n']==0
        assert db.execute('SELECT state FROM jobs').fetchone()['state']=='cancelled'
    session,_=start(client,['Mac/b.jpg'],content)
    with database.connection() as db:db.execute("UPDATE upload_sessions SET updated_at=now()-interval '49 hours' WHERE id=%s",(session['id'],))
    uploads.dispatch_once()
    assert client.get('/api/uploads/'+session['id']).json()['state']=='cancelled'


def test_cancel_active_preserves_completed_media_and_cleanup_is_bounded(env,monkeypatch):
    client,config,data=env;content=image_bytes();session,_=start(client,['Mac/a.jpg','Mac/b.jpg'],content)
    transfer(client,session,content);client.post('/api/uploads/'+session['id']+'/finalize')
    original=importer.copy_verified
    def cancel_after_copy(*args,**kwargs):
        original(*args,**kwargs)
        uploads.cancel('admin',session['id'])
    monkeypatch.setattr(importer,'copy_verified',cancel_after_copy)
    uploads.dispatch_once()
    assert client.get('/api/uploads/'+session['id']).json()['state']=='cancelled'
    with database.connection() as db:
        row=db.execute('SELECT * FROM media').fetchone()
        assert row and db.execute("SELECT count(*) n FROM import_items WHERE state='pending'").fetchone()['n']==1
    from pathlib import Path
    published=Path(config['destination']['root'])/row['relative_path']
    assert published.read_bytes()==content
    unexpected=uploads.folder(session['id'])/'unexpected';unexpected.write_bytes(b'keep')
    uploads.dispatch_once()
    assert uploads.file_path(session['id'],0,'Mac/a.jpg').exists()
    assert unexpected.read_bytes()==b'keep' and published.read_bytes()==content


def test_total_reservation_and_truncated_body_recovery(env,monkeypatch):
    client,config,data=env;content=image_bytes()
    config['uploads']['max_total_bytes']=1
    assert client.post('/api/uploads',json={'request_key':str(uuid.uuid4()),'files':[{'path':'a/x.jpg','size':2}]}).status_code==409
    config['uploads']['max_total_bytes']=10000
    with monkeypatch.context() as m:
        m.setattr(uploads.shutil,'disk_usage',lambda p:Mock(free=0))
        assert client.post('/api/uploads',json={'request_key':str(uuid.uuid4()),'files':[{'path':'a/x.jpg','size':2}]}).status_code==409
    session,payload=start(client,['Mac/a.jpg'],content)
    assert client.post('/api/uploads',json={**payload,'request_key':str(uuid.uuid4())}).status_code==409
    payload['files'][0]['size']+=1
    assert client.post('/api/uploads',json=payload).status_code==409
    path=uploads.file_path(session['id'],0,'Mac/a.jpg');path.parent.mkdir();path.write_bytes(b'unacknowledged crash bytes')
    transfer(client,session,content)
    assert path.read_bytes()==content


def test_optional_video_failure_does_not_reopen_finished_upload(env,monkeypatch):
    client,config,data=env;content=image_bytes();session,_=start(client,['Mac/a.jpg'],content)
    transfer(client,session,content);client.post('/api/uploads/'+session['id']+'/finalize')
    def fail_after_import(job_id,stage,inline=False):
        importer.run(upload_job_id=job_id)
        raise RuntimeError('Synthetic optional metadata failure')
    monkeypatch.setattr(pipeline,'launch',fail_after_import)
    uploads.dispatch_once()
    status=client.get('/api/uploads/'+session['id']).json()
    assert status['state']=='done' and 'metadata failure' in status['error']
    assert client.post('/api/uploads/'+session['id']+'/retry').status_code==409


def test_browser_disconnect_after_handoff_and_interrupted_import_recovery(env):
    client,config,data=env;content=image_bytes();session,_=start(client,['Mac/a.jpg'],content)
    transfer(client,session,content)
    handoff=client.post('/api/uploads/'+session['id']+'/finalize').json()
    assert handoff['handoff_ready'] and handoff['job_id']
    client.close()  # No browser polling or API request participates in completion.
    with database.connection() as db:
        db.execute("UPDATE upload_sessions SET state='importing' WHERE id=%s",(session['id'],))
        db.execute("UPDATE jobs SET state='running' WHERE id=%s",(handoff['job_id'],))
    uploads.dispatch_once()
    with database.connection() as db:
        assert db.execute('SELECT state FROM upload_sessions WHERE id=%s',(session['id'],)).fetchone()['state']=='done'
        assert db.execute('SELECT count(*) n FROM media').fetchone()['n']==1
    uploads.dispatch_once()
    assert not uploads.folder(session['id']).exists()


def test_cleanup_retains_staging_on_destination_corruption_pending_or_failed_import(env,monkeypatch):
    from pathlib import Path
    client,config,data=env;content=image_bytes();session,_=start(client,['Mac/a.jpg'],content)
    transfer(client,session,content);client.post('/api/uploads/'+session['id']+'/finalize').json()
    staged=uploads.file_path(session['id'],0,'Mac/a.jpg')
    with database.connection() as db:
        row=db.execute('SELECT * FROM upload_sessions WHERE id=%s',(session['id'],)).fetchone()
        assert uploads.cleanup(db,row) is False
    assert staged.read_bytes()==content
    with monkeypatch.context() as m:
        m.setattr(importer,'copy_verified',Mock(side_effect=OSError('Synthetic disk fault')))
        uploads.dispatch_once()
    assert staged.read_bytes()==content
    with database.connection() as db:
        row=db.execute('SELECT * FROM upload_sessions WHERE id=%s',(session['id'],)).fetchone()
        assert row['state']=='failed' and uploads.cleanup(db,row) is False
    client.post('/api/uploads/'+session['id']+'/retry');uploads.dispatch_once()
    with database.connection() as db:media=db.execute('SELECT * FROM media').fetchone()
    target=Path(config['destination']['root'])/media['relative_path'];target.write_bytes(b'corrupt fixture')
    uploads.dispatch_once()
    assert staged.read_bytes()==content
    with database.connection() as db:
        row=db.execute('SELECT * FROM upload_sessions WHERE id=%s',(session['id'],)).fetchone()
        assert row['cleaned_at'] is None and 'Staging retained' in row['error']
    target.write_bytes(content);uploads.dispatch_once();assert not staged.exists()


def test_cleanup_waits_for_requested_postprocessing_and_honors_explicit_pause(env,monkeypatch):
    client,config,data=env;content=image_bytes();session,_=start(client,['Mac/a.jpg'],content)
    transfer(client,session,content);handoff=client.post('/api/uploads/'+session['id']+'/finalize').json()
    with database.connection() as db:
        db.execute("UPDATE upload_sessions SET state='importing' WHERE id=%s",(session['id'],))
        db.execute("UPDATE jobs SET state='paused' WHERE id=%s",(handoff['job_id'],))
    uploads.dispatch_once()
    assert client.get('/api/uploads/'+session['id']).json()['state']=='paused'
    client.post('/api/uploads/'+session['id']+'/retry');uploads.dispatch_once()
    with database.connection() as db:db.execute('UPDATE jobs SET auto_metadata=true WHERE id=%s',(handoff['job_id'],))
    with monkeypatch.context() as m:
        m.setattr('app.jobs.job_scope.pending_work',lambda *a:True)
        uploads.dispatch_once()
    assert uploads.file_path(session['id'],0,'Mac/a.jpg').exists()


@pytest.mark.skipif(os.environ.get('FAMILYFOTO_BROWSER')!='1',reason='Explicit installed Windows Chrome verification')
def test_live_http_browser_upload(env,tmp_path,monkeypatch):
    """Real HTTP, JS hash worker, DB and importer; only synthetic media/schema."""
    import base64
    import html
    import re
    import socket
    import subprocess
    import threading
    import time
    from pathlib import Path
    import uvicorn
    from fastapi.responses import HTMLResponse
    client,config,data=env
    config['host']='127.0.0.1';config['browser_accounts']={}
    # Local fixture auth only; production still requires its normal login.
    monkeypatch.setattr(auth_api,'session_user',lambda *_:{'username':'admin','role':'admin'})
    content=base64.b64encode(image_bytes()).decode()
    script=r'''
    addEventListener('load',async()=>{
      const check=(x,m)=>{if(!x)throw Error(m);};
      const wait=async fn=>{for(let i=0;i<1000;i++){if(await fn())return;await new Promise(r=>setTimeout(r,30));}throw Error('Browser wait timed out');};
      try{
        await wait(()=>document.body.dataset.role==='admin');
        document.querySelector('[data-view="uploads"]').click();
        check(document.getElementById('importMode')===null,'Server imports no longer offer an upload mode');
        check(document.getElementById('uploadFolder').webkitdirectory,'Directory chooser configured');
        const bytes=Uint8Array.from(atob('CONTENT'),c=>c.charCodeAt(0));
        const files=['Holiday/日本/été.jpg','Holiday/second.jpg'].map(path=>{const f=new File([bytes],path.split('/').at(-1),{type:'image/jpeg',lastModified:123});Object.defineProperty(f,'webkitRelativePath',{value:path});return f;});
        const input=document.getElementById('uploadFolder');Object.defineProperty(input,'files',{value:files});input.dispatchEvent(new Event('change'));
        document.getElementById('startUpload').click();
        await wait(async()=>{const r=await fetch('/api/uploads');const d=await r.json();return d.sessions[0]?.state==='done';});
        const history=await (await fetch('/api/import/history')).json();check(history.imports[0].source_kind==='upload','Upload provenance kind');
        const matches=await (await fetch('/api/media?search='+encodeURIComponent('日本/'))).json();check(matches.total===1,'Relative Unicode search and dedup');
        // Refresh the grouped Uploads list after the worker completed.
        document.querySelector('[data-view="all"]').click();document.querySelector('[data-view="uploads"]').click();
        await wait(()=>[...document.querySelectorAll('.import-group summary')].some(e=>e.textContent.includes('Upload · Holiday')));
        await wait(()=>document.getElementById('uploadProgress').textContent.includes('import complete'));
        check(document.getElementById('uploadProgress').textContent.includes('100.0% transferred'),'Upload percentage visible');
        check(document.getElementById('uploadMeter').value===100,'Meter agrees with percentage');
        document.getElementById('search').value='stale no match';document.getElementById('year').value='unknown';
        [...document.querySelectorAll('.import-group button')].find(b=>b.textContent==='View in Library').click();
        await wait(()=>document.querySelectorAll('#grid .tile').length===1);
        check(document.getElementById('search').value===''&&document.getElementById('year').value==='all'&&document.body.dataset.view==='all','View in Library clears stale filters');
        check(document.getElementById('importFilter').value===String(history.imports[0].id),'Import selector follows history action');
        check(document.documentElement.scrollWidth<=innerWidth,'No page overflow');
        const report=document.createElement('pre');report.id='upload-browser-result';report.textContent=JSON.stringify({ok:true,browser:'Installed Windows Chrome over HTTP',checks:['Directory input configured','Actual File/Blob transfer','HTTP hash worker','Queued import','Global dedup','Unicode relative search','Upload label','No overflow']});report.hidden=true;document.body.append(report);
      }catch(e){const report=document.createElement('pre');report.id='upload-browser-result';report.textContent=JSON.stringify({ok:false,error:String(e),status:document.getElementById('uploadProgress')?.textContent});document.body.append(report);}
    });
    '''.replace('CONTENT',content)
    def page():
        source=(Path(__file__).resolve().parents[1]/'web/index.html').read_text(encoding='utf-8')
        return HTMLResponse(source.replace('</head>','<script>'+script+'</script></head>'))
    api.app.add_api_route('/upload-browser-fixture',page,methods=['GET'])
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(api.app,log_level='error',lifespan='off'))
    thread=threading.Thread(target=lambda:server.run(sockets=[sock]),daemon=True);thread.start()
    stop=threading.Event()
    def dispatcher():
        while not stop.wait(.1):uploads.dispatch_once()
    queue=threading.Thread(target=dispatcher,daemon=True);queue.start()
    try:
        result=subprocess.run([r'C:\Program Files\Google\Chrome\Application\chrome.exe','--headless','--no-first-run','--disable-background-networking','--no-default-browser-check',f'--user-data-dir={tmp_path/"chrome"}','--window-size=1280,1000','--virtual-time-budget=60000',f'--screenshot={tmp_path/"upload.png"}','--dump-dom',f'http://127.0.0.1:{port}/upload-browser-fixture'],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=90,creationflags=subprocess.CREATE_NO_WINDOW)
        (tmp_path/'browser.html').write_text(result.stdout,encoding='utf-8')
        (tmp_path/'browser.log').write_text(result.stderr,encoding='utf-8')
        match=re.search(r'<pre id="upload-browser-result"[^>]*>(.*?)</pre>',result.stdout,re.S)
        assert match,'Browser produced no report: '+result.stderr[-500:]
        report=json.loads(html.unescape(match.group(1)));assert report['ok'],report
        print(json.dumps(report))
    finally:
        stop.set();queue.join(timeout=10);server.should_exit=True;thread.join(timeout=10);sock.close()
        api.app.router.routes[:]=[r for r in api.app.router.routes if getattr(r,'path','')!='/upload-browser-fixture']


def test_library_import_provenance_filters_pagination_and_trash(env):
    client,_,_=env
    with database.connection() as db:
        jobs=[db.execute("INSERT INTO jobs(source_root,source_label) VALUES (%s,%s) RETURNING id",(root,label)).fetchone()['id'] for root,label in [('old','Old'),('new','Holiday'),('empty','Empty')]]
        a,b,empty=jobs
        ids=[]
        for name,kind,date,owner in [('shared.jpg','image','2020-01-01',a),('new.mp4','video','2021-01-01',b),('unrelated.jpg','image','2020-01-01',a),('trashed.jpg','image',None,b)]:
            mid=db.execute("INSERT INTO media(sha256,relative_path,name,kind,size,captured_at,first_import_job_id) VALUES (%s,%s,%s,%s,10,%s,%s) RETURNING id",(name,name,name,kind,date,owner)).fetchone()['id'];ids.append(mid)
            db.execute("INSERT INTO import_items(job_id,source_path,size,mtime_ns,state,media_id) VALUES (%s,%s,10,0,'imported',%s)",(owner,name,mid))
        for path in ['Holiday/shared.jpg','Holiday/shared-again.jpg']:
            db.execute("INSERT INTO import_items(job_id,source_path,size,mtime_ns,state,media_id) VALUES (%s,%s,10,0,'duplicate',%s)",(b,path,ids[0]))
        db.execute('INSERT INTO media_trash(media_id) VALUES (%s)',(ids[3],))
    result=client.get('/api/media',params={'import_job':b}).json()
    assert {m['id'] for m in result['items']}==set(ids[:2])
    assert result['total']==result['stats']['total']==2 and result['stats']['bytes']==20
    assert next(j for j in result['imports'] if j['id']==b)['label']=='Holiday'
    for extra,expected in [({'year':'2020'},[ids[0]]),({'kind':'video'},[ids[1]]),({'search':'Holiday/shared','year':'2020','kind':'image'},[ids[0]]),({'year':'unknown'},[]),({'kind':'trash'},[ids[3]])]:
        result=client.get('/api/media',params={'import_job':b,**extra}).json()
        assert [m['id'] for m in result['items']]==expected
    assert client.get('/api/media',params={'import_job':empty}).json()['total']==0
    for value in ['0','-1','1.5','bad','9007199254740992','1 OR 1=1']:
        assert client.get('/api/media',params={'import_job':value}).status_code==422
    assert client.get('/api/media',params={'import_job':9999999}).status_code==404
    with database.connection() as db:
        assert db.execute('SELECT first_import_job_id FROM media WHERE id=%s',(ids[0],)).fetchone()['first_import_job_id']==a
        db.execute("INSERT INTO media(sha256,relative_path,name,kind,size) SELECT 'page-'||g,'page-'||g,'page-'||g,'image',1 FROM generate_series(1,1001) g")
        db.execute("INSERT INTO import_items(job_id,source_path,size,mtime_ns,state,media_id) SELECT %s,name,size,0,'imported',id FROM media WHERE name LIKE 'page-%%'",(b,))
    first=client.get('/api/media',params={'import_job':b,'page':1}).json()
    last=client.get('/api/media',params={'import_job':b,'page':999}).json()
    assert first['total']==last['total']==1003 and last['page']==last['pages']==2
    assert len(first['items'])==1000 and len(last['items'])==3
    assert len({m['id'] for m in first['items']+last['items']})==1003
