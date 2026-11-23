from test_automatic_people import automatic, pool, env, observations, setup_groups
from test_automatic_people import test_targeted_undo_and_stale_undo
from test_unexamined_photos import disposable_migrations
from face_detect.recognition import automatic_people as auto
from app import database
import pytest


def test_selected_move_scopes_validation_and_reuses_group(automatic,monkeypatch):
    from face_detect.api.automatic_people_api import SelectedFaces
    from face_detect.api.automatic_people_api import selected_faces
    with database.connection() as db:
        setup_groups(db,automatic)
        view=auto.catalog(db)
        source=next(g for g in view['groups'] if len(g['faces'])==2)
        target=next(g for g in view['groups'] if g['id']!=source['id'])
    real=auto.catalog;calls=[]
    def scoped(db,gid=None,review_faces=None,import_id=None):
        assert gid==source['id'], 'Selected move must not load the full catalog'
        calls.append(gid)
        return real(db,gid,review_faces,import_id=import_id)
    monkeypatch.setattr(auto,'catalog',scoped)
    result=selected_faces(source['id'],SelectedFaces(revision=view['revision'],
        group_versions={str(source['id']):source['review_version']},
        detection_ids=[f['detection_id'] for f in source['faces']],action='move',person_id=target['person_id']),{'username':'test'})
    assert result=={'changed':2}
    assert calls==[source['id']]


def test_batch_move_constant_queries_and_undo_compatibility(automatic):
    class CountQueries:
        def __init__(self,db):self.db=db;self.count=0
        def execute(self,*args,**kwargs):
            self.count+=1
            return self.db.execute(*args,**kwargs)
        def transaction(self):return self.db.transaction()
    class Rollback(Exception):pass
    with database.connection() as db:
        source_id,target_id,source_person,target_person=setup_groups(db,automatic)
        source=auto.detail(db,source_id)['group']
        ids=sorted(f['detection_id'] for f in source['faces'])
        counts=[]
        for chosen in [ids[:1],ids]:
            counted=CountQueries(db)
            with pytest.raises(Rollback):
                with db.transaction():
                    auto.change_selected_faces(counted,source_id,chosen,'move',target_person,'test',validated_group=source)
                    counts.append(counted.count)
                    raise Rollback()
        assert counts[0]==counts[1]
        sequential=CountQueries(db)
        with pytest.raises(Rollback):
            with db.transaction():
                for did in ids:auto.correct(sequential,did,target_person,'test')
                raise Rollback()
        assert counts[-1]<sequential.count
        print(f'Face move queries: sequential={sequential.count}, batch={counts[-1]} for {len(ids)} faces')
        auto.change_selected_faces(db,source_id,ids,'move',target_person,'test',validated_group=source)
        events=db.execute("SELECT * FROM auto_people_events WHERE action='correct' ORDER BY id DESC LIMIT 2").fetchall()
        assert [e['before_state']['member']['detection_id'] for e in events]==list(reversed(ids))
        assert events[0]['after_revision']==events[1]['after_revision']+1
        assert auto.correction_is_current(db,events[0])
        auto.undo_correction(db,events[0]['id'],'test')
        assert db.execute('SELECT group_id FROM auto_people_members WHERE detection_id=%s',(ids[-1],)).fetchone()['group_id']==source_id
        assert db.execute('SELECT group_id FROM auto_people_members WHERE detection_id=%s',(ids[0],)).fetchone()['group_id']==target_id


def test_batch_move_rejects_constraints_within_selection(automatic):
    with database.connection() as db:
        source_id,_,_,target_person=setup_groups(db,automatic)
        source=auto.detail(db,source_id)['group']
        ids=sorted(f['detection_id'] for f in source['faces'])
        db.execute('INSERT INTO people_constraints(a,b) VALUES(%s,%s)',tuple(ids))
        with pytest.raises(ValueError,match='separation constraint'):
            auto.change_selected_faces(db,source_id,ids,'move',target_person,'test')
        assert {f['detection_id'] for f in auto.detail(db,source_id)['group']['faces']}==set(ids)


