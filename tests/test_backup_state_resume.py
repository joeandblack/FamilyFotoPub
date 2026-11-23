import io
import os

from cryptography.fernet import Fernet
import pytest

from app.backup import resumable_state as resume
from app.backup.encrypted import InvalidArtifact, read_chunks, verify_artifact
from app.backup.library import set_aside_unfinished
from app.backup.space import InsufficientSpace
from tests.test_backup_state_files import ample, roots, capture
from tests.test_backup_capture_retry import world, acknowledge


def seal(target, key, data, **kwargs):
    return resume.seal_state_stream(io.BytesIO(data), target, key, estimated_bytes=len(data),
                                    guard_factory=ample, finish=lambda: None, **kwargs)


def stop(size):
    raise InterruptedError('simulated shutdown')


def test_resume_reuses_authenticated_blocks_and_restores(tmp_path, roots):
    from app.backup.state_files import restore_state
    data = roots['data'] / 'conversion-temp/1/input.mts'
    data.parent.mkdir(parents=True)
    data.write_bytes(b'x' * (resume.CHUNK_SIZE * 2))
    target, key = tmp_path / 'state', Fernet.generate_key()
    with pytest.raises(InterruptedError):
        capture(roots, target, key, progress=stop)
    chunk = target / '00000000.fernet'
    saved, timestamp = chunk.read_bytes(), chunk.stat().st_mtime_ns
    progress = []
    result = capture(roots, target, key, resume=True, progress=progress.append)
    assert chunk.read_bytes() == saved and chunk.stat().st_mtime_ns == timestamp
    assert progress == sorted(progress) and progress[-1] == result['size']
    restored = tmp_path / 'restored'
    restore_state(target, key, restored, guard_factory=ample)
    assert (restored / 'data/conversion-temp/1/input.mts').read_bytes() == data.read_bytes()


@pytest.mark.parametrize('damage', ['truncated', 'wrong-position', 'missing', 'partial-write'])
def test_interrupted_or_corrupt_chunk_regenerated(tmp_path, damage):
    target, key = tmp_path / 'state', Fernet.generate_key()
    data = b'a' * (resume.CHUNK_SIZE + 7)
    with pytest.raises(InterruptedError):
        seal(target, key, data, progress=stop)
    chunk = target / '00000000.fernet'
    if damage == 'truncated':
        chunk.write_bytes(chunk.read_bytes()[:50])
    elif damage == 'wrong-position':
        chunk.rename(target / '00000001.fernet')
    elif damage == 'missing':
        chunk.unlink()
    else:
        (target / 'write.part').write_bytes(b'torn write')
    result = seal(target, key, data)
    assert b''.join(read_chunks(target, key, result)) == data
    assert not (target / 'write.part').exists()


def test_source_change_even_same_size_and_time_is_not_reused(tmp_path, roots):
    target, key = tmp_path / 'state', Fernet.generate_key()
    with pytest.raises(InterruptedError):
        capture(roots, target, key, progress=stop)
    chunk = target / '00000000.fernet'
    old = chunk.read_bytes()
    source = roots['config'] / 'local.json'
    info = source.stat()
    source.write_bytes(b'x' * info.st_size)
    os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns))
    capture(roots, target, key, resume=True)
    assert chunk.read_bytes() != old


def test_wrong_key_and_unknown_entries_never_modified(tmp_path):
    target, key = tmp_path / 'state', Fernet.generate_key()
    with pytest.raises(InterruptedError):
        seal(target, key, b'original', progress=stop)
    saved = {p.name: p.read_bytes() for p in target.iterdir()}
    with pytest.raises(InvalidArtifact):
        seal(target, Fernet.generate_key(), b'changed')
    assert saved == {p.name: p.read_bytes() for p in target.iterdir()}
    (target / 'unknown').write_bytes(b'keep')
    with pytest.raises(InvalidArtifact, match='Unsafe'):
        seal(target, key, b'changed')
    assert (target / 'unknown').read_bytes() == b'keep'


def test_producer_failure_invalidates_old_completion_and_keeps_blocks(tmp_path):
    target, key = tmp_path / 'state', Fernet.generate_key()
    seal(target, key, b'old')
    def fail():
        raise ValueError('source changed')
    with pytest.raises(ValueError, match='source changed'):
        resume.seal_state_stream(io.BytesIO(b'new'), target, key, estimated_bytes=3,
                                 guard_factory=ample, finish=fail)
    assert not (target / 'complete.fernet').exists()
    assert (target / '00000000.fernet').exists()
    seal(target, key, b'new')
    verify_artifact(target, key)


