"""Shared compute admission for People jobs and offline evaluations."""

from app.database.locks import PEOPLE_COMPUTE as COMPUTE_LOCK  # noqa: F401  (re-exported for callers and tests)
from app.database.locks import MEDIA_WORKERS as MAINTENANCE_LOCKS  # noqa: F401


def foreground_busy(db):
    from app.jobs.foreground import should_yield
    from app.jobs.work_admission import pending
    if pending() or should_yield():
        return True
    return bool(db.execute("SELECT 1 FROM upload_sessions WHERE state IN "
                           "('uploading','queued','importing','cancelling') LIMIT 1").fetchone())
