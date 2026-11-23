from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_automatic_people import automatic,pool,env,observations
from test_unexamined_photos import disposable_migrations
from app import database
from face_detect.recognition import automatic_people as auto
from face_detect.api.automatic_people_api import router

def test_fixture_preview_roles_naming_and_stale_revision(automatic):
    with database.connection() as db:auto.ingest(db,'preview','synthetic-auto',observations(automatic))
    app=FastAPI()
    @app.middleware('http')
    async def fixture_identity(request,call_next):
        role=request.headers.get('x-fixture-role','admin')
        request.state.authenticated=role!='none';request.state.user={'username':'fixture','role':role}
        return await call_next(request)
    app.include_router(router)
    with TestClient(app) as client:
        result=client.get('/api/automatic-people');assert result.status_code==200
        assert 'vector' not in result.text and 'embedding' not in result.text
        data=result.json();gid=data['groups'][0]['id']
        route=f'/api/automatic-people/groups/{gid}/order'
        assert client.post(route,json={'revision':data['revision'],'direction':'up'},headers={'x-fixture-role':'regular'}).status_code==403
        assert client.post(route,json={'revision':data['revision']+1,'direction':'up'}).status_code==409
        assert client.post(route,json={'revision':data['revision'],'direction':'sideways'}).status_code==422
        assert client.post(route,json={'revision':data['revision'],'direction':'up'}).status_code==200
        assert client.post(f'/api/automatic-people/groups/{gid}/name',json={'revision':data['revision'],'name':'Fixture Name'},headers={'x-fixture-role':'regular'}).status_code==403
        assert client.get('/api/automatic-people',headers={'x-fixture-role':'regular'}).status_code==200
        assert client.get('/api/automatic-people',headers={'x-fixture-role':'none'}).status_code==401
        assert client.post(f'/api/automatic-people/groups/{gid}/name',json={'revision':data['revision'],'name':'Fixture Name'}).status_code==200
        assert client.post(f'/api/automatic-people/groups/{gid}/name',json={'revision':data['revision'],'name':'Another'}).status_code==409
        assert any(g['name']=='Fixture Name' for g in client.get('/api/automatic-people').json()['groups'])

def test_router_not_registered_in_production_and_default_disabled(monkeypatch):
    from app.api import app
    assert not any(getattr(r,'path','').startswith('/api/automatic-people') for r in app.routes)
    monkeypatch.setattr(auto,'load_config',lambda:{})
    from face_detect.api.automatic_people_api import invoke
    from fastapi import HTTPException
    import pytest
    with pytest.raises(HTTPException) as e:invoke('catalog')
    assert e.value.status_code==409


def test_ignored_list_restore_revision_and_visibility(automatic):
    from face_detect.api.automatic_people_api import administrator
    from face_detect.api.automatic_people_api import reader
    with database.connection() as db:
        auto.ingest(db,'ignored-preview','synthetic-auto',observations(automatic))
        did=auto.catalog(db)['groups'][0]['faces'][0]['detection_id']
        auto.ignore(db,did,True,'fixture')
    app=FastAPI()
    app.dependency_overrides[reader]=lambda:{'username':'fixture','role':'admin'}
    app.dependency_overrides[administrator]=lambda:{'username':'fixture','role':'admin'}
    app.include_router(router)
    with TestClient(app) as client:
        data=client.get('/api/automatic-people/ignored').json()
        assert [r['detection_id'] for r in data['items']]==[did]
        assert 'vector' not in str(data) and 'embedding' not in str(data)
        assert client.get('/api/automatic-people/ignored?page=0').status_code==422
        path=f'/api/automatic-people/faces/{did}/ignored'
        assert client.post(path,json={'revision':data['revision']-1,'enabled':False}).status_code==409
        assert client.post(path,json={'revision':data['revision'],'enabled':False}).status_code==200
        assert client.get('/api/automatic-people/ignored').json()['total']==0
        assert any(f['detection_id']==did for g in client.get('/api/automatic-people').json()['groups'] for f in g['faces'])
    with database.connection() as db:
        auto.ignore(db,did,True,'fixture')
        mid=auto.ignored_collection(db)['items'][0]['media_id']
        db.execute('INSERT INTO media_trash(media_id) VALUES(%s)',(mid,))
        assert auto.ignored_collection(db)['total']==0


def test_raw_media_selection_contract_has_no_client_paths(monkeypatch):
    from face_detect.api.automatic_people_api import job_router
    from face_detect.api.automatic_people_api import administrator
    from face_detect.api.automatic_people_api import reader
    monkeypatch.setattr(auto,'load_config',lambda:{'automatic_people':{'enabled':True}})
    calls=[]
    class Jobs:
        def create(self,name,ids,user):calls.append((name,ids))
        def status(self,name,user):return {'id':name,'state':'queued'}
    app=FastAPI();identity=lambda:{'username':'fixture','role':'admin'}
    app.dependency_overrides[administrator]=identity;app.dependency_overrides[reader]=identity
    dispatch_result=[True]
    app.include_router(job_router(Jobs(),lambda ids:ids,lambda *args:dispatch_result[0],raw_media=True))
    with TestClient(app) as client:
        path='/api/automatic-people/jobs'
        assert client.post(path,json={'id':'raw','media_ids':[1,2]}).status_code==200
        assert calls==[('raw',[1,2])]
        for payload in ({'id':'raw','media_ids':[True]}, {'id':'raw','media_ids':[1],'path':'private'}, {'id':'raw','detection_ids':[1]}):
            assert client.post(path,json=payload).status_code==422
        dispatch_result[0]=False
        assert client.post(path,json={'id':'busy','media_ids':[1]}).status_code==409


