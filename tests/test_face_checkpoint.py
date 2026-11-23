from contextlib import contextmanager,nullcontext
import copy
import pytest
from PIL import Image
from face_detect.jobs.checkpoint import EvaluationJob
from face_detect.jobs.checkpoint import Excluded
from face_detect.jobs.checkpoint import IdentityMismatch
from face_detect.contracts import ProviderTransient
from face_detect.contracts import ProviderUnavailable

def inputs():return [dict(id=i,media_id=i,content_hash='a'*64,path='fixture.jpg',sha256='b'*64,target=[0,0,10,10]) for i in (1,2)]
@contextmanager
def load(row):yield Image.new('RGB',(10,10)),row['target']
def observe():return {'stamp':{},'evidence':{'start':'same'}}
class Provider:
    identity='fixture'
    def __init__(self):self.calls=0
    def validate(self,stamp):pass
    def embed(self,*args):self.calls+=1;return [1.,0.]

def test_idempotent_resume_pause_and_cancel(tmp_path):
    j=EvaluationJob(tmp_path/'job');j.create(inputs(),'fixture');p=Provider()
    assert j.run(p,observe,load,nullcontext,max_items=1)['state']=='paused'
    assert j.run(p,observe,load,nullcontext)['state']=='completed' and p.calls==2
    j.run(p,observe,load,nullcontext);assert p.calls==2
    j.command('cancel');assert j.run(p,observe,load,nullcontext)['state']=='cancelled'
    j.command('run');assert j.run(p,observe,load,nullcontext)['state']=='cancelled'

def test_crash_resume_and_transient(tmp_path):
    j=EvaluationJob(tmp_path/'job');j.create(inputs(),'fixture');p=Provider()
    def crash(*_):raise KeyboardInterrupt()
    p.embed=crash
    with pytest.raises(KeyboardInterrupt):j.run(p,observe,load,nullcontext)
    assert j.read()['items'][0]['state']=='running'
    def down():raise ProviderTransient()
    assert j.run(Provider(),down,load,nullcontext)['state']=='waiting-service'
    state=j.run(Provider(),observe,load,nullcontext)
    assert state['state']=='completed' and state['items'][0]['attempts']==2

def test_timeout_pause_and_cancel_inflight(tmp_path):
    j=EvaluationJob(tmp_path/'job');j.create(inputs(),'fixture');p=Provider()
    def timeout(*_):raise ProviderTransient()
    p.embed=timeout
    assert j.run(p,observe,load,nullcontext)['state']=='waiting-service'
    p.embed=lambda *_:(j.command('pause') or [1.,0.])
    assert j.run(p,observe,load,nullcontext)['state']=='paused'
    j.command('run');p.embed=lambda *_:(j.command('cancel') or [1.,0.])
    state=j.run(p,observe,load,nullcontext)
    assert state['state']=='cancelled' and 'vector' not in state['items'][1]

def test_exclusion_on_resume_and_after_request(tmp_path):
    j=EvaluationJob(tmp_path/'job');j.create(inputs(),'fixture');p=Provider()
    j.run(p,observe,load,nullcontext,max_items=1)
    @contextmanager
    def excluded(row):
        if row['id']==1:raise Excluded()
        yield Image.new('RGB',(10,10)),row['target']
        raise Excluded()
    state=j.run(p,observe,excluded,nullcontext)
    assert all(r['state']=='excluded' and 'vector' not in r for r in state['items'])

def test_duplicate_manifest_identity_and_malformed_result(tmp_path):
    j=EvaluationJob(tmp_path/'job')
    with pytest.raises(ValueError):j.create([inputs()[0]]*2,'fixture')
    j.create(inputs(),'fixture');p=Provider();p.identity='other'
    with pytest.raises(IdentityMismatch):j.run(p,observe,load,nullcontext)
    p.identity='fixture'
    def bad(*_):raise ProviderUnavailable()
    p.embed=bad
    assert j.run(p,observe,load,nullcontext)['state']=='blocked'

def test_service_changed_during_request(tmp_path):
    j=EvaluationJob(tmp_path/'job');j.create(inputs(),'fixture');calls=[]
    def changing():calls.append(1);return {'stamp':{},'evidence':len(calls)}
    state=j.run(Provider(),changing,load,nullcontext)
    assert state['state']=='blocked' and 'vector' not in state['items'][0]

def test_checkpoint_uses_provider_neutral_schema(tmp_path):
    job=EvaluationJob(tmp_path/'job')
    job.create(inputs(),'fixture')
    assert job.read()['schema']=='familyfoto-face-job-v1'
