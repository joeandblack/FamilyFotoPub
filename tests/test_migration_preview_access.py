import json
from pathlib import Path

import psycopg
import pytest

from app.backup.postgres import Endpoint
from app.backup.encrypted import InvalidArtifact
from app.migration import preview_access
from tests.test_backup_postgres import postgres  # noqa: F401
from tests.test_migration_credentials import recovery_role  # noqa: F401


def arguments(params, record, directory):
    return (Endpoint(params['host'], params['port'], 'postgres', params['user']), record, directory)


def test_preview_login_is_usable_only_within_explicit_access_scope(recovery_role):
    run, params, record, directory = recovery_role
    run(preview=True)
    with preview_access.preview_database_access(*arguments(params, record, directory),
            expected_cluster_directory=Path(record['cluster_directory'])) as cfg:
        with psycopg.connect(**cfg) as db:
            assert db.execute('SELECT count(*) FROM media').fetchone()[0] == 1
            assert db.execute('SHOW transaction_read_only').fetchone()[0] == 'on'
        with psycopg.connect(**{**cfg, 'options': '-c default_transaction_read_only=off'}) as db:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                db.execute('DELETE FROM media')
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(**cfg)
    assert json.loads((directory / 'PREVIEW-ACCESS.json').read_text())['state'] == 'closed'


def test_preview_failure_revokes_login_and_closes_leftover_connection(recovery_role):
    run, params, record, directory = recovery_role
    run(preview=True)
    db = None
    try:
        with pytest.raises(RuntimeError, match='preview failed'):
            with preview_access.preview_database_access(*arguments(params, record, directory),
                    expected_cluster_directory=Path(record['cluster_directory'])) as cfg:
                db = psycopg.connect(**cfg)
                raise RuntimeError('preview failed')
        with pytest.raises(psycopg.OperationalError):
            db.execute('SELECT 1')
        with pytest.raises(psycopg.OperationalError):
            psycopg.connect(**cfg)
    finally:
        if db:
            db.close()


def test_open_report_failure_still_revokes_preview_login(recovery_role, monkeypatch):
    run, params, record, directory = recovery_role
    result = run(preview=True)
    save = preview_access.save_private
    def fail(path, value):
        if value.get('state') == 'open':
            raise OSError('disk disconnected')
        save(path, value)
    monkeypatch.setattr(preview_access, 'save_private', fail)
    with pytest.raises(OSError, match='disconnected'):
        with preview_access.preview_database_access(*arguments(params, record, directory),
                expected_cluster_directory=Path(record['cluster_directory'])):
            pytest.fail('Must not expose an unjournaled open preview')
    with psycopg.connect(**params) as db:
        assert not db.execute('SELECT rolcanlogin FROM pg_roles WHERE rolname=%s', (result['role'],)).fetchone()[0]


def test_edit_credentials_cannot_open_preview_access(recovery_role):
    run, params, record, directory = recovery_role
    run()
    with pytest.raises(InvalidArtifact):
        with preview_access.preview_database_access(*arguments(params, record, directory),
                expected_cluster_directory=Path(record['cluster_directory'])):
            pytest.fail('Edit credentials are not preview credentials')


def test_explicit_cleanup_is_repeatable_for_owned_preview_role(recovery_role):
    run, params, record, directory = recovery_role
    run(preview=True)
    for _ in range(2):
        result = preview_access.close_preview_access(*arguments(params, record, directory),
                 expected_cluster_directory=Path(record['cluster_directory']))
        assert result == {'state': 'closed', 'activation_allowed': False, 'services_started': False}


def test_preview_can_reopen_after_clean_close_without_resuming_jobs(recovery_role):
    run, params, record, directory = recovery_role
    run(preview=True)
    for _ in range(2):
        with preview_access.preview_database_access(*arguments(params, record, directory),
                expected_cluster_directory=Path(record['cluster_directory'])) as cfg:
            with psycopg.connect(**cfg) as db:
                assert db.execute('SELECT rolvaliduntil>now() AND rolvaliduntil<=now()+interval \'1 hour\' '
                                  'FROM pg_roles WHERE rolname=current_user').fetchone()[0]


def test_cleanup_refuses_mixed_recovery_identity(recovery_role):
    run, params, record, directory = recovery_role
    result = run(preview=True)
    bad_record = {**record, 'ownership_token': '0' * 32}
    with pytest.raises(InvalidArtifact):
        preview_access.close_preview_access(*arguments(params, bad_record, directory),
                 expected_cluster_directory=Path(record['cluster_directory']))
    with psycopg.connect(**params) as db:
        assert not db.execute('SELECT rolcanlogin FROM pg_roles WHERE rolname=%s', (result['role'],)).fetchone()[0]
