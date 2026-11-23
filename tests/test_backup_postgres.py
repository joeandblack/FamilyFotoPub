"""Actual encrypted dump/restore drills, strictly on an opt-in disposable cluster."""
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import uuid

import psycopg
from psycopg import sql
import pytest

from app.backup.encrypted import read_chunks, verify_artifact
from app.backup.policy import UnclassifiedState
from app.backup.postgres import CaptureFailed, Endpoint, capture_database
from app.backup.restore_database import restore_database
from app.backup.catalog import capture_catalog, verify_catalog, freeze_database
from app.backup.admission import MaintenanceBlocked
from app.backup.encrypted import InvalidArtifact
from app.backup.recovery import restore_catalog
from app.backup.space import InsufficientSpace, SpaceGuard
from app.backup.library import capture_library, verify_library
from app.backup.library_restore import restore_library
from tests.backup_keys import backup_key


ROOT = Path(__file__).resolve().parents[1]


def ample(requirements):
    return SpaceGuard(requirements, minimum_reserve=10, reserve_percent=0,
                      disk_usage=lambda path: SimpleNamespace(free=10 ** 9))


@pytest.fixture
def postgres():
    port = os.environ.get("FAMILYFOTO_BACKUP_TEST_PORT")
    if not port:
        pytest.skip("Requires explicitly started disposable backup-plan-pg cluster")
    assert int(port) != 5432
    params = dict(host="127.0.0.1", port=int(port), user="backup_test", dbname="postgres")
    binary = Path(os.environ.get("FAMILYFOTO_BACKUP_PG_BIN", str(ROOT.parent / "FamilyFoto/runtime/postgres/bin")))
    suffix = ".exe" if os.name == "nt" else ""
    assert (binary / ("pg_dump" + suffix)).is_file()
    with psycopg.connect(**params, autocommit=True) as admin:
        actual = Path(admin.execute("SHOW data_directory").fetchone()[0]).resolve()
        assert actual == (ROOT / "temp/backup-plan-pg").resolve(), "Not the disposable cluster"
        source = "backup_source_" + uuid.uuid4().hex
        target = "backup_restore_" + uuid.uuid4().hex
        created = []
        try:
            for name in (source, target):
                admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
                created.append(name)
            yield params, source, target, binary / ("pg_dump" + suffix), binary / ("pg_restore" + suffix)
        finally:
            for name in reversed(created):
                admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))


def seed(params, name):
    native = "face_native_" + "a" * 32
    with psycopg.connect(**{**params, "dbname": name}) as db:
        db.execute("CREATE TABLE media(id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY, name text)")
        db.execute("INSERT INTO media(name) VALUES ('synthetic private picture')")
        db.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(native)))
        db.execute(sql.SQL("CREATE TABLE {}.people(id integer, name text)").format(sql.Identifier(native)))
        db.execute(sql.SQL("INSERT INTO {}.people VALUES (1, 'synthetic person')").format(sql.Identifier(native)))
        db.execute("CREATE FUNCTION media_count() RETURNS bigint LANGUAGE sql AS 'SELECT count(*) FROM media'")
    return native


