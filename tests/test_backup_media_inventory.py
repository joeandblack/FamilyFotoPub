import hashlib
import sqlite3

import pytest

from app.backup.media_inventory import (
    capture_media_inventory, InvalidMediaInventory, portable_path, verify_media_inventory,
)


class Guard:
    def __init__(self, requirements):
        pass

    def check(self, **kwargs):
        pass


def capture(root, target, **kwargs):
    return capture_media_inventory(root, target, offline_confirmed=True, guard_factory=Guard, **kwargs)


def test_inventory_includes_all_files_empty_directories_and_hashes(tmp_path):
    root = tmp_path / 'media'
    root.mkdir()
    for name in ('photo.jpg', '.retained/original.MTS', 'trash/old.bin', 'staging/upload.part'):
        path = root / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b'example')
    (root / 'empty').mkdir()
    result = capture(root, tmp_path / 'inventory')
    assert result['files'] == 4
    assert result['bytes'] == 28
    with sqlite3.connect(result['database']) as db:
        rows = db.execute("SELECT path, sha256 FROM entries WHERE kind='file'").fetchall()
        assert len(rows) == 4
        assert all(row[1] == hashlib.sha256(b'example').hexdigest() for row in rows)
        assert db.execute("SELECT kind FROM entries WHERE path='empty'").fetchone() == ('directory',)
        assert db.execute("SELECT value FROM metadata WHERE key='state'").fetchone() == ('complete',)
    assert not (tmp_path / 'inventory/INVENTORY-INCOMPLETE').exists()


def test_changed_source_leaves_incomplete_inventory(tmp_path):
    root = tmp_path / 'media'
    root.mkdir()
    photo = root / 'photo.jpg'
    photo.write_bytes(b'example')
    with pytest.raises(InvalidMediaInventory, match='changed'):
        capture(root, tmp_path / 'inventory', progress=lambda event: photo.write_bytes(b'changed-content'))
    assert (tmp_path / 'inventory/INVENTORY-INCOMPLETE').exists()


def test_inventory_requires_offline_and_independent_new_target(tmp_path):
    with pytest.raises(ValueError, match='offline'):
        capture_media_inventory(tmp_path, tmp_path / 'out')
    with pytest.raises(ValueError, match='separate'):
        capture(tmp_path, tmp_path / 'out')
    root = tmp_path / 'media'
    root.mkdir()
    target = tmp_path / 'existing'
    target.mkdir()
    with pytest.raises((FileExistsError, ValueError)):
        capture(root, target)


@pytest.mark.parametrize('name', ['../photo.jpg', '/a', 'a//b', 'CON.jpg', 'x/NUL',
                                   'a:b', 'a\\b', 'a.', 'a ', 'a?b', 'a\x00b',
                                   'CON .txt', 'con  .jpg', 'COM\u00b9', 'LPT\u00b2.jpg', 'COM\u00b3.txt'])
def test_nonportable_paths_rejected(name):
    with pytest.raises(InvalidMediaInventory):
        portable_path(name)


def test_unicode_and_case_collisions_have_same_key():
    assert portable_path('folder/PHOTO.JPG') == portable_path('Folder/photo.jpg')
    assert portable_path('caf\u00e9.jpg') == portable_path('cafe\u0301.jpg')


def test_space_failure_keeps_inventory_incomplete(tmp_path):
    root = tmp_path / 'media'
    root.mkdir()
    (root / 'a').write_bytes(b'a')

    class Full(Guard):
        def check(self, **kwargs):
            if kwargs:
                raise OSError('disk full')

    with pytest.raises(OSError, match='full'):
        capture_media_inventory(root, tmp_path / 'out', offline_confirmed=True, guard_factory=Full)
    assert (tmp_path / 'out/INVENTORY-INCOMPLETE').exists()


def test_verify_reconciles_catalog_and_detects_content_change(tmp_path):
    import os
    root = tmp_path / 'media'
    root.mkdir()
    photo = root / 'a.jpg'
    photo.write_bytes(b'abc')
    out = tmp_path / 'out'
    capture(root, out)
    assert verify_media_inventory(out, root, references=['a.jpg'])['content_verified']
    with pytest.raises(InvalidMediaInventory, match='absent'):
        verify_media_inventory(out, root, references=['missing.jpg'])
    info = photo.stat()
    photo.write_bytes(b'def')
    os.utime(photo, ns=(info.st_atime_ns, info.st_mtime_ns))
    with pytest.raises(InvalidMediaInventory, match='content changed'):
        verify_media_inventory(out, root)


def test_verify_refuses_deleted_file_extra_file_and_incomplete(tmp_path):
    root = tmp_path / 'media'
    root.mkdir()
    (root / 'a.jpg').write_bytes(b'a')
    out = tmp_path / 'out'
    capture(root, out)
    extra = root / 'extra.jpg'
    extra.write_bytes(b'b')
    with pytest.raises(InvalidMediaInventory, match='changed'):
        verify_media_inventory(out, root)
    extra.unlink()
    (root / 'a.jpg').unlink()
    with pytest.raises(InvalidMediaInventory, match='disappeared'):
        verify_media_inventory(out, root)
    (out / 'INVENTORY-INCOMPLETE').touch()
    with pytest.raises(InvalidMediaInventory, match='incomplete'):
        verify_media_inventory(out, root)


def test_no_fallback_to_replacement_root(tmp_path):
    root = tmp_path / 'media'
    root.mkdir()
    out = tmp_path / 'out'
    capture(root, out)
    replacement = tmp_path / 'replacement'
    replacement.mkdir()
    with pytest.raises(InvalidMediaInventory, match='another source'):
        verify_media_inventory(out, replacement)


def test_physical_unicode_collision_blocks_capture(tmp_path):
    root = tmp_path / 'media'
    root.mkdir()
    (root / 'caf\u00e9.jpg').write_bytes(b'a')
    second = root / 'cafe\u0301.jpg'
    if second.exists():
        pytest.skip('Filesystem normalizes Unicode names')
    second.write_bytes(b'b')
    with pytest.raises(InvalidMediaInventory, match='collide'):
        capture(root, tmp_path / 'out')
    assert (tmp_path / 'out/INVENTORY-INCOMPLETE').exists()
