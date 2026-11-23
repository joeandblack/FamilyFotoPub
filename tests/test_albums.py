"""Album mutations run only in the disposable upload-test schema."""
import hashlib
import uuid
from pathlib import Path
import pytest
from test_uploads import env
from app import database
from app.library import trash

@pytest.fixture
def catalog(env,tmp_path,monkeypatch):
    client,config,data=env
    ids=[]
    with database.connection() as db:
        for index,kind in enumerate(['image','video','image']):
            raw=f'album fixture {index}'.encode();relative=f'{index}.jpg';(tmp_path/'destination'/relative).write_bytes(raw)
            ids.append(db.execute("INSERT INTO media(sha256,relative_path,name,kind,size) VALUES (%s,%s,'same.jpg',%s,%s) RETURNING id",(hashlib.sha256(raw).hexdigest(),relative,kind,len(raw))).fetchone()['id'])
        job=db.execute("INSERT INTO jobs(source_root,state) VALUES ('fixture/import','completed-review-required') RETURNING id").fetchone()['id']
        for n,mid in enumerate([ids[0],ids[0],ids[1],ids[2]]):
            db.execute("INSERT INTO import_items(job_id,source_path,size,mtime_ns,state,media_id) VALUES (%s,%s,15,0,%s,%s)",(job,f'folder/{n}', 'imported' if n==0 else 'duplicate',mid))
    monkeypatch.setattr(trash,'DATA',data);monkeypatch.setattr(trash,'WORKER_LOCK',uuid.uuid4().int%(2**63-1))
    return client,ids,job,config

def create(client,**kwargs):
    r=client.post('/api/albums',json={'name':'Family',**kwargs});assert r.status_code==200,r.text;return r.json()['id']

def members(client,aid):return [r['id'] for r in client.get('/api/media',params={'album':aid}).json()['items']]

def test_lightweight_import_summaries_and_scoped_details(catalog,monkeypatch):
    client,_,job,_=catalog
    from app.imports import history as import_history
    with database.connection() as db:
        other=db.execute("INSERT INTO jobs(source_root,state) VALUES ('other/import','completed-review-required') RETURNING id").fetchone()['id']
    with monkeypatch.context() as patch:
        patch.setattr(import_history,'history',lambda *args:pytest.fail('Summary must not aggregate media'))
        result=client.get('/api/import/history?summary=true')
        assert result.status_code==200,result.text
        assert {b['id'] for b in result.json()['imports']}=={job,other}
        assert all(b['summary_only'] and 'counts' not in b for b in result.json()['imports'])
    full=client.get('/api/import/history').json()['imports']
    scoped=client.get(f'/api/import/history?job_id={job}')
    assert scoped.status_code==200,scoped.text
    rows=scoped.json()['imports'];assert len(rows)==1 and rows[0]['id']==job
    expected=next(b for b in full if b['id']==job)
    for key in ['counts','stages','total','total_bytes','media_totals']:
        assert rows[0][key]==expected[key]
    assert client.get(f'/api/issues?job_id={other}').json()=={'imports':[]}

def test_cover_framing_is_album_local_and_resets_on_replacement(catalog):
    c,ids,_,_=catalog
    a=create(c,media_ids=ids)
    b=create(c,media_ids=ids)
    data={'media_id':ids[0],'mode':'fit','zoom':1.5,'x':.3,'y':-.4}
    url=f'/api/albums/{a}/cover-framing'
    r=c.patch(url,json=data)
    assert r.status_code==200,r.text
    assert r.json()['cover_framing']==data
    assert c.get(f'/api/albums/{b}').json()['cover_framing']=={}
    assert c.patch(url,json={**data,'zoom':0}).status_code==422
    assert c.patch(url,json={**data,'x':2}).status_code==422
    assert c.patch(url,json={**data,'mode':'stretch'}).status_code==422
    assert c.post(f'/api/albums/{a}/items/{ids[2]}/cover',json={}).json()['cover_framing']=={}
    assert c.patch(url,json=data).status_code==409
    assert c.get(f'/api/albums/{a}').json()['cover_id']==ids[2]

