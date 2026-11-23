from collections import Counter
import json
import os
from pathlib import Path

import psycopg
from psycopg import sql
import pytest

from app.backup import media_inventory, media_tree
from app.backup.diagnostics import ExplainedFailure
from app.backup.encrypted import InvalidArtifact
from app.backup.library import capture_library
from app.backup.library_restore import restore_library
from app.backup.media_inventory import InvalidMediaInventory
from app.backup.postgres import Endpoint
from app.migration.credentials import prepare_recovery_role
from app.migration.tls import prepare_recovery_tls
from app.migration.validation import validate_plain_candidate
from tests.test_backup_postgres import postgres, ample  # noqa: F401
from tests.test_migration_disk import relocation  # noqa: F401
from tests.test_migration_tls import authority


@pytest.fixture
def reads(monkeypatch):
    """Every whole-file hash outside the copy itself, by the path it read."""
    counted = Counter()

    def counting(real):
        def hashed(path, expected):
            counted[Path(path)] += 1
            return real(path, expected)
        return hashed
    monkeypatch.setattr(media_tree, 'hash_file', counting(media_tree.hash_file))
    monkeypatch.setattr(media_inventory, 'hash_file', counting(media_inventory.hash_file))
    return counted


@pytest.fixture
def candidate(relocation, postgres, tmp_path, request, reads):
    run, config, data, media, _, _, key = relocation
    params, _, _, _, restore = postgres
    authority(config / 'tls')
    mode = getattr(request, 'param', False)
    fresh = mode == 'fresh'
    backup, target = tmp_path / 'whole-backup', tmp_path / 'whole-restore'
    role_dir, tls_dir = tmp_path / 'role', tmp_path / 'tls'
    cluster = Path(__file__).resolve().parents[1] / 'temp/backup-plan-pg'
    admin = Endpoint(params['host'], params['port'], 'postgres', params['user'])
    capture_library(run.endpoint, run.dump, config, data, media, backup, key,
                    database_budget=1024 ** 2, offline_confirmed=True, guard_factory=ample)
    database = role = None
    reads.clear()
    try:
        report = restore_library(backup, key, target, admin, restore, expected_cluster_directory=cluster,
                                 database_budget=1024 ** 2, guard_factory=ample, validation_follows=fresh)
        database = report['database']
        record = json.loads((target / 'catalog/database-target.json').read_text())
        role = prepare_recovery_role(admin, record, role_dir, expected_cluster_directory=cluster,
                                     preview=mode is True)['role']
        prepare_recovery_tls(target / 'catalog/files/config/tls', tls_dir, ['new-host.local', '127.0.0.1'])
        def validate(**kwargs):
            return validate_plain_candidate(backup, key, target, admin, role_dir, tls_dir,
                                            expected_cluster_directory=cluster, fresh_restore=fresh, **kwargs)
        def preview(scratch, launch, **kwargs):
            from app.migration.preview import preview_library
            return preview_library(backup, key, target, admin, role_dir, tls_dir, scratch,
                                   expected_cluster_directory=cluster, port=4439, launch=launch, **kwargs)
        validate.preview = preview
        def activation(output, **kwargs):
            from app.migration.activation import prepare_activation
            return prepare_activation(backup, key, target, admin, role_dir, tls_dir, output,
                                      expected_cluster_directory=cluster, guard_factory=ample, **kwargs)
        validate.activation = activation
        validate.key = key
        yield validate, target, role_dir, tls_dir
    finally:
        with psycopg.connect(**params, autocommit=True) as db:
            if database:
                db.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(database)))
            if role:
                db.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))


def test_candidate_validation_combines_actual_restored_components(candidate):
    validate, target, _, _ = candidate
    result = validate()
    assert result['state'] == 'candidate-validated'
    assert not result['activation_allowed'] and not result['services_started']
    assert not result['native_inference_qualified']
    config = json.loads((target / 'catalog/files/config/local.json').read_text())
    assert config['recovery']['activation_required'] is True


def read_counts(reads, target):
    """How many times each file was hashed, for the backup and for the restored copy."""
    restored = {count for path, count in reads.items() if path.is_relative_to(target)}
    backup = {count for path, count in reads.items() if not path.is_relative_to(target)}
    return backup, restored


def test_a_restore_reads_everything_and_later_steps_rely_on_its_receipt(candidate, reads):
    validate, target, _, _ = candidate
    assert read_counts(reads, target) == ({3}, {1})
    reads.clear()
    # The restore left a receipt for its copy. The backup has none until this reads it once.
    validate()
    assert read_counts(reads, target) == ({1}, set())
    reads.clear()
    assert validate()['state'] == 'candidate-validated'
    assert read_counts(reads, target) == (set(), set())


def test_reverify_reads_the_backup_and_the_restored_copy_again(candidate, reads):
    validate, target, _, _ = candidate
    validate()
    reads.clear()
    validate(reverify=True)
    assert read_counts(reads, target) == ({1}, {1})


