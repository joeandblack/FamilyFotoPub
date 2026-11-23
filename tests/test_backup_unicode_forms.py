"""One file, two spellings: macOS exFAT lists a name decomposed, and Windows lists it composed.

macOS stores a composed (NFC) name on exFAT whichever form was written, then lists it
decomposed (NFD). A backup moved between the two must verify under either listing, so an
inventory path is matched by Unicode form. The tests give the inventory one form and the
tree the other, which any filesystem that keeps the spelling it was given can show.
"""
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import unicodedata
import uuid

import pytest

from app.backup.library_restore import equivalent_inventories
from app.backup import media_inventory, media_tree
from app.backup.media_inventory import (capture_media_inventory, recorded_name, resolved_path, verify_media_inventory,
                                        InvalidMediaInventory)
from app.backup.media_tree import copy_media_tree, verify_media_tree
from tests.test_backup_capture_retry import ample

NAMES = ['café.jpg', 'Ångström/ñandú.jpg', '照片/写真.jpg', 'かな/が.jpg', '한국/한.jpg']
EXFAT_ROOT = os.environ.get('FAMILYFOTO_BACKUP_EXFAT_ROOT')


def form(name, which):
    return unicodedata.normalize(which, name)


def tree_with(root, which):
    for name in NAMES:
        path = root / form(name, which)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode('utf-8') * 20)
    return root


def keeps_spelling(tmp_path):
    """The simulated listing needs a disk that lists each name as it was written."""
    probe = tmp_path / 'spelling-probe'
    probe.mkdir()
    (probe / form('é', 'NFD')).write_bytes(b'x')
    return os.listdir(probe) == [form('é', 'NFD')]


@pytest.fixture
def spelled(tmp_path):
    if not keeps_spelling(tmp_path):
        pytest.skip('This filesystem normalizes names itself')
    return tmp_path


def respell(inventory, which):
    """Rewrite an inventory as if another system had listed its names in another form."""
    with closing(sqlite3.connect(inventory / 'inventory.sqlite3')) as db:
        for (path,) in db.execute('SELECT path FROM entries').fetchall():
            db.execute('UPDATE entries SET path=? WHERE path=?', (form(path, which), path))
        db.commit()


def paths(inventory):
    with closing(sqlite3.connect(inventory / 'inventory.sqlite3')) as db:
        return {row[0] for row in db.execute('SELECT path FROM entries')}


def inventory_of(tmp_path, root, name='inventory', **kwargs):
    capture_media_inventory(root, tmp_path / name, offline_confirmed=True, guard_factory=ample, **kwargs)
    return tmp_path / name


@pytest.mark.parametrize('written, recorded', [('NFD', 'NFC'), ('NFC', 'NFD')], ids=['mac-listing', 'windows-listing'])
def test_an_inventory_in_the_other_form_still_verifies(spelled, written, recorded):
    root = tree_with(spelled / 'media', written)
    inventory = inventory_of(spelled, root)
    respell(inventory, recorded)
    assert verify_media_inventory(inventory, root)['content_verified'] is True


@pytest.mark.parametrize('written, recorded', [('NFD', 'NFC'), ('NFC', 'NFD')], ids=['mac-listing', 'windows-listing'])
def test_catalog_references_match_in_either_form(spelled, written, recorded):
    root = tree_with(spelled / 'media', written)
    inventory = inventory_of(spelled, root)
    respell(inventory, recorded)
    verify_media_inventory(inventory, root, references=[form(NAMES[0], written)])


def test_the_other_form_does_not_hide_changed_content(spelled):
    root = tree_with(spelled / 'media', 'NFD')
    inventory = inventory_of(spelled, root)
    respell(inventory, 'NFC')
    (root / form(NAMES[0], 'NFD')).write_bytes(b'changed content')
    with pytest.raises(InvalidMediaInventory):
        verify_media_inventory(inventory, root)


def test_the_other_form_does_not_hide_a_missing_file(spelled):
    root = tree_with(spelled / 'media', 'NFD')
    inventory = inventory_of(spelled, root)
    respell(inventory, 'NFC')
    (root / form(NAMES[0], 'NFD')).unlink()
    with pytest.raises(InvalidMediaInventory):
        verify_media_inventory(inventory, root)


def test_only_the_unicode_form_is_forgiven_not_the_case(spelled):
    root = tree_with(spelled / 'media', 'NFD')
    inventory = inventory_of(spelled, root)
    with closing(sqlite3.connect(inventory / 'inventory.sqlite3')) as db:
        db.execute('UPDATE entries SET path=?, portable=? WHERE path=?',
                   (form('CAFÉ.jpg', 'NFC'), form('café.jpg', 'NFC'), form('café.jpg', 'NFD')))
        db.commit()
    with pytest.raises(InvalidMediaInventory):
        verify_media_inventory(inventory, root)


