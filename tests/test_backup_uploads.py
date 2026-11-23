from contextlib import contextmanager
import hashlib

import pytest

from app.backup.uploads import staged_path, check_staged, reconcile_uploads
from app.backup.state_files import InvalidState
from app.backup.policy import classify_file


SESSION = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'


def test_canonical_staging_path_and_policy(tmp_path):
    path = staged_path(tmp_path, SESSION, 0, 'Folder/photo.JPG')
    assert path.relative_to(tmp_path).as_posix() == f'uploads/{SESSION}/0.jpg'
    assert classify_file('data', path.relative_to(tmp_path).as_posix()).category == 'authoritative'
    for file_id in (-1, True, '3'):
        with pytest.raises(InvalidState):
            staged_path(tmp_path, SESSION, file_id, 'photo.jpg')


def test_verified_staging_requires_exact_bytes_and_digest(tmp_path):
    path = tmp_path / 'photo.jpg'
    path.write_bytes(b'photo')
    digest = hashlib.sha256(b'photo').hexdigest()
    check_staged(path, 5, 5, digest, True)
    path.write_bytes(b'other')
    with pytest.raises(InvalidState, match='checksum'):
        check_staged(path, 5, 5, digest, True)
    path.unlink()
    with pytest.raises(InvalidState, match='missing'):
        check_staged(path, 5, 5, digest, True)


def test_partial_upload_preserves_uncommitted_tail(tmp_path):
    path = tmp_path / 'photo.jpg'
    check_staged(path, 5, 0, None, False)
    path.write_bytes(b'phot')
    check_staged(path, 5, 3, None, False)
    assert path.read_bytes() == b'phot'
    with pytest.raises(InvalidState, match='size'):
        check_staged(path, 5, 5, None, False)
    with pytest.raises(InvalidState, match='size'):
        check_staged(path, 3, 3, None, False)


class Database:
    def __init__(self, rows=(), tables=('upload_sessions', 'upload_files')):
        self.rows = rows
        self.tables = tables

    def execute(self, query):
        if query.startswith('SELECT f.'):
            assert "s.cleaned_at IS NULL AND f.disposition='accepted'" in query
        return self

    def fetchone(self):
        return self.tables

    @contextmanager
    def cursor(self, name):
        yield self

    def __iter__(self):
        return iter(self.rows)


def test_database_reconciliation_detects_missing_received_bytes(tmp_path):
    reconcile_uploads(Database(tables=(None, None)), tmp_path)
    with pytest.raises(InvalidState, match='schema'):
        reconcile_uploads(Database(tables=('upload_sessions', None)), tmp_path)
    db = Database([(SESSION, 0, 'Folder/photo.JPG', 5, 3, None, False)])
    with pytest.raises(InvalidState, match='missing'):
        reconcile_uploads(db, tmp_path)
    path = staged_path(tmp_path, SESSION, 0, 'Folder/photo.JPG')
    path.parent.mkdir(parents=True)
    path.write_bytes(b'pho')
    reconcile_uploads(db, tmp_path)
