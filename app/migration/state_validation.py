"""Compare recovered file state with authenticated source state and allowed sanitation."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from ..backup.encrypted import InvalidArtifact, read_bounded, verify_artifact
from ..backup.policy import PAUSE_MARKERS, PROFILE, WORKER_STATUSES
from ..backup.space import directory_identity
from ..backup.state_files import MAX_REQUEST, walk_tar


def validate_settings(source, saved, target, capture_id, media, inventory_sha256):
    expected = dict(source)
    for name in ('postgres_admin_password', 'preview', 'live_existing_catalog', 'live_native_catalog',
                 'limited_metadata_edit', 'read_only_preview'):
        expected.pop(name, None)
    for name, pattern in (('auth_secret', r'[A-Za-z0-9_-]{64}'), ('session_epoch', r'[0-9a-f]{32}')):
        value = saved.get(name)
        if not isinstance(value, str) or not re.fullmatch(pattern, value) or value == source.get(name):
            raise InvalidArtifact('Recovered session secrets were not independently replaced')
        expected[name] = value
    expected['database'] = {'host': target.host, 'port': target.port, 'dbname': target.dbname,
                            'user': 'familyfoto_recovery_disabled',
                            'options': '-c default_transaction_read_only=on'}
    expected['source'] = expected['destination'] = {}
    expected['recovery'] = {'state': 'validation-only', 'capture_id': capture_id,
                            'activation_required': True, 'media_verified': True,
                            'media_root': str(media), 'media_inventory_sha256': inventory_sha256}
    if saved != expected:
        raise InvalidArtifact('Recovered settings contain unexpected changes')


def validate_file_state(artifact, key, files, target, capture_id, media, inventory_sha256, *,
                        maintenance_id=None, original_configuration=None):
    """Read only, bounded-memory comparison; do not trust a locally edited manifest."""
    artifact, files = Path(artifact), Path(files)
    manifest = verify_artifact(artifact, key)
    expected_names = {'RESTORE-STATE-ONLY.json', 'data/face-service-settings.json',
                      *('data/' + name for name in PAUSE_MARKERS)}
    if maintenance_id is not None:
        from .activation_database import require_activation_gate
        require_activation_gate(files / 'data', maintenance_id)
        expected_names.update({'data/.recovery-admission/maintenance.json', 'data/.recovery-admission/copy.lock'})
    controls = set()

    def compare(name, size, source):
        name = str(name)
        expected_names.add(name)
        path = files / name
        directory_identity(path.parent)
        job = re.fullmatch(rf'data/{PROFILE}/jobs/[^/]+/(?:raw|import|checkpoint)\.json', name)
        if job:
            controls.add(str(Path(name).parent.as_posix()) + '/control.json')
        if name == 'config/local.json' or name == 'data/face-service-settings.json' or job or name in {
                'data/' + value for value in WORKER_STATUSES}:
            if size > MAX_REQUEST:
                raise InvalidArtifact('Recovered JSON state exceeds verification bound')
            original = json.loads(source.read(size))
            saved = json.loads(read_bounded(path, MAX_REQUEST))
            if name == 'config/local.json':
                if original_configuration is not None:
                    # Activation separately checks that the on-disk value is
                    # exactly its sealed old/new configuration, not arbitrary JSON.
                    saved = json.loads(original_configuration)
                validate_settings(original, saved, target, capture_id, media, inventory_sha256)
                return
            if name == 'data/face-service-settings.json':
                original = {'start_with_app': False}
            elif job:
                if isinstance(original, dict) and original.get('state') in {
                        'queued', 'running', 'waiting-service', 'waiting-for-foreground'}:
                    original['state'] = 'paused'
            else:
                original['recovery_requires_review'] = True
                if original.get('state') not in {'paused', 'paused-error', 'complete', 'completed', 'done', 'cancelled', 'error'}:
                    original['recovery_previous_state'] = original.get('state')
                    original['state'] = 'paused'
            if saved != original:
                raise InvalidArtifact('Recovered control state contains unexpected changes: ' + name)
        else:
            # read_bounded rejects links, but large original state is streamed.
            info = path.lstat()
            if path.is_symlink() or path.is_junction() or not path.is_file() or info.st_nlink != 1 or info.st_size != size:
                raise InvalidArtifact('Recovered state file identity differs: ' + name)
            with path.open('rb') as saved:
                before = os.fstat(saved.fileno())
                if (before.st_ino, before.st_dev, before.st_size) != (info.st_ino, info.st_dev, info.st_size):
                    raise InvalidArtifact('Recovered state file changed before reading: ' + name)
                if hashlib.file_digest(saved, 'sha256').digest() != hashlib.file_digest(source, 'sha256').digest():
                    raise InvalidArtifact('Recovered state content differs: ' + name)
                after = os.fstat(saved.fileno())
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise InvalidArtifact('Recovered state file changed while reading: ' + name)

    walk_tar(artifact, key, manifest, compare)
    expected_names.update(controls)
    for name in controls:
        if json.loads(read_bounded(files / name, 65536)) != {'action': 'pause'}:
            raise InvalidArtifact('Recovered job control is not paused')
    for name in PAUSE_MARKERS:
        if read_bounded(files / 'data' / name, 1) != b'':
            raise InvalidArtifact('Recovered pause marker changed')
    if json.loads(read_bounded(files / 'data/face-service-settings.json', 65536)) != {'start_with_app': False}:
        raise InvalidArtifact('Recovered face service settings changed')
    report = json.loads(read_bounded(files / 'RESTORE-STATE-ONLY.json', 65536))
    if report != {'kind': 'restored-file-state-only', 'files': manifest['metadata']['file_count'],
                  'activation_allowed': False, 'database_restored': False}:
        raise InvalidArtifact('Recovered file-state report changed')
    actual = set()
    for root, dirs, names in os.walk(files, followlinks=False):
        directory_identity(Path(root))
        for directory in dirs:
            directory_identity(Path(root) / directory)
        for name in names:
            path = Path(root) / name
            info = path.lstat()
            if path.is_symlink() or path.is_junction() or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise InvalidArtifact('Linked or special file in recovered state')
            relative = path.relative_to(files).as_posix()
            if (maintenance_id is not None and original_configuration is not None
                    and re.fullmatch(r'config/local\.json\.[0-9a-f]{32}\.part', relative)):
                # An interrupted atomic configuration publication is never an
                # input. Preserve only bounded, regular attempt files for review.
                read_bounded(path, MAX_REQUEST)
                expected_names.add(relative)
            actual.add(relative)
    if actual != expected_names:
        raise InvalidArtifact('Recovered file-state inventory differs')
    return {'files': len(actual), 'state': 'verified-sanitized-state'}
