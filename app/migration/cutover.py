"""Offline disk switch with an encrypted, explicitly recoverable intent.

Rollback is permitted only while the original maintenance gate still excludes
writers. After release, the old copy is a recovery source, not a live fallback.
"""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import uuid

from ..backup.admission import MaintenanceBlocked, resume_maintenance
from ..backup.catalog import catalog_capture_scope, freeze_database, verify_catalog
from ..backup.encrypted import read_bounded, read_chunks, seal_stream, verify_artifact
from ..backup.library import reconcile_media
from ..backup.media_copy import copy_lock, sync_directory
from ..backup.passwords import publish as publish_password, require_context, validate_context_binding
from ..backup.media_inventory import verify_media_inventory
from ..backup.media_tree import file_digest, verify_media_tree
from ..backup.recovery import save_private
from ..backup.space import SpaceGuard, directory_identity
from ..backup.volumes import volume_identity
from .disk import disk_binding, settings_snapshot, source_binding


FORMAT = 'familyfoto-disk-cutover-v1'


def validate_job(directory, config_root, data_root, job):
    if (job.get('format') != 'familyfoto-disk-move-v1'
            or job.get('state') != 'ready-for-cutover-review'
            or job.get('config_root') != str(config_root) or job.get('data_root') != str(data_root)
            or job.get('job_directory') != list(directory_identity(directory))
            or job.get('job_volume') != volume_identity(directory)):
        raise MaintenanceBlocked('Disk preparation identity changed')
    source = Path(job['source'])
    if (job['source_directory'] != list(directory_identity(source))
            or job['source_volume'] != volume_identity(source)
            or job['new_destination']['root'] != str(directory / 'copy/media')
            or job['old_destination']['root'] != str(source)):
        raise MaintenanceBlocked('Prepared media roots changed')
    identity = json.loads(read_bounded(data_root / '.recovery-admission/library.json', 4096))
    if identity.get('library_id') != job['library_id']:
        raise MaintenanceBlocked('Logical library identity changed')


def validate_media(directory, job, old_config, db, probe):
    source, marker = source_binding(old_config, probe)
    if marker['library_id'] != job['marker_library_id'] or str(source) != job['source']:
        raise MaintenanceBlocked('Source marker changed')
    if probe(directory / 'copy/media') != job['new_destination']:
        raise MaintenanceBlocked('Destination disk changed')
    inventory = directory / 'inventory'
    if file_digest(inventory / 'inventory.sqlite3') != job['inventory_sha256']:
        raise MaintenanceBlocked('Prepared inventory changed')
    verify_media_inventory(inventory, source, rehash=True)
    verify_media_tree(directory / 'copy')
    # Both copies must match the SAME inventory, not just their own manifests.
    if file_digest(directory / 'copy/inventory/inventory.sqlite3') != job['inventory_sha256']:
        raise MaintenanceBlocked('Copied media belongs to a different inventory')
    reconcile_media(db, inventory)


def replace_settings(path, raw):
    """Atomic publication; retain an abandoned attempt after any write failure."""
    read_bounded(path, 1024 ** 2)
    if path.stat().st_nlink != 1:
        raise MaintenanceBlocked('Configuration has unexpected hard links')
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.part')
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    sync_directory(path.parent)
    if read_bounded(path, 1024 ** 2) != raw:
        raise MaintenanceBlocked('Configuration publication failed verification')


def apply_intent(directory, config_root, intent, *, rollback=False):
    old = base64.b64decode(intent['old_config'], validate=True)
    new = base64.b64decode(intent['new_config'], validate=True)
    current = read_bounded(config_root / 'local.json', 1024 ** 2)
    if current not in (old, new):
        raise MaintenanceBlocked('Configuration was independently changed; refusing to overwrite it')
    marker_path = directory / 'copy/.familyfoto-volume.json'
    marker = {**intent['job']['new_destination'], 'library_id': intent['job']['marker_library_id']}
    if marker_path.exists() or marker_path.is_symlink():
        if json.loads(read_bounded(marker_path, 65536)) != marker:
            raise MaintenanceBlocked('Destination marker belongs to another configuration')
    elif not rollback:
        save_private(marker_path, marker)
    replace_settings(config_root / 'local.json', old if rollback else new)
    report = {'format': FORMAT, 'job_id': intent['job']['id'],
              'state': 'rolled-back' if rollback else 'complete',
              'catalog_capture_id': intent['catalog']['id'], 'old_media_retained': True,
              'services_started': False}
    save_private(directory / 'CUTOVER-REPORT.json', report)
    return report


