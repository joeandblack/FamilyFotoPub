import os
import time
import uuid
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_automatic_people_handoff import completed, automatic, pool, env, ADMIN
from app import database
from face_detect.recognition import automatic_people as auto
from face_detect.jobs import automatic_people_jobs as worker
from face_detect.api.automatic_people_api import router
from face_detect.api.automatic_people_api import job_router


def test_explicit_scan_ignores_browser_activity(monkeypatch):
    from app.jobs import foreground
    from app.jobs import work_admission
    monkeypatch.setattr(foreground,'should_yield',lambda:True)
    monkeypatch.setattr(work_admission,'pending',lambda:False)
    class DB:
        uploading=False
        def execute(self,*args):return self
        def fetchone(self):return {'busy':1} if self.uploading else None
    db=DB()
    assert not worker.foreground_busy(db)
    db.uploading=True
    assert worker.foreground_busy(db)
    db.uploading=False
    monkeypatch.setattr(work_admission,'pending',lambda:True)
    assert worker.foreground_busy(db)


@pytest.fixture
def workflow(completed, tmp_path, monkeypatch):
    ids,state,observer,provider,load,job_for=completed
    # Exercise real shared lock semantics without touching production lock keys.
    monkeypatch.setattr(worker,'COMPUTE_LOCK',uuid.uuid4().int % (2**63-1))
    monkeypatch.setattr(worker,'MAINTENANCE_LOCKS',())
    monkeypatch.setattr(worker,'CHILD_LOCKS',())
    from app.jobs.foreground import MARKER
    MARKER.touch();os.utime(MARKER,(time.time()-10,time.time()-10))
    jobs=worker.Jobs(tmp_path/'workflow',provider,observer,load)
    def inputs(selected):
        return job_for(selected,'manifest-'+str(len(list(tmp_path.iterdir())))).read()['items']
    def create(name,selected):
        records=[r['input'] for r in inputs(selected)]
        return jobs.create(name,records,ADMIN)
    return ids,jobs,create,provider


def test_incremental_name_once_and_manual_constraint_precedence(workflow):
    ids,jobs,create,provider=workflow
    create('first',ids[:2]); assert jobs.run('first',ADMIN)['state']=='completed'
    with database.connection() as db:
        group=auto.catalog(db)['groups'][0]
        auto.name_group(db,group['id'],'Fixture person',ADMIN['username'])
    create('next',ids[2:3]); assert jobs.run('next',ADMIN)['receipt']['known_attachments']==1
    with database.connection() as db:
        other=db.execute("INSERT INTO people(name) VALUES('Other') RETURNING id").fetchone()['id']
        auto.correct(db,ids[2],other,ADMIN['username'])
        a,b=sorted((ids[0],ids[3]));db.execute('INSERT INTO people_constraints VALUES(%s,%s)',(a,b))
    create('incremental',ids[2:]); jobs.run('incremental',ADMIN)
    with database.connection() as db:
        assert db.execute('SELECT person_id FROM people_assignments WHERE detection_id=%s',(ids[2],)).fetchone()['person_id']==other
        assert db.execute('SELECT group_id FROM auto_people_members WHERE detection_id=%s',(ids[3],)).fetchone()['group_id']!=group['id']
    assert jobs.run('first',ADMIN)['state']=='completed'
    with pytest.raises(RuntimeError):jobs.control('first','cancel',ADMIN)


def test_concurrent_resume_pause_and_foreground_yield(workflow,monkeypatch):
    ids,jobs,create,provider=workflow;create('work',ids[:2]);saved=provider._transport
    def request(payload):
        with pytest.raises(RuntimeError,match='owns compute'): jobs.run('work',ADMIN)
        jobs.control('work','pause',ADMIN)
        return saved(payload)
    provider._transport=request
    assert jobs.run('work',ADMIN)['state']=='paused'
    assert jobs.status('work',ADMIN)['done']==1
    provider._transport=saved;jobs.control('work','resume',ADMIN)
    monkeypatch.setattr(worker,'foreground_busy',lambda db:True)
    assert jobs.run('work',ADMIN)['state']=='waiting-service'
    monkeypatch.setattr(worker,'foreground_busy',lambda db:False)
    assert jobs.run('work',ADMIN)['state']=='completed'


def test_cancel_during_request_and_no_resume(workflow):
    ids,jobs,create,provider=workflow;create('work',ids[:2]);saved=provider._transport
    def request(payload):jobs.control('work','cancel',ADMIN);return saved(payload)
    provider._transport=request
    assert jobs.run('work',ADMIN)['state']=='cancelled'
    assert jobs.status('work',ADMIN)['done']==0
    with pytest.raises(RuntimeError):jobs.control('work','resume',ADMIN)


def test_worker_crash_and_atomic_receipt_retry(workflow,monkeypatch):
    ids,jobs,create,provider=workflow;create('work',ids[:2]);saved=provider._transport
    def crash(_):raise KeyboardInterrupt()
    provider._transport=crash
    with pytest.raises(KeyboardInterrupt):jobs.run('work',ADMIN)
    provider._transport=saved;real=worker.accept_checkpoint
    def rollback(*args,**kwargs):real(*args,**kwargs);raise RuntimeError('before commit')
    monkeypatch.setattr(worker,'accept_checkpoint',rollback)
    with pytest.raises(RuntimeError,match='before commit'):jobs.run('work',ADMIN)
    assert jobs.status('work',ADMIN)['state']=='awaiting-receipt'
    provider._transport=crash # Delivery retry must never infer again.
    monkeypatch.setattr(worker,'accept_checkpoint',real)
    assert jobs.run('work',ADMIN)['state']=='completed'
    assert jobs.run('work',ADMIN)['state']=='completed'
    with database.connection() as db:
        assert db.execute('SELECT count(*) n FROM auto_people_receipts').fetchone()['n']==1


