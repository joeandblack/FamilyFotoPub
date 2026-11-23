"""One-pass capture: the library is read once, and its copies are proven later."""
import hashlib
import json

import psycopg
from psycopg import sql
import pytest

from app.backup import library, media_copy
from app.backup.library import capture_library, verify_library, verify_library_metadata
from app.backup.library_restore import restore_library
from app.backup.media_copy import InvalidCopy
from app.backup.media_inventory import InvalidMediaInventory
from app.backup.postgres import Endpoint
from tests.test_backup_postgres import ROOT, ample, postgres, seed  # noqa: F401
from tests.backup_keys import backup_key


PHOTO = b'fixture photo'
STRAY = b'a file the catalog does not list'


@pytest.fixture
def source(postgres, tmp_path):  # noqa: F811
    params, name, _, dump, restore = postgres
    seed(params, name)
    with psycopg.connect(**{**params, 'dbname': name}) as db:
        db.execute('ALTER TABLE media ADD COLUMN relative_path text, ADD COLUMN size bigint, '
                   'ADD COLUMN sha256 text, ADD COLUMN stored_sha256 text, '
                   'ADD COLUMN original_relative_path text, ADD COLUMN original_size bigint, '
                   'ADD COLUMN conversion_state text')
        db.execute("UPDATE media SET relative_path='trip/photo.jpg',size=%s,sha256=%s",
                   (len(PHOTO), hashlib.sha256(PHOTO).hexdigest()))
        db.execute('CREATE TABLE media_trash(media_id integer, deleting_at timestamptz, deleted_at timestamptz)')
        db.execute('CREATE TABLE video_metadata(media_id integer, metadata jsonb)')
    config, data, media = (tmp_path / folder for folder in ('config', 'data', 'media'))
    for root in (config, data, media / 'trip'):
        root.mkdir(parents=True)
    (config / 'local.json').write_text('{}')
    (media / 'trip/photo.jpg').write_bytes(PHOTO)
    (media / 'stray.bin').write_bytes(STRAY)
    key = backup_key()
    endpoint = Endpoint(params['host'], params['port'], name, params['user'])

    def capture(backup, **kwargs):
        return capture_library(endpoint, dump, config, data, media, backup, key, database_budget=1024 ** 2,
                               offline_confirmed=True, guard_factory=ample, **kwargs)

    def restore_into(backup, target):
        admin = Endpoint(params['host'], params['port'], 'postgres', params['user'])
        return restore_library(backup, key, target, admin, restore, database_budget=1024 ** 2,
                               expected_cluster_directory=ROOT / 'temp/backup-plan-pg', guard_factory=ample)
    try:
        yield capture, restore_into, key, media, tmp_path
    finally:
        with psycopg.connect(**params, autocommit=True) as db:
            for record in tmp_path.rglob('database-target.json'):
                db.execute(sql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(
                    sql.Identifier(json.loads(record.read_text())['database'])))


@pytest.fixture
def work(monkeypatch):
    """Which source files were hashed for the inventory, and how many copies were read back."""
    seen = {'hashed': [], 'read_back': 0}
    real_hash, real_prefix = library.hash_file, media_copy.prefix_digest

    def hashed(path, expected):
        seen['hashed'].append(path.name)
        return real_hash(path, expected)

    def prefix(handle, size, chunk_size):
        # The first call per file checks the resumed prefix, of nothing. Any other reads a copy.
        if size:
            seen['read_back'] += 1
        return real_prefix(handle, size, chunk_size)
    monkeypatch.setattr(library, 'hash_file', hashed)
    monkeypatch.setattr(media_copy, 'prefix_digest', prefix)
    return seen


def timing(backup):
    return json.loads((backup / 'capture-timing.json').read_text())


def test_one_pass_takes_checksums_from_the_catalog_and_reads_no_copy_back(source, work):
    capture, _, key, _, base = source
    payload = capture(base / 'backup', single_pass=True)
    # Only the file the catalog doesn't list had to be hashed for the inventory.
    assert work == {'hashed': ['stray.bin'], 'read_back': 0}
    assert payload['media_read_back'] is False and payload['media_files'] == 2
    assert (base / 'backup/media-copy/media/trip/photo.jpg').read_bytes() == PHOTO
    assert (base / 'backup/media-copy/media/stray.bin').read_bytes() == STRAY
    assert json.loads((base / 'backup/media-copy/TREE-COMPLETE.json').read_text())['content_verified'] is False
    # The full check still passes, and is what makes it a backup.
    assert verify_library(base / 'backup', key) == payload


