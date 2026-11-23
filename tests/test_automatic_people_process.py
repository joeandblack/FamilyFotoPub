import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
import urllib.request
import urllib.error
import pytest
from PIL import Image
from test_uploads import env
from app import database
from app.telemetry import write_json


@pytest.mark.skipif(not Path(r'C:\Program Files\Google\Chrome\Application\chrome.exe').exists(),
                    reason='tests/*-browser.mjs scripts hardcode the Windows Chrome path')
def test_raw_scan_actual_process_browser_controls_and_recovery(env,tmp_path):
    root=tmp_path/'raw-fixture';root.mkdir();(root/'media').mkdir()
    project=Path(__file__).parents[1]
    from people_catalog_fixture import catalog_beside
    stack=catalog_beside(env[1]);people,schema=stack.__enter__()
    (root/'fixture.json').write_text(json.dumps({'schema':schema,'catalog':people,'delay_seconds':2}))
    marker=root/'foreground';marker.touch();os.utime(marker,(time.time()-10,time.time()-10))
    def add(n):
        path=root/'media'/f'{n}.png';Image.new('RGB',(100,100),(n*15,80,90)).save(path)
        sha=hashlib.sha256(path.read_bytes()).hexdigest()
        with database.connection() as db:
            return db.execute("INSERT INTO media(sha256,relative_path,name,kind,size,captured_at) VALUES(%s,%s,%s,'image',%s,'2026-01-01'::timestamp+%s*interval '1 day') RETURNING id",(sha,path.name,path.name,path.stat().st_size,n)).fetchone()['id']
    ids=[add(i) for i in range(1,5)]
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    base=f'http://127.0.0.1:{port}'
    def request(path,body=None,role='admin'):
        req=urllib.request.Request(base+path,data=json.dumps(body).encode() if body is not None else None,
            headers={'Content-Type':'application/json','x-fixture-role':role})
        with urllib.request.urlopen(req,timeout=5) as response:return json.load(response)
    def wait(fn,seconds=25):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            try:
                value=fn()
                if value:return value
            except (ConnectionError,urllib.error.URLError,FileNotFoundError):pass
            time.sleep(.1)
        raise AssertionError('Fixture timeout; see '+str(root/'worker.log'))
    flags=getattr(subprocess,'CREATE_NO_WINDOW',0)
    log=(root/'server.log').open('wb')
    server=subprocess.Popen([sys.executable,'-m','face_detect.testing.automatic_people_fixture','--root',str(root),'--port',str(port)],cwd=project,stdout=log,stderr=log,creationflags=flags)
    processes=[]
    try:
        wait(lambda:request('/api/automatic-people'))
        for role in ('regular','none'):
            with pytest.raises(urllib.error.HTTPError) as error:request('/api/automatic-people/jobs',{'id':'denied','media_ids':ids},role)
            assert error.value.code in (401,403)
        with pytest.raises(urllib.error.HTTPError) as error:request('/api/automatic-people/jobs',{'id':'bad','media_ids':ids,'path':'C:/private'})
        assert error.value.code==422
        started=time.monotonic();status=request('/api/automatic-people/jobs',{'id':'cancel-probe','media_ids':ids})
        start_seconds=time.monotonic()-started
        assert start_seconds<1.5 and status['state'] in ('queued','running')
        (root/'start-timing.json').write_text(json.dumps({'seconds':start_seconds,'state':status['state'],'synthetic_request_seconds':2}))
        request('/api/automatic-people/jobs/cancel-probe/control',{'action':'cancel'})
        time.sleep(.5)
        browser=subprocess.run(['node',str(project/'tests/automatic-people-jobs-browser.mjs'),base],cwd=project,capture_output=True,text=True,timeout=60,creationflags=flags)
        assert browser.returncode==0,browser.stderr+'\n'+(root/'worker.log').read_text()
        json.loads(browser.stdout.strip());(root/'browser.json').write_text(browser.stdout)
        groups=request('/api/automatic-people')['groups'];assert len(groups)==1 and len(groups[0]['faces'])==4
        with database.connection() as db:
            rows=db.execute('SELECT id,review_state,box FROM face_detections ORDER BY id').fetchall()
            assert len(rows)==4 and all(r['review_state']=='unreviewed' for r in rows)
            assert all(set(r['box'])=={'x','y','width','height'} for r in rows)
        snapshot=[r['id'] for r in rows]
        catalog=request('/api/automatic-people')
        request(f"/api/automatic-people/groups/{groups[0]['id']}/name",{'revision':catalog['revision'],'name':'Fixture person'})
        # Real process termination after an inference checkpoint begins; resume must
        # reclaim the released lease and preserve previously committed detections.
        later=add(5);request('/api/automatic-people/jobs',{'id':'restart','media_ids':[later]})
        pid=wait(lambda:json.loads((root/'jobs/restart/process.json').read_text())['pid']);processes.append(pid)
        wait(lambda:json.loads((root/'jobs/restart/raw.json').read_text())['state']=='running')
        os.kill(pid,signal.SIGTERM);time.sleep(.3)
        request('/api/automatic-people/jobs/restart/control',{'action':'resume'})
        wait(lambda:request('/api/automatic-people/jobs/restart')['state']=='completed')
        assert request('/api/automatic-people/jobs/restart')['receipt']['known_attachments']==1
        request('/api/automatic-people/jobs',{'id':'replay','media_ids':ids+[later]})
        wait(lambda:request('/api/automatic-people/jobs/replay')['state']=='completed')
        with database.connection() as db:
            found=db.execute('SELECT id FROM face_detections ORDER BY id').fetchall()
            assert len(found)==5 and [r['id'] for r in found[:4]]==snapshot
        assert len(request('/api/automatic-people')['groups'][0]['faces'])==5
        add(6)
        cancel=subprocess.run(['node',str(project/'tests/automatic-people-jobs-browser.mjs'),base,'cancel'],cwd=project,capture_output=True,text=True,timeout=45,creationflags=flags)
        assert cancel.returncode==0,cancel.stderr
        (root/'browser-cancel.json').write_text(cancel.stdout)
        time.sleep(2.2) # Let the cancelled in-flight synthetic response be discarded.
        with database.connection() as db:
            other=db.execute("INSERT INTO people(name) VALUES('Manual person') RETURNING id").fetchone()['id']
        catalog=request('/api/automatic-people')
        request(f'/api/automatic-people/faces/{snapshot[0]}/correct',{'revision':catalog['revision'],'person_id':other})
        catalog=request('/api/automatic-people')
        request(f'/api/automatic-people/faces/{snapshot[1]}/ignored',{'revision':catalog['revision'],'enabled':True})
        with database.connection() as db:db.execute('INSERT INTO media_trash(media_id) VALUES(%s)',(ids[2],))
        # New receipt over cached detections must not undo manual/ignored decisions.
        request('/api/automatic-people/jobs',{'id':'manual-replay','media_ids':[ids[0],ids[1],ids[3],later]})
        wait(lambda:request('/api/automatic-people/jobs/manual-replay')['state']=='completed')
        with database.connection() as db:
            assert db.execute('SELECT person_id FROM people_assignments WHERE detection_id=%s',(snapshot[0],)).fetchone()['person_id']==other
            assert db.execute('SELECT ignored FROM face_detections WHERE id=%s',(snapshot[1],)).fetchone()['ignored']
        visible=[f['detection_id'] for g in request('/api/automatic-people')['groups'] for f in g['faces']]
        assert snapshot[1] not in visible and snapshot[2] not in visible
        with pytest.raises(urllib.error.HTTPError) as error:request('/api/automatic-people/jobs',{'id':'trash','media_ids':[ids[2]]})
        assert error.value.code==422
        changed=add(7);original=(root/'media/7.png').read_bytes();(root/'media/7.png').write_bytes(b'changed fixture')
        request('/api/automatic-people/jobs',{'id':'changed','media_ids':[changed]})
        # A checksum mismatch is a storage-integrity failure, not a successful scan.
        wait(lambda:request('/api/automatic-people/jobs/changed')['state']=='blocked')
        (root/'media/7.png').write_bytes(original) # Test restores its own synthetic input.
        escaped=add(8)
        with database.connection() as db:db.execute("UPDATE media SET relative_path='../outside.png' WHERE id=%s",(escaped,))
        request('/api/automatic-people/jobs',{'id':'escape','media_ids':[escaped]})
        wait(lambda:request('/api/automatic-people/jobs/escape')['state']=='blocked')
        with database.connection() as db:
            assert not db.execute('SELECT 1 FROM face_analyses WHERE media_id=ANY(%s)',([changed,escaped],)).fetchone()
            db.execute("UPDATE media SET relative_path='8.png' WHERE id=%s",(escaped,))
        waiting=add(9);original=(root/'media/9.png').read_bytes()
        request('/api/automatic-people/jobs',{'id':'receipt-wait','media_ids':[waiting]})
        wait(lambda:request('/api/automatic-people/jobs/receipt-wait')['state']=='running')
        # Browsing no longer pauses scans; use an actual import handoff signal.
        handoff=root/'import-handoff.json'
        write_json(handoff,{'state':'pending','deadline':time.time()+60})
        wait(lambda:request('/api/automatic-people/jobs/receipt-wait')['state']=='waiting-for-foreground')
        (root/'media/9.png').write_bytes(b'changed while receipt waited')
        handoff.unlink()
        # State is published before the process has released its launch lease.
        wait(lambda:request('/api/automatic-people/jobs/receipt-wait/control',{'action':'resume'}))
        wait(lambda:request('/api/automatic-people/jobs/receipt-wait')['state']=='blocked')
        (root/'media/9.png').write_bytes(original)
        # Synthetic originals were only read by the worker.
        with database.connection() as db:
            for row in db.execute('SELECT relative_path,sha256 FROM media').fetchall():
                assert hashlib.sha256((root/'media'/row['relative_path']).read_bytes()).hexdigest()==row['sha256']
    finally:
        # Only PIDs recorded by this fixture's child processes; no broad process kill.
        for path in (root/'jobs').glob('*/process.json'):
            pid=json.loads(path.read_text())['pid']
            try:os.kill(pid,signal.SIGTERM)
            except (OSError,PermissionError):pass
        server.terminate();server.wait(timeout=10);log.close()
        stack.__exit__(None,None,None)
