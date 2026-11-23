import pytest
from fastapi import HTTPException
from test_automatic_people import automatic, pool, env, add_face
from test_unexamined_photos import disposable_migrations
from test_people_review_sections import review_match_fixture
from face_detect.recognition import automatic_people as auto
from face_detect.api import automatic_people_api as api
from app import database


@pytest.mark.parametrize('action',['match','ignore','set-aside'])
def test_review_survives_unrelated_scan_receipts(automatic,action):
    with database.connection() as db:
        ids,pid=review_match_fixture(db,automatic)
        snapshot=auto.catalog(db)
        versions={str(g['id']):g['review_version'] for g in snapshot['groups'] if g['id'] in ids}
        did,_=add_face(db,10001)
        auto.ingest(db,'unrelated','synthetic-auto',[{'detection_id':did,'vector':[-1.,0.],'seed':True}])
        assert auto.catalog(db)['revision']>snapshot['revision']
    payload={'revision':snapshot['revision'],'group_ids':ids,'group_versions':versions}
    user={'username':'test-reviewer'}
    if action=='match': result=api.match_review(api.MatchReview(**payload,person_id=pid),user)
    elif action=='ignore': result=api.ignore_review(api.SetAside(**payload),user)
    else: result=api.set_aside(api.SetAside(**payload),user)
    assert result.get('groups',result.get('moved'))==len(ids)
    with database.connection() as db:
        selected_faces={f['detection_id'] for g in snapshot['groups'] if g['id'] in ids for f in g['faces']}
        later,_=add_face(db,10003)
        auto.ingest(db,'after-review','synthetic-auto',[{'detection_id':later,'vector':[-1.,0.],'seed':True}])
        after=auto.catalog(db)['groups']
        if action=='match':
            assert selected_faces <= {f['detection_id'] for g in after if g['person_id']==pid for f in g['faces']}
        elif action=='ignore':
            assert not selected_faces & {f['detection_id'] for g in after for f in g['faces']}
        else:
            assert all(g['section']=='other' for g in after if g['id'] in ids)


def test_changed_selected_group_and_legacy_stale_revision_rejected(automatic):
    with database.connection() as db:
        ids,_=review_match_fixture(db,automatic)
        snapshot=auto.catalog(db)
        versions={str(g['id']):g['review_version'] for g in snapshot['groups'] if g['id'] in ids}
        did,_=add_face(db,10002)
        auto.ingest(db,'selected-growth','synthetic-auto',[{'detection_id':did,'vector':[1.,0.],'seed':True}])
        before=auto.catalog(db)
    for expected,version in [('Selected groups changed',versions),('People changed',None)]:
        with pytest.raises(HTTPException) as error:
            api.ignore_review(api.SetAside(revision=snapshot['revision'],group_ids=ids,group_versions=version),{'username':'test'})
        assert error.value.status_code==409 and expected in error.value.detail
    with database.connection() as db:assert auto.catalog(db)==before


def test_review_snapshot_cannot_omit_selected_groups(automatic):
    with database.connection() as db:
        ids,_=review_match_fixture(db,automatic)
        view=auto.catalog(db)
    with pytest.raises(HTTPException) as error:
        api.ignore_review(api.SetAside(revision=view['revision'],group_ids=ids,group_versions={}),{'username':'test'})
    assert error.value.status_code==422


@pytest.mark.parametrize('section,action', [('review','match'),('review','ignore'),('review','set-aside'),('other','match'),('other','ignore')])
def test_bulk_review_applies_viewed_faces_while_selected_group_grows(automatic,section,action):
    with database.connection() as db:
        ids,pid=review_match_fixture(db,automatic)
        if section=='other':auto.set_aside_groups(db,ids,'test')
        view=auto.catalog(db)
        selected=[g for g in view['groups'] if g['id'] in ids]
        versions={str(g['id']):g['review_version'] for g in selected}
        faces={str(g['id']):[f['detection_id'] for f in g['faces']] for g in selected}
        later,_=add_face(db,10002)
        auto.ingest(db,'growth','synthetic-auto',[{'detection_id':later,'vector':[1.,0.],'seed':True}])
        grown=auto.catalog(db)
        assert any(g['id'] in ids and later in [f['detection_id'] for f in g['faces']] for g in grown['groups'])
    payload={'revision':view['revision'],'group_ids':ids,'group_versions':versions,'group_faces':faces}
    user={'username':'test'}
    if action=='match':
        result=(api.match_review if section=='review' else api.match_other)(api.MatchReview(**payload,person_id=pid),user)
    elif action=='ignore':
        result=(api.ignore_review if section=='review' else api.ignore_other)(api.SetAside(**payload),user)
    else:result=api.set_aside(api.SetAside(**payload),user)
    assert result.get('groups',result.get('moved'))==len(ids)
    original={did for members in faces.values() for did in members}
    with database.connection() as db:
        after=auto.catalog(db)['groups']
        remaining=next(g for g in after if later in [f['detection_id'] for f in g['faces']])
        assert remaining['person_id'] is None and remaining['section']=='review'
        if action=='match':
            matched={f['detection_id'] for g in after if g['person_id']==pid for f in g['faces']}
            assert original<=matched and later not in matched
        elif action=='ignore':
            ignored={r['id'] for r in db.execute('SELECT id FROM face_detections WHERE ignored').fetchall()}
            assert original<=ignored and later not in ignored
        else:
            deferred={f['detection_id'] for g in after if g['section']=='other' for f in g['faces']}
            assert original<=deferred and later not in deferred


