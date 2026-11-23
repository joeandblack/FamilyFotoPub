"""Offline restic recovery points pairing one media snapshot with one catalog.

Capture and restore never activate an installation. The repository and application
state keys are explicit; neither is read from the running application's config.
"""
from contextlib import closing
import io
import json
import os
from pathlib import Path
import sqlite3
import uuid

from .admission import collect_idle, maintenance, MaintenanceBlocked
from .catalog import capture_components, publish_catalog, freeze_database, reference, verify_catalog, journal
from .encrypted import storage_budget, verify_artifact, InvalidArtifact, seal_stream, read_chunks, read_bounded
from .identity import ensure_library_identity, read_library_identity, read_record
from .library import reconcile_media
from .media_copy import sync_directory, copy_lock
from .media_inventory import capture_media_inventory, verify_media_inventory
from .media_paths import check_paths
from .media_tree import file_digest, verify_tree
from .passwords import (BackupKey, BackupPassword, FILE as PASSWORD_FILE, publish as publish_password,
                        unlock_context, validate_context_binding)
from .private_storage import create_private_directory
from .recovery import restore_catalog, save_private, abandoned_publication
from .restic import identifier
from .restic_restore_job import regular_tree, restore_snapshot_job
from .space import SpaceGuard, Requirement, directory_identity
from .volumes import same_job, volume_identity
from .state_files import state_inventory
from .uploads import reconcile_uploads
from .retirement import (ensure_receipt, successor_id, mark_source_retired, publish_retirement,
                         validate_record, verify_retirement)


FORMAT = 'familyfoto-library-restic-v2'
# Once the index is sealed: source retirement and its receipt happen during the
# 'encrypted-media' phase, then the metadata snapshot during 'encrypted-catalog'.
RECOVERABLE_PHASES = {'encrypted-media', 'encrypted-catalog'}


def independent(paths):
    for index, left in enumerate(paths):
        for right in paths[index + 1:]:
            if left.is_relative_to(right) or right.is_relative_to(left):
                raise ValueError('Backup roots, working directory and repository must be independent')


