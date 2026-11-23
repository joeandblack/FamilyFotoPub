import copy
import hashlib
import json
from contextlib import contextmanager,nullcontext
from pathlib import Path
import pytest
from PIL import Image
from psycopg.types.json import Jsonb
from test_automatic_people import automatic,pool,env
from app import database
from face_detect.recognition import automatic_people as auto
from face_detect.jobs.automatic_people_handoff import accept_checkpoint
from face_detect.testing.provider import FixtureProvider
from face_detect.contracts import ProviderUnavailable
from face_detect.jobs.checkpoint import EvaluationJob

ADMIN={'role':'admin','username':'fixture-admin'}

@pytest.fixture
def completed(automatic,tmp_path):
    provider=FixtureProvider()
    evidence=provider.observe();observer=lambda:copy.deepcopy(evidence)
    with database.connection() as db:
        # The fixture provider's space is the library's active one for these receipts.
        db.execute("UPDATE space SET state='building' WHERE state='active'")
        db.execute("INSERT INTO space(space_id,manifest,state) VALUES(%s,%s,'active')",(provider.space_id,Jsonb(provider.manifest)))
        metas=auto.rows(db,automatic)
    @contextmanager
    def load(row):yield Image.new('RGB',(100,100)),row['target']
    def job_for(ids,name):
        records=[{'id':r['id'],'media_id':r['media_id'],'content_hash':r['content_hash'],'path':'synthetic-private-crop.jpg','sha256':'a'*64,'target':[0,0,100,100]} for r in metas if r['id'] in ids]
        job=EvaluationJob(tmp_path/name);job.create(records,provider.identity)
        return job
    job=job_for(automatic[:2],'first')
    assert job.run(provider,observer,load,nullcontext,max_items=1)['state']=='paused'
    saved=provider._transport
    def crash(_):raise KeyboardInterrupt()
    provider._transport=crash
    with pytest.raises(KeyboardInterrupt):job.run(provider,observer,load,nullcontext)
    provider._transport=saved
    state=job.run(provider,observer,load,nullcontext);assert state['state']=='completed'
    return automatic,state,observer,provider,load,job_for

def test_checkpoint_receipt_naming_later_attach_and_manual_override(completed):
    ids,state,observer,provider,load,job_for=completed
    with database.connection() as db:
        first=accept_checkpoint(db,state,ADMIN,observer,provider=provider)
        assert first['accepted']==2
        assert accept_checkpoint(db,state,ADMIN,observer,provider=provider)==first
        assert db.execute('SELECT count(*) n FROM auto_people_receipts').fetchone()['n']==1
        group=auto.catalog(db)['groups'][0];pid=auto.name_group(db,group['id'],'Fixture A',ADMIN['username'])['person_id']
    later=job_for(ids[2:3],'later').run(provider,observer,load,nullcontext)
    with database.connection() as db:
        assert accept_checkpoint(db,later,ADMIN,observer,provider=provider)['known_attachments']==1
        other=db.execute("INSERT INTO people(name) VALUES('Fixture B') RETURNING id").fetchone()['id']
        auto.correct(db,ids[2],other,ADMIN['username'])
        accept_checkpoint(db,later,ADMIN,observer,provider=provider)
        assert db.execute('SELECT person_id FROM people_assignments WHERE detection_id=%s',(ids[2],)).fetchone()['person_id']==other
        assert db.execute('SELECT 1 FROM auto_people_blocks WHERE detection_id=%s AND person_id=%s',(ids[2],pid)).fetchone()
        provenance=db.execute("SELECT before_state FROM auto_people_events WHERE action='receipt' ORDER BY id LIMIT 1").fetchone()['before_state']['provenance']
        assert len(provenance['items'])==2 and provenance['items'][0]['service']['evidence']['manifest']==provider.manifest
    constrained=job_for(ids[3:4],'constrained').run(provider,observer,load,nullcontext)
    with database.connection() as db:
        a,b=sorted((ids[0],ids[3]));db.execute('INSERT INTO people_constraints VALUES(%s,%s)',(a,b))
        assert accept_checkpoint(db,constrained,ADMIN,observer,provider=provider)['known_attachments']==0
        assert db.execute('SELECT group_id FROM auto_people_members WHERE detection_id=%s',(ids[3],)).fetchone()['group_id']!=group['id']

