"""What macOS exFAT does to a backup, simulated so every platform runs these checks.

Sidecars are real-format files placed by hand, and a disk without hard links is the
claim path called directly. Only a Mac with an exFAT disk proves the real behaviour.
"""
from contextlib import closing
import errno
import hashlib
import json
import os
from pathlib import Path
import sqlite3

import pytest

from app.backup import media_copy, media_inventory, media_tree
from app.backup.library import verify_library
from app.backup.library_restore import equivalent_inventories
from app.backup.encrypted import InvalidArtifact
from app.backup.media_copy import abandoned_claim, copy_media_file, InvalidCopy, publish_by_claim, publish_no_clobber
from app.backup.media_inventory import capture_media_inventory, InvalidMediaInventory, verify_media_inventory
from app.backup.media_tree import copy_media_tree, verify_media_tree
from app.backup.sidecars import APPLE_DOUBLE, DS_STORE, is_sidecar
from tests.test_backup_capture_retry import ample, interrupted, stop_at, world  # noqa: F401


APPLE_FILE = APPLE_DOUBLE + b'\x00' * 60
FINDER_FILE = DS_STORE + b'\x00' * 60


def media_with(tmp_path, *names):
    media = tmp_path / 'media'
    (media / 'album').mkdir(parents=True)
    (media / 'album/one.jpg').write_bytes(b'one')
    (media / 'two.jpg').write_bytes(b'two')
    for name in names:
        (media / name).write_bytes(APPLE_FILE)
    return media


def inventory_of(tmp_path, source, name='inventory', **kwargs):
    capture_media_inventory(source, tmp_path / name, offline_confirmed=True, guard_factory=ample, **kwargs)
    return tmp_path / name


def paths_in(inventory):
    with closing(sqlite3.connect(inventory / 'inventory.sqlite3')) as db:
        return {row[0] for row in db.execute("SELECT path FROM entries WHERE kind='file'")}


@pytest.mark.parametrize('name, content, sibling, expected', [
    ('._photo.jpg', APPLE_FILE, True, True),
    ('._photo.jpg', APPLE_FILE, False, False),
    ('._photo.jpg', b'real photo bytes', True, False),
    ('._photo.jpg', b'', True, False),
    ('.DS_Store', FINDER_FILE, False, True),
    ('.DS_Store', b'real file', False, False),
    ('._', APPLE_FILE, False, False),
    ('.photo.jpg', APPLE_FILE, True, False),
    ('photo.jpg', APPLE_FILE, True, False),
], ids=['apple-double', 'no-sibling', 'wrong-format', 'empty', 'ds-store', 'wrong-store', 'bare-prefix', 'other-dot', 'plain'])
def test_only_the_real_format_is_a_sidecar(tmp_path, name, content, sibling, expected):
    if sibling and name != 'photo.jpg':
        (tmp_path / 'photo.jpg').write_bytes(b'x')
    path = tmp_path / name
    path.write_bytes(content)
    assert is_sidecar(path) is expected


def test_a_folder_named_like_a_sidecar_is_not_one(tmp_path):
    (tmp_path / 'album').mkdir()
    (tmp_path / '._album').mkdir()
    assert is_sidecar(tmp_path / '._album') is False


def test_a_sidecar_can_describe_a_folder(tmp_path):
    (tmp_path / 'album').mkdir()
    (tmp_path / '._album').write_bytes(APPLE_FILE)
    assert is_sidecar(tmp_path / '._album') is True


def test_a_control_folder_sidecar_can_outlive_its_file(tmp_path):
    path = tmp_path / '._complete.part'
    path.write_bytes(APPLE_FILE)
    assert is_sidecar(path) is False
    assert is_sidecar(path, needs_sibling=False) is True


def test_a_hard_linked_sidecar_is_not_trusted(tmp_path):
    (tmp_path / 'photo.jpg').write_bytes(b'x')
    (tmp_path / 'elsewhere').write_bytes(APPLE_FILE)
    try:
        os.link(tmp_path / 'elsewhere', tmp_path / '._photo.jpg')
    except OSError:
        pytest.skip('This filesystem has no hard links')
    assert is_sidecar(tmp_path / '._photo.jpg') is False