def capture_encrypted_library(endpoint, pg_dump, config_root, data_root, media_root, directory, key,
                              engine, repository_id, *, database_budget, offline_confirmed=False,
                              guard_factory=SpaceGuard, progress=None, retire_to=None):
    if offline_confirmed is not True:
        raise MaintenanceBlocked('Offline confirmation required for encrypted library capture')
    if retire_to is not None:
        successor_id(retire_to)
    config_root, data_root, media_root, directory = (
        Path(os.path.abspath(path)) for path in (config_root, data_root, media_root, directory))
    independent([config_root, data_root, media_root, directory, engine.repository])
    if any(engine.password_file.resolve().is_relative_to(root.resolve())
           for root in (config_root, data_root, media_root, directory)):
        raise ValueError('Repository password must not be included in captured roots')
    if directory.exists():
        raise FileExistsError('A new encrypted capture working directory is required')
    if not isinstance(key, BackupKey):
        raise TypeError('An encrypted library backup needs a key made from the administrator password')
    if engine.repository_id() != identifier(repository_id):
        raise ValueError('Encrypted repository identity differs')
    capture_id = uuid.uuid4().hex
    with maintenance(data_root, 'backup') as admission_id:
        with freeze_database(endpoint) as db:
            library_id = ensure_library_identity(data_root, admission_id)
            reconcile_uploads(db, data_root)
            records = state_inventory({'config': config_root, 'data': data_root})
            state_budget = sum(row['size'] for row in records) + len(records) * 16384 + 10240
            catalog_budget = storage_budget(database_budget) + storage_budget(state_budget) + 1024 ** 2
            guard_factory([Requirement('working', directory.parent, catalog_budget)]).check()
            create_private_directory(directory)
            metadata = directory / 'metadata'
            create_private_directory(metadata)
            # The catalog's password context travels in the metadata snapshot, at its root.
            publish_password(metadata, key)
            journal(directory, capture_id, 'inventory')
            inventory = capture_media_inventory(media_root, metadata / 'inventory', offline_confirmed=True,
                                                guard_factory=guard_factory, progress=progress)
            physical = verify_media_inventory(metadata / 'inventory', media_root, rehash=False)
            counts = reconcile_media(db, metadata / 'inventory')
            growth = inventory['bytes'] + physical['entries'] * 32768 + catalog_budget
            guard_factory([Requirement('working', directory, catalog_budget),
                           Requirement('repository', engine.repository, growth)]).check()
            components = capture_components(endpoint, pg_dump, config_root, data_root, metadata / 'catalog',
                                             key, capture_id, database_budget=database_budget,
                                             guard_factory=guard_factory, progress=progress)
            journal(directory, capture_id, 'encrypted-media')
            media = engine.backup(media_root, repository_id, capture_id, estimated_bytes=growth,
                                  guard_factory=guard_factory, progress=progress)
            verify_media_inventory(metadata / 'inventory', media_root, rehash=True)
            reconcile_media(db, metadata / 'inventory')
            reconcile_uploads(db, data_root)
        catalog = publish_catalog(metadata / 'catalog', key, capture_id, components,
                                  guard_factory=guard_factory, library_id=library_id)
        payload = {'format': FORMAT, 'id': capture_id, 'library_id': library_id,
                   'repository_id': repository_id, 'media_snapshot': media['snapshot_id'],
                   'media_root': engine.snapshot(media['snapshot_id'])['restore_root'],
                   'media_inventory_sha256': file_digest(metadata / 'inventory/inventory.sqlite3'),
                   'media_files': inventory['files'], 'media_bytes': inventory['bytes'],
                   'catalog': reference(catalog), 'reconciliation': counts, 'password': key.binding(),
                   'capture_mode': 'offline', 'activation_allowed': False}
        if retire_to is not None:
            payload['migration_target'] = retire_to
        save_private(metadata / 'recovery-point.json', payload)
        encoded = json.dumps(payload, sort_keys=True).encode()
        seal_stream(io.BytesIO(encoded), metadata / 'library-index', key, estimated_bytes=len(encoded),
                    metadata={'kind': FORMAT}, guard_factory=guard_factory)
        if retire_to is not None:
            retirement = mark_source_retired(data_root, admission_id, payload)
            publish_retirement(metadata, key, retirement, guard_factory=guard_factory)
        journal(directory, capture_id, 'encrypted-catalog')
        size = sum(path.stat().st_size for path in metadata.rglob('*') if path.is_file())
        saved = engine.backup(metadata, repository_id, capture_id, estimated_bytes=size + catalog_budget,
                              guard_factory=guard_factory, progress=progress)
        receipt = {'format': FORMAT, 'id': capture_id, 'library_id': library_id,
                   'repository_id': repository_id, 'metadata_snapshot': saved['snapshot_id'],
                   'media_snapshot': media['snapshot_id'], 'media_files': inventory['files'],
                   'media_bytes': inventory['bytes'], 'activation_allowed': False}
        save_private(directory / 'ENCRYPTED-COMPLETE.json', receipt)
        journal(directory, capture_id, 'complete')
        return receipt


def tagged_snapshot(engine, snapshot, payload):
    info = engine.snapshot(snapshot)
    if 'familyfoto:' + payload['id'] not in info.get('tags', []):
        raise InvalidArtifact('Encrypted snapshot belongs to another capture')
    return info


def verify_published_receipt(engine, path, payload, repository_id):
    """An existing completion receipt must match its capture and repository."""
    receipt = json.loads(read_bounded(path, 65536))
    expected = {'format': FORMAT, 'id': payload['id'], 'library_id': payload['library_id'],
                'repository_id': repository_id, 'media_snapshot': payload['media_snapshot'],
                'media_files': payload['media_files'], 'media_bytes': payload['media_bytes'],
                'activation_allowed': False}
    if (not isinstance(receipt, dict) or set(receipt) != {*expected, 'metadata_snapshot'}
            or any(receipt[name] != value for name, value in expected.items())):
        raise InvalidArtifact('Encrypted completion receipt belongs to another capture')
    tagged_snapshot(engine, identifier(receipt['metadata_snapshot']), payload)
    return receipt


