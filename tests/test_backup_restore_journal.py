from contextlib import nullcontext

import pytest

from app.backup import restore_database as module
from app.backup.postgres import Endpoint
from tests.test_backup_compatibility import evidence


@pytest.mark.parametrize('journal_fails', [False, True])
def test_target_intent_is_recorded_before_database_creation(tmp_path, monkeypatch, journal_fails):
    executable = tmp_path / 'pg_restore.exe'
    executable.touch()
    events = []
    monkeypatch.setattr(module, 'verify_artifact', lambda *args: {
        'id': 'a' * 32, 'sha256': 'b' * 64,
        'metadata': {'kind': 'postgres-custom-dump', 'tables': [], 'server_version': 170000,
                     'compatibility': evidence()}})
    monkeypatch.setattr(module, 'preflight_target', lambda *args: events.append('compatibility'))

    class Database:
        def execute(self, statement):
            if statement == 'SHOW data_directory':
                return self
            if statement == 'SHOW server_version_num':
                self.version = True
                return self
            events.append('create')
            raise RuntimeError('Injected database creation failure')

        def fetchone(self):
            return ('170000',) if getattr(self, 'version', False) else (str(tmp_path),)

    class Guard:
        def __init__(self, requirements):
            pass

        def check(self):
            pass

    monkeypatch.setattr(module.psycopg, 'connect', lambda **kwargs: nullcontext(Database()))

    def record(value):
        events.append('journal')
        assert value['database'].startswith('familyfoto_recovery_')
        assert value['cluster_directory'] == str(tmp_path.resolve())
        assert value['state'] == 'creation-planned'
        assert value['verified'] is False
        assert value['activation_allowed'] is False
        if journal_fails:
            raise OSError('Injected journal failure')

    with pytest.raises(OSError if journal_fails else RuntimeError):
        module.restore_database(tmp_path, b'fixture', Endpoint('127.0.0.1', 55489, 'postgres', 'fixture'),
                                executable, expected_cluster_directory=tmp_path, required_bytes=1024,
                                guard_factory=Guard, record_target=record)
    assert events == (['compatibility', 'journal'] if journal_fails else ['compatibility', 'journal', 'create'])