def test_a_different_name_is_not_matched(spelled):
    root = tree_with(spelled / 'media', 'NFD')
    inventory = inventory_of(spelled, root)
    with closing(sqlite3.connect(inventory / 'inventory.sqlite3')) as db:
        assert recorded_name(db, form('café.jpg', 'NFC')) == form('café.jpg', 'NFD')
        assert recorded_name(db, 'cafe.jpg') is None
        assert recorded_name(db, 'other/café.jpg') is None


def rename_to(root, which):
    """Rename every path in a tree to one Unicode form, as another system would list it."""
    for directory, directories, files in os.walk(root, topdown=False):
        for entry in files + directories:
            target = Path(directory) / form(entry, which)
            if target.name != entry:
                os.rename(Path(directory) / entry, target)


def test_a_copied_tree_verifies_in_the_other_form(spelled):
    root = tree_with(spelled / 'media', 'NFC')
    inventory = inventory_of(spelled, root)
    copy_media_tree(root, inventory, spelled / 'copy', offline_confirmed=True, guard_factory=ample)
    rename_to(spelled / 'copy/media', 'NFD')
    assert verify_media_tree(spelled / 'copy')['files'] == len(NAMES)


def test_reading_a_backup_back_keeps_the_backups_own_spelling(spelled):
    """A restore compares against the authenticated inventory, whatever form the disk lists."""
    root = tree_with(spelled / 'media', 'NFD')
    authentic = inventory_of(spelled, tree_with(spelled / 'source', 'NFC'), 'authentic')
    observed = inventory_of(spelled, root, 'observed', authenticated=authentic / 'inventory.sqlite3')
    assert paths(observed) == paths(authentic)
    equivalent_inventories(authentic / 'inventory.sqlite3', observed / 'inventory.sqlite3')


def test_reading_back_still_refuses_a_name_the_backup_never_had(spelled):
    root = tree_with(spelled / 'media', 'NFD')
    (root / 'extra.jpg').write_bytes(b'extra')
    authentic = inventory_of(spelled, tree_with(spelled / 'source', 'NFC'), 'authentic')
    observed = inventory_of(spelled, root, 'observed', authenticated=authentic / 'inventory.sqlite3')
    with pytest.raises(Exception):
        equivalent_inventories(authentic / 'inventory.sqlite3', observed / 'inventory.sqlite3')


def twin_listing(monkeypatch, module, root, extra, twin):
    """List `twin` as one more entry beside `extra`, whether or not this disk keeps both spellings.

    A disk that keeps both spellings gets a real second file. A disk that merges them
    (APFS, macOS exFAT) lists the same file twice instead, which reads as one file.
    """
    second = root / twin
    if second.exists():
        # Merged: the other spelling opens the first file, so only the listing differs.
        real = module.walk

        def listing(base):
            for name, kind, info in real(base):
                yield name, kind, info
                if name == extra and Path(base) == root:
                    yield twin, kind, info
        monkeypatch.setattr(module, 'walk', listing)
    else:
        second.write_bytes((root / extra).read_bytes())


def test_a_second_spelling_cannot_stand_in_for_a_missing_file_in_a_tree(spelled, monkeypatch):
    nfc, nfd = form('café.jpg', 'NFC'), form('café.jpg', 'NFD')
    source = spelled / 'media'
    source.mkdir()
    (source / nfc).write_bytes(b'cafe photo')
    (source / 'two.jpg').write_bytes(b'two photo')
    inventory = inventory_of(spelled, source)
    copy_media_tree(source, inventory, spelled / 'copy', offline_confirmed=True, guard_factory=ample)
    assert verify_media_tree(spelled / 'copy')['files'] == 2
    (spelled / 'copy/media/two.jpg').unlink()
    listed = next(name for name in os.listdir(spelled / 'copy/media') if name != 'two.jpg')
    twin_listing(monkeypatch, media_tree, spelled / 'copy/media', listed, nfd if listed == nfc else nfc)
    with pytest.raises(InvalidMediaInventory):
        verify_media_tree(spelled / 'copy')


