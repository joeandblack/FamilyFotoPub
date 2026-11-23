import hashlib
from pathlib import Path
import pytest
from psycopg.types.json import Jsonb
from test_people_service import pool
from test_uploads import env
from app import database
from face_detect.recognition import automatic_people as auto

@pytest.fixture
def automatic(pool,monkeypatch):
    _,ids=pool
    monkeypatch.setattr(auto,'load_config',lambda:{'automatic_people':{'enabled':True}})
    with database.connection() as db:
        db.execute("INSERT INTO space(space_id,manifest,state) VALUES('synthetic-auto',%s,'active')",(Jsonb({'dimensions':2,'synthetic':True}),))
        db.execute("UPDATE face_detections SET review_state='unreviewed'")
        db.execute("UPDATE media SET captured_at='2026-01-01'::timestamp+id*interval '1 day'")
    return ids

def observations(ids):
    return [{'detection_id':did,'vector':[1.,0.] if n<2 else [0.,1.],'seed':True} for n,did in enumerate(ids)]


def test_review_pages_match_catalog_and_bound_memberships(automatic,monkeypatch):
    from face_detect.api.people_review_pages import review_page
    with database.connection() as db:
        auto.ingest(db,'paged','synthetic-auto',observations(automatic))
        original=auto.catalog
        expected=original(db)['groups']
        seen=[]
        def bounded(db,*args,**kwargs):
            assert 'group_ids' in kwargs
            seen.append(kwargs['group_ids'])
            return original(db,*args,**kwargs)
        monkeypatch.setattr(auto,'catalog',bounded)
        for section in ['review','other']:
            wanted=[g for g in expected if g['section']==section]
            for page in range(1,len(wanted)+2):
                result=review_page(db,section,page,1)
                assert result['total']==len(wanted)
                index=min(page,max(1,len(wanted)))-1
                assert result['groups']==wanted[index:index+1]
                assert len(seen[-1])<=1
        # A baseline sends existing groups to Review later; new faces remain reviewable.
        auto.event(db,'review-sections-baseline','manual','test',{'legacy':{str(g['id']):[f['detection_id'] for f in g['faces']] for g in expected}})
        assert review_page(db,'other',1,100)['total']==len(expected)
        auto.event(db,'review-set-aside','manual','test',{'groups':{str(expected[0]['id']):[]}})
        assert review_page(db,'review',1,100)['groups'][0]['id']==expected[0]['id']


def test_consistent_manual_receipt_memberships_do_no_database_work():
    class NoQueries:
        def execute(self,*args,**kwargs):raise AssertionError('Unchanged manual faces must not be rewritten')
    manual={did:7 for did in range(1,4059)}
    memberships={did:{'person_id':7,'origin':'manual','space_id':'test'} for did in manual}
    auto.restore_manual_memberships(NoQueries(),'test',set(manual),manual,memberships)


def test_manual_receipt_repairs_are_batched_and_preserve_ineligible_faces(automatic):
    with database.connection() as db:
        a,b,pa,pb=setup_groups(db,automatic)
        memberships={r['detection_id']:r for r in db.execute('SELECT m.*,g.person_id,p.deleted_at FROM auto_people_members m JOIN auto_people_groups g ON g.id=m.group_id LEFT JOIN people p ON p.id=g.person_id').fetchall()}
        chosen=automatic[:2]
        manual={did:pb for did in automatic}
        calls=[]
        class Count:
            def execute(self,*args,**kwargs):calls.append(args[0]);return db.execute(*args,**kwargs)
        auto.restore_manual_memberships(Count(),'synthetic-auto',set(chosen),manual,memberships)
        assert len(calls)==3
        for did in chosen:
            assert memberships[did]['group_id']==b and memberships[did]['origin']=='manual'
            assert db.execute('SELECT group_id,origin FROM auto_people_members WHERE detection_id=%s',(did,)).fetchone()=={'group_id':b,'origin':'manual'}
        assert memberships[automatic[2]]['origin']=='named_seed'
        calls.clear()
        auto.restore_manual_memberships(Count(),'synthetic-auto',set(chosen),manual,memberships)
        assert not calls

