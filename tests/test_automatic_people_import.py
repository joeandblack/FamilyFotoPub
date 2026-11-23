import json
import pytest
from contextlib import nullcontext
from types import SimpleNamespace
from test_automatic_people import automatic,pool,env,add_face
from face_detect.recognition import automatic_people as auto
from app import database
from face_detect.jobs.automatic_people_import import ImportJobs
from face_detect.jobs import automatic_people_import as batches
from face_detect.jobs.automatic_people_jobs import Jobs
from app.telemetry import write_json
from test_unexamined_photos import disposable_migrations


@pytest.fixture(autouse=True)
def fake_background_unit(monkeypatch):
    monkeypatch.setattr(batches.workers,'background_unit',lambda:nullcontext(object()))
    monkeypatch.setattr(batches.workers,'manual_lease',lambda progress:nullcontext(object()))


def test_global_recheck_uses_only_completed_imports(automatic,tmp_path,monkeypatch):
    raw=Jobs(tmp_path,None,None,None);jobs=ImportJobs(raw);user={'username':'fixture','role':'admin'}
    monkeypatch.setattr(batches.workers,'worker_lease',lambda **kwargs:nullcontext(object()))
    calls=[]
    monkeypatch.setattr(auto,'recheck_unnamed',lambda db,ids,actor:calls.append(ids) or {'matched':0})
    for number,phase in [(1,'completed'),(2,'paused'),(3,'completed')]:
        job=raw.job(f'import-{number}');job.directory.mkdir()
        write_json(job.directory/'import.json',{'state':phase,'items':[{'media_id':number,'state':'done'},{'media_id':99,'state':'excluded'}]})
        job.command('run')
    from face_detect.recognition import people
    with database.connection() as db:revision=people.revision(db)
    jobs.recheck('completed-imports',revision,user)
    assert calls==[[1,3]]
    jobs.recheck('completed-imports',revision-1,user)
    assert calls==[[1,3],[1,3]]


def test_batched_resume_and_completed_items_not_repeated(automatic,tmp_path,monkeypatch):
    calls=[]
    clock=[0]
    monkeypatch.setattr(batches.time,'monotonic',lambda:clock[0])
    class Raw(Jobs):
        def source(self,db,mid):return {'id':mid}
        def create(self,name,ids,user):
            self.job(name).directory.mkdir(parents=True)
            write_json(self.job(name).directory/'raw.json',{})
        def run(self,name,user,max_items,lease):
            calls.append(name);clock[0]+=10;return {'state':'completed','errors':[]}
    raw=Raw(tmp_path,None,None,None);jobs=ImportJobs(raw);user={'username':'fixture','role':'admin'}
    monkeypatch.setattr(batches.workers,'worker_lease',lambda **kwargs:nullcontext(object()))
    monkeypatch.setattr(batches.workers,'foreground_busy',lambda db:False)
    with pytest.raises(RuntimeError,match='older preview jobs'):jobs.recheck('legacy-preview',0,user)
    jobs.create('batch',list(range(1,463)),user)
    from face_detect.recognition import people
    with database.connection() as db:revision=people.revision(db)
    with pytest.raises(RuntimeError,match='Finish Discover people'):jobs.recheck('batch',revision,user)
    assert jobs.run('batch',user,max_items=2)['done']==2
    assert jobs.status('batch',user)['state']=='paused'
    assert jobs.status('batch',user)['run_seconds']==20
    clock[0]+=3600
    jobs.control('batch','pause',user)
    assert jobs.run('batch',user)['done']==2
    jobs.control('batch','resume',user)
    final=jobs.run('batch',user)
    assert final['state']=='completed' and final['done']==462
    assert final['run_seconds']==4620
    assert len(calls)==len(set(calls))==462
    assert jobs.run('batch',user)['done']==462 and len(calls)==462


def test_incremental_grouping_over_one_hundred_faces(automatic):
    with database.connection() as db:
        ids=[add_face(db,1000+i)[0] for i in range(105)]
        for offset in range(0,len(ids),10):
            auto.ingest(db,'batch-'+str(offset),'synthetic-auto',[
                {'detection_id':did,'vector':[1.,0.],'seed':True} for did in ids[offset:offset+10]])
        groups=auto.catalog(db)['groups']
        assert len(groups)==1 and len(groups[0]['faces'])==105


def test_receipt_deferred_by_foreground_continues_without_rescanning(automatic,tmp_path,monkeypatch):
    calls=[]
    class Raw(Jobs):
        def source(self,db,mid):return {'id':mid}
        def create(self,name,ids,user):
            self.job(name).directory.mkdir(parents=True)
            write_json(self.job(name).directory/'raw.json',{})
        def run(self,name,user,max_items,lease):
            calls.append(name)
            return {'state':'awaiting-receipt' if len(calls)==1 else 'completed'}
    jobs=ImportJobs(Raw(tmp_path,None,None,None));user={'username':'fixture','role':'admin'}
    monkeypatch.setattr(batches.workers,'worker_lease',lambda **kwargs:nullcontext(object()))
    monkeypatch.setattr(batches.workers,'foreground_busy',lambda db:False)
    monkeypatch.setattr(batches.time,'sleep',lambda seconds:None)
    jobs.create('batch',[1],user)
    assert jobs.run('batch',user)['state']=='completed'
    assert calls==['batch-photo-1','batch-photo-1']


def test_foreground_wait_excluded_from_runtime_and_auto_resume(automatic,tmp_path,monkeypatch):
    clock=[0];attempts=[None,None,object()];calls=[]
    class Raw(Jobs):
        def source(self,db,mid):return {'id':mid}
        def create(self,name,ids,user):
            self.job(name).directory.mkdir(parents=True)
            write_json(self.job(name).directory/'raw.json',{})
        def run(self,name,user,max_items,lease):
            calls.append(name);clock[0]+=5;return {'state':'completed'}
    jobs=ImportJobs(Raw(tmp_path,None,None,None));user={'username':'fixture','role':'admin'}
    monkeypatch.setattr(batches.workers,'worker_lease',lambda **kwargs:nullcontext(object()))
    monkeypatch.setattr(batches.workers,'background_unit',lambda:nullcontext(attempts.pop(0)))
    monkeypatch.setattr(batches.time,'monotonic',lambda:clock[0])
    def wait(seconds):
        assert jobs.status('batch',user)['state']=='waiting-for-foreground'
        clock[0]+=60
    monkeypatch.setattr(batches.time,'sleep',wait)
    jobs.create('batch',[1],user)
    result=jobs.run('batch',user)
    assert result['state']=='completed' and result['run_seconds']==5
    assert calls==['batch-photo-1']
