import hashlib
from pathlib import Path

import pytest

from app.backup import media_inventory, library
from app.fastbackup.tests.test_full_copy import Guard, copy, fixture
from app.fastbackup.tests.test_lightweight_preparation import edit_inventory


@pytest.mark.parametrize('immutable,walks', [(False, 2), (True, 1)])
def test_only_experimental_inventory_skips_second_source_walk(tmp_path, monkeypatch, immutable, walks):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'photo').write_bytes(b'photo')
    original, seen = media_inventory.walk, []

    def walk(root):
        seen.append(root)
        yield from original(root)

    monkeypatch.setattr(media_inventory, 'walk', walk)
    media_inventory.capture_media_inventory(source, tmp_path / 'inventory', offline_confirmed=True,
                                           guard_factory=Guard, assume_immutable_source=immutable)
    assert seen == [source] * walks


def test_standard_hash_keeps_change_checks_experimental_hash_skips_them(tmp_path):
    path = tmp_path / 'photo'
    path.write_bytes(b'photo')
    wrong_metadata = (0, 0, 0, 0)
    with pytest.raises(ValueError, match='changed'):
        media_inventory.hash_file(path, wrong_metadata)
    assert media_inventory.hash_file(path, wrong_metadata, check_changes=False) == hashlib.sha256(b'photo').hexdigest()


@pytest.mark.parametrize('digest', [None, '', 'x' * 64, 'a' * 63])
def test_full_verification_cannot_skip_missing_or_invalid_digest(tmp_path, digest):
    paths = fixture(tmp_path)
    edit_inventory(paths, "UPDATE entries SET sha256=? WHERE path='photo.jpg'", (digest,))
    with pytest.raises(ValueError, match='digest'):
        copy(paths)
    assert not paths[2].exists()


@pytest.mark.parametrize('immutable', [False, True])
def test_catalog_fallback_uses_explicit_source_policy(tmp_path, monkeypatch, immutable):
    from types import SimpleNamespace

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, query):
            pass

        def __iter__(self):
            return iter(())

    calls = []
    monkeypatch.setattr(library, 'hash_file', lambda path, expected, **options: calls.append(options) or 'digest')
    path = tmp_path / 'photo'
    path.write_bytes(b'photo')
    lookup = library.catalog_digests(SimpleNamespace(cursor=lambda **kwargs: Cursor()), tmp_path,
                                     assume_immutable_source=immutable)
    assert lookup('photo', path.stat()) == 'digest'
    assert calls == [{'check_changes': False} if immutable else {}]


def test_native_copy_never_restats_source_after_copy(tmp_path, monkeypatch):
    from app.fastbackup import native_copy
    source, target = tmp_path / 'photo', tmp_path / 'copy'
    source.write_bytes(b'photo')
    original_stat = Path.stat
    source_queries = []

    def stat(path, *args, **kwargs):
        if path == source:
            source_queries.append(target.exists())
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'stat', stat)
    native_copy.copy_file(source, target)
    assert target.read_bytes() == b'photo'
    assert source_queries and not any(source_queries)


def test_native_copy_never_overwrites_existing_destination(tmp_path):
    from app.fastbackup.native_copy import copy_file
    source, target = tmp_path / 'photo', tmp_path / 'copy'
    source.write_bytes(b'photo')
    target.write_bytes(b'keep')
    with pytest.raises(FileExistsError):
        copy_file(source, target)
    assert target.read_bytes() == b'keep'