@pytest.mark.parametrize('named',[False,True])
@pytest.mark.parametrize('all_faces',[False,True])
def test_selected_review_later_preserves_other_faces_and_media(automatic,named,all_faces):
    with database.connection() as db:
        if named:setup_groups(db,automatic)
        else:auto.ingest(db,'later-fixture','synthetic-auto',observations(automatic))
        source=next(g for g in auto.catalog(db)['groups'] if len(g['faces'])==2)
        ids=[f['detection_id'] for f in source['faces']]
        chosen=ids if all_faces else ids[:1]
        count=db.execute('SELECT count(*) n FROM media').fetchone()['n']
        result=auto.change_selected_faces(db,source['id'],chosen,'review-later',None,'test')
        target=auto.detail(db,result['group_ids'][0])['group']
        assert target['person_id'] is None and target['section']=='other'
        assert {f['detection_id'] for f in target['faces']}==set(chosen)
        if result['group_ids'][0]!=source['id']:
            assert {f['detection_id'] for f in auto.detail(db,source['id'])['group']['faces']}==set(ids)-set(chosen)
        assert db.execute('SELECT count(*) n FROM media').fetchone()['n']==count
        assert not db.execute('SELECT 1 FROM people_assignments WHERE detection_id=ANY(%s)',(chosen,)).fetchone()
        assert not db.execute('SELECT 1 FROM face_detections WHERE id=ANY(%s) AND ignored',(chosen,)).fetchone()
        if named:
            assert db.execute('SELECT count(*) n FROM auto_people_blocks WHERE detection_id=ANY(%s) AND person_id=%s',(chosen,source['person_id'])).fetchone()['n']==len(chosen)


def test_undo_survives_scan_receipts_but_not_new_manual_changes(automatic):
    from face_detect.api.automatic_people_api import Change
    from face_detect.api.automatic_people_api import undo
    from fastapi import HTTPException
    with database.connection() as db:
        setup_groups(db,automatic)
        view=auto.catalog(db);source,target=view['groups']
        did=source['faces'][0]['detection_id']
        eid=auto.correct(db,did,target['person_id'],'test')['event_id']
        revision=auto.people.revision(db)
        auto.event(db,'receipt','automatic','test',{})
        assert any(h['id']==eid and h['can_undo'] for h in auto.detail(db,source['id'])['history'])
    result=undo(eid,Change(revision=revision),{'username':'test'})
    assert result['ok']
    with database.connection() as db:
        assert did in {f['detection_id'] for f in auto.detail(db,source['id'])['group']['faces']}
        eid=auto.correct(db,did,target['person_id'],'test')['event_id']
        auto.event(db,'rename','manual','test',{})
    with pytest.raises(HTTPException) as error:undo(eid,Change(revision=revision),{'username':'test'})
    assert error.value.status_code==409


@pytest.mark.parametrize('action',['move','ignore'])
def test_selected_faces_changes_only_selection(automatic,action):
    from face_detect.api.automatic_people_api import SelectedFaces
    from face_detect.api.automatic_people_api import selected_faces
    with database.connection() as db:
        setup_groups(db,automatic)
        view=auto.catalog(db)
        source=next(g for g in view['groups'] if len(g['faces'])==2)
        target=next(g for g in view['groups'] if g['id']!=source['id'])
        chosen=source['faces'][0]['detection_id']
        remaining=source['faces'][1]['detection_id']
        media_count=db.execute('SELECT count(*) n FROM media').fetchone()['n']
        auto.event(db,'receipt','automatic','test',{})
    body=SelectedFaces(revision=view['revision'],group_versions={str(source['id']):source['review_version']},
                       detection_ids=[chosen],action=action,person_id=target['person_id'] if action=='move' else None)
    assert selected_faces(source['id'],body,{'username':'test'})=={'changed':1}
    with database.connection() as db:
        after=auto.catalog(db)
        assert {f['detection_id'] for f in auto.detail(db,source['id'])['group']['faces']}=={remaining}
        assert db.execute('SELECT count(*) n FROM media').fetchone()['n']==media_count
        if action=='move':
            dest=next(g for g in after['groups'] if g['person_id']==target['person_id'])
            assert chosen in {f['detection_id'] for f in dest['faces']}
        else:
            assert db.execute('SELECT ignored FROM face_detections WHERE id=%s',(chosen,)).fetchone()['ignored']


