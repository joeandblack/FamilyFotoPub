from dataclasses import replace

from cryptography.fernet import Fernet
import psycopg
from psycopg import sql
import pytest

from app.backup import restore_database as module
from app.backup.postgres import Endpoint, capture_database
from tests.test_backup_postgres import postgres, seed, ample, ROOT  # noqa: F401


@pytest.mark.parametrize('stage', ['before-load', 'after-load', 'changed-owner', 'public-access', 'writable', 'active'])
def test_database_resume_uses_only_journalled_owned_database(postgres, tmp_path, monkeypatch, stage):
    params, source, _, dump, restore = postgres
    seed(params, source)
    endpoint = Endpoint(params['host'], params['port'], source, params['user'])
    admin = replace(endpoint, dbname='postgres')
    key = Fernet.generate_key()
    artifact = tmp_path / 'dump'
    capture_database(endpoint, dump, artifact, key, estimated_bytes=1024 ** 2, guard_factory=ample)
    records = []
    load, verify = module._load_archive, module._verify_database

    def interrupted(*args, **kwargs):
        raise OSError('Synthetic interrupted restore')

    monkeypatch.setattr(module, '_verify_database' if stage == 'after-load' else '_load_archive', interrupted)
    options = dict(expected_cluster_directory=ROOT / 'temp/backup-plan-pg', required_bytes=1024 ** 2,
                   guard_factory=ample, record_target=lambda value: records.append(dict(value)))
    held = None
    try:
        with pytest.raises(OSError, match='interrupted'):
            module.restore_database(artifact, key, admin, restore, **options)
        record = records[-1]
        assert record['state'] == 'created' and type(record['database_oid']) is int
        assert records[0]['state'] == 'creation-planned'
        monkeypatch.setattr(module, '_load_archive', load)
        monkeypatch.setattr(module, '_verify_database', verify)
        with pytest.raises(module.RestoreFailed, match='journal identity'):
            module.restore_database(artifact, key, admin, restore,
                                    resume_record={**record, 'artifact_id': 'wrong'}, **options)
        if stage == 'changed-owner':
            with psycopg.connect(**params, autocommit=True) as db:
                db.execute(sql.SQL('COMMENT ON DATABASE {} IS NULL').format(sql.Identifier(record['database'])))
            with pytest.raises(module.RestoreFailed, match='ownership'):
                module.restore_database(artifact, key, admin, restore, resume_record=record, **options)
            return
        if stage in {'public-access', 'writable', 'active'}:
            if stage == 'active':
                held = psycopg.connect(**{**params, 'dbname': record['database']})
            else:
                statement = ('GRANT CONNECT ON DATABASE {} TO PUBLIC' if stage == 'public-access'
                             else 'ALTER DATABASE {} SET default_transaction_read_only = off')
                with psycopg.connect(**params, autocommit=True) as db:
                    db.execute(sql.SQL(statement).format(sql.Identifier(record['database'])))
            with pytest.raises(module.RestoreFailed, match='isolated and read-only'):
                module.restore_database(artifact, key, admin, restore, resume_record=record, **options)
            return
        if stage == 'after-load':
            monkeypatch.setattr(module, '_load_archive', lambda *args: pytest.fail('Do not replay an already committed dump'))
        result = module.restore_database(artifact, key, admin, restore, resume_record=record, **options)
        assert result['database'] == record['database'] and result['verified']
        assert result['database_oid'] == record['database_oid']
        with psycopg.connect(**{**params, 'dbname': result['database']}) as db:
            assert db.execute('SELECT count(*) FROM media').fetchone()[0] == 1
            assert db.execute('SHOW default_transaction_read_only').fetchone()[0] == 'on'
    finally:
        if held is not None:
            held.close()
        names = {record['database'] for record in records}
        with psycopg.connect(**params, autocommit=True) as db:
            for name in names:
                db.execute(sql.SQL('DROP DATABASE IF EXISTS {}').format(sql.Identifier(name)))
