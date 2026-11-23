"""Standalone full-only capture using the existing password and recovery format."""
import os
from pathlib import Path
import uuid
from contextlib import ExitStack

from app.backup import library as shared
from app.backup.passwords import BackupKey
from app.backup.space import Requirement, SpaceGuard
from app.backup.media_copy import copy_lock
from .copy_worker import copy_in_worker


def capture_library(endpoint, pg_dump, config_root, data_root, media_root, directory, key, *,
                    database_budget, offline_confirmed=False, allow_unprotected=False,
                    progress=None, cancelled=lambda: False, guard_factory=SpaceGuard,
                    copy_timeout=7 * 86400):
    """New destination only; full verification; no retirement, activation or resume.

    Participating writers are excluded for the entire capture. External writers
    must be stopped by the operator. Failure retains artifacts and the existing
    maintenance failure state; it does not resume scans or restart services.
    """
    if offline_confirmed is not True:
        raise shared.MaintenanceBlocked('Offline confirmation is required')
    if not isinstance(key, BackupKey):
        raise TypeError('An administrator-password BackupKey is required')
    if type(database_budget) is not int or database_budget < 0:
        raise ValueError('Non-negative database budget required')
    config_root, data_root, media_root, directory = [Path(os.path.abspath(p)) for p in
                                                   (config_root, data_root, media_root, directory)]
    for root in (config_root, data_root, media_root):
        shared.directory_identity(root)
        if root.is_relative_to(directory) or directory.is_relative_to(root):
            raise ValueError('Backup must be separate from all source roots')
    if os.path.lexists(directory):
        raise FileExistsError('Fast capture requires a new directory; never resumes')
    shared.check_destination_privacy(directory.parent, allow_unprotected=allow_unprotected)
    identity = uuid.uuid4().hex
    timer = shared.Timing(single_pass=False, resumed=False)
    def stage(name):
        if cancelled():
            raise InterruptedError('Capture cancelled')
        if directory.exists():
            shared.journal(directory, identity, name)
        if progress:
            progress(dict(phase=name, state='starting'))
    with shared.maintenance(data_root, 'backup') as admission_id, ExitStack() as locks:
        with shared.freeze_database(endpoint) as db:
            library_id = shared.ensure_library_identity(data_root, admission_id)
            shared.reconcile_uploads(db, data_root)
            records = shared.state_inventory({'config': config_root, 'data': data_root})
            state_bytes = sum(row['size'] for row in records) + len(records) * 16384 + 10240
            catalog_budget = (shared.storage_budget(database_budget) + shared.storage_budget(state_bytes)
                              + shared.storage_budget(shared.thumbnail_budget(data_root)) + 1024**2)
            guard_factory([Requirement('backup', directory.parent, catalog_budget)]).check()
            shared.create_private_directory(directory, allow_unprotected=allow_unprotected)
            locks.enter_context(copy_lock(directory))
            shared.publish_password(directory, key)
            shared.write_capture_job(directory, key,
                shared.capture_binding(config_root, data_root, media_root, directory, None),
                identity, library_id, engine='fastbackup-v1')
            timer.path = directory / shared.TIMING
            stage('media-inventory')
            with timer.phase('media-inventory'):
                inventory = shared.capture_media_inventory(media_root, directory / 'inventory',
                    offline_confirmed=True, guard_factory=guard_factory, progress=progress,
                    allow_unprotected=allow_unprotected, assume_immutable_source=True,
                    known_digest=shared.catalog_digests(db, media_root, assume_immutable_source=True))
            with timer.phase('reconcile'):
                counts = shared.reconcile_media(db, directory / 'inventory')
            overhead = (directory / 'inventory/inventory.sqlite3').stat().st_size * 3 + inventory['files'] * 32768
            guard_factory([Requirement('backup', directory, catalog_budget + inventory['bytes'] + overhead)]).check()
            # Freeze catalog and inventory before the long transfer; all artifacts
            # stay inside this same maintenance/database consistency window.
            stage('catalog')
            with timer.phase('catalog'):
                components = shared.capture_components(endpoint, pg_dump, config_root, data_root,
                    directory / 'catalog', key, identity, database_budget=database_budget,
                    guard_factory=guard_factory, progress=progress)
            stage('thumbnails')
            with timer.phase('thumbnails'):
                thumbnails = shared.capture_thumbnails(data_root, directory / shared.THUMBNAILS, key,
                    guard_factory=guard_factory, progress=progress)
            stage('media-copy')
            with timer.phase('media-copy'):
                media = copy_in_worker(media_root, directory / 'inventory', directory / 'media-copy',
                    offline_confirmed=True, guard_factory=guard_factory, progress=progress,
                    cancelled=cancelled, allow_unprotected=allow_unprotected, timeout=copy_timeout)
            if media.get('content_verified') is not True:
                raise ValueError('Fast capture requires full destination verification')
            with timer.phase('reconcile'):
                shared.reconcile_media(db, directory / 'inventory')
            shared.reconcile_uploads(db, data_root)
        stage('publication')
        catalog = shared.publish_catalog(directory / 'catalog', key, identity, components,
                                         guard_factory=guard_factory, library_id=library_id)
        payload = shared.publish_library_index(directory, key, identity, library_id, catalog, media,
                                               counts, thumbnails, guard_factory=guard_factory)
        shared.journal(directory, identity, 'complete')
    return payload
