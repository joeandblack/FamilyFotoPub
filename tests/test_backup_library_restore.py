import sqlite3

import pytest
from tests.backup_keys import backup_key

KEY = backup_key()

from app.backup import media_inventory
from app.backup.library_restore import authenticated_digests, equivalent_inventories, restore_library
from app.backup.encrypted import InvalidArtifact
from app.backup.media_copy import InvalidCopy
from app.backup.media_inventory import capture_media_inventory
from app.backup.media_tree import copy_media_tree
from app.backup.postgres import Endpoint


def inventory(path, rows):
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE entries(path text, kind text, size integer, sha256 text)')
        db.executemany('INSERT INTO entries VALUES (?,?,?,?)', rows)
    return path


def test_recovery_compares_content_not_source_directory_size(tmp_path):
    left = inventory(tmp_path / 'left', [('empty', 'directory', 12, None), ('a', 'file', 4, 'digest')])
    right = inventory(tmp_path / 'right', [('empty', 'directory', 4096, None), ('a', 'file', 4, 'digest')])
    equivalent_inventories(left, right)
    with sqlite3.connect(right) as db:
        db.execute("UPDATE entries SET sha256='different' WHERE path='a'")
    with pytest.raises(InvalidArtifact, match='differs'):
        equivalent_inventories(left, right)


def test_recovery_rejects_missing_or_extra_files(tmp_path):
    left = inventory(tmp_path / 'left', [('a', 'file', 4, 'digest')])
    right = inventory(tmp_path / 'right', [])
    for expected, observed in ((left, right), (right, left)):
        with pytest.raises(InvalidArtifact, match='differs'):
            equivalent_inventories(expected, observed)


class Guard:
    def __init__(self, requirements):
        pass

    def check(self, **kwargs):
        pass

    def written(self, *args):
        pass


def sealed_inventory_of(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'a.jpg').write_bytes(b'photo')
    capture_media_inventory(source, tmp_path / 'sealed', offline_confirmed=True, guard_factory=Guard)
    return source, tmp_path / 'sealed/inventory.sqlite3'


def inventory_from(source, sealed, directory):
    with authenticated_digests(sealed) as known:
        capture_media_inventory(source, directory, offline_confirmed=True, guard_factory=Guard, known_digest=known)


def test_a_proven_digest_stands_in_for_hashing_the_file(tmp_path, monkeypatch):
    source, sealed = sealed_inventory_of(tmp_path)
    monkeypatch.setattr(media_inventory, 'hash_file', lambda *a: pytest.fail('Already hashed'))
    inventory_from(source, sealed, tmp_path / 'new')
    equivalent_inventories(sealed, tmp_path / 'new/inventory.sqlite3')


@pytest.mark.parametrize('change', ['extra', 'resized'])
def test_a_file_the_sealed_inventory_does_not_describe_is_refused(tmp_path, change):
    source, sealed = sealed_inventory_of(tmp_path)
    if change == 'extra':
        (source / 'b.jpg').write_bytes(b'new')
    else:
        (source / 'a.jpg').write_bytes(b'photo-longer')
    with pytest.raises(InvalidArtifact, match='authenticated'):
        inventory_from(source, sealed, tmp_path / 'new')


def test_the_copy_still_hashes_what_it_reads_after_a_proven_digest(tmp_path):
    source, sealed = sealed_inventory_of(tmp_path)
    (source / 'a.jpg').write_bytes(b'PHOTO')
    inventory_from(source, sealed, tmp_path / 'new')
    with pytest.raises(InvalidCopy, match='did not match'):
        copy_media_tree(source, tmp_path / 'new', tmp_path / 'copy', offline_confirmed=True, guard_factory=Guard)
    assert not (tmp_path / 'copy/media/a.jpg').exists()


def test_existing_recovery_target_refused_before_backup_access(tmp_path, monkeypatch):
    monkeypatch.setattr('app.backup.library_restore.verify_library', lambda *args: pytest.fail('Must refuse first'))
    existing = tmp_path / 'existing'
    existing.mkdir()
    with pytest.raises(FileExistsError):
        restore_library(tmp_path / 'backup', b'key', existing, None, None,
                        expected_cluster_directory=tmp_path, database_budget=100)


def test_media_failure_retains_incomplete_restore_without_success_report(tmp_path, monkeypatch):
    from app.backup import library_restore as module
    monkeypatch.setattr(module, 'verify_library', lambda *args, **kwargs: {
        'id': 'capture', 'media_files': 1, 'media_bytes': 100, 'media_inventory_sha256': 'digest'})
    monkeypatch.setattr(module, 'verify_artifact', lambda *args: {'metadata': {'file_bytes': 5}})
    # The stubbed backup has no inventory for the destination path preflight to read.
    monkeypatch.setattr(module, 'check_copy_paths', lambda *args: 0)
    calls = []
    def catalog(*args, **kwargs):
        calls.append('catalog-restored')
        return {'database': 'fixture', 'activation_allowed': False}
    def fail(*args, **kwargs):
        raise OSError('Injected disk disconnect')
    monkeypatch.setattr(module, 'restore_catalog', catalog)
    monkeypatch.setattr(module, 'capture_media_inventory', fail)
    class Guard:
        def __init__(self, requirements):
            pass
        def check(self):
            pass
    target = tmp_path / 'recovery'
    with pytest.raises(OSError, match='disconnect'):
        restore_library(tmp_path / 'backup', KEY, target, Endpoint('127.0.0.1', 55489, 'postgres', 'fixture'), None,
                        expected_cluster_directory=tmp_path, database_budget=100, guard_factory=Guard)
    assert calls == ['catalog-restored']
    assert (target / 'LIBRARY-RESTORE-INCOMPLETE').exists()
    assert (target / 'restore-job.json').exists()
    assert not (target / 'LIBRARY-RECOVERY-REPORT.json').exists()
    with pytest.raises(InvalidArtifact, match='identity'):
        restore_library(tmp_path / 'backup', KEY, target, Endpoint('127.0.0.1', 55490, 'postgres', 'fixture'), None,
                        expected_cluster_directory=tmp_path, database_budget=100, resume=True, guard_factory=Guard)
    assert calls == ['catalog-restored']