def test_model_down_retry_and_changed_service_block(workflow):
    ids,jobs,create,provider=workflow;create('down',ids[:2]);saved=provider._transport
    from face_detect.contracts import ProviderTransient
    def down(_):raise ProviderTransient('offline')
    provider._transport=down
    assert jobs.run('down',ADMIN)['state']=='waiting-service'
    provider._transport=saved;assert jobs.run('down',ADMIN)['state']=='completed'
    create('changed',ids[2:]);original=jobs.observer;calls=[]
    def changed():
        result=original();calls.append(1)
        if len(calls)>1:result['evidence']['manifest']['provider']='changed'
        return result
    jobs.observer=changed
    assert jobs.run('changed',ADMIN)['state']=='blocked'


def test_exclusion_recheck_before_receipt(workflow,monkeypatch):
    ids,jobs,create,provider=workflow;create('work',ids[:2]);real=worker.accept_checkpoint
    def exclude(db,*args,**kwargs):
        db.execute('UPDATE face_detections SET ignored=true WHERE id=%s',(ids[0],))
        mid=auto.rows(db,[ids[1]])[0]['media_id']
        db.execute('INSERT INTO media_trash(media_id) VALUES(%s)',(mid,))
        return real(db,*args,**kwargs)
    monkeypatch.setattr(worker,'accept_checkpoint',exclude)
    result=jobs.run('work',ADMIN)
    assert result['receipt']['accepted']==0 and result['receipt']['excluded']==2


def test_yield_before_commit_and_recover_committed_receipt(workflow,monkeypatch):
    ids,jobs,create,provider=workflow;create('work',ids[:1]);saved=provider._transport
    def foreground_arrives(payload):
        monkeypatch.setattr(worker,'foreground_busy',lambda db:True)
        return saved(payload)
    provider._transport=foreground_arrives
    assert jobs.run('work',ADMIN)['state']=='awaiting-receipt'
    monkeypatch.setattr(worker,'foreground_busy',lambda db:False)
    real=worker.accept_checkpoint
    def committed_then_lost(db,*args,**kwargs):
        real(db,*args,**kwargs);db.commit();raise RuntimeError('response lost after commit')
    monkeypatch.setattr(worker,'accept_checkpoint',committed_then_lost)
    with pytest.raises(RuntimeError,match='response lost'):jobs.run('work',ADMIN)
    assert jobs.status('work',ADMIN)['state']=='completed'
    assert jobs.run('work',ADMIN)['state']=='completed'


def test_maintenance_lease_release_and_model_version(workflow,monkeypatch):
    ids,jobs,create,provider=workflow;create('work',ids[:1])
    # foreground_busy compares these single-key reservations with pg_locks.objid.
    key=uuid.uuid4().int % (2**31-1);monkeypatch.setattr(worker,'MAINTENANCE_LOCKS',(key,))
    with database.connection() as owner:
        owner.execute('SELECT pg_advisory_lock(%s)',(key,))
        with pytest.raises(RuntimeError,match='reservation'):jobs.run('work',ADMIN)
    # A job pins a space: another computer's exact model of the same space may carry it on,
    # another space may not.
    original=provider.space_id;provider.space_id='changed'
    with pytest.raises(ValueError,match='model differs'):jobs.run('work',ADMIN)
    provider.space_id=original
    assert jobs.run('work',ADMIN)['state']=='completed'


def test_direct_authorization_and_disabled_guard(workflow,monkeypatch):
    ids,jobs,create,provider=workflow;create('work',ids[:1])
    for user in (None,{'username':'reader','role':'regular'}):
        with pytest.raises(PermissionError):jobs.run('work',user)
        with pytest.raises(PermissionError):jobs.control('work','cancel',user)
    monkeypatch.setattr(auto,'load_config',lambda:{})
    with pytest.raises(RuntimeError,match='disabled'):jobs.run('work',ADMIN)


def test_api_single_start_status_roles_and_resume(workflow):
    ids,jobs,create,provider=workflow
    # Trusted fixture resolver supplies prepared inputs; HTTP accepts IDs only.
    create('template',ids[:2]);items=[r['input'] for r in jobs.job('template').read()['items']]
    app=FastAPI()
    @app.middleware('http')
    async def identity(request,call_next):
        role=request.headers.get('x-role','admin')
        request.state.authenticated=role!='none';request.state.user={'username':'fixture','role':role}
        return await call_next(request)
    app.include_router(router)
    app.include_router(job_router(jobs,lambda selected:[r for r in items if r['id'] in selected],lambda name,user:jobs.run(name,user,max_items=1)))
    with TestClient(app) as client:
        base='/api/automatic-people/jobs'
        body={'id':'api','detection_ids':ids[:2]}
        assert client.post(base,json=body,headers={'x-role':'regular'}).status_code==403
        assert client.post(base,json=body).json()['state']=='paused'
        assert client.post(base,json=body).status_code==409
        assert client.get(base+'/api',headers={'x-role':'none'}).status_code==401
        read=client.get(base+'/api',headers={'x-role':'regular'});assert read.status_code==200
        assert 'vector' not in read.text and 'path' not in read.text
        assert client.post(base+'/api/control',json={'action':'resume'},headers={'x-role':'regular'}).status_code==403
        assert client.post(base+'/api/control',json={'action':'resume'}).json()['state']=='completed'
        groups=client.get('/api/automatic-people').json()['groups'];assert len(groups)==1 and groups[0]['name'] is None