def test_a_source_library_keeps_every_file_it_has(tmp_path):
    """Capture from a library never drops a file because of its name or format."""
    source = media_with(tmp_path, '._two.jpg')
    assert '._two.jpg' in paths_in(inventory_of(tmp_path, source))


def test_reading_a_backup_ignores_sidecars_the_backup_does_not_list(tmp_path):
    source = media_with(tmp_path)
    authenticated = inventory_of(tmp_path, source)
    (source / '._two.jpg').write_bytes(APPLE_FILE)
    (source / 'album/._one.jpg').write_bytes(APPLE_FILE)
    (source / '._album').write_bytes(APPLE_FILE)
    (source / '.DS_Store').write_bytes(FINDER_FILE)
    observed = inventory_of(tmp_path, source, 'observed', authenticated=authenticated / 'inventory.sqlite3')
    assert paths_in(observed) == {'album/one.jpg', 'two.jpg'}
    equivalent_inventories(authenticated / 'inventory.sqlite3', observed / 'inventory.sqlite3')
    assert verify_media_inventory(observed, source)['files'] == 2


def test_a_file_the_backup_lists_is_kept_even_if_it_looks_like_a_sidecar(tmp_path):
    source = media_with(tmp_path, '._two.jpg')
    authenticated = inventory_of(tmp_path, source)
    observed = inventory_of(tmp_path, source, 'observed', authenticated=authenticated / 'inventory.sqlite3')
    assert '._two.jpg' in paths_in(observed)
    (source / '._two.jpg').unlink()
    with pytest.raises(InvalidMediaInventory):
        verify_media_inventory(observed, source)


def test_a_real_extra_file_still_fails_the_backup_comparison(tmp_path):
    source = media_with(tmp_path)
    authenticated = inventory_of(tmp_path, source)
    (source / '._two.jpg').write_bytes(b'not a sidecar')
    observed = inventory_of(tmp_path, source, 'observed', authenticated=authenticated / 'inventory.sqlite3')
    with pytest.raises(InvalidArtifact, match='differs'):
        equivalent_inventories(authenticated / 'inventory.sqlite3', observed / 'inventory.sqlite3')


def test_an_ordinary_inventory_stays_strict_about_new_sidecars(tmp_path):
    source = media_with(tmp_path)
    inventory = inventory_of(tmp_path, source)
    (source / '._two.jpg').write_bytes(APPLE_FILE)
    with pytest.raises(InvalidMediaInventory, match='changed'):
        verify_media_inventory(inventory, source)


@pytest.fixture
def copied(tmp_path):
    source = media_with(tmp_path)
    inventory = inventory_of(tmp_path, source)
    target = tmp_path / 'copy'
    copy_media_tree(source, inventory, target, offline_confirmed=True, guard_factory=ample)
    return source, inventory, target


def test_a_copied_tree_verifies_with_sidecars_beside_every_entry(copied):
    _, _, target = copied
    (target / 'media/._two.jpg').write_bytes(APPLE_FILE)
    (target / 'media/._album').write_bytes(APPLE_FILE)
    (target / 'media/album/._one.jpg').write_bytes(APPLE_FILE)
    (target / 'media/.DS_Store').write_bytes(FINDER_FILE)
    assert verify_media_tree(target)['files'] == 2


def test_a_look_alike_in_a_copied_tree_is_still_refused(copied):
    _, _, target = copied
    (target / 'media/._two.jpg').write_bytes(b'not a sidecar')
    with pytest.raises(InvalidMediaInventory, match='Unexpected'):
        verify_media_tree(target)


def test_a_sidecar_without_its_file_in_a_copied_tree_is_refused(copied):
    _, _, target = copied
    (target / 'media/._gone.jpg').write_bytes(APPLE_FILE)
    with pytest.raises(InvalidMediaInventory, match='Unexpected'):
        verify_media_tree(target)


def test_sidecars_do_not_hide_missing_media(copied):
    _, _, target = copied
    (target / 'media/._two.jpg').write_bytes(APPLE_FILE)
    (target / 'media/two.jpg').unlink()
    with pytest.raises(InvalidMediaInventory):
        verify_media_tree(target)


