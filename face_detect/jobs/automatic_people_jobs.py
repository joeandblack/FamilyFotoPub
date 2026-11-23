"""Disabled explicit worker slices over EvaluationJob; no daemon or second queue.

The caller supplies a trusted prepared-input resolver/provider/loader. Production
import selection and launch registration are deliberately absent. All worker slices
share existing People compute/maintenance admission; control and receipt commit
serialize through the existing People transaction lock.
"""
from contextlib import contextmanager, nullcontext
import json
import re
import time
from app.database.locks import PEOPLE_BACKGROUND
from app.database.locks import PEOPLE_MANUAL_PENDING
from face_detect.recognition import automatic_people as service
from app import database
from face_detect.recognition import people
from face_detect.jobs.automatic_people_handoff import accept_checkpoint
from face_detect.jobs.automatic_people_handoff import receipt_id
from face_detect.jobs.people_compute import COMPUTE_LOCK
from face_detect.jobs.people_compute import MAINTENANCE_LOCKS
from face_detect.jobs.checkpoint import EvaluationJob
from face_detect.jobs.checkpoint import Excluded
from face_detect.contracts import ProviderTransient

CHILD_LOCKS = (4340023, 4340031)


def foreground_busy(db):
    """Explicit scans yield only to operation handoffs/uploads, never browsing."""
    from app.jobs.work_admission import pending
    if pending():return True
    if db.execute("""SELECT 1 FROM pg_locks WHERE locktype='advisory' AND classid=0
        AND objid=ANY(%s) AND granted AND mode='ExclusiveLock' AND pid<>pg_backend_pid()
        LIMIT 1""",(list(MAINTENANCE_LOCKS),)).fetchone():return True
    return bool(db.execute("SELECT 1 FROM upload_sessions WHERE state IN ('uploading','queued','importing','cancelling') LIMIT 1").fetchone())


def authorize(user, mutate=True):
    service.guard()
    if not user or not user.get('username') or user.get('role') not in ('admin', 'regular'):
        raise PermissionError('Authentication required')
    if mutate and user['role'] != 'admin':
        raise PermissionError('Administrator authorization required')


@contextmanager
def worker_lease(*, maintenance=True):
    # Session locks are released even after a crash when the connection closes.
    with database.connection() as db:
        key = COMPUTE_LOCK if maintenance else PEOPLE_BACKGROUND
        if not db.execute('SELECT pg_try_advisory_lock(%s) ok', (key,)).fetchone()['ok']:
            raise RuntimeError('Another biometric worker owns compute')
        for key in (*(MAINTENANCE_LOCKS if maintenance else ()), *CHILD_LOCKS):
            if not db.execute('SELECT pg_try_advisory_lock_shared(%s) ok', (key,)).fetchone()['ok']:
                raise RuntimeError('Maintenance or child reservation unavailable')
        yield db


@contextmanager
def manual_lease(progress):
    """Announce manual demand before waiting for the current photo checkpoint."""
    with database.connection() as db:
        db.execute('SELECT pg_advisory_lock_shared(%s)', (PEOPLE_MANUAL_PENDING,))
        progress('waiting', 0, None)
        while not db.execute('SELECT pg_try_advisory_lock(%s) ok', (COMPUTE_LOCK,)).fetchone()['ok']:
            time.sleep(.25)
        for key in (*MAINTENANCE_LOCKS, *CHILD_LOCKS):
            db.execute('SELECT pg_advisory_lock_shared(%s)', (key,))
        yield db


