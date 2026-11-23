from concurrent.futures import ThreadPoolExecutor
import threading
import pytest
from test_people_service import pool,act
from test_uploads import env
from app import database
from face_detect.recognition import people

@pytest.mark.parametrize('variant',[' mEiLiN ', '\uff2d\uff45\uff49\uff4c\uff49\uff4e', 'MEILIN'])
def test_normalized_duplicate_creation(pool,variant):
    _,ids=pool
    with database.connection() as db:
        act(db,'assign',detection_ids=ids[:1],name='Meilin')
        with pytest.raises(RuntimeError,match='already exists.*Existing person'):
            people.preview(db,dict(action='assign',detection_ids=ids[1:2],name=variant),'admin')
        assert db.execute('SELECT count(*) n FROM people').fetchone()['n']==1

def test_unicode_whitespace_and_casefold():
    assert people.name_key('  Anne\u00a0  Marie ')==people.name_key('anne marie')
    assert people.name_key('Stra\u00dfe')==people.name_key('STRASSE')
    assert people.name_key('Jose\u0301')==people.name_key('Jos\u00e9')

def test_rename_split_and_legacy_duplicates(pool):
    _,ids=pool
    with database.connection() as db:
        a=act(db,'assign',detection_ids=ids[:2],name='Meilin')['person_id']
        b=act(db,'assign',detection_ids=ids[2:3],name='Other')['person_id']
        for payload in [dict(action='rename',person_id=b,name=' MEILIN '),dict(action='split',person_id=a,detection_ids=ids[:1],name='meilin')]:
            with pytest.raises(RuntimeError,match='already exists'):people.preview(db,payload,'admin')
        # Existing historical collisions remain unchanged and can be disambiguated.
        db.execute("UPDATE people SET name='Meilin' WHERE id=%s",(b,))
        act(db,'rename',person_id=a,name=' MEILIN ')
        with pytest.raises(RuntimeError,match='already exists'):people.preview(db,dict(action='assign',name='meilin',detection_ids=ids[3:]),'admin')
        act(db,'rename',person_id=b,name='Different person')
        assert db.execute('SELECT count(*) n FROM people').fetchone()['n']==2
        assert db.execute('SELECT count(*) n FROM people_assignments').fetchone()['n']==3

def test_competing_previews_and_duplicate_apply(pool):
    _,ids=pool
    with database.connection() as db:
        previews=[people.preview(db,dict(action='assign',name=n,detection_ids=[i]),'admin') for n,i in zip(['Meilin',' meilin '],ids)]
    barrier=threading.Barrier(2)
    def commit(token):
        barrier.wait()
        try:
            with database.connection() as db:return people.apply(db,token,'admin')
        except RuntimeError as e:return str(e)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results=list(executor.map(commit,[p['token'] for p in previews]))
    assert sum(isinstance(r,dict) for r in results)==1
    assert any('already exists' in r for r in results if isinstance(r,str))
    with database.connection() as db:
        assert db.execute('SELECT count(*) n FROM people').fetchone()['n']==1
        assert db.execute('SELECT count(*) n FROM people_assignments').fetchone()['n']==1
        for p in previews:
            with pytest.raises(RuntimeError):people.apply(db,p['token'],'admin')
