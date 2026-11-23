import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import psycopg
from psycopg import sql
import pytest

from app.backup import encrypted_library as module
from app.backup.postgres import Endpoint
from tests.test_backup_postgres import postgres, ROOT, seed, ample  # noqa: F401
from tests.test_backup_restic import engine  # noqa: F401
from tests.backup_keys import backup_key


@pytest.mark.parametrize('failure', [None, 'metadata-backup', 'damaged-media', 'wrong-key',
                                    'interrupt-metadata', 'interrupt-media', 'interrupt-catalog'])
def test_encrypted_library_restores_catalog_and_media_without_working_copy(
        postgres, engine, tmp_path, monkeypatch, failure):
    params, source, _, dump, restore = postgres
    seed(params, source)
    with psycopg.connect(**{**params, 'dbname': source}) as db:
        db.execute('ALTER TABLE media ADD COLUMN relative_path text, ADD COLUMN size bigint, '
                   'ADD COLUMN sha256 text, ADD COLUMN stored_sha256 text, '
                   'ADD COLUMN original_relative_path text, ADD COLUMN original_size bigint, '
                   'ADD COLUMN conversion_state text')
        db.execute("UPDATE media SET relative_path='photo.jpg',size=13,sha256=%s",
                   (hashlib.sha256(b'fixture photo').hexdigest(),))
        db.execute('CREATE TABLE media_trash(media_id integer, deleting_at timestamptz, deleted_at timestamptz)')
        db.execute('CREATE TABLE video_metadata(media_id integer, metadata jsonb)')
    config, data, media = (tmp_path / name for name in ('config', 'data', 'media'))
    for path in (config, data, media):
        path.mkdir()
    (config / 'local.json').write_text('{}')
    (media / 'photo.jpg').write_bytes(b'fixture photo')
    (media / 'empty').mkdir()
    repository = engine.initialize()
    key = backup_key()
    if failure == 'metadata-backup':
        original = engine.backup

        def fail_metadata(path, *args, **kwargs):
            if path.name == 'metadata':
                raise OSError('Synthetic disconnected repository')
            return original(path, *args, **kwargs)

        monkeypatch.setattr(engine, 'backup', fail_metadata)
        with pytest.raises(OSError, match='disconnected'):
            module.capture_encrypted_library(
                Endpoint(params['host'], params['port'], source, params['user']), dump,
                config, data, media, tmp_path / 'working', key, engine, repository,
                database_budget=1024 ** 2, offline_confirmed=True, guard_factory=ample)
        assert not (tmp_path / 'working/ENCRYPTED-COMPLETE.json').exists()
        assert (data / '.recovery-admission/maintenance.json').is_file()
        return
    receipt = module.capture_encrypted_library(
        Endpoint(params['host'], params['port'], source, params['user']), dump,
        config, data, media, tmp_path / 'working', key, engine, repository,
        database_budget=1024 ** 2, offline_confirmed=True, guard_factory=ample)
    assert receipt['media_files'] == 1 and not receipt['activation_allowed']
    assert (tmp_path / 'working/ENCRYPTED-COMPLETE.json').is_file()
    assert not (data / '.recovery-admission/maintenance.json').exists()
    # Recovery must use only the repository, both keys, and explicit target DB.
    for path in (config, data, media, tmp_path / 'working'):
        path.rename(path.with_name('disconnected-' + path.name))
    recovered = tmp_path / 'recovered'
    if failure in {'damaged-media', 'wrong-key'}:
        original = module.restore_snapshot_job

        def damage_media(tool, snapshot, repository, target, **kwargs):
            result = original(tool, snapshot, repository, target, **kwargs)
            if target.name == 'media-job':
                (result / 'photo.jpg').write_bytes(b'damaged photo')
            return result

        def forbidden_database_restore(*args, **kwargs):
            pytest.fail('Database creation must not precede media/key verification')

        if failure == 'damaged-media':
            monkeypatch.setattr(module, 'restore_snapshot_job', damage_media)
        monkeypatch.setattr(module, 'restore_catalog', forbidden_database_restore)
        with pytest.raises((ValueError, module.InvalidArtifact)):
            module.restore_encrypted_library(
                engine, repository, receipt['metadata_snapshot'],
                backup_key() if failure == 'wrong-key' else key, recovered,
                Endpoint(params['host'], params['port'], 'postgres', params['user']), restore,
                expected_cluster_directory=ROOT / 'temp/backup-plan-pg', database_budget=1024 ** 2,
                metadata_budget=4 * 1024 ** 2, guard_factory=ample)
        assert (recovered / 'LIBRARY-RESTORE-INCOMPLETE').is_file()
        assert not (recovered / 'LIBRARY-RECOVERY-REPORT.json').exists()
        assert not (recovered / 'catalog').exists()
        return
    resume = failure in {'interrupt-metadata', 'interrupt-media'}
    if resume:
        original = engine.run
        interrupted_job = 'metadata-job' if failure == 'interrupt-metadata' else 'media-job'
        calls = []

        def interrupt(arguments, **kwargs):
            if arguments[0] == 'restore':
                output = Path(arguments[arguments.index('--target') + 1])
                calls.append(output.parent.name)
                if output.parent.name == interrupted_job:
                    name = 'recovery-point.json' if interrupted_job == 'metadata-job' else 'photo.jpg'
                    (output / name).write_bytes(b'partial')
                    raise OSError('Synthetic interrupted storage stage')
            return original(arguments, **kwargs)

        monkeypatch.setattr(engine, 'run', interrupt)
        with pytest.raises(OSError, match='interrupted storage'):
            module.restore_encrypted_library(
                engine, repository, receipt['metadata_snapshot'], key, recovered,
                Endpoint(params['host'], params['port'], 'postgres', params['user']), restore,
                expected_cluster_directory=ROOT / 'temp/backup-plan-pg', database_budget=1024 ** 2,
                metadata_budget=4 * 1024 ** 2, guard_factory=ample)
        assert not (recovered / 'catalog').exists()
        assert (recovered / 'LIBRARY-RESTORE-INCOMPLETE').exists()
        with pytest.raises(module.InvalidArtifact, match='identity'):
            module.restore_encrypted_library(
                engine, repository, receipt['metadata_snapshot'], key, recovered,
                Endpoint(params['host'], params['port'] + 1, 'postgres', params['user']), restore,
                expected_cluster_directory=ROOT / 'temp/backup-plan-pg', database_budget=1024 ** 2,
                metadata_budget=4 * 1024 ** 2, resume=True, guard_factory=ample)

        def resumed(arguments, **kwargs):
            if arguments[0] == 'restore' and interrupted_job == 'media-job':
                assert Path(arguments[arguments.index('--target') + 1]).parent.name != 'metadata-job'
            return original(arguments, **kwargs)

        monkeypatch.setattr(engine, 'run', resumed)
    catalog_database = None
    if failure == 'interrupt-catalog':
        from app.backup import recovery
        original = recovery.sanitize_files

        def interrupt_catalog(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError('Synthetic catalog interruption')

        monkeypatch.setattr(recovery, 'sanitize_files', interrupt_catalog)
        with pytest.raises(OSError, match='catalog interruption'):
            module.restore_encrypted_library(
                engine, repository, receipt['metadata_snapshot'], key, recovered,
                Endpoint(params['host'], params['port'], 'postgres', params['user']), restore,
                expected_cluster_directory=ROOT / 'temp/backup-plan-pg', database_budget=1024 ** 2,
                metadata_budget=4 * 1024 ** 2, guard_factory=ample)
        catalog_database = json.loads((recovered / 'catalog/database-target.json').read_text())['database']
        monkeypatch.setattr(recovery, 'sanitize_files', original)
        resume = True
    report = module.restore_encrypted_library(
        engine, repository, receipt['metadata_snapshot'], key, recovered,
        Endpoint(params['host'], params['port'], 'postgres', params['user']), restore,
        expected_cluster_directory=ROOT / 'temp/backup-plan-pg', database_budget=1024 ** 2,
        metadata_budget=4 * 1024 ** 2, resume=resume, guard_factory=ample)
    try:
        assert report['media_verified'] and not report['activation_allowed']
        if catalog_database:
            assert report['database'] == catalog_database
        assert report['library_id'] == receipt['library_id']
        assert (Path(report['media_root']) / 'photo.jpg').read_bytes() == b'fixture photo'
        assert (Path(report['media_root']) / 'empty').is_dir()
        assert not (recovered / 'LIBRARY-RESTORE-INCOMPLETE').exists()
        settings = json.loads((recovered / 'catalog/files/config/local.json').read_text())
        assert settings['recovery']['activation_required'] is True
        assert settings['destination'] == {}
        with psycopg.connect(**{**params, 'dbname': report['database']}) as db:
            assert db.execute('SHOW default_transaction_read_only').fetchone()[0] == 'on'
            assert db.execute('SELECT count(*) FROM media').fetchone()[0] == 1
    finally:
        with psycopg.connect(**params, autocommit=True) as db:
            db.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(report['database'])))