def add_face(db,n):
    sha=hashlib.sha256(('auto-'+str(n)).encode()).hexdigest()
    mid=db.execute("INSERT INTO media(sha256,relative_path,name,kind,size,captured_at) VALUES(%s,%s,%s,'image',10,'2026-03-01'::timestamp+%s*interval '1 day') RETURNING id",(sha,f'auto-{n}.jpg',f'auto-{n}.jpg',n)).fetchone()['id']
    aid=db.execute("INSERT INTO face_analyses(media_id,content_hash,detector_id,width,height,outcome) VALUES(%s,%s,'fixture',100,100,'done') RETURNING id",(mid,sha)).fetchone()['id']
    did=db.execute("INSERT INTO face_detections(analysis_id,box,score) VALUES(%s,'{}',.99) RETURNING id",(aid,)).fetchone()['id']
    return did,mid

def setup_groups(db,ids):
    auto.ingest(db,'first','synthetic-auto',observations(ids))
    groups=auto.catalog(db)['groups'];assert len(groups)==2
    assert all(g['name'] is None for g in groups)
    a=next(g for g in groups if ids[0] in [f['detection_id'] for f in g['faces']])
    b=next(g for g in groups if ids[2] in [f['detection_id'] for f in g['faces']])
    pa=auto.name_group(db,a['id'],'Fixture A','fixture-admin')['person_id']
    pb=auto.name_group(db,b['id'],'Fixture B','fixture-admin')['person_id']
    return a['id'],b['id'],pa,pb

def test_feature_is_disabled_before_any_db_access(monkeypatch):
    monkeypatch.setattr(auto,'load_config',lambda:{})
    with pytest.raises(RuntimeError,match='disabled'):auto.catalog(None)


def test_live_person_cards_follow_corrections_and_exclusions(automatic):
    with database.connection() as db:
        a,b,pa,pb=setup_groups(db,automatic)
        cards={p['person_id']:p for p in auto.named_people(db)['people']}
        assert cards[pa]['photo_count']==2
        assert cards[pa]['cover_detection_id']==automatic[0]
        auto.set_cover(db,a,automatic[1],'admin')
        assert next(p for p in auto.named_people(db)['people'] if p['person_id']==pa)['cover_detection_id']==automatic[1]
        db.execute('UPDATE people SET name=%s WHERE id=%s',('Renamed',pa))
        db.execute('UPDATE face_detections SET ignored=true WHERE id=%s',(automatic[1],))
        card=next(p for p in auto.named_people(db)['people'] if p['person_id']==pa)
        assert card['name']=='Renamed' and card['photo_count']==1 and card['cover_detection_id']==automatic[0]
        db.execute('INSERT INTO people_assignments(detection_id,person_id) VALUES(%s,%s) ON CONFLICT(detection_id) DO UPDATE SET person_id=EXCLUDED.person_id',(automatic[0],pb))
        assert next(p for p in auto.named_people(db)['people'] if p['person_id']==pa)['photo_count']==0
        db.execute('UPDATE people SET deleted_at=now() WHERE id=%s',(pa,))
        assert pa not in [p['person_id'] for p in auto.named_people(db)['people']]


def test_manual_order_persists_without_changing_people(automatic):
    with database.connection() as db:
        a,b,pa,pb=setup_groups(db,automatic)
        original=auto.catalog(db)['groups']
        first,last=[g['id'] for g in original]
        assert auto.move_group(db,first,'up','admin')=={'moved':False}
        assert auto.move_group(db,last,'up','admin')=={'moved':True}
    with database.connection() as db:
        ordered=auto.catalog(db)['groups']
        assert [g['id'] for g in ordered]==[last,first]
        expected=[g['person_id'] for g in ordered]
        assert [p['person_id'] for p in auto.catalog(db)['people']]==expected
        assert [p['person_id'] for p in auto.named_people(db)['people']]==expected
        assert sorted(ordered,key=lambda g:g['id'])==sorted(original,key=lambda g:g['id'])
        auto.rename_person(db,pa,'Changed name','admin')
        assert [g['id'] for g in auto.catalog(db)['groups']]==[last,first]
        assert [p['person_id'] for p in auto.named_people(db)['people']]==expected
        assert auto.move_group(db,last,'down','admin')=={'moved':True}
        assert [g['id'] for g in auto.catalog(db)['groups']]==[first,last]
        with pytest.raises(ValueError):auto.move_group(db,first,'sideways','admin')
        with pytest.raises(LookupError):auto.move_group(db,999999,'up','admin')