@pytest.mark.parametrize('change',['ignore','move','set-aside','missing-snapshot','duplicate-face','extra-face','missing-face'])
def test_bulk_snapshot_rejects_conflicts_and_invalid_membership(automatic,change):
    with database.connection() as db:
        ids,pid=review_match_fixture(db,automatic)
        view=auto.catalog(db)
        selected=[g for g in view['groups'] if g['id'] in ids]
        versions={str(g['id']):g['review_version'] for g in selected}
        faces={str(g['id']):[f['detection_id'] for f in g['faces']] for g in selected}
        key=str(ids[0]);did=faces[key][0]
        if change=='ignore':auto.ignore(db,did,True,'someone-else')
        elif change=='move':auto.correct(db,did,pid,'someone-else')
        elif change=='set-aside':auto.set_aside_groups(db,ids,'someone-else')
        elif change=='missing-snapshot':del faces[key]
        elif change=='duplicate-face':faces[key].append(did)
        elif change=='extra-face':faces[key].append(99999999)
        elif change=='missing-face':faces[key].remove(did)
        before=auto.catalog(db)
    with pytest.raises(HTTPException) as error:
        api.ignore_review(api.SetAside(revision=view['revision'],group_ids=ids,group_versions=versions,group_faces=faces),{'username':'test'})
    assert error.value.status_code in (409,422)
    with database.connection() as db:assert auto.catalog(db)==before


@pytest.mark.parametrize('action',['match','ignore','set-aside'])
def test_stream_completion_follows_committed_bulk_changes(automatic,action):
    import asyncio
    import json
    with database.connection() as db:
        ids,pid=review_match_fixture(db,automatic)
        view=auto.catalog(db)
    selected=[g for g in view['groups'] if g['id'] in ids]
    faces={str(g['id']):[f['detection_id'] for f in g['faces']] for g in selected}
    data=api.BulkReview(revision=view['revision'],group_ids=ids,
        group_versions={str(g['id']):g['review_version'] for g in selected},group_faces=faces,
        section='review',action=action,person_id=pid if action=='match' else None)
    async def consume():
        response=await api.bulk_review(data,{'username':'test'})
        messages=[json.loads(line) async for line in response.body_iterator]
        assert messages[-1]['state']=='completed'
        assert any(m.get('phase')=='processing' and m.get('total')==sum(map(len,faces.values())) for m in messages)
        assert any(m.get('phase')=='saving' for m in messages)
        # A separate connection sees the committed result when completion is emitted.
        with database.connection() as db:
            after=auto.catalog(db)['groups']
            selected_ids={did for members in faces.values() for did in members}
            if action=='match':assert selected_ids<={f['detection_id'] for g in after if g['person_id']==pid for f in g['faces']}
            elif action=='ignore':assert not selected_ids&{f['detection_id'] for g in after for f in g['faces']}
            else:assert all(g['section']=='other' for g in after if g['id'] in ids)
    asyncio.run(consume())


@pytest.mark.parametrize('change',['move','ignore','set-aside','target-deleted'])
def test_stream_reconciles_stale_groups_without_blocking_other_selections(automatic,change):
    import asyncio
    import json
    with database.connection() as db:
        ids,pid=review_match_fixture(db,automatic)
        view=auto.catalog(db)
        selected=[g for g in view['groups'] if g['id'] in ids]
        faces={str(g['id']):[f['detection_id'] for f in g['faces']] for g in selected}
        changed_id=ids[0];did=faces[str(changed_id)][0]
        if change=='move':auto.correct(db,did,pid,'other-tab')
        elif change=='ignore':auto.ignore(db,did,True,'other-tab')
        elif change=='set-aside':auto.set_aside_groups(db,[changed_id],'other-tab')
        else:db.execute('UPDATE people SET deleted_at=now() WHERE id=%s',(pid,))
        before=auto.catalog(db)
    data=api.BulkReview(revision=view['revision'],group_ids=ids,
        group_versions={str(g['id']):g['review_version'] for g in selected},group_faces=faces,
        section='review',action='match',person_id=pid)
    async def consume():
        response=await api.bulk_review(data,{'username':'test'})
        return [json.loads(line) async for line in response.body_iterator]
    messages=asyncio.run(consume());final=messages[-1]
    assert final['state']=='completed'
    result=final['result'];assert 'updates' in result
    assert result['remaining_group_ids']==(ids if change=='target-deleted' else [changed_id])
    with database.connection() as db:
        after=auto.catalog(db)
        if change=='target-deleted':assert after==before
        else:
            safe={did for gid in ids[1:] for did in faces[str(gid)]}
            matched={f['detection_id'] for g in after['groups'] if g['person_id']==pid for f in g['faces']}
            assert safe<=matched
            assert result['matched']==len(safe)
            if change=='ignore':assert db.execute('SELECT ignored FROM face_detections WHERE id=%s',(did,)).fetchone()['ignored']
        live={g['id']:g for g in after['groups']}
        for group in result['updates']['groups']:assert live[group['id']]==group
        assert all(gid not in live for gid in result['updates']['removed_group_ids'])


def test_repeated_stream_does_not_repeat_saved_matches(automatic):
    import asyncio
    import json
    with database.connection() as db:
        ids,pid=review_match_fixture(db,automatic);view=auto.catalog(db)
    selected=[g for g in view['groups'] if g['id'] in ids]
    data=api.BulkReview(revision=view['revision'],group_ids=ids,
        group_versions={str(g['id']):g['review_version'] for g in selected},
        group_faces={str(g['id']):[f['detection_id'] for f in g['faces']] for g in selected},
        section='review',action='match',person_id=pid)
    async def run():
        response=await api.bulk_review(data,{'username':'test'})
        return [json.loads(line) async for line in response.body_iterator][-1]
    assert asyncio.run(run())['state']=='completed'
    with database.connection() as db:before=auto.catalog(db)
    assert asyncio.run(run())['state']=='completed'
    with database.connection() as db:assert auto.catalog(db)==before