def test_membership_warning_and_album_only_removal(catalog):
    c,ids,_,_=catalog
    a=create(c,name='A',media_ids=ids)
    b=create(c,name='B',media_ids=[ids[0]])
    result=c.post('/api/albums/membership',json={'media_ids':[ids[0],ids[0],ids[1]]})
    assert result.status_code==200
    assert result.json()['albums']==[{'id':a,'name':'A','item_count':2},{'id':b,'name':'B','item_count':1}]
    assert c.post(f'/api/albums/{a}/remove',json={'media_ids':[ids[0]]}).json()=={'removed':1}
    assert members(c,b)==[ids[0]]
    with database.connection() as db:
        assert db.execute('SELECT 1 FROM media WHERE id=%s',(ids[0],)).fetchone()
        assert not db.execute('SELECT 1 FROM media_trash WHERE media_id=%s',(ids[0],)).fetchone()
    assert c.post('/api/albums/membership',json={'media_ids':[]}).json()=={'albums':[]}

def test_drag_item_order_is_album_local_and_validates_target(catalog):
    c,ids,_,_=catalog
    a=create(c,name='A',media_ids=ids)
    b=create(c,name='B',media_ids=ids)
    response=c.post(f'/api/albums/{a}/items/{ids[0]}/position',json={'target_id':ids[2],'after':True})
    assert response.json()=={'order':[ids[1],ids[2],ids[0]]}
    assert members(c,a)==[ids[1],ids[2],ids[0]]
    assert members(c,b)==ids
    assert c.post(f'/api/albums/{a}/items/{ids[0]}/position',json={'target_id':ids[1]}).json()=={'order':ids}
    assert c.post(f'/api/albums/{a}/items/{ids[0]}/position',json={'target_id':ids[0]}).json()=={'order':ids}
    assert c.post(f'/api/albums/{a}/items/{ids[0]}/position',json={'target_id':999999}).status_code==404
    assert members(c,a)==ids

def test_transfer_existing_album_is_atomic_and_preserves_other_albums(catalog):
    c,ids,_,_=catalog
    source=create(c,name='Source',media_ids=ids)
    target=create(c,name='Target',media_ids=[ids[0]])
    other=create(c,name='Other',media_ids=ids)
    response=c.post(f'/api/albums/{source}/transfer',json={'target_id':target,'media_ids':ids[:2]})
    assert response.status_code==200,response.text
    assert response.json()=={'id':target,'moved':2,'added':1}
    assert members(c,source)==ids[2:]
    assert members(c,target)==ids[:2]
    assert members(c,other)==ids
    assert c.post(f'/api/albums/{source}/transfer',json={'target_id':target,'media_ids':ids}).status_code==409
    assert members(c,source)==ids[2:]
    assert c.post(f'/api/albums/{source}/transfer',json={'target_id':source,'media_ids':ids[2:]}).status_code==400
    assert c.post(f'/api/albums/{source}/transfer',json={'target_id':999999,'media_ids':ids[2:]}).status_code==404

def test_transfer_new_album_rolls_back_on_failure(catalog):
    c,ids,_,_=catalog
    source=create(c,media_ids=ids)
    with database.connection() as db:
        db.execute('INSERT INTO media_trash(media_id) VALUES (%s)',(ids[0],))
    response=c.post(f'/api/albums/{source}/transfer',json={'new_album':{'name':'Failed'},'media_ids':ids})
    assert response.status_code==409
    assert len(c.get('/api/albums').json()['albums'])==1
    response=c.post(f'/api/albums/{source}/transfer',json={'new_album':{'name':'Moved'},'media_ids':ids[1:]})
    assert response.status_code==200,response.text
    assert members(c,response.json()['id'])==ids[1:]
    with database.connection() as db:
        assert db.execute('SELECT count(*) n FROM media WHERE id=ANY(%s)',(ids,)).fetchone()['n']==3