def check_endpoint(endpoint, config):
    if any(config['database'].get(name) != getattr(endpoint, name)
           for name in ('host', 'port', 'dbname', 'user')):
        raise MaintenanceBlocked('Relocation database differs from the source configuration')


def commit_disk_move(endpoint, pg_dump, config_root, data_root, directory, backup, key, *,
                     database_budget, offline_confirmed=False, guard_factory=SpaceGuard,
                     progress=None, probe=disk_binding):
    if offline_confirmed is not True:
        raise MaintenanceBlocked('Explicit offline confirmation is required')
    config_root, data_root, directory, backup = (
        Path(os.path.abspath(path)) for path in (config_root, data_root, directory, backup))
    with copy_lock(directory):
        if (directory / 'cutover-intent').exists() or (directory / 'CUTOVER-REPORT.json').exists():
            raise MaintenanceBlocked('Cutover already attempted; use explicit interrupted-switch recovery')
        job = json.loads(read_bounded(directory / 'READY.json', 65536))
        validate_job(directory, config_root, data_root, job)
        config, digest = settings_snapshot(config_root)
        check_endpoint(endpoint, config)
        if digest != job['config_sha256']:
            raise MaintenanceBlocked('Configuration changed since preparation; prepare a new disk move')
        for root in (config_root.parent, data_root, directory, Path(job['source'])):
            if backup.is_relative_to(root) or root.is_relative_to(backup):
                raise ValueError('Cutover catalog backup must be separate from application and media roots')
        with catalog_capture_scope(endpoint, pg_dump, config_root, data_root, backup, key,
                database_budget=database_budget, offline_confirmed=True,
                guard_factory=guard_factory, progress=progress) as catalog:
            with freeze_database(endpoint) as db:
                validate_job(directory, config_root, data_root, job)
                validate_media(directory, job, config, db, probe)
                old = read_bounded(config_root / 'local.json', 1024 ** 2)
                if hashlib.sha256(old).hexdigest() != digest or catalog['library_id'] != job['library_id']:
                    raise MaintenanceBlocked('Source changed during cutover preparation')
                new = json.dumps({**config, 'destination': job['new_destination']}, indent=2).encode()
                admission = json.loads(read_bounded(data_root / '.recovery-admission/maintenance.json', 4096))
                intent = {'format': FORMAT, 'job': job, 'admission': admission,
                          'backup': str(backup), 'catalog': catalog,
                          'old_config': base64.b64encode(old).decode(),
                          'new_config': base64.b64encode(new).decode()}
                raw = json.dumps(intent).encode()
                publish_password(directory, key)
                seal_stream(io.BytesIO(raw), directory / 'cutover-intent', key, estimated_bytes=len(raw),
                            guard_factory=guard_factory, metadata={'kind': FORMAT})
                # Publication has completed before either live pathname changes.
                return apply_intent(directory, config_root, intent)


def recover_disk_move(endpoint, config_root, data_root, directory, key, *,
                      rollback=False, offline_confirmed=False, probe=disk_binding):
    if offline_confirmed is not True:
        raise MaintenanceBlocked('Explicit offline confirmation is required')
    config_root, data_root, directory = (
        Path(os.path.abspath(path)) for path in (config_root, data_root, directory))
    with copy_lock(directory):
        artifact = directory / 'cutover-intent'
        require_context(directory, key)
        manifest = verify_artifact(artifact, key)
        if manifest['metadata'].get('kind') != FORMAT or manifest['size'] > 4 * 1024 ** 2:
            raise ValueError('Not a supported disk cutover intent')
        intent = json.loads(b''.join(read_chunks(artifact, key, manifest)))
        if intent.get('format') != FORMAT:
            raise ValueError('Unsupported disk cutover intent')
        validate_context_binding(key, (intent.get('catalog') or {}).get('password'))
        job = intent['job']
        validate_job(directory, config_root, data_root, job)
        old = json.loads(base64.b64decode(intent['old_config'], validate=True))
        check_endpoint(endpoint, old)
        admission = intent['admission']
        with resume_maintenance(data_root, admission['id'], admission['operation']):
            with freeze_database(endpoint) as db:
                if verify_catalog(Path(intent['backup']), key) != intent['catalog']:
                    raise MaintenanceBlocked('Cutover recovery set changed')
                validate_media(directory, job, old, db, probe)
                return apply_intent(directory, config_root, intent, rollback=rollback)