def test_a_listed_look_alike_in_a_copied_tree_is_still_verified(tmp_path):
    source = media_with(tmp_path, '._two.jpg')
    inventory = inventory_of(tmp_path, source)
    target = tmp_path / 'copy'
    copy_media_tree(source, inventory, target, offline_confirmed=True, guard_factory=ample)
    assert verify_media_tree(target)['files'] == 3
    (target / 'media/._two.jpg').write_bytes(APPLE_DOUBLE + b'\xff' * 60)
    with pytest.raises(InvalidMediaInventory, match='verification'):
        verify_media_tree(target)


def test_a_copy_retries_with_sidecars_in_its_folders(tmp_path):
    source = media_with(tmp_path)
    inventory = inventory_of(tmp_path, source)
    target = tmp_path / 'copy'

    def stop(event):
        if event.get('phase') == 'copy':
            raise KeyError('stop')
    with pytest.raises(KeyError):
        copy_media_tree(source, inventory, target, offline_confirmed=True, guard_factory=ample, progress=stop)
    (target / '._copy-job.json').write_bytes(APPLE_FILE)
    (target / '.DS_Store').write_bytes(FINDER_FILE)
    (target / '._media').write_bytes(APPLE_FILE)
    (target / 'media/._album').write_bytes(APPLE_FILE)
    for job in (target / 'file-jobs').iterdir():
        (job / '._copy.lock').write_bytes(APPLE_FILE)
    assert copy_media_tree(source, inventory, target, offline_confirmed=True, guard_factory=ample)['files'] == 2
    assert verify_media_tree(target)['files'] == 2


def test_an_unknown_file_in_a_copy_folder_is_still_refused(tmp_path):
    source = media_with(tmp_path)
    inventory = inventory_of(tmp_path, source)
    target = tmp_path / 'copy'
    copy_media_tree(source, inventory, target, offline_confirmed=True, guard_factory=ample)
    (target / '._unexpected').write_bytes(b'not a sidecar')
    with pytest.raises(InvalidMediaInventory, match='Unowned'):
        copy_media_tree(source, inventory, target, offline_confirmed=True, guard_factory=ample)


def test_a_new_copy_folder_holding_only_a_sidecar_is_not_unowned(tmp_path, monkeypatch):
    source = media_with(tmp_path)
    inventory = inventory_of(tmp_path, source)
    real = media_tree.create_private_directory

    def made_with_sidecar(path, **kwargs):
        made = real(path, **kwargs)
        (made / '._copy.lock').write_bytes(APPLE_FILE)
        return made
    monkeypatch.setattr(media_tree, 'create_private_directory', made_with_sidecar)
    assert copy_media_tree(source, inventory, tmp_path / 'copy', offline_confirmed=True, guard_factory=ample)['files'] == 2


def test_a_file_job_refuses_anything_but_a_real_sidecar(tmp_path):
    source = tmp_path / 'photo.jpg'
    source.write_bytes(b'photo')
    digest = hashlib.sha256(b'photo').hexdigest()
    job = tmp_path / 'job'
    job.mkdir()
    (job / '._copy.lock').write_bytes(APPLE_FILE)
    assert copy_media_file(source, job, digest, guard_factory=ample).read_bytes() == b'photo'
    (job / '._other').write_bytes(b'not a sidecar')
    with pytest.raises(InvalidCopy, match='Unexpected'):
        copy_media_file(source, job, digest, guard_factory=ample)


def test_a_capture_retries_with_sidecars_in_its_folder(world):  # noqa: F811
    interrupted(world, progress=stop_at('copy', 1))
    (world.backup / '._journal.json').write_bytes(APPLE_FILE)
    (world.backup / '.DS_Store').write_bytes(FINDER_FILE)
    payload = world.capture(resume=True)
    assert verify_library(world.backup, world.key) == payload


def test_a_capture_retry_refuses_a_look_alike_in_its_folder(world):  # noqa: F811
    interrupted(world, progress=stop_at('copy', 1))
    (world.backup / '._journal.json').write_bytes(b'not a sidecar')
    with pytest.raises(InvalidMediaInventory, match='Unowned'):
        world.capture(resume=True)


def empty_file_inventory(tmp_path):
    source = media_with(tmp_path)
    (source / 'empty.bin').write_bytes(b'')
    return source, inventory_of(tmp_path, source)


