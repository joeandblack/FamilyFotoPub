from test_automatic_people import automatic, pool, env, observations
from test_unexamined_photos import disposable_migrations
from face_detect.recognition import automatic_people as auto
from app import database
import pytest


def review_match_fixture(db,automatic):
    from test_automatic_people import add_face
    auto.initialize_review_sections(db,[])
    auto.ingest(db,'bulk-match','synthetic-auto',observations(automatic),'fixture')
    groups=auto.catalog(db)['groups']
    did,_=add_face(db,999)
    gid=db.execute('INSERT INTO auto_people_groups DEFAULT VALUES RETURNING id').fetchone()['id']
    auto.put_member(db,did,gid,'manual','synthetic-auto')
    pid=auto.name_group(db,gid,'Bulk target','fixture')['person_id']
    return [g['id'] for g in groups],pid


def test_bulk_match_moves_all_groups_and_faces(automatic):
    with database.connection() as db:
        ids,pid=review_match_fixture(db,automatic)
        count=sum(len(g['faces']) for g in auto.catalog(db)['groups'])
        result=auto.match_review_groups(db,ids,pid,'fixture')
        assert result['groups']==2
        after=auto.catalog(db)['groups']
        assert len(after)==1 and after[0]['person_id']==pid
        assert len(after[0]['faces'])==count


@pytest.mark.parametrize('action',['match','ignore'])
def test_other_photos_bulk_actions_are_section_scoped(automatic,action):
    with database.connection() as db:
        ids,pid=review_match_fixture(db,automatic)
        def run():
            return auto.match_review_groups(db,ids,pid,'fixture','other') if action=='match' else auto.ignore_review_groups(db,ids,'fixture','other')
        before=auto.catalog(db)
        with pytest.raises(ValueError,match='Selected groups changed'):run()
        assert auto.catalog(db)==before
        auto.set_aside_groups(db,ids,'fixture')
        assert run()['groups']==len(ids)
        after=auto.catalog(db)['groups']
        assert len(after)==1 and after[0]['person_id']==pid


def test_bulk_ignore_preserves_media_and_can_restore_faces(automatic):
    with database.connection() as db:
        ids,pid=review_match_fixture(db,automatic)
        before=auto.catalog(db)
        faces=[f['detection_id'] for g in before['groups'] if g['id'] in ids for f in g['faces']]
        media=db.execute('SELECT * FROM media ORDER BY id').fetchall()
        assert auto.ignore_review_groups(db,ids,'fixture')=={'ignored':len(faces),'groups':len(ids)}
        assert all(g['person_id']==pid for g in auto.catalog(db)['groups'])
        assert db.execute('SELECT count(*) n FROM face_detections WHERE ignored').fetchone()['n']==len(faces)
        assert db.execute('SELECT * FROM media ORDER BY id').fetchall()==media
        for did in faces:auto.ignore(db,did,False,'fixture')
        assert auto.catalog(db)['groups']==before['groups']


def test_bulk_ignore_failure_rolls_back(automatic,monkeypatch):
    with database.connection() as db:
        ids,_=review_match_fixture(db,automatic)
        before=auto.catalog(db)
        real=auto.ignored_faces.set_ignored
        def fail_after_update(*args):
            real(*args)
            raise ValueError('fixture failure')
        monkeypatch.setattr(auto.ignored_faces,'set_ignored',fail_after_update)
        with pytest.raises(ValueError,match='fixture failure'):auto.ignore_review_groups(db,ids,'fixture')
        assert auto.catalog(db)==before
        assert db.execute('SELECT count(*) n FROM face_detections WHERE ignored').fetchone()['n']==0


def test_bulk_match_rolls_back_partial_corrections(automatic,monkeypatch):
    with database.connection() as db:
        ids,pid=review_match_fixture(db,automatic)
        before=auto.catalog(db)
        real=auto.correct
        calls=[]
        def conflict(db,did,pid,actor):
            if calls:raise ValueError('fixture conflict')
            calls.append(did);return real(db,did,pid,actor)
        monkeypatch.setattr(auto,'correct',conflict)
        with pytest.raises(ValueError,match='fixture conflict'):auto.match_review_groups(db,ids,pid,'fixture')
        assert auto.catalog(db)==before
        with pytest.raises(ValueError,match='Selected groups changed'):auto.match_review_groups(db,ids+[9999999],pid,'fixture')


