import errno
import json
import sys
from types import SimpleNamespace

import pytest

from fastbackup.diagnostics import CopyFailure, describe
from fastbackup.native_copy import flush_device
from app.backup.diagnostics import ExplainedFailure


@pytest.mark.parametrize('code,category', [(errno.ENOSPC, 'disk-full'), (errno.EACCES, 'permission-denied')])
def test_safe_os_diagnostics(code, category):
    details = describe(OSError(code, 'secret password', 'private/path'))
    assert details['errno'] == code
    assert category in details['categories']
    assert 'secret' not in json.dumps(details)
    assert 'private/path' not in json.dumps(details)
    assert describe(CopyFailure(details)) == details


def test_explained_and_unknown_errors():
    assert describe(ExplainedFailure('Safe explanation'))['message'] == 'Safe explanation'
    assert 'secret' not in json.dumps(describe(ValueError('secret')))
    assert 'new directory' in describe(FileExistsError('private/path'))['message']


def test_worker_preserves_safe_error_codes(monkeypatch):
    from fastbackup import copy_worker
    messages = []
    closed = []
    connection = SimpleNamespace(send_bytes=lambda raw: messages.append(json.loads(raw)),
                                 close=lambda: closed.append(True))
    def fail(*args, **kwargs):
        raise OSError(errno.ENOSPC, 'secret', 'private/path')
    monkeypatch.setattr(copy_worker, 'copy_media_tree', fail)
    copy_worker._worker(connection, 'source', 'inventory', 'target', False, None)
    assert closed == [True]
    assert messages[0]['kind'] == 'error'
    assert messages[0]['value']['errno'] == errno.ENOSPC
    assert 'secret' not in json.dumps(messages)


def test_darwin_flush_requests_fullsync_and_propagates_error(tmp_path, monkeypatch):
    path = tmp_path / 'file'
    path.write_bytes(b'content')
    calls = []
    monkeypatch.setattr(sys, 'platform', 'darwin')
    fake = SimpleNamespace(F_FULLFSYNC=51, fcntl=lambda fd, op: calls.append(op))
    monkeypatch.setitem(sys.modules, 'fcntl', fake)
    flush_device(path)
    assert calls == [51]
    def fail(fd, op):
        raise OSError(errno.EIO, 'flush failure')
    fake.fcntl = fail
    with pytest.raises(OSError):
        flush_device(path)
