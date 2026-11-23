"""Discover People releases foreground reservations between photos."""
import uuid
from test_uploads import env
from test_unexamined_photos import disposable_migrations
from app import database
from face_detect.jobs import automatic_people_jobs as workers
from app.jobs import work_admission
from concurrent.futures import ThreadPoolExecutor
from threading import Event


def test_background_yields_and_reacquires_after_foreground(env,monkeypatch,tmp_path):
    keys=[uuid.uuid4().int % (2**31-1) for _ in range(6)]
    monkeypatch.setattr(workers,'COMPUTE_LOCK',keys[0])
    monkeypatch.setattr(workers,'MAINTENANCE_LOCKS',tuple(keys[1:4]))
    monkeypatch.setattr(workers,'CHILD_LOCKS',tuple(keys[4:]))
    monkeypatch.setattr(work_admission,'PATH',tmp_path/'handoff.json')
    with workers.worker_lease(maintenance=False),database.connection() as foreground:
        with workers.background_unit() as unit:
            assert unit is not None
            assert not foreground.execute('SELECT pg_try_advisory_lock(%s) ok',(keys[1],)).fetchone()['ok']
        # Background stays alive, but foreground can now own every reservation.
        for key in keys[1:4]:
            assert foreground.execute('SELECT pg_try_advisory_lock(%s) ok',(key,)).fetchone()['ok']
        with workers.background_unit() as unit:assert unit is None
        foreground.execute('SELECT pg_advisory_unlock_all()')
        request,_=work_admission.reserve(foreground,4,'conversion');foreground.commit()
        with workers.background_unit() as unit:assert unit is None
        work_admission.finish(foreground,request['token'],'started');foreground.commit()
        with workers.background_unit() as unit:assert unit is not None


def test_partial_reservations_released_on_contention(env,monkeypatch,tmp_path):
    keys=[uuid.uuid4().int % (2**31-1) for _ in range(3)]
    monkeypatch.setattr(workers,'MAINTENANCE_LOCKS',tuple(keys))
    monkeypatch.setattr(work_admission,'PATH',tmp_path/'handoff.json')
    with database.connection() as foreground:
        foreground.execute('SELECT pg_advisory_lock(%s)',(keys[1],))
        with workers.background_unit() as unit:
            assert unit is None
            assert foreground.execute('SELECT pg_try_advisory_lock(%s) ok',(keys[0],)).fetchone()['ok']


def test_manual_recheck_waits_for_photo_then_background_resumes(env,monkeypatch,tmp_path):
    keys=[uuid.uuid4().int % (2**31-1) for _ in range(7)]
    monkeypatch.setattr(workers,'COMPUTE_LOCK',keys[0])
    monkeypatch.setattr(workers,'PEOPLE_BACKGROUND',keys[1])
    monkeypatch.setattr(workers,'PEOPLE_MANUAL_PENDING',keys[2])
    monkeypatch.setattr(workers,'MAINTENANCE_LOCKS',(keys[3],))
    monkeypatch.setattr(workers,'CHILD_LOCKS',tuple(keys[4:]))
    monkeypatch.setattr(work_admission,'PATH',tmp_path/'handoff.json')
    waiting,admitted,release=Event(),Event(),Event()
    def manual():
        with workers.manual_lease(lambda *args: waiting.set()):
            admitted.set()
            assert release.wait(10)
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            with workers.worker_lease(maintenance=False):
                with workers.background_unit() as unit:
                    assert unit is not None
                    future=pool.submit(manual)
                    assert waiting.wait(5)
                    assert not admitted.is_set()
                    with workers.background_unit() as blocked:assert blocked is None
                assert admitted.wait(5)
                with workers.background_unit() as blocked:assert blocked is None
                release.set();future.result(timeout=5)
                with workers.background_unit() as resumed:assert resumed is not None
        finally:release.set()
