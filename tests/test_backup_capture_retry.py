"""Whole-library capture retry: reuse only verified work, and never publish early."""
from contextlib import closing, contextmanager
import hashlib
import errno
import time
import io
import json
import os
import sqlite3
import subprocess
import sys
from types import SimpleNamespace
import shutil

from cryptography.fernet import Fernet
import pytest

from app.backup import library, manage, media_inventory, media_tree
from app.backup.admission import acknowledge_failed_maintenance, MaintenanceBlocked
from app.backup.catalog import reference
from app.backup.encrypted import InvalidArtifact, seal_stream, verify_artifact
from app.backup.library import capture_library, verify_library
from app.backup.media_copy import copy_media_file, InvalidCopy
from app.backup.media_inventory import capture_media_inventory, verify_media_inventory, InvalidMediaInventory
from app.backup.recovery import save_private
from app.backup.space import directory_identity, SpaceGuard, StorageUnavailable
from app.backup.volumes import volume_identity
from tests.backup_keys import backup_key


class Interrupted(Exception):
    pass


def ample(requirements):
    return SpaceGuard(requirements, minimum_reserve=10, reserve_percent=0,
                      disk_usage=lambda path: SimpleNamespace(free=10 ** 9))


def fake_components(endpoint, pg_dump, config_root, data_root, directory, key, identity, **kwargs):
    directory.mkdir(mode=0o700)
    built = {}
    for name, kind in (('database', 'postgres-custom-dump'), ('state', 'familyfoto-state-tar-v1')):
        raw = f'{name} fixture'.encode()
        seal_stream(io.BytesIO(raw), directory / name, key, estimated_bytes=len(raw),
                    metadata={'kind': kind}, guard_factory=ample)
        built[name] = reference(verify_artifact(directory / name, key))
    return built


@pytest.fixture
def world(tmp_path, monkeypatch):
    @contextmanager
    def frozen(endpoint):
        yield object()

    config, data, media = (tmp_path / name for name in ('config', 'data', 'media'))
    for path in (config, data, media):
        path.mkdir()
    (media / 'album').mkdir()
    for name in ('album/one.jpg', 'album/two.jpg', 'three.jpg'):
        (media / name).write_bytes(name.encode())
    monkeypatch.setattr(library, 'freeze_database', frozen)
    monkeypatch.setattr(library, 'capture_components', fake_components)
    monkeypatch.setattr(library, 'reconcile_uploads', lambda db, root: None)
    monkeypatch.setattr(library, 'reconcile_media', lambda db, root: {})
    monkeypatch.setattr(library, 'state_inventory', lambda roots: [])
    key = backup_key()

    def capture(*, resume=False, retire_to=None, use_key=key, progress=None, root=media, backup=None, **kwargs):
        return capture_library(object(), None, config, data, root, backup or tmp_path / 'backup', use_key,
                               database_budget=1024, offline_confirmed=True, guard_factory=ample,
                               retire_to=retire_to, resume=resume, progress=progress, **kwargs)

    return SimpleNamespace(capture=capture, config=config, data=data, media=media, key=key,
                           backup=tmp_path / 'backup', tmp=tmp_path)


def acknowledge(world):
    marker = world.data / '.recovery-admission/maintenance.json'
    acknowledge_failed_maintenance(world.data, json.loads(marker.read_text())['id'])


def stop_at(phase, count=1):
    seen = []

    def progress(event):
        if event.get('phase') == phase:
            seen.append(event)
            if len(seen) == count:
                raise Interrupted(phase)
    return progress


def interrupted(world, **kwargs):
    with pytest.raises(Interrupted):
        world.capture(**kwargs)
    acknowledge(world)


def assert_no_recovery_point(world):
    assert not (world.backup / 'library/complete.fernet').exists()
    with pytest.raises(Exception):
        verify_library(world.backup, world.key)


def job_id(world):
    return json.loads((world.backup / 'journal.json').read_text())['id']


def fail_inventory(world, monkeypatch):
    interrupted(world, progress=stop_at('inventory', 2))


def fail_inventory_before_marker(world, monkeypatch):
    def nothing_yet(root, directory, **kwargs):
        directory.mkdir()
        raise Interrupted('inventory')
    with monkeypatch.context() as patch:
        patch.setattr(library, 'capture_media_inventory', nothing_yet)
        interrupted(world)


def fail_database(world, monkeypatch):
    def partial(endpoint, pg_dump, config_root, data_root, directory, key, identity, **kwargs):
        (directory / 'database').mkdir(parents=True)
        raise Interrupted('database')
    with monkeypatch.context() as patch:
        patch.setattr(library, 'capture_components', partial)
        interrupted(world)


def fail_before_copy_job(world, monkeypatch):
    def only_a_folder(source, inventory, target, **kwargs):
        target.mkdir()
        (target / 'copy.lock').touch()
        raise Interrupted('copy')
    with monkeypatch.context() as patch:
        patch.setattr(library, 'copy_media_tree', only_a_folder)
        interrupted(world)


