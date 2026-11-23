import os

import pytest

from fastbackup import full_copy
from fastbackup.tests.test_full_copy import copy, fixture


def test_all_copies_precede_verification(tmp_path):
    paths = fixture(tmp_path)
    events = []
    def progress(event):
        events.append(event)
        if event['phase'] == 'media-verification':
            assert all((paths[2] / 'media' / name).exists() for name in ['photo.jpg', 'nested/video.mp4', 'zero'])
            assert not (paths[2] / 'TREE-COMPLETE.json').exists()
    result = copy(paths, progress=progress)
    assert result['content_verified'] is True
    phases = ['media-preparation', 'media-transfer', 'media-verification', 'media-final-check']
    assert [e['phase'] for e in events if e['state'] == 'started'] == phases
    assert [e['phase'] for e in events if e['state'] == 'completed'] == phases
    for phase in phases:
        selected = [e for e in events if e['phase'] == phase]
        assert [e['elapsed_seconds'] for e in selected] == sorted(e['elapsed_seconds'] for e in selected)
    transfer_end = next(i for i, e in enumerate(events) if e['phase'] == 'media-transfer' and e['state'] == 'completed')
    verify_start = next(i for i, e in enumerate(events) if e['phase'] == 'media-verification')
    assert transfer_end < verify_start


def test_verification_cancellation_retains_all_copies_without_completion(tmp_path):
    paths = fixture(tmp_path)
    events = []
    with pytest.raises(InterruptedError, match='Verification cancelled'):
        copy(paths, progress=events.append,
             cancelled=lambda: any(e['phase'] == 'media-verification' for e in events))
    assert (paths[2] / 'TREE-INCOMPLETE').exists()
    assert not (paths[2] / 'TREE-COMPLETE.json').exists()
    assert len([p for p in (paths[2] / 'media').rglob('*') if p.is_file()]) == 3
    assert events[-1]['state'] == 'failed'


@pytest.mark.skipif(os.name != 'nt', reason='Windows verification path')
def test_windows_defers_flush_and_hash_without_retained_file_locks(tmp_path, monkeypatch):
    paths = fixture(tmp_path)
    events, operations = [], []
    original_verify = full_copy._flush_and_verify
    def verify(path, size, digest):
        assert events[-1]['phase'] == 'media-verification'
        assert len([p for p in (paths[2] / 'media').rglob('*') if p.is_file()]) == 3
        operations.append(path)
        return original_verify(path, size, digest)
    monkeypatch.setattr(full_copy, '_flush_and_verify', verify)
    def progress(event):
        events.append(event)
        if event['phase'] == 'media-verification' and event['state'] in {'started', 'completed'}:
            with (paths[2] / 'media/photo.jpg').open('r+b'):
                pass
    copy(paths, progress=progress)
    assert len(operations) == 3


def test_phase_duration_and_failed_status(monkeypatch):
    now = [10.0]
    monkeypatch.setattr(full_copy.time, 'monotonic', lambda: now[0])
    events = []
    with full_copy._timed_phase(events.append, 'media-transfer', {'files': 1, 'bytes': 5}) as report:
        now[0] = 12.25
        report(dict(files=1, bytes=5))
    assert events[-1]['elapsed_seconds'] == 2.25
    assert events[-1]['state'] == 'completed'
    with pytest.raises(OSError):
        with full_copy._timed_phase(events.append, 'media-verification', {'files': 1, 'bytes': 5}):
            now[0] = 14.0
            raise OSError('disconnected')
    assert events[-1]['elapsed_seconds'] == 1.75
    assert events[-1]['state'] == 'failed'


@pytest.mark.parametrize('callback_fails_first', [False, True])
def test_failed_progress_report_preserves_original_error(callback_fails_first):
    original = OSError('original failure')
    events = []
    def progress(event):
        events.append(event)
        if event['state'] == 'failed':
            raise BrokenPipeError('failure report channel broken')
        if event['state'] == 'running' and callback_fails_first:
            raise original
    with pytest.raises(OSError) as caught:
        with full_copy._timed_phase(progress, 'media-transfer', {'files': 1, 'bytes': 5}) as report:
            if callback_fails_first:
                report(dict(files=1, bytes=5))
            else:
                raise original
    assert caught.value is original
    assert events[-1]['state'] == 'failed'


def test_phase_boundary_logs_are_not_throttled(monkeypatch):
    from app.backup import admin_runner
    monkeypatch.setattr(admin_runner, 'read_run', lambda _: {'id': 'test'})
    monkeypatch.setattr(admin_runner, 'write_run', lambda _: None)
    notes = []
    monkeypatch.setattr(admin_runner, 'note_activity', lambda _, text: notes.append(text))
    monkeypatch.setattr(admin_runner, 'LOG_PROGRESS_SECONDS', 3600)
    run = admin_runner.Run('test')
    for state, seconds in [('started', 0), ('running', 0.1), ('completed', 0.2)]:
        run.progress(dict(phase='media-transfer', state=state, elapsed_seconds=seconds))
    assert len(notes) == 2
    assert 'started, 0.000 seconds' in notes[0]
    assert 'completed, 0.200 seconds' in notes[1]


def test_worker_heartbeat_retains_phase_counts_and_freezes_completed_duration():
    from fastbackup.copy_worker import _heartbeat_event
    last = dict(phase='media-verification', state='running', elapsed_seconds=4, files=2, bytes=8)
    assert _heartbeat_event(last, 6) == dict(last, elapsed_seconds=10)
    assert last['elapsed_seconds'] == 4
    completed = dict(last, state='completed')
    assert _heartbeat_event(completed, 10) == dict(completed, state='waiting')


@pytest.mark.skipif(os.name != 'nt', reason='Windows flush failure')
def test_flush_failure_during_second_pass_never_publishes(tmp_path, monkeypatch):
    paths = fixture(tmp_path)
    events = []
    def fail(*_):
        raise OSError('disk disconnected while flushing')
    monkeypatch.setattr(full_copy, '_flush_and_verify', fail)
    with pytest.raises(OSError, match='disconnected'):
        copy(paths, progress=events.append)
    assert events[-1]['phase'] == 'media-verification' and events[-1]['state'] == 'failed'
    assert (paths[2] / 'TREE-INCOMPLETE').exists()
    assert not (paths[2] / 'TREE-COMPLETE.json').exists()


def test_short_lived_verifier_rejects_same_size_corruption(tmp_path):
    import hashlib
    path = tmp_path / 'photo'
    path.write_bytes(b'wrong')
    with pytest.raises(ValueError, match='verification'):
        full_copy._flush_and_verify(path, 5, hashlib.sha256(b'right').hexdigest())


def test_verifier_cannot_skip_hashing_with_missing_digest(tmp_path):
    path = tmp_path / 'photo'
    path.write_bytes(b'photo')
    with pytest.raises(ValueError, match='verification'):
        full_copy._flush_and_verify(path, 5, None)


def test_short_lived_verifier_preserves_contents_and_flushes(tmp_path, monkeypatch):
    import hashlib
    path = tmp_path / 'photo'
    path.write_bytes(b'photo')
    real = os.fsync
    calls = []
    def flush(fd):
        calls.append(fd)
        return real(fd)
    monkeypatch.setattr(os, 'fsync', flush)
    full_copy._flush_and_verify(path, 5, hashlib.sha256(b'photo').hexdigest())
    assert len(calls) == 1
    assert path.read_bytes() == b'photo'
    path.unlink()
