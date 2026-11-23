import json
from pathlib import Path
import uuid

import psycopg
import pytest

from app.backup.admission import maintenance
from app.backup.catalog import freeze_database
from app.backup.postgres import Endpoint
from app.backup.restore_database import RestoreFailed
from app.migration.activation_database import activate_database, rollback_database
from app.migration.credentials import owned_target, verify_staged_role
from tests.test_backup_postgres import postgres  # noqa: F401
from tests.test_migration_credentials import recovery_role  # noqa: F401


@pytest.fixture
def activation(recovery_role, tmp_path):
    prepare, params, database, directory = recovery_role
    report = prepare()
    identity = json.loads((directory / 'credentials.json').read_text())
    cluster = Path(database['cluster_directory'])
    data = tmp_path / 'data'
    data.mkdir()
    endpoint = Endpoint(params['host'], params['port'], database['database'], params['user'])
    intent = uuid.uuid4().hex

    def change(function, gate, *, fail=False, intent_id=intent):
        with freeze_database(endpoint) as db:
            function(db, database, identity, report, cluster, intent_id, data_root=data, maintenance_id=gate)
            if fail:
                raise RuntimeError('Simulated interruption before commit')

    def verify_disabled():
        with freeze_database(endpoint) as db:
            owned_target(db, database, cluster)
            verify_staged_role(db, identity, report)
    yield change, verify_disabled, data, params, database, identity


def test_activation_and_rollback_are_owned_and_transactional(activation):
    change, disabled, data, params, database, identity = activation
    with maintenance(data, 'restore') as gate:
        change(activate_database, gate)
        with psycopg.connect(**{**params, 'dbname': database['database'], 'user': identity['role'],
                               'password': identity['password']}) as db:
            assert db.execute('SHOW default_transaction_read_only').fetchone()[0] == 'off'
            db.execute("INSERT INTO media(name) VALUES('permission probe, rolled back')")
            db.rollback()
        change(rollback_database, gate)
        disabled()
        change(rollback_database, gate)  # A repeated rollback is harmless while gated.
    with pytest.raises((RestoreFailed, OSError)):
        change(activate_database, gate)
    disabled()


def test_failed_transaction_does_not_partially_enable_role(activation):
    change, disabled, data, *_ = activation
    with maintenance(data, 'restore') as gate:
        with pytest.raises(RuntimeError, match='Simulated'):
            change(activate_database, gate, fail=True)
        disabled()


def test_another_intent_cannot_rollback_or_modify_activation(activation):
    change, disabled, data, *_ = activation
    with maintenance(data, 'restore') as gate:
        change(activate_database, gate)
        with pytest.raises(RestoreFailed, match='does not belong'):
            change(rollback_database, gate, intent_id=uuid.uuid4().hex)
        change(rollback_database, gate)
        disabled()


def test_released_gate_cannot_rollback_activation(activation):
    change, _, data, *_ = activation
    with maintenance(data, 'restore') as gate:
        change(activate_database, gate)
    with pytest.raises((RestoreFailed, OSError)):
        change(rollback_database, gate)


def test_active_client_prevents_database_transition(activation):
    change, disabled, data, params, database, _ = activation
    with maintenance(data, 'restore') as gate:
        with psycopg.connect(**{**params, 'dbname': database['database']}):
            with pytest.raises(RuntimeError, match='[Cc]lient|[Ss]top|[Cc]onnection|[Ss]ession'):
                change(activate_database, gate)
        disabled()
