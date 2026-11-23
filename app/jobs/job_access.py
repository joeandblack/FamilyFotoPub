"""Upload batches belong to the person who uploaded them; administrators see all."""
import re
from ..database import connection
from .. import viewer

JOB_PATH = re.compile(r'^/api/import/(\d+)(/|$)')
UPLOAD_VISIBLE = "(j.source_kind<>'upload' OR u.owner_name=%s)"


def job_in_path(path):
    match = JOB_PATH.match(path)
    return int(match[1]) if match else None


def can_access(user, job_id):
    """False only for another person's upload batch; unknown ids fall through to a 404."""
    if user['role'] == 'admin':
        return True
    with connection() as db:
        row = db.execute('SELECT j.source_kind,u.owner_name FROM jobs j LEFT JOIN upload_sessions u ON u.job_id=j.id WHERE j.id=%s', (job_id,)).fetchone()
    return row is None or row['source_kind'] != 'upload' or row['owner_name'] == user['username']


def visible_job(db, job_id):
    """The job row id if the current viewer may see it, else None."""
    hide, values = upload_clause()
    return db.execute('SELECT j.id FROM jobs j LEFT JOIN upload_sessions u ON u.job_id=j.id WHERE j.id=%s' + (' AND ' + hide if hide else ''), (job_id, *values)).fetchone()


def hide_other_uploads(db, status, key='job_id'):
    """Blank file names in a progress snapshot of someone else's upload batch."""
    if not status or status.get(key) is None or viewer.current() is None or visible_job(db, status[key]):
        return
    for name in ('current', 'current_size', 'current_file', 'source_root', 'label'):
        if name in status:
            status[name] = None


def upload_clause():
    """(sql, params) hiding other people's upload batches from the current viewer.

    The SQL needs `jobs j LEFT JOIN upload_sessions u ON u.job_id=j.id`.
    """
    who = viewer.current()
    return ('', ()) if who is None else (UPLOAD_VISIBLE, (who,))
