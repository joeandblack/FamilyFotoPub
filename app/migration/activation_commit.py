"""Explicit recovery activation with a durable, irreversible write-access fence.

Before the fence, an interrupted configuration switch can be rolled back. Once
the fence exists, a database commit may have enabled writes: only finishing is
safe, even if the application maintenance marker still exists.
"""
import base64
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path

from ..backup.admission import MaintenanceBlocked, maintenance, resume_maintenance
from ..backup.catalog import freeze_database
from ..backup.encrypted import read_bounded, read_chunks, seal_stream, verify_artifact
from ..backup.media_copy import copy_lock, sync_directory
from ..backup.passwords import require_context, validate_context_binding
from ..backup.recovery import save_private
from ..backup.space import SpaceGuard, directory_identity
from ..backup.volumes import volume_identity
from .activation import FORMAT, acceptance_checks
from .activation_database import activate_database, activation_comment, require_activation_gate, rollback_database
from .credentials import owned_target, verify_staged_role
from .cutover import replace_settings
from .disk import disk_binding
from .tls import verify_recovery_tls
from .state_validation import validate_file_state
from .validation import record, validate_plain_candidate
from .candidate_layout import verify_candidate_media
from ..backup.library_restore import equivalent_inventories
from ..backup.media_receipt import verified_media


JOURNAL_FORMAT = 'familyfoto-activation-switch-v1'


def sealed_record(path, key, kind):
    manifest = verify_artifact(path, key)
    if manifest['metadata'].get('kind') != kind or manifest['size'] > 4 * 1024 ** 2:
        raise MaintenanceBlocked('Unsupported activation record')
    value = json.loads(b''.join(read_chunks(path, key, manifest)))
    if not isinstance(value, dict) or value.get('format') != kind:
        raise MaintenanceBlocked('Invalid activation record')
    return value


def seal_record(path, key, value, guard_factory):
    raw = json.dumps(value, sort_keys=True).encode()
    seal_stream(io.BytesIO(raw), path, key, estimated_bytes=len(raw),
                metadata={'kind': value['format']}, guard_factory=guard_factory)


def check_identity(intent, admin, probe):
    directory = Path(intent['directory'])
    media = Path(intent['media_root'])
    if (intent['endpoint'] != [admin.host, admin.port, admin.user]
            or intent['candidate_identity'] != list(directory_identity(directory))
            or intent['candidate_volume'] != volume_identity(directory)
            or intent['media_identity'] != list(directory_identity(media))
            or intent['media_volume'] != volume_identity(media)
            or intent['destination'] != probe(media)):
        raise MaintenanceBlocked('Prepared activation target identity changed')
    acceptance_checks(intent['accepted_checks'])
    if not intent['validation']['source_retired'] and intent['source_unavailable_acknowledged'] is not True:
        raise MaintenanceBlocked('Source retirement or unavailable acknowledgement is required')
    for path, expected in ((Path(intent['role_directory']) / 'credentials.json', intent['credentials']),
                           (Path(intent['role_directory']) / 'ROLE-REPORT.json', intent['role_report']),
                           (directory / 'catalog/database-target.json', intent['database'])):
        if record(path) != expected:
            raise MaintenanceBlocked('Activation input changed after preparation')
    tls = verify_recovery_tls(Path(intent['tls_directory']),
                              expected_ca_sha256=intent['tls_report']['ca_sha256'])
    if tls != intent['tls_report']:
        raise MaintenanceBlocked('Prepared TLS identity changed')
    return directory, media


