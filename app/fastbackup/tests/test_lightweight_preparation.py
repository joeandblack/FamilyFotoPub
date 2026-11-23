import sqlite3

import pytest

from app.backup.media_inventory import portable_path
from app.fastbackup import full_copy
from app.fastbackup.tests.test_full_copy import copy, fixture


def edit_inventory(paths, statement, values=()):
    with sqlite3.connect(paths[1] / 'inventory.sqlite3') as db:
        db.execute(statement, values)


def test_preparation_does_not_walk_source_or_prepare_nested_folders(tmp_path, monkeypatch):
    paths = fixture(tmp_path)
    from app.backup import media_inventory
    phases = []

    def recheck(*args, **kwargs):
        pytest.fail('Experimental copy must not recheck the source inventory')

    monkeypatch.setattr(media_inventory, 'verify_media_inventory', recheck)

    def progress(event):
        phases.append(event['phase'])
        if event['phase'] == 'media-preparation' and event['state'] == 'completed':
            assert list((paths[2] / 'media').iterdir()) == []

    result = copy(paths, progress=progress)
    assert result['files'] == 3 and result['content_verified'] is True
    assert (paths[2] / 'media/empty-dir').is_dir()


def test_changed_source_is_not_checked_in_preparation_but_cannot_be_published(tmp_path):
    paths = fixture(tmp_path)
    (paths[0] / 'photo.jpg').write_bytes(b'wrong!')
    phases = []
    with pytest.raises(ValueError, match='verification'):
        copy(paths, progress=lambda event: phases.append(event['phase']))
    assert 'media-transfer' in phases and 'media-verification' in phases
    assert not (paths[2] / 'TREE-COMPLETE.json').exists()


def test_new_source_entry_outside_inventory_is_not_rescanned_under_immutable_policy(tmp_path):
    paths = fixture(tmp_path)
    (paths[0] / 'new-photo').write_bytes(b'new')
    result = copy(paths)
    assert result['files'] == 3 and result['content_verified']
    assert not (paths[2] / 'media/new-photo').exists()


@pytest.mark.parametrize('key,value', [('state', 'incomplete'), ('format', 'unknown'),
                                      ('source_root', 'different'), ('inode', '-1'), ('device', '-1')])
def test_root_inventory_binding_is_still_required(tmp_path, key, value):
    paths = fixture(tmp_path)
    edit_inventory(paths, 'UPDATE metadata SET value=? WHERE key=?', (value, key))
    if key == 'device':
        edit_inventory(paths, "DELETE FROM metadata WHERE key='volume'")
    with pytest.raises(ValueError):
        copy(paths)
    assert not paths[2].exists()


def test_incomplete_inventory_marker_is_rejected_before_copy(tmp_path):
    paths = fixture(tmp_path)
    (paths[1] / 'INVENTORY-INCOMPLETE').touch()
    with pytest.raises(ValueError, match='incomplete'):
        copy(paths)
    assert not paths[2].exists()


@pytest.mark.parametrize('kind', ['file', 'directory'])
def test_unsafe_inventory_paths_never_escape_target(tmp_path, kind):
    paths = fixture(tmp_path)
    original = 'photo.jpg' if kind == 'file' else 'empty-dir'
    edit_inventory(paths, 'UPDATE entries SET path=?,portable=? WHERE path=?', ('../../escape', '../../escape', original))
    with pytest.raises(ValueError, match='Unsafe'):
        copy(paths)
    assert not (tmp_path / 'escape').exists()
    assert not (paths[2] / 'TREE-COMPLETE.json').exists()


def test_missing_inventory_parent_never_creates_an_unlisted_directory(tmp_path):
    paths = fixture(tmp_path)
    edit_inventory(paths, "DELETE FROM entries WHERE path='nested'")
    with pytest.raises(ValueError, match='parent directory'):
        copy(paths)
    assert not (paths[2] / 'media/nested').exists()


def test_top_level_overlap_still_fails_before_writes(tmp_path):
    source, inventory, _ = fixture(tmp_path)
    with pytest.raises(ValueError, match='separate'):
        copy((source, inventory, source / 'backup'))
    assert not (source / 'backup').exists()


