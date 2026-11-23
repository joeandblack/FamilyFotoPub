import hashlib
import json
import os
from pathlib import Path
import uuid

import pytest

from app.backup.restic import Restic, ResticFailed, identifier, snapshot_root
from tests.test_backup_media_tree import Guard


@pytest.fixture
def engine(tmp_path):
    executable = os.environ.get('FAMILYFOTO_BACKUP_RESTIC')
    if not executable:
        pytest.skip('Explicit checksum-pinned restic test executable required')
    digest = os.environ['FAMILYFOTO_BACKUP_RESTIC_SHA256']
    secret = tmp_path / 'repository-password'
    secret.write_text('synthetic-test-password-' + uuid.uuid4().hex)
    return Restic(executable, digest, tmp_path / 'repository', secret)


def test_encrypted_incremental_snapshots_restore_without_source(engine, tmp_path):
    repository = engine.initialize()
    source = tmp_path / 'media'
    source.mkdir()
    marker = b'private synthetic photo content\x00' * 1000
    (source / 'photo.jpg').write_bytes(marker)
    first = engine.backup(source, repository, uuid.uuid4().hex, estimated_bytes=1024 ** 2, guard_factory=Guard)
    assert first['repository_read_verified'] and not first['familyfoto_recovery_point']
    second = engine.backup(source, repository, uuid.uuid4().hex, estimated_bytes=1024 ** 2, guard_factory=Guard)
    assert first['snapshot_id'] != second['snapshot_id']
    assert first['data_added'] > 0 and second['data_added'] == 0
    snapshots = json.loads(engine.run(['snapshots', '--json']))
    assert {item['id'] for item in snapshots} == {first['snapshot_id'], second['snapshot_id']}
    # No cleartext content may appear anywhere in the encrypted repository.
    for path in engine.repository.rglob('*'):
        if path.is_file():
            assert marker not in path.read_bytes()
    source.rename(tmp_path / 'disconnected-source')
    target = tmp_path / 'restored'
    result = engine.restore(first['snapshot_id'], repository, target,
                            required_bytes=1024 ** 2, guard_factory=Guard)
    assert result['restored'] and not result['activation_allowed']
    assert not (target / 'RESTIC-RESTORE-INCOMPLETE').exists()
    assert [path.read_bytes() for path in target.rglob('photo.jpg')] == [marker]
    with pytest.raises(FileExistsError):
        engine.restore(first['snapshot_id'], repository, target,
                       required_bytes=1024 ** 2, guard_factory=Guard)


def test_wrong_password_and_repository_identity_never_restore(engine, tmp_path):
    repository = engine.initialize()
    with pytest.raises(ResticFailed, match='identity'):
        engine.restore('a' * 64, 'b' * 64, tmp_path / 'refused', required_bytes=1, guard_factory=Guard)
    assert not (tmp_path / 'refused').exists()
    engine.password_file.write_text('wrong-password')
    with pytest.raises(ResticFailed, match='failed'):
        engine.repository_id()
    assert len(repository) == 64


def test_corrupt_encrypted_pack_fails_full_read_verification(engine, tmp_path):
    repository = engine.initialize()
    source = tmp_path / 'media'
    source.mkdir()
    (source / 'photo.jpg').write_bytes(b'synthetic photo' * 100)
    engine.backup(source, repository, uuid.uuid4().hex, estimated_bytes=1024 ** 2, guard_factory=Guard)
    pack = next(path for path in (engine.repository / 'data').rglob('*') if path.is_file())
    pack.chmod(0o600)  # restic packs are read-only on POSIX; this fixture deliberately corrupts one.
    with pack.open('r+b') as output:
        first = output.read(1)
        output.seek(0)
        output.write(bytes([first[0] ^ 1]))
    with pytest.raises(ResticFailed, match='failed'):
        engine.check(repository)


