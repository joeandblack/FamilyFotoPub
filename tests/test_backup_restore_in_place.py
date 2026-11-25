"""In-place restore: the backup's verified media becomes the recovered library's, by one rename."""
import hashlib
import json
import os
from pathlib import Path
import shutil

import psycopg
from psycopg import sql
import pytest

from app.backup import library_restore as module
from app.backup.diagnostics import ExplainedFailure
from app.backup.encrypted import InvalidArtifact
from app.backup.library import capture_library, media_copy_of, verify_library
from app.backup.library_restore import restore_library
from app.backup.media_inventory import InvalidMediaInventory
from app.backup.postgres import Endpoint
from tests.test_backup_postgres import ROOT, ample, postgres, seed  # noqa: F401
from tests.backup_keys import backup_key


PHOTO = b'fixture photo'
THUMBNAIL = b'fixture thumbnail'


@pytest.fixture
def library(postgres, tmp_path):  # noqa: F811
    params, source, _, dump, restore = postgres
    seed(params, source)
    with psycopg.connect(**{**params, 'dbname': source}) as db:
        db.execute('ALTER TABLE media ADD COLUMN relative_path text, ADD COLUMN size bigint, '
                   'ADD COLUMN sha256 text, ADD COLUMN stored_sha256 text, '
                   'ADD COLUMN original_relative_path text, ADD COLUMN original_size bigint, '
                   'ADD COLUMN conversion_state text')
        db.execute("UPDATE media SET relative_path='trip/photo.jpg',size=%s,sha256=%s",
                   (len(PHOTO), hashlib.sha256(PHOTO).hexdigest()))
        db.execute('CREATE TABLE media_trash(media_id integer, deleting_at timestamptz, deleted_at timestamptz)')
        db.execute('CREATE TABLE video_metadata(media_id integer, metadata jsonb)')
    config, data, media = (tmp_path / name for name in ('config', 'data', 'media'))
    for root in (config, data, media / 'trip'):
        root.mkdir(parents=True)
    (config / 'local.json').write_text('{}')
    (media / 'trip/photo.jpg').write_bytes(PHOTO)
    (data / 'thumbnails/ab').mkdir(parents=True)
    (data / 'thumbnails/ab/abcd.jpg').write_bytes(THUMBNAIL)
    key = backup_key()
    backup = tmp_path / 'backup'
    capture_library(Endpoint(params['host'], params['port'], source, params['user']), dump, config, data, media, backup, key,
                    database_budget=1024 ** 2, offline_confirmed=True, guard_factory=ample)
    admin = Endpoint(params['host'], params['port'], 'postgres', params['user'])
    def run(target, **kwargs):
        return restore_library(backup, key, target, admin, restore, expected_cluster_directory=ROOT / 'temp/backup-plan-pg',
                               database_budget=1024 ** 2, guard_factory=ample, **kwargs)
    try:
        yield backup, key, tmp_path, run
    finally:
        with psycopg.connect(**params, autocommit=True) as db:
            for record in tmp_path.rglob('database-target.json'):
                name = json.loads(record.read_text())['database']
                db.execute(sql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(sql.Identifier(name)))


def test_the_media_is_moved_not_copied_and_the_backup_is_used_up(library, monkeypatch):
    backup, key, base, run = library
    monkeypatch.setattr(module, 'copy_media_tree', lambda *a, **k: pytest.fail('An in-place restore must not copy media'))
    report = run(base / 'recovered', in_place=True)
    assert report['media_in_place'] is True and report['media_verified'] is True and report['activation_allowed'] is False
    assert report['thumbnails'] == 'verified'
    assert (base / 'recovered/media-copy/media/trip/photo.jpg').read_bytes() == PHOTO
    assert not (backup / 'media-copy').exists()
    moved = json.loads((backup / 'MEDIA-MOVED.json').read_text())
    assert moved['target'] == str(base / 'recovered') and moved['restoration_id'] == report['restoration_id']
    # Every later check still finds the backup's media, and still proves it against the sealed index.
    assert media_copy_of(backup) == base / 'recovered/media-copy'
    assert verify_library(backup, key)['media_files'] == 1
    settings = json.loads((base / 'recovered/catalog/files/config/local.json').read_text())
    assert settings['recovery']['media_root'] == str(base / 'recovered/media-copy/media')


