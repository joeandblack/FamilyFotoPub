from contextlib import contextmanager
from unittest.mock import Mock, patch
import pytest
from fastapi import HTTPException
from app.library.api import rotate


@pytest.mark.parametrize('kind', ['image', 'video'])
def test_rotation_saves_adjustment_only(kind):
    db = Mock()
    db.execute.return_value.fetchone.side_effect = [{'kind': kind}, None, {'rotation': 90}]
    @contextmanager
    def connection():
        yield db
    with patch('app.library.api.connection', connection):
        assert rotate(1) == {'rotation': 90}
    assert 'media_adjustments' in db.execute.call_args_list[-1].args[0]


def test_rotation_rejects_deletion_in_progress():
    db = Mock()
    db.execute.return_value.fetchone.side_effect = [{'kind': 'video'}, {'exists': 1}]
    @contextmanager
    def connection():
        yield db
    with patch('app.library.api.connection', connection), pytest.raises(HTTPException) as exc:
        rotate(1)
    assert exc.value.status_code == 409
    assert db.execute.call_count == 2
