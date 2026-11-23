from concurrent.futures import ThreadPoolExecutor
import threading
import pytest
from test_people_service import pool,act
from test_uploads import env
from face_detect.recognition import people
from app import database

def change(db,pid,action,**kw):
    p=people.preview(db,dict(action=action,person_id=pid,**kw),'admin')
    return people.apply(db,p['token'],'admin')

def test_delete_restore_persistence_and_no_media_changes(pool):
    _,ids=pool
    with database.connection() as db:
        pid=act(db,'assign',detection_ids=ids[:2],name='Person')['person_id']
        before=db.execute('SELECT * FROM people_assignments ORDER BY detection_id').fetchall()
        preview=people.preview(db,dict(action='delete',person_id=pid),'admin')
        assert preview['stored_face_count']==2 and preview['photo_count']==2 and 'preserved' in preview['preservation']
        people.apply(db,preview['token'],'admin')
        with pytest.raises(RuntimeError):people.apply(db,preview['token'],'admin')
        assert people.list_people(db)['items']==[]
        with pytest.raises(LookupError):people.person(db,pid)
        assert people.deleted_people(db)['items'][0]['id']==pid
        assert before==db.execute('SELECT * FROM people_assignments ORDER BY detection_id').fetchall()
        assert not set(ids[:2]) & {f['detection_id'] for f in people.unassigned(db)['faces']}
        assert db.execute('SELECT count(*) n FROM media').fetchone()['n']==4
    with database.connection() as db:
        act(db,'assign',detection_ids=ids[2:3],name='Later correction')
        change(db,pid,'restore')
        assert len(people.person(db,pid)['faces'])==2
        assert people.deleted_people(db)['items']==[]
        assert db.execute('SELECT count(*) n FROM people_lifecycle_events').fetchone()['n']==2

def test_deleted_name_reserved_restore_conflict_and_history(pool):
    _,ids=pool
    with database.connection() as db:
        first=act(db,'assign',detection_ids=ids[:2],name='Person');pid=first['person_id']
        change(db,pid,'delete')
        with pytest.raises(RuntimeError,match='Deleted people'):people.preview(db,dict(action='assign',name=' PERSON ',detection_ids=ids[2:3]),'admin')
        with pytest.raises(RuntimeError):people.undo(db,first['event_id'],people.revision(db),'admin')
        # Model a historical collision, without automatically merging it.
        other=db.execute("INSERT INTO people(name) VALUES('PERSON') RETURNING id").fetchone()['id']
        with pytest.raises(RuntimeError):people.preview(db,dict(action='restore',person_id=pid),'admin')
        change(db,pid,'restore',name='Person restored')
        assert people.person(db,pid)['name']=='Person restored'
        assert db.execute('SELECT name FROM people WHERE id=%s',(other,)).fetchone()['name']=='PERSON'

def test_deleted_assignments_remain_protected(pool):
    _,ids=pool
    with database.connection() as db:
        pid=act(db,'assign',detection_ids=ids[:2],name='Person')['person_id'];change(db,pid,'delete')
        for payload in [dict(action='rename',person_id=pid,name='New'),dict(action='unassign',detection_ids=ids[:1]),dict(action='assign',person_id=pid,detection_ids=ids[2:3]),dict(action='split',person_id=pid,detection_ids=ids[:1],name='Split')]:
            with pytest.raises((RuntimeError,LookupError)):people.preview(db,payload,'admin')

def test_delete_race_and_stale_preview(pool):
    _,ids=pool
    with database.connection() as db:
        pid=act(db,'assign',detection_ids=ids[:2],name='Person')['person_id']
        tokens=[people.preview(db,dict(action='delete',person_id=pid),'admin')['token'] for _ in range(2)]
    barrier=threading.Barrier(2)
    def commit(token):
        barrier.wait()
        try:
            with database.connection() as db:people.apply(db,token,'admin');return True
        except RuntimeError:return False
    with ThreadPoolExecutor(max_workers=2) as executor:assert sum(executor.map(commit,tokens))==1
    with database.connection() as db:assert db.execute('SELECT count(*) n FROM people_lifecycle_events').fetchone()['n']==1

def test_permissions_and_restore_when_photos_hidden(pool):
    client,ids=pool
    with database.connection() as db:
        pid=act(db,'assign',detection_ids=ids[:1],name='Person')['person_id'];change(db,pid,'delete')
        mid=people.eligible(db,ids[:1])[0]['media_id'];db.execute('INSERT INTO media_trash(media_id) VALUES(%s)',(mid,))
        assert people.deleted_people(db)['items'][0]['face_count']==0
        change(db,pid,'restore');assert people.list_people(db)['items']==[]
        assert people.list_people(db,True)['items'][0]['id']==pid
    client.post('/api/login',json={'username':'reader','password':'fixture'})
    assert client.get('/api/people/deleted').status_code==404
    assert client.post('/api/people/preview',json={'action':'delete','person_id':pid}).status_code==404
