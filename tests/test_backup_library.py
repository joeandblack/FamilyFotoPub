from contextlib import contextmanager
import hashlib

import pytest

from app.backup.library import reconcile_media
from app.backup.media_inventory import capture_media_inventory, InvalidMediaInventory


class Guard:
    def __init__(self, requirements):
        pass

    def check(self, **kwargs):
        pass


class Database:
    def __init__(self, rows):
        self.rows = rows

    @contextmanager
    def cursor(self, name):
        yield self

    def execute(self, query):
        assert 't.deleted_at IS NULL' in query

    def __iter__(self):
        return iter(self.rows)


def inventory(tmp_path):
    root = tmp_path / 'media'
    root.mkdir()
    (root / 'photo.jpg').write_bytes(b'photo')
    target = tmp_path / 'inventory'
    capture_media_inventory(root, target, offline_confirmed=True, guard_factory=Guard)
    return target


def row(**changes):
    data = dict(path='photo.jpg', size=5, digest=hashlib.sha256(b'photo').hexdigest(),
                original=None, original_size=None, original_digest=None,
                conversion=None, metadata=None, deleting=None)
    data.update(changes)
    return tuple(data.values())


def test_catalog_files_must_match_inventory(tmp_path):
    target = inventory(tmp_path)
    assert reconcile_media(Database([row()]), target)['catalog_media'] == 1
    for bad in (row(path='missing'), row(size=6), row(digest='0' * 64)):
        with pytest.raises(InvalidMediaInventory, match='missing or'):
            reconcile_media(Database([bad]), target)


def test_unfinished_delete_blocks_publication(tmp_path):
    target = inventory(tmp_path)
    with pytest.raises(InvalidMediaInventory, match='interrupted media deletion'):
        reconcile_media(Database([row(deleting='intent')]), target)
    with pytest.raises(InvalidMediaInventory, match='interrupted original deletion'):
        reconcile_media(Database([row(original='old.MTS', metadata={
            'conversion_validation': {'original_delete_started_at': 'intent'}})]), target)


def test_missing_retained_original_needs_known_removal_evidence(tmp_path):
    target = inventory(tmp_path)
    with pytest.raises(InvalidMediaInventory, match='missing or'):
        reconcile_media(Database([row(original='old.MTS', conversion='done')]), target)
    removed = row(original='old.MTS', conversion='done', metadata={
        'conversion_validation': {'original_deleted_at': 'receipt'}})
    assert reconcile_media(Database([removed]), target)['removed_originals'] == 1
    assert reconcile_media(Database([row(original='old.AVI', conversion='done')]), target)['removed_originals'] == 1
