"""Reconcile retained upload bytes against the frozen catalog, without app config."""
import hashlib
from pathlib import Path, PurePosixPath
import re
import stat
import uuid

from .space import directory_identity
from .state_files import InvalidState


def staged_path(data_root, session_id, file_id, original_path):
    if type(file_id) is not int or file_id < 0:
        raise InvalidState('Invalid upload file identity')
    suffix = PurePosixPath(original_path).suffix.lower()
    if not re.fullmatch(r'\.[a-z0-9]{1,12}', suffix):
        raise InvalidState('Invalid staged upload extension')
    return Path(data_root) / 'uploads' / str(uuid.UUID(str(session_id))) / (str(file_id) + suffix)


def check_staged(path, size, received, digest, verified):
    if type(size) is not int or type(received) is not int or not 0 <= received <= size:
        raise InvalidState('Invalid received upload size')
    if verified and (received != size or not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest)):
        raise InvalidState('Invalid verified upload record')
    if not path.exists() and not path.is_symlink():
        if received or verified:
            raise InvalidState('Recorded upload bytes are missing')
        return
    directory_identity(path.parent)
    before = path.lstat()
    if path.is_symlink() or path.is_junction() or not stat.S_ISREG(before.st_mode):
        raise InvalidState('Unsafe upload staging entry')
    if not received <= before.st_size <= size or (verified and before.st_size != size):
        raise InvalidState('Upload staging size differs from recorded bytes')
    if verified:
        with path.open('rb') as stream:
            observed = hashlib.file_digest(stream, 'sha256').hexdigest()
        after = path.stat()
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise InvalidState('Upload staging changed during reconciliation')
        if observed != digest:
            raise InvalidState('Verified upload checksum differs from staged file')


def reconcile_uploads(db, data_root):
    """Caller holds database freeze and DATA admission; no upload is modified."""
    sessions, files = db.execute("SELECT to_regclass('public.upload_sessions'), "
                                 "to_regclass('public.upload_files')").fetchone()
    if not sessions and not files:
        return
    if not sessions or not files:
        raise InvalidState('Incomplete upload schema prevents reconciliation')
    with db.cursor(name='backup_upload_reconciliation') as rows:
        rows.itersize = 1000
        rows.execute('SELECT f.session_id,f.file_id,f.relative_path,f.size,f.received,f.sha256,f.verified '
                     'FROM public.upload_files f JOIN public.upload_sessions s ON s.id=f.session_id '
                     "WHERE s.cleaned_at IS NULL AND f.disposition='accepted'")
        for session, file_id, original, size, received, digest, verified in rows:
            check_staged(staged_path(data_root, session, file_id, original), size, received, digest, verified)
