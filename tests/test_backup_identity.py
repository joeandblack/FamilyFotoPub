import json

import pytest

from app.backup.admission import maintenance, participant, acknowledge_failed_maintenance, MaintenanceBlocked
from app.backup.identity import ensure_library_identity, read_library_identity
from app.backup.policy import classify_file


def test_identity_is_stable_and_requires_matching_admission(tmp_path):
    with maintenance(tmp_path, 'backup') as admission:
        with pytest.raises(ValueError, match='matching'):
            ensure_library_identity(tmp_path, 'wrong')
        identity = ensure_library_identity(tmp_path, admission)
        assert ensure_library_identity(tmp_path, admission) == identity
    with maintenance(tmp_path, 'backup') as admission:
        assert ensure_library_identity(tmp_path, admission) == identity
    assert read_library_identity(tmp_path) == identity
    assert classify_file('data', '.recovery-admission/library.json').category == 'authoritative'
    assert classify_file('data', '.recovery-admission/retired.json').category == 'transient'


def test_corrupt_identity_is_not_silently_replaced(tmp_path):
    with maintenance(tmp_path, 'backup') as admission:
        path = tmp_path / '.recovery-admission/library.json'
        path.write_text('{"library_id":"broken"}')
        with pytest.raises(ValueError, match='Invalid library identity'):
            ensure_library_identity(tmp_path, admission)
        assert json.loads(path.read_text())['library_id'] == 'broken'


def test_retirement_blocks_registration_and_new_maintenance(tmp_path):
    with maintenance(tmp_path, 'relocate'):
        (tmp_path / '.recovery-admission/retired.json').write_text('{}')
    with pytest.raises(MaintenanceBlocked, match='retired'):
        with participant(tmp_path):
            pytest.fail('Retired source admitted a writer')
    with pytest.raises(MaintenanceBlocked, match='retired'):
        with maintenance(tmp_path, 'backup'):
            pytest.fail('Retired source admitted maintenance')


def test_failed_maintenance_acknowledgement_does_not_unretire_source(tmp_path):
    with pytest.raises(RuntimeError):
        with maintenance(tmp_path, 'relocate') as admission:
            (tmp_path / '.recovery-admission/retired.json').write_text('{}')
            raise RuntimeError('injected crash after retirement')
    acknowledge_failed_maintenance(tmp_path, admission)
    assert (tmp_path / '.recovery-admission/retired.json').exists()
    with pytest.raises(MaintenanceBlocked, match='retired'):
        with participant(tmp_path):
            pass