def test_encrypted_database_restores_tables_sequences_and_functions(postgres, tmp_path):
    params, source, target, dump, restore = postgres
    native = seed(params, source)
    endpoint = Endpoint(params["host"], params["port"], source, params["user"])
    key = backup_key()
    directory = tmp_path / "snapshot"
    receipt = capture_database(endpoint, dump, directory, key, estimated_bytes=1024 ** 2, guard_factory=ample)
    assert verify_artifact(directory, key) == receipt
    assert receipt["metadata"]["media_included"] is False
    assert receipt["metadata"]["file_state_included"] is False
    assert len(receipt["metadata"]["tables"]) == 2
    # The fixture is tiny; the production restore adapter must stream and enforce
    # its own fresh-target admission, never materialize a large dump this way.
    archive = b"".join(read_chunks(directory, key, receipt))
    result = subprocess.run([str(restore), f"--host={params['host']}", f"--port={params['port']}",
                             f"--username={params['user']}", f"--dbname={target}", "--no-password",
                             "--no-owner", "--no-privileges", "--single-transaction", "--exit-on-error"],
                            input=archive, capture_output=True, timeout=30,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    with psycopg.connect(**{**params, "dbname": target}) as db:
        for table in receipt["metadata"]["tables"]:
            count = db.execute(sql.SQL("SELECT count(*) FROM {}.{}").format(
                sql.Identifier(table["schema"]), sql.Identifier(table["table"]))).fetchone()[0]
            assert count == table["rows"]
        assert db.execute("SELECT media_count()").fetchone()[0] == 1
        assert db.execute("INSERT INTO media(name) VALUES ('new') RETURNING id").fetchone()[0] == 2
        assert db.execute(sql.SQL("SELECT name FROM {}.people").format(sql.Identifier(native))).fetchone()[0] == "synthetic person"
    with psycopg.connect(**{**params, "dbname": source}) as db:
        assert db.execute("SELECT count(*) FROM media").fetchone()[0] == 1
    for path in directory.iterdir():
        assert b"synthetic private picture" not in path.read_bytes()


def test_dump_process_failure_never_publishes(postgres, tmp_path):
    params, source, _, _, _ = postgres
    seed(params, source)
    endpoint = Endpoint(params["host"], params["port"], source, params["user"])
    directory = tmp_path / "failed"
    # Python is not pg_dump and rejects its arguments; exercise a real failed child.
    with pytest.raises(CaptureFailed):
        capture_database(endpoint, sys.executable, directory, backup_key(),
                         estimated_bytes=1024, guard_factory=ample)
    assert not (directory / "complete.fernet").exists()


def test_unknown_schema_blocks_capture(postgres, tmp_path):
    params, source, _, dump, _ = postgres
    with psycopg.connect(**{**params, "dbname": source}) as db:
        db.execute("CREATE SCHEMA unexpected_feature")
    endpoint = Endpoint(params["host"], params["port"], source, params["user"])
    directory = tmp_path / "blocked"
    with pytest.raises(UnclassifiedState):
        capture_database(endpoint, dump, directory, backup_key(),
                         estimated_bytes=1024, guard_factory=ample)
    assert not directory.exists()


def test_metadata_counts_remain_on_exported_snapshot(postgres, tmp_path, monkeypatch):
    from app.backup import postgres as adapter
    params, source, _, dump, _ = postgres
    seed(params, source)
    original = adapter.seal_stream

    def write_after_snapshot(*args, **kwargs):
        with psycopg.connect(**{**params, "dbname": source}) as writer:
            writer.execute("INSERT INTO media(name) VALUES ('later write')")
        return original(*args, **kwargs)

    monkeypatch.setattr(adapter, "seal_stream", write_after_snapshot)
    receipt = capture_database(Endpoint(params["host"], params["port"], source, params["user"]),
                               dump, tmp_path / "snapshot", backup_key(),
                               estimated_bytes=1024 ** 2, guard_factory=ample)
    assert next(table["rows"] for table in receipt["metadata"]["tables"] if table["table"] == "media") == 1
    with psycopg.connect(**{**params, "dbname": source}) as db:
        assert db.execute("SELECT count(*) FROM media").fetchone()[0] == 2


def test_endpoint_never_puts_password_in_arguments_or_repr():
    endpoint = Endpoint("127.0.0.1", 55489, "fixture", "backup_test", "PRIVATE_PASSWORD")
    assert "PRIVATE_PASSWORD" not in repr(endpoint)
    assert "PRIVATE_PASSWORD" not in " ".join(endpoint.arguments())
    assert endpoint.environment()["PGPASSWORD"] == "PRIVATE_PASSWORD"


def test_restore_adapter_creates_only_new_readonly_database(postgres, tmp_path):
    from app.backup.compatibility import verify_restored_evidence
    from app.backup.inventory import inspect_snapshot
    params, source, _, dump, restore = postgres
    seed(params, source)
    key = backup_key()
    artifact = tmp_path / 'snapshot'
    manifest = capture_database(Endpoint(params['host'], params['port'], source, params['user']),
                                dump, artifact, key, estimated_bytes=1024 ** 2, guard_factory=ample)
    admin = Endpoint(params['host'], params['port'], 'postgres', params['user'])
    report = restore_database(artifact, key, admin, restore,
                              expected_cluster_directory=ROOT / 'temp/backup-plan-pg',
                              required_bytes=1024 ** 2, guard_factory=ample)
    try:
        assert report['verified'] and not report['activation_allowed']
        with psycopg.connect(**{**params, 'dbname': report['database']}) as db:
            assert db.execute('SHOW default_transaction_read_only').fetchone()[0] == 'on'
            assert db.execute('SELECT media_count()').fetchone()[0] == 1
            with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                db.execute("INSERT INTO media(name) VALUES ('blocked')")
        with psycopg.connect(**{**params, 'dbname': report['database'],
                               'options': '-c default_transaction_read_only=off'}) as db:
            db.execute("CREATE OR REPLACE FUNCTION media_count() RETURNS bigint LANGUAGE sql AS 'SELECT 999::bigint'")
        with psycopg.connect(**{**params, 'dbname': report['database']}) as db:
            assert db.execute('SELECT count(*) FROM media').fetchone()[0] == 1
            with pytest.raises(ValueError, match='differs'):
                verify_restored_evidence(db, list(inspect_snapshot(db)), manifest['metadata']['compatibility'])
    finally:
        with psycopg.connect(**params, autocommit=True) as db:
            db.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(report['database'])))


