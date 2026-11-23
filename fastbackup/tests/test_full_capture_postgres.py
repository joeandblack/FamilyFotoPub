"""Opt-in real dump/restore drill; fixture refuses any non-disposable cluster."""
import hashlib
import json
from types import SimpleNamespace

import psycopg
import pytest
from psycopg import sql

from app.backup.postgres import Endpoint
from fastbackup.capture import capture_library
from fastbackup.restore import restore_library, verify
from tests.backup_keys import backup_key
from tests.test_backup_postgres import postgres, seed, ample, ROOT  # noqa: F401


def seed_full_library(params, source):
    native = seed(params, source)
    digest = hashlib.sha256(b'fixture photo').hexdigest()
    with psycopg.connect(**{**params, 'dbname': source}) as db:
        db.execute('ALTER TABLE media ADD COLUMN relative_path text, ADD COLUMN size bigint, '
                   'ADD COLUMN sha256 text, ADD COLUMN stored_sha256 text, '
                   'ADD COLUMN original_relative_path text, ADD COLUMN original_size bigint, '
                   'ADD COLUMN conversion_state text')
        db.execute("UPDATE media SET relative_path='photo.jpg',size=13,sha256=%s", (digest,))
        db.execute('CREATE TABLE media_trash(media_id integer, deleting_at timestamptz, deleted_at timestamptz)')
        db.execute('CREATE TABLE video_metadata(media_id integer, metadata jsonb)')
        db.execute('CREATE TABLE albums(id integer PRIMARY KEY, name text)')
        db.execute('INSERT INTO albums VALUES(10,\'Fixture album\')')
        db.execute('CREATE TABLE album_items(album_id integer REFERENCES albums(id), media_id integer REFERENCES media(id))')
        db.execute('INSERT INTO album_items VALUES(10,1)')
    return native


@pytest.mark.parametrize('validation_follows', [False, True])
def test_real_database_capture_restore_preserves_source_and_relationships(postgres, tmp_path, validation_follows):  # noqa: F811
    params, source, _, dump, pg_restore = postgres
    native = seed_full_library(params, source)
    config, data, media = [tmp_path / name for name in ('config', 'data', 'media')]
    for path in (config, data, media):
        path.mkdir()
    (config / 'local.json').write_text('{}')
    (media / 'photo.jpg').write_bytes(b'fixture photo')
    key = backup_key()
    backup, target = tmp_path / 'backup', tmp_path / 'restored'
    result = capture_library(Endpoint(params['host'], params['port'], source, params['user']), dump,
        config, data, media, backup, key, database_budget=1024**2, offline_confirmed=True,
        guard_factory=ample)
    assert verify(backup, key) == result
    events = []
    # Recovery must work with the original tree absent, not accidentally read it.
    media.rename(tmp_path / 'original-offline')
    from app.backup.library_restore import restore_library as application_restore
    restore = application_restore if validation_follows else restore_library
    options = {'validation_follows': True} if validation_follows else {}
    restored = restore(backup, key, target,
        Endpoint(params['host'], params['port'], 'postgres', params['user']), pg_restore,
        expected_cluster_directory=ROOT / 'temp/backup-plan-pg', database_budget=1024**2,
        progress=events.append, **options)
    try:
        assert restored['media_verified'] and not restored['activation_allowed']
        assert (target / 'media-copy/media/photo.jpg').read_bytes() == b'fixture photo'
        assert (backup / 'media-copy/media/photo.jpg').read_bytes() == b'fixture photo'
        with psycopg.connect(**{**params, 'dbname': restored['database']}) as db:
            assert db.execute('SELECT album_id, media_id FROM album_items').fetchall() == [(10, 1)]
            assert db.execute(sql.SQL('SELECT name FROM {}.people').format(sql.Identifier(native))).fetchone()[0] == 'synthetic person'
        with psycopg.connect(**{**params, 'dbname': source}) as db:
            assert db.execute('SELECT count(*) FROM media').fetchone()[0] == 1
    finally:
        with psycopg.connect(**params, autocommit=True) as db:
            db.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(restored['database'])))


def test_real_fast_full_runner_capture(postgres, tmp_path, monkeypatch):  # noqa: F811
    from app.backup import admin_runner
    params, source, _, dump, _ = postgres
    seed_full_library(params, source)
    config, data, media = [tmp_path / name for name in ('config', 'data', 'media')]
    for path in (config, data, media):
        path.mkdir()
    settings = dict(database=dict(host=params['host'], port=params['port'], dbname=source, user=params['user']),
                    destination=dict(root=str(media)))
    (config / 'local.json').write_text(json.dumps(settings))
    (media / 'photo.jpg').write_bytes(b'fixture photo')
    monkeypatch.setattr(admin_runner, 'configuration_root', lambda: config)
    monkeypatch.setattr(admin_runner, 'DATA', data)
    backup = tmp_path / 'backup'
    value = dict(id='runner-fixture', copy_strategy='fast-full', verification='full', directory=str(backup),
                 destination=str(tmp_path), pg_dump=str(dump), database_budget=1024**2, allow_unprotected=False)
    events, notes = [], []
    run = SimpleNamespace(value=value, update=lambda **changes: value.update(changes),
                          progress=events.append, note=notes.append)
    key = backup_key()
    # No engine, worker, pg_dump, space checks or verifier is mocked.
    # This calls capture only: never stop/start services or bind a progress server.
    admin_runner.capture(run, settings, key)
    result = verify(backup, key)
    assert value['verified_at'] and value['assurance'] == 'full'
    assert value['capture_id'] == result['id'] and value['media_files'] == 1
    assert (backup / 'media-copy/media/photo.jpg').read_bytes() == b'fixture photo'
    assert (media / 'photo.jpg').read_bytes() == b'fixture photo'
    assert any(event.get('phase') == 'media-transfer' for event in events)
    assert any(event.get('phase') == 'media-verification' for event in events)
    assert any(admin_runner.progress_note(event) for event in events)
    assert 'Full-copy capture and verification completed' in notes[-1]
    with psycopg.connect(**{**params, 'dbname': source}) as db:
        assert db.execute('SELECT count(*) FROM media').fetchone()[0] == 1
