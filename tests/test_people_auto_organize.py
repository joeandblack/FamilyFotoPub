import pytest
from face_detect.recognition import people_auto_organize as organize
from app import database
from face_detect.recognition import automatic_people as auto
from test_automatic_people import automatic, pool, env, setup_groups, add_face


def face(did,vector,person=None,group=None,photo=None,query=True):
    return {'id':did,'vector':vector,'person_id':person,'group':group or did,
            'photo':photo or did,'content':str(photo or did),'seed':False,
            'trusted':person is not None,'query':query and person is None}


def data(rows,blocks=(),constraints=()):
    return {'models':{'fixture':rows},'blocks':set(blocks),'constraints':set(map(frozenset,constraints)),
            'revision':1,'members':{}}


def test_named_matches_allow_nonseed_and_same_day_with_independent_photos():
    rows=[face(1,[1,0],7),face(2,[.99,.1],7),face(3,[0,1],8),face(4,[.1,.99],8),face(5,[1,0])]
    result=organize.plan(data(rows))
    assert result['moves'][5]['person_id']==7
    assert result['summary']['matched_faces']==1
    assert 5 not in organize.plan(data(rows,blocks=[(5,7)]))['moves']


def test_same_photo_and_ambiguous_people_are_not_automatched():
    rows=[face(1,[1,0],7),face(2,[1,0],7),face(3,[1,0],8),face(4,[1,0],8),face(5,[1,0])]
    assert not organize.plan(data(rows))['moves']
    rows=[face(1,[1,0],7),face(2,[1,0],7),face(5,[1,0],photo=1)]
    assert not organize.plan(data(rows))['moves']


def test_existing_unnamed_groups_merge_but_constraints_survive():
    rows=[face(1,[1,0]),face(2,[.99,.1]),face(3,[0,1])]
    result=organize.plan(data(rows))
    assert result['moves'][2]['group_id']==1
    assert result['summary']['groups_after']==2
    assert not organize.plan(data(rows,constraints=[(1,2)]))['moves']


def test_apply_undo_preserves_manual_edits_and_is_atomic(automatic):
    with database.connection() as db:
        a,b,pa,pb=setup_groups(db,automatic)
        auto.event(db,'review-sections-baseline','manual','test',{'legacy':{}})
        did,mid=add_face(db,50)
        auto.ingest(db,'extra','synthetic-auto',[{'detection_id':did,'vector':[1.,0.],'seed':False}])
        snap=organize.snapshot(db)
        proposal=organize.plan(snap)
        assert proposal['moves'][did]['person_id']==pa
        old=dict(snap['members'][did])
        result=organize.apply(db,snap,proposal,'test')
        assert db.execute('SELECT group_id FROM auto_people_members WHERE detection_id=%s',(did,)).fetchone()['group_id']==a
        restored=organize.undo(db,result['event_id'],'test')
        assert restored=={'restored':1,'kept_later_changes':0}
        assert db.execute('SELECT * FROM auto_people_members WHERE detection_id=%s',(did,)).fetchone()==old
        with pytest.raises(ValueError):organize.undo(db,result['event_id'],'test')
        snap=organize.snapshot(db);proposal=organize.plan(snap)
        auto.event(db,'test-change','manual','test',{})
        with pytest.raises(RuntimeError,match='no changes'):organize.apply(db,snap,proposal,'test')
        snap=organize.snapshot(db);proposal=organize.plan(snap)
        result=organize.apply(db,snap,proposal,'test')
        auto.correct(db,did,pb,'test')
        assert organize.undo(db,result['event_id'],'test')['kept_later_changes']==1
        assert db.execute('SELECT group_id FROM auto_people_members WHERE detection_id=%s',(did,)).fetchone()['group_id']==b


def test_organize_stream_permissions_progress_and_last_run(automatic,monkeypatch):
    import json
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from face_detect.api.automatic_people_api import router
    app=FastAPI()
    @app.middleware('http')
    async def identity(request,next):
        request.state.authenticated=True
        request.state.user={'username':'test','role':request.headers.get('x-role','admin')}
        return await next(request)
    app.include_router(router)
    def run(actor,progress,import_id=None,connect=None):
        from face_detect.api.automatic_people_api import connection
        assert connect is connection
        progress('matching',1,2)
        return {'matched_faces':1,'groups_before':2,'groups_after':1,'event_id':None}
    monkeypatch.setattr(organize,'run',run)
    with TestClient(app) as client:
        assert client.get('/api/automatic-people/auto-organize').json()=={'last_run':None}
        assert client.post('/api/automatic-people/auto-organize/stream',json={'action':'run'},headers={'x-role':'regular'}).status_code==403
        assert client.post('/api/automatic-people/auto-organize/stream',json={'action':'undo'}).status_code==422
        response=client.post('/api/automatic-people/auto-organize/stream',json={'action':'run'})
        messages=[json.loads(line) for line in response.text.splitlines()]
        assert any(m.get('phase')=='matching' for m in messages)
        assert messages[-1]['state']=='completed'


def test_native_manual_trial_snapshot_and_suggestions(automatic):
    from psycopg.types.json import Jsonb
    from app.config import ROOT
    from face_detect.recognition import policy, people_relaxed
    with database.connection() as db:
        a,b,pa,pb=setup_groups(db,automatic)
        auto.event(db,'review-sections-baseline','manual','test',{'legacy':{}})
        did,mid=add_face(db,50)
        auto.ingest(db,'native-trial-extra','synthetic-auto',[
            {'detection_id':did,'vector':[1.,0.],'seed':False}])
        db.execute('UPDATE auto_people_observations SET seed=true WHERE detection_id=%s',(did,))
        db.execute("UPDATE space SET manifest=manifest || %s WHERE space_id='synthetic-auto'",
                   (Jsonb({'schema':'familyfoto-native-model-v1','matching':{'calibrated':False}}),))
        db.execute((ROOT/'face_detect/storage/matching_policy.sql').read_text())
        trial={'calibrated':False,'manual_trial':{'enabled':True,'threshold':.65,'margin':.2,'suggestion_threshold':.5}}
        db.execute("INSERT INTO policy VALUES('fixture-policy','synthetic-auto',%s,'{}',now())",(Jsonb(trial),))
        before=db.execute('SELECT * FROM auto_people_members ORDER BY detection_id').fetchall()
        assert not policy.matching_policy(db,'synthetic-auto')['calibrated']
        snap=organize.snapshot(db)
        assert 'synthetic-auto' in snap['models']
        assert snap['native_policies']['synthetic-auto']['manual_enabled']
        result=people_relaxed.generate(db,[mid],'test')
        assert result['suggestions']>=1
        assert db.execute('SELECT * FROM auto_people_members ORDER BY detection_id').fetchall()==before
        assert organize.plan(snap)['moves'][did]['person_id']==pa