@contextmanager
def background_unit():
    """Reserve only one photo; closing releases its content and maintenance locks."""
    with database.connection() as db:
        if db.execute("SELECT 1 FROM pg_locks WHERE locktype='advisory' AND classid=0 AND objid=%s AND granted LIMIT 1",
                      (PEOPLE_MANUAL_PENDING,)).fetchone():
            yield None
            return
        if not db.execute('SELECT pg_try_advisory_lock(%s) ok', (COMPUTE_LOCK,)).fetchone()['ok']:
            yield None
            return
        acquired=[]
        for key in (*MAINTENANCE_LOCKS,*CHILD_LOCKS):
            if not db.execute('SELECT pg_try_advisory_lock_shared(%s) ok',(key,)).fetchone()['ok']:
                for held in reversed(acquired):
                    db.execute('SELECT pg_advisory_unlock_shared(%s)',(held,))
                yield None
                return
            acquired.append(key)
        if foreground_busy(db):
            for held in reversed(acquired):
                db.execute('SELECT pg_advisory_unlock_shared(%s)',(held,))
            yield None
        else:yield db


class Jobs:
    def __init__(self, root, provider, observer, load):
        self.root, self.provider, self.observer, self.load = root, provider, observer, load

    def job(self, name):
        if not re.fullmatch('[a-z0-9-]{1,64}', name):
            raise ValueError('Safe job identifier required')
        return EvaluationJob(self.root / name)

    def create(self, name, items, user):
        authorize(user)
        # mkdir(exist_ok=False) is the durable duplicate-creation guard.
        job = self.job(name)
        with database.connection() as db:
            people._lock(db)
            job.create(items, self.provider.identity, space_id=self.provider.space_id)
        return self.status(name, user)

    def status(self, name, user):
        authorize(user, False)
        job = self.job(name); state = job.read()
        with database.connection() as db:
            delivered = db.execute('SELECT result FROM auto_people_receipts WHERE id=%s', (receipt_id(state),)).fetchone()
        control = json.loads(job.control.read_text())['action']
        stage = state['state']
        if delivered: stage = 'completed'
        elif control == 'cancel': stage = 'cancelled'
        elif control == 'pause': stage = 'paused'
        elif stage == 'completed': stage = 'awaiting-receipt'
        return {'id': name, 'state': stage, 'total': len(state['items']),
                'done': sum(r['state'] == 'done' for r in state['items']),
                'excluded': sum(r['state'] == 'excluded' for r in state['items']),
                'receipt': delivered['result'] if delivered else None}

    def control(self, name, action, user):
        authorize(user)
        if action not in ('pause', 'cancel', 'resume'): raise ValueError('Unknown control')
        job = self.job(name)
        with database.connection() as db:
            people._lock(db)
            status = self.status(name, user)
            if status['state'] in ('completed', 'cancelled'):
                raise RuntimeError('Job is terminal')
            job.command('run' if action == 'resume' else action)
        return self.status(name, user)

    def run(self, name, user, max_items=2):
        authorize(user)
        job = self.job(name)
        with worker_lease() as lease:
            if self.status(name, user)['state'] in ('completed', 'cancelled'):
                return self.status(name, user)

            @contextmanager
            def checked_load(item):
                if foreground_busy(lease): raise ProviderTransient('Foreground work active')
                def eligible():
                    with database.connection() as db:
                        current = service.rows(db, [item['id']])
                    if not current or current[0]['media_id'] != item['media_id'] or current[0]['content_hash'] != item['content_hash']:
                        raise Excluded('Source no longer eligible')
                eligible()
                with self.load(item) as value:
                    yield value
                eligible()

            # Once inference completes, never rewrite its immutable payload merely
            # to deliver again after a crash between DB commit and local acknowledgement.
            state = job.read()
            if state['state'] != 'completed':
                state = job.run(self.provider, self.observer, checked_load, nullcontext, max_items=max_items)
            if state['state'] == 'completed':
                with database.connection() as db:
                    people._lock(db)
                    if json.loads(job.control.read_text())['action'] != 'run' or foreground_busy(lease):
                        return self.status(name, user)
                    accept_checkpoint(db, state, user, self.observer, provider=self.provider)
                # The DB receipt is the completion acknowledgement. A crash after
                # commit needs no second file write and cannot lose the outcome.
        return self.status(name, user)
