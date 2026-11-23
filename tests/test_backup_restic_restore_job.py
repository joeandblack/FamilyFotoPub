import json
import uuid
from types import SimpleNamespace

import pytest

from app.backup.restic import ResticFailed
from app.backup.restic_restore_job import restore_snapshot_job
from app.backup import restic_restore_job as module
from app.backup.media_copy import copy_lock
from tests.test_backup_restic import engine  # noqa: F401
from tests.test_backup_media_tree import Guard


@pytest.fixture
def unfinished(tmp_path):
    def interrupted(*args, **kwargs):
        raise OSError('Interrupted fixture')

    engine = SimpleNamespace(repository=tmp_path / 'repo', password_file=tmp_path / 'key',
                             snapshot=lambda snapshot: {'restore_root': '/source'},
                             check=lambda repository: None, run=interrupted)
    job = tmp_path / 'job'
    with pytest.raises(OSError, match='Interrupted'):
        restore_snapshot_job(engine, 'a' * 64, 'b' * 64, job, required_bytes=1024, guard_factory=Guard)
    return engine, job


@pytest.mark.parametrize('field', ['snapshot', 'repository', 'selection', 'volume', 'directory'])
def test_retry_refuses_changed_identity(unfinished, field):
    engine, job = unfinished
    path = job / 'job.json'
    record = json.loads(path.read_text())
    record[field] = 'changed'
    path.write_text(json.dumps(record))
    with pytest.raises(ResticFailed, match='identity'):
        restore_snapshot_job(engine, 'a' * 64, 'b' * 64, job,
                             required_bytes=1024, resume=True, guard_factory=Guard)


def test_retry_refuses_replaced_volume(unfinished, monkeypatch):
    engine, job = unfinished
    monkeypatch.setattr(module, 'volume_identity', lambda path: {'volume': 'replacement'})
    with pytest.raises(ResticFailed, match='identity'):
        restore_snapshot_job(engine, 'a' * 64, 'b' * 64, job,
                             required_bytes=1024, resume=True, guard_factory=Guard)


def test_retry_refuses_concurrent_owner(unfinished):
    engine, job = unfinished
    with copy_lock(job), pytest.raises(OSError):
        restore_snapshot_job(engine, 'a' * 64, 'b' * 64, job,
                             required_bytes=1024, resume=True, guard_factory=Guard)


def test_retry_refuses_hardlinked_payload(unfinished, tmp_path):
    engine, job = unfinished
    outside = tmp_path / 'unrelated'
    outside.write_bytes(b'must stay unchanged')
    (job / 'payload/photo').hardlink_to(outside)
    with pytest.raises(ResticFailed, match='linked'):
        restore_snapshot_job(engine, 'a' * 64, 'b' * 64, job,
                             required_bytes=1024, resume=True, guard_factory=Guard)
    assert outside.read_bytes() == b'must stay unchanged'


def test_output_walk_permission_error_is_not_silently_skipped(tmp_path, monkeypatch):
    def inaccessible(root, **kwargs):
        kwargs['onerror'](PermissionError('Unreadable restored folder'))
        return iter(())

    monkeypatch.setattr(module.os, 'walk', inaccessible)
    with pytest.raises(PermissionError, match='Unreadable'):
        module.regular_tree(tmp_path)


def test_both_markers_require_successful_reverification(unfinished):
    engine, job = unfinished
    (job / 'VERIFIED').touch()
    with pytest.raises(OSError, match='Interrupted'):
        restore_snapshot_job(engine, 'a' * 64, 'b' * 64, job, required_bytes=1024,
                             resume=True, reuse_verified=True, guard_factory=Guard)
    assert (job / 'INCOMPLETE').exists()
    calls = []
    engine.run = lambda arguments, **kwargs: calls.append(arguments)
    restore_snapshot_job(engine, 'a' * 64, 'b' * 64, job, required_bytes=1024,
                         resume=True, reuse_verified=True, guard_factory=Guard)
    assert len(calls) == 1 and '--verify' in calls[0]
    assert (job / 'VERIFIED').exists() and not (job / 'INCOMPLETE').exists()


@pytest.mark.parametrize('name', ['job.json', 'INCOMPLETE', 'VERIFIED'])
def test_retry_refuses_hardlinked_control_files(unfinished, tmp_path, name):
    engine, job = unfinished
    path = job / name
    if not path.exists():
        path.touch()
    outside = tmp_path / 'outside-control'
    outside.hardlink_to(path)
    with pytest.raises(ResticFailed, match='control file'):
        restore_snapshot_job(engine, 'a' * 64, 'b' * 64, job,
                             required_bytes=1024, resume=True, guard_factory=Guard)


def test_retry_refuses_nonempty_completion_marker(unfinished):
    engine, job = unfinished
    (job / 'VERIFIED').write_text('unexpected')
    with pytest.raises(ResticFailed, match='control file'):
        restore_snapshot_job(engine, 'a' * 64, 'b' * 64, job,
                             required_bytes=1024, resume=True, guard_factory=Guard)


def test_interrupted_encrypted_restore_repairs_only_owned_payload(engine, tmp_path, monkeypatch):
    repository = engine.initialize()
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'photo.jpg').write_bytes(b'fixture photo')
    snapshot = engine.backup(source, repository, uuid.uuid4().hex,
                             estimated_bytes=1024, guard_factory=Guard)['snapshot_id']
    job = tmp_path / 'job'
    run = engine.run

    def interrupt(arguments, **kwargs):
        if arguments[0] == 'restore':
            (job / 'payload/photo.jpg').write_bytes(b'partial')
            raise OSError('Synthetic interruption')
        return run(arguments, **kwargs)

    monkeypatch.setattr(engine, 'run', interrupt)
    with pytest.raises(OSError, match='interruption'):
        restore_snapshot_job(engine, snapshot, repository, job, required_bytes=1024, guard_factory=Guard)
    assert (job / 'INCOMPLETE').exists()
    assert not (job / 'VERIFIED').exists()
    with pytest.raises(FileExistsError):
        restore_snapshot_job(engine, snapshot, repository, job, required_bytes=1024, guard_factory=Guard)
    with pytest.raises(ResticFailed, match='identity'):
        restore_snapshot_job(engine, snapshot, 'b' * 64, job, required_bytes=1024, resume=True, guard_factory=Guard)
    record = json.loads((job / 'job.json').read_text())
    assert record['snapshot'] == snapshot
    source.rename(tmp_path / 'disconnected')
    monkeypatch.setattr(engine, 'run', run)
    payload = restore_snapshot_job(engine, snapshot, repository, job,
                                   required_bytes=1024, resume=True, guard_factory=Guard)
    assert (payload / 'photo.jpg').read_bytes() == b'fixture photo'
    assert (job / 'VERIFIED').exists() and not (job / 'INCOMPLETE').exists()
    with pytest.raises(ResticFailed, match='unfinished'):
        restore_snapshot_job(engine, snapshot, repository, job,
                             required_bytes=1024, resume=True, guard_factory=Guard)