def test_recheck_stream_reports_wait_then_result():
    import json
    from face_detect.api.automatic_people_api import job_router
    from face_detect.api.automatic_people_api import administrator
    class Jobs:
        def recheck(self,name,revision,user,progress):
            assert name=='completed-imports' and revision==3
            progress('waiting',0,None)
            progress('comparing',0,None)
            return {'matched':2}
    app=FastAPI()
    app.dependency_overrides[administrator]=lambda:{'username':'fixture','role':'admin'}
    app.include_router(job_router(Jobs(),lambda ids:ids,lambda *args:True,raw_media=True))
    with TestClient(app) as client:
        response=client.post('/api/automatic-people/jobs/completed-imports/recheck/stream',json={'revision':3})
        assert response.status_code==200
        events=[json.loads(line) for line in response.text.splitlines()]
        assert any(event.get('phase')=='waiting' for event in events)
        assert any(event.get('phase')=='comparing' for event in events)
        assert events[-1]=={'state':'completed','result':{'matched':2}}


def test_explicit_launcher_tracks_distinct_jobs(monkeypatch,tmp_path):
    from types import SimpleNamespace
    from face_detect.jobs import people_launch
    calls=[]
    monkeypatch.setattr(people_launch,'_processes',{})
    monkeypatch.setattr(people_launch.subprocess,'Popen',lambda *a,**kw:calls.append(a) or SimpleNamespace(poll=lambda:None))
    for key in ('first','second'):
        assert people_launch.launch_explicit('face_detect.testing.automatic_people_fixture',['--worker',key],key,tmp_path/'worker.log')
    assert not people_launch.launch_explicit('face_detect.testing.automatic_people_fixture',['--worker','first'],'first',tmp_path/'worker.log')
    assert len(calls)==2


def test_fixture_admission_failure_is_explicitly_resumable(monkeypatch,tmp_path):
    import json
    import sys
    from types import SimpleNamespace
    from face_detect.testing import automatic_people_fixture as fixture
    job=SimpleNamespace(directory=tmp_path)
    (tmp_path/'raw.json').write_text(json.dumps({'state':'queued','actor':'fixture'}))
    def busy(*args):raise RuntimeError('Compute unavailable')
    monkeypatch.setattr(fixture,'runtime',lambda root:SimpleNamespace(job=lambda name:job,run=busy))
    monkeypatch.setattr(sys,'argv',['fixture','--root',str(tmp_path),'--worker','one'])
    import pytest
    with pytest.raises(RuntimeError,match='Compute'):fixture.main()
    assert json.loads((tmp_path/'raw.json').read_text())['state']=='waiting-worker'


def test_desktop_year_is_resolved_server_side(automatic,tmp_path):
    from types import SimpleNamespace
    from face_detect.testing.automatic_people_fixture import application
    captured=[]
    jobs=SimpleNamespace(source=lambda db,mid:None,create=lambda name,ids,user:captured.extend(ids),status=lambda *args:{'state':'queued'})
    app=application(tmp_path,jobs=jobs,dispatch=lambda *args:True,desktop=True)
    with TestClient(app) as client:
        choices=client.get('/api/automatic-people/selections');assert choices.status_code==200
        assert choices.json()[0]['id']=='year:2026'
        assert client.post('/api/automatic-people/jobs',json={'id':'one','selection':'year:2026'}).status_code==200
        assert captured==automatic
        assert client.post('/api/automatic-people/jobs',json={'id':'two','media_ids':automatic}).status_code==422
        assert client.post('/api/automatic-people/jobs',json={'id':'two','selection':'year:1900'}).status_code==422
        with database.connection() as db:
            jid=db.execute("INSERT INTO jobs(source_root) VALUES('fixture-import') RETURNING id").fetchone()['id']
            for mid in automatic[:2]:db.execute("INSERT INTO import_items(job_id,source_path,size,mtime_ns,media_id) VALUES(%s,%s,0,0,%s)",(jid,str(mid),mid))
        captured.clear()
        assert client.post('/api/automatic-people/jobs',json={'id':'import','selection':'import:'+str(jid)}).status_code==200
        assert captured==automatic[:2]


def test_detail_history_and_targeted_undo_preserve_other_members(automatic):
    from test_automatic_people import setup_groups
    import pytest
    with database.connection() as db:
        ga,gb,pa,pb=setup_groups(db,automatic)
        eid=auto.correct(db,automatic[0],pb,'fixture')['event_id']
        detail=auto.detail(db,ga);entry=next(e for e in detail['history'] if e['id']==eid)
        assert entry['can_undo'] and 'before_state' not in entry
        auto.undo_correction(db,eid,'fixture')
        assert db.execute('SELECT group_id FROM auto_people_members WHERE detection_id=%s',(automatic[0],)).fetchone()['group_id']==ga
        assert db.execute('SELECT group_id FROM auto_people_members WHERE detection_id=%s',(automatic[2],)).fetchone()['group_id']==gb
        assert next(e for e in auto.detail(db,ga)['history'] if e['id']==eid)['undone']
        stale=auto.correct(db,automatic[0],pb,'fixture')['event_id']
        auto.ignore(db,automatic[1],True,'fixture')
        assert not next(e for e in auto.detail(db,gb)['history'] if e['id']==stale)['can_undo']
        with pytest.raises(RuntimeError,match='Undo is no longer available'):auto.undo_correction(db,stale,'fixture')