def test_picker_order_matches_legacy_unnamed_group_entries(automatic):
    with database.connection() as db:
        a,b,pa,pb=setup_groups(db,automatic)
        auto.event(db,'order','manual','admin',{'order':[
            {'group_id':b,'person_id':None},
            {'group_id':a,'person_id':pa},
            {'group_id':b,'person_id':pb}]})
        view=auto.catalog(db)
        assert [g['person_id'] for g in view['groups']]==[pb,pa]
        assert [p['person_id'] for p in view['people']]==[pb,pa]
        assert [p['person_id'] for p in auto.named_people(db)['people']]==[pb,pa]


def test_main_people_cards_skip_catalog_and_return_one_cover(automatic,monkeypatch):
    with database.connection() as db:
        a,b,pa,pb=setup_groups(db,automatic)
        expected={g['person_id']:g for g in auto.catalog(db)['groups']}
        monkeypatch.setattr(auto,'catalog',lambda *args,**kwargs:pytest.fail('Full catalog must not load'))
        monkeypatch.setattr(auto,'rows',lambda *args,**kwargs:pytest.fail('Full face rows must not load'))
        result=auto.named_cards(db)
        assert result['summary_only'] is True
        assert len(result['groups'])==2
        for group in result['groups']:
            full=expected[group['person_id']]
            assert group['id']==full['id']
            assert len(group['faces'])==1
            assert group['face_count']==len(full['faces'])
            assert group['photo_count']==len({f['media_id'] for f in full['faces']})
            assert group['cover_detection_id']==full['cover_detection_id']


def test_people_with_all_faces_ignored_are_last_in_all_pickers(automatic):
    with database.connection() as db:
        a,b,pa,pb=setup_groups(db,automatic)
        auto.event(db,'order','manual','admin',{'order':[
            {'group_id':a,'person_id':pa},{'group_id':b,'person_id':pb}]})
        db.execute('UPDATE face_detections SET ignored=true WHERE id IN '
                   '(SELECT detection_id FROM auto_people_members WHERE group_id=%s)',(a,))
        assert [g['person_id'] for g in auto.catalog(db)['groups']]==[pb]
        assert [p['person_id'] for p in auto.catalog(db)['people']]==[pb,pa]
        assert [p['person_id'] for p in auto.named_people(db)['people']]==[pb,pa]
        db.execute('UPDATE face_detections SET ignored=false WHERE id IN '
                   '(SELECT detection_id FROM auto_people_members WHERE group_id=%s)',(a,))
        assert [p['person_id'] for p in auto.catalog(db)['people']]==[pa,pb]


def test_catalog_cohort_not_limited_to_prototype_size(monkeypatch):
    records=[{'detection_id':i,'seed':False,'vector':[1.,0.]} for i in range(5001)]
    metadata=[{'id':i,'media_id':i,'content_hash':str(i),'captured_at':None} for i in range(5001)]
    class Result:
        def fetchall(self):return records
    class DB:
        def execute(self,*args):return Result()
    monkeypatch.setattr(auto,'rows',lambda db:metadata)
    monkeypatch.setattr('face_detect.recognition.policy.native_policy',lambda db,model:{})
    result=auto.cohort(DB(),'fixture')
    assert len(result)==5001 and result[-1]['space_id']=='fixture'

def test_cover_selection_persists_and_falls_back_when_ineligible(automatic):
    with database.connection() as db:
        ga,gb,pa,pb=setup_groups(db,automatic)
        group=next(g for g in auto.catalog(db)['groups'] if g['id']==ga)
        chosen=group['faces'][-1]['detection_id']
        auto.set_cover(db,ga,chosen,'admin')
    with database.connection() as db:
        group=next(g for g in auto.catalog(db)['groups'] if g['id']==ga)
        assert group['cover_detection_id']==chosen
        with pytest.raises(ValueError,match='eligible'):auto.set_cover(db,ga,automatic[2],'admin')
        auto.ignore(db,chosen,True,'admin')
        group=next(g for g in auto.catalog(db)['groups'] if g['id']==ga)
        assert group['cover_detection_id']!=chosen

