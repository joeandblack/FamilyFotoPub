import os

import pytest

from app.backup import media_receipt, media_tree
from app.backup.media_inventory import InvalidMediaInventory
from app.backup.media_receipt import read_receipt, verified_media
from tests.test_backup_media_tree import copy, fixture_tree
from tests.backup_keys import backup_key


KEY = backup_key()


@pytest.fixture
def tree(tmp_path):
    source, inventory, target = fixture_tree(tmp_path)
    copy(source, inventory, target)
    home = tmp_path / 'recovery'
    home.mkdir()
    return target, home


@pytest.fixture
def full_reads(monkeypatch):
    """Count the calls that read content, and still make them."""
    reads = []
    real = media_tree.verify_media_tree

    def counted(target, progress=None, *, content=True):
        if content:
            reads.append(target)
        return real(target, progress, content=content)
    monkeypatch.setattr(media_receipt, 'verify_media_tree', counted)
    return reads


def rewrite_keeping_size(path, content):
    """Change a file's bytes and leave it a new modification time, as any rewrite does."""
    info = path.stat()
    assert len(content) == info.st_size
    path.write_bytes(content)
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 2_000_000_000))


def test_first_check_reads_everything_and_later_checks_only_walk(tree, full_reads):
    target, home = tree
    first = verified_media(home, KEY, target)
    assert first['content_verified'] is True and 'from_receipt' not in first
    assert full_reads == [target]

    events = []
    second = verified_media(home, KEY, target, progress=events.append)
    assert second['content_verified'] is True and second['from_receipt'] is True
    assert (second['files'], second['bytes']) == (first['files'], first['bytes']) == (2, 19)
    assert full_reads == [target]
    assert [event['phase'] for event in events] == ['verify-receipt']


def test_reverify_reads_everything_again(tree, full_reads):
    target, home = tree
    verified_media(home, KEY, target)
    assert 'from_receipt' not in verified_media(home, KEY, target, reverify=True)
    assert len(full_reads) == 2


def test_rewritten_file_of_the_same_size_is_read_again_and_refused(tree, full_reads):
    target, home = tree
    verified_media(home, KEY, target)
    rewrite_keeping_size(target / 'media/a.jpg', b'PHOTO')
    with pytest.raises(InvalidMediaInventory):
        verified_media(home, KEY, target)
    assert len(full_reads) == 2


def test_touched_but_unchanged_file_costs_one_full_read_then_is_trusted_again(tree, full_reads):
    target, home = tree
    verified_media(home, KEY, target)
    rewrite_keeping_size(target / 'media/a.jpg', b'photo')
    assert 'from_receipt' not in verified_media(home, KEY, target)
    assert verified_media(home, KEY, target)['from_receipt'] is True
    assert len(full_reads) == 2


def test_missing_or_resized_file_fails_on_the_quick_path(tree, full_reads):
    target, home = tree
    verified_media(home, KEY, target)
    (target / 'media/a.jpg').write_bytes(b'longer photo')
    with pytest.raises(InvalidMediaInventory):
        verified_media(home, KEY, target)
    (target / 'media/a.jpg').unlink()
    with pytest.raises(InvalidMediaInventory):
        verified_media(home, KEY, target)
    assert len(full_reads) == 1


def test_receipt_from_another_key_or_a_damaged_one_means_a_full_read(tree, full_reads):
    target, home = tree
    verified_media(home, backup_key(), target)
    assert read_receipt(home, KEY) is None
    assert 'from_receipt' not in verified_media(home, KEY, target)
    (home / media_receipt.CANDIDATE).write_text('{"sealed": "not a token"}')
    assert 'from_receipt' not in verified_media(home, KEY, target)
    assert len(full_reads) == 3


def test_receipt_names_one_tree_only(tree, full_reads, tmp_path):
    target, home = tree
    verified_media(home, KEY, target)
    moved = tmp_path / 'elsewhere'
    target.rename(moved)
    assert 'from_receipt' not in verified_media(home, KEY, moved)
    assert len(full_reads) == 2


def test_backup_and_candidate_receipts_are_separate(tree, full_reads):
    target, home = tree
    verified_media(home, KEY, target, name=media_receipt.BACKUP)
    assert read_receipt(home, KEY) is None
    assert 'from_receipt' not in verified_media(home, KEY, target)
    assert len(full_reads) == 2


def test_receipt_is_sealed_and_holds_no_file_names(tree):
    target, home = tree
    verified_media(home, KEY, target)
    raw = (home / media_receipt.CANDIDATE).read_text()
    assert 'a.jpg' not in raw and str(target) not in raw
    assert set(read_receipt(home, KEY)) == {'format', 'tree', 'identity', 'volume', 'inventory_sha256', 'files',
                                            'bytes', 'tree_signature', 'verified_at'}


def test_only_a_full_read_earns_a_receipt(tree):
    target, home = tree
    quick = media_tree.verify_media_tree(target, content=False)
    assert quick['content_verified'] is False
    with pytest.raises(ValueError):
        media_receipt.write_receipt(home, KEY, target, quick)