def configurations(intent):
    old = base64.b64decode(intent['original_configuration'], validate=True)
    if hashlib.sha256(old).hexdigest() != intent['configuration_sha256']:
        raise MaintenanceBlocked('Original activation configuration digest differs')
    cfg = json.loads(old)
    credentials = intent['credentials']
    cfg.update(host=intent['hostname'], port=intent['port'], source={}, destination=intent['destination'],
               database={'host': intent['endpoint'][0], 'port': intent['endpoint'][1],
                         'dbname': credentials['database'], 'user': credentials['role'],
                         'password': credentials['password']},
               tls={'certfile': str(Path(intent['tls_directory']) / 'server.pem'),
                    'keyfile': str(Path(intent['tls_directory']) / 'server-key.pem')})
    cfg['recovery'].update(state='activated', activation_required=False,
                           activation_id=intent['id'], native_inference_qualified=False)
    return old, json.dumps(cfg, indent=2).encode()


def publish_configuration(intent, *, rollback=False):
    directory = Path(intent['directory'])
    path = directory / 'catalog/files/config/local.json'
    old, new = configurations(intent)
    if read_bounded(path, 1024 ** 2) not in (old, new):
        raise MaintenanceBlocked('Recovery configuration was independently changed')
    marker_path = Path(intent['media_root']).parent / '.familyfoto-volume.json'
    marker = {**intent['destination'], 'library_id': intent['validation']['library_id']}
    if marker_path.exists() or marker_path.is_symlink():
        if record(marker_path) != marker:
            raise MaintenanceBlocked('Storage enrollment marker belongs to another library')
    elif not rollback:
        save_private(marker_path, marker)
    replace_settings(path, old if rollback else new)
    if rollback and marker_path.exists():
        marker_path.unlink()
        sync_directory(marker_path.parent)


def finish_switch(intent, journal, output, key, admin, *, rollback, guard_factory, checkpoint):
    directory = Path(intent['directory'])
    data = directory / 'catalog/files/data'
    cluster = Path(intent['cluster_directory']).resolve(strict=True)
    target = replace(admin, dbname=intent['database']['database'])
    fence_path = output / 'write-access-fence'
    fenced = fence_path.exists()
    if fenced:
        if sealed_record(fence_path, key, JOURNAL_FORMAT) != journal:
            raise MaintenanceBlocked('Activation write-access fence differs')
        if rollback:
            raise MaintenanceBlocked('Write access may have been enabled; rollback is no longer safe')
    require_activation_gate(data, journal['maintenance_id'])
    with freeze_database(target) as db:
        current = db.execute("SELECT shobj_description(oid,'pg_database') FROM pg_database "
                             "WHERE datname=current_database()").fetchone()[0]
        if current != activation_comment(intent['database'], intent['id']):
            owned_target(db, intent['database'], cluster)
            verify_staged_role(db, intent['credentials'], intent['role_report'])
            encrypted = intent.get('encrypted_media') is True
            if not encrypted:
                # A full read, or the receipt validation has just left of one.
                copy = Path(intent['media_root']).parent
                verified_media(directory, key, copy)
                # That read was against the recovery folder's own inventory copy. Unless it is
                # the sealed inventory itself, as after an in-place restore, compare the two.
                sealed, own = Path(intent['inventory_root']) / 'inventory.sqlite3', copy / 'inventory/inventory.sqlite3'
                if os.path.abspath(sealed) != os.path.abspath(own):
                    equivalent_inventories(sealed, own)
            verify_candidate_media(Path(intent['media_root']), Path(intent['inventory_root']),
                                   intent['recovery_payload'], content=encrypted)
            old, _ = configurations(intent)
            cfg = json.loads(old)
            validate_file_state(Path(intent['backup']) / 'catalog/state', key, directory / 'catalog/files',
                                target, intent['validation']['capture_id'], Path(intent['media_root']),
                                cfg['recovery']['media_inventory_sha256'], maintenance_id=journal['maintenance_id'],
                                original_configuration=old)
        if rollback:
            # No login has ever been enabled by this coordinator before its fence.
            owned_target(db, intent['database'], cluster)
            verify_staged_role(db, intent['credentials'], intent['role_report'])
            publish_configuration(intent, rollback=True)
        else:
            publish_configuration(intent)
            checkpoint('configuration-published')
            if not fenced:
                seal_record(fence_path, key, journal, guard_factory)
            checkpoint('write-access-fenced')
            if current != activation_comment(intent['database'], intent['id']):
                activate_database(db, intent['database'], intent['credentials'], intent['role_report'],
                                  cluster, intent['id'], data_root=data, maintenance_id=journal['maintenance_id'])
            else:
                # A committed transition can only be completed, never reversed.
                if not fenced or Path(db.execute('SHOW data_directory').fetchone()[0]).resolve() != cluster:
                    raise MaintenanceBlocked('Activated database identity differs')
                row = db.execute('SELECT oid FROM pg_database WHERE datname=current_database()').fetchone()
                if row[0] != intent['database']['database_oid']:
                    raise MaintenanceBlocked('Activated database was replaced')
                # Check the complete staged-role contract transactionally. No
                # NOLOGIN state is committed or visible to another connection.
                rollback_database(db, intent['database'], intent['credentials'], intent['role_report'],
                                  cluster, intent['id'], data_root=data, maintenance_id=journal['maintenance_id'])
                activate_database(db, intent['database'], intent['credentials'], intent['role_report'],
                                  cluster, intent['id'], data_root=data, maintenance_id=journal['maintenance_id'])
            require_activation_gate(data, journal['maintenance_id'])
    checkpoint('database-committed')
    report = {'format': JOURNAL_FORMAT, 'intent_id': intent['id'],
              'state': 'rolled-back' if rollback else 'activated', 'services_started': False,
              'jobs_resumed': False, 'native_inference_qualified': False,
              'configuration': str(directory / 'catalog/files/config/local.json')}
    save_private(output / 'ACTIVATION-REPORT.json', report)
    return report


