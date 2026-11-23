"""Offline cross-component checks for a restored plain-library candidate.

This read-only check is not activation and does not prove target-native inference
or client playback qualification. Those gates remain separate.
"""
from dataclasses import replace
import hashlib
import json
from pathlib import Path

from ..backup.catalog import freeze_database
from ..backup.encrypted import read_bounded, verify_artifact, InvalidArtifact
from ..backup.identity import read_library_identity
from ..backup.library import reconcile_media
from ..backup.library_restore import equivalent_inventories
from ..backup.media_copy import copy_lock
from ..backup.media_receipt import verified_media
from ..backup.recovery import PAUSE_MARKERS
from ..backup.restore_database import _verify_database
from ..backup.retirement import verify_retirement, successor_id
from ..backup.space import directory_identity
from ..backup.state_files import walk_tar
from ..backup.volumes import same_except_device, volume_identity
from .credentials import owned_target, verify_staged_role
from .tls import verify_recovery_tls
from .state_validation import validate_file_state
from .candidate_layout import candidate_layout


def record(path):
    value = json.loads(read_bounded(path, 65536))
    if not isinstance(value, dict):
        raise InvalidArtifact('Recovery component record must be an object')
    return value


def archived_ca_digest(backup, key):
    artifact = backup / 'catalog/state'
    manifest = verify_artifact(artifact, key)
    found = []
    def inspect(name, size, stream):
        if str(name) == 'config/tls/ca.pem':
            if size > 65536:
                raise InvalidArtifact('Archived CA exceeds its bound')
            raw = stream.read(size)
            if len(raw) != size:
                raise InvalidArtifact('Archived CA is truncated')
            found.append(hashlib.sha256(raw).hexdigest())
    walk_tar(artifact, key, manifest, inspect)
    if len(found) != 1:
        raise InvalidArtifact('Recovery requires the backed-up managed TLS CA')
    return found[0]