def fail_during_copy(world, monkeypatch):
    interrupted(world, progress=stop_at('copy', 2))


def fail_after_copy(world, monkeypatch):
    real = library.reconcile_media
    calls = []

    def second_fails(db, inventory):
        calls.append(inventory)
        if len(calls) == 2:
            raise Interrupted('reconcile')
        return real(db, inventory)
    with monkeypatch.context() as patch:
        patch.setattr(library, 'reconcile_media', second_fails)
        interrupted(world)


def fail_publishing_catalog(world, monkeypatch):
    def refuse(*args, **kwargs):
        raise Interrupted('catalog')
    with monkeypatch.context() as patch:
        patch.setattr(library, 'publish_catalog', refuse)
        interrupted(world)


def fail_sealing_library(world, monkeypatch):
    def partial(stream, directory, key, **kwargs):
        directory.mkdir()
        (directory / 'complete.part').write_bytes(b'partial')
        raise Interrupted('library')
    with monkeypatch.context() as patch:
        patch.setattr(library, 'seal_stream', partial)
        interrupted(world)


BOUNDARIES = [fail_inventory, fail_inventory_before_marker, fail_database, fail_before_copy_job,
              fail_during_copy, fail_after_copy, fail_publishing_catalog, fail_sealing_library]


@pytest.mark.parametrize('fail', BOUNDARIES, ids=lambda fail: fail.__name__)
def test_retry_finishes_the_same_capture_from_every_boundary(world, monkeypatch, fail):
    fail(world, monkeypatch)
    assert_no_recovery_point(world)
    identity = job_id(world)
    payload = world.capture(resume=True)
    assert payload['id'] == identity
    assert payload['media_files'] == 3
    assert verify_library(world.backup, world.key) == payload
    assert json.loads((world.backup / 'journal.json').read_text())['phase'] == 'complete'
    assert not (world.data / '.recovery-admission/maintenance.json').exists()
    # The verifier needs no source disk.
    world.media.rename(world.tmp / 'disconnected')
    assert verify_library(world.backup, world.key) == payload


@pytest.mark.parametrize('fail, preserved', [
    (fail_inventory, 'previous-inventory-*'), (fail_inventory_before_marker, 'previous-inventory-*'),
    (fail_database, 'previous-catalog-*'), (fail_before_copy_job, 'previous-media-copy-*'),
    (fail_sealing_library, 'previous-library-*')], ids=['inventory', 'empty-inventory', 'catalog', 'empty-copy', 'index'])
def test_unusable_work_is_kept_for_diagnosis(world, monkeypatch, fail, preserved):
    fail(world, monkeypatch)
    world.capture(resume=True)
    assert len(list(world.backup.glob(preserved))) == 1


def test_a_retry_copies_only_what_is_missing(world, monkeypatch):
    interrupted(world, progress=stop_at('copy', 2))
    copied = []
    real = media_tree.copy_media_file

    def spy(source, *args, **kwargs):
        copied.append(source.name)
        return real(source, *args, **kwargs)
    monkeypatch.setattr(media_tree, 'copy_media_file', spy)
    world.capture(resume=True)
    assert copied == ['three.jpg']


def test_a_retry_always_takes_a_new_catalog(world, monkeypatch):
    interrupted(world, progress=stop_at('copy', 1))
    first = (world.backup / 'catalog/database/complete.fernet').read_bytes()
    world.capture(resume=True)
    assert (world.backup / 'catalog/database/complete.fernet').read_bytes() != first
    assert len(list(world.backup.glob('previous-catalog-*'))) == 1


def test_a_retry_rereads_every_source_file(world):
    interrupted(world, progress=stop_at('copy', 1))
    seen = []
    world.capture(resume=True, progress=seen.append)
    assert sum(event['phase'] == 'inventory-verify' for event in seen) == 4
    assert not any(event['phase'] == 'inventory' for event in seen)


def test_a_one_pass_retry_hashes_no_source_file_twice_and_rechecks_what_it_copied(world, monkeypatch):
    """The first run hashed every file for the inventory. The retry trusts no copy it finds:
    it reads the two already copied, and copies the third, hashing it as it reads."""
    interrupted(world, progress=stop_at('copy', 2))
    hashed, rechecked = [], []
    real_hash, real_digest = media_inventory.hash_file, media_tree.file_digest
    monkeypatch.setattr(media_inventory, 'hash_file', lambda path, expected: hashed.append(path) or real_hash(path, expected))
    monkeypatch.setattr(media_tree, 'file_digest',
                        lambda path: (rechecked.append(path.name) if path.suffix == '.jpg' else None) or real_digest(path))
    payload = world.capture(resume=True, single_pass=True)
    assert hashed == [] and sorted(rechecked) == ['one.jpg', 'two.jpg']
    assert payload['media_read_back'] is False
    assert verify_library(world.backup, world.key) == payload
    timing = json.loads((world.backup / 'capture-timing.json').read_text())
    assert timing['resumed'] is True and 'recheck_copied' in timing['copy_seconds']


