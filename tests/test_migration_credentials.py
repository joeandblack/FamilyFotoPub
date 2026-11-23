import json
from pathlib import Path
import uuid

import psycopg
from psycopg import sql
import pytest

from app.backup.postgres import Endpoint
from app.backup.restore_database import RestoreFailed
from app.migration import credentials
from tests.test_backup_postgres import postgres  # noqa: F401


@pytest.fixture
def recovery_role(postgres, tmp_path):
    params, _, _, _, _ = postgres
    name, token = 'familyfoto_recovery_' + uuid.uuid4().hex, uuid.uuid4().hex
    directory = tmp_path / 'credentials'
    with psycopg.connect(**params, autocommit=True) as admin:
        cluster = Path(admin.execute('SHOW data_directory').fetchone()[0]).resolve()
        admin.execute(sql.SQL('CREATE DATABASE {} TEMPLATE template0').format(sql.Identifier(name)))
        try:
            admin.execute(sql.SQL('ALTER DATABASE {} SET default_transaction_read_only=on').format(sql.Identifier(name)))
            admin.execute(sql.SQL('REVOKE CONNECT ON DATABASE {} FROM PUBLIC').format(sql.Identifier(name)))
            admin.execute(sql.SQL('COMMENT ON DATABASE {} IS {}').format(
                sql.Identifier(name), sql.Literal('familyfoto-recovery:' + token + ':sanitized')))
            oid = admin.execute('SELECT oid FROM pg_database WHERE datname=%s', (name,)).fetchone()[0]
            with psycopg.connect(**{**params, 'dbname': name, 'options': '-c default_transaction_read_only=off'}) as db:
                db.execute('CREATE TABLE media(id bigserial PRIMARY KEY, name text)')
                db.execute("INSERT INTO media(name) VALUES('fixture')")
                db.execute("CREATE FUNCTION fixture_count() RETURNS bigint LANGUAGE sql AS 'SELECT count(*) FROM media'")
                db.execute('GRANT CREATE ON SCHEMA public TO PUBLIC')
            record = {'database': name, 'database_oid': oid, 'ownership_token': token,
                      'cluster_directory': str(cluster), 'verified': True, 'activation_allowed': False}
            endpoint = Endpoint(params['host'], params['port'], 'postgres', params['user'])
            def run(**kwargs):
                return credentials.prepare_recovery_role(endpoint, record, directory,
                                                        expected_cluster_directory=cluster, **kwargs)
            yield run, params, record, directory
        finally:
            admin.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))
            journal = directory / 'credentials.json'
            if journal.exists():
                role = json.loads(journal.read_text())['role']
                assert role.startswith('familyfoto_app_') and len(role) == 47
                admin.execute(sql.SQL('DROP ROLE IF EXISTS {}').format(sql.Identifier(role)))


def test_recovery_role_is_restricted_and_cannot_login(recovery_role):
    run, params, record, directory = recovery_role
    result = run()
    identity = json.loads((directory / 'credentials.json').read_text())
    assert result['state'] == 'staged-no-login' and not result['activation_allowed']
    assert identity['password'] not in json.dumps(result)
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(**{**params, 'dbname': record['database'], 'user': identity['role'],
                           'password': identity['password'], 'connect_timeout': 3})
    with psycopg.connect(**{**params, 'dbname': record['database']}) as db:
        assert db.execute('SELECT has_table_privilege(%s,%s,%s)', (identity['role'], 'media', 'INSERT')).fetchone()[0]
        assert db.execute('SELECT has_sequence_privilege(%s,%s,%s)', (identity['role'], 'media_id_seq', 'USAGE')).fetchone()[0]
        assert db.execute('SELECT has_function_privilege(%s,%s,%s)', (identity['role'], 'fixture_count()', 'EXECUTE')).fetchone()[0]
        assert not db.execute('SELECT has_schema_privilege(%s,%s,%s)', (identity['role'], 'public', 'CREATE')).fetchone()[0]
        assert db.execute('SHOW default_transaction_read_only').fetchone()[0] == 'on'
    assert run(resume=True) == result
    assert json.loads((directory / 'credentials.json').read_text()) == identity


def test_preview_role_has_no_effective_writes_even_if_transaction_readonly_disabled(recovery_role):
    run, params, record, directory = recovery_role
    result = run(preview=True)
    assert result['access'] == 'preview-select-only'
    assert run(preview=True, resume=True) == result
    with pytest.raises(RestoreFailed):
        run(resume=True)
    with psycopg.connect(**{**params, 'dbname': record['database'], 'options': '-c default_transaction_read_only=off'}) as db:
        credentials.verify_preview_privileges(db, result['role'], result['schemas'])
        identity = json.loads((directory / 'credentials.json').read_text())
        assert credentials.verify_staged_role(db, identity, result)['select_only']
        db.execute(sql.SQL('SET ROLE {}').format(sql.Identifier(result['role'])))
        assert db.execute('SELECT name FROM media').fetchone()[0] == 'fixture'
        for statement in ("INSERT INTO media(name) VALUES('wrong')", 'DELETE FROM media',
                          'TRUNCATE media', 'CREATE TEMP TABLE unexpected(id int)',
                          "SELECT nextval('media_id_seq')", 'SELECT fixture_count()'):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with db.transaction():
                    db.execute(statement)
    identity = json.loads((directory / 'credentials.json').read_text())
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(**{**params, 'dbname': record['database'], 'user': identity['role']})