def test_path_failure_is_reported_during_copy_and_attempt_stays_incomplete(tmp_path, monkeypatch):
    paths = fixture(tmp_path)
    phases = []

    def too_long(*args, **kwargs):
        raise OSError(36, 'destination path too long')

    monkeypatch.setattr(full_copy, 'copy_file', too_long)
    with pytest.raises(OSError, match='too long'):
        copy(paths, progress=lambda event: phases.append(event['phase']))
    assert phases[-1] == 'media-transfer'
    assert (paths[2] / 'TREE-INCOMPLETE').exists()
    assert not (paths[2] / 'TREE-COMPLETE.json').exists()


def test_nested_empty_folders_preserved_without_upfront_directory_pass(tmp_path):
    from app.backup.media_inventory import capture_media_inventory
    from app.fastbackup.tests.test_full_copy import Guard
    source = tmp_path / 'source'
    (source / 'a/b/c').mkdir(parents=True)
    inventory, target = tmp_path / 'inventory', tmp_path / 'copy'
    capture_media_inventory(source, inventory, offline_confirmed=True, guard_factory=Guard)
    result = copy((source, inventory, target))
    assert result['files'] == 0
    assert (target / 'media/a/b/c').is_dir()


def test_portable_name_mismatch_is_rejected_when_file_is_reached(tmp_path):
    paths = fixture(tmp_path)
    edit_inventory(paths, "UPDATE entries SET portable=? WHERE path='photo.jpg'", (portable_path('different'),))
    with pytest.raises(ValueError, match='inventory path'):
        copy(paths)
    assert not (paths[2] / 'media/photo.jpg').exists()


def test_capacity_checks_upfront_then_by_bytes_or_time():
    from types import SimpleNamespace
    clock, checks, written = [0.0], [], []
    guard = SimpleNamespace(check=lambda: checks.append(clock[0]), written=lambda *args: written.append(args))
    capacity = full_copy._CopyCapacity(guard, clock=lambda: clock[0])
    capacity.check()
    assert checks == [0]
    for _ in range(1000):
        capacity.check()
        capacity.written(1)
    assert checks == [0] and len(written) == 1000
    capacity.written(full_copy.SPACE_CHECK_BYTES - 1000)
    capacity.check()
    assert checks == [0, 0]
    clock[0] = full_copy.SPACE_CHECK_SECONDS
    capacity.check()
    assert checks == [0, 0, full_copy.SPACE_CHECK_SECONDS]
    capacity.check()
    assert len(checks) == 3


def test_upfront_insufficient_space_copies_nothing(tmp_path):
    paths = fixture(tmp_path)
    requirements = []

    class FullDisk:
        def __init__(self, requested):
            requirements.extend(requested)

        def check(self):
            raise OSError(28, 'not enough space upfront')

    with pytest.raises(OSError, match='upfront'):
        full_copy.copy_media_tree(*paths, offline_confirmed=True, guard_factory=FullDisk)
    assert requirements[0].remaining_bytes > 50006
    assert not paths[2].exists()


def test_periodic_capacity_failure_is_not_suppressed():
    from types import SimpleNamespace
    checks = []

    def check():
        checks.append(True)
        if len(checks) > 1:
            raise OSError(28, 'another program consumed space')

    capacity = full_copy._CopyCapacity(SimpleNamespace(check=check, written=lambda *args: None), clock=lambda: 0)
    capacity.check()
    capacity.written(full_copy.SPACE_CHECK_BYTES)
    with pytest.raises(OSError, match='consumed space'):
        capacity.check()


@pytest.mark.parametrize('error_number', [28, 122])
def test_write_space_error_never_publishes(tmp_path, monkeypatch, error_number):
    paths = fixture(tmp_path)

    def fail(source, target, **kwargs):
        target.write_bytes(b'partial')
        raise OSError(error_number, 'space or quota exhausted during write')

    monkeypatch.setattr(full_copy, 'copy_file', fail)
    with pytest.raises(OSError, match='exhausted'):
        copy(paths)
    assert (paths[2] / 'TREE-INCOMPLETE').exists()
    assert not (paths[2] / 'TREE-COMPLETE.json').exists()


def test_short_copy_rejected_by_verification_not_per_file_size_stat(tmp_path, monkeypatch):
    paths = fixture(tmp_path)
    phases = []

    def truncate(source, target, **kwargs):
        target.write_bytes(b'')

    monkeypatch.setattr(full_copy, 'copy_file', truncate)
    with pytest.raises(ValueError):
        copy(paths, progress=lambda event: phases.append((event['phase'], event['state'])))
    assert ('media-transfer', 'completed') in phases
    assert phases[-1] == ('media-verification', 'failed')
    assert not (paths[2] / 'TREE-COMPLETE.json').exists()
