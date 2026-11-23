import io
import os
import tarfile
from types import SimpleNamespace

from cryptography.fernet import Fernet
import pytest

from app.backup import state_files as state
from app.backup.encrypted import InvalidArtifact, seal_stream
from app.backup.space import InsufficientSpace, SpaceGuard


def ample(requirements):
    return SpaceGuard(requirements, minimum_reserve=10, reserve_percent=0,
                      disk_usage=lambda path: SimpleNamespace(free=10 ** 9))


@pytest.fixture
def roots(tmp_path):
    result = {name: tmp_path / name for name in ("config", "data")}
    for path in result.values():
        path.mkdir()
    (result["config"] / "local.json").write_bytes(b'{"password":"secret-fixture-only"}')
    (result["data"] / "people-preview.json").write_bytes(b'{"people":[]}')
    (result["data"] / "foreground-activity").write_bytes(b'{"running":true}')
    return result


def capture(roots, target, key, **kwargs):
    return state.capture_state(roots["config"], roots["data"], target, key, guard_factory=ample, **kwargs)


def test_worker_roundtrip_private_state_and_no_control_replay(tmp_path, roots):
    tls = roots["config"] / "tls"
    tls.mkdir()
    for name in ("ca.pem", "ca-key.pem", "server.pem", "server-key.pem"):
        (tls / name).write_bytes(b"synthetic-certificate-fixture")
    (roots['data'] / 'PAUSE').touch()
    (roots['data'] / 'monitor.json').write_text('{"state":"healthy"}')
    preview = roots['data'] / 'previews/aa/bb/fixture.jpg'
    preview.parent.mkdir(parents=True)
    preview.write_bytes(b'cache')
    key = Fernet.generate_key()
    artifact = tmp_path / "artifact"
    result = capture(roots, artifact, key)
    assert result["metadata"]["file_count"] == 6
    assert result["metadata"]["requires_write_admission"]
    for path in artifact.iterdir():
        assert b"secret-fixture-only" not in path.read_bytes()
    target = tmp_path / "restore"
    report = state.restore_state(artifact, key, target, guard_factory=ample)
    assert not report["activation_allowed"]
    assert not (target / "RESTORE-INCOMPLETE").exists()
    assert not (target / "data/foreground-activity").exists()
    assert not (target / 'data/PAUSE').exists()
    assert not (target / 'data/monitor.json').exists()
    assert not (target / 'data/previews').exists()
    for record in state.state_inventory(roots):
        relative = record["path"]
        assert (target / record["root"] / relative).read_bytes() == (roots[record["root"]] / relative).read_bytes()
    if os.name != "nt":
        assert target.stat().st_mode & 0o777 == 0o700
        assert (target / "config/local.json").stat().st_mode & 0o777 == 0o600


def test_capture_changed_inventory_rejected(roots):
    records = state.state_inventory(roots)
    (roots["config"] / "local.json").write_bytes(b"changed")
    with pytest.raises(state.InvalidState, match="changed"):
        state.write_state_tar(roots, records, io.BytesIO())


def test_calibration_preferences_and_original_retention_roundtrip(tmp_path, roots):
    profile = 'face_detect/profiles/' + 'a' * 32 + '/calibration/'
    names = ['face-service-settings.json', 'conversion-recovery/23/retain-original.json',
             'avi-job.json', 'avi-status.json', 'conversion-temp/23/input.mts',
             'conversion-temp/23/output.mp4', 'conversion-temp/23/mts-plan.json',
             'conversion-temp/23/frame-validation.json', 'conversion-temp/23/previous-123.mp4',
             'uploads/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/0.jpg',
             profile + 'identity-equivalences.json', profile + 'latest-evaluation.json',
             profile + 'activation.json',
             profile + 'evaluation-20261102T120000Z-abcdef01/evaluation.json',
             profile + 'evaluation-20261102T120000Z-abcdef01/labeled-inputs.json']
    for name in names:
        path = roots['data'] / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'{"fixture":"sensitive-label-evidence"}')
    key = Fernet.generate_key()
    artifact = tmp_path / 'artifact'
    capture(roots, artifact, key)
    target = tmp_path / 'restored'
    state.restore_state(artifact, key, target, guard_factory=ample)
    for name in names:
        assert (target / 'data' / name).read_bytes() == (roots['data'] / name).read_bytes()
    for path in artifact.iterdir():
        assert b'sensitive-label-evidence' not in path.read_bytes()


def test_capture_detects_same_length_mutation_during_stream(roots):
    records = state.state_inventory(roots)
    path = roots["config"] / "local.json"
    before = path.stat()

    class MutatingOutput(io.BytesIO):
        def write(self, block):
            path.write_bytes(b"x" * before.st_size)
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            return super().write(block)

    with pytest.raises(state.InvalidState, match="content changed"):
        state.write_state_tar(roots, records, MutatingOutput())