def test_a_retry_can_be_cancelled_and_retried(world):
    interrupted(world, progress=stop_at('copy', 1))
    with pytest.raises(KeyboardInterrupt):
        def cancel(event):
            if event.get('phase') == 'copy':
                raise KeyboardInterrupt
        world.capture(resume=True, progress=cancel)
    assert_no_recovery_point(world)
    with pytest.raises(MaintenanceBlocked, match='Previous maintenance'):
        world.capture(resume=True)
    acknowledge(world)
    payload = world.capture(resume=True)
    assert verify_library(world.backup, world.key) == payload


def test_retry_after_a_full_disk_counts_only_the_remaining_media(world, monkeypatch):
    size = 50 * 1024 ** 2
    (world.media / 'big.bin').write_bytes(b'x' * size)
    interrupted(world, progress=stop_at('copy', 3))
    assert (world.backup / 'media-copy/media/big.bin').exists()

    needed = []

    def measure(requirements):
        needed.extend(item.remaining_bytes for item in requirements if item.role == 'backup')
        return ample(requirements)
    payload = capture_library(object(), None, world.config, world.data, world.media, world.backup, world.key,
                              database_budget=1024, offline_confirmed=True, guard_factory=measure, resume=True)
    assert payload['media_files'] == 4
    assert max(needed) < size


def changed_size(world):
    (world.media / 'three.jpg').write_bytes(b'a longer replacement')


def changed_content_only(world):
    path = world.media / 'three.jpg'
    before = path.stat()
    path.write_bytes(b'X' * before.st_size)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert path.stat().st_mtime_ns == before.st_mtime_ns


def added_file(world):
    (world.media / 'new.jpg').write_bytes(b'new')


def removed_file(world):
    (world.media / 'three.jpg').unlink()


@pytest.mark.parametrize('change', [changed_size, changed_content_only, added_file, removed_file],
                         ids=lambda change: change.__name__)
def test_a_changed_source_is_refused_and_keeps_the_gate(world, monkeypatch, change):
    interrupted(world, progress=stop_at('copy', 1))
    change(world)
    monkeypatch.setattr(media_tree, 'copy_media_file', lambda *a, **k: pytest.fail('Must not copy after a change'))
    with pytest.raises(InvalidMediaInventory, match='Capture into a new directory'):
        world.capture(resume=True)
    assert_no_recovery_point(world)
    assert (world.data / '.recovery-admission/maintenance.json').exists()


def gate_untouched(world):
    assert not (world.data / '.recovery-admission/maintenance.json').exists()


def test_a_directory_without_a_retry_record_is_refused(world):
    world.backup.mkdir()
    (world.backup / 'journal.json').write_text('{}')
    with pytest.raises(InvalidArtifact, match='no retry record'):
        world.capture(resume=True)
    gate_untouched(world)


def test_a_missing_directory_is_refused(world):
    with pytest.raises(FileNotFoundError):
        world.capture(resume=True)
    gate_untouched(world)


def test_another_key_is_refused(world):
    interrupted(world, progress=stop_at('copy', 1))
    from app.backup.passwords import WrongPassword
    with pytest.raises(WrongPassword, match='belongs to another backup'):
        world.capture(resume=True, use_key=backup_key())
    gate_untouched(world)


@pytest.mark.parametrize('content', [b'not json', b'[]', b'{"sealed": 1}', b'{"sealed": "forged"}'],
                         ids=['text', 'list', 'number', 'forged'])
def test_a_forged_retry_record_is_refused(world, content):
    interrupted(world, progress=stop_at('copy', 1))
    (world.backup / 'capture-job.json').write_bytes(content)
    with pytest.raises(InvalidArtifact, match='another key'):
        world.capture(resume=True)
    gate_untouched(world)


@pytest.mark.parametrize('job', [[], {'no': 'id'}, {'id': 'not-an-id'}], ids=['list', 'missing-id', 'bad-id'])
def test_a_sealed_record_with_a_bad_shape_is_refused(world, job):
    interrupted(world, progress=stop_at('copy', 1))
    token = Fernet(world.key).encrypt(json.dumps(job).encode()).decode()
    (world.backup / 'capture-job.json').write_text(json.dumps({'sealed': token}))
    with pytest.raises(InvalidArtifact, match='another key'):
        world.capture(resume=True)
    gate_untouched(world)


def test_another_media_root_is_refused(world):
    interrupted(world, progress=stop_at('copy', 1))
    other = world.tmp / 'other-media'
    shutil.copytree(world.media, other)
    with pytest.raises(InvalidArtifact, match='changed'):
        world.capture(resume=True, root=other)
    gate_untouched(world)


def test_a_replaced_source_at_the_same_path_is_refused(world):
    interrupted(world, progress=stop_at('copy', 1))
    world.media.rename(world.tmp / 'original-media')
    shutil.copytree(world.tmp / 'original-media', world.media)
    with pytest.raises(InvalidArtifact, match='changed'):
        world.capture(resume=True)
    gate_untouched(world)


