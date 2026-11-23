import io
import json
from types import SimpleNamespace

from cryptography.fernet import Fernet
import pytest

from app.backup.encrypted import (
    InvalidArtifact, encrypt_token, load_manifest, read_chunks, seal_stream, storage_budget,
    token_size, verify_artifact,
)
from app.backup.space import InsufficientSpace, SpaceGuard


def ample(requirements):
    return SpaceGuard(requirements, minimum_reserve=10, reserve_percent=0,
                      disk_usage=lambda path: SimpleNamespace(free=10 ** 9))


def seal(data, directory, key, **kwargs):
    return seal_stream(io.BytesIO(data), directory, key, estimated_bytes=len(data),
                       chunk_size=100, guard_factory=ample, **kwargs)


@pytest.mark.parametrize("size", [0, 1, 99, 100, 101, 4001])
def test_encrypted_roundtrip_is_bounded_and_no_plaintext(tmp_path, size):
    key = Fernet.generate_key()
    data = (b"private DB password and face data" * 200)[:size]
    directory = tmp_path / "artifact"
    manifest = seal(data, directory, key)
    assert manifest == verify_artifact(directory, key)
    assert b"".join(read_chunks(directory, key, manifest)) == data
    assert sum(path.stat().st_size for path in directory.iterdir()) <= storage_budget(size, 100)
    for path in directory.iterdir():
        assert b"private DB password" not in path.read_bytes()
        assert key not in path.read_bytes()


def test_wrong_key_rejected(tmp_path):
    directory = tmp_path / "artifact"
    seal(b"test", directory, Fernet.generate_key())
    with pytest.raises(InvalidArtifact, match="Wrong recovery key"):
        verify_artifact(directory, Fernet.generate_key())


def test_chunk_directory_synced_before_completion_publication(tmp_path, monkeypatch):
    from app.backup import encrypted
    directory = tmp_path / 'artifact'
    states = []
    def sync(path):
        assert path == directory
        assert (path / '00000000.fernet').is_file()
        states.append((path / 'complete.fernet').exists())
    monkeypatch.setattr(encrypted, 'sync_directory', sync)
    seal(b'fixture', directory, Fernet.generate_key())
    assert states == [False, True]


def test_failed_prepublication_directory_sync_never_marks_complete(tmp_path, monkeypatch):
    from app.backup import encrypted
    directory = tmp_path / 'artifact'
    def fail(path):
        raise OSError('fixture directory sync failure')
    monkeypatch.setattr(encrypted, 'sync_directory', fail)
    with pytest.raises(OSError, match='directory sync'):
        seal(b'fixture', directory, Fernet.generate_key())
    assert not (directory / 'complete.fernet').exists()


@pytest.mark.parametrize("damage", ["swap", "missing", "truncate", "corrupt", "other_artifact"])
def test_chunk_damage_is_rejected(tmp_path, damage):
    key = Fernet.generate_key()
    directory = tmp_path / "artifact"
    seal(b"secret" * 50, directory, key)
    first = directory / "00000000.fernet"
    if damage == "swap":
        second = directory / "00000001.fernet"
        a, b = first.read_bytes(), second.read_bytes()
        first.write_bytes(b)
        second.write_bytes(a)
    elif damage == "missing":
        first.unlink()
    elif damage == "truncate":
        first.write_bytes(first.read_bytes()[:10])
    elif damage == "corrupt":
        first.write_bytes(b"invalid" + first.read_bytes()[7:])
    else:
        other = tmp_path / "other"
        seal(b"different" * 50, other, key)
        first.write_bytes((other / first.name).read_bytes())
    with pytest.raises((InvalidArtifact, OSError)):
        verify_artifact(directory, key)


def test_missing_manifest_is_never_complete(tmp_path):
    key = Fernet.generate_key()
    directory = tmp_path / "artifact"
    def fail():
        raise RuntimeError("Producer failed after writing partial output")
    with pytest.raises(RuntimeError):
        seal(b"test", directory, key, finish=fail)
    assert list(directory.glob("000*.fernet"))
    assert not (directory / "complete.fernet").exists()
    with pytest.raises(OSError):
        verify_artifact(directory, key)


