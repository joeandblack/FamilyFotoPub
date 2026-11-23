"""Consent, atomic state and the existing @@ progress-line transport."""
import json
import os
import tempfile
import time
from pathlib import Path
from app.migration.progress_lines import ProgressLines
from .acquire import plain_path
from .manifest import sha256


def atomic_json(path, value):
    path = plain_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.state-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class Events:
    def __init__(self, attempt, stream=None):
        self.attempt, self.sequence = attempt, 0
        self.start = time.monotonic()
        self.writer = ProgressLines(stream, interval=0)

    def __call__(self, **event):
        self.sequence += 1
        self.writer(dict(event, schema_version=1, attempt_id=self.attempt,
                         sequence=self.sequence, elapsed_ms=int((time.monotonic()-self.start)*1000)))


def acceptance(manifest, features, agreement, accepted_digest):
    digest = sha256(agreement)
    if digest != manifest.document['agreement_sha256'] or accepted_digest != digest:
        raise ValueError('Explicit acceptance of this agreement digest is required')
    return {'manifest_digest': manifest.digest, 'agreement_digest': digest,
            'release_id': manifest.document['release_id'], 'features': sorted(set(features))}


class Attempt:
    """Acquisition receipt only; platform setup journals retain provisioning ownership."""
    def __init__(self, path, binding):
        self.path = plain_path(path)
        self.binding = binding
        if self.path.exists():
            self.value = json.loads(self.path.read_text(encoding='utf-8'))
            if self.value.get('binding') != binding or self.value.get('schema_version') != 1:
                raise ValueError('Attempt changed; use a new attempt directory')
        else:
            self.value = {'schema_version': 1, 'binding': binding, 'completed': []}

    def completed(self, component):
        if component['id'] not in self.value['completed']:
            self.value['completed'].append(component['id'])
        atomic_json(self.path, self.value)