def recover_encrypted_publication(data_root, directory, key, engine, repository_id, *,
                                  guard_factory=SpaceGuard, progress=None):
    """Finish publishing a retired source's capture from its retained metadata.

    Never unretires, activates, clears admission or reuses an unverified snapshot.
    An earlier attempt may leave an unreferenced snapshot; only the receipt counts.
    """
    data_root, directory = (Path(os.path.abspath(path)) for path in (data_root, directory))
    independent([data_root, directory, engine.repository])
    metadata = directory / 'metadata'
    admission = data_root / '.recovery-admission'
    directory_identity(admission)
    directory_identity(metadata)
    # A running capture holds this latch throughout, so this cannot race one.
    # Holding it also keeps the single-writer rule during the engine run.
    with copy_lock(admission):
        collect_idle(admission)
        if engine.repository_id() != identifier(repository_id):
            raise ValueError('Encrypted repository identity differs')
        payload = verify_metadata(metadata, key, repository_id, retirement=False)
        if not payload.get('migration_target'):
            raise InvalidArtifact('Only a capture that retired its source can be recovered here; '
                                  'otherwise resolve the failed maintenance and capture again')
        retired = admission / 'retired.json'
        if not retired.exists() and not retired.is_symlink():
            raise InvalidArtifact('This installation was not retired by the retained capture')
        record = read_record(retired)
        validate_record(record, payload)
        if read_library_identity(data_root) != payload['library_id']:
            raise InvalidArtifact('Retired installation belongs to another library')
        complete = directory / 'ENCRYPTED-COMPLETE.json'
        published = complete.exists() or complete.is_symlink()
        state = json.loads(read_bounded(directory / 'journal.json', 4096))
        if (not isinstance(state, dict) or state.get('id') != payload['id']
                or state.get('phase') not in RECOVERABLE_PHASES | ({'complete'} if published else set())):
            raise InvalidArtifact('Capture journal does not match a recoverable publication')
        media = tagged_snapshot(engine, payload['media_snapshot'], payload)
        if media['restore_root'] != payload.get('media_root'):
            raise InvalidArtifact('Encrypted media root differs')
        if published:
            receipt = verify_published_receipt(engine, complete, payload, repository_id)
        else:
            ensure_receipt(metadata, directory, key, record, payload, guard_factory=guard_factory)
            journal(directory, payload['id'], 'encrypted-catalog')
            allowed = {PASSWORD_FILE, 'recovery-point.json', 'library-index', 'retirement', 'catalog', 'inventory'}
            if any(path.name not in allowed and not abandoned_publication(path, ('recovery-point.json',))
                   for path in metadata.iterdir()):
                raise InvalidArtifact('Unexpected entry in retained encrypted metadata')
            regular_tree(metadata)
            size = sum(path.stat().st_size for path in metadata.rglob('*') if path.is_file())
            saved = engine.backup(metadata, repository_id, payload['id'], estimated_bytes=size + 1024 ** 2,
                                  guard_factory=guard_factory, progress=progress)
            tagged_snapshot(engine, saved['snapshot_id'], payload)
            # The published bytes must still be the ones authenticated above.
            if verify_metadata(metadata, key, repository_id) != payload:
                raise InvalidArtifact('Recovery metadata changed during publication')
            receipt = {'format': FORMAT, 'id': payload['id'], 'library_id': payload['library_id'],
                       'repository_id': repository_id, 'metadata_snapshot': saved['snapshot_id'],
                       'media_snapshot': payload['media_snapshot'], 'media_files': payload['media_files'],
                       'media_bytes': payload['media_bytes'], 'activation_allowed': False}
            save_private(complete, receipt)
        journal(directory, payload['id'], 'complete')
        return receipt