def test_a_replacement_backup_disk_at_the_same_path_is_refused(world):
    interrupted(world, progress=stop_at('copy', 1))
    world.backup.rename(world.tmp / 'original-backup')
    shutil.copytree(world.tmp / 'original-backup', world.backup)
    with pytest.raises(InvalidArtifact, match='changed'):
        world.capture(resume=True)
    gate_untouched(world)


def test_another_volume_is_refused(world, monkeypatch):
    interrupted(world, progress=stop_at('copy', 1))
    real = library.volume_identity
    monkeypatch.setattr(library, 'volume_identity',
                        lambda path: 'other-disk' if path == world.backup else real(path))
    with pytest.raises(InvalidArtifact, match='changed'):
        world.capture(resume=True)
    gate_untouched(world)


def test_another_retirement_target_is_refused(world):
    interrupted(world, progress=stop_at('copy', 1), retire_to='a' * 32)
    with pytest.raises(InvalidArtifact, match='changed'):
        world.capture(resume=True, retire_to='b' * 32)
    gate_untouched(world)
    with pytest.raises(InvalidArtifact, match='changed'):
        world.capture(resume=True)


def test_a_retried_migration_capture_retires_only_after_verification(world):
    interrupted(world, progress=stop_at('copy', 1), retire_to='a' * 32)
    assert not (world.data / '.recovery-admission/retired.json').exists()
    payload = world.capture(resume=True, retire_to='a' * 32)
    assert payload['migration_target'] == 'a' * 32
    assert (world.data / '.recovery-admission/retired.json').is_file()


def test_another_library_identity_is_refused(world):
    interrupted(world, progress=stop_at('copy', 1))
    (world.data / '.recovery-admission/library.json').write_text(
        json.dumps({'format': 'familyfoto-library-identity-v1', 'library_id': 'c' * 32}))
    with pytest.raises(InvalidArtifact, match='another library'):
        world.capture(resume=True)
    gate_untouched(world)


def test_an_unowned_entry_is_refused(world):
    interrupted(world, progress=stop_at('copy', 1))
    (world.backup / 'something-else').write_bytes(b'x')
    with pytest.raises(InvalidMediaInventory, match='Unowned'):
        world.capture(resume=True)
    gate_untouched(world)


def test_a_journal_from_another_capture_is_refused(world):
    interrupted(world, progress=stop_at('copy', 1))
    (world.backup / 'journal.json').write_text(json.dumps({'id': 'd' * 32, 'phase': 'media-copy'}))
    with pytest.raises(InvalidArtifact, match='another capture'):
        world.capture(resume=True)
    gate_untouched(world)


def test_a_retired_source_points_to_receipt_recovery(world):
    interrupted(world, progress=stop_at('copy', 1))
    (world.data / '.recovery-admission/retired.json').write_text('{}')
    with pytest.raises(MaintenanceBlocked, match='recover-retirement'):
        world.capture(resume=True)
    gate_untouched(world)


def test_an_unacknowledged_failure_stays_gated(world):
    with pytest.raises(Interrupted):
        world.capture(progress=stop_at('copy', 1))
    with pytest.raises(MaintenanceBlocked, match='Previous maintenance'):
        world.capture(resume=True)


def test_a_finished_capture_is_never_rebuilt(world):
    payload = world.capture()
    before = (world.backup / 'catalog/catalog/complete.fernet').read_bytes()
    with pytest.raises(FileExistsError, match='already finished'):
        world.capture(resume=True)
    gate_untouched(world)
    assert (world.backup / 'catalog/catalog/complete.fernet').read_bytes() == before
    assert verify_library(world.backup, world.key) == payload


@pytest.mark.parametrize('retire_to, phase', [(None, 'media-copy'), ('a' * 32, 'retiring-source')],
                         ids=['plain', 'migration'])
def test_a_valid_index_refuses_even_if_the_journal_never_finished(world, monkeypatch, retire_to, phase):
    """A crash right after sealing the index leaves a real recovery point: never rebuild it."""
    def crash(*args, **kwargs):
        raise Interrupted('after sealing')
    if retire_to:
        monkeypatch.setattr(library, 'mark_source_retired', crash)
    else:
        real = library.journal

        def crash_on_complete(directory, identity, value):
            if value == 'complete' and directory == world.backup:
                raise Interrupted('after sealing')
            return real(directory, identity, value)
        monkeypatch.setattr(library, 'journal', crash_on_complete)
    interrupted(world, retire_to=retire_to)
    assert json.loads((world.backup / 'journal.json').read_text())['phase'] == phase
    payload = verify_library(world.backup, world.key)
    with pytest.raises(FileExistsError, match='already finished'):
        world.capture(resume=True, retire_to=retire_to)
    gate_untouched(world)
    assert not list(world.backup.glob('previous-*'))
    assert verify_library(world.backup, world.key) == payload
    assert not (world.data / '.recovery-admission/retired.json').exists()


