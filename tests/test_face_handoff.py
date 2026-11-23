"""Isolated admission tests; no subprocesses, catalog or original media."""
from contextlib import contextmanager
import os
import uuid
import pytest
from app import jobs as pipeline
from app.jobs import work_admission as admission


class DB:
    def execute(self, sql, params=()):
        self.sql = sql
        return self

    def fetchone(self):
        if 'FROM jobs' in self.sql:
            return {'id': 7, 'state': 'paused', 'source_kind': 'folder'}
        return {'available': True}

    def commit(self):
        pass


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(admission, 'PATH', tmp_path / 'handoff.json')
    monkeypatch.setattr(pipeline, 'DATA', tmp_path)
    db = DB()
    @contextmanager
    def connection():
        yield db
    monkeypatch.setattr(pipeline, 'connection', connection)
    return db


def test_durable_scope_duplicate_expiry_and_fencing(isolated, monkeypatch):
    monkeypatch.setattr(admission.time, 'time', lambda: 100)
    first, created = admission.reserve(isolated, 7, 'import')
    assert created and admission.pending()
    assert admission.reserve(isolated, 7, 'import') == (first, False)
    with pytest.raises(RuntimeError):
        admission.reserve(isolated, 8, 'import')
    monkeypatch.setattr(admission.time, 'time', lambda: 161)
    assert not admission.pending()
    assert admission.snapshot()['state'] == 'failed'
    with pytest.raises(RuntimeError):
        admission.validate(first['token'])
    second, created = admission.reserve(isolated, 8, 'import')
    admission.finish(isolated, first['token'], 'started')
    assert admission.read() == second


def test_unreadable_signal_blocks_faces(isolated):
    admission.PATH.write_text('{broken', encoding='utf-8')
    assert admission.pending()


def test_pause_cancels_pending_request_without_touching_worker(isolated):
    request, _ = admission.reserve(isolated, 7, 'import')
    assert 'cancelled' in pipeline.pause(7)['message']
    assert not admission.pending()
    with pytest.raises(RuntimeError):
        admission.validate(request['token'])


def test_only_face_contention_queues_and_repeated_click_reuses(isolated, monkeypatch):
    def busy(db):
        raise pipeline.ReservationBusy('busy')
    monkeypatch.setattr(pipeline, 'idle', busy)
    monkeypatch.setattr(admission, 'face_owns_reservation', lambda db: True)
    launched = []
    class Thread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
        def start(self):
            launched.append(self.kwargs)
    monkeypatch.setattr(pipeline.threading, 'Thread', Thread)
    first = pipeline.launch(7, 'import')
    second = pipeline.launch(7, 'import')
    assert first['handoff']['token'] == second['handoff']['token']
    assert len(launched) == 1
    monkeypatch.setattr(admission, 'face_owns_reservation', lambda db: False)
    with pytest.raises(pipeline.ReservationBusy):
        pipeline.launch(7, 'import')


def test_photo_scan_yields_to_explicit_import(isolated,monkeypatch):
    import json
    def busy(db):raise pipeline.ReservationBusy('busy')
    monkeypatch.setattr(pipeline,'idle',busy)
    monkeypatch.setattr(admission,'face_owns_reservation',lambda db:False)
    (pipeline.DATA/'pipeline-status.json').write_text(json.dumps({'stage':'duplicates','state':'running'}))
    monkeypatch.setattr(pipeline.threading.Thread,'start',lambda self:None)
    result=pipeline.launch(7,'import')
    assert result['handoff']['job_id']==7
    assert admission.pending()


def test_decoder_fault_not_queued(isolated, monkeypatch):
    def fault(db):
        raise RuntimeError('Decoder lease requires review')
    monkeypatch.setattr(pipeline, 'idle', fault)
    monkeypatch.setattr(admission, 'face_owns_reservation', lambda db: True)
    with pytest.raises(RuntimeError, match='Decoder lease'):
        pipeline.launch(7, 'import')
    assert not admission.pending()


def test_waiter_retries_only_reservation_and_keeps_signal_until_process_owns_it(isolated, monkeypatch):
    request, _ = admission.reserve(isolated, 7, 'import')
    calls = []
    def launch(job, stage, **kwargs):
        calls.append((job, stage, kwargs))
        if len(calls) == 1:
            raise pipeline.HandoffBusy('current face')
    monkeypatch.setattr(pipeline, 'launch', launch)
    monkeypatch.setattr(pipeline.time, 'sleep', lambda seconds: None)
    pipeline._wait_handoff(request)
    assert len(calls) == 2
    assert admission.pending()
    admission.finish(isolated, request['token'], 'started')
    assert not admission.pending()


def test_waiter_disk_failure_is_terminal_and_explicit(isolated, monkeypatch):
    request, _ = admission.reserve(isolated, 7, 'import')
    def launch(*args, **kwargs):
        raise OSError('Destination missing')
    monkeypatch.setattr(pipeline, 'launch', launch)
    pipeline._wait_handoff(request)
    assert admission.read()['state'] == 'failed'
    assert admission.read()['error'] == 'Destination missing'
    assert not admission.pending()


def test_idle_face_worker_still_reserves_subprocess_handoff(isolated, monkeypatch):
    monkeypatch.setattr(pipeline, 'idle', lambda db: None)
    monkeypatch.setattr(admission, 'face_owns_reservation', lambda db: True)
    queued = []
    monkeypatch.setattr(pipeline, '_queue_handoff', lambda db, job, stage: queued.append((job, stage)))
    pipeline.launch(7, 'import')
    assert queued == [(7, 'import')]


def test_expired_or_cancelled_request_cannot_start_pipeline(isolated, monkeypatch):
    request, _ = admission.reserve(isolated, 7, 'import')
    admission.finish(isolated, request['token'], 'cancelled')
    monkeypatch.setattr(pipeline, 'idle', lambda db: None)
    with pytest.raises(RuntimeError, match='expired'):
        pipeline.run(7, 'import', request['token'])


def test_inline_upload_never_creates_waiter(isolated, monkeypatch):
    def busy(db):
        raise pipeline.ReservationBusy('busy')
    monkeypatch.setattr(pipeline, 'idle', busy)
    monkeypatch.setattr(admission, 'face_owns_reservation', lambda db: True)
    with pytest.raises(pipeline.ReservationBusy):
        pipeline.launch(7, 'import', inline=True)
    assert not admission.pending()


@pytest.mark.skipif(os.environ.get('FAMILYFOTO_INTEGRATION')!='1', reason='Requires local PostgreSQL')
def test_real_lock_ownership_distinguishes_face_child_and_other_work(monkeypatch):
    from app.database import connection
    keys=[1_000_000_000 + uuid.uuid4().int % 900_000_000 for _ in range(5)]
    monkeypatch.setattr(admission,'FACE_LOCKS',tuple(keys[:2]))
    monkeypatch.setattr(admission,'RESERVATION_LOCKS',tuple(keys[2:]))
    with connection() as face, connection() as child, connection() as other, connection() as caller:
        assert not admission.face_owns_reservation(caller)
        face.execute('SELECT pg_advisory_lock(%s)',(keys[0],))
        child.execute('SELECT pg_advisory_lock_shared(%s)',(keys[1],))
        for db in (face,child):
            db.execute('SELECT pg_advisory_lock_shared(%s)',(keys[2],))
        assert admission.face_owns_reservation(caller)
        other.execute('SELECT pg_advisory_lock(%s)',(keys[3],))
        assert not admission.face_owns_reservation(caller)
