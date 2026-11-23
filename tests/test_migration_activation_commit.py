import json
import os
from pathlib import Path

import psycopg
import pytest

from app.backup.admission import MaintenanceBlocked, participant
from app.backup.postgres import Endpoint
from app.backup.restore_database import RestoreFailed
from app.migration.activation import ACCEPTANCE_CHECKS
from app.migration.activation_commit import commit_activation
from tests.test_backup_postgres import ample, postgres  # noqa: F401
from tests.test_migration_disk import relocation  # noqa: F401
from tests.test_migration_validation import candidate, reads  # noqa: F401


def binding(path):
    return {'root': str(path), 'drive': 'fixture', 'serial': 'serial', 'volume_serial': 'volume', 'volume_id': 'id'}


@pytest.fixture
def activation(candidate, tmp_path):
    validate, target, role, _ = candidate
    output = tmp_path / 'activation-intent'
    validate.activation(output, accepted=ACCEPTANCE_CHECKS, source_unavailable=True,
                        hostname='new-host.local', port=4340, probe=binding)
    credentials = json.loads((role / 'credentials.json').read_text())
    host, port, user = credentials['endpoint']
    admin = Endpoint(host, port, 'postgres', user)
    def commit(**kwargs):
        return commit_activation(output, validate.key, admin, offline_confirmed=True,
                                 probe=binding, guard_factory=ample, **kwargs)
    return commit, target, output


def test_commit_enables_only_reviewed_target_and_preserves_pauses(activation):
    commit, target, output = activation
    report = commit()
    assert report['state'] == 'activated'
    assert report['jobs_resumed'] is report['services_started'] is False
    cfg = json.loads((target / 'catalog/files/config/local.json').read_text())
    assert not cfg['recovery']['activation_required']
    with psycopg.connect(**cfg['database']) as db:
        assert db.execute('SELECT count(*) FROM media').fetchone()[0] == 1
        assert db.execute('SHOW default_transaction_read_only').fetchone()[0] == 'off'
        db.rollback()
    with participant(target / 'catalog/files/data'):
        pass
    assert (output / 'write-access-fence/complete.fernet').is_file()
    with pytest.raises(MaintenanceBlocked):
        commit(resume=True, rollback=True)


def test_failed_configuration_replace_can_resume_without_adopting_temporary_file(activation, monkeypatch):
    commit, target, _ = activation
    destination = target / 'catalog/files/config/local.json'
    replace = os.replace
    def fail(source, path):
        if Path(path) == destination:
            raise OSError('simulated configuration publication failure')
        return replace(source, path)
    with monkeypatch.context() as patch:
        patch.setattr(os, 'replace', fail)
        with pytest.raises(OSError, match='publication'):
            commit()
    remnants = list(destination.parent.glob('local.json.*.part'))
    assert len(remnants) == 1
    before = remnants[0].read_bytes()
    assert commit(resume=True)['state'] == 'activated'
    assert remnants[0].read_bytes() == before  # preserved, never treated as committed settings


@pytest.mark.parametrize('phase', ['configuration-published', 'write-access-fenced', 'database-committed'])
def test_interrupted_activation_finishes_idempotently(activation, phase):
    commit, target, _ = activation
    def interrupt(current):
        if current == phase:
            raise RuntimeError('simulated power loss')
    with pytest.raises(RuntimeError, match='simulated'):
        commit(checkpoint=interrupt)
    with pytest.raises(MaintenanceBlocked):
        with participant(target / 'catalog/files/data'):
            pass
    assert commit(resume=True)['state'] == 'activated'
    with pytest.raises(MaintenanceBlocked, match='released'):
        commit(resume=True)


def test_rollback_before_write_fence_restores_disabled_configuration(activation):
    commit, target, _ = activation
    path = target / 'catalog/files/config/local.json'
    original = path.read_bytes()
    def interrupt(phase):
        if phase == 'configuration-published':
            raise RuntimeError('interrupted')
    with pytest.raises(RuntimeError):
        commit(checkpoint=interrupt)
    assert commit(resume=True, rollback=True)['state'] == 'rolled-back'
    assert path.read_bytes() == original
    assert not (target / 'media-copy/.familyfoto-volume.json').exists()


def test_fenced_interruption_refuses_rollback_even_before_login_commits(activation):
    commit, _, _ = activation
    def interrupt(phase):
        if phase == 'write-access-fenced':
            raise RuntimeError('interrupted')
    with pytest.raises(RuntimeError):
        commit(checkpoint=interrupt)
    with pytest.raises(MaintenanceBlocked, match='may have been enabled'):
        commit(resume=True, rollback=True)
    assert commit(resume=True)['state'] == 'activated'


def test_changed_configuration_is_not_overwritten_during_recovery(activation):
    commit, target, _ = activation
    def interrupt(phase):
        if phase == 'configuration-published':
            raise RuntimeError('interrupted')
    with pytest.raises(RuntimeError):
        commit(checkpoint=interrupt)
    path = Path(target) / 'catalog/files/config/local.json'
    path.write_text('{}')
    with pytest.raises(MaintenanceBlocked, match='independently changed'):
        commit(resume=True)
    assert path.read_text() == '{}'


def test_missing_fence_cannot_rollback_committed_database(activation):
    commit, target, output = activation
    def interrupt(phase):
        if phase == 'database-committed':
            raise RuntimeError('interrupted')
    with pytest.raises(RuntimeError):
        commit(checkpoint=interrupt)
    path = target / 'catalog/files/config/local.json'
    activated = path.read_bytes()
    # Simulate someone removing the fence without altering the database marker.
    (output / 'write-access-fence').rename(output / 'removed-fence')
    with pytest.raises(RestoreFailed, match='ownership or sanitization'):
        commit(resume=True, rollback=True)
    assert path.read_bytes() == activated
    with psycopg.connect(**json.loads(activated)['database']) as db:
        assert db.execute('SHOW default_transaction_read_only').fetchone()[0] == 'off'