def test_create_and_add_all_accept_more_than_one_gallery_page(catalog):
    c,_,_,_=catalog
    with database.connection() as db:
        ids=[row['id'] for row in db.execute("INSERT INTO media(sha256,relative_path,name,kind,size) SELECT md5('large-album-'||n)||md5('large-album-'||n),'large-album-'||n||'.jpg','Album fixture','image',1 FROM generate_series(1,1205) n RETURNING id").fetchall()]
    aid=create(c,name='Large person',media_ids=ids)
    assert c.get(f'/api/albums/{aid}').json()['item_count']==1205
    target=create(c,name='Existing album')
    assert c.post(f'/api/albums/{target}/items',json={'media_ids':ids}).json()=={'added':1205}
    assert c.post(f'/api/albums/{target}/items',json={'media_ids':ids}).json()=={'added':0}
    response=c.post('/api/albums',json={'name':'Invalid large selection','media_ids':ids+[999999999]})
    assert response.status_code==404
    assert len(c.get('/api/albums').json()['albums'])==2

def test_album_order_is_persistent_and_does_not_change_members(catalog):
    c,ids,_,_=catalog
    a=create(c,name='A',media_ids=ids)
    b=create(c,name='B')
    d=create(c,name='C')
    order=lambda:[r['id'] for r in c.get('/api/albums').json()['albums']]
    assert order()==[d,b,a]
    assert c.post(f'/api/albums/{a}/position',json={'target_id':d,'after':False}).json()['order']==[a,d,b]
    assert order()==[a,d,b]
    assert c.post(f'/api/albums/{a}/position',json={'target_id':b,'after':True}).json()['order']==[d,b,a]
    c.patch(f'/api/albums/{d}',json={'name':'Renamed'})
    assert order()==[d,b,a] and members(c,a)==ids
    new=create(c,name='New')
    assert order()==[d,b,a,new]
    assert c.post(f'/api/albums/{a}/position',json={'target_id':a}).status_code==200
    assert c.post(f'/api/albums/{a}/position',json={'target_id':999999}).status_code==404
    assert c.post(f'/api/albums/{a}/position',json={'target_id':True}).status_code==422
    assert order()==[d,b,a,new]

def test_empty_selected_later_additions_and_identity(catalog,tmp_path):
    c,ids,job,_=catalog
    empty=create(c,description='Add later');assert members(c,empty)==[]
    selected=create(c,media_ids=[ids[0],ids[1],ids[0]])
    assert members(c,selected)==ids[:2]
    assert c.post(f'/api/albums/{empty}/items',json={'media_ids':ids}).json()=={'added':3}
    assert c.post(f'/api/albums/{empty}/items',json={'media_ids':ids}).json()=={'added':0}
    assert members(c,empty)==ids
    assert c.patch(f'/api/albums/{empty}',json={'name':'  Renamed  ','description':'<text>'}).json()['name']=='Renamed'
    assert len(c.get('/api/albums').json()['albums'])==2
    for n in range(3):assert (tmp_path/'destination'/f'{n}.jpg').read_bytes()==f'album fixture {n}'.encode()

def test_import_associations_snapshot_and_trash(catalog):
    c,ids,job,_=catalog
    assert c.post(f'/api/media/{ids[2]}/trash/trash').status_code==200
    aid=create(c,import_job=job)
    assert set(members(c,aid))==set(ids[:2])
    assert len(members(c,aid))==2
    assert c.post(f'/api/media/{ids[2]}/trash/restore').status_code==200
    assert len(members(c,aid))==2 # snapshot, not a smart album
    assert c.post(f'/api/albums/{aid}/items',json={'media_ids':[ids[2]]}).json()['added']==1

def test_order_cover_remove_and_delete_are_membership_only(catalog,tmp_path):
    c,ids,_,_=catalog;aid=create(c,media_ids=ids)
    assert c.post(f'/api/albums/{aid}/items/{ids[1]}/cover').json()['cover_id']==ids[1]
    for direction,expected in [('first',[ids[2],ids[0],ids[1]]),('next',[ids[0],ids[2],ids[1]]),('last',ids),('previous',[ids[0],ids[2],ids[1]])]:
        assert c.post(f'/api/albums/{aid}/items/{ids[2]}/move',json={'direction':direction}).status_code==200
        assert members(c,aid)==expected
    assert c.post(f'/api/albums/{aid}/remove',json={'media_ids':[ids[1]]}).json()['removed']==1
    assert c.get(f'/api/albums/{aid}').json()['cover_id']==ids[0]
    assert c.get('/api/media').json()['total']==3
    assert c.delete(f'/api/albums/{aid}').status_code==200
    assert c.get('/api/media').json()['total']==3
    with database.connection() as db:assert db.execute('SELECT count(*) n FROM album_items').fetchone()['n']==0
    assert len(list((tmp_path/'destination').iterdir()))==3

