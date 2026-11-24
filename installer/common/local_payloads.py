"""Private verified inputs passed to existing provisioning helpers."""
import json
import os
from .acquire import file_digest, plain_path


def payload(digest):
    receipt = os.environ.get('FAMILYFOTO_PAYLOAD_RECEIPT')
    if not receipt:
        return None
    doc = json.loads(plain_path(receipt).read_text(encoding='utf-8'))
    if doc.get('schema_version') != 1 or doc.get('stage') not in ('acquired', 'materialized'):
        raise ValueError('Unsupported payload receipt')
    name = doc['payloads'].get(digest.lower())
    if not name:
        raise ValueError('Required payload missing; network fallback is disabled')
    path = plain_path(name)
    if file_digest(path) != digest.lower():
        raise ValueError('Prepared payload was modified')
    return path


# People's provider libraries are not shipped: opencv-python and others bundle native libraries
# whose sources a release cannot publish. Setup installs them from PyPI by hash instead
# (face_detect/cli/setup.py). Every other role stays prepared-payloads-only.
ONLINE_ROLES = ('insightface', 'facenet')


def pip_arguments(role):
    receipt = os.environ.get('FAMILYFOTO_PAYLOAD_RECEIPT')
    if not receipt:
        return []
    doc = json.loads(plain_path(receipt).read_text(encoding='utf-8'))
    if doc.get('schema_version') != 1 or doc.get('stage') != 'materialized':
        raise ValueError('Dependencies require a materialized payload receipt')
    if role in ONLINE_ROLES and role not in doc.get('locks', {}):
        return []  # the caller installs this role from PyPI with its hash-pinned lock
    wheels = plain_path(doc['wheelhouse'])
    lock = plain_path(doc['locks'][role]['path'])
    if file_digest(lock) != doc['locks'][role]['sha256']:
        raise ValueError('Prepared dependency lock was modified')
    return ['--isolated', '--no-index', '--find-links', str(wheels), '--require-hashes',
            '--only-binary=:all:', '--disable-pip-version-check', '-r', str(lock)]