def test_a_restored_file_rewritten_after_its_receipt_is_still_found(candidate):
    validate, target, _, _ = candidate
    validate()
    damaged = next(path for path in (target / 'media-copy/media').rglob('*') if path.is_file())
    raw = bytearray(damaged.read_bytes())
    raw[0] ^= 1
    info = damaged.stat()
    damaged.write_bytes(raw)
    os.utime(damaged, ns=(info.st_atime_ns, info.st_mtime_ns + 2_000_000_000))
    with pytest.raises(InvalidMediaInventory, match='content'):
        validate()


@pytest.mark.parametrize('candidate', [True], indirect=True)
def test_preview_refuses_missing_packages_before_reading_any_media(candidate, tmp_path, reads):
    validate, _, _, _ = candidate
    reads.clear()
    scratch = tmp_path / 'preview-scratch'
    with pytest.raises(ExplainedFailure, match='numpy'):
        validate.preview(scratch, lambda cfg: pytest.fail('Must not launch'), packages=lambda people: ['numpy'])
    assert not reads and not scratch.exists()


def test_activation_refuses_a_hostname_in_another_case_before_reading_any_media(candidate, tmp_path, reads):
    from app.migration.activation import ACCEPTANCE_CHECKS
    validate, _, _, _ = candidate
    reads.clear()
    with pytest.raises(ExplainedFailure, match='upper and lower case'):
        validate.activation(tmp_path / 'activation', accepted=ACCEPTANCE_CHECKS, hostname='New-Host.local',
                            port=4340, source_unavailable=True)
    assert not reads and not (tmp_path / 'activation').exists()


def test_activation_refuses_an_unretired_source_before_reading_any_media(candidate, tmp_path, reads):
    from app.migration.activation import ACCEPTANCE_CHECKS
    validate, _, _, _ = candidate
    reads.clear()
    with pytest.raises(ValueError, match='Retire the source'):
        validate.activation(tmp_path / 'activation', accepted=ACCEPTANCE_CHECKS, hostname='new-host.local', port=4340)
    assert not reads


@pytest.mark.parametrize('candidate', ['fresh'], indirect=True)
def test_the_restore_test_hashes_each_copy_once_outside_the_copy(candidate, reads):
    validate, target, _, _ = candidate
    assert read_counts(reads, target) == ({1}, set())
    reads.clear()
    assert validate()['state'] == 'candidate-validated'
    assert read_counts(reads, target) == (set(), {1})


@pytest.mark.parametrize('candidate', ['fresh'], indirect=True)
def test_the_restore_test_still_finds_a_damaged_restored_file(candidate):
    validate, target, _, _ = candidate
    damaged = next(path for path in (target / 'media-copy/media').rglob('*') if path.is_file())
    raw = bytearray(damaged.read_bytes())
    raw[0] ^= 1
    damaged.write_bytes(raw)
    with pytest.raises(InvalidMediaInventory, match='content'):
        validate()


@pytest.mark.parametrize('candidate', ['fresh'], indirect=True)
def test_the_restore_test_still_finds_a_missing_restored_file(candidate):
    validate, target, _, _ = candidate
    next(path for path in (target / 'media-copy/media').rglob('*') if path.is_file()).unlink()
    with pytest.raises(InvalidMediaInventory, match='incomplete'):
        validate()


def test_an_encrypted_candidate_always_rereads_its_media(tmp_path, monkeypatch):
    from app.migration import candidate_layout as module
    directory = tmp_path / 'target'
    backup = directory / 'metadata-job/payload'
    backup.mkdir(parents=True)
    (backup / 'recovery-point.json').write_text('{}')
    (directory / 'restore-job.json').write_text(json.dumps({'repository_id': 'r'}))
    monkeypatch.setattr(module, 'verify_metadata', lambda *a: {'id': 'capture'})
    seen = {}
    monkeypatch.setattr(module, 'verify_candidate_media', lambda *a, **k: seen.update(k))
    module.candidate_layout(backup, b'key', directory, fresh_restore=True)
    assert seen['content'] is True


def shift_record(path, field, position):
    value = json.loads(path.read_text())
    value[field][position] += 7
    path.write_text(json.dumps(value))


def test_candidate_validation_accepts_a_remount_of_the_same_disks(candidate):
    validate, target, role, _ = candidate
    shift_record(target / 'restore-job.json', 'identity', 0)
    shift_record(role / 'credentials.json', 'directory', 0)
    assert validate()['state'] == 'candidate-validated'


@pytest.mark.parametrize('record', ['target', 'role'])
def test_candidate_validation_refuses_a_replaced_folder(candidate, record):
    validate, target, role, _ = candidate
    if record == 'target':
        shift_record(target / 'restore-job.json', 'identity', 1)
    else:
        shift_record(role / 'credentials.json', 'directory', 1)
    with pytest.raises(InvalidArtifact):
        validate()


