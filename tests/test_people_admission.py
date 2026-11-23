"""Cross-provider compute leases tested against isolated fixture database."""
import uuid
import pytest
from test_uploads import env
from app import database
from app.jobs import work_admission

def test_import_handoff_recognizes_people_reservations(env, monkeypatch):
    worker,child,reservation=[uuid.uuid4().int % (2**31-1) for _ in range(3)]
    monkeypatch.setattr(work_admission,'FACE_LOCKS',(worker,child))
    monkeypatch.setattr(work_admission,'RESERVATION_LOCKS',(reservation,))
    with database.connection() as owner, database.connection() as probe:
        owner.execute('SELECT pg_advisory_lock(%s)',(worker,))
        owner.execute('SELECT pg_advisory_lock_shared(%s)',(reservation,))
        assert work_admission.face_owns_reservation(probe)