def verify_metadata(metadata, key, repository_id, *, retirement=True):
    path = metadata / 'recovery-point.json'
    file_digest(path)
    if path.stat().st_size > 65536:
        raise InvalidArtifact('Oversized encrypted recovery index')
    payload = json.loads(path.read_text(encoding='utf-8'))
    manifest = verify_artifact(metadata / 'library-index', key)
    if manifest['metadata'].get('kind') != FORMAT or manifest['size'] > 65536:
        raise InvalidArtifact('Invalid authenticated encrypted recovery index')
    authenticated = json.loads(b''.join(read_chunks(metadata / 'library-index', key, manifest)))
    if authenticated != payload:
        raise InvalidArtifact('Encrypted recovery index differs from its authenticated copy')
    if (not isinstance(payload, dict) or payload.get('format') != FORMAT
            or payload.get('activation_allowed') is not False or payload.get('repository_id') != repository_id):
        raise InvalidArtifact('Invalid encrypted recovery point')
    validate_context_binding(key, payload.get('password'))
    identifier(payload.get('media_snapshot'))
    for field in ('media_files', 'media_bytes'):
        if type(payload.get(field)) is not int or payload[field] < 0:
            raise InvalidArtifact('Invalid encrypted media totals')
    catalog = verify_catalog(metadata / 'catalog', key)
    if (catalog.get('id') != payload.get('id') or catalog.get('library_id') != payload.get('library_id')
            or reference(catalog) != payload.get('catalog')):
        raise InvalidArtifact('Encrypted catalog belongs to another recovery point')
    if file_digest(metadata / 'inventory/inventory.sqlite3') != payload.get('media_inventory_sha256'):
        raise InvalidArtifact('Encrypted media inventory differs')
    if retirement and payload.get('migration_target'):
        verify_retirement(metadata, key, payload)
    return payload


def restore_encrypted_library(engine, repository_id, metadata_snapshot, password, target, admin, pg_restore, *,
                              expected_cluster_directory, database_budget, metadata_budget,
                              resume=False, guard_factory=SpaceGuard, progress=None):
    """`password` is a BackupPassword: the key is unlocked from the context inside the restored
    metadata snapshot, before any of it is trusted or any catalog is restored."""
    if not isinstance(password, BackupPassword):
        raise TypeError('An encrypted library restore needs the administrator password')
    target = Path(os.path.abspath(target))
    independent([target, engine.repository])
    identifier(metadata_snapshot)
    engine.check(repository_id)
    guard_factory([Requirement('metadata', target.parent, metadata_budget)]).check()
    if resume:
        directory_identity(target)
    else:
        create_private_directory(target)
    with copy_lock(target):
        expected = {'format': FORMAT, 'repository_id': repository_id, 'metadata_snapshot': metadata_snapshot,
                    'target_identity': list(directory_identity(target)), 'volume': volume_identity(target),
                    'cluster': str(Path(expected_cluster_directory).resolve()),
                    'database_endpoint': [admin.host, admin.port, admin.user], 'activation_allowed': False}
        job = target / 'restore-job.json'
        if resume:
            file_digest(job)
            if (job.stat().st_size > 65536
                    or not same_job(json.loads(job.read_text(encoding='utf-8')), expected, 'target_identity')):
                raise InvalidArtifact('Encrypted restore job identity changed')
            if (not (target / 'LIBRARY-RESTORE-INCOMPLETE').is_file()
                    or (target / 'LIBRARY-RECOVERY-REPORT.json').exists()):
                raise InvalidArtifact('Only incomplete encrypted library restores can be resumed')
        else:
            save_private(job, expected)
            with (target / 'LIBRARY-RESTORE-INCOMPLETE').open('xb') as stream:
                stream.flush()
                os.fsync(stream.fileno())
            sync_directory(target)
        allowed = {'copy.lock', 'restore-job.json', 'LIBRARY-RESTORE-INCOMPLETE', 'metadata-job', 'media-job', 'catalog'}
        if any((path.name not in allowed and not abandoned_publication(path, ('restore-job.json', 'LIBRARY-RECOVERY-REPORT.json')))
               or path.is_symlink() or path.is_junction() for path in target.iterdir()):
            raise InvalidArtifact('Unexpected encrypted restore job entry')
        return _restore_encrypted_contents(engine, repository_id, metadata_snapshot, password, target, admin, pg_restore,
                    expected_cluster_directory=expected_cluster_directory, database_budget=database_budget,
                    metadata_budget=metadata_budget, guard_factory=guard_factory, progress=progress)