def test_recheck_existing_unnamed_scope_and_idempotence(automatic):
    with database.connection() as db:
        ids=[add_face(db,800+i)[0] for i in range(5)]
        auto.ingest(db,'recheck-fixture','synthetic-auto',[{'detection_id':did,'vector':[1.,0.],'seed':True} for did in ids])
        gid=auto.catalog(db)['groups'][0]['id']
        # Separate two faces before naming the reference group.
        other=db.execute('INSERT INTO auto_people_groups DEFAULT VALUES RETURNING id').fetchone()['id']
        for did in ids[-2:]:auto.put_member(db,did,other,'automatic','synthetic-auto')
        pid=auto.name_group(db,gid,'Reference','admin')['person_id']
        mid=auto.rows(db,[ids[-2]])[0]['media_id']
        result=auto.recheck_unnamed(db,[mid],'admin')
        assert result=={'checked':1,'matched':1,'remaining':0}
        assert auto.recheck_unnamed(db,[mid],'admin')['matched']==0
        remaining=next(g for g in auto.catalog(db)['groups'] if g['person_id'] is None)
        assert [f['detection_id'] for f in remaining['faces']]==[ids[-1]]
        assert next(g for g in auto.catalog(db)['groups'] if g['person_id']==pid)

@pytest.mark.parametrize('conflict',[False,True])
@pytest.mark.parametrize('named',[False,True])
def test_match_whole_group_is_atomic(automatic,conflict,named):
    with database.connection() as db:
        auto.ingest(db,'first','synthetic-auto',observations(automatic))
        groups=auto.catalog(db)['groups']
        source=next(g for g in groups if len(g['faces'])==2)
        target=next(g for g in groups if g['id']!=source['id'])
        pid=auto.name_group(db,target['id'],'Target','admin')['person_id']
        if named:
            source_pid=auto.name_group(db,source['id'],'Source','admin')['person_id']
            with pytest.raises(ValueError,match='different person'):auto.match_group(db,source['id'],source_pid,'admin')
        ids=sorted(f['detection_id'] for f in source['faces'])
        if conflict:
            a,b=sorted([ids[-1],target['faces'][0]['detection_id']])
            db.execute('INSERT INTO people_constraints(a,b) VALUES(%s,%s)',(a,b))
            with pytest.raises(ValueError,match='separation'):auto.match_group(db,source['id'],pid,'admin')
            assert not db.execute('SELECT 1 FROM people_assignments WHERE detection_id=ANY(%s)',(ids,)).fetchone()
        else:
            assert auto.match_group(db,source['id'],pid,'admin')['matched']==2
            assert len(auto.catalog(db)['groups'])==1
            assert db.execute('SELECT count(*) n FROM people_assignments WHERE person_id=%s',(pid,)).fetchone()['n']==2

def test_unreviewed_group_name_once_known_import_and_idempotence(automatic):
    ids=automatic
    with database.connection() as db:
        ga,gb,pa,pb=setup_groups(db,ids)
        with pytest.raises(ValueError,match='already named'):auto.name_group(db,ga,'Other','fixture-admin')
        did,_=add_face(db,1);obs=[{'detection_id':did,'vector':[1.,0.],'seed':True}]
        result=auto.ingest(db,'later','synthetic-auto',obs);assert result['known_attachments']==1
        assert auto.ingest(db,'later','synthetic-auto',obs)==result
        member=db.execute('SELECT * FROM auto_people_members WHERE detection_id=%s',(did,)).fetchone()
        assert member['group_id']==ga and member['origin']=='automatic'
        assert not db.execute('SELECT 1 FROM people_assignments WHERE detection_id=%s',(did,)).fetchone()
        assert db.execute('SELECT count(*) n FROM people').fetchone()['n']==2
        with pytest.raises(ValueError,match='payload changed'):auto.ingest(db,'later','synthetic-auto',observations(ids))

def test_wrong_match_correction_ignore_unignore_later_import_and_trash(automatic):
    ids=automatic
    with database.connection() as db:
        ga,gb,pa,pb=setup_groups(db,ids);did,mid=add_face(db,2)
        obs=[{'detection_id':did,'vector':[1.,0.],'seed':True}]
        auto.ingest(db,'wrong-match','synthetic-auto',obs)
        auto.correct(db,did,pb,'fixture-admin')
        assert db.execute('SELECT 1 FROM auto_people_blocks WHERE detection_id=%s AND person_id=%s',(did,pa)).fetchone()
        auto.ignore(db,did,True,'fixture-admin')
        assert all(did not in [f['detection_id'] for f in g['faces']] for g in auto.catalog(db)['groups'])
        auto.ignore(db,did,False,'fixture-admin')
        auto.ingest(db,'replayed-observation','synthetic-auto',obs)
        assert db.execute('SELECT group_id,origin FROM auto_people_members WHERE detection_id=%s',(did,)).fetchone()=={'group_id':gb,'origin':'manual'}
        db.execute('INSERT INTO media_trash(media_id) VALUES(%s)',(mid,))
        assert all(did not in [f['detection_id'] for f in g['faces']] for g in auto.catalog(db)['groups'])
        db.execute('DELETE FROM media_trash WHERE media_id=%s',(mid,))
        assert any(did in [f['detection_id'] for f in g['faces']] and g['person_id']==pb for g in auto.catalog(db)['groups'])
        assert db.execute('SELECT person_id FROM people_assignments WHERE detection_id=%s',(did,)).fetchone()['person_id']==pb