def test_a_finished_journal_alone_also_refuses(world):
    interrupted(world, progress=stop_at('copy', 1))
    journal = json.loads((world.backup / 'journal.json').read_text())
    (world.backup / 'journal.json').write_text(json.dumps({**journal, 'phase': 'complete'}))
    with pytest.raises(FileExistsError, match='already finished'):
        world.capture(resume=True)


def test_a_damaged_finished_capture_is_left_for_review(world):
    world.capture()
    (world.backup / 'media-copy/media/three.jpg').write_bytes(b'damaged')
    with pytest.raises(ValueError, match='verification'):
        world.capture(resume=True)
    gate_untouched(world)
    assert not list(world.backup.glob('previous-*'))


def test_damaged_copied_media_is_refused_not_overwritten(world):
    interrupted(world, progress=stop_at('copy', 2))
    copied = world.backup / 'media-copy/media/album/one.jpg'
    copied.write_bytes(b'damaged!!!!!!')
    with pytest.raises(InvalidMediaInventory, match='damaged'):
        world.capture(resume=True)
    assert copied.read_bytes() == b'damaged!!!!!!'
    assert_no_recovery_point(world)


def test_a_new_capture_still_refuses_an_existing_directory(world):
    interrupted(world, progress=stop_at('copy', 1))
    with pytest.raises(FileExistsError, match='new directory'):
        world.capture()


def test_the_retry_record_keeps_paths_out_of_plain_text(world):
    world.capture()
    assert str(world.media).encode() not in (world.backup / 'capture-job.json').read_bytes()


def shift_devices(directory, *, step=7, entries='1=1'):
    with closing(sqlite3.connect(directory / 'inventory.sqlite3')) as db:
        # Windows device numbers can exceed what SQLite integers hold.
        db.create_function('shifted', 1, lambda value: str(int(value) + step))
        db.execute("UPDATE metadata SET value=shifted(value) WHERE key='device'")
        db.execute(f'UPDATE entries SET device=shifted(device) WHERE {entries}')
        db.commit()


def remount(world, backup=None, step=7):
    """Rewrite every recorded device number, which is what a remount looks like to a retry."""
    backup = backup or world.backup
    token = json.loads((backup / 'capture-job.json').read_text())['sealed']
    job = json.loads(Fernet(world.key).decrypt(token.encode()))
    for root in job['roots'].values():
        root['identity'][0] += step
    save_private(backup / 'capture-job.json', {'sealed': Fernet(world.key).encrypt(json.dumps(job).encode()).decode()})
    database = backup / 'inventory/inventory.sqlite3'
    shift_devices(database.parent, step=step)
    copy = backup / 'media-copy'
    record = json.loads((copy / 'copy-job.json').read_text())
    record['source_identity'][0] += step
    record['target_identity'][0] += step
    record['inventory_sha256'] = hashlib.sha256(database.read_bytes()).hexdigest()
    (copy / 'copy-job.json').write_text(json.dumps(record))
    (copy / 'inventory/inventory.sqlite3').write_bytes(database.read_bytes())
    for checkpoint in (copy / 'file-jobs').glob('*/copy.json'):
        value = json.loads(checkpoint.read_text())
        value['source'][0] += step
        value['directory'][0] += step
        checkpoint.write_text(json.dumps(value))


def test_retry_finishes_after_a_remount_and_copies_only_what_is_missing(world, monkeypatch):
    interrupted(world, progress=stop_at('copy', 2))
    remount(world)
    copied = []
    real = media_tree.copy_media_file

    def spy(source, *args, **kwargs):
        copied.append(source.name)
        return real(source, *args, **kwargs)
    monkeypatch.setattr(media_tree, 'copy_media_file', spy)
    payload = world.capture(resume=True)
    assert copied == ['three.jpg']
    assert verify_library(world.backup, world.key) == payload


def test_a_remount_does_not_skip_the_reread(world):
    interrupted(world, progress=stop_at('copy', 1))
    remount(world)
    changed_content_only(world)
    with pytest.raises(InvalidMediaInventory, match='Capture into a new directory'):
        world.capture(resume=True)
    assert_no_recovery_point(world)


def test_a_remount_does_not_excuse_damaged_copies(world):
    interrupted(world, progress=stop_at('copy', 2))
    remount(world)
    (world.backup / 'media-copy/media/album/one.jpg').write_bytes(b'damaged!!!!!!')
    with pytest.raises(InvalidMediaInventory, match='damaged'):
        world.capture(resume=True)
    assert_no_recovery_point(world)


@pytest.mark.parametrize('module', ['library', 'media_inventory', 'media_tree'])
def test_a_different_disk_is_refused_after_a_remount(world, monkeypatch, module):
    """Same paths and same inodes, with another volume ID, is a replacement disk."""
    interrupted(world, progress=stop_at('copy', 1))
    remount(world)
    target = {'library': library, 'media_inventory': media_inventory, 'media_tree': media_tree}[module]
    real = target.volume_identity

    def other(path):
        return {'kind': 'test', 'id': 'other-disk'} if path == world.media else real(path)
    monkeypatch.setattr(target, 'volume_identity', other)
    with pytest.raises((InvalidArtifact, InvalidMediaInventory), match='changed|another source|Copy source'):
        world.capture(resume=True)
    assert_no_recovery_point(world)