def test_set_aside_keeps_faces_and_persists(automatic):
    with database.connection() as db:
        auto.initialize_review_sections(db,[])
        auto.ingest(db,'set-aside','synthetic-auto',observations(automatic),'fixture')
        before=auto.catalog(db)['groups']
        ids=[g['id'] for g in before if g['section']=='review']
        assert ids
        faces={g['id']:[f['detection_id'] for f in g['faces']] for g in before}
        assert auto.set_aside_groups(db,ids,'fixture')['moved']==len(ids)
    with database.connection() as db:
        after=auto.catalog(db)['groups']
        assert {g['id']:[f['detection_id'] for f in g['faces']] for g in after}==faces
        assert all(g['section']=='other' for g in after if g['id'] in ids)


def test_set_aside_rejects_invalid_batch_without_changes(automatic):
    with database.connection() as db:
        auto.initialize_review_sections(db,[])
        auto.ingest(db,'set-aside-invalid','synthetic-auto',observations(automatic),'fixture')
        before=auto.catalog(db)
        selected=next(g for g in before['groups'] if g['section']=='review')
        with pytest.raises(ValueError):
            auto.set_aside_groups(db,[selected['id'],99999999],'fixture')
        assert auto.catalog(db)==before


def test_section_policy():
    old={'id':1,'person_id':None,'optional_outlier':True,'faces':[{'detection_id':10}]}
    baseline={'1':[10]}
    assert auto.review_section(old,baseline)=='other'
    assert auto.review_section({**old,'faces':[{'detection_id':10},{'detection_id':11}]},baseline)=='review'
    assert auto.review_section({**old,'id':2},baseline)=='review'
    assert auto.review_section({**old,'person_id':7},baseline)=='people'


def test_drag_order_persists_and_rejects_unnamed_targets(automatic):
    from test_automatic_people import setup_groups,add_face
    with database.connection() as db:
        ga,gb,pa,pb=setup_groups(db,automatic)
        before=auto.catalog(db)['groups']
        first,last=[g['id'] for g in before]
        auto.place_named_group(db,last,first,False,'fixture')
    with database.connection() as db:
        assert [g['id'] for g in auto.catalog(db)['groups']]==[last,first]
        auto.place_named_group(db,last,first,True,'fixture')
        assert auto.catalog(db)['groups']==before
        with pytest.raises(ValueError):auto.place_named_group(db,first,999999,False,'fixture')
        did,_=add_face(db,999)
        gid=db.execute('INSERT INTO auto_people_groups DEFAULT VALUES RETURNING id').fetchone()['id']
        auto.put_member(db,did,gid,'manual','synthetic-auto')
        with pytest.raises(ValueError):auto.place_named_group(db,first,gid,False,'fixture')


def test_baseline_survives_reload_and_keeps_current_scan_in_review(automatic):
    with database.connection() as db:
        auto.ingest(db,'review-sections','synthetic-auto',observations(automatic),'fixture')
        db.execute("UPDATE media SET captured_at='2026-01-01'")
        groups=auto.catalog(db)['groups']
        assert len(groups)>=2
        current=groups[-1]['faces'][0]['media_id']
        auto.initialize_review_sections(db,[current])
        result=auto.catalog(db)['groups']
        assert next(g for g in result if any(f['media_id']==current for f in g['faces']))['section']=='review'
        assert any(g['section']=='other' for g in result)
        auto.initialize_review_sections(db,[])
        assert [(g['id'],g['section']) for g in auto.catalog(db)['groups']]==[(g['id'],g['section']) for g in result]
        selected=next(g for g in result if g['section']=='review')
        auto.name_group(db,selected['id'],'Reviewed person','fixture')
        assert next(g for g in auto.catalog(db)['groups'] if g['id']==selected['id'])['section']=='people'


def test_catalog_bulk_person_lookup_and_group_merge(automatic):
    from test_automatic_people import setup_groups
    with database.connection() as db:
        ga,gb,pa,pb=setup_groups(db,automatic)
        queries=[]
        class Traced:
            def execute(self,query,*args):
                queries.append(query)
                return db.execute(query,*args)
        before=auto.catalog(Traced())
        assert sum(q=='SELECT id,name,deleted_at FROM people' for q in queries)==1
        assert not any(q.startswith('SELECT name,deleted_at FROM people WHERE') for q in queries)
        count=sum(len(g['faces']) for g in before['groups'])
        auto.match_group(db,gb,pa,'fixture')
        after=auto.catalog(db)
        assert sum(len(g['faces']) for g in after['groups'])==count
        assert not any(g['person_id']==pb for g in after['groups'])
        assert auto.detail(db,ga)['group']['person_id']==pa