def test_smaller_stream_removes_only_old_tail(tmp_path):
    target, key = tmp_path / 'state', Fernet.generate_key()
    seal(target, key, b'a' * (resume.CHUNK_SIZE + 7))
    seal(target, key, b'b')
    assert not (target / '00000001.fernet').exists()
    verify_artifact(target, key)


def test_first_header_write_interrupted_can_retry(tmp_path, monkeypatch):
    target, key = tmp_path / 'state', Fernet.generate_key()
    writer = resume.write_token
    def torn(path, token, guard):
        path.write_bytes(token[:10])
        raise OSError('disconnect')
    monkeypatch.setattr(resume, 'write_token', torn)
    with pytest.raises(OSError):
        seal(target, key, b'data')
    monkeypatch.setattr(resume, 'write_token', writer)
    seal(target, key, b'data')
    verify_artifact(target, key)


def test_set_aside_keeps_state_but_never_reuses_database(tmp_path):
    target, key = tmp_path / 'catalog/state', Fernet.generate_key()
    target.parent.mkdir()
    with pytest.raises(InterruptedError):
        seal(target, key, b'data', progress=stop)
    (target.parent / 'database').mkdir()
    (target.parent / 'database/old').write_bytes(b'old dump')
    (target.parent / 'journal.json').write_text('{}')
    saved = (target / '00000000.fernet').read_bytes()
    set_aside_unfinished(tmp_path, key)
    assert {p.name for p in target.parent.iterdir()} == {'state'}
    assert (target / '00000000.fernet').read_bytes() == saved
    assert len(list(tmp_path.glob('previous-catalog-*/database/old'))) == 1
    # A second interruption during catalog preparation still keeps the state.
    set_aside_unfinished(tmp_path, key)
    assert (target / '00000000.fernet').read_bytes() == saved


def test_capacity_credit_and_failed_write_do_not_publish(tmp_path):
    target, key = tmp_path / 'state', Fernet.generate_key()
    data = b'x' * resume.CHUNK_SIZE * 3
    def stop_second(size):
        if size == resume.CHUNK_SIZE * 2:
            stop(size)
    fresh = resume.remaining_budget(target, key, len(data))
    with pytest.raises(InterruptedError):
        seal(target, key, data, progress=stop_second)
    assert resume.remaining_budget(target, key, len(data)) < fresh
    def full(requirements):
        guard = ample(requirements)
        def fail(**kwargs):
            raise InsufficientSpace([])
        guard.check = fail
        return guard
    saved = (target / '00000000.fernet').read_bytes()
    with pytest.raises(InsufficientSpace):
        resume.seal_state_stream(io.BytesIO(data), target, key, estimated_bytes=len(data),
                                 guard_factory=full, finish=lambda: None)
    assert not (target / 'complete.fernet').exists()
    assert (target / '00000000.fernet').read_bytes() == saved


def test_whole_library_retry_keeps_state_and_takes_fresh_dump(world, monkeypatch):
    from app.backup import catalog, library
    from app.backup.encrypted import seal_stream
    (world.config / 'local.json').write_text('{}')
    attempts = []
    def database(endpoint, pg_dump, directory, key, **kwargs):
        attempts.append(len(attempts))
        raw = f'database attempt {len(attempts)}'.encode()
        return seal_stream(io.BytesIO(raw), directory, key, estimated_bytes=len(raw),
                           guard_factory=ample, metadata={'kind': 'postgres-custom-dump'})
    monkeypatch.setattr(catalog, 'capture_database', database)
    monkeypatch.setattr(library, 'capture_components', catalog.capture_components)
    def interrupt(event):
        if event['phase'] == 'state':
            stop(event['bytes'])
    with pytest.raises(InterruptedError):
        world.capture(progress=interrupt)
    chunk = world.backup / 'catalog/state/00000000.fernet'
    original = chunk.read_bytes()
    acknowledge(world)
    world.capture(resume=True)
    assert len(attempts) == 2
    assert chunk.read_bytes() == original
    library.verify_library(world.backup, world.key)


def test_progress_label():
    from app.backup.admin_runner import PHASES
    assert PHASES['state'] == 'Saving application data'


