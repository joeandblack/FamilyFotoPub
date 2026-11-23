"""Offline disk relocation preparation. Never switches or deletes active storage."""
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid

from ..backup.admission import MaintenanceBlocked, resume_maintenance
from ..backup.catalog import catalog_capture_scope, freeze_database, verify_catalog
from ..backup.encrypted import read_bounded
from ..backup.library import reconcile_media
from ..backup.media_copy import copy_lock
from ..backup.media_inventory import capture_media_inventory, verify_media_inventory
from ..backup.media_tree import copy_media_tree, verify_media_tree, file_digest
from ..backup.private_storage import create_private_directory
from ..backup.recovery import save_private
from ..backup.space import SpaceGuard, directory_identity
from ..backup.volumes import volume_identity
from ..storage.config import storage_drive
from ..storage.disks import snapshot, match_disk


def disk_binding(path):
    """Probe the same platform identifiers used by ordinary application writes."""
    drive = storage_drive(path)
    disk = snapshot([drive])[drive]
    if not disk.get('online') or (sys.platform == 'darwin' and not disk.get('writable')):
        raise ValueError('Destination disk is offline or read-only')
    return {'root': str(path), 'drive': drive,
            **{name: disk[name] for name in ('serial', 'volume_serial', 'volume_id')}}


def settings_snapshot(config_root):
    raw = read_bounded(config_root / 'local.json', 1024 ** 2)
    config = json.loads(raw)
    if not isinstance(config, dict) or config.get('recovery', {}).get('activation_required'):
        raise ValueError('Disk relocation requires an active source configuration')
    return config, hashlib.sha256(raw).hexdigest()


def source_binding(config, probe):
    destination = config['destination']
    source = Path(destination['root'])
    if not source.is_absolute():
        raise ValueError('An absolute enrolled media root is required')
    directory_identity(source)
    match_disk(destination, probe(source))
    marker = json.loads(read_bounded(source.parent / '.familyfoto-volume.json', 65536))
    match_disk(destination, marker)
    if marker.get('root') != destination['root'] or not isinstance(marker.get('library_id'), str):
        raise ValueError('Source library marker does not match its configured root')
    return source, marker


def prepare_disk_move(endpoint, pg_dump, config_root, data_root, directory, backup, key, *,
                      database_budget, offline_confirmed=False, guard_factory=SpaceGuard,
                      progress=None, probe=disk_binding, resume=False):
    """Copy once to a new disk job; keep a separate encrypted catalog recovery set.

    A ready report is not permission to write to the new disk. Cutover must recheck
    the old configuration, both volume identities and the full media inventory.
    """
    if offline_confirmed is not True:
        raise MaintenanceBlocked('Stop application and external writers before disk relocation')
    config_root, data_root, directory, backup = (
        Path(os.path.abspath(path)) for path in (config_root, data_root, directory, backup))
    config, config_hash = settings_snapshot(config_root)
    database = config['database']
    if any(database.get(name) != getattr(endpoint, name) for name in ('host', 'port', 'dbname', 'user')):
        raise ValueError('Relocation database differs from the source configuration')
    source, marker = source_binding(config, probe)
    if resume:
        return resume_preparation(endpoint, config_root, data_root, directory, backup, key,
                                  config, config_hash, source, marker, guard_factory, progress, probe)
    for output in (directory, backup):
        directory_identity(output.parent)
        if output.exists():
            raise FileExistsError('Relocation requires new job and catalog backup directories')
        for root in (config_root.parent, data_root, source):
            if output.is_relative_to(root) or root.is_relative_to(output):
                raise ValueError('Relocation outputs must be separate from application state and media')
    if directory.is_relative_to(backup) or backup.is_relative_to(directory):
        raise ValueError('Catalog recovery set must be separate from the disk copy job')
    with catalog_capture_scope(endpoint, pg_dump, config_root, data_root, backup, key,
            database_budget=database_budget, offline_confirmed=True,
            guard_factory=guard_factory, progress=progress) as catalog:
        if settings_snapshot(config_root)[1] != config_hash:
            raise MaintenanceBlocked('Configuration changed before disk preparation')
        create_private_directory(directory)
        with copy_lock(directory):
            job = {'format': 'familyfoto-disk-move-v1', 'id': uuid.uuid4().hex,
                   'config_root': str(config_root), 'data_root': str(data_root),
                   'config_sha256': config_hash, 'source': str(source),
                   'source_directory': list(directory_identity(source)), 'source_volume': volume_identity(source),
                   'job_directory': list(directory_identity(directory)), 'job_volume': volume_identity(directory),
                   'old_destination': config['destination'], 'marker_library_id': marker['library_id'],
                   'library_id': catalog['library_id'], 'capture_id': catalog['id'], 'backup': str(backup),
                   'admission': json.loads(read_bounded(data_root / '.recovery-admission/maintenance.json', 4096)),
                   'activation_allowed': False}
            save_private(directory / 'move-job.json', job)
            return prepare_media(endpoint, config_root, directory, backup, key, config, source,
                                 marker, job, catalog, guard_factory, progress, probe)