def test_a_second_spelling_cannot_stand_in_for_a_missing_file_in_an_inventory(spelled, monkeypatch):
    nfc, nfd = form('café.jpg', 'NFC'), form('café.jpg', 'NFD')
    source = spelled / 'media'
    source.mkdir()
    (source / nfc).write_bytes(b'cafe photo')
    (source / 'two.jpg').write_bytes(b'two photo')
    inventory = inventory_of(spelled, source)
    assert verify_media_inventory(inventory, source)['files'] == 2
    (source / 'two.jpg').unlink()
    listed = next(name for name in os.listdir(source) if name != 'two.jpg')
    twin = nfd if listed == nfc else nfc
    if (source / twin).exists():
        real = media_inventory.walk

        def listing(base):
            for name, kind, info in real(base):
                yield name, kind, info
                if name == listed:
                    yield twin, kind, info
        monkeypatch.setattr(media_inventory, 'walk', listing)
    else:
        os.link(source / listed, source / twin)
    with pytest.raises(InvalidMediaInventory):
        verify_media_inventory(inventory, source)


@pytest.fixture
def distinct_spellings(monkeypatch):
    """Make this disk answer like NTFS: a name exists only in the spelling it is stored in."""
    def exact(path):
        path = Path(path)
        return path.parent.is_dir() and path.name in os.listdir(path.parent)
    monkeypatch.setattr(media_inventory.os.path, 'lexists', exact)


def test_a_name_resolves_to_the_spelling_this_disk_stores(spelled, distinct_spellings):
    (spelled / form('café', 'NFC')).mkdir()
    (spelled / form('café', 'NFC') / form('é.jpg', 'NFC')).write_bytes(b'x')
    found = resolved_path(spelled, form('café/é.jpg', 'NFD'))
    assert found == spelled / form('café', 'NFC') / form('é.jpg', 'NFC')
    assert os.path.exists(found)


def test_a_part_not_on_the_disk_yet_keeps_the_inventorys_spelling(spelled, distinct_spellings):
    (spelled / form('café', 'NFC')).mkdir()
    name = form('café/new-é.jpg', 'NFD')
    assert resolved_path(spelled, name) == spelled / form('café', 'NFC') / form('new-é.jpg', 'NFD')


def test_an_ambiguous_name_is_not_guessed(spelled, distinct_spellings, monkeypatch):
    """Two stored spellings that both read as the asked-for name: refuse to pick one."""
    asked = unicodedata.normalize('NFC', 'a\u0323\u0308.jpg')
    first, second = 'a\u0323\u0308.jpg', 'a\u0308\u0323.jpg'
    assert len({first, second, asked}) == 3
    assert {unicodedata.normalize('NFC', name) for name in (first, second, asked)} == {asked}
    monkeypatch.setattr(media_inventory.os, 'listdir', lambda directory: [first, second])
    assert resolved_path(spelled, asked) == spelled / asked


def test_one_equivalent_spelling_is_taken(spelled, distinct_spellings, monkeypatch):
    asked = unicodedata.normalize('NFC', 'a\u0323\u0308.jpg')
    stored = 'a\u0323\u0308.jpg'
    monkeypatch.setattr(media_inventory.os, 'listdir', lambda directory: [stored, 'other.jpg'])
    assert resolved_path(spelled, asked) == spelled / stored


def test_a_name_that_exists_exactly_is_never_rewritten(spelled, distinct_spellings):
    (spelled / form('é.jpg', 'NFD')).write_bytes(b'x')
    (spelled / form('é.jpg', 'NFC')).write_bytes(b'y')
    if len(os.listdir(spelled)) < 3:  # the probe folder, and one file per spelling
        pytest.skip('This filesystem merges the two spellings')
    assert resolved_path(spelled, form('é.jpg', 'NFC')) == spelled / form('é.jpg', 'NFC')


def test_a_copy_opens_the_source_in_the_spelling_the_disk_stores(spelled, distinct_spellings, monkeypatch):
    """A name recorded decomposed on a Mac, on a disk that stores it composed (Windows)."""
    source = tree_with(spelled / 'media', 'NFC')
    inventory = inventory_of(spelled, source)
    respell(inventory, 'NFD')
    opened = []
    real = media_tree.copy_media_file

    def record(path, *args, **kwargs):
        opened.append(Path(path))
        return real(path, *args, **kwargs)
    monkeypatch.setattr(media_tree, 'copy_media_file', record)
    copy_media_tree(source, inventory, spelled / 'copy', offline_confirmed=True, guard_factory=ample)
    stored = {form(name, 'NFC') for name in NAMES}
    assert opened and all(form(path.relative_to(source).as_posix(), 'NFC') in stored for path in opened)
    assert all(path.relative_to(source).as_posix() == form(path.relative_to(source).as_posix(), 'NFC') for path in opened)