def test_a_replaced_backup_folder_is_refused_after_a_remount(world):
    interrupted(world, progress=stop_at('copy', 1))
    remount(world)
    world.backup.rename(world.tmp / 'original-backup')
    shutil.copytree(world.tmp / 'original-backup', world.backup)
    with pytest.raises(InvalidArtifact, match='changed'):
        world.capture(resume=True)


def test_a_replaced_media_folder_is_refused_after_a_remount(world):
    interrupted(world, progress=stop_at('copy', 1))
    remount(world)
    world.media.rename(world.tmp / 'original-media')
    shutil.copytree(world.tmp / 'original-media', world.media)
    with pytest.raises(InvalidArtifact, match='changed'):
        world.capture(resume=True)


def recorded_inventory(world):
    (world.tmp / 'held').mkdir()
    directory = world.tmp / 'held/inventory'
    capture_media_inventory(world.media, directory, offline_confirmed=True, guard_factory=ample)
    return directory


def test_inventory_accepts_a_remount_of_the_same_volume(world):
    directory = recorded_inventory(world)
    shift_devices(directory)
    assert verify_media_inventory(directory, world.media)['files'] == 3


def test_inventory_records_its_volume(world):
    directory = recorded_inventory(world)
    with closing(sqlite3.connect(directory / 'inventory.sqlite3')) as db:
        recorded = db.execute("SELECT value FROM metadata WHERE key='volume'").fetchone()[0]
    assert json.loads(recorded) == volume_identity(world.media)


def test_an_inventory_without_a_volume_stays_strict(world):
    directory = recorded_inventory(world)
    with closing(sqlite3.connect(directory / 'inventory.sqlite3')) as db:
        db.execute("DELETE FROM metadata WHERE key='volume'")
        db.commit()
    assert verify_media_inventory(directory, world.media)['files'] == 3
    shift_devices(directory)
    with pytest.raises(InvalidMediaInventory, match='another source root'):
        verify_media_inventory(directory, world.media)


def test_inventory_refuses_another_volume(world, monkeypatch):
    directory = recorded_inventory(world)
    shift_devices(directory)
    monkeypatch.setattr(media_inventory, 'volume_identity', lambda path: {'kind': 'test', 'id': 'other-disk'})
    with pytest.raises(InvalidMediaInventory, match='another source root'):
        verify_media_inventory(directory, world.media)


def test_inventory_refuses_a_nested_mount_on_another_device_after_a_remount(world):
    directory = recorded_inventory(world)
    # Only the root and most entries moved. This one was recorded on some other device.
    shift_devices(directory, entries="path<>'three.jpg'")
    with closing(sqlite3.connect(directory / 'inventory.sqlite3')) as db:
        db.execute("UPDATE entries SET device='999999' WHERE path='three.jpg'")
        db.commit()
    with pytest.raises(InvalidMediaInventory, match='changed'):
        verify_media_inventory(directory, world.media)


def test_inventory_refuses_a_replaced_file_after_a_remount(world):
    directory = recorded_inventory(world)
    shift_devices(directory)
    path = world.media / 'three.jpg'
    before = path.stat()
    raw = path.read_bytes()
    path.unlink()
    (world.media / 'filler.jpg').write_bytes(b'takes the old inode number if it can')
    path.write_bytes(raw)
    (world.media / 'filler.jpg').unlink()
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    if path.stat().st_ino == before.st_ino:
        pytest.skip('This filesystem reused the inode number')
    with pytest.raises(InvalidMediaInventory, match='changed'):
        verify_media_inventory(directory, world.media, rehash=False)


def test_a_root_without_a_durable_volume_can_still_be_captured(world, monkeypatch):
    def unavailable(path):
        raise StorageUnavailable('No durable identity')
    monkeypatch.setattr(media_inventory, 'volume_identity', unavailable)
    directory = recorded_inventory(world)
    assert verify_media_inventory(directory, world.media)['files'] == 3
    shift_devices(directory)
    with pytest.raises(InvalidMediaInventory, match='another source root'):
        verify_media_inventory(directory, world.media)


