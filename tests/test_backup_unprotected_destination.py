"""A disk that ignores permissions must be refused early, unless explicitly accepted."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
from types import SimpleNamespace
import unicodedata
import uuid

import psycopg
from psycopg import sql
import pytest

from app.backup import manage, private_storage, volumes
from app.backup.library import capture_library, verify_library
from app.backup.library_restore import restore_library
from app.backup.admission import acknowledge_failed_maintenance
from app.backup.media_inventory import capture_media_inventory
from app.backup.media_tree import copy_media_tree
from app.backup.postgres import Endpoint
from app.backup.private_storage import (check_destination_privacy, create_private_directory, keeps_permissions,
                                        UnprotectedDestination)
from app.backup.restic import Restic
from app.backup.state_files import restore_state
from app.migration.credentials import prepare_recovery_role
from app.migration.tls import prepare_recovery_tls
from tests.test_backup_capture_retry import ample, stop_at, world  # noqa: F401
from tests.test_backup_postgres import postgres, ROOT, seed  # noqa: F401
from tests.backup_keys import backup_key


EXFAT_ROOT = os.environ.get('FAMILYFOTO_BACKUP_EXFAT_ROOT')
real_exfat = pytest.mark.skipif(not EXFAT_ROOT, reason='Set FAMILYFOTO_BACKUP_EXFAT_ROOT to a folder on a real exFAT disk')


@pytest.fixture
def unprotected(monkeypatch):
    """Make this disk behave like exFAT: permission changes silently do nothing."""
    if os.name == 'nt':
        monkeypatch.setattr(private_storage, 'windows_acls_persist', lambda path: False)
        # icacls proves nothing on this disk, so reaching it would be a failure.
        monkeypatch.setattr(private_storage, 'subprocess', None)
    else:
        real = Path.chmod
        monkeypatch.setattr(Path, 'chmod', lambda self, mode: real(self, 0o777))


class Folder:
    """A folder whose reported mode follows a rule, like a filesystem would."""

    def __init__(self, report):
        self.report, self.mode, self.calls = report, 0o777, []

    def chmod(self, mode):
        self.calls.append(mode)
        self.mode = mode

    def stat(self):
        return SimpleNamespace(st_mode=stat.S_IFDIR | self.report(self.mode))


def test_a_real_filesystem_keeps_both_modes_and_ends_owner_only():
    folder = Folder(lambda mode: mode)
    assert keeps_permissions(folder) is True
    assert folder.mode == 0o700
    assert len(set(folder.calls)) == 2


@pytest.mark.parametrize('fixed', [0o700, 0o500, 0o777], ids=['macos-exfat', 'read-only', 'open'])
def test_one_fixed_answer_is_not_protection(fixed):
    folder = Folder(lambda mode: fixed)
    assert keeps_permissions(folder) is False
    assert folder.mode == 0o700


def test_a_disk_that_refuses_chmod_is_not_protection():
    class Refusing(Folder):
        def chmod(self, mode):
            raise OSError('Not supported')
    assert keeps_permissions(Refusing(lambda mode: mode)) is False


@pytest.mark.skipif(os.name == 'nt', reason='POSIX modes')
def test_a_disk_reporting_owner_only_for_everything_is_refused(tmp_path, monkeypatch):
    real = Path.stat

    def fixed(self, **kwargs):
        info = real(self, **kwargs)
        return SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_dev=info.st_dev, st_ino=info.st_ino)
    monkeypatch.setattr(Path, 'stat', fixed)
    with pytest.raises(UnprotectedDestination):
        create_private_directory(tmp_path / 'private')
    monkeypatch.undo()
    assert not (tmp_path / 'private').exists()


def test_a_protected_disk_is_unchanged(tmp_path):
    path = create_private_directory(tmp_path / 'private')
    assert path.is_dir()
    check_destination_privacy(tmp_path)
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ['private']


def test_an_unprotected_disk_is_refused_and_left_clean(tmp_path, unprotected):
    with pytest.raises(UnprotectedDestination, match='Nothing was written'):
        create_private_directory(tmp_path / 'private')
    assert not (tmp_path / 'private').exists()


def test_refusal_is_a_permission_error(tmp_path, unprotected):
    with pytest.raises(PermissionError):
        create_private_directory(tmp_path / 'private')


def test_accepting_an_unprotected_disk_is_explicit(tmp_path, unprotected):
    assert create_private_directory(tmp_path / 'private', allow_unprotected=True).is_dir()


def test_the_privacy_check_leaves_nothing_behind(tmp_path, unprotected):
    with pytest.raises(UnprotectedDestination):
        check_destination_privacy(tmp_path)
    check_destination_privacy(tmp_path, allow_unprotected=True)
    assert list(tmp_path.iterdir()) == []


def test_an_existing_folder_is_never_touched(tmp_path, unprotected):
    existing = tmp_path / 'existing'
    existing.mkdir()
    (existing / 'file').write_bytes(b'kept')
    with pytest.raises(FileExistsError):
        create_private_directory(existing, allow_unprotected=True)
    assert (existing / 'file').read_bytes() == b'kept'


@pytest.mark.skipif(os.name != 'nt', reason='Windows volume capability')
def test_this_windows_system_volume_keeps_permissions():
    assert volumes.windows_acls_persist(Path(os.environ['SystemRoot'])) is True


@pytest.mark.skipif(os.name != 'nt', reason='Windows volume capability')
def test_windows_capability_read_failure_is_not_treated_as_protected(tmp_path, monkeypatch):
    def unreadable(path):
        raise volumes.StorageUnavailable('Cannot read Windows volume capabilities')
    monkeypatch.setattr(private_storage, 'windows_acls_persist', unreadable)
    with pytest.raises(volumes.StorageUnavailable):
        create_private_directory(tmp_path / 'private', allow_unprotected=True)


def test_capture_refuses_before_taking_the_gate(world, unprotected):  # noqa: F811
    with pytest.raises(UnprotectedDestination):
        world.capture()
    assert not world.backup.exists()
    assert not (world.data / '.recovery-admission/maintenance.json').exists()


def test_capture_runs_when_accepted(world, request):  # noqa: F811
    # The source installation's protected gate exists before simulating the backup disk.
    from app.backup.admission import prepare
    prepare(world.data)
    request.getfixturevalue('unprotected')
    payload = capture_library(object(), None, world.config, world.data, world.media, world.backup, world.key,
                              database_budget=1024, offline_confirmed=True, guard_factory=ample,
                              allow_unprotected=True)
    assert verify_library(world.backup, world.key) == payload


def test_a_retry_needs_the_same_acceptance(world, request):  # noqa: F811
    def run(**kwargs):
        return capture_library(object(), None, world.config, world.data, world.media, world.backup, world.key,
                               database_budget=1024, offline_confirmed=True, guard_factory=ample, **kwargs)
    with pytest.raises(Exception, match='copy'):
        run(allow_unprotected=True, progress=stop_at('copy', 1))
    marker = world.data / '.recovery-admission/maintenance.json'
    acknowledge_failed_maintenance(world.data, json.loads(marker.read_text())['id'])
    request.getfixturevalue('unprotected')
    with pytest.raises(UnprotectedDestination):
        run(resume=True)
    assert not marker.exists()
    payload = run(resume=True, allow_unprotected=True)
    assert verify_library(world.backup, world.key) == payload


@pytest.fixture
def media(tmp_path):
    source = tmp_path / 'media'
    source.mkdir()
    (source / 'photo.jpg').write_bytes(b'fixture')
    inventory = tmp_path / 'inventory'
    capture_media_inventory(source, inventory, offline_confirmed=True, guard_factory=ample)
    return source, inventory


def test_a_standalone_copy_refuses_without_acceptance(tmp_path, media, unprotected):
    with pytest.raises(UnprotectedDestination):
        copy_media_tree(*media, tmp_path / 'copy', offline_confirmed=True, guard_factory=ample)
    assert not (tmp_path / 'copy').exists()
    assert copy_media_tree(*media, tmp_path / 'copy', offline_confirmed=True, guard_factory=ample,
                           allow_unprotected=True)['files'] == 1


def test_a_copy_retry_refuses_without_acceptance(tmp_path, media, request):
    def stop(event):
        if event.get('phase') == 'copy':
            raise KeyError('stop')
    with pytest.raises(KeyError):
        copy_media_tree(*media, tmp_path / 'copy', offline_confirmed=True, guard_factory=ample, progress=stop)
    request.getfixturevalue('unprotected')
    with pytest.raises(UnprotectedDestination):
        copy_media_tree(*media, tmp_path / 'copy', offline_confirmed=True, guard_factory=ample)


def test_inventory_refuses_without_acceptance(tmp_path, unprotected):
    source = tmp_path / 'media'
    source.mkdir()
    with pytest.raises(UnprotectedDestination):
        capture_media_inventory(source, tmp_path / 'inventory', offline_confirmed=True, guard_factory=ample)
    assert capture_media_inventory(source, tmp_path / 'inventory', offline_confirmed=True, guard_factory=ample,
                                   allow_unprotected=True)['complete'] is True


@pytest.mark.parametrize('function', [restore_state, restore_library, prepare_recovery_role, prepare_recovery_tls],
                         ids=lambda function: function.__name__)
def test_restored_state_has_no_way_to_accept(function):
    """Decrypted state, credentials and keys have no such option at all."""
    assert 'allow_unprotected' not in function.__code__.co_varnames


def test_repository_refuses_without_acceptance(tmp_path, unprotected):
    engine = object.__new__(Restic)
    engine.repository = tmp_path / 'repository'
    engine.run = lambda *args, **kwargs: pytest.fail('Must refuse before running restic')
    with pytest.raises(UnprotectedDestination):
        engine.initialize()
    assert not engine.repository.exists()


def test_cli_passes_acceptance_only_when_asked(world, monkeypatch, tmp_path):  # noqa: F811
    seen = []

    def fake(*args, **kwargs):
        seen.append(kwargs['allow_unprotected'])
        return {'id': 'e' * 32}
    monkeypatch.setattr(manage, 'capture_library', fake)
    monkeypatch.setattr(manage.passwords, 'from_arguments', lambda args: 'the password')
    monkeypatch.setattr(manage, 'key_for', lambda args, text: world.key)
    arguments = ['capture-library', '--directory', str(world.backup), '--backup-password-stdin', '--port', '55490',
                 '--username', 'backup_test', '--dbname', 'fixture', '--pg-dump', 'pg_dump', '--config-root', str(world.config),
                 '--data-root', str(world.data), '--media-root', str(world.media), '--database-budget-bytes', '1024', '--offline']
    assert manage.main(arguments) == 0
    assert manage.main(arguments + ['--allow-unprotected-destination']) == 0
    assert seen == [False, True]


@real_exfat
def test_real_exfat_refuses_by_default_and_works_when_accepted():
    root = Path(EXFAT_ROOT)
    with pytest.raises(UnprotectedDestination):
        check_destination_privacy(root)
    check_destination_privacy(root, allow_unprotected=True)
    assert not list(root.glob('.familyfoto-privacy-check-*'))


@real_exfat
def test_real_exfat_whole_capture_resume_and_restore(postgres, tmp_path):  # noqa: F811
    """A real dump, interrupted and resumed on exFAT, restored to a protected local disk."""
    params, source, _, dump, restore = postgres
    seed(params, source)
    digest = hashlib.sha256(b'fixture photo').hexdigest()
    with psycopg.connect(**{**params, 'dbname': source}) as db:
        db.execute('ALTER TABLE media ADD COLUMN relative_path text, ADD COLUMN size bigint, '
                   'ADD COLUMN sha256 text, ADD COLUMN stored_sha256 text, '
                   'ADD COLUMN original_relative_path text, ADD COLUMN original_size bigint, '
                   'ADD COLUMN conversion_state text')
        db.execute("UPDATE media SET relative_path='photo.jpg',size=13,sha256=%s", (digest,))
        db.execute('CREATE TABLE media_trash(media_id integer, deleting_at timestamptz, deleted_at timestamptz)')
        db.execute('CREATE TABLE video_metadata(media_id integer, metadata jsonb)')
    config, data, media_root = (tmp_path / name for name in ('config', 'data', 'media'))
    for root in (config, data, media_root):
        root.mkdir()
    (config / 'local.json').write_text('{}')
    (media_root / 'photo.jpg').write_bytes(b'fixture photo')
    key = backup_key()
    target = Path(EXFAT_ROOT) / f'familyfoto-test-{uuid.uuid4().hex}'
    endpoint = Endpoint(params['host'], params['port'], source, params['user'])

    def capture(**kwargs):
        return capture_library(endpoint, dump, config, data, media_root, target, key, database_budget=1024 ** 2,
                               offline_confirmed=True, guard_factory=ample, allow_unprotected=True, **kwargs)
    report = None
    try:
        def stop(event):
            if event.get('phase') == 'copy':
                raise OSError('Synthetic interrupted capture')
        with pytest.raises(OSError, match='Synthetic'):
            capture(progress=stop)
        marker = data / '.recovery-admission/maintenance.json'
        acknowledge_failed_maintenance(data, json.loads(marker.read_text())['id'])
        payload = capture(resume=True)
        assert verify_library(target, key) == payload
        media_root.rename(tmp_path / 'disconnected-media')
        report = restore_library(target, key, tmp_path / 'recovered',
                                 Endpoint(params['host'], params['port'], 'postgres', params['user']), restore,
                                 expected_cluster_directory=ROOT / 'temp/backup-plan-pg',
                                 database_budget=1024 ** 2, guard_factory=ample)
        assert report['media_verified'] is True and report['activation_allowed'] is False
        assert (tmp_path / 'recovered/media-copy/media/photo.jpg').read_bytes() == b'fixture photo'
    finally:
        if report:
            with psycopg.connect(**params, autocommit=True) as db:
                db.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(report['database'])))
        remove_owned(target)


@real_exfat
def test_real_exfat_encrypted_capture_and_restore(postgres, tmp_path):  # noqa: F811
    """A whole encrypted capture into a restic repository on exFAT, restored with the sources gone."""
    from app.backup import encrypted_library
    executable = os.environ.get('FAMILYFOTO_BACKUP_RESTIC')
    if not executable:
        pytest.skip('Explicit checksum-pinned restic test executable required')
    params, source, _, dump, restore = postgres
    seed(params, source)
    name = unicodedata.normalize('NFC', 'café/写真.jpg')
    with psycopg.connect(**{**params, 'dbname': source}) as db:
        db.execute('ALTER TABLE media ADD COLUMN relative_path text, ADD COLUMN size bigint, '
                   'ADD COLUMN sha256 text, ADD COLUMN stored_sha256 text, '
                   'ADD COLUMN original_relative_path text, ADD COLUMN original_size bigint, '
                   'ADD COLUMN conversion_state text')
        db.execute('UPDATE media SET relative_path=%s,size=13,sha256=%s',
                   (name, hashlib.sha256(b'fixture photo').hexdigest()))
        db.execute('CREATE TABLE media_trash(media_id integer, deleting_at timestamptz, deleted_at timestamptz)')
        db.execute('CREATE TABLE video_metadata(media_id integer, metadata jsonb)')
    config, data, media = (tmp_path / label for label in ('config', 'data', 'media'))
    for root in (config, data, media):
        root.mkdir()
    (config / 'local.json').write_text('{}')
    (media / name).parent.mkdir()
    (media / name).write_bytes(b'fixture photo')
    secret = tmp_path / 'repository-password'
    secret.write_text('synthetic-test-password-' + uuid.uuid4().hex)
    target = Path(EXFAT_ROOT) / f'familyfoto-test-{uuid.uuid4().hex}'
    target.mkdir()
    report = None
    try:
        engine = Restic(executable, os.environ['FAMILYFOTO_BACKUP_RESTIC_SHA256'], target / 'repository', secret)
        repository = engine.initialize(allow_unprotected=True)
        key = backup_key()
        receipt = encrypted_library.capture_encrypted_library(
            Endpoint(params['host'], params['port'], source, params['user']), dump, config, data, media,
            tmp_path / 'working', key, engine, repository, database_budget=1024 ** 2,
            offline_confirmed=True, guard_factory=ample)
        assert receipt['media_files'] == 1 and not receipt['activation_allowed']
        engine.check(repository)
        for path in (config, data, media, tmp_path / 'working'):
            path.rename(path.with_name('disconnected-' + path.name))
        report = encrypted_library.restore_encrypted_library(
            engine, repository, receipt['metadata_snapshot'], key, tmp_path / 'recovered',
            Endpoint(params['host'], params['port'], 'postgres', params['user']), restore,
            expected_cluster_directory=ROOT / 'temp/backup-plan-pg', database_budget=1024 ** 2,
            metadata_budget=4 * 1024 ** 2, guard_factory=ample)
        assert report['media_verified'] and not report['activation_allowed']
        assert (Path(report['media_root']) / name).read_bytes() == b'fixture photo'
    finally:
        if report:
            with psycopg.connect(**params, autocommit=True) as db:
                db.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(report['database'])))
        remove_owned(target)


def remove_owned(path):
    if path.name.startswith('familyfoto-test-') and path.exists():
        # Restic marks its files read-only, and the extended form allows long paths.
        def writable(function, name, error):
            # Removing a file on macOS exFAT also removes its sidecar, and it can list a name
            # in a spelling only another form of it can remove.
            if isinstance(error, FileNotFoundError) and Path(name).name.startswith('._'):
                return
            os.chmod(name, 0o700)
            for spelled in (os.fspath(name), unicodedata.normalize('NFC', os.fspath(name)),
                            unicodedata.normalize('NFD', os.fspath(name))):
                try:
                    function(spelled)
                    return
                except OSError:
                    pass
            raise error
        shutil.rmtree('\\\\?\\' + str(path) if os.name == 'nt' else path, onexc=writable)


@real_exfat
def test_real_exfat_restic_repository(tmp_path):
    executable = os.environ.get('FAMILYFOTO_BACKUP_RESTIC')
    if not executable:
        pytest.skip('Explicit checksum-pinned restic test executable required')
    secret = tmp_path / 'repository-password'
    secret.write_text('synthetic-test-password-' + uuid.uuid4().hex)
    target = Path(EXFAT_ROOT) / f'familyfoto-test-{uuid.uuid4().hex}'
    target.mkdir()
    try:
        engine = Restic(executable, os.environ['FAMILYFOTO_BACKUP_RESTIC_SHA256'], target / 'repository', secret)
        with pytest.raises(UnprotectedDestination):
            engine.initialize()
        assert not engine.repository.exists()
        repository = engine.initialize(allow_unprotected=True)
        source = tmp_path / 'source'
        source.mkdir()
        (source / 'photo.jpg').write_bytes(b'fixture photo')
        first = engine.backup(source, repository, uuid.uuid4().hex, estimated_bytes=1024 ** 2, guard_factory=ample)
        assert first['repository_read_verified'] and first['data_added'] > 0
        second = engine.backup(source, repository, uuid.uuid4().hex, estimated_bytes=1024 ** 2, guard_factory=ample)
        assert second['data_added'] == 0
        engine.check(repository)
        source.rename(tmp_path / 'disconnected-source')
        restored = tmp_path / 'restored'
        assert engine.restore(first['snapshot_id'], repository, restored, required_bytes=1024 ** 2,
                              guard_factory=ample)['restored']
        assert [path.read_bytes() for path in restored.rglob('photo.jpg')] == [b'fixture photo']
    finally:
        remove_owned(target)
