"""Disposable-schema migrations must not contend with live media workers."""
import uuid
import pytest
from face_detect.recognition import people
from app.database import schema_upgrading
from pathlib import Path
import time
import re


def admit_empty_test_schema(db, applied, pending):
    """Only initialize empty, UUID-named fixture schemas; never waive upgrades."""
    row = db.execute('SELECT current_schema() AS schema, current_schemas(false) AS schemas').fetchone()
    schema = row['schema']
    if (not applied and isinstance(schema, str)
            and re.fullmatch(r'(?:fftest_|uploadtest_|trash_test_|mactest_|runtimetest_)[0-9a-f]{32}', schema)
            and row['schemas'] == [schema]):
        exists = db.execute(
            'SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_class c '
            'JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace '
            'WHERE n.nspname=%s) AS present', (schema,)).fetchone()['present']
        if not exists:
            return
    ORIGINAL_BACKUP_REQUIREMENT(db, applied, pending)


ORIGINAL_BACKUP_REQUIREMENT = schema_upgrading.require_catalog_backup


def pytest_configure(config):
    config.addinivalue_line('markers', 'real_scrypt: run the real backup-password scrypt profile')
    # Concurrent local test commands must not clear another command's fixtures.
    if config.option.basetemp and Path(config.option.basetemp).resolve() == Path('temp/tests').resolve():
        config.option.basetemp = str(Path('temp/test-runs') / uuid.uuid4().hex)
    # The configured parent is ignored by Git and absent in fresh checkouts.
    # Pytest creates/owns basetemp itself; only prepare its parent directory.
    if config.option.basetemp:
        Path(config.option.basetemp).parent.mkdir(parents=True, exist_ok=True)


@pytest.fixture(autouse=True)
def isolated_migration_worker_locks(monkeypatch, tmp_path):
    # CI also has a populated public catalog for pipeline tests. It must not make
    # a separate, empty fixture schema look like an existing catalog upgrade.
    monkeypatch.setattr(schema_upgrading, 'require_catalog_backup', admit_empty_test_schema)
    monkeypatch.setattr('app.jobs.foreground.MARKER', tmp_path / 'foreground-activity')
    monkeypatch.setattr('app.jobs.work_admission.PATH', tmp_path / 'import-handoff.json')
    # A stale runtime/macos-stop left by a real shutdown must not block fixtures.
    monkeypatch.setattr('app.service.macos_lifecycle.STOP', tmp_path / 'macos-stop')
    monkeypatch.setattr(people, 'CORRECTION_LOCK', uuid.uuid4().int % (2**63-1))
    monkeypatch.setattr('app.auth.store.LOGIN_ADMISSION', uuid.uuid4().int % (2**63-1))
    monkeypatch.setattr(schema_upgrading,'MIGRATION_WORKER_LOCKS',
                        tuple(uuid.uuid4().int % (2**63-1) for _ in range(3)))
    for name in ('MIGRATION_LOCK','MIGRATION_FACE_LOCKS','MIGRATION_PEOPLE_LOCKS','MIGRATION_REVIEW_LOCKS'):
        value=getattr(schema_upgrading,name)
        monkeypatch.setattr(schema_upgrading,name,
            tuple(uuid.uuid4().int % (2**31-1) for _ in value) if isinstance(value,tuple)
            else uuid.uuid4().int % (2**31-1))
    # Never start a production queue worker from a fixture TestClient lifespan.
    import threading
    monkeypatch.setattr('app.uploads.start_dispatcher',lambda:threading.Event())


@pytest.fixture(autouse=True)
def quick_backup_password_kdf(request, monkeypatch):
    """Backup tests make hundreds of password contexts. Outside the password tests, which are
    marked real_scrypt, swap scrypt (about 0.3 s and 128 MiB each) for a keyed hash of the same
    password and salt, so keys still differ by password and salt but cost nothing."""
    if request.node.get_closest_marker('real_scrypt'):
        return
    import base64
    import hashlib
    from app.backup import passwords

    def derive(password, salt):
        if not isinstance(password, passwords.BackupPassword):
            raise TypeError('derive() takes a BackupPassword')
        return base64.urlsafe_b64encode(hashlib.sha256(b'test-only-kdf' + salt + password.encoded).digest())
    monkeypatch.setattr(passwords, 'derive', derive)


@pytest.fixture
def auth_memory(monkeypatch):
    """Explicit unit-test double; integration tests use the real PostgreSQL store."""
    from app import auth
    class Store:
        def __init__(self):
            self.rows = {}
            self.retry = 0
        def create(self, digest, username, signature, expires):
            self.rows[digest] = {'username': username, 'credential_signature': signature, 'expires': expires}
        def get(self, digest):
            row = self.rows.get(digest)
            return row if row and row['expires'] > time.time() else None
        def revoke(self, digest):
            self.rows.pop(digest, None)
        def revoke_user(self, username):
            keys = [key for key, row in self.rows.items() if row['username'] == username]
            for key in keys:
                del self.rows[key]
            return len(keys)
        def admit_login(self, account_key, address_key):
            return self.retry
    store = Store()
    monkeypatch.setattr(auth, 'SessionStore', lambda config: store)
    return store


@pytest.fixture
def sample_avi(tmp_path):
    """Generate the conversion fixture instead of relying on an ignored local file."""
    import subprocess
    from app.video.metadata import tool_paths
    path=tmp_path/'sample.avi'
    subprocess.run([tool_paths()['ffmpeg'],'-nostdin','-v','error',
        '-f','lavfi','-i','testsrc=size=160x120:rate=10',
        '-f','lavfi','-i','sine=frequency=440:sample_rate=44100',
        '-t','1','-c:v','mjpeg','-threads','1','-c:a','pcm_s16le',str(path)],
        check=True,timeout=30,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    return path
