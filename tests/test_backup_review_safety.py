import subprocess
from types import SimpleNamespace
from io import BytesIO

import pytest

from app.backup.restore_database import RestoreFailed, wait_for_archive_completion


def test_restore_can_finalize_beyond_two_minutes():
    elapsed = [0]
    checks = []

    class Process:
        def wait(self, timeout):
            elapsed[0] += timeout
            if elapsed[0] < 180:
                raise subprocess.TimeoutExpired('fixture', timeout)
            return 0

    guard = SimpleNamespace(check=lambda: checks.append(elapsed[0]))
    assert wait_for_archive_completion(Process(), guard, clock=lambda: elapsed[0]) == 0
    assert elapsed[0] == 180 and len(checks) == 36


def test_restore_finalization_has_bounded_wait():
    elapsed = [0]

    class Process:
        def wait(self, timeout):
            elapsed[0] += timeout
            raise subprocess.TimeoutExpired('fixture', timeout)

    with pytest.raises(RestoreFailed, match='target remains isolated'):
        wait_for_archive_completion(Process(), SimpleNamespace(check=lambda: None), timeout=10, clock=lambda: elapsed[0])


def test_finalization_propagates_disk_failure_before_wait():
    def failed():
        raise OSError('fixture disconnected')
    with pytest.raises(OSError, match='disconnected'):
        wait_for_archive_completion(None, SimpleNamespace(check=failed))


@pytest.mark.parametrize('message,category', [
    (b'permission denied for secret_customer /private/file', 'permission-denied'),
    (b'could not write: No space left on device /secret/path', 'disk-full'),
    (b'aborting because of server version mismatch', 'version-mismatch'),
    (b'password authentication failed for SECRET_USER', 'connection-failed'),
    (b'WARNING: SECRET_UNKNOWN', 'unclassified-tool-output'),
])
def test_diagnostics_publish_only_categories(message, category):
    from app.backup.diagnostics import ProcessDiagnostics, safe_categories
    diagnostics = ProcessDiagnostics()
    diagnostics.drain(BytesIO(message))
    error = diagnostics.failure(RuntimeError, 'Operation failed')
    assert diagnostics.has_output
    assert safe_categories(error) == [category]
    assert str(error) == 'Operation failed'
    assert not any(isinstance(value, bytes) for value in vars(diagnostics).values())


def test_diagnostic_pattern_can_span_read_boundary():
    from app.backup.diagnostics import ProcessDiagnostics, safe_categories
    diagnostic = ProcessDiagnostics()
    diagnostic.drain(BytesIO(b'x' * 65530 + b'No space left on device'))
    assert safe_categories(diagnostic.failure(RuntimeError, 'Failed')) == ['disk-full']


def test_unknown_exception_categories_are_not_printed():
    from app.backup.diagnostics import safe_categories
    error = RuntimeError('secret')
    error.diagnostic_categories = ['password=secret', 'disk-full', {'private': 'data'}]
    assert safe_categories(error) == ['disk-full']
