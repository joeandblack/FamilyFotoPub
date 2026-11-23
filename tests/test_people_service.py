import hashlib
import pytest
from test_uploads import env
from app import database
from face_detect.recognition import people

@pytest.fixture
def pool(env):
    from people_catalog_fixture import catalog_beside
    ids=[]
    with catalog_beside(env[1]):
        with database.connection() as db:
            db.execute("INSERT INTO detector(detector_id,manifest) VALUES('fixture','{}')")
            for i in range(4):
                sha=hashlib.sha256(str(i).encode()).hexdigest()
                m=db.execute("INSERT INTO media(sha256,relative_path,name,kind,size) VALUES(%s,%s,%s,'image',10) RETURNING id",(sha,f'{i}.jpg',f'{i}.jpg')).fetchone()['id']
                a=db.execute("INSERT INTO face_analyses(media_id,content_hash,detector_id,width,height,outcome) VALUES(%s,%s,'fixture',100,100,'done') RETURNING id",(m,sha)).fetchone()['id']
                d=db.execute("INSERT INTO face_detections(analysis_id,box,score,review_state) VALUES(%s,'{}',.99,'confirmed') RETURNING id",(a,)).fetchone()['id'];ids.append(d)
        yield env[0],ids

def act(db,action,**kw):
    preview=people.preview(db,{'action':action,**kw},'admin')
    return people.apply(db,preview['token'],'admin')

def test_dedup_visibility_and_bound(pool):
    _,ids=pool
    with database.connection() as db:
        assert [r['detection_id'] for r in people.eligible(db)]==ids
        row=people.eligible(db,[ids[0]])[0]
        db.execute("INSERT INTO detector(detector_id,manifest) VALUES('new','{}')")
        db.execute("INSERT INTO face_analyses(media_id,content_hash,detector_id,width,height,outcome) VALUES(%s,%s,'new',100,100,'no-faces')",(row['media_id'],row['content_hash']))
        assert ids[0] not in [r['detection_id'] for r in people.eligible(db)]
        db.execute("UPDATE face_detections SET review_state='rejected' WHERE id=%s",(ids[1],))
        assert [r['detection_id'] for r in people.eligible(db)]==ids[2:]

def test_corrections_constraints_undo_conflicts(pool):
    _,ids=pool
    with database.connection() as db:
        named=act(db,'assign',detection_ids=ids[:3],name='Alice');pid=named['person_id']
        assert people.person(db,pid)['name']=='Alice'
        stale=people.preview(db,{'action':'rename','person_id':pid,'name':'A'},'admin')
        split=act(db,'split',person_id=pid,detection_ids=[ids[2]],name='Bob')
        with pytest.raises(RuntimeError,match='changed'):people.apply(db,stale['token'],'admin')
        assert db.execute('SELECT count(*) n FROM people_constraints').fetchone()['n']==2
        with pytest.raises(RuntimeError,match='separated'):people.preview(db,{'action':'merge','person_id':pid,'target_person_id':split['person_id']},'admin')
        with pytest.raises(RuntimeError,match='conflicts'):people.undo(db,named['event_id'],split['revision'],'admin')
        people.undo(db,split['event_id'],split['revision'],'admin')
        assert len(people.person(db,pid)['faces'])==3
        assert db.execute('SELECT count(*) n FROM people_constraints').fetchone()['n']==0

def test_trash_restore_and_optout_cleanup(pool):
    from face_detect.recognition import faces
    _,ids=pool
    with database.connection() as db:
        made=act(db,'assign',detection_ids=ids[:2],name='Example')
        media=people.eligible(db,[ids[0]])[0]['media_id']
        db.execute('INSERT INTO media_trash(media_id) VALUES(%s)',(media,))
        assert len(people.person(db,made['person_id'])['faces'])==1
        db.execute('DELETE FROM media_trash WHERE media_id=%s',(media,))
        assert len(people.person(db,made['person_id'])['faces'])==2
        act(db,'rename',person_id=made['person_id'],name='Example 2')
        people.preview(db,{'action':'unassign','detection_ids':ids[:1]},'admin')
        faces.opt_out(db,media,True,'admin')
        assert not db.execute('SELECT 1 FROM people_assignments WHERE detection_id=%s',(ids[0],)).fetchone()
        assert not db.execute('SELECT 1 FROM people_previews').fetchone()
        assert db.execute('SELECT count(*) n FROM people_events').fetchone()['n']==1
        assert ids[0] not in [r['detection_id'] for r in people.eligible(db)]

def test_preview_once_and_actor_guard(pool):
    _,ids=pool
    with database.connection() as db:
        p=people.preview(db,{'action':'assign','detection_ids':ids[:1],'name':'Person'},'admin')
        with pytest.raises(RuntimeError):people.apply(db,p['token'],'different-admin')
        people.apply(db,p['token'],'admin')
        with pytest.raises(RuntimeError):people.apply(db,p['token'],'admin')
