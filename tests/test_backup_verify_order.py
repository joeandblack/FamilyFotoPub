"""Content verification reads files in the order copies write them, not directory order."""
from contextlib import closing
import os
import sqlite3
import sys

import pytest

from app.backup import media_tree
from app.backup.media_inventory import InvalidMediaInventory, capture_media_inventory
from app.fastbackup import full_copy


class Guard:
    def __init__(self, requirements):
        pass

    def check(self, **kwargs):
        pass

    def written(self, *args):
        pass


NAMES = ['ff/9.jpg', '00/b.mov', '7a/1.heic', '00/a.jpg', 'c3/x.mp4']


def fixture(tmp_path):
    source = tmp_path / 'source'
    for index, name in enumerate(NAMES):
        (source / name).parent.mkdir(parents=True, exist_ok=True)
        (source / name).write_bytes(name.encode() * (index + 1))
    inventory = tmp_path / 'inventory'
    capture_media_inventory(source, inventory, offline_confirmed=True, guard_factory=Guard)
    return source, inventory


def database(inventory):
    return closing(sqlite3.connect((inventory / 'inventory.sqlite3').as_uri() + '?mode=ro', uri=True))


def recording_hashes(monkeypatch, tree):
    order, real = [], media_tree.hash_file
    def record(path, expected, **options):
        order.append(path.relative_to(tree).as_posix())
        return real(path, expected, **options)
    monkeypatch.setattr(media_tree, 'hash_file', record)
    return order


def test_content_pass_reads_in_path_order_whatever_the_listing_order(tmp_path, monkeypatch):
    source, inventory = fixture(tmp_path)
    real_scandir = os.scandir
    class Reversed:
        def __init__(self, path):
            self.entries = sorted(real_scandir(path), key=lambda e: e.name, reverse=True)
        def __iter__(self):
            return self
        def __next__(self):
            if not self.entries:
                raise StopIteration
            return self.entries.pop(0)
        def close(self):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            pass
    monkeypatch.setattr(os, 'scandir', Reversed)
    order = recording_hashes(monkeypatch, source)
    with database(inventory) as db:
        media_tree.verify_tree(db, source)
    assert order == sorted(NAMES)


def test_progress_counts_every_file_once(tmp_path):
    source, inventory = fixture(tmp_path)
    events = []
    with database(inventory) as db:
        media_tree.verify_tree(db, source, progress=events.append)
    assert [e['files'] for e in events] == list(range(1, len(NAMES) + 1))
    assert events[-1]['bytes'] == events[-1]['total_bytes']
    assert {e['phase'] for e in events} == {'verify'}


def test_the_walk_still_refuses_an_unlisted_file(tmp_path):
    source, inventory = fixture(tmp_path)
    (source / '00/extra.jpg').write_bytes(b'not in the inventory')
    with database(inventory) as db, pytest.raises(InvalidMediaInventory, match='Unexpected entry'):
        media_tree.verify_tree(db, source)


def test_a_missing_file_is_reported_as_incomplete(tmp_path):
    source, inventory = fixture(tmp_path)
    (source / '7a/1.heic').unlink()
    with database(inventory) as db, pytest.raises(InvalidMediaInventory, match='incomplete'):
        media_tree.verify_tree(db, source)


def test_a_corrupt_file_fails_the_content_pass(tmp_path):
    source, inventory = fixture(tmp_path)
    target = source / 'c3/x.mp4'
    target.write_bytes(b'?' * target.stat().st_size)
    with database(inventory) as db, pytest.raises(InvalidMediaInventory, match='content verification'):
        media_tree.verify_tree(db, source)


def test_a_file_replaced_after_it_was_read_fails_the_walk(tmp_path):
    source, inventory = fixture(tmp_path)
    with database(inventory) as db:
        hashed = media_tree.hash_in_order(db, source)
        target = source / '00/a.jpg'
        info, content = target.stat(), target.read_bytes()
        target.unlink()
        target.write_bytes(content)  # same name, size, content and time; another file
        os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))
        media_tree.verify_tree(db, source, content=False)  # names and sizes alone still match
        with pytest.raises(InvalidMediaInventory, match='changed during verification'):
            media_tree.verify_tree(db, source, content=False, hashed=hashed)


def test_hashed_is_only_for_a_walk_without_content(tmp_path):
    source, inventory = fixture(tmp_path)
    with database(inventory) as db, pytest.raises(ValueError, match='content=False'):
        media_tree.verify_tree(db, source, hashed='0' * 64)


@pytest.mark.skipif(sys.platform == 'win32', reason='Windows has its own ordered flush-and-hash pass')
def test_fast_copy_verifies_in_write_order_and_binds_the_final_walk(tmp_path, monkeypatch):
    source, inventory = fixture(tmp_path)
    target = tmp_path / 'copy'
    written, real_copy = [], full_copy.copy_file
    def record_copy(src, dst, **kwargs):
        written.append(dst.relative_to(target / 'media').as_posix())
        return real_copy(src, dst, **kwargs)
    monkeypatch.setattr(full_copy, 'copy_file', record_copy)
    order = recording_hashes(monkeypatch, target / 'media')
    walks, real_verify = [], full_copy.verify_tree
    def record_walk(db, tree, **kwargs):
        walks.append(kwargs)
        return real_verify(db, tree, **kwargs)
    monkeypatch.setattr(full_copy, 'verify_tree', record_walk)
    result = full_copy.copy_media_tree(source, inventory, target, offline_confirmed=True, guard_factory=Guard)
    assert result['content_verified'] and result['files'] == len(NAMES)
    assert order == written == sorted(NAMES)
    assert len(walks) == 1 and walks[0]['content'] is False and len(walks[0]['hashed']) == 64
