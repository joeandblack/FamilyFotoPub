"""Private verified inputs passed to existing provisioning helpers."""
import json
import os
from .acquire import file_digest, plain_path
from .manifest import INDEX_LOCK_ROLES, PYPI


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


def pip_arguments(role):
    receipt = os.environ.get('FAMILYFOTO_PAYLOAD_RECEIPT')
    if not receipt:
        return []
    doc = json.loads(plain_path(receipt).read_text(encoding='utf-8'))
    if doc.get('schema_version') != 1 or doc.get('stage') != 'materialized':
        raise ValueError('Dependencies require a materialized payload receipt')
    wheels = plain_path(doc['wheelhouse'])
    lock = plain_path(doc['locks'][role]['path'])
    if file_digest(lock) != doc['locks'][role]['sha256']:
        raise ValueError('Prepared dependency lock was modified')
    index = doc['locks'][role].get('index')
    if index:
        # Not in the release (People's provider wheels): from PyPI, every file pinned by the lock.
        if role not in INDEX_LOCK_ROLES or index != PYPI:
            raise ValueError('Only People provider locks may install from PyPI')
        return ['--isolated', '--index-url', PYPI, '--require-hashes', '--only-binary=:all:',
                '--disable-pip-version-check', '-r', str(lock)]
    return ['--isolated', '--no-index', '--find-links', str(wheels), '--require-hashes',
            '--only-binary=:all:', '--disable-pip-version-check', '-r', str(lock)]