def prepare_media(endpoint, config_root, directory, backup, key, config, source, marker,
                  job, catalog, guard_factory, progress, probe):
    with freeze_database(endpoint) as db:
        inventory = directory / 'inventory'
        if not inventory.exists():
            capture_media_inventory(source, inventory, offline_confirmed=True,
                                    guard_factory=guard_factory, progress=progress)
        # Never rebuild a completed inventory: existing copy checkpoints bind its
        # exact bytes. Incomplete inventory capture requires separate diagnosis.
        verify_media_inventory(inventory, source, rehash=True)
        reconcile_media(db, inventory)
        copy_media_tree(source, inventory, directory / 'copy', offline_confirmed=True,
                        guard_factory=guard_factory, progress=progress)
        verify_media_tree(directory / 'copy')
        verify_media_inventory(inventory, source, rehash=True)
        reconcile_media(db, inventory)
        if (settings_snapshot(config_root)[1] != job['config_sha256']
                or source_binding(config, probe)[1] != marker
                or volume_identity(source) != job['source_volume']
                or volume_identity(directory) != job['job_volume']):
            raise MaintenanceBlocked('Source configuration or disk identity changed during preparation')
    if verify_catalog(backup, key) != catalog:
        raise MaintenanceBlocked('Catalog recovery set changed during preparation')
    report = {**job, 'state': 'ready-for-cutover-review', 'new_destination': probe(directory / 'copy/media'),
              'inventory_sha256': file_digest(inventory / 'inventory.sqlite3'),
              'old_media_retained': True, 'configuration_changed': False}
    save_private(directory / 'READY.json', report)
    return report


def resume_preparation(endpoint, config_root, data_root, directory, backup, key, config,
                       config_hash, source, marker, guard_factory, progress, probe):
    """Reuse an owned copy only while the original failed operation is still gated."""
    directory_identity(directory)
    with copy_lock(directory):
        if (directory / 'cutover-intent').exists() or (directory / 'CUTOVER-REPORT.json').exists():
            raise MaintenanceBlocked('Cutover already started; preparation cannot be resumed')
        job = json.loads(read_bounded(directory / 'move-job.json', 65536))
        expected = {'format': 'familyfoto-disk-move-v1', 'config_root': str(config_root),
                    'data_root': str(data_root), 'config_sha256': config_hash, 'source': str(source),
                    'source_directory': list(directory_identity(source)), 'source_volume': volume_identity(source),
                    'job_directory': list(directory_identity(directory)), 'job_volume': volume_identity(directory),
                    'old_destination': config['destination'], 'marker_library_id': marker['library_id'],
                    'backup': str(backup), 'activation_allowed': False}
        if any(job.get(name) != value for name, value in expected.items()) or 'admission' not in job:
            raise MaintenanceBlocked('Preparation configuration, disk or ownership changed')
        admission = job['admission']
        with resume_maintenance(data_root, admission['id'], admission['operation']):
            catalog = verify_catalog(backup, key)
            from ..backup.identity import read_library_identity
            if (catalog['id'] != job['capture_id'] or catalog.get('library_id') != job['library_id']
                    or read_library_identity(data_root) != job['library_id']):
                raise MaintenanceBlocked('Preparation recovery set or library identity changed')
            return prepare_media(endpoint, config_root, directory, backup, key, config, source,
                                 marker, job, catalog, guard_factory, progress, probe)
