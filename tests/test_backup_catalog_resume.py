import json

import psycopg
from psycopg import sql
import pytest

from app.backup import recovery as module
from app.backup.catalog import capture_catalog
from app.backup.diagnostics import ExplainedFailure
from app.backup.postgres import Endpoint
from tests.test_backup_postgres import postgres, seed, ample, ROOT  # noqa: F401
from tests.backup_keys import backup_key


def test_a_restore_killed_before_recording_anything_says_to_start_again(postgres, tmp_path):
    params, source, _, dump, restore = postgres
    seed(params, source)
    config, data = tmp_path / 'config', tmp_path / 'data'
    config.mkdir()
    data.mkdir()
    (config / 'local.json').write_text('{}')
    key = backup_key()
    capture_catalog(Endpoint(params['host'], params['port'], source, params['user']), dump, config, data,
                    tmp_path / 'backup', key, database_budget=1024 ** 2, offline_confirmed=True, guard_factory=ample)
    target = tmp_path / 'restore'
    target.mkdir()
    admin = Endpoint(params['host'], params['port'], 'postgres', params['user'])
    with pytest.raises(ExplainedFailure, match='start again in a new folder'):
        module.restore_catalog(tmp_path / 'backup', key, target, admin, restore, resume=True, guard_factory=ample,
                               expected_cluster_directory=ROOT / 'temp/backup-plan-pg', database_budget=1024 ** 2)
    assert not (target / 'catalog-job.json').exists()


@pytest.mark.parametrize('stage', ['before-sanitation', 'after-sanitation', 'partial-files'])
def test_catalog_retry_reuses_database_and_rebuilds_partial_state(postgres, tmp_path, monkeypatch, stage):
    params, source, _, dump, restore = postgres
    seed(params, source)
    with psycopg.connect(**{**params, 'dbname': source}) as db:
        db.execute('CREATE TABLE auth_sessions(token_hash text)')
        db.execute("INSERT INTO auth_sessions VALUES ('old-cookie')")
        db.execute('CREATE TABLE jobs(state text, error text)')
        db.execute("INSERT INTO jobs VALUES ('running', NULL)")
    config, data = tmp_path / 'config', tmp_path / 'data'
    config.mkdir()
    data.mkdir()
    (config / 'local.json').write_text('{}')
    key = backup_key()
    artifact, target = tmp_path / 'backup', tmp_path / 'restore'
    admin = Endpoint(params['host'], params['port'], 'postgres', params['user'])
    capture_catalog(Endpoint(params['host'], params['port'], source, params['user']), dump,
                    config, data, artifact, key, database_budget=1024 ** 2,
                    offline_confirmed=True, guard_factory=ample)
    options = dict(expected_cluster_directory=ROOT / 'temp/backup-plan-pg',
                   database_budget=1024 ** 2, guard_factory=ample)
    function = 'sanitize_files' if stage == 'partial-files' else 'sanitize_database'
    original = getattr(module, function)

    def interrupt(*args, **kwargs):
        if stage != 'before-sanitation':
            original(*args, **kwargs)
        raise OSError('Synthetic sanitation interruption')

    monkeypatch.setattr(module, function, interrupt)
    try:
        with pytest.raises(OSError, match='sanitation interruption'):
            module.restore_catalog(artifact, key, target, admin, restore, **options)
        identity = json.loads((target / 'database-target.json').read_text())
        assert (target / 'RECOVERY-INCOMPLETE').exists()
        monkeypatch.setattr(module, function, original)
        report = module.restore_catalog(artifact, key, target, admin, restore, resume=True, **options)
        assert report['database'] == identity['database']
        assert len(list(target.glob('previous-files-*'))) == 1
        settings = json.loads((target / 'files/config/local.json').read_text())
        assert settings['recovery']['activation_required'] and not report['activation_allowed']
        with psycopg.connect(**{**params, 'dbname': report['database']}) as db:
            assert db.execute('SELECT count(*) FROM auth_sessions').fetchone()[0] == 0
            assert db.execute('SELECT state FROM jobs').fetchone()[0] == 'paused'
        with psycopg.connect(**{**params, 'dbname': source}) as db:
            assert db.execute('SELECT count(*) FROM auth_sessions').fetchone()[0] == 1
        repeated = module.restore_catalog(artifact, key, target, admin, restore, resume=True, **options)
        assert repeated['database'] == report['database']
        assert len(list(target.glob('previous-files-*'))) == 2
        settings_path = target / 'files/config/local.json'
        settings = json.loads(settings_path.read_text())
        settings['recovery']['activation_required'] = False
        settings_path.write_text(json.dumps(settings))
        with pytest.raises(ValueError, match='activated'):
            module.restore_catalog(artifact, key, target, admin, restore, resume=True, **options)
    finally:
        record = target / 'database-target.json'
        if record.exists():
            name = json.loads(record.read_text())['database']
            with psycopg.connect(**params, autocommit=True) as db:
                db.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))
