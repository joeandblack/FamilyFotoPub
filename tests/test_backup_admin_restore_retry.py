"""Try again on a failed restore test, with a real backup and real temporary databases."""
import importlib
import json
import os
from pathlib import Path
import shutil
import socket
import tempfile
from types import SimpleNamespace

import pytest

from app.backup import admin_restore, admin_runs, library_restore, media_inventory
from app.backup.diagnostics import ExplainedFailure
from app.backup.encrypted import InvalidArtifact
from app.backup.library import capture_library
from tests.test_backup_postgres import ample, postgres  # noqa: F401
from tests.test_migration_disk import relocation  # noqa: F401
from tests.test_migration_tls import authority


PG_BIN = os.environ.get('FAMILYFOTO_BACKUP_PG_BIN')
pytestmark = pytest.mark.skipif(not PG_BIN, reason='Set FAMILYFOTO_BACKUP_PG_BIN to real PostgreSQL tools')
ROLE = ('app.migration.credentials', 'prepare_recovery_role')
TLS = ('app.migration.tls', 'prepare_recovery_tls')
VALIDATE = ('app.migration.validation', 'validate_plain_candidate')


@pytest.fixture
def attempt(relocation, postgres, tmp_path, monkeypatch):  # noqa: F811
    run, config, data, media, _, _, key = relocation
    authority(config / 'tls')
    backup = tmp_path / 'whole-backup'
    capture_library(run.endpoint, run.dump, config, data, media, backup, key,
                    database_budget=1024 ** 2, offline_confirmed=True, guard_factory=ample)
    monkeypatch.setattr(admin_restore, 'RESTORES', tmp_path / 'runs')
    # Short, because the restored copy's job folders run close to the Windows path limit.
    # Resolved, because macOS's temp folder sits under the /var symlink, which backups refuse.
    parent = Path(tempfile.mkdtemp(prefix='ff-retry-')).resolve()
    run_id = admin_restore.new_run_id()
    folder = admin_restore.create_folder(parent, run_id)
    admin_restore.write_run({'id': run_id, 'state': 'starting', 'started_at': admin_runs.now(), 'directory': str(folder),
                             'backup_directory': str(backup), 'tools': PG_BIN,
                             'database_budget': 1024 ** 2})
    restorations = []
    real_restore = admin_restore.restore_library

    def counted_restore(*args, **kwargs):
        restorations.append(kwargs['resume'])
        return real_restore(*args, **kwargs)
    monkeypatch.setattr(admin_restore, 'restore_library', counted_restore)

    def go():
        admin_restore.restore(admin_restore.Run(run_id), key, guard_factory=ample)
        return admin_restore.read_run(run_id)
    yield SimpleNamespace(go=go, folder=Path(folder), run_id=run_id, restorations=restorations, patch=monkeypatch)
    admin_restore.RecoveryCluster(Path(folder), PG_BIN, None).stop()
    shutil.rmtree(parent, ignore_errors=True)


def fail_once(fixture, module, name, before=None, after_real=False):
    """The named step fails on its first call only, optionally leaving something behind first."""
    module = importlib.import_module(module) if isinstance(module, str) else module
    real = getattr(module, name)
    seen = []

    def step(*args, **kwargs):
        seen.append(kwargs)
        if len(seen) == 1:
            if after_real:
                real(*args, **kwargs)
            if before:
                before()
            raise OSError('Injected failure')
        return real(*args, **kwargs)
    fixture.patch.setattr(module, name, step)
    return seen


def job_port(fixture):
    return json.loads((fixture.folder / 'library/restore-job.json').read_text())['endpoint'][1]


def assert_finished(run, fixture):
    assert run['state'] == 'finished' and run['validated_at'] and run['media_files'] == 1
    assert (Path(run['restored_library']) / 'photo.jpg').read_bytes() == b'fixture photo'
    assert not admin_restore.RecoveryCluster(fixture.folder, PG_BIN, None).running()


def test_a_restore_interrupted_while_copying_continues(attempt):
    fail_once(attempt, library_restore, 'copy_media_tree')
    with pytest.raises(OSError, match='Injected'):
        attempt.go()
    assert_finished(attempt.go(), attempt)
    assert attempt.restorations == [False, True]


@pytest.mark.parametrize('step', [ROLE, TLS, VALIDATE], ids=['role', 'tls', 'validate'])
def test_a_failure_after_the_restore_does_not_restore_again(attempt, step):
    fail_once(attempt, *step)
    with pytest.raises(OSError, match='Injected'):
        attempt.go()
    assert_finished(attempt.go(), attempt)
    assert attempt.restorations == [False]


def test_a_later_attempt_rechecks_everything_itself(attempt):
    seen = fail_once(attempt, *VALIDATE)
    with pytest.raises(OSError, match='Injected'):
        attempt.go()
    attempt.go()
    assert [call['fresh_restore'] for call in seen] == [True, False]


def test_a_role_step_that_only_made_its_folder_starts_over(attempt):
    def leave_folder():
        (attempt.folder / 'role').mkdir(mode=0o700)
        (attempt.folder / 'role/copy.lock').touch()
    fail_once(attempt, *ROLE, before=leave_folder)
    with pytest.raises(OSError, match='Injected'):
        attempt.go()
    assert_finished(attempt.go(), attempt)