def test_trash_restore_cover_order_and_permanent_cleanup(catalog):
    c,ids,_,_=catalog;aid=create(c,media_ids=ids)
    c.post(f'/api/albums/{aid}/items/{ids[0]}/cover')
    for mid in [ids[0],ids[1]]:
        assert c.post(f'/api/media/{mid}/trash/trash').status_code==200
        assert mid not in members(c,aid)
        assert c.post(f'/api/albums/{aid}/items',json={'media_ids':[mid]}).status_code==409
        assert c.post(f'/api/media/{mid}/trash/restore').status_code==200
        assert members(c,aid)==ids
    assert c.get(f'/api/albums/{aid}').json()['cover_id']==ids[0]
    # Existing permanent-deletion completion writes this tombstone; exercise the DB trigger.
    c.post(f'/api/media/{ids[0]}/trash/trash')
    with database.connection() as db:
        db.execute('UPDATE media_trash SET deleting_at=now(),deleted_at=now() WHERE media_id=%s',(ids[0],))
        assert not db.execute('SELECT 1 FROM album_items WHERE media_id=%s',(ids[0],)).fetchone()
    assert c.post(f'/api/media/{ids[0]}/trash/restore').status_code==410
    assert c.get(f'/api/albums/{aid}').json()['cover_id']==ids[1]

def test_validation_atomicity_permissions_and_csrf(catalog):
    c,ids,job,config=catalog
    for payload in [{'name':' '},{'name':'x'*201},{'name':'ok','media_ids':[True]},{'name':'ok','media_ids':[ids[0],999999999]},{'name':'ok','import_job':job,'media_ids':ids[:1]}]:
        assert c.post('/api/albums',json=payload).status_code in (404,422)
    assert c.get('/api/albums').json()['albums']==[]
    c.post('/api/login',json={'username':'reader','password':'fixture'})
    aid=create(c,media_ids=ids[:1])
    assert c.post(f'/api/albums/{aid}/items/{ids[1]}/cover').status_code==404
    assert c.post('/api/albums',json={'name':'CSRF'},headers={'Origin':'https://other.invalid'}).status_code==403
    from app.auth import password_hash
    config['browser_accounts']['guest']={'role':'guest','password_hash':password_hash('fixture')}
    c.post('/api/login',json={'username':'guest','password':'fixture'})
    assert c.get(f'/api/albums/{aid}').status_code==200
    for method,path,payload in [('post','/api/albums',{'name':'No'}),('patch',f'/api/albums/{aid}',{'name':'No'}),('delete',f'/api/albums/{aid}',None),('post',f'/api/albums/{aid}/items',{'media_ids':ids}),('post',f'/api/albums/{aid}/remove',{'media_ids':ids}),('post',f'/api/albums/{aid}/items/{ids[0]}/cover',{}),('post',f'/api/albums/{aid}/items/{ids[0]}/move',{'direction':'first'})]:
        assert c.request(method,path,json=payload).status_code==403
    c.cookies.clear();assert c.get('/api/albums').status_code==401

def test_additive_migration_rollback_and_reapply(catalog):
    c,ids,_,_=catalog;aid=create(c,media_ids=ids)
    with database.connection() as db:
        before=db.execute('SELECT * FROM media ORDER BY id').fetchall()
        db.execute('SAVEPOINT rollback_test')
        db.execute('DROP TABLE album_collection_members')
        db.execute('DROP TRIGGER album_permanent_delete ON media_trash')
        db.execute('DROP FUNCTION cleanup_deleted_album_membership()')
        db.execute('DROP TABLE album_items');db.execute('DROP TABLE albums');db.execute('DELETE FROM schema_versions WHERE version=12')
        assert db.execute('SELECT * FROM media ORDER BY id').fetchall()==before
        db.execute('ROLLBACK TO SAVEPOINT rollback_test')
        assert db.execute('SELECT count(*) n FROM album_items').fetchone()['n']==3
        db.execute((Path('databases')/'012_albums.sql').read_text())
        assert db.execute('SELECT * FROM media ORDER BY id').fetchall()==before
    database.migrate();assert members(c,aid)==ids