def change_inode(inventory, path):
    with closing(sqlite3.connect(inventory / 'inventory.sqlite3')) as db:
        db.execute("UPDATE entries SET inode='12345' WHERE path=?", (path,))
        db.commit()


def test_an_empty_file_may_get_a_new_inode_on_the_same_disk(tmp_path):
    source, inventory = empty_file_inventory(tmp_path)
    change_inode(inventory, 'empty.bin')
    assert verify_media_inventory(inventory, source)['files'] == 3


def test_an_empty_file_with_a_new_inode_is_refused_on_another_disk(tmp_path, monkeypatch):
    source, inventory = empty_file_inventory(tmp_path)
    change_inode(inventory, 'empty.bin')
    monkeypatch.setattr(media_inventory, 'volume_identity', lambda path: {'kind': 'test', 'id': 'other-disk'})
    with pytest.raises(InvalidMediaInventory, match='changed'):
        verify_media_inventory(inventory, source)


def test_an_empty_file_with_a_new_inode_needs_a_recorded_volume(tmp_path):
    source, inventory = empty_file_inventory(tmp_path)
    change_inode(inventory, 'empty.bin')
    with closing(sqlite3.connect(inventory / 'inventory.sqlite3')) as db:
        db.execute("DELETE FROM metadata WHERE key='volume'")
        db.commit()
    with pytest.raises(InvalidMediaInventory, match='changed'):
        verify_media_inventory(inventory, source)


def test_a_file_with_content_may_not_get_a_new_inode(tmp_path):
    source, inventory = empty_file_inventory(tmp_path)
    change_inode(inventory, 'two.jpg')
    with pytest.raises(InvalidMediaInventory, match='changed'):
        verify_media_inventory(inventory, source)


def test_an_empty_file_that_gained_content_is_refused(tmp_path):
    source, inventory = empty_file_inventory(tmp_path)
    change_inode(inventory, 'empty.bin')
    (source / 'empty.bin').write_bytes(b'content now')
    with pytest.raises(InvalidMediaInventory, match='changed'):
        verify_media_inventory(inventory, source)


def test_an_empty_file_time_must_still_match(tmp_path):
    source, inventory = empty_file_inventory(tmp_path)
    change_inode(inventory, 'empty.bin')
    with closing(sqlite3.connect(inventory / 'inventory.sqlite3')) as db:
        db.execute("UPDATE entries SET mtime='1000' WHERE path='empty.bin'")
        db.commit()
    with pytest.raises(InvalidMediaInventory, match='changed'):
        verify_media_inventory(inventory, source)


def interrupted_empty_job(tmp_path):
    source = tmp_path / 'empty.bin'
    source.write_bytes(b'')
    job = tmp_path / 'job'
    job.mkdir()
    digest = hashlib.sha256(b'').hexdigest()
    info = os.stat(source)
    record = {'format': 'familyfoto-media-copy-v1', 'sha256': digest,
              'source': [info.st_dev, info.st_ino + 1, 0, info.st_mtime_ns],
              'directory': list(media_copy.directory_identity(job)),
              'source_volume': media_copy.volume_identity(source.parent), 'target_volume': media_copy.volume_identity(job),
              'offset': 0, 'prefix_sha256': digest, 'state': 'copying'}
    (job / 'copy.json').write_text(json.dumps(record))
    return source, job, digest


def test_a_file_job_accepts_an_empty_source_with_a_new_inode(tmp_path):
    source, job, digest = interrupted_empty_job(tmp_path)
    assert copy_media_file(source, job, digest, guard_factory=ample).read_bytes() == b''


def test_a_file_job_still_checks_other_fields_of_an_empty_source(tmp_path):
    source, job, digest = interrupted_empty_job(tmp_path)
    record = json.loads((job / 'copy.json').read_text())
    record['source'][3] += 1
    (job / 'copy.json').write_text(json.dumps(record))
    with pytest.raises(InvalidCopy, match='changed'):
        copy_media_file(source, job, digest, guard_factory=ample)