def test_the_media_is_read_once_and_the_move_leaves_a_receipt_of_it(library, monkeypatch):
    from app.backup import media_receipt, media_tree
    backup, key, base, run = library
    hashed = []
    real = media_tree.hash_file
    monkeypatch.setattr(media_tree, 'hash_file', lambda path, expected: hashed.append(path) or real(path, expected))
    run(base / 'recovered', in_place=True)
    assert len(hashed) == 1
    copy = base / 'recovered/media-copy'
    receipt = media_receipt.read_receipt(base / 'recovered', key)
    assert receipt['tree'] == str(copy) and (receipt['files'], receipt['bytes']) == (1, len(PHOTO))
    assert media_receipt.verified_media(base / 'recovered', key, copy)['from_receipt'] is True
    assert len(hashed) == 1


def test_the_backup_keeps_thumbnails_and_an_activated_library_gets_them_back(library):
    from app.backup.thumbnails import restore_thumbnails
    backup, key, base, run = library
    assert verify_library(backup, key)['thumbnails']
    target = base / 'recovered'
    run(target, in_place=True)
    data = target / 'catalog/files/data'
    # Thumbnails are no part of the restored catalog, which validation checks file by file.
    assert not (data / 'thumbnails').exists()
    with pytest.raises(ExplainedFailure, match='Activate'):
        restore_thumbnails(backup, key, target, guard_factory=ample)
    settings = target / 'catalog/files/config/local.json'
    value = json.loads(settings.read_text())
    value['recovery'].update(state='activated', activation_required=False)
    settings.write_text(json.dumps(value))
    result = restore_thumbnails(backup, key, target, guard_factory=ample)
    assert (result['restored'], result['kept']) == (1, 0)
    assert (data / 'thumbnails/ab/abcd.jpg').read_bytes() == THUMBNAIL