def validate_plain_candidate(backup, key, directory, admin, role_directory, tls_directory, *,
                             expected_cluster_directory, maintenance_id=None, progress=None, fresh_restore=False,
                             reverify=False):
    """`fresh_restore` is for the run that just called restore_library with `validation_follows`.
    The restored media is still checked here, under the lock. Activation never sets it.

    The media is read in full unless a sealed receipt shows it already was and that no file
    has changed since. `reverify` reads it in full regardless."""
    backup, directory, role_directory, tls_directory = (
        Path(p).absolute() for p in (backup, directory, role_directory, tls_directory))
    payload, media, inventory_root, encrypted = candidate_layout(backup, key, directory, progress, fresh_restore,
                                                                reverify)
    retirement = verify_retirement(backup, key, payload) if payload.get('migration_target') else None
    cluster = Path(expected_cluster_directory).resolve(strict=True)
    with copy_lock(directory), copy_lock(role_directory):
        job = record(directory / 'restore-job.json')
        report = record(directory / 'LIBRARY-RECOVERY-REPORT.json')
        successor_id(report.get('restoration_id'))
        if encrypted:
            job_matches = (job.get('format') == 'familyfoto-library-restic-v2'
                           and job.get('repository_id') == payload['repository_id']
                           and job.get('metadata_snapshot') == report.get('metadata_snapshot')
                           and same_except_device(job.get('target_identity'), directory_identity(directory))
                           and job.get('database_endpoint') == [admin.host, admin.port, admin.user]
                           and report.get('repository_id') == payload['repository_id'])
        else:
            job_matches = (job.get('format') == 'familyfoto-plain-library-restore-job-v1'
                           and job.get('capture_id') == payload['id'] and job.get('backup') == str(backup)
                           and same_except_device(job.get('identity'), directory_identity(directory))
                           and job.get('endpoint') == [admin.host, admin.port, admin.user]
                           and report.get('restoration_id') == job.get('restoration_id'))
        if (not job_matches
                or job.get('volume') != volume_identity(directory) or job.get('cluster') != str(cluster)
                or report.get('capture_id') != payload['id'] or report.get('state') != 'validation-only'
                or report.get('activation_allowed') is not False or report.get('media_verified') is not True
                or report.get('library_id') != payload['library_id']):
            raise InvalidArtifact('Recovery candidate identity differs')
        if retirement and retirement['successor_id'] != report['restoration_id']:
            raise InvalidArtifact('Retirement receipt names another successor')
        files = directory / 'catalog/files'
        for marker in (directory / 'LIBRARY-RESTORE-INCOMPLETE', directory / 'catalog/RECOVERY-INCOMPLETE',
                       files / 'RESTORE-INCOMPLETE'):
            if marker.exists() or marker.is_symlink():
                raise InvalidArtifact('Recovery candidate is incomplete')
        if read_library_identity(files / 'data') != payload['library_id']:
            raise InvalidArtifact('Restored library identity differs')
        config = json.loads(read_bounded(files / 'config/local.json', 1024 ** 2))
        recovery = config.get('recovery', {})
        if (recovery.get('activation_required') is not True or recovery.get('media_verified') is not True
                or recovery.get('capture_id') != payload['id'] or recovery.get('media_root') != str(media)
                or config.get('destination') != {} or config.get('database', {}).get('user') != 'familyfoto_recovery_disabled'):
            raise InvalidArtifact('Recovery settings no longer describe a disabled candidate')
        for name in PAUSE_MARKERS:
            if read_bounded(files / 'data' / name, 1) != b'':
                raise InvalidArtifact('Recovered worker pause marker changed')
        if record(files / 'data/face-service-settings.json').get('start_with_app') is not False:
            raise InvalidArtifact('Recovered face service is not disabled')
        inventory = inventory_root / 'inventory.sqlite3'
        if not encrypted:
            # The recovery folder's own copy, or a split restore's media folder.
            copy = media.parent
            # After candidate_layout's full read this finds its receipt and only walks the tree.
            verified_media(directory, key, copy, progress=progress)
            equivalent_inventories(inventory, copy / 'inventory/inventory.sqlite3')
        database = record(directory / 'catalog/database-target.json')
        credentials = record(role_directory / 'credentials.json')
        if (credentials.get('database') != database.get('database')
                or credentials.get('database_oid') != database.get('database_oid')
                or credentials.get('database_token') != database.get('ownership_token')
                or credentials.get('cluster_directory') != str(cluster)
                or credentials.get('endpoint') != [admin.host, admin.port, admin.user]
                or not same_except_device(credentials.get('directory'), directory_identity(role_directory))
                or credentials.get('volume') != volume_identity(role_directory)):
            raise InvalidArtifact('Staged credentials belong to another recovery candidate')
        target = replace(admin, dbname=database['database'])
        configured_database = config['database']
        if any(configured_database.get(name) != getattr(target, name) for name in ('host', 'port', 'dbname')):
            raise InvalidArtifact('Disabled database settings point to another candidate')
        with freeze_database(target) as db:
            owned_target(db, database, cluster)
            validate_file_state(backup / 'catalog/state', key, files, target, payload['id'], media,
                                payload['media_inventory_sha256'], maintenance_id=maintenance_id)
            artifact = verify_artifact(backup / 'catalog/database', key)
            if (database.get('verified') is not True or database.get('activation_allowed') is not False
                    or database.get('artifact_id') != artifact['id']
                    or database.get('artifact_sha256') != artifact['sha256']):
                raise InvalidArtifact('Database journal belongs to another archive')
            metadata = artifact['metadata']
            _verify_database(target, metadata, sanitized=True)
            reconcile_media(db, inventory.parent)
            verify_staged_role(db, credentials, record(role_directory / 'ROLE-REPORT.json'))
            tls = verify_recovery_tls(tls_directory, expected_ca_sha256=archived_ca_digest(backup, key))
        return {'state': 'candidate-validated', 'capture_id': payload['id'], 'library_id': payload['library_id'],
                'restoration_id': report['restoration_id'], 'source_retired': retirement is not None,
                'tls_fingerprint': tls['certificate_fingerprint'], 'activation_allowed': False,
                'native_inference_qualified': False, 'services_started': False}