def test_incomplete_certificate_pair_rejected(roots):
    tls = roots["config"] / "tls"
    tls.mkdir()
    (tls / "ca.pem").write_bytes(b"fixture")
    with pytest.raises(state.InvalidState, match="pair"):
        state.state_inventory(roots)


@pytest.mark.parametrize("name", ["../config/local.json", "config/../local.json", "config/local.json:stream",
                                  "config\\local.json", "/config/local.json", "config/NUL.json",
                                  "data/foreground-activity"])
def test_unsafe_members_rejected(name):
    with pytest.raises(ValueError):
        state.safe_member(name)


def make_archive(tmp_path, members):
    buffer = io.BytesIO()
    total = 0
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, data, kind in members:
            info = tarfile.TarInfo(name)
            info.type = kind
            info.size = len(data)
            total += len(data)
            archive.addfile(info, io.BytesIO(data))
    key = Fernet.generate_key()
    directory = tmp_path / "artifact"
    seal_stream(io.BytesIO(buffer.getvalue()), directory, key, estimated_bytes=len(buffer.getvalue()),
                guard_factory=ample, metadata={"kind": state.STATE_KIND, "file_count": len(members), "file_bytes": total})
    return directory, key


@pytest.mark.parametrize("members", [
    [("config/local.json", b"x", tarfile.REGTYPE), ("config/local.json", b"y", tarfile.REGTYPE)],
    [("config/local.json", b"", tarfile.SYMTYPE)],
    [("config/../local.json", b"x", tarfile.REGTYPE)],
    [("data/people-preview.json", b"x", tarfile.REGTYPE)],
])
def test_invalid_archive_never_creates_restore_target(tmp_path, members):
    artifact, key = make_archive(tmp_path, members)
    with pytest.raises(ValueError):
        state.restore_state(artifact, key, tmp_path / "restore", guard_factory=ample)
    assert not (tmp_path / "restore").exists()


def test_wrong_key_existing_target_and_disk_full(tmp_path, roots):
    key = Fernet.generate_key()
    artifact = tmp_path / "artifact"
    capture(roots, artifact, key)
    target = tmp_path / "restore"
    with pytest.raises(InvalidArtifact):
        state.restore_state(artifact, Fernet.generate_key(), target, guard_factory=ample)
    assert not target.exists()

    def low(requirements):
        return SpaceGuard(requirements, disk_usage=lambda path: SimpleNamespace(free=0))

    with pytest.raises(InsufficientSpace):
        state.restore_state(artifact, key, target, guard_factory=low)
    assert not target.exists()
    target.mkdir()
    (target / "keep").write_bytes(b"unchanged")
    with pytest.raises(FileExistsError):
        state.restore_state(artifact, key, target, guard_factory=ample)
    assert (target / "keep").read_bytes() == b"unchanged"


def test_write_failure_leaves_incomplete_marker(tmp_path, roots):
    artifact = tmp_path / "artifact"
    key = Fernet.generate_key()
    capture(roots, artifact, key)

    def failing(requirements):
        guard = ample(requirements)
        original = guard.check
        def check(**kwargs):
            if kwargs:
                raise OSError("Simulated disk disconnect")
            return original()
        guard.check = check
        return guard

    target = tmp_path / "restore"
    with pytest.raises(OSError, match="disconnect"):
        state.restore_state(artifact, key, target, guard_factory=failing)
    assert (target / "RESTORE-INCOMPLETE").exists()
    assert not (target / "RESTORE-STATE-ONLY.json").exists()


def test_capture_interruption_never_publishes_completion(tmp_path, roots):
    artifact = tmp_path / "artifact"
    def interrupt(size):
        raise InterruptedError("fixture stop")
    with pytest.raises(InterruptedError):
        capture(roots, artifact, Fernet.generate_key(), progress=interrupt)
    assert not (artifact / "complete.fernet").exists()
    assert list(artifact.glob("*.fernet"))


def test_permission_failure_writes_no_secrets(tmp_path, roots, monkeypatch):
    artifact = tmp_path / "artifact"
    key = Fernet.generate_key()
    capture(roots, artifact, key)
    target = tmp_path / "restore"
    def fail(path):
        path.mkdir()
        raise PermissionError("Fixture ACL failure")
    monkeypatch.setattr(state, "create_private_directory", fail)
    with pytest.raises(PermissionError):
        state.restore_state(artifact, key, target, guard_factory=ample)
    assert list(target.iterdir()) == []


def test_capture_preflight_failure_does_not_start_worker(tmp_path, roots, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("worker started before capacity preflight")
    monkeypatch.setattr(state.subprocess, "Popen", unexpected)
    def low(requirements):
        return SpaceGuard(requirements, disk_usage=lambda path: SimpleNamespace(free=0))
    target = tmp_path / "artifact"
    with pytest.raises(InsufficientSpace):
        state.capture_state(roots["config"], roots["data"], target, Fernet.generate_key(), guard_factory=low)
    assert not target.exists()
