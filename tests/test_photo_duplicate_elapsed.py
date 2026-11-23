from types import SimpleNamespace
import pytest
from psycopg.types.json import Jsonb
from app import database
from app.photos import duplicates as photos
from test_uploads import env
from test_unexamined_photos import disposable_migrations
from test_photo_duplicates import seed, jpeg, configure_worker


@pytest.mark.parametrize('status,expected',[
    ({},0),({'run_seconds':1020},1020),
    ({'active_seconds':1020,'run_seconds':1020},1020),
    ({'active_seconds':None,'run_seconds':1020},1020),
    ({'active_seconds':0,'run_seconds':1020},1020),
    ({'active_seconds':float('nan'),'run_seconds':-1},0),
])
def test_resume_checkpoint_compatibility(status,expected):
    assert photos.saved_active_seconds(status)==expected


def test_pause_resume_accumulates_without_paused_gap(env,monkeypatch):
    config,data=configure_worker(env,monkeypatch)
    job,_,_=seed(config,jpeg(),'elapsed')
    with database.connection() as db:
        db.execute('UPDATE jobs SET duplicates_status=%s WHERE id=%s',
                   (Jsonb({'state':'paused','run_seconds':1020}),job))
    clock={'now':1000,'work':60}
    monkeypatch.setattr(photos,'time',SimpleNamespace(monotonic=lambda:clock['now']))
    def prepare(self):clock['now']+=clock['work']
    monkeypatch.setattr(photos.Monitor,'prepare',prepare)
    (data/'PHOTO-DUPLICATE-PAUSE').touch()
    photos.run(job)
    with database.connection() as db:
        status=db.execute('SELECT duplicates_status FROM jobs WHERE id=%s',(job,)).fetchone()['duplicates_status']
        assert status['active_seconds']==status['run_seconds']==1080
    clock.update(now=100000,work=120)
    photos.run(job)
    with database.connection() as db:
        status=db.execute('SELECT duplicates_status FROM jobs WHERE id=%s',(job,)).fetchone()['duplicates_status']
        assert status['state']=='paused'
        assert status['job_id']==job
        assert status['active_seconds']==status['run_seconds']==1200