def test_the_usual_capture_still_reads_every_copy_back(source, work):
    capture, _, key, _, base = source
    payload = capture(base / 'backup')
    assert work['hashed'] == [] and work['read_back'] == 2
    assert 'media_read_back' not in payload
    assert json.loads((base / 'backup/media-copy/TREE-COMPLETE.json').read_text())['content_verified'] is True


@pytest.mark.parametrize('single_pass', [False, True])
def test_timing_names_every_phase_and_no_file(source, single_pass):
    capture, _, _, _, base = source
    capture(base / 'backup', single_pass=single_pass)
    record = timing(base / 'backup')
    assert record['single_pass'] is single_pass and (record['media_files'], record['media_bytes']) == (
        2, len(PHOTO) + len(STRAY))
    assert set(record['phase_seconds']) == {'media-inventory', 'reconcile', 'catalog', 'thumbnails', 'media-copy'}
    work = set(record['copy_seconds'])
    assert {'source_read', 'hash', 'destination_write', 'flush_to_disk', 'checkpoint', 'publish'} <= work
    assert ('read_back' in work) is not single_pass
    raw = (base / 'backup/capture-timing.json').read_text()
    assert 'photo' not in raw and 'stray' not in raw and str(base) not in raw


def test_a_source_file_that_differs_from_the_catalog_stops_a_one_pass_capture(source):
    capture, _, key, media, base = source
    (media / 'trip/photo.jpg').write_bytes(b'fixture phot0')
    with pytest.raises(InvalidCopy, match='did not match'):
        capture(base / 'backup', single_pass=True)
    assert not (base / 'backup/library').exists()
    assert not (base / 'backup/media-copy/media/trip/photo.jpg').exists()
    # What happened up to the failure is still on record.
    assert 'media-copy' in timing(base / 'backup')['phase_seconds']


def test_a_file_of_another_size_than_the_catalog_says_is_hashed_and_refused(source, work):
    capture, _, _, media, base = source
    (media / 'trip/photo.jpg').write_bytes(PHOTO + b'!')
    with pytest.raises(InvalidMediaInventory, match='does not match'):
        capture(base / 'backup', single_pass=True)
    assert sorted(work['hashed']) == ['photo.jpg', 'stray.bin']
    assert not (base / 'backup/library').exists()


def test_a_copy_damaged_on_the_way_is_found_by_the_check_and_by_the_restore(source):
    capture, restore_into, key, _, base = source
    capture(base / 'backup', single_pass=True)
    (base / 'backup/media-copy/media/trip/photo.jpg').write_bytes(b'fixture phot0')
    assert verify_library_metadata(base / 'backup', key)['media_read_back'] is False
    with pytest.raises(InvalidMediaInventory, match='content'):
        verify_library(base / 'backup', key)
    with pytest.raises(InvalidMediaInventory, match='content'):
        restore_into(base / 'backup', base / 'recovered')


def test_a_one_pass_backup_restores(source):
    capture, restore_into, _, _, base = source
    capture(base / 'backup', single_pass=True)
    report = restore_into(base / 'backup', base / 'recovered')
    assert report['media_verified'] is True and report['media_files'] == 2
    assert (base / 'recovered/media-copy/media/trip/photo.jpg').read_bytes() == PHOTO


def test_a_timing_record_that_cannot_be_written_neither_fails_a_capture_nor_hides_its_error(source, monkeypatch):
    from app.backup import recovery
    capture, _, key, media, base = source
    real = recovery.save_private

    def full_disk(path, value):
        if path.name == 'capture-timing.json':
            raise OSError(28, 'No space left on device')
        return real(path, value)
    monkeypatch.setattr(recovery, 'save_private', full_disk)
    payload = capture(base / 'backup', single_pass=True)
    assert verify_library(base / 'backup', key) == payload and not (base / 'backup/capture-timing.json').exists()
    (media / 'trip/photo.jpg').write_bytes(b'fixture phot0')
    with pytest.raises(InvalidCopy, match='did not match'):
        capture(base / 'second', single_pass=True)
