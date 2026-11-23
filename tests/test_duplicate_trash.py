from datetime import datetime,timezone
from app.photos.duplicate_trash import preferred_copy, apply_group, review
from app import database
from app.photos import duplicates as photos
from app.library import trash
from test_uploads import env
from test_photo_duplicates import jpeg,seed,configure_worker


def test_keeper_metadata_conflicts_and_richness():
    def member(id,**e):return dict(id=id,created_at=datetime(2020,1,1,tzinfo=timezone.utc),captured_at=None,evidence=e)
    assert preferred_copy([member(1),member(2,camera='Canon')])['id']==2
    assert preferred_copy([member(2),member(1)])['id']==1
    assert preferred_copy([member(1,camera='Canon'),member(2,camera='Nikon')]) is None
    assert preferred_copy([member(1,date='2020:01:01'),member(2,date='2021:01:01')]) is None
    assert preferred_copy([member(1,gps='A'),member(2,gps='B')]) is None
    assert preferred_copy([member(1,camera='Canon'),member(2,date='2020:01:01')]) is None
    assert preferred_copy([member(1,date='2020:01:01 12:00:00'),member(2,date='2020-01-01T12:00:00')])['id']==1


def pair(env,monkeypatch):
    config,data=configure_worker(env,monkeypatch)
    j1,m1,p1=seed(config,jpeg(camera=''),'first')
    j2,m2,p2=seed(config,jpeg(camera='Canon'),'second')
    with database.connection() as db:
        db.execute('UPDATE media SET first_import_job_id=%s WHERE id=%s',(j1,m2))
    photos.run(j1)
    with database.connection() as db:key=photos.groups(db,j1)['groups'][0]['key']
    return config,data,j1,m1,m2,p1,p2,key


def test_reversible_atomic_trash_and_restore_protection(env,monkeypatch):
    config,data,job,m1,m2,p1,p2,key=pair(env,monkeypatch)
    before=(p1.read_bytes(),p2.read_bytes())
    with database.connection() as db:
        assert apply_group(db,job,key)==1
        assert db.execute('SELECT media_id FROM media_trash').fetchone()['media_id']==m1
        assert review(db,job)['items'][0]['preferred_id']==m2
    assert (p1.read_bytes(),p2.read_bytes())==before
    with database.connection() as db:assert apply_group(db,job,key)==0
    with database.connection() as db:trash.change(db,m1,'restore',config)
    with database.connection() as db:
        assert apply_group(db,job,key)==0
        assert review(db,job)['total']==0
        assert photos.groups(db,job)['total']==1
        assert db.execute('SELECT count(*) n FROM media').fetchone()['n']==2


def test_manual_separation_stale_and_edited_members_are_protected(env,monkeypatch):
    config,data,job,m1,m2,p1,p2,key=pair(env,monkeypatch)
    with database.connection() as db:
        photos.decide(db,job,key,'separate',m1,0,[m1,m2])
    with database.connection() as db:assert apply_group(db,job,key)==0
    with database.connection() as db:
        db.execute('DELETE FROM photo_duplicate_decisions')
        db.execute("UPDATE photo_fingerprints SET stored_sha256='stale' WHERE media_id=%s",(m1,))
        assert apply_group(db,job,key)==0
        db.execute('UPDATE photo_fingerprints SET stored_sha256=(SELECT sha256 FROM media WHERE id=%s) WHERE media_id=%s',(m1,m1))
        db.execute('INSERT INTO media_adjustments(media_id,rotation) VALUES(%s,90)',(m1,))
        assert apply_group(db,job,key)==0


def test_worker_processes_existing_matches_when_enabled(env,monkeypatch):
    config,data,job,m1,m2,p1,p2,key=pair(env,monkeypatch)
    with database.connection() as db:db.execute('UPDATE jobs SET auto_trash_duplicates=true WHERE id=%s',(job,))
    photos.run(job)
    with database.connection() as db:
        assert review(db,job)['total']==1
        assert photos.groups(db,job)['total']==0
    assert p1.exists() and p2.exists()


def test_manual_preference_is_honored(env,monkeypatch):
    config,data,job,m1,m2,p1,p2,key=pair(env,monkeypatch)
    with database.connection() as db:
        photos.decide(db,job,key,'confirmed',m1,0,[m1,m2])
    with database.connection() as db:
        assert apply_group(db,job,key)==1
        item=review(db,job)['items'][0]
        assert item['id']==m2 and item['preferred_id']==m1
        assert 'manually confirmed' in item['reason']


def test_other_import_exclusive_member_is_not_trashed(env,monkeypatch):
    config,data,job,m1,m2,p1,p2,key=pair(env,monkeypatch)
    with database.connection() as db:
        # Seed does not need queue rows; a separate completed job is sufficient.
        other_job=db.execute('SELECT id FROM jobs WHERE id<>%s ORDER BY id LIMIT 1',(job,)).fetchone()['id']
        db.execute('UPDATE media SET first_import_job_id=%s WHERE id=%s',(other_job,m1))
        db.execute('DELETE FROM import_items WHERE job_id=%s AND media_id=%s',(job,m1))
    with database.connection() as db:
        assert apply_group(db,job,key)==0
        assert review(db,job)['total']==0


def test_different_capture_dates_require_review(env,monkeypatch):
    config,data=configure_worker(env,monkeypatch)
    job,m1,p1=seed(config,jpeg(date='2000:01:01'),'older')
    _,m2,p2=seed(config,jpeg(date='2020:01:01'),'newer')
    with database.connection() as db:
        db.execute('UPDATE media SET first_import_job_id=%s WHERE id=%s',(job,m2))
        db.execute('UPDATE jobs SET auto_trash_duplicates=true WHERE id=%s',(job,))
    photos.run(job)
    with database.connection() as db:
        assert review(db,job)['total']==0
        assert photos.groups(db,job)['total']==1