def test_browser_album_workflows(catalog,monkeypatch,tmp_path):
    import os
    import json
    import subprocess
    import threading
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    from urllib.parse import urlsplit
    if os.environ.get('FAMILYFOTO_BROWSER')!='1':pytest.skip('Requires installed Windows Chrome')
    c,ids,job,config=catalog
    config['destination'].update(drive='fixture',volume_serial='fixture')
    monkeypatch.setattr('app.storage.disks.volume_serial',lambda _: 'fixture')
    from PIL import Image
    with database.connection() as db:
        for row in db.execute('SELECT id,sha256,kind,relative_path FROM media WHERE id=ANY(%s)',(ids,)):
            image=Image.new('RGB',(160,120),(70,110+row['id']*20,90))
            image.save(tmp_path/'data'/'thumbnails'/(row['sha256']+'.jpg'))
            if row['kind']=='image':
                image.save(tmp_path/'destination'/row['relative_path'])
    root=Path(os.environ.get('FAMILYFOTO_WEB_ROOT','web')).resolve()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def handle_request(self):
            path=urlsplit(self.path).path
            if path=='/' or path.startswith('/web/'):
                file=root/('index.html' if path=='/' else path[5:])
                assert file.resolve().is_relative_to(root)
                body=file.read_bytes();status=200;mime='text/html' if path=='/' else 'text/css' if file.suffix=='.css' else 'text/javascript'
            else:
                data=self.rfile.read(int(self.headers.get('Content-Length',0)))
                response=c.request(self.command,self.path,content=data,headers={'Content-Type':'application/json'})
                body=response.content;status=response.status_code;mime=response.headers.get('content-type','application/octet-stream')
            self.send_response(status);self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        do_GET=do_POST=do_PATCH=do_DELETE=handle_request
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    fixture=tmp_path/'browser-fixture.json';fixture.write_text(json.dumps({'url':f'http://127.0.0.1:{server.server_port}/','ids':ids,'job':job}))
    try:
        result=subprocess.run(['node','tests/albums-browser.mjs',str(fixture)],capture_output=True,text=True,timeout=120)
        (tmp_path/'browser-result.txt').write_text(result.stdout+result.stderr)
        assert result.returncode==0,result.stdout+result.stderr
        print(result.stdout)
    finally:server.shutdown();server.server_close();worker.join()


def test_large_import_album_order_across_pages(catalog):
    c,ids,job,_=catalog
    with database.connection() as db:
        db.execute("INSERT INTO media(sha256,relative_path,name,kind,size) SELECT md5(n::text)||md5(n::text),n::text||'.jpg','page fixture','image',1 FROM generate_series(1000,2000) n")
        db.execute("INSERT INTO import_items(job_id,source_path,size,mtime_ns,state,media_id) SELECT %s,'page/'||id,1,0,'duplicate',id FROM media WHERE name='page fixture'",(job,))
    aid=create(c,import_job=job)
    first=c.get('/api/media',params={'album':aid}).json()
    second=c.get('/api/media',params={'album':aid,'page':2}).json()
    assert first['total']==1004 and len(first['items'])==1000 and len(second['items'])==4
    small=c.get('/api/media',params={'album':aid,'page_size':50,'page':2}).json()
    assert small['page_size']==50 and len(small['items'])==50 and small['pages']==21
    assert [row['id'] for row in small['items']]==[row['id'] for row in first['items'][50:100]]
    for invalid in [0,-1,1001,'bad']:
        assert c.get('/api/media',params={'page_size':invalid}).status_code==422
    mid=second['items'][-1]['id']
    assert c.post(f'/api/albums/{aid}/items/{mid}/move',json={'direction':'first'}).status_code==200
    assert members(c,aid)[0]==mid
    assert c.get(f'/api/albums/{aid}').json()['item_count']==1004
