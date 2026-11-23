"""Offline coordinated catalog sets. Media is deliberately NOT included.

The application and external writers must be stopped. Updated application writers
are excluded by admission; PostgreSQL locks additionally freeze existing tables.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import uuid

from cryptography.fernet import Fernet
import psycopg
from psycopg import sql

from .admission import maintenance, MaintenanceBlocked
from .encrypted import read_chunks, seal_stream, storage_budget, verify_artifact, InvalidArtifact
from .inventory import inspect_snapshot
from .media_copy import sync_directory
from .passwords import BackupKey, publish as publish_password, validate_context_binding
from .postgres import capture_database
from .space import Requirement, SpaceGuard, directory_identity
from .state_files import capture_state, state_inventory
from .identity import ensure_library_identity
from .uploads import reconcile_uploads
from ..database import locks


FORMAT = 'familyfoto-offline-catalog-v2'


def reference(manifest):
    return hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def no_other_clients(db):
    count = db.execute("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                       "AND pid<>pg_backend_pid() AND backend_type='client backend'").fetchone()[0]
    if count:
        raise MaintenanceBlocked('Stop all database clients before offline catalog capture')


@contextmanager
def freeze_database(endpoint):
    options = endpoint.connection()
    options['options'] = '-c default_transaction_read_only=off'
    with psycopg.connect(**options) as db:
        db.execute("SET LOCAL lock_timeout='3s'")
        db.execute("SET LOCAL statement_timeout='60s'")
        no_other_clients(db)
        guards = {locks.SCHEMA_MIGRATION, *locks.MEDIA_WORKERS, *locks.PEOPLE_WORKERS,
                  locks.PEOPLE_CORRECTION, locks.ALBUM_COLLECTION_HIERARCHY}
        for key in sorted(guards):
            if not db.execute('SELECT pg_try_advisory_xact_lock(%s)', (key,)).fetchone()[0]:
                raise MaintenanceBlocked('A database worker still owns a maintenance lock')
        tables = list(inspect_snapshot(db))
        if tables:
            names = sql.SQL(', ').join(sql.SQL('{}.{}').format(sql.Identifier(row['schema']),
                                                            sql.Identifier(row['name'])) for row in tables)
            db.execute(sql.SQL('LOCK TABLE {} IN SHARE MODE').format(names))
        yield db
        no_other_clients(db)
        if list(inspect_snapshot(db)) != tables:
            raise MaintenanceBlocked('Database inventory changed during capture')


def journal(directory, identity, phase):
    record = {'id': identity, 'phase': phase, 'updated_at': datetime.now(timezone.utc).isoformat()}
    temporary = directory / 'journal.part'
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(record, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, directory / 'journal.json')
    sync_directory(directory)


def capture_components(endpoint, pg_dump, config_root, data_root, directory, key, identity, *,
                       database_budget, guard_factory=SpaceGuard, progress=None, resume_state=False,
                       own_context=False):
    """Internal coordinator step: caller holds maintenance and database freeze.

    `own_context` is for a standalone catalog: its password context is published at its own root
    before anything is sealed. A catalog inside a library uses the library's context."""
    if directory.exists() and resume_state:
        directory_identity(directory)
        if {entry.name for entry in directory.iterdir()} != {'state'}:
            raise InvalidArtifact('Unexpected entries in resumed catalog')
    else:
        directory.mkdir(mode=0o700)
    if own_context:
        publish_password(directory, key)
    journal(directory, identity, 'database')
    def report(phase):
        return (lambda count: progress({'phase': phase, 'bytes': count})) if progress else None
    database = capture_database(endpoint, pg_dump, directory / 'database', key,
                                estimated_bytes=database_budget, guard_factory=guard_factory,
                                progress=report('database'))
    journal(directory, identity, 'state')
    state = capture_state(config_root, data_root, directory / 'state', key,
                          guard_factory=guard_factory, progress=report('state'), resume=resume_state)
    journal(directory, identity, 'verifying')
    if verify_artifact(directory / 'database', key) != database or verify_artifact(directory / 'state', key) != state:
        raise InvalidArtifact('Recovery component changed before publication')
    return {'database': reference(database), 'state': reference(state)}


