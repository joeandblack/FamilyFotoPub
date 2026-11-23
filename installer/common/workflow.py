"""Shared preparation: admission, consent, acquisition and adapter handoff."""
import shutil
from contextlib import contextmanager
from .acquire import Acquisition, plain_path
from .manifest import Manifest
from .state import Attempt, Events, acceptance, atomic_json


@contextmanager
def exclusive(path):
    path = plain_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        stream.seek(0)
        if stream.read(1) == b'':
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        if __import__('os').name == 'nt':
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            stream.seek(0)
            if __import__('os').name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def prepare(manifest, *, platform, features, agreement, accepted_digest,
            directory, mode, bundle=None, stream=None, opener=None, cancelled=None,
            install_root=None, os_version=None):
    components = manifest.plan(features, platform, os_version)
    consent = acceptance(manifest, features, agreement, accepted_digest)
    directory = plain_path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with exclusive(directory / 'attempt.lock'):
        binding = dict(consent, platform=platform, mode=mode)
        if install_root is not None:
            binding['install_root'] = str(plain_path(install_root))
        attempt = Attempt(directory / 'attempt.json', binding)
        events = Events(manifest.digest[:16], stream)
        def emit(**event):
            if cancelled and cancelled():
                raise InterruptedError('Installation preparation cancelled; verified downloads retained')
            events(**event)
        cache = directory / 'cache'
        kwargs = {'opener': opener} if opener else {}
        acquire = Acquisition(cache, mode=mode, bundle=bundle, emit=emit, **kwargs)
        # Reserve the full remaining payload footprint before downloading, not after hours.
        missing = sum(c['byte_size'] for c in components if not (cache / c['sha256']).exists())
        if shutil.disk_usage(directory).free < missing:
            raise OSError('Insufficient disk space for selected component downloads')
        atomic_json(directory / 'acceptance.json', consent)
        paths = {}
        for component in components:
            emit(stage='acquire', component_id=component['id'], state='running')
            paths[component['id']] = str(acquire.ensure(component))
            attempt.completed(component)
        receipt = {'schema_version': 1, 'stage': 'acquired', 'manifest_digest': manifest.digest,
                   'features': consent['features'], 'paths': paths,
                   'payloads': {c['sha256']: paths[c['id']] for c in components}}
        atomic_json(directory / 'prepared.json', receipt)
        events(stage='prepare', state='completed')
        return receipt


def read_manifest(path, expected_digest):
    return Manifest(plain_path(path).read_bytes(), expected_digest)