def test_encrypted_capture_requires_offline_admission_before_any_access():
    with pytest.raises(module.MaintenanceBlocked, match='Offline'):
        module.capture_encrypted_library(None, None, None, None, None, None, None, None, None,
                                         database_budget=1)


@pytest.mark.parametrize('root_name', ['config', 'data', 'media', 'working'])
def test_repository_key_in_captured_root_refused_before_database_access(tmp_path, root_name):
    roots = [tmp_path / name for name in ('config', 'data', 'media', 'working')]
    engine = SimpleNamespace(repository=tmp_path / 'repository', password_file=tmp_path / root_name / 'key')
    with pytest.raises(ValueError, match='password'):
        module.capture_encrypted_library(None, None, *roots, None, engine, 'a' * 64,
                                         database_budget=1, offline_confirmed=True)


def test_resume_delegates_existing_catalog_to_its_owned_recovery_job(tmp_path, monkeypatch):
    engine = SimpleNamespace(repository=tmp_path / 'repository', check=lambda repository: None)
    admin = Endpoint('127.0.0.1', 55489, 'postgres', 'fixture')
    target = tmp_path / 'restore'
    calls = []

    def interrupted(*args, **kwargs):
        calls.append('entered')
        if len(calls) == 1:
            (target / 'catalog').mkdir()
            raise OSError('Synthetic interrupted database stage')
        return {'resumed': True}

    monkeypatch.setattr(module, '_restore_encrypted_contents', interrupted)
    from app.backup.passwords import BackupPassword
    arguments = (engine, 'a' * 64, 'b' * 64, BackupPassword('fixture password'), target, admin, 'fixture-restore')
    options = dict(expected_cluster_directory=tmp_path / 'cluster', database_budget=1024,
                   metadata_budget=1024, guard_factory=ample)
    with pytest.raises(OSError, match='database stage'):
        module.restore_encrypted_library(*arguments, **options)
    abandoned = target / ('LIBRARY-RECOVERY-REPORT.json.' + 'c' * 32 + '.part')
    abandoned.write_bytes(b'{incomplete report')
    assert module.restore_encrypted_library(*arguments, resume=True, **options) == {'resumed': True}
    assert abandoned.read_bytes() == b'{incomplete report'
    assert calls == ['entered', 'entered']
    assert (target / 'LIBRARY-RESTORE-INCOMPLETE').exists()