def test_a_role_step_that_finished_can_run_again(attempt):
    fail_once(attempt, *ROLE, after_real=True)
    with pytest.raises(OSError, match='Injected'):
        attempt.go()
    assert_finished(attempt.go(), attempt)


def test_unfinished_certificates_are_replaced(attempt):
    def leave_partial():
        (attempt.folder / 'tls').mkdir(mode=0o700)
        (attempt.folder / 'tls/ca.pem').write_bytes(b'partial')
    fail_once(attempt, *TLS, before=leave_partial)
    with pytest.raises(OSError, match='Injected'):
        attempt.go()
    assert_finished(attempt.go(), attempt)
    assert (attempt.folder / 'tls/ca.pem').read_bytes() != b'partial'


def test_a_restore_that_only_made_its_folder_starts_over(attempt):
    def leave_folder():
        (attempt.folder / 'library').mkdir(mode=0o700)
        (attempt.folder / 'library/copy.lock').touch()
    fail_once(attempt, library_restore, 'verify_library', before=leave_folder)
    with pytest.raises(OSError, match='Injected'):
        attempt.go()
    assert_finished(attempt.go(), attempt)
    assert attempt.restorations == [False, False]


def test_a_catalog_step_that_only_made_its_folder_starts_over(attempt):
    def leave_folder():
        (attempt.folder / 'library/catalog').mkdir(mode=0o700)
        (attempt.folder / 'library/catalog/copy.lock').touch()
    fail_once(attempt, library_restore, 'restore_catalog', before=leave_folder)
    with pytest.raises(OSError, match='Injected'):
        attempt.go()
    assert_finished(attempt.go(), attempt)
    assert attempt.restorations == [False, True]


def test_a_folder_holding_anything_else_is_left_alone_and_refused(attempt):
    (attempt.folder / 'library').mkdir(mode=0o700)
    (attempt.folder / 'library/unknown.txt').write_text('keep')
    with pytest.raises(ExplainedFailure, match='start again in a new folder'):
        attempt.go()
    assert (attempt.folder / 'library/unknown.txt').read_text() == 'keep'


def test_a_crash_just_after_the_final_report_still_finishes(attempt):
    def leave_marker():
        (attempt.folder / 'library/LIBRARY-RESTORE-INCOMPLETE').touch()
    fail_once(attempt, *ROLE, before=leave_marker)
    with pytest.raises(OSError, match='Injected'):
        attempt.go()
    assert_finished(attempt.go(), attempt)
    assert attempt.restorations == [False]


def test_a_restore_that_stopped_after_copying_continues(attempt):
    fail_once(attempt, library_restore, 'verify_library_metadata')
    with pytest.raises(OSError, match='Injected'):
        attempt.go()
    assert_finished(attempt.go(), attempt)
    assert attempt.restorations == [False, True]


def test_a_retry_does_not_rehash_a_finished_inventory(attempt):
    fail_once(attempt, library_restore, 'copy_media_tree')
    with pytest.raises(OSError, match='Injected'):
        attempt.go()
    attempt.patch.setattr(media_inventory, 'hash_file', lambda *a: pytest.fail('Read 1 of this attempt covered it'))
    assert_finished(attempt.go(), attempt)


def test_a_backup_whose_sealed_parts_changed_during_the_restore_is_refused(attempt):
    real = library_restore.verify_library_metadata
    attempt.patch.setattr(library_restore, 'verify_library_metadata', lambda *a, **k: {**real(*a, **k), 'id': 'another-capture'})
    with pytest.raises(InvalidArtifact, match='changed during recovery'):
        attempt.go()
    assert not (attempt.folder / 'library/LIBRARY-RECOVERY-REPORT.json').exists()


def test_a_retry_uses_the_port_the_restore_recorded(attempt):
    fail_once(attempt, *VALIDATE)
    with pytest.raises(OSError, match='Injected'):
        attempt.go()
    port = job_port(attempt)
    used = []
    real_start = admin_restore.RecoveryCluster.start

    def start(cluster):
        real_start(cluster)
        used.append(cluster.port)
    attempt.patch.setattr(admin_restore.RecoveryCluster, 'start', start)
    attempt.go()
    assert used == [port]


def test_a_taken_port_is_explained_and_can_be_retried(attempt):
    fail_once(attempt, *VALIDATE)
    with pytest.raises(OSError, match='Injected'):
        attempt.go()
    with socket.socket() as holder:
        holder.bind(('127.0.0.1', job_port(attempt)))
        holder.listen()
        with pytest.raises(RuntimeError, match='in use'):
            attempt.go()
    assert_finished(attempt.go(), attempt)


@pytest.mark.parametrize('recorded', ['not-json', '{"endpoint": ["127.0.0.1", "5432", "postgres"]}',
                                      '{"endpoint": ["127.0.0.1", 70000, "postgres"]}'],
                         ids=['garbled', 'text-port', 'out-of-range'])
def test_a_damaged_job_record_is_refused_not_guessed_at(tmp_path, recorded):
    (tmp_path / 'library').mkdir()
    (tmp_path / 'library/restore-job.json').write_text(recorded)
    with pytest.raises(RuntimeError, match='port'):
        admin_restore.RecoveryCluster(tmp_path, PG_BIN, None).recorded_port()