def test_refuses_unpinned_executable_and_inherited_password_commands(tmp_path, monkeypatch):
    executable = tmp_path / 'restic'
    executable.write_bytes(b'not executable')
    password = tmp_path / 'password'
    password.write_text('secret')
    with pytest.raises(ValueError, match='checksum'):
        Restic(executable, 'a' * 64, tmp_path / 'repo', password)
    engine = Restic(executable, hashlib.sha256(executable.read_bytes()).hexdigest(), tmp_path / 'repo', password)
    monkeypatch.setenv('RESTIC_PASSWORD_COMMAND', 'do not execute')
    monkeypatch.setenv('RESTIC_REPOSITORY', 'remote:never')
    monkeypatch.setenv('RESTIC_PASSWORD', 'not-used')
    environment = engine.environment()
    assert 'RESTIC_PASSWORD_COMMAND' not in environment
    assert 'RESTIC_REPOSITORY' not in environment
    assert 'RESTIC_PASSWORD' not in environment
    assert environment['RESTIC_PASSWORD_FILE'] == str(password)


@pytest.mark.parametrize('value', ['latest', 'a123', '-option', '../repo', None])
def test_only_exact_ids_are_accepted(value):
    with pytest.raises(ValueError):
        identifier(value)


def test_snapshot_root_is_portable_without_guessing_current_platform():
    assert snapshot_root(r'D:\Photos\family') == '/D/Photos/family'
    assert snapshot_root('/Volumes/Photos/family') == '/Volumes/Photos/family'
    for path in ('../escape', '/a/../b', r'\\server\share', '/a:b', None):
        with pytest.raises(ValueError):
            snapshot_root(path)


def fake_engine(tmp_path):
    executable = tmp_path / 'restic'
    executable.write_bytes(b'fixture executable')
    password = tmp_path / 'password'
    password.write_text('fixture password')
    engine = Restic(executable, hashlib.sha256(executable.read_bytes()).hexdigest(), tmp_path / 'repo', password)
    engine.repository.mkdir()
    return engine


def test_partial_backup_exit_never_becomes_verified(tmp_path, monkeypatch):
    engine = fake_engine(tmp_path)
    source = tmp_path / 'media'
    source.mkdir()
    monkeypatch.setattr(engine, 'repository_id', lambda: 'a' * 64)
    def partial(args, **kwargs):
        kwargs['handler'](json.dumps({'message_type': 'summary', 'snapshot_id': 'b' * 64}).encode())
        # restic may publish a snapshot despite skipped/unreadable source files.
        raise ResticFailed('Backup engine failed (exit 3)')
    monkeypatch.setattr(engine, 'run', partial)
    with pytest.raises(ResticFailed, match='exit 3'):
        engine.backup(source, 'a' * 64, 'c' * 32, estimated_bytes=1, guard_factory=Guard)


def test_restore_failure_retains_incomplete_marker(tmp_path, monkeypatch):
    engine = fake_engine(tmp_path)
    monkeypatch.setattr(engine, 'repository_id', lambda: 'a' * 64)
    def fail(*args, **kwargs):
        raise OSError('disk disconnected')
    monkeypatch.setattr(engine, 'run', fail)
    with pytest.raises(OSError, match='disconnected'):
        engine.restore('b' * 64, 'a' * 64, tmp_path / 'restore', required_bytes=1, guard_factory=Guard)
    assert (tmp_path / 'restore/RESTIC-RESTORE-INCOMPLETE').exists()


def test_progress_does_not_expose_private_filenames(tmp_path, monkeypatch):
    engine = fake_engine(tmp_path)
    source = tmp_path / 'media'
    source.mkdir()
    monkeypatch.setattr(engine, 'repository_id', lambda: 'a' * 64)
    monkeypatch.setattr(engine, 'check', lambda identity: None)
    def run(args, **kwargs):
        kwargs['handler'](json.dumps({'message_type': 'status', 'files_done': 1,
                                     'current_files': ['private-name.jpg']}).encode())
        kwargs['handler'](json.dumps({'message_type': 'summary', 'snapshot_id': 'b' * 64}).encode())
    monkeypatch.setattr(engine, 'run', run)
    events = []
    engine.backup(source, 'a' * 64, 'c' * 32, estimated_bytes=1, guard_factory=Guard, progress=events.append)
    assert events == [{'files_done': 1}]
