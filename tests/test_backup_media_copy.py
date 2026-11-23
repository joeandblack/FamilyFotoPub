import errno
import hashlib
import json
import os
from types import SimpleNamespace

import pytest

from app.backup import media_copy
from app.backup.media_copy import InvalidCopy, copy_media_file
from app.backup.space import InsufficientSpace, SpaceGuard


@pytest.fixture
def media(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"synthetic media" * 100)
    destination = tmp_path / "copy-job"
    destination.mkdir()
    return source, destination, hashlib.sha256(source.read_bytes()).hexdigest()


def ample_guard(requirements):
    return SpaceGuard(requirements, minimum_reserve=10, reserve_percent=0,
                      disk_usage=lambda path: SimpleNamespace(free=10 ** 9))


def run(media, **options):
    return copy_media_file(*media, chunk_size=100, guard_factory=ample_guard, **options)


def test_a_byte_damaged_on_the_way_to_disk_is_refused(media, monkeypatch):
    real = os.fsync

    def damage(descriptor):
        part = media[1] / "payload.part"
        if part.exists():
            with part.open("r+b") as output:
                output.write(b"Z")
        return real(descriptor)
    monkeypatch.setattr(media_copy.os, "fsync", damage)
    with pytest.raises(InvalidCopy, match="read-back"):
        copy_media_file(*media, chunk_size=media[0].stat().st_size, guard_factory=ample_guard)
    assert not (media[1] / "payload").exists()


def test_copy_verifies_before_publish(media):
    notifications = []
    result = run(media, progress=lambda copied, total: notifications.append(copied))
    assert result.read_bytes() == media[0].read_bytes()
    assert notifications[-1] == media[0].stat().st_size
    assert not (media[1] / "payload.part").exists()
    assert run(media) == result


def test_interrupted_copy_resumes_from_verified_checkpoint(media):
    def interrupt(copied, total):
        if copied == 200:
            raise InterruptedError("Simulated shutdown")
    with pytest.raises(InterruptedError):
        run(media, progress=interrupt)
    assert not (media[1] / "payload").exists()
    assert json.loads((media[1] / "copy.json").read_text())["offset"] == 200
    progress = []
    result = run(media, progress=lambda copied, total: progress.append(copied))
    assert progress[0] == 300
    assert result.read_bytes() == media[0].read_bytes()


def test_uncheckpointed_tail_is_discarded(media):
    def interrupt(*args):
        raise InterruptedError()
    with pytest.raises(InterruptedError):
        run(media, progress=interrupt)
    with (media[1] / "payload.part").open("ab") as handle:
        handle.write(b"uncheckpointed garbage")
    assert run(media).read_bytes() == media[0].read_bytes()


def test_bad_verified_prefix_blocks_resume(media):
    def interrupt(*args):
        raise InterruptedError()
    with pytest.raises(InterruptedError):
        run(media, progress=interrupt)
    with (media[1] / "payload.part").open("r+b") as handle:
        handle.write(b"corrupt")
    with pytest.raises(InvalidCopy, match="prefix failed"):
        run(media)
    assert not (media[1] / "payload").exists()


def test_changed_source_blocks_resume(media):
    def interrupt(*args):
        raise InterruptedError()
    with pytest.raises(InterruptedError):
        run(media, progress=interrupt)
    media[0].write_bytes(b"replaced")
    with pytest.raises(InvalidCopy, match="source or destination changed"):
        run(media)


def test_wrong_expected_hash_never_publishes(media):
    with pytest.raises(InvalidCopy, match="expected SHA"):
        run((*media[:2], "0" * 64))
    assert not (media[1] / "payload").exists()


def test_disk_pressure_preserves_checkpoint(media):
    free = [10 ** 9]
    def factory(requirements):
        return SpaceGuard(requirements, minimum_reserve=10, reserve_percent=0,
                          disk_usage=lambda path: SimpleNamespace(free=free[0]))
    def pressure(*args):
        free[0] = 1
    with pytest.raises(InsufficientSpace):
        copy_media_file(*media, chunk_size=100, guard_factory=factory, progress=pressure)
    assert json.loads((media[1] / "copy.json").read_text())["offset"] == 100
    assert not (media[1] / "payload").exists()
    assert run(media).read_bytes() == media[0].read_bytes()


def test_enospc_at_checkpoint_is_recoverable(media, monkeypatch):
    real = media_copy.save_checkpoint
    def fail(directory, record):
        if record["offset"] == 100:
            raise OSError(errno.ENOSPC, "Simulated full disk after write")
        real(directory, record)
    monkeypatch.setattr(media_copy, "save_checkpoint", fail)
    with pytest.raises(OSError) as error:
        run(media)
    assert error.value.errno == errno.ENOSPC
    assert not (media[1] / "payload").exists()
    monkeypatch.setattr(media_copy, "save_checkpoint", real)
    assert run(media).read_bytes() == media[0].read_bytes()