@pytest.mark.parametrize('grant', ['table', 'routine', 'sequence', 'temporary'])
def test_preview_role_detects_public_privilege_drift(recovery_role, grant):
    run, params, record, directory = recovery_role
    report = run(preview=True)
    identity = json.loads((directory / 'credentials.json').read_text())
    with psycopg.connect(**{**params, 'dbname': record['database'], 'options': '-c default_transaction_read_only=off'}) as db:
        if grant == 'table':
            db.execute('GRANT UPDATE ON media TO PUBLIC')
        elif grant == 'routine':
            db.execute('GRANT EXECUTE ON FUNCTION fixture_count() TO PUBLIC')
        elif grant == 'sequence':
            db.execute('GRANT USAGE ON SEQUENCE media_id_seq TO PUBLIC')
        else:
            db.execute(sql.SQL('GRANT TEMP ON DATABASE {} TO PUBLIC').format(sql.Identifier(record['database'])))
        with pytest.raises(RestoreFailed):
            credentials.verify_staged_role(db, identity, report)


@pytest.mark.parametrize('definer', [False, True])
def test_preview_visibility_helper_requires_invoker_security(recovery_role, definer):
    run, params, record, directory = recovery_role
    with psycopg.connect(**{**params, 'dbname': record['database'], 'options': '-c default_transaction_read_only=off'}) as db:
        db.execute("CREATE FUNCTION familyfoto_can_see(text,text) RETURNS boolean LANGUAGE sql "
                   + ('SECURITY DEFINER ' if definer else '') + "AS 'SELECT true'")
    if definer:
        with pytest.raises(RestoreFailed, match='caller privileges'):
            run(preview=True)
    else:
        result = run(preview=True)
        with psycopg.connect(**{**params, 'dbname': record['database']}) as db:
            db.execute(sql.SQL('SET ROLE {}').format(sql.Identifier(result['role'])))
            assert db.execute("SELECT familyfoto_can_see('test','shared')").fetchone()[0]


def test_report_failure_resumes_same_disabled_role(recovery_role, monkeypatch):
    run, _, _, directory = recovery_role
    save = credentials.save_private
    def fail_report(path, value):
        if path.name == 'ROLE-REPORT.json':
            raise OSError('Disconnected')
        return save(path, value)
    monkeypatch.setattr(credentials, 'save_private', fail_report)
    with pytest.raises(OSError):
        run()
    before = json.loads((directory / 'credentials.json').read_text())
    monkeypatch.setattr(credentials, 'save_private', save)
    result = run(resume=True)
    assert result['role'] == before['role']
    assert json.loads((directory / 'credentials.json').read_text()) == before


def test_role_transaction_failure_leaves_retryable_intent(recovery_role, monkeypatch):
    run, params, _, directory = recovery_role
    stage = credentials.stage_role
    def fail(db, identity):
        stage(db, identity)
        raise OSError('Interrupted before commit')
    monkeypatch.setattr(credentials, 'stage_role', fail)
    with pytest.raises(OSError):
        run()
    role = json.loads((directory / 'credentials.json').read_text())['role']
    with psycopg.connect(**params) as db:
        assert db.execute('SELECT 1 FROM pg_roles WHERE rolname=%s', (role,)).fetchone() is None
    monkeypatch.setattr(credentials, 'stage_role', stage)
    assert run(resume=True)['role'] == role


@pytest.mark.parametrize('change', ['login', 'ownership'])
def test_changed_role_or_database_refuses_retry(recovery_role, change):
    run, params, record, directory = recovery_role
    result = run()
    with psycopg.connect(**params) as db:
        if change == 'login':
            db.execute(sql.SQL('ALTER ROLE {} LOGIN').format(sql.Identifier(result['role'])))
        else:
            db.execute(sql.SQL("COMMENT ON DATABASE {} IS 'foreign'").format(sql.Identifier(record['database'])))
    with pytest.raises(RestoreFailed):
        run(resume=True)


@pytest.mark.parametrize('change', ['none', 'table', 'sequence', 'function', 'schema', 'login'])
def test_readonly_role_verifier_checks_actual_grants(recovery_role, change):
    run, params, record, directory = recovery_role
    report = run()
    identity = json.loads((directory / 'credentials.json').read_text())
    with psycopg.connect(**{**params, 'dbname': record['database'],
                           'options': '-c default_transaction_read_only=off'}) as db:
        role = sql.Identifier(identity['role'])
        if change == 'table':
            db.execute(sql.SQL('REVOKE UPDATE ON media FROM {}').format(role))
        elif change == 'sequence':
            db.execute(sql.SQL('REVOKE USAGE ON media_id_seq FROM {}').format(role))
        elif change == 'function':
            db.execute('REVOKE EXECUTE ON FUNCTION fixture_count() FROM PUBLIC')
            db.execute(sql.SQL('REVOKE EXECUTE ON FUNCTION fixture_count() FROM {}').format(role))
        elif change == 'schema':
            db.execute(sql.SQL('GRANT CREATE ON SCHEMA public TO {}').format(role))
        elif change == 'login':
            db.execute(sql.SQL('ALTER ROLE {} LOGIN').format(role))
    with psycopg.connect(**{**params, 'dbname': record['database']}) as db:
        assert db.execute('SHOW transaction_read_only').fetchone()[0] == 'on'
        if change == 'none':
            assert credentials.verify_staged_role(db, identity, report)['login_disabled']
        else:
            with pytest.raises(RestoreFailed):
                credentials.verify_staged_role(db, identity, report)
