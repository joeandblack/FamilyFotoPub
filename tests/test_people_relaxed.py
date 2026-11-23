import math
import pytest
from test_automatic_people import automatic,pool,env,add_face
from test_unexamined_photos import disposable_migrations
from app import database
from face_detect.recognition import automatic_people as auto
from face_detect.recognition import people_relaxed as relaxed


def test_lower_threshold_still_needs_two_events_and_quality():
    anchors=[{'id':i,'person_id':1,'vector':[1.,0.],'space_id':'test','event_id':str(i),'photo_id':i,'content_id':str(i),'seed':True} for i in (1,2)]
    query={'id':3,'vector':[.35,math.sqrt(1-.35**2)],'space_id':'test','event_id':'3','photo_id':3,'content_id':'3','seed':True}
    assert len(relaxed.propose(anchors,[query]))==1
    assert relaxed.propose(anchors[:1],[query])==[]
    assert relaxed.propose(anchors,[query|{'seed':False}])==[]
    assert relaxed.propose(anchors,[query|{'vector':[.29,math.sqrt(1-.29**2)]}])==[]


def seed(db):
    ids=[add_face(db,900+i)[0] for i in range(3)]
    auto.ingest(db,'relaxed-fixture','synthetic-auto',[
        {'detection_id':did,'seed':True,'vector':[1.,0.] if n<2 else [.35,math.sqrt(1-.35**2)]}
        for n,did in enumerate(ids)])
    group=next(g for g in auto.catalog(db)['groups'] if any(f['detection_id']==ids[0] for f in g['faces']))
    pid=auto.name_group(db,group['id'],'Reference','fixture')['person_id']
    mid=auto.rows(db,[ids[-1]])[0]['media_id']
    return ids,pid,mid


def test_relaxed_is_persisted_review_only_and_confirm_is_explicit(automatic):
    with database.connection() as db:
        ids,pid,mid=seed(db)
        assert auto.recheck_unnamed(db,[mid],'fixture')['matched']==0
        before=auto.catalog(db)['groups']
        result=relaxed.generate(db,[mid],'fixture')
        assert result['suggestions']==1
        assert auto.catalog(db)['groups']==before
    with database.connection() as db:
        view=relaxed.collection(db)
        assert view['total']==1 and view['items'][0]['score']==pytest.approx(.35)
        relaxed.decide(db,view['run_id'],ids[-1],pid,'confirm','fixture')
        assert relaxed.collection(db)['total']==0
        assert db.execute('SELECT person_id FROM people_assignments WHERE detection_id=%s',(ids[-1],)).fetchone()['person_id']==pid


def test_rejection_persists_and_stale_run_cannot_confirm(automatic):
    with database.connection() as db:
        ids,pid,mid=seed(db)
        first=relaxed.generate(db,[mid],'fixture')
        second=relaxed.generate(db,[mid],'fixture')
        with pytest.raises(RuntimeError):relaxed.decide(db,first['suggestion_run'],ids[-1],pid,'confirm','fixture')
        relaxed.decide(db,second['suggestion_run'],ids[-1],pid,'reject','fixture')
        assert relaxed.generate(db,[mid],'fixture')['suggestions']==0
        assert not db.execute('SELECT 1 FROM people_assignments WHERE detection_id=%s',(ids[-1],)).fetchone()


@pytest.mark.parametrize('exclusion',['ignored','constraint','same-photo','same-content','scope'])
def test_relaxed_preserves_exclusions(automatic,exclusion):
    with database.connection() as db:
        ids,pid,mid=seed(db)
        if exclusion=='ignored':auto.ignore(db,ids[-1],True,'fixture')
        if exclusion=='constraint':db.execute('INSERT INTO people_constraints(a,b) VALUES(%s,%s)',tuple(sorted([ids[0],ids[-1]])))
        if exclusion=='same-photo':
            aid=db.execute('SELECT analysis_id FROM face_detections WHERE id=%s',(ids[0],)).fetchone()['analysis_id']
            db.execute('UPDATE face_detections SET analysis_id=%s WHERE id=%s',(aid,ids[-1]))
        if exclusion=='same-content':
            # Eligibility also checks peers outside the model-specific anchor dates.
            ctx=relaxed.context(db);ctx[0][ids[-1]]['content_hash']=ctx[0][ids[0]]['content_hash']
            assert not relaxed.eligible({'detection_id':ids[-1],'person_id':pid},ctx);return
        assert relaxed.generate(db,[] if exclusion=='scope' else [mid],'fixture')['suggestions']==0


@pytest.mark.parametrize('decision',['confirm','reject'])
def test_decision_endpoint_requires_admin_but_tolerates_scan_updates(automatic,decision):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from face_detect.api.automatic_people_api import router
    from face_detect.api.automatic_people_api import reader
    from face_detect.api.automatic_people_api import administrator
    from fastapi import HTTPException
    with database.connection() as db:
        ids,pid,mid=seed(db);relaxed.generate(db,[mid],'fixture');view=relaxed.collection(db)
        did,_=add_face(db,9999)
        auto.ingest(db,'unrelated-scan','synthetic-auto',[{'detection_id':did,'seed':True,'vector':[-1.,0.]}])
        assert auto.catalog(db)['revision']>view['revision']
    app=FastAPI();app.include_router(router)
    app.dependency_overrides[reader]=lambda:{'username':'fixture','role':'admin'}
    app.dependency_overrides[administrator]=lambda:{'username':'fixture','role':'admin'}
    with TestClient(app) as client:
        body={'revision':view['revision'],'run_id':view['run_id'],'detection_id':ids[-1],'person_id':pid,'decision':decision}
        assert client.post('/api/automatic-people/suggestions/decision',json=body).status_code==200
        assert client.post('/api/automatic-people/suggestions/decision',json=body).status_code==409
        def forbidden():raise HTTPException(403,'Administrator required')
        app.dependency_overrides[administrator]=forbidden
        body['revision']=view['revision']
        assert client.post('/api/automatic-people/suggestions/decision',json=body).status_code==403


@pytest.mark.parametrize('change',['new-run','ignored','assigned'])
def test_suggestion_api_rejects_actual_conflicts(automatic,change):
    from face_detect.api.automatic_people_api import SuggestionDecision
    from face_detect.api.automatic_people_api import suggestion_decision
    from fastapi import HTTPException
    with database.connection() as db:
        ids,pid,mid=seed(db);relaxed.generate(db,[mid],'fixture');view=relaxed.collection(db)
        if change=='new-run':relaxed.generate(db,[mid],'fixture')
        elif change=='ignored':auto.ignore(db,ids[-1],True,'fixture')
        else:auto.correct(db,ids[-1],pid,'fixture')
    data=SuggestionDecision(revision=view['revision'],run_id=view['run_id'],detection_id=ids[-1],person_id=pid,decision='reject')
    with pytest.raises(HTTPException) as error:suggestion_decision(data,{'username':'fixture'})
    assert error.value.status_code==409