@pytest.mark.parametrize('damage', ['changed', 'missing'])
def test_a_damaged_thumbnail_copy_fails_the_backup_check_but_never_a_restore(library, damage):
    from app.backup.library import verify_library_thumbnails
    from app.backup.thumbnails import restore_thumbnails
    backup, key, base, run = library
    chunk = backup / 'thumbnails/00000000.fernet'
    if damage == 'missing':
        shutil.rmtree(backup / 'thumbnails')
    else:
        raw = bytearray(chunk.read_bytes())
        raw[len(raw) // 2] ^= 1
        chunk.write_bytes(raw)
    payload = verify_library(backup, key)
    with pytest.raises((InvalidArtifact, OSError)):
        verify_library_thumbnails(backup, key, payload)
    # Thumbnails can be rebuilt. The photos and the catalog still restore, and the report says so.
    report = run(base / 'recovered', in_place=True)
    assert report['thumbnails'] == 'damaged' and report['media_verified'] is True
    assert (base / 'recovered/media-copy/media/trip/photo.jpg').read_bytes() == PHOTO
    settings = base / 'recovered/catalog/files/config/local.json'
    value = json.loads(settings.read_text())
    value['recovery'].update(state='activated', activation_required=False)
    settings.write_text(json.dumps(value))
    with pytest.raises((InvalidArtifact, OSError)):
        restore_thumbnails(backup, key, base / 'recovered', guard_factory=ample)
    assert not (base / 'recovered/catalog/files/data/thumbnails').exists()


def test_a_used_backup_cannot_be_restored_again(library):
    backup, _, base, run = library
    run(base / 'recovered', in_place=True)
    for kwargs in ({}, {'in_place': True}):
        with pytest.raises(ExplainedFailure, match='no longer a backup'):
            run(base / 'another', **kwargs)
        assert not (base / 'another').exists()


def test_another_disk_is_refused_before_anything_changes(library, monkeypatch):
    backup, _, base, run = library
    real = module.volume_identity
    monkeypatch.setattr(module, 'volume_identity',
                        lambda path: 'other-disk' if path == base else real(path))
    with pytest.raises(ExplainedFailure, match='same disk'):
        run(base / 'recovered', in_place=True)
    assert (backup / 'media-copy').is_dir() and not (backup / 'MEDIA-MOVED.json').exists()
    assert not (base / 'recovered').exists()


def test_a_damaged_backup_is_found_before_the_move(library):
    backup, _, base, run = library
    (backup / 'media-copy/media/trip/photo.jpg').write_bytes(b'fixture phot0')
    with pytest.raises(InvalidMediaInventory):
        run(base / 'recovered', in_place=True)
    assert (backup / 'media-copy').is_dir() and not (backup / 'MEDIA-MOVED.json').exists()


@pytest.mark.parametrize('stop', ['before-rename', 'after-rename'])
def test_a_stop_around_the_move_resumes(library, monkeypatch, stop):
    backup, key, base, run = library
    target = base / 'recovered'
    with monkeypatch.context() as patch:
        if stop == 'before-rename':
            real = module.os.rename

            def fail(source, destination, *args, **kwargs):
                if Path(source).name == Path(destination).name == 'media-copy':
                    raise OSError('Synthetic stop before the rename')
                return real(source, destination, *args, **kwargs)
            patch.setattr(module.os, 'rename', fail)
        else:
            def fail(*args, **kwargs):
                raise OSError('Synthetic stop after the rename')
            patch.setattr(module, 'verify_tree', fail)
        with pytest.raises(OSError, match='Synthetic stop'):
            run(target, in_place=True)
    assert (backup / 'MEDIA-MOVED.json').exists() and (target / 'LIBRARY-RESTORE-INCOMPLETE').exists()
    assert (backup / 'media-copy').exists() is (stop == 'before-rename')
    # Not without the same mode and target.
    with pytest.raises(ExplainedFailure, match='--in-place'):
        run(target, resume=True)
    report = run(target, resume=True, in_place=True)
    assert report['media_in_place'] is True and not (target / 'LIBRARY-RESTORE-INCOMPLETE').exists()
    assert (target / 'media-copy/media/trip/photo.jpg').read_bytes() == PHOTO
    assert not (backup / 'media-copy').exists()


@pytest.mark.parametrize('content', [b'[]', b'not json', b'{"format": "familyfoto-media-moved-v1", "target": "relative", '
                                     b'"restoration_id": "' + b'a' * 32 + b'"}'])
def test_a_damaged_moved_record_is_refused(tmp_path, content):
    from app.backup.library import moved_record
    (tmp_path / 'MEDIA-MOVED.json').write_bytes(content)
    with pytest.raises(InvalidArtifact, match='moved-media record'):
        moved_record(tmp_path)
    with pytest.raises(InvalidArtifact):
        media_copy_of(tmp_path)


def fail_the_rename(patch, error=PermissionError):
    real = module.os.rename

    def rename(source, destination, *args, **kwargs):
        if Path(source).name == Path(destination).name == 'media-copy':
            raise error('Synthetic failed rename: a file is open')
        return real(source, destination, *args, **kwargs)
    patch.setattr(module.os, 'rename', rename)


@pytest.mark.parametrize('in_place', [False, True])
def test_a_failed_rename_leaves_the_backup_whole(library, monkeypatch, in_place):
    backup, key, base, run = library
    with monkeypatch.context() as patch:
        fail_the_rename(patch)
        with pytest.raises(PermissionError):
            run(base / 'abandoned', in_place=True)
    # The record was written, but nothing moved. It must not make the backup look used up.
    assert (backup / 'MEDIA-MOVED.json').exists() and (backup / 'media-copy').is_dir()
    assert media_copy_of(backup) == backup / 'media-copy'
    assert verify_library(backup, key)['media_files'] == 1
    report = run(base / 'recovered', in_place=in_place)
    assert report['media_in_place'] is in_place
    assert (base / 'recovered/media-copy/media/trip/photo.jpg').read_bytes() == PHOTO
    if in_place:
        # The stale record was replaced by this restore's.
        assert json.loads((backup / 'MEDIA-MOVED.json').read_text())['target'] == str(base / 'recovered')
        assert media_copy_of(backup) == base / 'recovered/media-copy'


@pytest.fixture
def exfat_ssd(tmp_path, monkeypatch):
    """Folders under tmp_path/ssd behave like exFAT: permission changes silently do nothing."""
    from app.backup import private_storage
    ssd = tmp_path / 'ssd'
    ssd.mkdir()
    name = 'windows_acls_persist' if os.name == 'nt' else 'keeps_permissions'
    real = getattr(private_storage, name)
    monkeypatch.setattr(private_storage, name, lambda path: not Path(path).is_relative_to(ssd) and real(path))
    return ssd


def test_a_split_restore_puts_media_on_the_ssd_and_the_rest_on_a_protected_disk(library, exfat_ssd, monkeypatch):
    from app.backup.private_storage import UnprotectedDestination
    backup, key, base, run = library
    media = exfat_ssd / 'library'
    with pytest.raises(UnprotectedDestination):
        run(base / 'recovered', in_place=True, media_directory=media)
    # Refused before anything was made or moved.
    assert not (base / 'recovered').exists() and not media.exists()
    assert (backup / 'media-copy').is_dir() and not (backup / 'MEDIA-MOVED.json').exists()
    # The private part is never accepted on such a disk, whatever the flag.
    with pytest.raises(UnprotectedDestination):
        run(exfat_ssd / 'recovered', in_place=True, media_directory=media, allow_unprotected_media=True)
    assert not (exfat_ssd / 'recovered').exists() and not media.exists()

    monkeypatch.setattr(module, 'copy_media_tree', lambda *a, **k: pytest.fail('An in-place restore must not copy media'))
    report = run(base / 'recovered', in_place=True, media_directory=media, allow_unprotected_media=True)
    assert report['media_in_place'] is True and report['media_directory'] == str(media)
    assert report['media_root'] == str(media / 'media-copy/media')
    assert (media / 'media-copy/media/trip/photo.jpg').read_bytes() == PHOTO
    assert not (base / 'recovered/media-copy').exists() and not (backup / 'media-copy').exists()
    moved = json.loads((backup / 'MEDIA-MOVED.json').read_text())
    assert moved['target'] == str(base / 'recovered') and moved['media_directory'] == str(media)
    assert media_copy_of(backup) == media / 'media-copy'
    assert module.recovered_media_copy(base / 'recovered') == media / 'media-copy'
    assert verify_library(backup, key)['media_files'] == 1
    settings = json.loads((base / 'recovered/catalog/files/config/local.json').read_text())
    assert settings['recovery']['media_root'] == str(media / 'media-copy/media')
    with pytest.raises(ExplainedFailure, match='--media-directory'):
        run(base / 'another', in_place=True, media_directory=exfat_ssd / 'other')


def test_a_copy_restore_puts_media_on_an_accepted_exfat_disk(library, exfat_ssd):
    """The acceptance reaches the copy itself, not only the early folder check."""
    from app.backup.private_storage import UnprotectedDestination
    backup, key, base, run = library
    media = exfat_ssd / 'library'
    with pytest.raises(UnprotectedDestination):
        run(base / 'refused', media_directory=media)
    assert not media.exists()
    report = run(base / 'recovered', media_directory=media, allow_unprotected_media=True)
    assert report['media_in_place'] is False and report['media_root'] == str(media / 'media-copy/media')
    assert (media / 'media-copy/media/trip/photo.jpg').read_bytes() == PHOTO
    assert (backup / 'media-copy/media/trip/photo.jpg').read_bytes() == PHOTO


def test_a_split_media_folder_must_be_on_the_backups_disk(library, monkeypatch, tmp_path):
    backup, _, base, run = library
    (tmp_path / 'elsewhere').mkdir()
    real = module.volume_identity
    monkeypatch.setattr(module, 'volume_identity',
                        lambda path: 'other-disk' if path == tmp_path / 'elsewhere' else real(path))
    with pytest.raises(ExplainedFailure, match='media folder on the same disk'):
        run(base / 'recovered', in_place=True, media_directory=tmp_path / 'elsewhere/library')
    assert not (base / 'recovered').exists() and (backup / 'media-copy').is_dir()


def test_a_split_restore_refuses_unclear_folders(library):
    backup, _, base, run = library
    with pytest.raises(ValueError, match='Only a separate media folder'):
        run(base / 'recovered', in_place=True, allow_unprotected_media=True)
    for inside in (base / 'recovered/library', backup / 'library'):
        with pytest.raises(ValueError, match='separate'):
            run(base / 'recovered', in_place=True, media_directory=inside)
    (base / 'library').mkdir()
    with pytest.raises(FileExistsError):
        run(base / 'recovered', in_place=True, media_directory=base / 'library')
    assert not (base / 'recovered').exists() and (backup / 'media-copy').is_dir()


def test_a_split_restore_resumes_only_with_the_same_folders(library, monkeypatch):
    backup, _, base, run = library
    target, media = base / 'recovered', base / 'library'
    with monkeypatch.context() as patch:
        patch.setattr(module, 'verify_tree', lambda *a, **k: (_ for _ in ()).throw(OSError('Synthetic stop')))
        with pytest.raises(OSError):
            run(target, in_place=True, media_directory=media)
    assert (media / 'media-copy').is_dir() and not (backup / 'media-copy').exists()
    for kwargs in ({'in_place': True}, {'in_place': True, 'media_directory': base / 'other'}):
        with pytest.raises(ExplainedFailure, match=f'--media-directory {media}'.replace('\\', '\\\\')):
            run(target, resume=True, **kwargs)
    report = run(target, resume=True, in_place=True, media_directory=media)
    assert report['media_root'] == str(media / 'media-copy/media')
    assert (media / 'media-copy/media/trip/photo.jpg').read_bytes() == PHOTO


def test_a_stop_before_the_split_rename_resumes(library, monkeypatch):
    backup, _, base, run = library
    target, media = base / 'recovered', base / 'library'
    with monkeypatch.context() as patch:
        fail_the_rename(patch, OSError)
        with pytest.raises(OSError):
            run(target, in_place=True, media_directory=media)
    with pytest.raises(ExplainedFailure, match='--media-directory'):
        run(target, resume=True, in_place=True)
    report = run(target, resume=True, in_place=True, media_directory=media)
    assert report['media_directory'] == str(media) and not (backup / 'media-copy').exists()


def test_a_missing_media_folder_needs_review(library, monkeypatch):
    _, _, base, run = library
    target, media = base / 'recovered', base / 'library'
    with monkeypatch.context() as patch:
        patch.setattr(module, 'verify_tree', lambda *a, **k: (_ for _ in ()).throw(OSError('Synthetic stop')))
        with pytest.raises(OSError):
            run(target, in_place=True, media_directory=media)
    media.rename(base / 'moved-away')
    from app.backup.space import StorageUnavailable
    # The backup's own check finds its media gone, before the restore looks at its folders.
    with pytest.raises(StorageUnavailable):
        run(target, resume=True, in_place=True, media_directory=media)
    assert (target / 'LIBRARY-RESTORE-INCOMPLETE').exists() and not media.exists()


def test_media_in_both_places_needs_review(library, monkeypatch):
    backup, _, base, run = library
    target = base / 'recovered'
    with monkeypatch.context() as patch:
        patch.setattr(module, 'verify_tree', lambda *a, **k: (_ for _ in ()).throw(OSError('Synthetic stop')))
        with pytest.raises(OSError):
            run(target, in_place=True)
    # Someone put a second copy back into the backup by hand.
    shutil.copytree(target / 'media-copy', backup / 'media-copy')
    with pytest.raises(InvalidArtifact, match='review required'):
        run(target, resume=True, in_place=True)


def test_a_copy_restore_can_put_the_media_in_its_own_folder(library, monkeypatch, tmp_path):
    """A copy keeps the backup whole, and its media may go to another disk than the catalog."""
    backup, key, base, run = library
    target, media = base / 'recovered', tmp_path / 'photos-disk/library'
    media.parent.mkdir()
    real = module.volume_identity
    # Another disk is fine for a copy: only a move needs the backup's disk.
    monkeypatch.setattr(module, 'volume_identity', lambda path: 'photos' if Path(path).is_relative_to(media.parent) else real(path))
    report = run(target, media_directory=media)
    assert report['media_in_place'] is False and report['media_directory'] == str(media)
    assert report['media_root'] == str(media / 'media-copy/media')
    assert (media / 'media-copy/media/trip/photo.jpg').read_bytes() == PHOTO
    assert not (target / 'media-copy').exists()
    # The backup is untouched and still a backup.
    assert (backup / 'media-copy/media/trip/photo.jpg').read_bytes() == PHOTO and not (backup / 'MEDIA-MOVED.json').exists()
    assert module.recovered_media_copy(target) == media / 'media-copy'
    settings = json.loads((target / 'catalog/files/config/local.json').read_text())
    assert settings['recovery']['media_root'] == str(media / 'media-copy/media')
    # A receipt of the full read sits with the catalog, for the checks that follow.
    from app.backup.media_receipt import read_receipt
    assert read_receipt(target, key)['files'] == 1
    with pytest.raises(ExplainedFailure, match='normal restore and --media-directory'):
        run(target, resume=True)