def test_transaction_crash_replay_and_changed_payload(completed,monkeypatch):
    _,state,observer,provider,_,_=completed;real=auto.ingest
    def crash(*a,**kw):real(*a,**kw);raise RuntimeError('crash before commit')
    monkeypatch.setattr(auto,'ingest',crash)
    with pytest.raises(RuntimeError):
        with database.connection() as db:accept_checkpoint(db,state,ADMIN,observer,provider=provider)
    monkeypatch.setattr(auto,'ingest',real)
    with database.connection() as db:
        assert not db.execute('SELECT 1 FROM auto_people_receipts').fetchone()
        accept_checkpoint(db,state,ADMIN,observer,provider=provider)
        altered=copy.deepcopy(state);v=[0.,1.]+[0.]*510;altered['items'][0]['vector']=v;altered['items'][0]['result']['vector']=v
        with pytest.raises(ValueError,match='payload changed'):accept_checkpoint(db,altered,ADMIN,observer,provider=provider)

def test_ignored_and_trash_excluded_at_handoff(completed):
    ids,state,observer,provider,_,_=completed
    with database.connection() as db:
        db.execute('UPDATE face_detections SET ignored=true WHERE id=%s',(ids[0],))
        mid=auto.rows(db,[ids[1]])[0]['media_id'];db.execute('INSERT INTO media_trash(media_id) VALUES(%s)',(mid,))
        result=accept_checkpoint(db,state,ADMIN,observer,provider=provider)
        assert result['accepted']==0 and result['excluded']==2
        assert not db.execute('SELECT 1 FROM auto_people_members').fetchone()

@pytest.mark.parametrize('bad',['role','anonymous','paused','version','manifest','service','live-service','quality','bounds','small-overflow'])
def test_rejects_unauthorized_and_conflicting_checkpoint(completed,bad):
    _,state,observer,provider,_,_=completed;state=copy.deepcopy(state);user=ADMIN
    if bad=='role':user={'role':'regular','username':'fixture'}
    if bad=='anonymous':user=None
    if bad=='paused':state['state']='paused'
    if bad=='version':state['model_id']='wrong'
    if bad=='manifest':state['items'][0]['input']['media_id']+=1
    if bad=='service':state['items'][0]['service_evidence']['stamp']['model_id']='wrong'
    if bad=='live-service':
        invalid=observer();invalid['evidence']['manifest']['provider']='wrong-provider'
        observer=lambda:invalid
    if bad=='quality':state['items'][0].pop('result')
    if bad=='bounds':state['items'][0]['result']['box']['x_max']=999
    if bad=='small-overflow':state['items'][0]['result']['box']['x_max']=100.0001
    with database.connection() as db:
        with pytest.raises((ValueError,PermissionError,ProviderUnavailable)):accept_checkpoint(db,state,user,observer,provider=provider)
        assert not db.execute('SELECT 1 FROM auto_people_receipts').fetchone()


def test_checkpoint_edge_roundoff_is_replayable(completed):
    _,state,observer,provider,_,_=completed
    state['items'][0]['result']['box']['x_max']=100.00000000000001
    with database.connection() as db:
        result=accept_checkpoint(db,state,ADMIN,observer,provider=provider)
        assert result['accepted']==2
        assert accept_checkpoint(db,state,ADMIN,observer,provider=provider)==result


def test_a_checkpoint_from_an_unchecked_backend_of_the_same_space_is_refused(completed):
    """Decision 5 on the handoff path: another backend's vectors need its own passed check."""
    ids,state,observer,provider,load,job_for=completed
    with database.connection() as db:
        accept_checkpoint(db,state,ADMIN,observer,provider=provider)
    later=copy.deepcopy(job_for(ids[2:3],'other-backend').run(provider,observer,load,nullcontext))
    # The same model on another device: same space, another backend.
    for item in later['items']:
        item['service_evidence']['evidence']['manifest']['device']='coreml'
    from face_detect.jobs import automatic_people_handoff as handoff
    original=handoff.evidence_space
    handoff.evidence_space=lambda record:provider.space_id
    try:
        with database.connection() as db,pytest.raises(ValueError,match='not checked against this library'):
            accept_checkpoint(db,later,ADMIN,observer,provider=provider)
    finally:
        handoff.evidence_space=original
