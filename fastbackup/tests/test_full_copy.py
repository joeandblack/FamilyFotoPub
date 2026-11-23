import json

import pytest

from app.backup.media_inventory import capture_media_inventory
from app.backup.media_tree import verify_media_tree
from fastbackup import full_copy


class Guard:
    def __init__(self, requirements):
        pass
    def check(self, **kwargs):
        pass
    def written(self, *args):
        pass


def fixture(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'nested').mkdir()
    (source / 'empty-dir').mkdir()
    (source / 'photo.jpg').write_bytes(b'photo\x1a')
    (source / 'nested/video.mp4').write_bytes(b'video' * 10000)
    (source / 'zero').write_bytes(b'')
    inventory = tmp_path / 'inventory'
    capture_media_inventory(source, inventory, offline_confirmed=True, guard_factory=Guard)
    return source, inventory, tmp_path / 'copy'


def copy(paths, **kwargs):
    return full_copy.copy_media_tree(*paths, offline_confirmed=True, guard_factory=Guard, **kwargs)


def test_native_copy_is_compatible_and_nonresumable(tmp_path):
    paths = fixture(tmp_path)
    events = []
    result = copy(paths, progress=events.append)
    assert result['content_verified'] and result['files'] == 3
    assert verify_media_tree(paths[2])['bytes'] == 50006
    assert (paths[2] / 'media/empty-dir').is_dir()
    assert not (paths[2] / 'file-jobs').exists()
    assert (paths[0] / 'photo.jpg').read_bytes() == b'photo\x1a'
    assert events[-1]['files'] == 3
    with pytest.raises(FileExistsError):
        copy(paths)


def test_failure_retains_incomplete_attempt(tmp_path, monkeypatch):
    paths = fixture(tmp_path)
    real = full_copy.copy_file
    def fail(source, target, **kwargs):
        real(source, target, **kwargs)
        raise OSError('simulated disconnect')
    monkeypatch.setattr(full_copy, 'copy_file', fail)
    with pytest.raises(OSError):
        copy(paths)
    assert (paths[2] / 'TREE-INCOMPLETE').exists()
    assert not (paths[2] / 'TREE-COMPLETE.json').exists()
    assert list((paths[2] / 'media').rglob('*'))
    with pytest.raises(FileExistsError):
        copy(paths)


def test_corruption_never_publishes(tmp_path, monkeypatch):
    paths = fixture(tmp_path)
    real = full_copy.copy_file
    def corrupt(source, target, **kwargs):
        real(source, target, **kwargs)
        target.write_bytes(b'x' * target.stat().st_size)
    monkeypatch.setattr(full_copy, 'copy_file', corrupt)
    with pytest.raises(ValueError, match='verification'):
        copy(paths)
    assert not (paths[2] / 'TREE-COMPLETE.json').exists()


def test_cancel_prevents_completion(tmp_path):
    paths = fixture(tmp_path)
    with pytest.raises(InterruptedError):
        copy(paths, cancelled=lambda: True)
    assert not (paths[2] / 'TREE-COMPLETE.json').exists()


def test_extra_destination_rejected(tmp_path):
    paths = fixture(tmp_path)
    def inject(event):
        if event.get('phase') == 'media-transfer' and event.get('files') == 1:
            (paths[2] / 'media/unexpected').write_bytes(b'foreign')
    with pytest.raises(ValueError, match='Unexpected'):
        copy(paths, progress=inject)
    assert not (paths[2] / 'TREE-COMPLETE.json').exists()


def test_manifest_binding_and_offline_gate(tmp_path):
    paths = fixture(tmp_path)
    with pytest.raises(ValueError, match='Offline'):
        full_copy.copy_media_tree(*paths)
    assert not paths[2].exists()
    result = copy(paths)
    record = json.loads((paths[2] / 'copy-job.json').read_text())
    assert record['inventory_sha256'] == result['inventory_sha256']