def test_restore_wrong_cluster_identity_refuses_before_create(postgres, tmp_path):
    params, source, _, dump, restore = postgres
    seed(params, source)
    key = backup_key()
    artifact = tmp_path / 'snapshot'
    capture_database(Endpoint(params['host'], params['port'], source, params['user']),
                     dump, artifact, key, estimated_bytes=1024 ** 2, guard_factory=ample)
    with pytest.raises(ValueError, match='identity'):
        restore_database(artifact, key, Endpoint(params['host'], params['port'], 'postgres', params['user']),
                         restore, expected_cluster_directory=tmp_path,
                         required_bytes=1024 ** 2, guard_factory=ample)


def test_model_and_matching_policy_evidence_survives_restore_and_detects_changes(postgres, tmp_path):
    from app.backup.compatibility import verify_restored_evidence
    from app.backup.inventory import inspect_snapshot
    params, source, _, dump, restore = postgres
    native = seed(params, source)
    with psycopg.connect(**{**params, 'dbname': source}) as db:
        db.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(native)))
        db.execute('CREATE TABLE people_models(id text PRIMARY KEY, manifest jsonb NOT NULL)')
        db.execute('CREATE TABLE native_matching_policies(model_id text PRIMARY KEY, policy jsonb, evidence jsonb)')
        db.execute('CREATE TABLE native_catalog_state(singleton boolean PRIMARY KEY, model_id text, legacy_pilot text)')
        db.execute("INSERT INTO people_models VALUES ('fixture-model', %s)",
                   ('{"provider":"fixture","assets":{"recognizer":{"sha256":"original"}}}',))
        db.execute("INSERT INTO native_matching_policies VALUES ('fixture-model', %s, %s)",
                   ('{"threshold":0.8}', '{"qualified":true}'))
        db.execute("INSERT INTO native_catalog_state VALUES (true, 'fixture-model', 'historical')")
    key = backup_key()
    artifact = tmp_path / 'models'
    manifest = capture_database(Endpoint(params['host'], params['port'], source, params['user']),
                                dump, artifact, key, estimated_bytes=1024 ** 2, guard_factory=ample)
    expected = manifest['metadata']['compatibility']
    assert {item['table'] for item in expected['models']} == {
        'people_models', 'native_matching_policies', 'native_catalog_state'}
    report = restore_database(artifact, key, Endpoint(params['host'], params['port'], 'postgres', params['user']),
                              restore, expected_cluster_directory=ROOT / 'temp/backup-plan-pg',
                              required_bytes=1024 ** 2, guard_factory=ample)
    try:
        for statement in (
            "UPDATE people_models SET manifest='{}'::jsonb",
            "UPDATE native_matching_policies SET policy='{}'::jsonb",
            "UPDATE native_matching_policies SET evidence='{}'::jsonb",
            "UPDATE native_catalog_state SET model_id='different'",
        ):
            with psycopg.connect(**{**params, 'dbname': report['database'],
                                   'options': '-c default_transaction_read_only=off'}) as db:
                db.execute(sql.SQL('SET LOCAL search_path TO {},public').format(sql.Identifier(native)))
                verify_restored_evidence(db, list(inspect_snapshot(db)), expected)
                # Evidence uses schema-qualified queries, independent of search_path.
                # Roll back each simulated corruption; never change the source.
                db.execute(statement)
                with pytest.raises(ValueError, match='differs'):
                    verify_restored_evidence(db, list(inspect_snapshot(db)), expected)
                db.rollback()
    finally:
        with psycopg.connect(**params, autocommit=True) as db:
            db.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(report['database'])))


