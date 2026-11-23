"""Retired-source encrypted publication recovery, at real coordinator boundaries.

The coordinator runs for real against a fake engine and fake database freeze, so
each failure lands exactly where capture would stop. An opt-in drill further down
uses the disposable PostgreSQL cluster and pinned restic.
"""
from contextlib import contextmanager
import hashlib
import io
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
import uuid

import psycopg
import pytest

from app.backup import encrypted_library as module, manage, retirement
from app.backup.admission import maintenance, participant, MaintenanceBlocked
from app.backup.catalog import reference
from app.backup.encrypted import InvalidArtifact, seal_stream, verify_artifact
from app.backup.postgres import Endpoint
from app.backup.space import SpaceGuard
from tests.test_backup_postgres import postgres, seed  # noqa: F401
from tests.test_backup_restic import engine  # noqa: F401
from tests.backup_keys import backup_key


REPOSITORY = 'a' * 64
SUCCESSOR = 'b' * 32


def ample(requirements):
    return SpaceGuard(requirements, minimum_reserve=10, reserve_percent=0,
                      disk_usage=lambda path: SimpleNamespace(free=10 ** 9))


class FakeEngine:
    """Copies each snapshot so a test can compare what was actually published."""

    def __init__(self, root):
        self.repository = root / 'repository'
        self.repository.mkdir()
        self.password_file = root / 'repository-password'
        self.password_file.write_text('synthetic')
        self.snapshots = {}
        self.fail_next = {}
        self.calls = []

    def repository_id(self):
        return REPOSITORY

    def backup(self, source, repository, capture_id, *, estimated_bytes, guard_factory=SpaceGuard, progress=None):
        self.calls.append(source.name)
        failure = self.fail_next.pop(source.name, None)
        if failure is not None:
            raise failure
        snapshot = hashlib.sha256(f'{len(self.snapshots)}'.encode()).hexdigest()
        shutil.copytree(source, self.repository / snapshot)
        self.snapshots[snapshot] = {'tags': ['familyfoto:' + capture_id], 'restore_root': '/' + source.name,
                                    'source': source.name}
        return {'repository_id': repository, 'snapshot_id': snapshot, 'capture_id': capture_id}

    def snapshot(self, snapshot):
        if snapshot not in self.snapshots:
            raise OSError('Synthetic missing snapshot')
        return self.snapshots[snapshot]

    def metadata_snapshots(self):
        return [name for name, info in self.snapshots.items() if info['source'] == 'metadata']


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Real coordinator; only the database freeze and catalog dump are synthetic."""
    @contextmanager
    def frozen(endpoint):
        yield object()

    def components(endpoint, pg_dump, config, data, directory, key, identity, **kwargs):
        directory.mkdir(mode=0o700)
        made = {}
        for name, kind in (('database', 'postgres-custom-dump'), ('state', 'familyfoto-state-tar-v1')):
            seal_stream(io.BytesIO(b'synthetic ' + name.encode()), directory / name, key, estimated_bytes=64,
                        metadata={'kind': kind, 'file_bytes': 64}, guard_factory=ample)
            made[name] = reference(verify_artifact(directory / name, key))
        return made

    monkeypatch.setattr(module, 'freeze_database', frozen)
    monkeypatch.setattr(module, 'capture_components', components)
    monkeypatch.setattr(module, 'reconcile_uploads', lambda db, root: None)
    monkeypatch.setattr(module, 'reconcile_media', lambda db, root: {'catalog_media': 1})
    monkeypatch.setattr(module, 'state_inventory', lambda roots: [])
    roots = SimpleNamespace(config=tmp_path / 'config', data=tmp_path / 'data', media=tmp_path / 'media',
                            working=tmp_path / 'working', key=backup_key(), engine=FakeEngine(tmp_path))
    for path in (roots.config, roots.data, roots.media):
        path.mkdir()
    (roots.media / 'photo.jpg').write_bytes(b'fixture photo')

    def capture(retire_to=SUCCESSOR):
        return module.capture_encrypted_library(
            object(), None, roots.config, roots.data, roots.media, roots.working, roots.key, roots.engine,
            REPOSITORY, database_budget=1024, offline_confirmed=True, guard_factory=ample, retire_to=retire_to)

    def recover(**kwargs):
        return module.recover_encrypted_publication(roots.data, roots.working, kwargs.pop('key', roots.key),
                                                    kwargs.pop('tool', roots.engine), REPOSITORY,
                                                    guard_factory=ample, **kwargs)

    roots.capture, roots.recover = capture, recover
    roots.complete = roots.working / 'ENCRYPTED-COMPLETE.json'
    roots.interrupted = lambda: _interrupted(roots)
    return roots


def _interrupted(roots):
    """The metadata snapshot fails after the source was durably retired."""
    roots.engine.fail_next['metadata'] = OSError('Synthetic disconnected repository')
    with pytest.raises(OSError, match='disconnected'):
        roots.capture()
    assert not roots.complete.exists()
    assert (roots.data / '.recovery-admission/retired.json').is_file()


def assert_still_retired(roots):
    with pytest.raises(MaintenanceBlocked, match='retired'):
        with participant(roots.data):
            pytest.fail('Recovery reopened the retired source for writers')
    with pytest.raises(MaintenanceBlocked, match='retired'):
        with maintenance(roots.data, 'backup'):
            pytest.fail('Recovery admitted maintenance on the retired source')


def published_tree(roots, snapshot):
    root = roots.engine.repository / snapshot
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob('*') if path.is_file()}


def test_metadata_failure_recovers_without_reopening_source(world):
    world.interrupted()
    marker = world.data / '.recovery-admission/maintenance.json'
    before_marker, before_retired = marker.read_bytes(), (world.data / '.recovery-admission/retired.json').read_bytes()
    receipt = world.recover()
    assert receipt['metadata_snapshot'] in world.engine.snapshots
    assert receipt['activation_allowed'] is False
    assert json.loads(world.complete.read_text()) == receipt
    assert json.loads((world.working / 'journal.json').read_text())['phase'] == 'complete'
    # Failed-operation evidence and retirement are left exactly as they were.
    assert marker.read_bytes() == before_marker
    assert (world.data / '.recovery-admission/retired.json').read_bytes() == before_retired
    assert_still_retired(world)
    payload = module.verify_metadata(world.working / 'metadata', world.key, REPOSITORY)
    assert payload['migration_target'] == SUCCESSOR
    # The snapshot holds the receipt a later restore will verify.
    assert any(name.startswith('retirement/') for name in published_tree(world, receipt['metadata_snapshot']))


def test_recovery_receipt_matches_normal_capture_shape(world):
    normal = world.capture(retire_to=None)
    shutil.rmtree(world.working)
    (world.data / '.recovery-admission/library.json').unlink()
    world.interrupted()
    assert set(world.recover()) == set(normal)


def test_retry_after_second_failure_uses_same_job(world):
    world.interrupted()
    world.engine.fail_next['metadata'] = OSError('Still disconnected')
    with pytest.raises(OSError, match='Still'):
        world.recover()
    assert not world.complete.exists()
    assert_still_retired(world)
    assert world.recover()['metadata_snapshot'] in world.engine.snapshots
    assert_still_retired(world)


def test_cancelled_engine_run_leaves_retryable_state(world):
    world.interrupted()
    world.engine.fail_next['metadata'] = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        world.recover()
    assert not world.complete.exists()
    assert world.recover()['id']


def test_recovery_is_idempotent_and_never_publishes_twice(world):
    world.interrupted()
    first = world.recover()
    count = len(world.engine.metadata_snapshots())
    assert world.recover() == first
    assert len(world.engine.metadata_snapshots()) == count == 1


def test_crash_after_receipt_finishes_without_new_snapshot(world, monkeypatch):
    world.interrupted()
    real = module.journal
    def crash_on_complete(directory, identity, phase):
        if phase == 'complete':
            raise OSError('Synthetic crash before final journal')
        real(directory, identity, phase)
    monkeypatch.setattr(module, 'journal', crash_on_complete)
    with pytest.raises(OSError, match='crash'):
        world.recover()
    assert world.complete.is_file()
    monkeypatch.setattr(module, 'journal', real)
    assert world.recover()['metadata_snapshot'] == world.engine.metadata_snapshots()[0]
    assert len(world.engine.metadata_snapshots()) == 1


@pytest.mark.parametrize('completed', [False, True])
def test_interrupted_receipt_publication_is_repaired_first(world, monkeypatch, completed):
    real = module.publish_retirement
    def interrupted(directory, key, record, **kwargs):
        if completed:
            real(directory, key, record, **kwargs)
        else:
            (Path(directory) / 'retirement').mkdir()
            (Path(directory) / 'retirement/00000000.fernet').write_bytes(b'interrupted')
        raise OSError('Synthetic failure around receipt publication')
    monkeypatch.setattr(module, 'publish_retirement', interrupted)
    with pytest.raises(OSError, match='receipt publication'):
        world.capture()
    monkeypatch.setattr(module, 'publish_retirement', real)
    assert json.loads((world.working / 'journal.json').read_text())['phase'] == 'encrypted-media'
    receipt = world.recover()
    assert world.engine.calls[-1] == 'metadata'
    module.verify_metadata(world.working / 'metadata', world.key, REPOSITORY)
    archived = list(world.working.glob('retirement-incomplete-*'))
    assert len(archived) == (0 if completed else 1)
    if archived:
        assert (archived[0] / '00000000.fernet').read_bytes() == b'interrupted'
    # Diagnostic output never travels inside the published recovery metadata.
    assert not any('incomplete' in name for name in published_tree(world, receipt['metadata_snapshot']))


def assert_refused(world, error, **kwargs):
    published = list(world.engine.metadata_snapshots())
    with pytest.raises(error):
        world.recover(**kwargs)
    assert world.engine.metadata_snapshots() == published
    assert not world.complete.exists()


def test_mismatched_record_is_refused_before_writing_a_missing_receipt(world, monkeypatch):
    real = module.publish_retirement
    def interrupted(*args, **kwargs):
        raise OSError('Synthetic failure before receipt publication')
    monkeypatch.setattr(module, 'publish_retirement', interrupted)
    with pytest.raises(OSError, match='receipt publication'):
        world.capture()
    monkeypatch.setattr(module, 'publish_retirement', real)
    path = world.data / '.recovery-admission/retired.json'
    path.write_text(json.dumps({**json.loads(path.read_text()), 'successor_id': 'd' * 32}))
    assert_refused(world, InvalidArtifact)
    assert not (world.working / 'metadata/retirement').exists()


def test_refuses_capture_that_did_not_retire_source(world):
    world.engine.fail_next['metadata'] = OSError('Synthetic disconnected repository')
    with pytest.raises(OSError):
        world.capture(retire_to=None)
    # The operator is told what to do instead, not merely that something differs.
    with pytest.raises(InvalidArtifact, match='capture again'):
        world.recover()
    assert world.engine.metadata_snapshots() == []
    assert not world.complete.exists()
    assert (world.data / '.recovery-admission/maintenance.json').is_file()
    assert not (world.data / '.recovery-admission/retired.json').exists()


def test_refuses_wrong_key_without_publishing(world):
    world.interrupted()
    assert_refused(world, InvalidArtifact, key=backup_key())
    assert world.recover()['id']


def test_refuses_another_repository(world):
    world.interrupted()
    other = SimpleNamespace(repository=world.engine.repository, repository_id=lambda: 'c' * 64)
    assert_refused(world, ValueError, tool=other)


@pytest.mark.parametrize('field', ['successor_id', 'capture_id', 'library_id'])
def test_refuses_mismatched_retirement_record(world, field):
    world.interrupted()
    path = world.data / '.recovery-admission/retired.json'
    record = json.loads(path.read_text())
    path.write_text(json.dumps({**record, field: 'd' * 32}))
    assert_refused(world, InvalidArtifact)


def test_refuses_source_that_was_not_retired(world):
    world.interrupted()
    (world.data / '.recovery-admission/retired.json').unlink()
    assert_refused(world, InvalidArtifact)


def test_refuses_other_library_installation(world):
    world.interrupted()
    path = world.data / '.recovery-admission/library.json'
    path.write_text(json.dumps({**json.loads(path.read_text()), 'library_id': 'd' * 32}))
    assert_refused(world, InvalidArtifact)


def test_refuses_tampered_metadata(world):
    world.interrupted()
    index = world.working / 'metadata/recovery-point.json'
    index.write_text(json.dumps({**json.loads(index.read_text()), 'migration_target': 'e' * 32}))
    assert_refused(world, InvalidArtifact)


def test_refuses_changed_inventory(world):
    world.interrupted()
    with (world.working / 'metadata/inventory/inventory.sqlite3').open('ab') as stream:
        stream.write(b'x')
    assert_refused(world, InvalidArtifact)


def test_refuses_damaged_completed_receipt_without_overwriting(world):
    world.interrupted()
    path = world.working / 'metadata/retirement/complete.fernet'
    path.write_bytes(b'damaged')
    assert_refused(world, InvalidArtifact)
    assert path.read_bytes() == b'damaged'
    assert not list(world.working.glob('retirement-incomplete-*'))


def test_refuses_missing_media_snapshot(world):
    world.interrupted()
    world.engine.snapshots.clear()
    with pytest.raises(OSError, match='missing snapshot'):
        world.recover()
    assert not world.complete.exists()


def test_refuses_media_snapshot_from_another_capture(world):
    world.interrupted()
    for info in world.engine.snapshots.values():
        info['tags'] = ['familyfoto:' + 'f' * 32]
    assert_refused(world, InvalidArtifact)


def test_refuses_unexpected_metadata_entry(world):
    world.interrupted()
    (world.working / 'metadata/planted.jpg').write_bytes(b'not ours')
    assert_refused(world, InvalidArtifact)


def test_refuses_journal_from_another_capture(world):
    world.interrupted()
    path = world.working / 'journal.json'
    path.write_text(json.dumps({**json.loads(path.read_text()), 'id': uuid.uuid4().hex}))
    assert_refused(world, InvalidArtifact)


def test_refuses_foreign_completion_receipt_without_overwriting(world):
    world.interrupted()
    world.complete.write_text(json.dumps({'id': 'f' * 32}))
    with pytest.raises(InvalidArtifact):
        world.recover()
    assert json.loads(world.complete.read_text()) == {'id': 'f' * 32}
    assert world.engine.metadata_snapshots() == []


def test_refuses_while_capture_still_holds_admission(world, monkeypatch):
    seen = []
    real = world.engine.backup
    def racing(source, *args, **kwargs):
        if source.name == 'metadata':
            with pytest.raises(OSError):
                world.recover()
            seen.append(True)
        return real(source, *args, **kwargs)
    monkeypatch.setattr(world.engine, 'backup', racing)
    assert world.capture()['id']
    assert seen == [True]
    assert len(world.engine.metadata_snapshots()) == 1


def test_recovery_never_calls_the_database_or_activation(world, monkeypatch):
    world.interrupted()
    def forbidden(*args, **kwargs):
        pytest.fail('Publication recovery must not touch the catalog or restart anything')
    monkeypatch.setattr(module, 'freeze_database', forbidden)
    monkeypatch.setattr(module, 'restore_catalog', forbidden)
    monkeypatch.setattr(retirement, 'mark_source_retired', forbidden)
    monkeypatch.setattr(module, 'mark_source_retired', forbidden)
    assert world.recover()['id']


def test_command_reports_recovery_and_requires_explicit_identity(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(manage.passwords, 'from_arguments', lambda args: 'the password')
    seen = []
    monkeypatch.setattr(manage, 'unlock', lambda root, text: seen.append(root) or backup_key())
    calls = []
    def recover(data_root, directory, supplied, tool, repository, progress=None):
        calls.append((data_root, directory, tool, repository))
        return {'id': 'a' * 32, 'metadata_snapshot': 'b' * 64, 'media_snapshot': 'c' * 64}
    monkeypatch.setattr(manage, 'recover_encrypted_publication', recover)
    monkeypatch.setattr(manage, 'engine_for', lambda args: 'engine')
    arguments = ['recover-encrypted-publication', '--restic', str(tmp_path / 'restic'), '--restic-sha256', 'd' * 64,
                 '--repository', str(tmp_path / 'repository'), '--repository-password-file', str(tmp_path / 'secret'),
                 '--repository-id', 'e' * 64, '--data-root', str(tmp_path / 'data'),
                 '--directory', str(tmp_path / 'working'), '--backup-password-stdin']
    assert manage.main(arguments) == 0
    assert calls == [(tmp_path / 'data', tmp_path / 'working', 'engine', 'e' * 64)]
    assert seen == [tmp_path / 'working' / 'metadata']  # The capture's context travels in its metadata.
    assert json.loads(capsys.readouterr().out) == {
        'state': 'publication-verified', 'capture_id': 'a' * 32, 'metadata_snapshot': 'b' * 64,
        'media_snapshot': 'c' * 64, 'source_retired': True, 'activation_allowed': False}


def test_real_engine_recovered_publication_restores(postgres, engine, tmp_path, monkeypatch):  # noqa: F811
    """Disposable cluster and pinned restic: retire, fail, recover, then restore."""
    params, source, target, dump, restore = postgres
    seed(params, source)
    with psycopg.connect(**{**params, 'dbname': source}) as db:
        db.execute('ALTER TABLE media ADD COLUMN relative_path text, ADD COLUMN size bigint, '
                   'ADD COLUMN sha256 text, ADD COLUMN stored_sha256 text, '
                   'ADD COLUMN original_relative_path text, ADD COLUMN original_size bigint, '
                   'ADD COLUMN conversion_state text')
        db.execute("UPDATE media SET relative_path='photo.jpg',size=13,sha256=%s",
                   (hashlib.sha256(b'fixture photo').hexdigest(),))
        db.execute('CREATE TABLE media_trash(media_id integer, deleting_at timestamptz, deleted_at timestamptz)')
        db.execute('CREATE TABLE video_metadata(media_id integer, metadata jsonb)')
    config, data, media = (tmp_path / name for name in ('config', 'data', 'media'))
    for path in (config, data, media):
        path.mkdir()
    (config / 'local.json').write_text('{}')
    (media / 'photo.jpg').write_bytes(b'fixture photo')
    repository = engine.initialize()
    key = backup_key()
    original = engine.backup
    def fail_metadata(path, *args, **kwargs):
        if path.name == 'metadata':
            raise OSError('Synthetic disconnected repository')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(engine, 'backup', fail_metadata)
    with pytest.raises(OSError, match='disconnected'):
        module.capture_encrypted_library(
            Endpoint(params['host'], params['port'], source, params['user']), dump,
            config, data, media, tmp_path / 'working', key, engine, repository,
            database_budget=1024 ** 2, offline_confirmed=True, guard_factory=ample, retire_to=SUCCESSOR)
    monkeypatch.setattr(engine, 'backup', original)
    receipt = module.recover_encrypted_publication(data, tmp_path / 'working', key, engine, repository,
                                                   guard_factory=ample)
    with pytest.raises(MaintenanceBlocked, match='retired'):
        with participant(data):
            pytest.fail('Recovery reopened the retired source')
    # Restore uses only the repository, both keys and the explicit target database.
    for path in (config, data, media, tmp_path / 'working'):
        path.rename(path.with_name('disconnected-' + path.name))
    with psycopg.connect(**params) as admin:
        cluster = Path(admin.execute('SHOW data_directory').fetchone()[0])
    report = module.restore_encrypted_library(
        engine, repository, receipt['metadata_snapshot'], key, tmp_path / 'recovered',
        Endpoint(params['host'], params['port'], 'postgres', params['user']), restore,
        expected_cluster_directory=cluster, database_budget=1024 ** 2, metadata_budget=1024 ** 2,
        guard_factory=ample)
    try:
        assert report['source_retired'] is True and report['restoration_id'] == SUCCESSOR
        assert report['activation_allowed'] is False and report['media_files'] == 1
    finally:
        with psycopg.connect(**params, autocommit=True) as admin:
            admin.execute(psycopg.sql.SQL('DROP DATABASE IF EXISTS {}').format(psycopg.sql.Identifier(report['database'])))