def publish_catalog(directory, key, identity, components, *, guard_factory=SpaceGuard, library_id=None):
    """Publish only after the coordinator's frozen-database validation succeeded."""
    if not isinstance(key, BackupKey):
        raise TypeError('A catalog is sealed with a key unlocked from a backup password')
    payload = {'format': FORMAT, 'id': identity, 'created_at': datetime.now(timezone.utc).isoformat(),
               'components': components, 'media_included': False, 'password': key.binding(),
               'capture_mode': 'offline', 'activation_allowed': False}
    if library_id is not None:
        payload['library_id'] = library_id
    raw = json.dumps(payload, sort_keys=True).encode()
    seal_stream(io.BytesIO(raw), directory / 'catalog', key, estimated_bytes=len(raw), guard_factory=guard_factory,
                metadata={'kind': FORMAT})
    journal(directory, identity, 'complete')
    return payload


@contextmanager
def catalog_capture_scope(endpoint, pg_dump, config_root, data_root, directory, key, *,
                    database_budget, offline_confirmed=False, guard_factory=SpaceGuard, progress=None):
    """No live-app config loading, service stopping, implicit resume or overwrite."""
    if offline_confirmed is not True:
        raise MaintenanceBlocked('Explicit confirmation that application and external writers are stopped is required')
    if not isinstance(key, BackupKey):
        raise TypeError('A catalog backup needs a key made from a backup password')
    Fernet(key)
    config_root, data_root, directory = (Path(os.path.abspath(path)) for path in (config_root, data_root, directory))
    directory_identity(directory.parent)
    if directory.exists():
        raise FileExistsError('A new recovery-set directory is required')
    for root in (config_root, data_root):
        if directory.is_relative_to(root) or root.is_relative_to(directory):
            raise ValueError('Recovery output must be separate from state roots')
    records = state_inventory({'config': config_root, 'data': data_root})
    # Include a new identity record in this early estimate; recheck under admission.
    state_budget = sum(row['size'] for row in records) + (len(records) + 1) * 16384 + 10240
    budget = storage_budget(database_budget) + storage_budget(state_budget) + storage_budget(65536)
    guard_factory([Requirement('destination', directory.parent, budget)]).check()
    identity = uuid.uuid4().hex
    with maintenance(data_root, 'backup') as admission_id:
        with freeze_database(endpoint) as db:
            library_id = ensure_library_identity(data_root, admission_id)
            reconcile_uploads(db, data_root)
            records = state_inventory({'config': config_root, 'data': data_root})
            state_budget = sum(row['size'] for row in records) + len(records) * 16384 + 10240
            budget = storage_budget(database_budget) + storage_budget(state_budget) + storage_budget(65536)
            guard_factory([Requirement('destination', directory.parent, budget)]).check()
            components = capture_components(endpoint, pg_dump, config_root, data_root, directory, key, identity,
                                             database_budget=database_budget, guard_factory=guard_factory,
                                             progress=progress, own_context=True)
            reconcile_uploads(db, data_root)
        # Lock-release validation above must succeed before publishing completion.
        payload = publish_catalog(directory, key, identity, components, guard_factory=guard_factory, library_id=library_id)
        yield payload


def capture_catalog(*args, **kwargs):
    """Capture a verified set, releasing maintenance only after successful return."""
    with catalog_capture_scope(*args, **kwargs) as payload:
        return payload


def verify_catalog(directory, key):
    directory = Path(directory)
    manifest = verify_artifact(directory / 'catalog', key)
    if manifest['metadata'].get('kind') != FORMAT or manifest['size'] > 65536:
        raise InvalidArtifact('Not a supported catalog recovery set')
    try:
        payload = json.loads(b''.join(read_chunks(directory / 'catalog', key, manifest)))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise InvalidArtifact('Invalid catalog index') from exc
    if (not isinstance(payload, dict) or payload.get('format') != FORMAT
            or payload.get('media_included') is not False or payload.get('activation_allowed') is not False
            or not isinstance(payload.get('components'), dict) or set(payload['components']) != {'database', 'state'}):
        raise InvalidArtifact('Invalid catalog component declaration')
    validate_context_binding(key, payload.get('password'))
    for name, expected_kind in (('database', 'postgres-custom-dump'), ('state', 'familyfoto-state-tar-v1')):
        component = verify_artifact(directory / name, key)
        if component['metadata'].get('kind') != expected_kind or reference(component) != payload['components'][name]:
            raise InvalidArtifact('Catalog component is missing, changed or from another recovery set')
    return payload