def test_selected_faces_rejects_other_group_and_rolls_back(automatic,monkeypatch):
    with database.connection() as db:
        setup_groups(db,automatic)
        before=auto.catalog(db)
        source=next(g for g in before['groups'] if len(g['faces'])==2)
        target=next(g for g in before['groups'] if g['id']!=source['id'])
        with pytest.raises(ValueError):
            auto.change_selected_faces(db,source['id'],[target['faces'][0]['detection_id']],'ignore',None,'test')
        real=auto.correct_many
        def fail_after_batch(db,ids,pid,actor):
            real(db,ids,pid,actor)
            raise RuntimeError('Fixture failure')
        monkeypatch.setattr(auto,'correct_many',fail_after_batch)
        with pytest.raises(RuntimeError,match='Fixture failure'):
            auto.change_selected_faces(db,source['id'],[f['detection_id'] for f in source['faces']],'move',target['person_id'],'test')
        assert auto.catalog(db)==before


@pytest.mark.parametrize('named',[False,True])
def test_detail_matches_catalog_without_loading_unrelated_faces(automatic,monkeypatch,named):
    with database.connection() as db:
        if named:setup_groups(db,automatic)
        else:auto.ingest(db,'detail-fixture','synthetic-auto',observations(automatic))
        view=auto.catalog(db)
        group=view['groups'][0]
        expected={f['detection_id'] for f in group['faces']}
        real_rows=auto.rows
        calls=[]
        def scoped_rows(db,ids=None):
            assert ids is not None,'Detail must not load the entire face catalog'
            calls.append(set(ids))
            return real_rows(db,ids)
        monkeypatch.setattr(auto,'rows',scoped_rows)
        assert auto.detail(db,group['id'])['group']==group
        assert calls[0]==expected
        with pytest.raises(LookupError):auto.detail(db,999999)


def test_detail_preserves_deferred_section_and_cover(automatic):
    with database.connection() as db:
        auto.initialize_review_sections(db,[])
        auto.ingest(db,'detail-fixture','synthetic-auto',observations(automatic))
        group=auto.catalog(db)['groups'][0]
        auto.set_aside_groups(db,[group['id']],'test')
        auto.set_cover(db,group['id'],group['faces'][-1]['detection_id'],'test')
        expected=next(g for g in auto.catalog(db)['groups'] if g['id']==group['id'])
        assert expected['section']=='other'
        assert auto.detail(db,group['id'])['group']==expected


def test_cover_api_allows_scan_updates_but_checks_concurrent_cover(automatic):
    from face_detect.api.automatic_people_api import Cover
    from face_detect.api.automatic_people_api import cover
    from fastapi import HTTPException
    with database.connection() as db:
        setup_groups(db,automatic)
        view=auto.catalog(db)
        group=next(g for g in view['groups'] if len(g['faces'])>1)
        chosen=group['faces'][-1]['detection_id']
        auto.event(db,'receipt','automatic','test',{})
    payload=Cover(revision=view['revision'],detection_id=chosen,expected_cover_id=group['cover_detection_id'])
    assert cover(group['id'],payload,{'username':'test'})['cover_detection_id']==chosen
    with pytest.raises(HTTPException) as error:cover(group['id'],payload,{'username':'test'})
    assert error.value.status_code==409
    other=next(f['detection_id'] for g in view['groups'] if g['id']!=group['id'] for f in g['faces'])
    with pytest.raises(HTTPException) as error:
        cover(group['id'],Cover(revision=view['revision'],detection_id=other,expected_cover_id=chosen),{'username':'test'})
    assert error.value.status_code==422
    with database.connection() as db:assert auto.detail(db,group['id'])['group']['cover_detection_id']==chosen