def test_offline_catalog_pairs_database_and_state(postgres, tmp_path):
    params, source, _, dump, _ = postgres
    seed(params, source)
    config = tmp_path / 'config'
    data = tmp_path / 'data'
    config.mkdir()
    data.mkdir()
    (config / 'local.json').write_text('{"fixture":"private test value"}')
    (data / 'people-preview.json').write_text('{}')
    endpoint = Endpoint(params['host'], params['port'], source, params['user'])
    key = backup_key()
    target = tmp_path / 'catalog-set'
    payload = capture_catalog(endpoint, dump, config, data, target, key,
                              database_budget=1024 ** 2, offline_confirmed=True, guard_factory=ample)
    assert verify_catalog(target, key) == payload
    assert payload['media_included'] is False
    assert not (data / '.recovery-admission/maintenance.json').exists()
    (target / 'state/00000000.fernet').unlink()
    with pytest.raises((InvalidArtifact, OSError)):
        verify_catalog(target, key)


def test_freeze_blocks_unregistered_database_writer(postgres):
    params, source, _, _, _ = postgres
    seed(params, source)
    endpoint = Endpoint(params['host'], params['port'], source, params['user'])
    with freeze_database(endpoint):
        with psycopg.connect(**{**params, 'dbname': source}) as writer:
            writer.execute("SET LOCAL lock_timeout='100ms'")
            with pytest.raises(psycopg.errors.LockNotAvailable):
                writer.execute("INSERT INTO media(name) VALUES ('not admitted')")


def test_offline_capture_refuses_existing_client(postgres, tmp_path):
    params, source, _, _, _ = postgres
    seed(params, source)
    endpoint = Endpoint(params['host'], params['port'], source, params['user'])
    with psycopg.connect(**{**params, 'dbname': source}):
        with pytest.raises(MaintenanceBlocked, match='clients'):
            with freeze_database(endpoint):
                pytest.fail('Admitted while another client was connected')