def test_a_file_job_refuses_a_new_inode_for_a_source_with_content(tmp_path):
    source = tmp_path / 'photo.jpg'
    source.write_bytes(b'photo')
    job = tmp_path / 'job'
    job.mkdir()
    digest = hashlib.sha256(b'photo').hexdigest()
    info = os.stat(source)
    record = {'format': 'familyfoto-media-copy-v1', 'sha256': digest,
              'source': [info.st_dev, info.st_ino + 1, 5, info.st_mtime_ns],
              'directory': list(media_copy.directory_identity(job)),
              'source_volume': media_copy.volume_identity(source.parent), 'target_volume': media_copy.volume_identity(job),
              'offset': 0, 'prefix_sha256': hashlib.sha256(b'').hexdigest(), 'state': 'copying'}
    (job / 'copy.json').write_text(json.dumps(record))
    with pytest.raises(InvalidCopy, match='changed'):
        copy_media_file(source, job, digest, guard_factory=ample)


def test_a_file_job_refuses_another_volume_for_an_empty_source(tmp_path):
    source, job, digest = interrupted_empty_job(tmp_path)
    with pytest.raises(InvalidCopy, match='changed'):
        copy_media_file(source, job, digest, guard_factory=ample,
                        volume_probe=lambda path: {'kind': 'test', 'id': 'other-disk'})


def test_claim_publication_moves_the_file(tmp_path):
    (tmp_path / 'source').write_bytes(b'verified')
    publish_by_claim(tmp_path / 'source', tmp_path / 'target')
    assert (tmp_path / 'target').read_bytes() == b'verified'
    assert not (tmp_path / 'source').exists()


def test_claim_publication_never_replaces_an_existing_file(tmp_path):
    (tmp_path / 'source').write_bytes(b'verified')
    (tmp_path / 'target').write_bytes(b'someone elses file')
    with pytest.raises(FileExistsError):
        publish_by_claim(tmp_path / 'source', tmp_path / 'target')
    assert (tmp_path / 'target').read_bytes() == b'someone elses file'
    assert (tmp_path / 'source').read_bytes() == b'verified'


def test_claim_publication_stops_if_the_claim_gained_content(tmp_path, monkeypatch):
    (tmp_path / 'source').write_bytes(b'verified')
    real = os.close

    def another_writer(descriptor):
        real(descriptor)
        if (tmp_path / 'target').exists():
            (tmp_path / 'target').write_bytes(b'written meanwhile')
    monkeypatch.setattr(media_copy.os, 'close', another_writer)
    with pytest.raises(InvalidCopy, match='claimed'):
        publish_by_claim(tmp_path / 'source', tmp_path / 'target')
    monkeypatch.undo()
    assert (tmp_path / 'target').read_bytes() == b'written meanwhile'
    assert (tmp_path / 'source').read_bytes() == b'verified'


@pytest.mark.skipif(os.name == 'nt', reason='Windows rename already refuses an existing target')
@pytest.mark.parametrize('code', sorted(media_copy.NO_HARD_LINKS), ids=lambda code: errno.errorcode[code])
def test_a_disk_without_hard_links_uses_the_claim(tmp_path, monkeypatch, code):
    def refuse(*args):
        raise OSError(code, 'No hard links here')
    monkeypatch.setattr(media_copy.os, 'link', refuse)
    (tmp_path / 'source').write_bytes(b'verified')
    publish_no_clobber(tmp_path / 'source', tmp_path / 'target')
    assert (tmp_path / 'target').read_bytes() == b'verified'


@pytest.mark.skipif(os.name == 'nt', reason='Windows rename already refuses an existing target')
def test_other_link_failures_do_not_fall_back(tmp_path, monkeypatch):
    def refuse(*args):
        raise OSError(errno.ENOSPC, 'Disk full')
    monkeypatch.setattr(media_copy.os, 'link', refuse)
    (tmp_path / 'source').write_bytes(b'verified')
    with pytest.raises(OSError, match='Disk full'):
        publish_no_clobber(tmp_path / 'source', tmp_path / 'target')
    assert not (tmp_path / 'target').exists()


def test_publication_refuses_an_existing_target_on_this_system(tmp_path):
    (tmp_path / 'source').write_bytes(b'verified')
    (tmp_path / 'target').write_bytes(b'someone elses file')
    with pytest.raises(FileExistsError):
        publish_no_clobber(tmp_path / 'source', tmp_path / 'target')
    assert (tmp_path / 'target').read_bytes() == b'someone elses file'


