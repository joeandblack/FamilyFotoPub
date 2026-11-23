"""Explicit source retirement and authenticated successor handoff evidence."""
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import re
import uuid

from .identity import read_record, read_library_identity
from .media_copy import sync_directory, copy_lock
from .encrypted import seal_stream, verify_artifact, read_chunks, InvalidArtifact
from .space import SpaceGuard, directory_identity


FORMAT = 'familyfoto-source-retirement-v1'


def successor_id(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{32}', value):
        raise ValueError('An explicit 32-character successor identity is required')
    return value


def mark_source_retired(data_root, maintenance_id, payload):
    """Only the final capture coordinator may call while holding admission."""
    directory = Path(data_root) / '.recovery-admission'
    lease = read_record(directory / 'maintenance.json')
    if lease.get('id') != maintenance_id or lease.get('state') != 'in-progress':
        raise ValueError('Source retirement requires the matching maintenance lease')
    target = successor_id(payload.get('migration_target'))
    if read_library_identity(data_root) != payload.get('library_id'):
        raise ValueError('Migration belongs to another library')
    record = {'format': FORMAT, 'capture_id': payload['id'], 'library_id': payload['library_id'],
              'successor_id': target, 'source_retired': True,
              'retired_at': datetime.now(timezone.utc).isoformat()}
    descriptor = os.open(directory / 'retired.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        json.dump(record, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    sync_directory(directory)
    return record


def publish_retirement(directory, key, record, *, guard_factory=SpaceGuard):
    raw = json.dumps(record, sort_keys=True).encode()
    seal_stream(io.BytesIO(raw), Path(directory) / 'retirement', key, estimated_bytes=len(raw),
                metadata={'kind': FORMAT}, guard_factory=guard_factory)


def verify_retirement(directory, key, payload):
    """The caller must first authenticate the library index passed as payload."""
    artifact = Path(directory) / 'retirement'
    manifest = verify_artifact(artifact, key)
    if manifest['metadata'].get('kind') != FORMAT or manifest['size'] > 4096:
        raise InvalidArtifact('Invalid source retirement receipt')
    record = json.loads(b''.join(read_chunks(artifact, key, manifest)))
    validate_record(record, payload)
    return record


def validate_record(record, payload):
    target = successor_id(payload.get('migration_target'))
    if (not isinstance(record, dict) or record.get('format') != FORMAT
            or record.get('source_retired') is not True
            or record.get('successor_id') != target or record.get('capture_id') != payload['id']
            or record.get('library_id') != payload['library_id']):
        raise InvalidArtifact('Source retirement receipt does not match this successor')


def recover_retirement(data_root, directory, key, *, guard_factory=SpaceGuard):
    """Repair only receipt publication; never unretire, activate or clear admission."""
    from .library import verify_library
    from .admission import collect_idle

    directory = Path(directory).absolute()
    admission = Path(data_root).absolute() / '.recovery-admission'
    directory_identity(admission)
    directory_identity(directory)
    # Capture holds this same latch until receipt publication has finished.
    # Recovery cannot race a still-running source retirement operation.
    with copy_lock(admission):
        collect_idle(admission)
        payload = verify_library(directory, key)
        record = read_record(admission / 'retired.json')
        validate_record(record, payload)
        if read_library_identity(data_root) != payload['library_id']:
            raise InvalidArtifact('Retired installation belongs to another library')
        return ensure_receipt(directory, directory, key, record, payload, guard_factory=guard_factory)


def ensure_receipt(directory, archive, key, record, payload, *, guard_factory=SpaceGuard):
    """Keep a verified receipt, or republish after archiving incomplete output.

    The caller holds the admission latch and has authenticated payload and record.
    `archive` is on the same volume, outside anything later published as a unit.
    """
    directory, archive = Path(directory), Path(archive)
    receipt = directory / 'retirement'
    if receipt.exists() or receipt.is_symlink():
        directory_identity(receipt)
        if (receipt / 'complete.fernet').exists():
            if verify_retirement(directory, key, payload) != record:
                raise InvalidArtifact('Published receipt differs from source retirement')
            return record
        # Preserve incomplete ciphertext for diagnosis, never overwrite a
        # completed (even damaged) receipt or delete interrupted output.
        receipt.rename(archive / ('retirement-incomplete-' + uuid.uuid4().hex))
        sync_directory(archive)
        sync_directory(directory)
    publish_retirement(directory, key, record, guard_factory=guard_factory)
    if verify_retirement(directory, key, payload) != record:
        raise InvalidArtifact('Retirement receipt verification failed')
    return record