def test_catalog_restore_invalidates_sessions_and_does_not_activate(postgres, tmp_path):
    import hashlib
    import json
    params, source, _, dump, restore = postgres
    seed(params, source)
    with psycopg.connect(**{**params, 'dbname': source}) as db:
        db.execute('CREATE TABLE auth_sessions(token_hash text)')
        db.execute("INSERT INTO auth_sessions VALUES ('old-cookie')")
        db.execute('CREATE TABLE jobs(state text, error text)')
        db.execute("INSERT INTO jobs VALUES ('running', NULL)")
        db.execute('CREATE TABLE upload_sessions(id uuid, state text, error text, cleaned_at timestamptz)')
        db.execute("INSERT INTO upload_sessions VALUES ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa','queued', NULL, NULL)")
        db.execute('CREATE TABLE upload_files(session_id uuid,file_id integer,relative_path text,size bigint,'
                   'received bigint,sha256 text,verified boolean,disposition text)')
        db.execute("INSERT INTO upload_files VALUES ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',0,'photo.JPG',"
                   "6,6,%s,true,'accepted')", (hashlib.sha256(b'staged').hexdigest(),))
    config = tmp_path / 'config'
    data = tmp_path / 'data'
    config.mkdir()
    data.mkdir()
    staged = data / 'uploads/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/0.jpg'
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b'staged')
    (config / 'local.json').write_text(json.dumps({'database': {'password': 'old-password'},
                                                  'auth_secret': 'old-secret', 'destination': {'root': 'old-disk'}}))
    endpoint = Endpoint(params['host'], params['port'], source, params['user'])
    admin = Endpoint(params['host'], params['port'], 'postgres', params['user'])
    key = backup_key()
    artifact = tmp_path / 'catalog'
    capture_catalog(endpoint, dump, config, data, artifact, key,
                    database_budget=1024 ** 2, offline_confirmed=True, guard_factory=ample)
    target = tmp_path / 'recovered'
    report = restore_catalog(artifact, key, target, admin, restore,
                             expected_cluster_directory=ROOT / 'temp/backup-plan-pg',
                             database_budget=1024 ** 2, guard_factory=ample)
    try:
        assert report['activation_allowed'] is False
        assert report['captured_at'] == verify_catalog(artifact, key)['created_at']
        assert (target / 'files/data/uploads/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/0.jpg').read_bytes() == b'staged'
        assert not (target / 'RECOVERY-INCOMPLETE').exists()
        settings = json.loads((target / 'files/config/local.json').read_text())
        assert settings['database']['dbname'] == report['database']
        assert 'password' not in settings['database']
        assert settings['auth_secret'] != 'old-secret'
        assert settings['destination'] == {}
        assert settings['recovery']['activation_required']
        with psycopg.connect(**{**params, 'dbname': report['database']}) as db:
            assert db.execute('SELECT count(*) FROM auth_sessions').fetchone()[0] == 0
            assert db.execute('SELECT state FROM jobs').fetchone()[0] == 'paused'
            assert db.execute('SELECT state FROM upload_sessions').fetchone()[0] == 'paused'
        with psycopg.connect(**{**params, 'dbname': source}) as db:
            assert db.execute('SELECT count(*) FROM auth_sessions').fetchone()[0] == 1
            assert db.execute('SELECT state FROM jobs').fetchone()[0] == 'running'
    finally:
        with psycopg.connect(**params, autocommit=True) as db:
            db.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(report['database'])))


def test_low_space_blocks_before_database_connection(tmp_path, monkeypatch):
    monkeypatch.setattr(psycopg, "connect", lambda **kwargs: pytest.fail("Connected before space check"))
    def low(requirements):
        return SpaceGuard(requirements, disk_usage=lambda path: SimpleNamespace(free=0))
    with pytest.raises(InsufficientSpace):
        capture_database(Endpoint("127.0.0.1", 55489, "fixture", "backup_test"), sys.executable,
                         tmp_path / "blocked", backup_key(), estimated_bytes=1024, guard_factory=low)
    assert not (tmp_path / "blocked").exists()


@pytest.mark.parametrize("name", ["postgresql://host/database", "host=another dbname=live", "--clean"])
def test_database_connstring_override_rejected(name):
    with pytest.raises(ValueError):
        Endpoint("127.0.0.1", 55489, name, "backup_test")


