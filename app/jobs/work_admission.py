"""Durable, explicit import handoff; stale requests never launch themselves."""
import json
import time
import uuid
from ..config import DATA
from ..telemetry import write_json

LOCK = 4340028
FACE_LOCKS = (4340020, 4340023, 4340030, 4340031)
RESERVATION_LOCKS = (4340006, 4340005, 4340002)
WAIT_SECONDS = 60
PATH = DATA / 'import-handoff.json'


def read():
    try:
        return json.loads(PATH.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}


def pending():
    try:
        request = read()
        return request.get('state') == 'pending' and time.time() < request['deadline']
    except (OSError, ValueError):
        return True  # Do not admit background work on an unreadable signal.


def snapshot():
    request = read()
    if request.get('state') == 'pending' and time.time() >= request['deadline']:
        return {**request, 'state': 'failed', 'error': 'Handoff expired; explicitly request it again'}
    return request


def reserve(db, job_id, stage):
    db.execute('SELECT pg_advisory_xact_lock(%s)', (LOCK,))
    previous = read()
    if previous.get('state') == 'pending' and time.time() < previous['deadline']:
        if (previous['job_id'], previous['stage']) == (job_id, stage):
            return previous, False
        raise RuntimeError('Another explicit import handoff is pending')
    request = dict(token=uuid.uuid4().hex, job_id=job_id, stage=stage,
                   state='pending', deadline=time.time() + WAIT_SECONDS)
    write_json(PATH, request)
    return request, True


def finish(db, token, state, error=None):
    db.execute('SELECT pg_advisory_xact_lock(%s)', (LOCK,))
    request = read()
    if request.get('token') != token or request.get('state') != 'pending':
        return
    request.update(state=state, error=error)
    write_json(PATH, request)


def validate(token):
    request = read()
    if request.get('token') != token or request.get('state') != 'pending' or time.time() >= request['deadline']:
        raise RuntimeError('Import handoff expired; explicitly request it again')
    return request


def face_owns_reservation(db):
    """Only defer contention when every blocking reservation belongs to faces."""
    return db.execute('''SELECT EXISTS(SELECT 1 FROM pg_locks
        WHERE locktype='advisory' AND classid=0 AND objid=ANY(%s) AND granted)
        AND NOT EXISTS(SELECT 1 FROM pg_locks busy
        WHERE busy.locktype='advisory' AND busy.classid=0
        AND busy.objid=ANY(%s) AND busy.granted
        AND busy.pid<>pg_backend_pid() AND NOT EXISTS(SELECT 1 FROM pg_locks face
            WHERE face.locktype='advisory' AND face.classid=0 AND face.objid=ANY(%s)
            AND face.granted AND face.pid=busy.pid)) AS available''',
        (list(FACE_LOCKS),list(RESERVATION_LOCKS),list(FACE_LOCKS))).fetchone()['available']
