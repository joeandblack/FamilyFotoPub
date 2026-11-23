from copy import deepcopy

import pytest

from app.backup import compatibility as module


def evidence():
    environment = dict.fromkeys(module.ENVIRONMENT_KEYS)
    environment.update(encoding=6, datcollate='C', datctype='C', datlocprovider='c')
    return {'format': module.FORMAT, 'environment': environment,
            'extensions': [['plpgsql', '1.0', 'pg_catalog']], 'schema_histories': {},
            'functions': {'count': 0, 'sha256': 'a' * 64}, 'models': []}


@pytest.mark.parametrize('versions', [[1, 3], [1, 1], [True], ['1'], [0], [10**15]])
def test_schema_history_refuses_gaps_duplicates_and_invalid_versions(versions):
    source = evidence()
    source['schema_histories'] = {'public': versions}
    with pytest.raises(ValueError, match='contiguous'):
        module.validate_source_evidence(source)


def test_newer_source_schema_is_not_silently_downgraded(monkeypatch):
    source = evidence()
    source['schema_histories'] = {'public': [1, 2, 3]}
    monkeypatch.setattr(module, 'release_metadata', lambda: {'core_schema_version': 2, 'minimum_upgrade_schema': 1})
    with pytest.raises(ValueError, match='unsupported'):
        module.validate_source_evidence(source)


def test_preflight_refuses_locale_and_extension_mismatch(monkeypatch):
    source = evidence()
    monkeypatch.setattr(module, 'database_environment', lambda db, name: {**source['environment'], 'datctype': 'other'})
    with pytest.raises(ValueError, match='locale'):
        module.preflight_target(None, source)
    def template_environment(db, name):
        assert name == 'template0'
        return source['environment']
    monkeypatch.setattr(module, 'database_environment', template_environment)

    class Database:
        def execute(self, query):
            return [('plpgsql', 'different')]

    with pytest.raises(ValueError, match='extension'):
        module.preflight_target(Database(), source)


def test_restore_verifies_function_definitions_not_only_row_counts(monkeypatch):
    source = evidence()
    changed = deepcopy(source)
    changed['functions']['sha256'] = 'b' * 64
    monkeypatch.setattr(module, 'database_evidence', lambda *args: changed)
    with pytest.raises(ValueError, match='differs'):
        module.verify_restored_evidence(None, [], source)


def test_environment_selects_requested_database_and_refuses_missing():
    class Database:
        def execute(self, query, parameters):
            assert parameters == ('template0',)
            return self

        def fetchone(self):
            return None

    with pytest.raises(ValueError, match='unavailable'):
        module.database_environment(Database(), 'template0')


@pytest.mark.parametrize('models', [None, {}, [{'schema': 'public', 'table': 'unknown'}],
                                  [{'schema': 'public', 'table': 'people_models', 'count': True,
                                    'sha256': 'a' * 64}]])
def test_invalid_model_evidence_is_refused(models):
    source = evidence()
    source['models'] = models
    with pytest.raises(ValueError, match='model'):
        module.validate_source_evidence(source)


def test_duplicate_model_evidence_is_refused():
    source = evidence()
    record = {'schema': 'public', 'table': 'people_models', 'count': 1, 'sha256': 'a' * 64}
    source['models'] = [record, record]
    with pytest.raises(ValueError, match='Duplicate model'):
        module.validate_source_evidence(source)
