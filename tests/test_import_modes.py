import json
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb

from app import database
from app.imports import importer
from app.imports.modes import skip_duplicate, validate_mode
from app.imports.history import history, import_details
from test_uploads import env
from test_unexamined_photos import disposable_migrations


@pytest.mark.parametrize('value', [None, '', 'missing', True, {}, []])
def test_invalid_mode(value):
    with pytest.raises(ValueError):
        validate_mode(value)


def create_job(config, source, mode='normal'):
    with database.connection() as db:
        return db.execute('INSERT INTO jobs(source_root,source_storage) VALUES(%s,%s) RETURNING id',
            (str(source), Jsonb({**config['source'], 'root': str(source)}))).fetchone()['id']


def seed(config):
    source = Path(config['source']['root'])
    (source/'original.mp4').write_bytes(b'fixture-video-' + str(source).encode())
    job = create_job(config, source)
    importer.run(str(source))
    backup = source.parent/'backup'
    backup.mkdir()
    (backup/'renamed.mp4').write_bytes((source/'original.mp4').read_bytes())
    return job, backup


@pytest.mark.parametrize('mode', ['normal', 'missing-only'])
def test_import_retention_progress_and_ownership(env, mode):
    _, config, data = env
    original, backup = seed(config)
    job = create_job(config, backup, mode)
    importer.run(str(backup))
    with database.connection() as db:
        row = db.execute('SELECT * FROM jobs WHERE id=%s', (job,)).fetchone()
        items = db.execute('SELECT * FROM import_items WHERE job_id=%s', (job,)).fetchall()
        assert row['state'] == 'completed-review-required'
        assert 'import_mode' not in row
        assert row['skipped_duplicate_files'] == 0
        assert len(items) == 1 and items[0]['state']=='duplicate' and items[0]['media_id'] is None
        assert db.execute('SELECT first_import_job_id FROM media').fetchone()['first_import_job_id'] == original
        assert db.execute('SELECT count(*) n FROM import_items WHERE job_id=%s', (original,)).fetchone()['n'] == 1
        batch = next(b for b in import_details(db, history(db)) if b['id'] == job)
        assert batch['total'] == batch['import_status']['processed'] == 1
        assert batch['total_bytes'] == batch['import_status']['processed_bytes']
    live = json.loads((data/'import-status.json').read_text())
    assert live['total'] == live['processed'] == 1
    assert live['processed_bytes'] == live['total_bytes']


def test_duplicate_transaction_rollback_and_resume(env, monkeypatch):
    _, config, _ = env
    _, backup = seed(config)
    job = create_job(config, backup, 'missing-only')
    def interrupted(db, job_id, item_id):
        assert skip_duplicate(db, job_id, item_id)
        raise OSError('Simulated disconnect before commit')
    monkeypatch.setattr(importer, 'skip_duplicate', interrupted)
    with pytest.raises(OSError):importer.run(str(backup))
    with database.connection() as db:
        assert db.execute('SELECT skipped_duplicate_files FROM jobs WHERE id=%s', (job,)).fetchone()['skipped_duplicate_files'] == 0
        item = db.execute('SELECT * FROM import_items WHERE job_id=%s', (job,)).fetchone()
        assert item['state'] == 'pending'
    monkeypatch.setattr(importer, 'skip_duplicate', skip_duplicate)
    importer.run(str(backup))
    with database.connection() as db:
        assert not skip_duplicate(db, job, item['id'])
        assert db.execute("SELECT count(*) n FROM import_items WHERE job_id=%s AND state='duplicate' AND media_id IS NULL", (job,)).fetchone()['n'] == 1


def test_recovery_and_new_files_keep_records(env):
    _, config, _ = env
    original, backup = seed(config)
    with database.connection() as db:
        media = db.execute('SELECT * FROM media').fetchone()
    target = Path(config['destination']['root'])/media['relative_path']
    target.unlink()  # Disposable test destination only.
    (backup/'new.mp4').write_bytes(b'new-fixture-' + str(backup).encode())
    job = create_job(config, backup, 'missing-only')
    importer.run(str(backup))
    assert target.exists()
    with database.connection() as db:
        rows = db.execute('SELECT * FROM import_items WHERE job_id=%s ORDER BY id', (job,)).fetchall()
        assert sorted(r['state'] for r in rows) == ['duplicate', 'imported']
        assert next(r for r in rows if r['state'] == 'duplicate')['recovery_attempted']
        assert next(r for r in rows if r['state'] == 'duplicate')['media_id'] is None
        assert db.execute('SELECT first_import_job_id FROM media WHERE id=%s', (media['id'],)).fetchone()['first_import_job_id'] == original


