from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
import pytest

from app.backup import upgrade as module
from app.backup.admission import MaintenanceBlocked, participant
from app.backup.catalog import verify_catalog
from app.backup.encrypted import verify_artifact
from app.backup.postgres import Endpoint
from app.database.schema_upgrading import migrate, require_catalog_backup
from tests.test_backup_postgres import postgres, ample  # noqa: F401
from tests.backup_keys import backup_key


@pytest.fixture
def upgrade_case(postgres, tmp_path):
    params, source, _, dump, _ = postgres
    endpoint = Endpoint(params['host'], params['port'], source, params['user'])
    config, data, scripts = (tmp_path / name for name in ('config', 'data', 'scripts'))
    for path in (config, data, scripts):
        path.mkdir()
    (config / 'local.json').write_text('{}')
    (scripts / '001_initial.sql').write_text('CREATE TABLE schema_versions(version integer PRIMARY KEY); '
        'CREATE TABLE media(id integer PRIMARY KEY); INSERT INTO schema_versions VALUES(1);')
    key, backup = backup_key(), tmp_path / 'backup'

    @contextmanager
    def connect():
        options = endpoint.connection()
        options['options'] = '-c default_transaction_read_only=off'
        with psycopg.connect(**options, row_factory=dict_row) as db:
            yield db

    def run():
        return module.upgrade_catalog(endpoint, dump, config, data, backup, key,
            migrations_directory=scripts, database_budget=1024 ** 2,
            offline_confirmed=True, catalog_only_confirmed=True, guard_factory=ample)

    return run, connect, scripts, data, backup, key


def add_upgrade(scripts):
    (scripts / '002_upgrade.sql').write_text('ALTER TABLE media ADD COLUMN note text; '
                                            'INSERT INTO schema_versions VALUES(2);')


def test_fresh_and_current_schema_do_not_create_backup(upgrade_case):
    run, connect, scripts, data, backup, _ = upgrade_case
    assert run() == {'state': 'complete', 'applied': [1], 'backup_created': False}
    assert not backup.exists()
    assert run() == {'state': 'unchanged', 'applied': [], 'backup_created': False}
    assert not backup.exists()
    assert not (data / '.recovery-admission/maintenance.json').exists()


def test_read_only_plan_never_creates_admission_or_backup(upgrade_case, postgres):
    _, _, scripts, data, backup, _ = upgrade_case
    params, source, _, _, _ = postgres
    endpoint = Endpoint(params['host'], params['port'], source, params['user'])
    plan = module.plan_upgrade(endpoint, scripts)
    assert plan['pending_versions'] == [1] and not plan['backup_required']
    assert not (data / '.recovery-admission').exists()
    assert not backup.exists()


def test_existing_upgrade_has_verified_old_catalog_and_blocks_writers(upgrade_case, monkeypatch):
    run, connect, scripts, data, backup, key = upgrade_case
    run()
    add_upgrade(scripts)
    with pytest.raises(RuntimeError, match='verified offline backup'):
        migrate(connect, scripts)
    original = module.migrate

    def checked(*args, **kwargs):
        assert verify_catalog(backup, key)['media_included'] is False
        with pytest.raises((MaintenanceBlocked, OSError)), participant(data):
            pytest.fail('Writer admitted during upgrade')
        return original(*args, **kwargs)

    monkeypatch.setattr(module, 'migrate', checked)
    report = run()
    assert report['backup_created'] and report['applied'] == [2]
    assert not report['jobs_resumed']
    assert verify_artifact(backup / 'database', key)['metadata']['compatibility']['schema_histories']['public'] == [1]
    with connect() as db:
        assert {row['version'] for row in db.execute('SELECT version FROM schema_versions')} == {1, 2}
    assert not (data / '.recovery-admission/maintenance.json').exists()


@pytest.mark.parametrize('failure', ['capture', 'backup', 'sql', 'changed-script'])
def test_failed_upgrade_keeps_old_schema_and_maintenance_gate(upgrade_case, monkeypatch, failure):
    run, connect, scripts, data, backup, key = upgrade_case
    run()
    add_upgrade(scripts)
    if failure == 'capture':
        from app.backup import catalog
        monkeypatch.setattr(catalog, 'capture_components', lambda *args, **kwargs:
                            (_ for _ in ()).throw(OSError('Disk disconnected')))
    elif failure == 'backup':
        monkeypatch.setattr(module, 'verify_catalog', lambda *args: (_ for _ in ()).throw(ValueError('bad backup')))
    elif failure == 'sql':
        (scripts / '002_upgrade.sql').write_text('ALTER TABLE media ADD COLUMN note text; SELECT 1/0;')
    else:
        original = module.migrate

        def changed(*args, **kwargs):
            (scripts / '002_upgrade.sql').write_text('DROP TABLE media;')
            return original(*args, **kwargs)

        monkeypatch.setattr(module, 'migrate', changed)
    with pytest.raises((ValueError, MaintenanceBlocked, psycopg.Error, OSError)):
        run()
    with connect() as db:
        assert [row['version'] for row in db.execute('SELECT version FROM schema_versions')] == [1]
        assert db.execute("SELECT to_regclass('public.media') AS name").fetchone()['name'] == 'media'
        assert not db.execute("SELECT 1 FROM information_schema.columns WHERE table_name='media' AND column_name='note'").fetchone()
    assert (data / '.recovery-admission/maintenance.json').exists()
    if failure != 'capture':
        assert verify_catalog(backup, key)


def test_unversioned_existing_tables_are_not_a_fresh_install(upgrade_case):
    _, connect, scripts, _, _, _ = upgrade_case
    with connect() as db:
        db.execute('CREATE TABLE media(id integer)')
    with pytest.raises(RuntimeError, match='verified offline backup'):
        migrate(connect, scripts, before_upgrade=require_catalog_backup)
    with connect() as db:
        assert db.execute("SELECT to_regclass('schema_versions') AS name").fetchone()['name'] is None


def test_a_new_empty_schema_beside_an_existing_catalog_is_not_an_upgrade(upgrade_case):
    # The People install migrates a new schema in the same database as the main catalog.
    _, connect, scripts, _, _, _ = upgrade_case
    with connect() as db:
        db.execute('CREATE TABLE media(id integer)')
        db.execute('CREATE SCHEMA people_fixture')

    @contextmanager
    def scoped():
        with connect() as db:
            db.execute('SET search_path TO people_fixture')
            yield db
    assert migrate(scoped, scripts, before_upgrade=require_catalog_backup) == [1]
    with connect() as db:
        assert db.execute("SELECT to_regclass('people_fixture.schema_versions') AS name").fetchone()['name']
    # Once that schema holds a catalog, its own upgrades are guarded again.
    add_upgrade(scripts)
    with pytest.raises(RuntimeError, match='verified offline backup'):
        migrate(scoped, scripts, before_upgrade=require_catalog_backup)