@pytest.mark.parametrize('record', ['target', 'role'])
def test_candidate_validation_refuses_another_disk_after_a_remount(candidate, record):
    validate, target, role, _ = candidate
    path, field = ((target / 'restore-job.json', 'identity') if record == 'target'
                   else (role / 'credentials.json', 'directory'))
    shift_record(path, field, 0)
    value = json.loads(path.read_text())
    value['volume'] = {'kind': 'test', 'id': 'other-disk'}
    path.write_text(json.dumps(value))
    with pytest.raises(InvalidArtifact):
        validate()


@pytest.mark.parametrize('change', ['credentials', 'settings', 'incomplete', 'worker', 'identity'])
def test_candidate_rejects_mixed_components(candidate, change):
    validate, target, role, _ = candidate
    if change == 'credentials':
        path = role / 'credentials.json'
        value = json.loads(path.read_text())
        value['database_token'] = '0' * 32
    elif change == 'settings':
        path = target / 'catalog/files/config/local.json'
        value = json.loads(path.read_text())
        value['database']['port'] += 1
    elif change == 'incomplete':
        (target / 'LIBRARY-RESTORE-INCOMPLETE').touch()
        with pytest.raises(InvalidArtifact, match='incomplete'):
            validate()
        return
    elif change == 'worker':
        path = target / 'catalog/files/data/face-service-settings.json'
        value = json.loads(path.read_text())
        value['start_with_app'] = True
    else:
        path = target / 'LIBRARY-RECOVERY-REPORT.json'
        value = json.loads(path.read_text())
        value['restoration_id'] = '0' * 32
    path.write_text(json.dumps(value))
    with pytest.raises(InvalidArtifact):
        validate()


@pytest.mark.parametrize('candidate', [True], indirect=True)
def test_verified_candidate_preview_revokes_credentials_after_use(candidate, tmp_path):
    validate, target, _, _ = candidate
    before = (target / 'catalog/files/config/local.json').read_bytes()
    used = []
    def launch(cfg):
        used.append(cfg['database'])
        assert cfg['host'] == '127.0.0.1' and cfg['recovery']['activation_required']
        with psycopg.connect(**cfg['database']) as db:
            assert db.execute('SELECT count(*) FROM media').fetchone()[0] == 1
    result = validate.preview(tmp_path / 'preview-scratch', launch)
    assert result['state'] == 'preview-closed' and not result['activation_allowed']
    assert (target / 'catalog/files/config/local.json').read_bytes() == before
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(**used[0])
    assert validate()['state'] == 'candidate-validated'


def test_activation_intent_is_sealed_and_does_not_enable_candidate(candidate, tmp_path):
    from app.backup.encrypted import verify_artifact, read_chunks
    from app.migration.activation import ACCEPTANCE_CHECKS, FORMAT

    validate, target, role_dir, _ = candidate
    before = (target / 'catalog/files/config/local.json').read_bytes()
    output = tmp_path / 'activation'
    binding = lambda path: {'root': str(path), 'drive': 'fixture', 'serial': 'serial',
                            'volume_serial': 'volume', 'volume_id': 'id'}
    args = {'accepted': ACCEPTANCE_CHECKS, 'hostname': 'new-host.local', 'port': 4340, 'probe': binding}
    with pytest.raises(ValueError, match='Retire the source'):
        validate.activation(output, **args)
    assert not output.exists()
    result = validate.activation(output, source_unavailable=True, **args)
    assert result['state'] == 'activation-prepared'
    assert not result['activation_allowed'] and not result['resume_jobs']
    manifest = verify_artifact(output, validate.key)
    assert manifest['metadata']['kind'] == FORMAT
    intent = json.loads(b''.join(read_chunks(output, validate.key, manifest)))
    credentials = json.loads((role_dir / 'credentials.json').read_text())
    assert intent['credentials'] == credentials
    assert intent['accepted_checks'] == sorted(ACCEPTANCE_CHECKS)
    assert (target / 'catalog/files/config/local.json').read_bytes() == before
    assert not (target / 'media-copy/.familyfoto-volume.json').exists()
    assert validate()['state'] == 'candidate-validated'  # Includes NOLOGIN verification.
    for path in output.rglob('*'):
        if path.is_file():
            assert credentials['password'].encode() not in path.read_bytes()


@pytest.mark.parametrize('candidate', [True], indirect=True)
def test_preview_role_cannot_prepare_activation(candidate, tmp_path):
    from app.migration.activation import ACCEPTANCE_CHECKS
    validate, _, _, _ = candidate
    with pytest.raises(ValueError, match='preview credentials'):
        validate.activation(tmp_path / 'intent', accepted=ACCEPTANCE_CHECKS, source_unavailable=True,
                            hostname='new-host.local', port=4340)