def commit_activation(intent_directory, key, admin, *, offline_confirmed=False, resume=False, rollback=False,
                      probe=disk_binding, guard_factory=SpaceGuard, checkpoint=lambda phase: None, reverify=False):
    """Change only the sealed candidate. Never launch a server or resume a job.

    `reverify` reads all the media once more first, instead of relying on the receipt of
    the read that validation made."""
    if offline_confirmed is not True or (rollback and not resume):
        raise MaintenanceBlocked('Explicit offline confirmation and interrupted-switch recovery are required')
    output = Path(intent_directory).absolute()
    require_context(output, key)
    intent = sealed_record(output, key, FORMAT)
    validate_context_binding(key, (intent.get('recovery_payload') or {}).get('password'))
    directory, _ = check_identity(intent, admin, probe)
    data = directory / 'catalog/files/data'
    with copy_lock(output):
        journal_path = output / 'switch-journal'
        if resume:
            journal = sealed_record(journal_path, key, JOURNAL_FORMAT)
            if journal['intent_id'] != intent['id']:
                raise MaintenanceBlocked('Activation journal belongs to another intent')
            gate = resume_maintenance(data, journal['maintenance_id'], 'restore')
        else:
            if journal_path.exists() or (output / 'ACTIVATION-REPORT.json').exists():
                raise MaintenanceBlocked('Activation already attempted; explicitly recover the interrupted switch')
            gate = maintenance(data, 'restore')
        with gate as maintenance_id:
            if not resume:
                validation = validate_plain_candidate(Path(intent['backup']), key, directory, admin,
                             Path(intent['role_directory']), Path(intent['tls_directory']),
                             expected_cluster_directory=Path(intent['cluster_directory']), maintenance_id=maintenance_id,
                             reverify=reverify)
                if validation != intent['validation']:
                    raise MaintenanceBlocked('Candidate validation changed after acceptance')
                journal = {'format': JOURNAL_FORMAT, 'intent_id': intent['id'], 'maintenance_id': maintenance_id}
                seal_record(journal_path, key, journal, guard_factory)
            with copy_lock(directory), copy_lock(Path(intent['role_directory'])):
                check_identity(intent, admin, probe)
                return finish_switch(intent, journal, output, key, admin, rollback=rollback,
                                     guard_factory=guard_factory, checkpoint=checkpoint)
