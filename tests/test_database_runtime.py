"""Request setup and schema upgrade regressions; DB tests use disposable schemas."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import time
import uuid
from unittest.mock import patch

import psycopg
from psycopg import sql
import pytest

from app import api, config
from app.database import pool as database_pool
from app.database import schema_upgrading
from app.auth import api as auth_api
from routers import CONFIG_MODULES, DATA_MODULES, patch_all
from app.auth import password_hash


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    for name in ('ROOT', 'DATA', 'LOGS', 'TEMP'):
        monkeypatch.setattr(config, name, tmp_path if name == 'ROOT' else tmp_path/name.lower())
    for name in ('_cached_signature', '_cached_config', '_initialized_directories'):
        monkeypatch.setattr(config, name, None)
    path = tmp_path/'config'/'local.json'
    path.parent.mkdir()
    settings = {'source': {'root': str(tmp_path/'source')},
                'destination': {'root': str(tmp_path/'destination')},
                'host': '0.0.0.0', 'browser_user': 'fixture', 'browser_password_hash': password_hash('first'),
                'auth_secret':'fixture-secret-'*4}
    path.write_text(json.dumps(settings), encoding='utf-8')
    return path, settings


def test_settings_cached_and_defensively_copied(settings_file):
    read, mkdir = Path.read_text, Path.mkdir
    with patch.object(Path, 'read_text', autospec=True, side_effect=read) as reads, \
            patch.object(Path, 'mkdir', autospec=True, side_effect=mkdir) as directories:
        first = config.load_config()
        initial_mkdirs = directories.call_count
        first['source']['root'] = 'mutated'
        for _ in range(10):
            assert config.load_config()['source']['root'] != 'mutated'
        assert reads.call_count == 1
        assert directories.call_count == initial_mkdirs


def test_settings_reload_and_reject_invalid_replacement(settings_file):
    path, settings = settings_file
    assert config.load_config()['browser_password_hash'] == settings['browser_password_hash']
    settings['browser_password_hash'] = password_hash('replacement')
    replacement = path.with_suffix('.new')
    replacement.write_text(json.dumps(settings), encoding='utf-8')
    replacement.replace(path)
    assert config.load_config()['browser_password_hash'] == settings['browser_password_hash']
    path.write_text('{invalid', encoding='utf-8')
    with pytest.raises(json.JSONDecodeError):
        config.load_config()
    settings['destination'] = settings['source']
    path.write_text(json.dumps(settings), encoding='utf-8')
    with pytest.raises(ValueError, match='separate trees'):
        config.load_config()


def test_cached_settings_revoke_session_and_verify_once(settings_file, monkeypatch,auth_memory):
    from app.auth import session_token, session_user
    from fastapi.testclient import TestClient
    path, settings = settings_file
    patch_all(monkeypatch, CONFIG_MODULES, 'load_config', config.load_config)
    client = TestClient(api.app)
    client.cookies.set('familyfoto_session', session_token(settings, int(time.time())+60))
    with patch.object(auth_api, 'session_user', wraps=session_user) as verify:
        assert client.get('/api/me').status_code == 200
        assert verify.call_count == 1
        settings['browser_password_hash'] = password_hash('changed-password')
        path.write_text(json.dumps(settings), encoding='utf-8')
        assert client.get('/api/me').status_code == 401
        assert verify.call_count == 2


@pytest.mark.parametrize('names', [[], ['002_gap.sql'], ['001_a.sql','001_b.sql'], ['bad.sql']])
def test_migration_names_must_be_consecutive(tmp_path, names):
    for name in names:
        (tmp_path/name).touch()
    with pytest.raises(ValueError):
        schema_upgrading.discover(tmp_path)


def test_current_schema_never_acquires_migration_or_worker_locks(tmp_path):
    (tmp_path/'001_initial.sql').write_text('MUST NOT EXECUTE', encoding='utf-8')
    class DB:
        def execute(self, query):
            assert query.startswith('SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_class') or query == 'SELECT version FROM schema_versions'
            return self
        def fetchone(self):
            return {'present': True}
        def fetchall(self):
            return [{'version': 1}]
    @contextmanager
    def connect():
        yield DB()
    assert schema_upgrading.migrate(connect, tmp_path) == []


def test_default_upgrade_paths_use_databases_folder(monkeypatch):
    from app import database
    upgrades = schema_upgrading.discover(schema_upgrading.ROOT / 'databases')
    assert upgrades[0].path.name == '001_initial.sql'
    assert len(upgrades) >= 34

    class DB:
        def execute(self, query):
            assert query.startswith('SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_class') or query == 'SELECT version FROM schema_versions'
            return self

        def fetchone(self):
            return {'present': True}

        def fetchall(self):
            return [{'version': item.version} for item in upgrades]

    @contextmanager
    def connect():
        yield DB()

    monkeypatch.setattr(database, 'connection', connect)
    assert schema_upgrading.migrate() == []
    assert database.migrate() == []


@pytest.fixture
def isolated_db():
    if os.environ.get('FAMILYFOTO_INTEGRATION') != '1':
        pytest.skip('Requires installed local PostgreSQL')
    settings = config.load_config()['database']
    schema = 'runtimetest_' + uuid.uuid4().hex
    with psycopg.connect(**settings) as admin:
        admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    local = {**settings, 'options': f'-c search_path={schema}'}
    try:
        yield settings, local, schema
    finally:
        database_pool.close_pools()
        with psycopg.connect(**settings) as admin:
            admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def test_pool_reuses_connection_commits_rolls_back_and_resets(isolated_db):
    settings, local, schema = isolated_db
    connect = lambda: database_pool.pooled_connection(local)
    lock = uuid.uuid4().int % (2**63-1)
    with connect() as db:
        pid = db.info.backend_pid
        db.execute('CREATE TABLE fixture(value int)')
        db.execute('INSERT INTO fixture VALUES (1)')
        db.commit()
        db.execute('SELECT pg_advisory_lock(%s)', (lock,))
        db.execute('CREATE TEMP TABLE temporary_fixture(value int)')
        db.execute('SET search_path TO pg_catalog')
        db.execute("SET statement_timeout='1234ms'")
    # A separate connection must acquire this immediately, not after async reset.
    with psycopg.connect(**settings) as other:
        assert other.execute('SELECT pg_try_advisory_xact_lock(%s)', (lock,)).fetchone()[0]
    with connect() as db:
        assert db.info.backend_pid == pid
        assert db.execute('SELECT current_schema() name').fetchone()['name'] == schema
        assert db.execute("SELECT to_regclass('pg_temp.temporary_fixture') name").fetchone()['name'] is None
        assert db.execute('SHOW statement_timeout').fetchone()['statement_timeout'] != '1234ms'
        assert db.execute('SELECT value FROM fixture').fetchone()['value'] == 1
    with pytest.raises(RuntimeError, match='rollback'):
        with connect() as db:
            db.execute('INSERT INTO fixture VALUES (2)')
            raise RuntimeError('rollback')
    with connect() as db:
        assert db.execute('SELECT count(*) n FROM fixture').fetchone()['n'] == 1


def test_pool_settings_isolation_broken_connection_and_bound(isolated_db, monkeypatch):
    _, local, schema = isolated_db
    alternate = {**local, 'options': '-c search_path=pg_catalog'}
    with database_pool.pooled_connection(local) as first:
        with database_pool.pooled_connection(alternate) as second:
            assert first.info.backend_pid != second.info.backend_pid
            assert first.execute('SELECT current_schema() name').fetchone()['name'] == schema
            assert second.execute('SELECT current_schema() name').fetchone()['name'] == 'pg_catalog'
        old_pid = first.info.backend_pid
        first.close()
    with database_pool.pooled_connection(local) as db:
        assert db.info.backend_pid != old_pid
    database_pool.close_pools()
    monkeypatch.setattr(database_pool, 'MAX_CONNECTIONS', 1)
    monkeypatch.setattr(database_pool, 'CHECKOUT_TIMEOUT', 0.05)
    from psycopg_pool import PoolTimeout
    with database_pool.pooled_connection(local):
        with pytest.raises(PoolTimeout):
            with database_pool.pooled_connection(local):
                pytest.fail('Exceeded pool size')


def test_failed_reset_discards_connection(isolated_db, monkeypatch):
    _, local, _ = isolated_db
    reset = database_pool._reset
    def fail_reset(db):
        raise RuntimeError('simulated reset failure')
    monkeypatch.setattr(database_pool, '_reset', fail_reset)
    with database_pool.pooled_connection(local) as db:
        pid = db.info.backend_pid
        db.execute('CREATE TABLE committed_fixture(value int)')
    monkeypatch.setattr(database_pool, '_reset', reset)
    with database_pool.pooled_connection(local) as db:
        assert db.info.backend_pid != pid
        assert db.execute("SELECT to_regclass('committed_fixture') name").fetchone()['name']


@pytest.mark.parametrize('_attempt', range(3))
def test_concurrent_startup_applies_migration_once(isolated_db, tmp_path, _attempt):
    from concurrent.futures import ThreadPoolExecutor
    _, local, _ = isolated_db
    connect = lambda: database_pool.pooled_connection(local)
    (tmp_path/'001_initial.sql').write_text(
        'SELECT pg_sleep(0.15); CREATE TABLE schema_versions(version int PRIMARY KEY); '
        'INSERT INTO schema_versions VALUES (1);', encoding='utf-8')
    with ThreadPoolExecutor(max_workers=2) as executor:
        # This fixture uses a schema inside a populated test database. Its
        # isolated SQL-engine test supplies admission, not a production upgrade.
        pending = [executor.submit(schema_upgrading.migrate, connect, tmp_path,
                                   before_upgrade=lambda *args: None) for _ in range(2)]
        assert sorted(future.result(timeout=10) for future in pending) == [[], [1]]


def test_migrations_fresh_upgrade_rollback_and_worker_guard(isolated_db, tmp_path, monkeypatch):
    _, local, _ = isolated_db
    connect = lambda: database_pool.pooled_connection(local)
    first = tmp_path/'001_initial.sql'
    first.write_text('CREATE TABLE schema_versions(version int PRIMARY KEY); '
                     'INSERT INTO schema_versions VALUES (1);', encoding='utf-8')
    admitted = []
    def fixture_admission(db, applied, pending):
        admitted.append([item.version for item in pending])
    assert schema_upgrading.migrate(connect, tmp_path, before_upgrade=fixture_admission) == [1]
    second = tmp_path/'002_upgrade.sql'
    second.write_text('CREATE TABLE upgrade_fixture(value int); '
                      'INSERT INTO schema_versions VALUES (2);', encoding='utf-8')
    lock = uuid.uuid4().int % (2**63-1)
    monkeypatch.setattr(schema_upgrading.Migration, 'guards', lambda self: (lock,))
    with psycopg.connect(**local) as other:
        other.execute('SELECT pg_advisory_xact_lock(%s)', (lock,))
        with pytest.raises(RuntimeError, match='Pause media operations'):
            schema_upgrading.migrate(connect, tmp_path)
    with connect() as db:
        assert db.execute("SELECT to_regclass('upgrade_fixture') name").fetchone()['name'] is None
    with pytest.raises(RuntimeError, match='verified offline backup'):
        schema_upgrading.migrate(connect, tmp_path)
    assert schema_upgrading.migrate(connect, tmp_path, before_upgrade=fixture_admission) == [2]
    assert admitted[-1] == [2]
    # An up-to-date schema must not contend with an active worker.
    with psycopg.connect(**local) as other:
        other.execute('SELECT pg_advisory_xact_lock(%s)', (lock,))
        assert schema_upgrading.migrate(connect, tmp_path) == []
    third = tmp_path/'003_failed.sql'
    third.write_text('CREATE TABLE failed_fixture(value int); SELECT 1/0;', encoding='utf-8')
    with pytest.raises(psycopg.errors.DivisionByZero):
        schema_upgrading.migrate(connect, tmp_path, before_upgrade=fixture_admission)
    with connect() as db:
        assert db.execute("SELECT to_regclass('failed_fixture') name").fetchone()['name'] is None
        db.execute('INSERT INTO schema_versions VALUES (999)')
    with pytest.raises(RuntimeError, match='newer'):
        schema_upgrading.migrate(connect, tmp_path)