def test_targeted_undo_and_stale_undo(automatic):
    with database.connection() as db:
        ga,gb,pa,pb=setup_groups(db,automatic)
        eid=auto.correct(db,automatic[0],pb,'fixture-admin')['event_id']
        auto.undo_correction(db,eid,'fixture-admin')
        assert db.execute('SELECT group_id FROM auto_people_members WHERE detection_id=%s',(automatic[0],)).fetchone()['group_id']==ga
        assert not db.execute('SELECT 1 FROM people_assignments').fetchone()
        with pytest.raises(RuntimeError):auto.undo_correction(db,eid,'fixture-admin')
        eid=auto.correct(db,automatic[0],pb,'fixture-admin')['event_id']
        auto.ignore(db,automatic[1],True,'fixture-admin')
        with pytest.raises(RuntimeError):auto.undo_correction(db,eid,'fixture-admin')

def test_transaction_crash_receipt_resume_and_unnamed_incremental(automatic):
    ids=automatic
    with pytest.raises(RuntimeError):
        with database.connection() as db:
            auto.ingest(db,'crashed','synthetic-auto',observations(ids[:1]));raise RuntimeError('injected crash before commit')
    with database.connection() as db:
        assert not db.execute('SELECT 1 FROM auto_people_receipts').fetchone()
        auto.ingest(db,'crashed','synthetic-auto',observations(ids[:1]))
        group=auto.catalog(db)['groups'][0];assert group['optional_outlier']
        auto.ingest(db,'second-import','synthetic-auto',observations(ids[1:2]))
        groups=auto.catalog(db)['groups'];assert len(groups)==1 and groups[0]['id']==group['id'] and not groups[0]['optional_outlier']

def test_constraints_rejected_faces_and_model_mixing(automatic):
    ids=automatic
    with database.connection() as db:
        db.execute('INSERT INTO people_constraints VALUES(%s,%s)',(min(ids[:2]),max(ids[:2])))
        auto.ingest(db,'constrained','synthetic-auto',observations(ids[:2]))
        assert len(auto.catalog(db)['groups'])==2
        db.execute("UPDATE face_detections SET review_state='rejected' WHERE id=%s",(ids[2],))
        assert auto.ingest(db,'excluded','synthetic-auto',observations(ids[2:3]))['excluded']==1
        with pytest.raises(ValueError):auto.ingest(db,'missing-model','unknown',observations(ids))

def test_named_people_never_automerge_and_legacy_corrections_win(automatic):
    with database.connection() as db:
        ga,gb,pa,pb=setup_groups(db,automatic)
        did,_=add_face(db,8)
        from math import sqrt
        auto.ingest(db,'ambiguous','synthetic-auto',[{'detection_id':did,'vector':[1/sqrt(2),1/sqrt(2)],'seed':True}])
        assert db.execute('SELECT count(*) n FROM people').fetchone()['n']==2
        assert db.execute('SELECT group_id FROM auto_people_members WHERE detection_id=%s',(did,)).fetchone()['group_id'] not in (ga,gb)
        db.execute('INSERT INTO people_assignments VALUES(%s,%s)',(automatic[0],pb))
        auto.ingest(db,'legacy-change','synthetic-auto',observations(automatic))
        assert db.execute('SELECT group_id,origin FROM auto_people_members WHERE detection_id=%s',(automatic[0],)).fetchone()=={'group_id':gb,'origin':'manual'}
        a,b=sorted((automatic[1],automatic[2]));db.execute('INSERT INTO people_constraints VALUES(%s,%s)',(a,b))
        with pytest.raises(ValueError,match='separation'):auto.correct(db,automatic[1],pb,'fixture-admin')
