"""Space, detector and backend identities, from a model's identity manifest.

A space ID is the only identity vectors may be compared by. It is built from an allowlist
of what defines a vector: the recognizer's weights, how a face is aligned, how the photo is
decoded, and the vector's size and normalization. The device, runtime versions, download
details, licence record, matching policy and detector are left out, so another computer or
a library update never makes a new space. Whether such a backend really gives the same
vectors is proven separately, by conformance (`face_detect.spaces.conformance`).

The detector ID records where boxes and landmarks came from. The backend records the
machinery. Neither keys a vector.
"""
import hashlib
import json
import platform
import sys

from face_detect.contracts import ConfigurationError

SPACE_FORMAT = 'familyfoto-face-space-v1'
DETECTOR_FORMAT = 'familyfoto-face-detector-v1'

# How each provider turns a stored face geometry into the recognizer's input. A change to
# any of these steps is a new space, so each one is named with a version.
ALIGNMENT = {
    # insightface face_align.norm_crop: five ordered points to a 112x112 ArcFace crop.
    'insightface': {'kind': 'landmarks', 'method': 'arcface-norm-crop-112-v1', 'recognizer': 'recognizer'},
    # facenet-pytorch MTCNN.extract: the box, cropped and resized to 160x160, then standardized.
    'facenet': {'kind': 'box', 'method': 'facenet-mtcnn-extract-160-v1', 'recognizer': 'recognizer'},
}
DETECTOR_ASSETS = {'insightface': ('detector',), 'facenet': ('pnet', 'rnet', 'onet')}
DETECTOR_SETTINGS = ('detection_threshold', 'detector_size')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _asset(manifest, name):
    try:
        value = manifest['assets'][name]['sha256']
    except (KeyError, TypeError):
        raise ConfigurationError(f'Model manifest has no checksum for {name}') from None
    if not isinstance(value, str) or len(value) != 64:
        raise ConfigurationError(f'Invalid checksum for {name}')
    return value


def space_spec(manifest):
    """What defines this model's vectors. Takes FaceService.manifest (the identity manifest)."""
    provider = manifest.get('provider')
    if provider not in ALIGNMENT:
        raise ConfigurationError('No embedding space is defined for this provider')
    alignment = ALIGNMENT[provider]
    for name in ('preprocessing', 'dimensions', 'normalization'):
        if manifest.get(name) is None:
            raise ConfigurationError(f'Model manifest has no {name}')
    embedding = manifest.get('embedding', {})
    if not isinstance(embedding, dict):
        raise ConfigurationError('Invalid embedding settings')
    return {'format': SPACE_FORMAT, 'provider': provider,
            'recognizer_sha256': _asset(manifest, alignment['recognizer']),
            'alignment': {'kind': alignment['kind'], 'method': alignment['method']},
            'preprocessing': manifest['preprocessing'], 'dimensions': manifest['dimensions'],
            'normalization': manifest['normalization'],
            # A later manifest may name settings that change vectors. They join the space as they are.
            'embedding': embedding}


def space_id(manifest):
    return digest(space_spec(manifest))


def detector_spec(manifest):
    provider = manifest.get('provider')
    if provider not in DETECTOR_ASSETS:
        raise ConfigurationError('No detector is defined for this provider')
    settings = manifest.get('settings', {})
    return {'format': DETECTOR_FORMAT, 'provider': provider,
            'assets': {name: _asset(manifest, name) for name in DETECTOR_ASSETS[provider]},
            'settings': {name: settings[name] for name in DETECTOR_SETTINGS if name in settings},
            'preprocessing': manifest.get('preprocessing')}


def detector_id(manifest):
    return digest(detector_spec(manifest))


def backend(manifest):
    """The machinery of this process: recorded with each vector and certificate, never part
    of a space. The platform and CPU type are this computer's, so only call it for the
    manifest of the backend running here."""
    return {'device': manifest.get('device'), 'runtime_versions': manifest.get('runtime_versions'),
            'platform': sys.platform, 'machine': platform.machine()}


def recorded_backend(manifest):
    """What a stored manifest says about the backend that made its vectors. Today's manifests
    don't record the platform or CPU type, so those are left out rather than guessed."""
    return {'device': manifest.get('device'), 'runtime_versions': manifest.get('runtime_versions')}


def backend_id(value):
    return digest(value)


def describe(manifest):
    value = backend(manifest)
    return {'space_id': space_id(manifest), 'space': space_spec(manifest),
            'detector_id': detector_id(manifest), 'backend': value, 'backend_id': backend_id(value)}