def test_a_copy_keeps_the_inventorys_spelling_for_what_it_writes(spelled, distinct_spellings):
    source = tree_with(spelled / 'media', 'NFC')
    inventory = inventory_of(spelled, source)
    respell(inventory, 'NFD')
    copy_media_tree(source, inventory, spelled / 'copy', offline_confirmed=True, guard_factory=ample)
    listed = {Path(directory).relative_to(spelled / 'copy/media') / entry
              for directory, directories, files in os.walk(spelled / 'copy/media') for entry in files}
    assert {form(path.as_posix(), 'NFD') for path in listed} == {form(name, 'NFD') for name in NAMES}
    assert all(path.as_posix() == form(path.as_posix(), 'NFD') for path in listed)


def test_a_resumed_copy_reuses_what_the_disk_already_holds_in_the_other_spelling(
        spelled, distinct_spellings, monkeypatch):
    """A tree a Mac started on exFAT, resumed where the stored composed names are separate names."""
    source = tree_with(spelled / 'media', 'NFC')
    inventory = inventory_of(spelled, source)
    respell(inventory, 'NFD')

    def stop(event):
        if event.get('phase') == 'copy' and event['files'] == 2:
            raise OSError('Synthetic interruption')
    with pytest.raises(OSError, match='Synthetic'):
        copy_media_tree(source, inventory, spelled / 'copy', offline_confirmed=True, guard_factory=ample,
                        progress=stop)
    # What this disk stores: every name composed.
    rename_to(spelled / 'copy/media', 'NFC')
    real_mkdir, real_copy = Path.mkdir, media_tree.copy_media_file
    made, copied = [], []

    def exists(self, *args, **kwargs):
        parent = self.parent
        return parent.is_dir() and self.name in os.listdir(parent)

    def mkdir(self, *args, **kwargs):
        made.append(self)
        return real_mkdir(self, *args, **kwargs)

    def copy(path, *args, **kwargs):
        copied.append(Path(path))
        return real_copy(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'exists', exists)
    monkeypatch.setattr(Path, 'mkdir', mkdir)
    monkeypatch.setattr(media_tree, 'copy_media_file', copy)
    copy_media_tree(source, inventory, spelled / 'copy', offline_confirmed=True, guard_factory=ample)
    monkeypatch.undo()
    assert not any(path.name != form(path.name, 'NFC') for path in made if path.is_relative_to(spelled / 'copy/media'))
    # Only the files that were not done yet were copied again, and nothing was written twice.
    assert len(copied) == len(NAMES) - 2
    written = [Path(directory, entry).relative_to(spelled / 'copy/media').as_posix()
               for directory, directories, files in os.walk(spelled / 'copy/media') for entry in files]
    assert len(written) == len(NAMES)
    assert len({form(name, 'NFC') for name in written}) == len(written)
    assert verify_media_tree(spelled / 'copy')['files'] == len(NAMES)


@pytest.mark.skipif(not EXFAT_ROOT, reason='Set FAMILYFOTO_BACKUP_EXFAT_ROOT to a folder on a real exFAT disk')
@pytest.mark.parametrize('written', ['NFC', 'NFD'])
def test_real_exfat_names_verify_under_both_listings(tmp_path, written):
    """Names written composed or decomposed on a real disk, then read as the other system would."""
    exfat = Path(EXFAT_ROOT) / f'familyfoto-test-{uuid.uuid4().hex}'
    exfat.mkdir()
    try:
        root = tree_with(exfat / 'media', written)
        inventory = inventory_of(tmp_path, root)
        assert verify_media_inventory(inventory, root)['content_verified'] is True
        # A source tree on this disk lists the sidecars macOS made, as media.
        files = verify_media_inventory(inventory, root)['files']
        assert files >= len(NAMES)
        for recorded in ('NFC', 'NFD'):
            respell(inventory, recorded)
            assert verify_media_inventory(inventory, root)['files'] == files
        for recorded in ('NFC', 'NFD'):
            respell(inventory, recorded)
            copy = exfat / f'copy-{recorded}'
            copy_media_tree(root, inventory, copy, offline_confirmed=True, guard_factory=ample,
                            allow_unprotected=True)
            assert verify_media_tree(copy)['files'] == files
            observed = inventory_of(tmp_path, copy / 'media', f'observed-{recorded}',
                                    authenticated=copy / 'inventory/inventory.sqlite3')
            equivalent_inventories(copy / 'inventory/inventory.sqlite3', observed / 'inventory.sqlite3')
    finally:
        # Within one mount, macOS exFAT deletes a name only by the spelling it was created with.
        for directory, directories, files in os.walk(exfat, topdown=False):
            for entry in files + directories:
                for spelling in (lambda x: x, lambda x: form(x, 'NFC'), lambda x: form(x, 'NFD')):
                    target = Path(directory) / spelling(entry)
                    try:
                        os.rmdir(target) if target.is_dir() else os.unlink(target)
                        break
                    except OSError:
                        continue
        try:
            os.rmdir(exfat)
        except OSError:
            pass
