"""Durable logical library identity, created only during admitted maintenance."""
import json
import os
from pathlib import Path
import re
import uuid

from .media_copy import sync_directory
from .space import directory_identity


def read_record(path):
    directory_identity(path.parent)
    if path.is_symlink() or path.is_junction() or not path.is_file() or path.stat().st_size > 4096:
        raise ValueError('Invalid library identity record')
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('Library identity record must be an object')
    return value


def read_library_identity(root):
    value = read_record(Path(root) / '.recovery-admission/library.json')
    if value.get('format') != 'familyfoto-library-identity-v1' or not re.fullmatch(
            '[0-9a-f]{32}', str(value.get('library_id', ''))):
        raise ValueError('Invalid library identity')
    return value['library_id']


def ensure_library_identity(root, maintenance_id):
    """Caller holds the matching maintenance lease throughout this operation."""
    directory = Path(root) / '.recovery-admission'
    lease = read_record(directory / 'maintenance.json')
    if not maintenance_id or lease.get('id') != maintenance_id or lease.get('state') != 'in-progress':
        raise ValueError('Library identity requires the matching maintenance lease')
    path = directory / 'library.json'
    if path.exists():
        return read_library_identity(root)
    identity = uuid.uuid4().hex
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        json.dump({'format': 'familyfoto-library-identity-v1', 'library_id': identity}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    sync_directory(directory)
    return identity