def test_interrupted_library_capture_resumes_with_a_new_catalog(postgres, tmp_path):
    """Real dump and freeze: the retried set restores, and its catalog sees a later change."""
    import hashlib
    import json
    from app.backup.admission import acknowledge_failed_maintenance
    params, source, _, dump, restore = postgres
    seed(params, source)
    digest = hashlib.sha256(b'fixture photo').hexdigest()
    with psycopg.connect(**{**params, 'dbname': source}) as db:
        db.execute('ALTER TABLE media ADD COLUMN relative_path text, ADD COLUMN size bigint, '
                   'ADD COLUMN sha256 text, ADD COLUMN stored_sha256 text, '
                   'ADD COLUMN original_relative_path text, ADD COLUMN original_size bigint, '
                   'ADD COLUMN conversion_state text')
        db.execute("UPDATE media SET relative_path='photo.jpg',size=13,sha256=%s", (digest,))
        db.execute('CREATE TABLE media_trash(media_id integer, deleting_at timestamptz, deleted_at timestamptz)')
        db.execute('CREATE TABLE video_metadata(media_id integer, metadata jsonb)')
    config, data, media = (tmp_path / name for name in ('config', 'data', 'media'))
    for root in (config, data, media):
        root.mkdir()
    (config / 'local.json').write_text('{}')
    (media / 'photo.jpg').write_bytes(b'fixture photo')
    endpoint = Endpoint(params['host'], params['port'], source, params['user'])
    key = backup_key()
    target = tmp_path / 'library'

    def capture(**kwargs):
        return capture_library(endpoint, dump, config, data, media, target, key,
                               database_budget=1024 ** 2, offline_confirmed=True, guard_factory=ample, **kwargs)

    def stop(event):
        if event.get('phase') == 'copy':
            raise OSError('Synthetic interrupted capture')

    with pytest.raises(OSError, match='Synthetic interrupted'):
        capture(progress=stop)
    with pytest.raises(Exception):
        verify_library(target, key)
    marker = data / '.recovery-admission/maintenance.json'
    acknowledge_failed_maintenance(data, json.loads(marker.read_text())['id'])
    # A catalog-only change between the attempts must appear in the retried recovery point.
    with psycopg.connect(**{**params, 'dbname': source}) as db:
        db.execute('INSERT INTO media_trash(media_id,deleted_at) VALUES (-1,now())')
    payload = capture(resume=True)
    assert verify_library(target, key) == payload
    assert len(list(target.glob('previous-catalog-*'))) == 1
    media.rename(tmp_path / 'disconnected-media')
    report = restore_library(target, key, tmp_path / 'recovered',
                             Endpoint(params['host'], params['port'], 'postgres', params['user']), restore,
                             expected_cluster_directory=ROOT / 'temp/backup-plan-pg',
                             database_budget=1024 ** 2, guard_factory=ample)
    try:
        assert report['media_verified'] is True
        assert report['activation_allowed'] is False
        with psycopg.connect(**{**params, 'dbname': report['database']}) as db:
            assert db.execute('SELECT count(*) FROM media_trash WHERE media_id=-1').fetchone()[0] == 1
    finally:
        with psycopg.connect(**params, autocommit=True) as db:
            db.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(report['database'])))


