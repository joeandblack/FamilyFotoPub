import pytest

from app.backup.admission import maintenance, participant, MaintenanceBlocked
from app.backup.identity import ensure_library_identity
from app.backup.retirement import mark_source_retired, publish_retirement, verify_retirement, successor_id
from app.backup.encrypted import InvalidArtifact
from app.backup.space import StorageUnavailable
from app.backup.retirement import recover_retirement
from tests.backup_keys import backup_key


def retire(root):
    root.mkdir()
    with maintenance(root, 'backup') as lease:
        payload = {'id': 'a' * 32, 'migration_target': 'b' * 32,
                   'library_id': ensure_library_identity(root, lease)}
        record = mark_source_retired(root, lease, payload)
    return payload, record


@pytest.mark.parametrize('value', [None, '', 'B' * 32, '../target', 'a' * 31, 12])
def test_successor_identity_must_be_explicit(value):
    with pytest.raises(ValueError):
        successor_id(value)


def test_retirement_requires_correct_lease_and_library(tmp_path):
    with maintenance(tmp_path, 'backup') as lease:
        payload = {'id': 'a' * 32, 'migration_target': 'b' * 32,
                   'library_id': ensure_library_identity(tmp_path, lease)}
        with pytest.raises(ValueError, match='matching maintenance'):
            mark_source_retired(tmp_path, 'wrong', payload)
        with pytest.raises(ValueError, match='another library'):
            mark_source_retired(tmp_path, lease, {**payload, 'library_id': 'c' * 32})
        assert not (tmp_path / '.recovery-admission/retired.json').exists()


def test_receipt_binds_capture_library_and_successor(tmp_path):
    payload, record = retire(tmp_path / 'data')
    key = backup_key()
    publish_retirement(tmp_path, key, record)
    assert verify_retirement(tmp_path, key, payload) == record
    for field in ('id', 'library_id', 'migration_target'):
        with pytest.raises(InvalidArtifact, match='does not match'):
            verify_retirement(tmp_path, key, {**payload, field: 'd' * 32})
    with pytest.raises(InvalidArtifact):
        verify_retirement(tmp_path, backup_key(), payload)
    with pytest.raises(MaintenanceBlocked, match='retired'):
        with participant(tmp_path / 'data'):
            pytest.fail('Retired source admitted a writer')


def test_failed_receipt_publication_does_not_unretire_source(tmp_path):
    payload, record = retire(tmp_path / 'data')

    class Full:
        def __init__(self, requirements):
            pass

        def check(self, **kwargs):
            raise OSError('injected disk full')

    key = backup_key()
    with pytest.raises(OSError, match='disk full'):
        publish_retirement(tmp_path, key, record, guard_factory=Full)
    with pytest.raises(StorageUnavailable):
        verify_retirement(tmp_path, key, payload)
    with pytest.raises(MaintenanceBlocked, match='retired'):
        with participant(tmp_path / 'data'):
            pytest.fail('Failed receipt silently reopened the source')


@pytest.mark.parametrize('partial', [False, True])
def test_recover_receipt_is_idempotent_and_keeps_source_retired(tmp_path, monkeypatch, partial):
    payload, record = retire(tmp_path / 'data')
    monkeypatch.setattr('app.backup.library.verify_library', lambda *args: payload)
    key = backup_key()
    if partial:
        (tmp_path / 'retirement').mkdir()
        (tmp_path / 'retirement/00000000.fernet').write_bytes(b'interrupted')
    assert recover_retirement(tmp_path / 'data', tmp_path, key) == record
    assert recover_retirement(tmp_path / 'data', tmp_path, key) == record
    assert verify_retirement(tmp_path, key, payload) == record
    if partial:
        archived = list(tmp_path.glob('retirement-incomplete-*'))
        assert len(archived) == 1
        assert (archived[0] / '00000000.fernet').read_bytes() == b'interrupted'
    with pytest.raises(MaintenanceBlocked):
        with participant(tmp_path / 'data'):
            pytest.fail('Receipt recovery unretired source')


def test_recovery_refuses_corrupt_completed_receipt(tmp_path, monkeypatch):
    payload, record = retire(tmp_path / 'data')
    monkeypatch.setattr('app.backup.library.verify_library', lambda *args: payload)
    key = backup_key()
    publish_retirement(tmp_path, key, record)
    path = tmp_path / 'retirement/complete.fernet'
    path.write_bytes(b'damaged')
    with pytest.raises(InvalidArtifact):
        recover_retirement(tmp_path / 'data', tmp_path, key)
    assert path.read_bytes() == b'damaged'
    assert not list(tmp_path.glob('retirement-incomplete-*'))


def test_recovery_verifies_backup_before_creating_receipt(tmp_path, monkeypatch):
    retire(tmp_path / 'data')

    def damaged(*args):
        raise InvalidArtifact('damaged backup')

    monkeypatch.setattr('app.backup.library.verify_library', damaged)
    with pytest.raises(InvalidArtifact, match='damaged backup'):
        recover_retirement(tmp_path / 'data', tmp_path, backup_key())
    assert not (tmp_path / 'retirement').exists()


def test_receipt_recovery_refuses_while_capture_holds_admission(tmp_path):
    with maintenance(tmp_path, 'backup') as lease:
        payload = {'id': 'a' * 32, 'migration_target': 'b' * 32,
                   'library_id': ensure_library_identity(tmp_path, lease)}
        mark_source_retired(tmp_path, lease, payload)
        with pytest.raises(OSError):
            recover_retirement(tmp_path, tmp_path, backup_key())
    assert not (tmp_path / 'retirement').exists()
