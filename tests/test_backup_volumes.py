from pathlib import Path, PurePosixPath
import plistlib
from types import SimpleNamespace

import pytest

from app.backup import volumes
from app.backup.space import StorageUnavailable


def test_volume_identity_is_stable_for_same_local_disk(tmp_path):
    child = tmp_path / 'child'
    child.mkdir()
    assert volumes.volume_identity(tmp_path) == volumes.volume_identity(child)


def test_macos_probe_uses_plist_uuid_without_mount_name(monkeypatch):
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(stdout=plistlib.dumps({'VolumeUUID': '12345678-1234-1234-1234-123456789ABC'}))
    monkeypatch.setattr(volumes.subprocess, 'run', run)
    assert volumes.macos_volume(PurePosixPath('/Volumes/My Photos')) == {
        'kind': 'macos-volume-uuid', 'id': '12345678-1234-1234-1234-123456789abc'}
    assert calls[0][0] == ['/usr/sbin/diskutil', 'info', '-plist', '/Volumes/My Photos']
    assert calls[0][1]['timeout'] == 15


@pytest.mark.parametrize('payload', [b'not a plist', plistlib.dumps({}), plistlib.dumps({'VolumeUUID': 'bad'})])
def test_macos_missing_or_bad_identity_fails_closed(monkeypatch, payload):
    monkeypatch.setattr(volumes.subprocess, 'run', lambda *a, **k: SimpleNamespace(stdout=payload))
    with pytest.raises(StorageUnavailable):
        volumes.macos_volume(Path('/Volumes/Test'))


def test_changed_directory_during_probe_is_refused(tmp_path, monkeypatch):
    identities = iter([(1, 1), (2, 1)])
    monkeypatch.setattr(volumes, 'directory_identity', lambda path: next(identities))
    monkeypatch.setattr(volumes.sys, 'platform', 'win32')
    monkeypatch.setattr(volumes, 'windows_volume', lambda path: {'kind': 'fixture', 'id': 'a'})
    with pytest.raises(StorageUnavailable, match='changed'):
        volumes.volume_identity(tmp_path)


def test_single_file_checkpoint_refuses_changed_volume(tmp_path):
    import hashlib
    from app.backup.media_copy import copy_media_file, InvalidCopy
    from tests.test_backup_media_tree import Guard
    source = tmp_path / 'photo.jpg'
    source.write_bytes(b'fixture picture')
    job = tmp_path / 'job'
    job.mkdir()
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    def interrupt(*args):
        raise OSError('disconnect')
    with pytest.raises(OSError, match='disconnect'):
        copy_media_file(source, job, digest, chunk_size=2, guard_factory=Guard,
                        progress=interrupt, volume_probe=lambda path: {'kind': 'fixture', 'id': 'original'})
    with pytest.raises(InvalidCopy, match='changed'):
        copy_media_file(source, job, digest, guard_factory=Guard,
                        volume_probe=lambda path: {'kind': 'fixture', 'id': 'replacement'})
    assert (job / 'payload.part').read_bytes() == b'fi'
    assert not (job / 'payload').exists()