def test_corrupt_destination_is_not_skipped(env):
    _, config, _ = env
    _, backup = seed(config)
    with database.connection() as db:
        media = db.execute('SELECT * FROM media').fetchone()
    target = Path(config['destination']['root'])/media['relative_path']
    target.write_bytes(b'corrupt fixture')
    job = create_job(config, backup, 'missing-only')
    with pytest.raises(OSError, match='incorrect checksum'):importer.run(str(backup))
    assert target.read_bytes() == b'corrupt fixture'
    with database.connection() as db:
        assert db.execute('SELECT skipped_duplicate_files FROM jobs WHERE id=%s', (job,)).fetchone()['skipped_duplicate_files'] == 0


def test_deleted_identity_stays_deleted(env):
    _, config, _ = env
    _, backup = seed(config)
    with database.connection() as db:
        media = db.execute('SELECT * FROM media').fetchone()
        db.execute('INSERT INTO media_trash(media_id,deleting_at) VALUES(%s,now())', (media['id'],))
    target = Path(config['destination']['root'])/media['relative_path']
    target.unlink()
    job = create_job(config, backup, 'missing-only')
    importer.run(str(backup))
    assert not target.exists()
    with database.connection() as db:
        assert db.execute("SELECT count(*) n FROM import_items WHERE job_id=%s AND state='duplicate' AND media_id IS NULL", (job,)).fetchone()['n'] == 1


def test_legacy_mode_normalized_on_resume_and_invalid_api_value(env, monkeypatch):
    client, config, _ = env
    source = Path(config['source']['root'])
    create_job(config, source, 'missing-only')
    monkeypatch.setattr('app.jobs.idle', lambda db: None)
    monkeypatch.setattr('app.imports.api.resume_group',lambda job_id,stage:{'job_id':job_id})
    response = client.post('/api/import/start', json={'source': str(source), 'import_mode': 'normal'})
    assert response.status_code == 200
    response = client.post('/api/import/start', json={'source': str(source), 'import_mode': 'typo'})
    assert response.status_code == 422


@pytest.mark.parametrize('legacy_mode',[None,'normal','missing-only'])
def test_start_api_has_one_policy_without_stored_mode(env, monkeypatch,legacy_mode):
    client, config, _ = env
    source = Path(config['source']['root'])
    monkeypatch.setattr('app.jobs.idle', lambda db: None)
    monkeypatch.setattr('app.storage.config.enroll_source', lambda config, source: {'root': source})
    monkeypatch.setattr('app.imports.api.resume_group', lambda job_id, stage: {'job_id': job_id})
    payload={'source':str(source),'auto_metadata':True,'auto_duplicates':True}
    if legacy_mode is not None:payload['import_mode']=legacy_mode
    response = client.post('/api/import/start', json=payload)
    assert response.status_code == 200
    with database.connection() as db:
        row = db.execute('SELECT * FROM jobs WHERE id=%s', (response.json()['job_id'],)).fetchone()
        assert 'import_mode' not in row
        assert row['auto_metadata'] and row['auto_duplicates'] and not row['auto_conversion']


def test_recovery_audit_survives_interruption_after_copy(env, monkeypatch):
    _, config, _ = env
    _, backup = seed(config)
    with database.connection() as db:
        media = db.execute('SELECT * FROM media').fetchone()
    target = Path(config['destination']['root'])/media['relative_path']
    target.unlink()
    job = create_job(config, backup, 'missing-only')
    real_copy = importer.copy_verified
    def interrupted(*args, **kwargs):
        real_copy(*args, **kwargs)
        raise OSError('Simulated interruption after copy publication')
    monkeypatch.setattr(importer, 'copy_verified', interrupted)
    with pytest.raises(OSError):importer.run(str(backup))
    monkeypatch.setattr(importer, 'copy_verified', real_copy)
    importer.run(str(backup))
    with database.connection() as db:
        assert db.execute('SELECT state FROM import_items WHERE job_id=%s', (job,)).fetchone()['state'] == 'duplicate'
        assert db.execute('SELECT skipped_duplicate_files FROM jobs WHERE id=%s', (job,)).fetchone()['skipped_duplicate_files'] == 0


def test_skip_refuses_incomplete_inventory(env):
    _, config, _ = env
    source = Path(config['source']['root'])
    job = create_job(config, source, 'missing-only')
    with database.connection() as db:
        item = db.execute("INSERT INTO import_items(job_id,source_path,size,mtime_ns) VALUES(%s,'fixture',4,0) RETURNING id", (job,)).fetchone()['id']
        assert not skip_duplicate(db, job, item)
        assert db.execute('SELECT count(*) n FROM import_items WHERE job_id=%s', (job,)).fetchone()['n'] == 1