def test_interrupted_library_restore_resumes_after_a_simulated_remount(postgres, tmp_path, monkeypatch):
    """Real database and copy: every record the resume reads shows a new device number."""
    import hashlib
    import json
    from app.backup import library_restore as recovery_module
    from tests.test_backup_capture_retry import shift_devices
    params, source, _, dump, restore = postgres
    seed(params, source)
    digest = hashlib.sha256(b'fixture photo').hexdigest()
    with psycopg.connect(**{**params, 'dbname': source}) as db:
        db.execute('ALTER TABLE media ADD COLUMN relative_path text, ADD COLUMN size bigint, '
                   'ADD COLUMN sha256 text, ADD COLUMN stored_sha256 text, '
                   'ADD COLUMN original_relative_path text, ADD COLUMN original_size bigint, '
                   'ADD COLUMN conversion_state text')
        db.execute("UPDATE media SET relative_path='photo.jpg',size=13,sha256=%s", (digest,))
        db.execute('CREATE TABLE media_trash(media_id integer, deleting_at timestamptz, deleted_at timestamptz)')
        db.execute('CREATE TABLE video_metadata(media_id integer, metadata jsonb)')
    config, data, media = (tmp_path / name for name in ('config', 'data', 'media'))
    for root in (config, data, media):
        root.mkdir()
    (config / 'local.json').write_text('{}')
    (media / 'photo.jpg').write_bytes(b'fixture photo')
    key = backup_key()
    backup, recovered = tmp_path / 'library', tmp_path / 'recovered'
    capture_library(Endpoint(params['host'], params['port'], source, params['user']), dump, config, data, media, backup, key,
                    database_budget=1024 ** 2, offline_confirmed=True, guard_factory=ample)
    admin = Endpoint(params['host'], params['port'], 'postgres', params['user'])

    def run(**kwargs):
        return restore_library(backup, key, recovered, admin, restore,
                               expected_cluster_directory=ROOT / 'temp/backup-plan-pg',
                               database_budget=1024 ** 2, guard_factory=ample, **kwargs)
    copy = recovery_module.copy_media_tree

    def fail_copy(*args, **kwargs):
        def progress(value):
            if value.get('phase') == 'copy':
                raise OSError('Synthetic interrupted library copy')
        return copy(*args, **{**kwargs, 'progress': progress})
    with monkeypatch.context() as patch:
        patch.setattr(recovery_module, 'copy_media_tree', fail_copy)
        with pytest.raises(OSError, match='Synthetic interrupted'):
            run()
    prior_database = json.loads((recovered / 'catalog/database-target.json').read_text())['database']

    def shifted(path, *fields):
        value = json.loads(path.read_text())
        for field in fields:
            value[field][0] += 7
        path.write_text(json.dumps(value))
    shifted(recovered / 'restore-job.json', 'identity')
    shifted(recovered / 'catalog/catalog-job.json', 'identity')
    shift_devices(recovered / 'media-inventory')
    job = json.loads((recovered / 'media-copy/copy-job.json').read_text())
    job['source_identity'][0] += 7
    job['target_identity'][0] += 7
    job['inventory_sha256'] = hashlib.sha256((recovered / 'media-inventory/inventory.sqlite3').read_bytes()).hexdigest()
    (recovered / 'media-copy/copy-job.json').write_text(json.dumps(job))
    (recovered / 'media-copy/inventory/inventory.sqlite3').write_bytes(
        (recovered / 'media-inventory/inventory.sqlite3').read_bytes())
    monkeypatch.setattr('app.backup.media_tree.copy_media_file',
                        lambda *args, **kwargs: pytest.fail('Completed photo must not be copied again'))
    report = run(resume=True)
    try:
        assert report['media_verified'] is True and report['activation_allowed'] is False
        assert report['database'] == prior_database
        assert (recovered / 'media-copy/media/photo.jpg').read_bytes() == b'fixture photo'
        assert not (recovered / 'LIBRARY-RESTORE-INCOMPLETE').exists()
    finally:
        with psycopg.connect(**params, autocommit=True) as db:
            db.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(report['database'])))