def test_interruption_does_not_publish_or_reuse_folder(tmp_path):
    key = Fernet.generate_key()
    directory = tmp_path / "artifact"
    def stop(size):
        raise InterruptedError()
    with pytest.raises(InterruptedError):
        seal(b"x" * 200, directory, key, progress=stop)
    with pytest.raises(FileExistsError):
        seal(b"x" * 200, directory, key)
    assert not (directory / "complete.fernet").exists()


def test_low_space_blocks_before_directory_creation(tmp_path):
    def low(requirements):
        return SpaceGuard(requirements, disk_usage=lambda path: SimpleNamespace(free=0))
    directory = tmp_path / "artifact"
    with pytest.raises(InsufficientSpace):
        seal_stream(io.BytesIO(b"secret"), directory, Fernet.generate_key(), estimated_bytes=6, guard_factory=low)
    assert not directory.exists()


def test_short_pipe_reads_are_combined(tmp_path):
    class Pipe(io.BytesIO):
        def read(self, size):
            return super().read(min(3, size))
    key = Fernet.generate_key()
    directory = tmp_path / "artifact"
    seal_stream(Pipe(b"secret" * 100), directory, key, estimated_bytes=600,
                chunk_size=100, guard_factory=ample)
    manifest = verify_artifact(directory, key)
    assert manifest["count"] == 6
    assert b"".join(read_chunks(directory, key, manifest)) == b"secret" * 100


@pytest.mark.parametrize("size", [0, 1, 15, 16, 17, 1000, 4096])
def test_budget_matches_standard_fernet_tokens(size):
    assert len(encrypt_token(Fernet(Fernet.generate_key()), b"x" * size)) == token_size(size)


def test_manifest_traversal_and_oversized_values_rejected(tmp_path):
    key = Fernet.generate_key()
    directory = tmp_path / "artifact"
    manifest = seal(b"test", directory, key)
    manifest["id"] = "../another"
    (directory / "complete.fernet").write_bytes(encrypt_token(Fernet(key), json.dumps(manifest).encode()))
    with pytest.raises(InvalidArtifact):
        load_manifest(directory, key)


def test_corrupt_whole_stream_hash_rejected(tmp_path):
    key = Fernet.generate_key()
    directory = tmp_path / "artifact"
    manifest = seal(b"test", directory, key)
    manifest["sha256"] = "0" * 64
    (directory / "complete.fernet").write_bytes(encrypt_token(Fernet(key), json.dumps(manifest).encode()))
    with pytest.raises(InvalidArtifact, match="stream verification"):
        verify_artifact(directory, key)


def password_set(tmp_path, password="test administrator password"):
    """An artifact inside a recovery set with a password context, as a backup holds them."""
    from app.backup import passwords
    key = passwords.create_context(tmp_path, passwords.BackupPassword(password))
    directory = tmp_path / "catalog" / "artifact"
    directory.parent.mkdir()
    seal(b"test", directory, key)
    return directory


def test_verify_cli_does_not_claim_restore_or_full_backup(tmp_path, monkeypatch, capsys):
    from app.backup.verify import main
    directory = password_set(tmp_path)
    monkeypatch.setattr("app.backup.passwords.from_arguments", lambda args: "test administrator password")
    assert main([str(directory)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "verified"
    assert result["restore_tested"] is False
    assert result["complete_library_backup"] is False


def test_verify_cli_wrong_password_is_redacted(tmp_path, monkeypatch, capsys):
    from app.backup.verify import main
    directory = password_set(tmp_path)
    monkeypatch.setattr("app.backup.passwords.from_arguments", lambda args: "PRIVATE_WRONG_PASSWORD")
    assert main([str(directory)]) == 1
    output = capsys.readouterr().out
    assert "PRIVATE_WRONG_PASSWORD" not in output and json.loads(output)["reason"] == "wrong-password"