@pytest.mark.parametrize('target_size, source_exists, expected', [
    (0, True, True), (5, True, False), (0, False, False)], ids=['claim', 'real-file', 'nothing-to-publish'])
def test_only_an_empty_target_with_a_waiting_file_is_an_abandoned_claim(tmp_path, target_size, source_exists, expected):
    (tmp_path / 'target').write_bytes(b'x' * target_size)
    if source_exists:
        (tmp_path / 'source').write_bytes(b'verified')
    assert abandoned_claim(tmp_path / 'target', tmp_path / 'source') is expected


def use_claims(monkeypatch):
    """Behave like a disk with no hard links, on any platform."""
    monkeypatch.setattr(media_copy, 'publish_no_clobber', publish_by_claim)
    monkeypatch.setattr(media_tree, 'publish_no_clobber', publish_by_claim)


def test_a_whole_capture_works_without_hard_links(world, monkeypatch):  # noqa: F811
    use_claims(monkeypatch)
    payload = world.capture()
    assert verify_library(world.backup, world.key) == payload


def test_a_capture_retry_works_without_hard_links(world, monkeypatch):  # noqa: F811
    use_claims(monkeypatch)
    interrupted(world, progress=stop_at('copy', 1))
    payload = world.capture(resume=True)
    assert verify_library(world.backup, world.key) == payload


def stop_after_claiming(monkeypatch, name):
    """Stop once, at the replace that would publish a file with this name."""
    real = media_copy.os.replace
    stopped = []

    def replace(source, target):
        if Path(source).name == name and not stopped:
            stopped.append(Path(target))
            raise KeyError('stopped after claiming')
        return real(source, target)
    monkeypatch.setattr(media_copy.os, 'replace', replace)
    return stopped


def test_a_file_job_recovers_from_a_crash_after_claiming(tmp_path, monkeypatch):
    use_claims(monkeypatch)
    source = tmp_path / 'photo.jpg'
    source.write_bytes(b'photo')
    digest = hashlib.sha256(b'photo').hexdigest()
    job = tmp_path / 'job'
    job.mkdir()
    stop_after_claiming(monkeypatch, 'payload.part')
    with pytest.raises(KeyError):
        copy_media_file(source, job, digest, guard_factory=ample)
    assert (job / 'payload').stat().st_size == 0
    assert copy_media_file(source, job, digest, guard_factory=ample).read_bytes() == b'photo'


def test_a_tree_recovers_from_a_crash_after_claiming(tmp_path, monkeypatch):
    use_claims(monkeypatch)
    source = media_with(tmp_path)
    inventory = inventory_of(tmp_path, source)
    target = tmp_path / 'copy'
    stopped = stop_after_claiming(monkeypatch, 'payload')
    with pytest.raises(KeyError):
        copy_media_tree(source, inventory, target, offline_confirmed=True, guard_factory=ample)
    assert stopped[0].stat().st_size == 0
    assert copy_media_tree(source, inventory, target, offline_confirmed=True, guard_factory=ample)['files'] == 2
    assert verify_media_tree(target)['files'] == 2


def test_an_empty_media_file_is_not_mistaken_for_a_claim(tmp_path, monkeypatch):
    use_claims(monkeypatch)
    source = media_with(tmp_path)
    (source / 'empty.bin').write_bytes(b'')
    inventory = inventory_of(tmp_path, source)
    target = tmp_path / 'copy'
    copy_media_tree(source, inventory, target, offline_confirmed=True, guard_factory=ample)
    assert copy_media_tree(source, inventory, target, offline_confirmed=True, guard_factory=ample)['files'] == 3
    assert (target / 'media/empty.bin').read_bytes() == b''


def test_a_damaged_empty_copy_without_a_waiting_file_is_still_refused(tmp_path):
    source = media_with(tmp_path)
    inventory = inventory_of(tmp_path, source)
    target = tmp_path / 'copy'
    copy_media_tree(source, inventory, target, offline_confirmed=True, guard_factory=ample)
    (target / 'media/two.jpg').write_bytes(b'')
    with pytest.raises(InvalidMediaInventory, match='damaged'):
        copy_media_tree(source, inventory, target, offline_confirmed=True, guard_factory=ample)