def _restore_encrypted_contents(engine, repository_id, metadata_snapshot, password, target, admin, pg_restore, *,
                                expected_cluster_directory, database_budget, metadata_budget, guard_factory, progress):
    marker = target / 'LIBRARY-RESTORE-INCOMPLETE'
    metadata_job = target / 'metadata-job'
    if progress:
        progress({'phase': 'restore-metadata', 'resuming': metadata_job.exists()})
    metadata = restore_snapshot_job(engine, metadata_snapshot, repository_id, metadata_job,
                   required_bytes=metadata_budget, resume=metadata_job.exists(), reuse_verified=True,
                   guard_factory=guard_factory)
    key = unlock_context(metadata, password)
    payload = verify_metadata(metadata, key, repository_id)
    for snapshot in (metadata_snapshot, payload['media_snapshot']):
        info = engine.snapshot(snapshot)
        if 'familyfoto:' + payload['id'] not in info.get('tags', []):
            raise InvalidArtifact('Encrypted snapshot belongs to another capture')
        if snapshot == payload['media_snapshot'] and info['restore_root'] != payload.get('media_root'):
            raise InvalidArtifact('Encrypted media root differs')
    state = verify_artifact(metadata / 'catalog/state', key)
    budget = payload['media_bytes'] + payload['media_files'] * 32768 + state['metadata']['file_bytes'] + 1024 ** 2
    guard_factory([Requirement('recovered-library', target, budget),
                   Requirement('database', Path(expected_cluster_directory), database_budget)]).check()
    media_job = target / 'media-job'
    check_paths(metadata / 'inventory/inventory.sqlite3', media_job / 'payload')
    if progress:
        progress({'phase': 'restore-media', 'resuming': media_job.exists()})
    media_root = restore_snapshot_job(engine, payload['media_snapshot'], repository_id, media_job,
                   required_bytes=budget, resume=media_job.exists(), reuse_verified=True, guard_factory=guard_factory)
    if progress:
        progress({'phase': 'verify-media'})
    with closing(sqlite3.connect((metadata / 'inventory/inventory.sqlite3').as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('PRAGMA trusted_schema=OFF')
        db.execute('BEGIN')
        verify_tree(db, media_root)
        files, size = db.execute("SELECT count(*),coalesce(sum(size),0) FROM entries WHERE kind='file'").fetchone()
        if (files, size) != (payload['media_files'], payload['media_bytes']):
            raise InvalidArtifact('Encrypted media totals differ')
    catalog = restore_catalog(metadata / 'catalog', key, target / 'catalog', admin, pg_restore,
                              expected_cluster_directory=expected_cluster_directory, database_budget=database_budget,
                              resume=(target / 'catalog').exists(), guard_factory=guard_factory, progress=progress)
    if read_library_identity(target / 'catalog/files/data') != payload['library_id']:
        raise InvalidArtifact('Recovered library identity differs')
    if verify_metadata(metadata, key, repository_id) != payload:
        raise InvalidArtifact('Recovery metadata changed during restore')
    settings = target / 'catalog/files/config/local.json'
    config = json.loads(settings.read_text(encoding='utf-8'))
    config['recovery'].update(media_verified=True, media_root=str(media_root),
                              media_inventory_sha256=payload['media_inventory_sha256'])
    save_private(settings, config)
    report = {**catalog, 'state': 'validation-only', 'media_verified': True, 'media_root': str(media_root),
              'library_id': payload['library_id'], 'restoration_id': payload.get('migration_target') or uuid.uuid4().hex,
              'media_files': files, 'media_bytes': size, 'activation_allowed': False,
              'media_inventory_sha256': payload['media_inventory_sha256'],
              'repository_id': repository_id, 'metadata_snapshot': metadata_snapshot,
              'source_retired': bool(payload.get('migration_target'))}
    save_private(target / 'catalog/RECOVERY-REPORT.json', report)
    save_private(target / 'LIBRARY-RECOVERY-REPORT.json', report)
    marker.unlink()
    sync_directory(target)
    return report