@pytest.mark.parametrize('retire_to', [None, 'a' * 32])
@pytest.mark.parametrize('interrupt', [None, 'copy', 'inventory'])
def test_whole_library_capture_binds_catalog_and_media(postgres, tmp_path, retire_to, monkeypatch, interrupt):
    import hashlib
    import json
    params, source, _, dump, restore = postgres
    seed(params, source)
    digest = hashlib.sha256(b'fixture photo').hexdigest()
    with psycopg.connect(**{**params, 'dbname': source}) as db:
        db.execute('ALTER TABLE media ADD COLUMN relative_path text, ADD COLUMN size bigint, '
                   'ADD COLUMN sha256 text, ADD COLUMN stored_sha256 text, '
                   'ADD COLUMN original_relative_path text, ADD COLUMN original_size bigint, '
                   'ADD COLUMN conversion_state text')
        db.execute("UPDATE media SET relative_path='photo.jpg',size=13,sha256=%s", (digest,))
        db.execute('CREATE TABLE media_trash(media_id integer, deleting_at timestamptz, deleted_at timestamptz)')
        db.execute('CREATE TABLE video_metadata(media_id integer, metadata jsonb)')
    config, data, media = (tmp_path / name for name in ('config', 'data', 'media'))
    for root in (config, data, media):
        root.mkdir()
    (config / 'local.json').write_text('{}')
    (media / 'photo.jpg').write_bytes(b'fixture photo')
    endpoint = Endpoint(params['host'], params['port'], source, params['user'])
    key = backup_key()
    target = tmp_path / 'library'
    def capture():
        return capture_library(endpoint, dump, config, data, media, target, key,
                               database_budget=1024 ** 2, offline_confirmed=True, guard_factory=ample,
                               retire_to=retire_to)
    if retire_to:
        from app.backup.retirement import recover_retirement

        def fail_receipt(*args, **kwargs):
            raise OSError('interrupted receipt publication')

        with monkeypatch.context() as patch:
            patch.setattr('app.backup.library.publish_retirement', fail_receipt)
            with pytest.raises(OSError, match='interrupted receipt'):
                capture()
        result = verify_library(target, key)
        marker = data / '.recovery-admission/maintenance.json'
        before = marker.read_bytes()
        receipt = recover_retirement(data, target, key, guard_factory=ample)
        assert receipt['successor_id'] == retire_to
        assert marker.read_bytes() == before
    else:
        result = capture()
    assert result['reconciliation']['catalog_media'] == 1
    assert result['media_files'] == 1
    assert verify_library(target, key) == result
    media.rename(tmp_path / 'disconnected-media')
    assert verify_library(target, key) == result
    with pytest.raises(ValueError):
        verify_library(target, backup_key())
    recovered = tmp_path / 'recovered-library'
    prior_id = prior_database = None
    if interrupt:
        from app.backup import library_restore as recovery_module
        copy = recovery_module.copy_media_tree

        def fail_copy(*args, **kwargs):
            def progress(value):
                if value.get('phase') == 'copy':
                    raise OSError('Synthetic interrupted library copy')
            return copy(*args, **{**kwargs, 'progress': progress})

        def fail_inventory(source, directory, **kwargs):
            directory.mkdir()
            (directory / 'INVENTORY-INCOMPLETE').touch()
            raise OSError('Synthetic interrupted library inventory')

        with monkeypatch.context() as patch:
            patch.setattr(recovery_module, 'copy_media_tree' if interrupt == 'copy' else 'capture_media_inventory',
                          fail_copy if interrupt == 'copy' else fail_inventory)
            with pytest.raises(OSError, match='Synthetic interrupted'):
                restore_library(target, key, recovered,
                    Endpoint(params['host'], params['port'], 'postgres', params['user']), restore,
                    expected_cluster_directory=ROOT / 'temp/backup-plan-pg',
                    database_budget=1024 ** 2, guard_factory=ample)
        prior_id = json.loads((recovered / 'restore-job.json').read_text())['restoration_id']
        prior_database = json.loads((recovered / 'catalog/database-target.json').read_text())['database']
        assert (recovered / 'LIBRARY-RESTORE-INCOMPLETE').exists()
        if interrupt == 'copy':
            monkeypatch.setattr('app.backup.media_tree.copy_media_file',
                                lambda *args, **kwargs: pytest.fail('Completed photo must not be copied again'))
    report = restore_library(target, key, recovered,
                             Endpoint(params['host'], params['port'], 'postgres', params['user']), restore,
                             expected_cluster_directory=ROOT / 'temp/backup-plan-pg',
                             database_budget=1024 ** 2, resume=bool(interrupt), guard_factory=ample)
    try:
        assert report['media_verified'] is True
        assert report['activation_allowed'] is False
        if interrupt:
            assert report['restoration_id'] == prior_id and report['database'] == prior_database
        if interrupt == 'inventory':
            assert len(list(recovered.glob('previous-inventory-*'))) == 1
        assert report['source_retired'] is (retire_to is not None)
        if retire_to:
            assert report['restoration_id'] == retire_to
            assert (data / '.recovery-admission/retired.json').is_file()
        assert (recovered / 'media-copy/media/photo.jpg').read_bytes() == b'fixture photo'
        assert not (recovered / 'LIBRARY-RESTORE-INCOMPLETE').exists()
        settings = json.loads((recovered / 'catalog/files/config/local.json').read_text())
        assert settings['recovery']['activation_required'] is True
        assert settings['recovery']['media_verified'] is True
        assert settings['destination'] == {}
        with psycopg.connect(**{**params, 'dbname': report['database']}) as db:
            assert db.execute('SELECT count(*) FROM media').fetchone()[0] == 1
            assert db.execute('SHOW default_transaction_read_only').fetchone()[0] == 'on'
    finally:
        with psycopg.connect(**params, autocommit=True) as db:
            db.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(report['database'])))
    (target / 'media-copy/media/photo.jpg').write_bytes(b'corrupt photo')
    with pytest.raises(ValueError, match='verification'):
        verify_library(target, key)
