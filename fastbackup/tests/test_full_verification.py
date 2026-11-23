import os

import pytest

from fastbackup import full_copy
from fastbackup.tests.test_full_copy import Guard, fixture


@pytest.mark.skipif(os.name != 'nt', reason='Windows flush/hash pass')
def test_windows_flushes_and_hashes_every_file(tmp_path, monkeypatch):
    source, inventory, target = fixture(tmp_path)
    flushed, hashed = [], []
    original = full_copy._flush_and_verify
    def verify(path, size, digest):
        flushed.append(path)
        if digest is not None:
            hashed.append(path)
        return original(path, size, digest)
    monkeypatch.setattr(full_copy, '_flush_and_verify', verify)
    full_copy.copy_media_tree(source, inventory, target,
                              offline_confirmed=True, guard_factory=Guard)
    assert len(flushed) == 3
    assert len(hashed) == 3


def test_corrupt_copy_never_completes(tmp_path, monkeypatch):
    source, inventory, target = fixture(tmp_path)
    original_copy = full_copy.copy_file
    def corrupt(src, dst, **kwargs):
        original_copy(src, dst, **kwargs)
        if dst.stat().st_size:
            dst.write_bytes(b'x' * dst.stat().st_size)
    monkeypatch.setattr(full_copy, 'copy_file', corrupt)
    with pytest.raises(ValueError, match='verification'):
        full_copy.copy_media_tree(source, inventory, target,
                                  offline_confirmed=True, guard_factory=Guard)
    assert not (target / 'TREE-COMPLETE.json').exists()
