from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.backup import library
from app.backup.encrypted import InvalidArtifact
from app.fastbackup import capture
from app.fastbackup.restore import verify
from tests.backup_keys import backup_key
from tests.test_backup_capture_retry import fake_components, ample


@pytest.fixture
def world(tmp_path, monkeypatch):
    config, data, media = [tmp_path / name for name in ('config', 'data', 'media')]
    for path in (config, data, media):
        path.mkdir()
    (media / 'image.jpg').write_bytes(b'image')
    @contextmanager
    def frozen(endpoint):
        yield object()
    monkeypatch.setattr(library, 'freeze_database', frozen)
    monkeypatch.setattr(library, 'capture_components', fake_components)
    monkeypatch.setattr(library, 'reconcile_uploads', lambda *args: None)
    monkeypatch.setattr(library, 'reconcile_media', lambda *args: {})
    monkeypatch.setattr(library, 'state_inventory', lambda *args: [])
    monkeypatch.setattr(library, 'catalog_digests', lambda *args, **kwargs: None)
    return SimpleNamespace(config=config, data=data, media=media, backup=tmp_path / 'backup', key=backup_key())


def run(world, **kwargs):
    result = capture.capture_library(object(), None, world.config, world.data, world.media, world.backup,
        world.key, database_budget=1024, offline_confirmed=True, guard_factory=ample, **kwargs)
    return result


def test_complete_encrypted_catalog_and_full_media_compatible_with_existing_reader(world):
    result = run(world)
    assert verify(world.backup, world.key) == result
    assert result['format'] == library.FORMAT
    assert 'media_read_back' not in result
    assert result['media_files'] == 1
    assert not result['activation_allowed']
    with pytest.raises(FileExistsError):
        run(world)


@pytest.mark.parametrize('reader', [library.verify_library, library.verify_library_preflight,
                                  library.verify_library_metadata])
def test_unknown_library_format_is_rejected_before_media_access(tmp_path, monkeypatch, reader):
    monkeypatch.setattr(library, 'verify_artifact', lambda *args: {
        'metadata': {'kind': 'unsupported-library-format'}, 'size': 1})
    monkeypatch.setattr(library, 'verify_media_tree', lambda *args: pytest.fail('Unknown index must be rejected first'))
    with pytest.raises(InvalidArtifact, match='Unsupported library recovery index'):
        reader(tmp_path, backup_key())


def test_media_failure_never_publishes_library(world, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError('copy interrupted')
    monkeypatch.setattr(capture, 'copy_in_worker', fail)
    with pytest.raises(OSError):
        run(world)
    assert not (world.backup / 'library').exists()
    assert (world.data / '.recovery-admission/maintenance.json').exists()


def test_unverified_copy_cannot_seal_library(world, monkeypatch):
    monkeypatch.setattr(capture, 'copy_in_worker', lambda *a, **k: dict(content_verified=False))
    with pytest.raises(ValueError, match='full destination'):
        run(world)
    assert not (world.backup / 'library').exists()


def test_tampered_media_is_rejected_by_original_reader(world):
    run(world)
    (world.backup / 'media-copy/media/image.jpg').write_bytes(b'wrong')
    with pytest.raises(ValueError):
        verify(world.backup, world.key)


def test_wrong_password_context_rejected(world):
    run(world)
    with pytest.raises((InvalidArtifact, ValueError)):
        verify(world.backup, backup_key())