def test_a_partly_copied_file_resumes_after_a_remount(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    raw = bytes(range(64))
    (source / 'photo.jpg').write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    job = tmp_path / 'job'
    job.mkdir()

    def stop(offset, size):
        if offset == 16:
            raise Interrupted('copy')
    with pytest.raises(Interrupted):
        copy_media_file(source / 'photo.jpg', job, digest, chunk_size=8, guard_factory=ample, progress=stop)
    checkpoint = json.loads((job / 'copy.json').read_text())
    checkpoint['source'][0] += 7
    checkpoint['directory'][0] += 7
    (job / 'copy.json').write_text(json.dumps(checkpoint))
    written = []
    final = copy_media_file(source / 'photo.jpg', job, digest, chunk_size=8, guard_factory=ample,
                            progress=lambda offset, size: written.append(offset))
    assert final.read_bytes() == raw
    assert written[0] == 24


@pytest.mark.parametrize('field, position', [('source', 1), ('source', 2), ('source', 3), ('directory', 1)],
                         ids=['source-inode', 'source-size', 'source-time', 'job-inode'])
def test_a_checkpoint_only_forgives_the_device(tmp_path, field, position):
    source = tmp_path / 'source'
    source.mkdir()
    raw = bytes(range(64))
    (source / 'photo.jpg').write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    job = tmp_path / 'job'
    job.mkdir()

    def stop(offset, size):
        raise Interrupted('copy')
    with pytest.raises(Interrupted):
        copy_media_file(source / 'photo.jpg', job, digest, chunk_size=8, guard_factory=ample, progress=stop)
    checkpoint = json.loads((job / 'copy.json').read_text())
    checkpoint[field][position] += 1
    (job / 'copy.json').write_text(json.dumps(checkpoint))
    with pytest.raises(InvalidCopy, match='changed'):
        copy_media_file(source / 'photo.jpg', job, digest, chunk_size=8, guard_factory=ample)


@pytest.mark.parametrize('moved', ['source', 'job'])
def test_a_checkpoint_refuses_another_volume_on_either_side(tmp_path, moved):
    source = tmp_path / 'source'
    source.mkdir()
    raw = bytes(range(64))
    (source / 'photo.jpg').write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    job = tmp_path / 'job'
    job.mkdir()

    def stop(offset, size):
        raise Interrupted('copy')
    with pytest.raises(Interrupted):
        copy_media_file(source / 'photo.jpg', job, digest, chunk_size=8, guard_factory=ample, progress=stop)
    checkpoint = json.loads((job / 'copy.json').read_text())
    checkpoint['source'][0] += 7
    checkpoint['directory'][0] += 7
    (job / 'copy.json').write_text(json.dumps(checkpoint))
    changed = source if moved == 'source' else job

    def probe(path):
        return {'kind': 'test', 'id': 'other-disk'} if path == changed else volume_identity(path)
    with pytest.raises(InvalidCopy, match='changed'):
        copy_media_file(source / 'photo.jpg', job, digest, chunk_size=8, guard_factory=ample, volume_probe=probe)


@pytest.mark.parametrize('key, value', [('inode', '1'), ('source_root', 'somewhere-else')])
def test_inventory_root_must_still_match_after_a_remount(world, key, value):
    directory = recorded_inventory(world)
    shift_devices(directory)
    with closing(sqlite3.connect(directory / 'inventory.sqlite3')) as db:
        db.execute('UPDATE metadata SET value=? WHERE key=?', (value, key))
        db.commit()
    with pytest.raises(InvalidMediaInventory, match='another source root'):
        verify_media_inventory(directory, world.media)


def test_a_recorded_path_must_still_match_after_a_remount(world):
    """A disk that comes back at another path is refused, even with the same identity."""
    interrupted(world, progress=stop_at('copy', 1))
    remount(world)
    token = json.loads((world.backup / 'capture-job.json').read_text())['sealed']
    job = json.loads(Fernet(world.key).decrypt(token.encode()))
    job['roots']['media']['path'] += '-elsewhere'
    save_private(world.backup / 'capture-job.json',
                 {'sealed': Fernet(world.key).encrypt(json.dumps(job).encode()).decode()})
    with pytest.raises(InvalidArtifact, match='changed'):
        world.capture(resume=True)


@pytest.mark.parametrize('field', ['source_identity', 'target_identity'])
def test_a_copy_job_only_forgives_the_device(world, field):
    interrupted(world, progress=stop_at('copy', 1))
    path = world.backup / 'media-copy/copy-job.json'
    record = json.loads(path.read_text())
    record[field][1] += 1
    path.write_text(json.dumps(record))
    with pytest.raises(InvalidMediaInventory, match='changed'):
        world.capture(resume=True)
    assert_no_recovery_point(world)


def attach(image, mount):
    mount.mkdir(exist_ok=True)
    subprocess.run(['hdiutil', 'attach', str(image), '-mountpoint', str(mount), '-nobrowse'],
                   check=True, capture_output=True)


def detach(mount):
    # Disk Arbitration can briefly retain the test image after filesystem I/O.
    # Retry only EBUSY, with a bounded retry count; never ignore failed cleanup or identity checks.
    for attempt in range(5):
        try:
            subprocess.run(['hdiutil', 'detach', str(mount), '-force'], check=True, capture_output=True)
            return
        except subprocess.CalledProcessError as error:
            if error.returncode != errno.EBUSY or attempt == 4:
                raise
            time.sleep(0.25 * (attempt + 1))


@pytest.mark.parametrize('codes,raises', [([16, 0], False), ([16] * 5, True), ([1], True)])
def test_disk_image_cleanup_retries_only_transient_busy(monkeypatch, codes, raises):
    remaining = iter(codes)
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        code = next(remaining)
        if code:
            raise subprocess.CalledProcessError(code, command)
    monkeypatch.setattr(subprocess, 'run', run)
    monkeypatch.setattr(time, 'sleep', lambda delay: None)
    if raises:
        with pytest.raises(subprocess.CalledProcessError):
            detach('fixture-mount')
    else:
        detach('fixture-mount')
    assert len(calls) == len(codes)


def new_image(path, name):
    subprocess.run(['hdiutil', 'create', '-size', '32m', '-fs', 'APFS', '-volname', name, str(path)],
                   check=True, capture_output=True)


@pytest.mark.skipif(sys.platform != 'darwin', reason='Detaches and reattaches real APFS disk images')
@pytest.mark.ci_apfs
def test_real_apfs_remount_resumes_and_a_replacement_disk_is_refused(world):
    media_image, backup_image, filler_image, other_image = (
        world.tmp / f'{name}.dmg' for name in ('media', 'backup', 'filler', 'other'))
    for image in (media_image, backup_image, filler_image, other_image):
        new_image(image, image.stem)
    media_mount, backup_mount, filler_mount = (world.tmp / f'mnt-{name}' for name in ('media', 'backup', 'filler'))
    attached = []

    def mount(image, where):
        attach(image, where)
        attached.append(where)

    def unmount(where):
        detach(where)
        attached.remove(where)
    try:
        mount(media_image, media_mount)
        mount(backup_image, backup_mount)
        media, backup = media_mount / 'media', backup_mount / 'first'
        shutil.copytree(world.media, media)
        before = directory_identity(media)
        interrupted(world, root=media, backup=backup, progress=stop_at('copy', 2))
        assert_no_recovery_point(SimpleNamespace(backup=backup, key=world.key))

        unmount(media_mount)
        unmount(backup_mount)
        # Another disk takes the old device numbers, as it would after a real reconnection.
        mount(filler_image, filler_mount)
        mount(media_image, media_mount)
        mount(backup_image, backup_mount)
        assert directory_identity(media)[0] != before[0]
        assert directory_identity(media)[1] == before[1]

        payload = world.capture(resume=True, root=media, backup=backup)
        assert verify_library(backup, world.key) == payload

        # The same folder, copied to a different disk at the same path, is a replacement.
        second = backup_mount / 'second'
        interrupted(world, root=media, backup=second, progress=stop_at('copy', 2))
        held = world.tmp / 'held-backup'
        shutil.copytree(second, held)
        unmount(backup_mount)
        mount(other_image, backup_mount)
        shutil.copytree(held, second)
        with pytest.raises(InvalidArtifact, match='changed'):
            world.capture(resume=True, root=media, backup=second)
    finally:
        for where in reversed(attached):
            detach(where)


def test_cli_passes_resume_only_when_asked(world, monkeypatch, tmp_path):
    seen = []

    def fake(*args, **kwargs):
        seen.append(kwargs['resume'])
        return {'id': 'e' * 32}
    monkeypatch.setattr(manage, 'capture_library', fake)
    monkeypatch.setattr(manage.passwords, 'from_arguments', lambda args: 'the password')
    monkeypatch.setattr(manage, 'key_for', lambda args, text: world.key)
    arguments = ['capture-library', '--directory', str(world.backup), '--backup-password-stdin', '--port', '55490',
                 '--username', 'backup_test', '--dbname', 'fixture', '--pg-dump', 'pg_dump', '--config-root', str(world.config),
                 '--data-root', str(world.data), '--media-root', str(world.media), '--database-budget-bytes', '1024', '--offline']
    assert manage.main(arguments) == 0
    assert manage.main(arguments + ['--resume']) == 0
    assert seen == [False, True]


def test_capture_packs_mac_handover_and_resumes_after_packaging_interruption(world, monkeypatch):
    from app.backup.handover import FILES, FOLDER, SOURCE
    write = library.write_handover

    def interrupted(directory):
        write(directory)
        raise Interrupted('handover publication')

    monkeypatch.setattr(library, 'write_handover', interrupted)
    with pytest.raises(Interrupted):
        world.capture()
    assert not (world.backup / 'library/complete.fernet').exists()
    for name in FILES:
        assert (world.backup / FOLDER / name).read_bytes() == (SOURCE / name).read_bytes()
    acknowledge(world)
    monkeypatch.setattr(library, 'write_handover', write)
    world.capture(resume=True)
    assert verify_library(world.backup, world.key)['media_files'] == 3
    for name in FILES:
        assert (world.backup / FOLDER / name).read_bytes() == (SOURCE / name).read_bytes()


@pytest.mark.parametrize('name', ['README.txt', 'restore-familyfoto-mac.command'])
def test_handover_retry_refuses_linked_launcher(world, name):
    from app.backup.handover import FILES, FOLDER, RETIRED, write_handover
    assert name in FILES + RETIRED
    world.backup.mkdir()
    folder = world.backup / FOLDER
    folder.mkdir()
    original = world.tmp / 'keep.txt'
    original.write_text('keep this file')
    os.link(original, folder / name)
    with pytest.raises(ValueError, match='Linked'):
        write_handover(world.backup)
    assert original.read_text() == 'keep this file'