def test_save_name_survives_scan_updates_and_rejects_stale_rename(automatic):
    from face_detect.api.automatic_people_api import Name
    from face_detect.api.automatic_people_api import Rename
    from face_detect.api.automatic_people_api import name
    from face_detect.api.automatic_people_api import rename
    from fastapi import HTTPException
    with database.connection() as db:
        auto.ingest(db,'naming-fixture','synthetic-auto',observations(automatic))
        view=auto.catalog(db);group=view['groups'][0]
        auto.event(db,'receipt','automatic','test',{})
    result=name(group['id'],Name(name='Test name',revision=view['revision'],group_versions={str(group['id']):group['review_version']}),{'username':'test'})
    pid=result['person_id']
    with database.connection() as db:auto.event(db,'receipt','automatic','test',{})
    rename(pid,Rename(name='New test name',expected_name='Test name',revision=view['revision']),{'username':'test'})
    with pytest.raises(HTTPException) as error:
        rename(pid,Rename(name='Stale change',expected_name='Test name',revision=view['revision']),{'username':'test'})
    assert error.value.status_code==409
    with database.connection() as db:assert auto.detail(db,group['id'])['group']['name']=='New test name'


@pytest.mark.parametrize('bulk',[False,True])
def test_ignored_person_remains_available_for_assignment(automatic,bulk):
    with database.connection() as db:
        auto.initialize_review_sections(db,[])
        auto.ingest(db,'reuse-name-fixture','synthetic-auto',observations(automatic))
        groups=auto.catalog(db)['groups'];old=groups[0];other=groups[1]
        pid=auto.name_group(db,old['id'],'Reusable name','test')['person_id']
        ignored=[f['detection_id'] for f in old['faces']]
        for did in ignored:auto.ignore(db,did,True,'test')
        view=auto.catalog(db)
        assert not any(g['person_id']==pid for g in view['groups'])
        assert {'person_id':pid,'name':'Reusable name'} in view['people']
        if bulk:auto.match_review_groups(db,[other['id']],pid,'test')
        else:auto.match_group(db,other['id'],pid,'test')
        after=auto.catalog(db)
        person=next(g for g in after['groups'] if g['person_id']==pid)
        assert {f['detection_id'] for f in person['faces']}=={f['detection_id'] for f in other['faces']}
        assert db.execute('SELECT count(*) n FROM face_detections WHERE id=ANY(%s) AND ignored',(ignored,)).fetchone()['n']==len(ignored)
        assert db.execute('SELECT count(*) n FROM people WHERE name=%s',('Reusable name',)).fetchone()['n']==1


def test_reorder_api_allows_scanning_but_rejects_conflicting_reorder(automatic):
    from face_detect.api.automatic_people_api import Place
    from face_detect.api.automatic_people_api import place_group
    from fastapi import HTTPException
    with database.connection() as db:
        setup_groups(db,automatic)
        view=auto.catalog(db);first,last=[g['id'] for g in view['groups']]
        auto.event(db,'receipt','automatic','test',{})
    body=Place(revision=view['revision'],order_revision=view['order_revision'],target_id=first,after=False)
    assert place_group(last,body,{'username':'test'})=={'moved':True}
    with database.connection() as db:assert [g['id'] for g in auto.catalog(db)['groups']]==[last,first]
    with pytest.raises(HTTPException) as error:place_group(last,body,{'username':'test'})
    assert error.value.status_code==409
