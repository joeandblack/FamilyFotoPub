from unittest.mock import Mock

import pytest

from tests.conftest import admit_empty_test_schema


def database(schema, *, schemas=None, populated=False):
    db = Mock()
    db.execute.return_value.fetchone.side_effect = [
        {'schema': schema, 'schemas': [schema] if schemas is None else schemas},
        {'present': populated},
    ]
    return db


def test_allows_only_empty_isolated_fixture_schema():
    schema = 'uploadtest_' + 'a' * 32
    db = database(schema)
    admit_empty_test_schema(db, set(), [1])
    assert db.execute.call_args.args[1] == (schema,)


@pytest.mark.parametrize('schema,schemas,applied,populated', [
    ('public', None, set(), False),
    ('uploadtest_' + 'a' * 32, ['public'], set(), False),
    ('uploadtest_' + 'a' * 32, ['uploadtest_' + 'a' * 32, 'public'], set(), False),
    ('uploadtest_' + 'a' * 32, None, {1}, False),
    ('uploadtest_' + 'a' * 32, None, set(), True),
    ('uploadtest_not_random', None, set(), False),
    ('uploadtest_' + 'a' * 31, None, set(), False),
    ('uploadtest_' + 'a' * 33, None, set(), False),
    (None, [], set(), False),
])
def test_existing_or_nonisolated_catalog_still_requires_backup(schema, schemas, applied, populated):
    with pytest.raises(RuntimeError, match='requires a verified offline backup'):
        admit_empty_test_schema(database(schema, schemas=schemas, populated=populated), applied, [2])