def test_disk_disconnect_mid_write_preserves_completed_blocks(tmp_path, monkeypatch):
    target, key = tmp_path / 'state', Fernet.generate_key()
    data = b'a' * resume.CHUNK_SIZE + b'b' * resume.CHUNK_SIZE
    writer = resume.write_token
    calls = []
    def torn(path, token, guard):
        calls.append(path)
        if len(calls) == 3:  # header, first chunk, then interrupted second chunk
            path.write_bytes(token[:123])
            raise OSError('simulated disk disconnect')
        writer(path, token, guard)
    monkeypatch.setattr(resume, 'write_token', torn)
    with pytest.raises(OSError, match='disconnect'):
        seal(target, key, data)
    first = (target / '00000000.fernet').read_bytes()
    assert not (target / 'complete.fernet').exists()
    monkeypatch.setattr(resume, 'write_token', writer)
    result = seal(target, key, data)
    assert first == (target / '00000000.fernet').read_bytes()
    assert b''.join(read_chunks(target, key, result)) == data


def test_hardlinked_chunk_is_rejected_without_touching_link_target(tmp_path):
    target, key = tmp_path / 'state', Fernet.generate_key()
    with pytest.raises(InterruptedError):
        seal(target, key, b'data', progress=stop)
    chunk = target / '00000000.fernet'
    other = tmp_path / 'outside'
    chunk.rename(other)
    os.link(other, chunk)
    before = other.read_bytes()
    with pytest.raises(InvalidArtifact, match='Unsafe'):
        seal(target, key, b'new')
    assert other.read_bytes() == before


def test_corrupt_resume_record_is_not_silently_reinitialized(tmp_path):
    target, key = tmp_path / 'state', Fernet.generate_key()
    with pytest.raises(InterruptedError):
        seal(target, key, b'data', progress=stop)
    (target / resume.HEADER).write_bytes(b'corrupt')
    original = (target / '00000000.fernet').read_bytes()
    with pytest.raises(InvalidArtifact, match='resume record'):
        seal(target, key, b'new')
    assert original == (target / '00000000.fernet').read_bytes()


APPLE_DOUBLE = b'\x00\x05\x16\x07\x00\x02\x00\x00' + bytes(24)


def sidecars(target):
    """What macOS writes beside each file on exFAT: a real AppleDouble `._name`."""
    for path in list(target.iterdir()):
        if not path.name.startswith('._'):
            (target / ('._' + path.name)).write_bytes(APPLE_DOUBLE)


def test_resume_accepts_macos_exfat_sidecars(tmp_path):
    target, key = tmp_path / 'state', Fernet.generate_key()
    data = b'a' * (resume.CHUNK_SIZE * 2 + 7)
    with pytest.raises(InterruptedError):
        seal(target, key, data, progress=stop)
    sidecars(target)
    assert {path.name for path in resume.inspect_entries(target)} <= {'resume.fernet', '00000000.fernet', 'write.part'}
    result = seal(target, key, data)
    assert b''.join(read_chunks(target, key, result)) == data
    sidecars(target)
    assert verify_artifact(target, key) == result


def test_retry_tolerates_sidecars_its_own_files_left(tmp_path):
    """A Mac wrote `._` files on exFAT; Windows then retries, replacing and removing files but
    leaving their sidecars. Covers a completed artifact, write.part renames and a shrinking tail."""
    target, key = tmp_path / 'state', Fernet.generate_key()
    first = b'a' * (resume.CHUNK_SIZE * 3 + 7)
    seal(target, key, first)
    sidecars(target)
    assert seal(target, key, first)['size'] == len(first)
    assert (target / '._complete.fernet').exists()
    sidecars(target)
    (target / 'write.part').write_bytes(b'torn')
    (target / '._write.part').write_bytes(APPLE_DOUBLE)
    changed = b'b' * (resume.CHUNK_SIZE + 3)  # Rewrites block 0, removes tail blocks 2 and 3.
    result = seal(target, key, changed)
    assert not (target / '00000002.fernet').exists() and (target / '._00000002.fernet').exists()
    assert not (target / 'write.part').exists() and (target / '._write.part').exists()
    assert b''.join(read_chunks(target, key, result)) == changed
    assert verify_artifact(target, key) == result
    assert seal(target, key, changed) == result


@pytest.mark.parametrize('case', ['lookalike', 'foreign', 'hardlink'])
def test_unvalidated_sidecar_still_refused(tmp_path, case):
    target, key = tmp_path / 'state', Fernet.generate_key()
    with pytest.raises(InterruptedError):
        seal(target, key, b'a' * (resume.CHUNK_SIZE + 7), progress=stop)
    name = target / '._resume.fernet'
    if case == 'lookalike':
        name.write_bytes(b'not apple double')
    elif case == 'foreign':
        (target / '._photo.jpg').write_bytes(APPLE_DOUBLE)  # Not a name this folder owns.
    else:
        name.write_bytes(APPLE_DOUBLE)
        os.link(name, tmp_path / 'second-name')
    with pytest.raises(InvalidArtifact, match='Unsafe application-data resume entry'):
        resume.inspect_entries(target)