def test_existing_output_never_overwritten(media):
    (media[1] / "payload").write_bytes(b"previous backup")
    with pytest.raises(InvalidCopy, match="Unowned"):
        run(media)
    assert (media[1] / "payload").read_bytes() == b"previous backup"


def test_publication_crash_recovery(media, monkeypatch):
    real = media_copy.sync_directory
    def fail(directory):
        real(directory)
        if (directory / "payload").exists():
            raise InterruptedError()
    monkeypatch.setattr(media_copy, "sync_directory", fail)
    with pytest.raises(InterruptedError):
        run(media)
    assert (media[1] / "payload").exists()
    monkeypatch.setattr(media_copy, "sync_directory", real)
    assert run(media).read_bytes() == media[0].read_bytes()


def test_a_file_of_one_block_is_flushed_only_for_its_start_and_its_data(media, monkeypatch):
    """Large libraries are mostly such files, so anything more here multiplies by every file."""
    media[0].write_bytes(b"x" * 100)
    flushed = []
    real = os.fsync
    monkeypatch.setattr(media_copy.os, "fsync", lambda descriptor: (flushed.append(descriptor), real(descriptor))[1])
    monkeypatch.setattr(media_copy, "sync_directory", lambda directory: None)
    copy_media_file(media[0], media[1], hashlib.sha256(b"x" * 100).hexdigest(), chunk_size=100, guard_factory=ample_guard)
    assert len(flushed) == 2


def test_each_block_but_the_last_is_still_checkpointed(media, monkeypatch):
    offsets = []
    real = media_copy.save_checkpoint
    monkeypatch.setattr(media_copy, "save_checkpoint",
                        lambda directory, record: (offsets.append(record["offset"]), real(directory, record))[1])
    media[0].write_bytes(b"x" * 300)
    copy_media_file(media[0], media[1], hashlib.sha256(b"x" * 300).hexdigest(), chunk_size=100, guard_factory=ample_guard)
    assert offsets == [0, 100, 200]


def test_a_crash_after_the_last_block_copies_it_again(media, monkeypatch):
    media[0].write_bytes(b"x" * 300)
    digest = hashlib.sha256(b"x" * 300).hexdigest()
    def stop(copied, total):
        if copied == 300:
            raise InterruptedError()
    with pytest.raises(InterruptedError):
        copy_media_file(media[0], media[1], digest, chunk_size=100, guard_factory=ample_guard, progress=stop)
    assert json.loads((media[1] / "copy.json").read_text())["offset"] == 200
    progress = []
    assert copy_media_file(media[0], media[1], digest, chunk_size=100, guard_factory=ample_guard,
                           progress=lambda copied, total: progress.append(copied)).read_bytes() == b"x" * 300
    assert progress == [300]


def test_empty_media(media):
    media[0].write_bytes(b"")
    assert run((*media[:2], hashlib.sha256().hexdigest())).stat().st_size == 0


def test_concurrent_writer_rejected(media):
    with media_copy.copy_lock(media[1]):
        with pytest.raises(OSError):
            run(media)


def test_process_exit_releases_lock_and_resumes(media):
    import subprocess
    import sys
    from pathlib import Path

    code = """
import os, sys
from types import SimpleNamespace
from app.backup.media_copy import copy_media_file
from app.backup.space import SpaceGuard
def factory(requirements):
    return SpaceGuard(requirements, minimum_reserve=10, reserve_percent=0,
                      disk_usage=lambda path: SimpleNamespace(free=10**9))
def stop(copied, total):
    if copied == 200:
        os._exit(23)
copy_media_file(*sys.argv[1:], chunk_size=100, guard_factory=factory, progress=stop)
"""
    child = subprocess.run([sys.executable, "-c", code, *map(str, media)],
                           cwd=Path(__file__).resolve().parents[1], timeout=15, capture_output=True)
    assert child.returncode == 23, child.stderr
    assert not (media[1] / "payload").exists()
    progress = []
    assert run(media, progress=lambda copied, total: progress.append(copied)).read_bytes() == media[0].read_bytes()
    assert progress[0] == 300


def test_changed_destination_record_blocks_resume(media):
    def interrupt(*args):
        raise InterruptedError()
    with pytest.raises(InterruptedError):
        run(media, progress=interrupt)
    checkpoint = media[1] / "copy.json"
    record = json.loads(checkpoint.read_text())
    # A device number alone can change on remount; the volume identity guards that case.
    record["directory"][1] += 1
    checkpoint.write_text(json.dumps(record))
    with pytest.raises(InvalidCopy, match="destination changed"):
        run(media)


def test_published_file_corruption_is_detected(media):
    result = run(media)
    with result.open("r+b") as output:
        output.write(b"corruption")
    with pytest.raises(InvalidCopy, match="Published copy failed"):
        run(media)
