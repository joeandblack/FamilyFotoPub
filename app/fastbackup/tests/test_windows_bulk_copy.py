import threading
import time

import pytest

from app.fastbackup.windows_bulk_copy import copy_files


def test_primitive_checks_destination_without_reading_source(tmp_path, monkeypatch):
    from app.fastbackup import windows_bulk_copy as bulk
    calls = []

    def native(source, target, parameters):
        calls.append((source, target, parameters._obj.flags))
        return 0

    monkeypatch.setattr(bulk, 'copy_api', lambda: native)
    bulk.copy_file(tmp_path / 'not-inspected-source', tmp_path / 'not-inspected-target')
    assert calls == [(str(tmp_path / 'not-inspected-source'), str(tmp_path / 'not-inspected-target'), 0x8801)]


@pytest.mark.parametrize('kind', ['file', 'directory', 'entry-reported-by-lstat'])
def test_existing_entry_refused_before_native_copy(tmp_path, monkeypatch, kind):
    from app.fastbackup import windows_bulk_copy as bulk
    target = tmp_path / 'target'
    if kind == 'file':
        target.write_bytes(b'keep')
    elif kind == 'directory':
        target.mkdir()
    else:
        # Exercise a reported entry without creating a link; the native test covers real links.
        monkeypatch.setattr(bulk.os, 'lstat', lambda path: object())
    def forbidden():
        pytest.fail('Native copy must not be called for an existing entry')
    monkeypatch.setattr(bulk, 'copy_api', forbidden)
    with pytest.raises(FileExistsError):
        bulk.copy_file(tmp_path / 'source', target)
    if kind == 'file':
        assert target.read_bytes() == b'keep'


def test_destination_inspection_error_fails_closed(tmp_path, monkeypatch):
    from app.fastbackup import windows_bulk_copy as bulk
    def denied(path):
        raise PermissionError('cannot inspect target')
    monkeypatch.setattr(bulk.os, 'lstat', denied)
    monkeypatch.setattr(bulk, 'copy_api', lambda: pytest.fail('Native copy must not run'))
    with pytest.raises(PermissionError):
        bulk.copy_file(tmp_path / 'source', tmp_path / 'target')


def test_native_failure_is_not_reported_as_success(tmp_path, monkeypatch):
    from app.fastbackup import windows_bulk_copy as bulk
    monkeypatch.setattr(bulk, 'copy_api', lambda: lambda *args: -2147024784)
    with pytest.raises(OSError, match='HRESULT'):
        bulk.copy_file(tmp_path / 'source', tmp_path / 'target')


def test_bulk_copy_runs_concurrently_and_accounts_on_coordinator(tmp_path):
    rendezvous = threading.Barrier(2)
    coordinator = threading.get_ident()
    workers = set()
    done = []

    def copier(source, target):
        workers.add(threading.get_ident())
        rendezvous.wait(timeout=5)

    def completed(size):
        assert threading.get_ident() == coordinator
        done.append(size)

    copy_files(tmp_path, tmp_path, [('one', 10), ('two', 20)], copier=copier, completed=completed)
    assert len(workers) == 2
    assert sorted(done) == [10, 20]


def test_failure_joins_active_writers_before_return(tmp_path):
    rendezvous = threading.Barrier(2)
    finished = threading.Event()

    def copier(source, target):
        rendezvous.wait(timeout=5)
        if source.name == 'fail':
            raise OSError('disconnected')
        time.sleep(0.05)
        finished.set()

    with pytest.raises(OSError, match='disconnected'):
        copy_files(tmp_path, tmp_path, [('fail', 1), ('slow', 1)], copier=copier)
    assert finished.is_set()


def test_cancel_prevents_queued_work_and_does_not_return_with_writers(tmp_path):
    stopped = threading.Event()
    written = []

    def copier(source, target):
        written.append(source.name)
        stopped.set()

    with pytest.raises(InterruptedError):
        copy_files(tmp_path, tmp_path, [(str(i), 1) for i in range(100)],
                   copier=copier, cancelled=stopped.is_set)
    assert len(written) < 100


def test_callback_failure_is_propagated(tmp_path):
    def completed(size):
        raise OSError('capacity exhausted')

    with pytest.raises(OSError, match='capacity exhausted'):
        copy_files(tmp_path, tmp_path, [('one', 1)], copier=lambda *args: None, completed=completed)
