"""Conformance from the library itself: a sample of stored faces is the reference.

After a move (Windows to a Mac, say), the stored vectors were made by the old backend.
This backend may adopt them only if it is the same space and it gives the same vectors for
the same faces. A sample of good faces, one per photo, is read in place, each face is found
again at its stored box and embedded, and the result is compared with the stored vector.
Nothing is copied or written: the photos stay where they are, and only similarity numbers
leave this module.

Faces are found again at their box rather than embedded from stored landmarks, because
most stored landmarks came from an older detector than the one that made the vectors.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import statistics

from face_detect.contracts import ProviderUnavailable

from .conformance import CERTIFICATE_FORMAT, cosine
from .identity import backend, backend_id, recorded_backend, space_id

SAMPLE_SIZE = 24
MIN_SAMPLES = 12
# From the check in #60, tried on the Windows library: the same vectors gave 1.000, another
# person's face at most 0.66, and the old model's vectors a median of -0.02.
MIN_SIMILARITY = 0.95
MEDIAN_SIMILARITY = 0.99
MAX_UNMATCHED = 0.1
# The clearest faces are sampled first. How high a detector's scores go depends on the
# detector: a library scanned only with the current one may have no face at 0.95 at all
# (776 faces on a test library topped out at 0.935). So when a library has too few of
# those to be checked at all (fewer than MIN_SAMPLES), the sample is filled with its
# next-best faces, never below LOWEST_SCORE. That is still above the detector's own threshold
# of 0.8, where a box is least certain. A library with enough clear faces to be checked is
# never given weaker ones: they are the likeliest not to be found again, and could turn a
# pass into a failure. A weaker sample can't pass a wrong model either way.
MIN_SCORE = 0.95
LOWEST_SCORE = 0.85
MIN_SIDE = 0.05


def good_face(face, floor=None):
    """Clear enough that finding it again at its box is unambiguous."""
    box, score = face.get('box') or {}, face.get('score')
    return (type(score) in (int, float) and score >= (MIN_SCORE if floor is None else floor)
            and all(type(box.get(k)) in (int, float) for k in ('x', 'y', 'width', 'height'))
            and box['width'] >= MIN_SIDE and box['height'] >= MIN_SIDE)


def choose_samples(faces, size=SAMPLE_SIZE):
    """One good face per photo, in a fixed order that doesn't depend on the catalog's order.

    Faces at MIN_SCORE or above come first, in that fixed order. A library with at least
    MIN_SAMPLES of them gets exactly the sample it always got, so its result can't change.
    Only a library with fewer, which could not be checked at all, also gets other photos'
    best faces, highest score first, down to LOWEST_SCORE.
    """
    def key(face):
        box = face['box']
        return hashlib.sha256(f"{face['content_hash']}:{box['x']}:{box['y']}:{box['width']}:{box['height']}".encode()).hexdigest()
    chosen, seen = [], set()

    def take(candidates):
        for face in candidates:
            if len(chosen) == size:
                return
            if face['content_hash'] not in seen:
                seen.add(face['content_hash'])
                chosen.append(face)
    take(sorted((f for f in faces if good_face(f)), key=key))
    if len(chosen) < MIN_SAMPLES:
        take(sorted((f for f in faces if good_face(f, LOWEST_SCORE) and not good_face(f)),
                    key=lambda face: (-face['score'], key(face))))
    return chosen


SAMPLE_FORMAT = 'familyfoto-library-sample-v1'
DIGEST = re.compile('[0-9a-f]{64}')
PHOTO_NAME = re.compile('[0-9a-f]{64}\\.[a-z0-9]{1,5}')


def write_sample(directory, manifest, faces, paths, file_sha256, *, now=None):
    """Carry a library sample to another computer: the stored model manifest, the sampled
    faces with their vectors, and a copy of each photo named by its content checksum.
    `file_sha256` is each photo file's own checksum, which differs from the content checksum
    for a photo stored repaired. Nothing else from the catalog, and no names. The folder must
    be new; `sample.json` is written last, so a folder without it is incomplete."""
    directory = Path(directory)
    directory.mkdir()
    (directory / 'photos').mkdir()
    entries = []
    for face in faces:
        digest, expected = face['content_hash'], file_sha256[face['content_hash']]
        source = Path(paths[digest])
        name = digest + source.suffix.lower()
        if not DIGEST.fullmatch(digest) or not DIGEST.fullmatch(expected) or not PHOTO_NAME.fullmatch(name):
            raise ValueError('Unexpected photo checksum or file type')
        target = directory / 'photos' / name
        if not target.exists():
            data = source.read_bytes()
            if hashlib.sha256(data).hexdigest() != expected:
                raise ValueError('A sampled photo no longer matches its checksum')
            with open(target, 'xb') as out:
                out.write(data)
        entries.append({'content_hash': digest, 'photo': name, 'photo_sha256': expected, 'box': face['box'],
                        'score': face['score'], 'vector': [float(v) for v in face['vector']]})
    sample = {'format': SAMPLE_FORMAT, 'made_at': (now or datetime.now(timezone.utc)).isoformat(),
              'space_id': space_id(manifest), 'manifest': manifest, 'faces': entries}
    with open(directory / 'sample.json', 'x', encoding='utf-8') as out:
        json.dump(sample, out)
    return {'written': str(directory), 'faces': len(entries), 'photos': len({e['photo'] for e in entries}),
            'space_id': sample['space_id']}


def read_sample(directory):
    """The stored manifest, faces and photo paths of a sample `write_sample` made."""
    directory = Path(directory)
    sample = json.loads((directory / 'sample.json').read_text(encoding='utf-8'))
    if sample.get('format') != SAMPLE_FORMAT or not isinstance(sample.get('faces'), list):
        raise ValueError('Not a FamilyFoto library sample')
    if space_id(sample['manifest']) != sample.get('space_id'):
        raise ValueError('Library sample manifest does not match its space')
    faces, paths, file_sha256 = [], {}, {}
    for entry in sample['faces']:
        digest, name, expected = entry.get('content_hash'), entry.get('photo'), entry.get('photo_sha256')
        if (not all(isinstance(v, str) for v in (digest, name, expected)) or not DIGEST.fullmatch(digest)
                or not DIGEST.fullmatch(expected) or not PHOTO_NAME.fullmatch(name) or not name.startswith(digest)):
            raise ValueError('Library sample has an unexpected photo')
        faces.append({key: entry[key] for key in ('content_hash', 'box', 'score', 'vector')})
        paths[digest], file_sha256[digest] = directory / 'photos' / name, expected
    return sample['manifest'], faces, paths, file_sha256


def verified(load_image, paths, file_sha256):
    """Open a sampled photo only if it is still the file the sample was made from."""
    def load(digest):
        if hashlib.sha256(paths[digest].read_bytes()).hexdigest() != file_sha256[digest]:
            raise ValueError('Photo does not match its checksum')
        return load_image(paths[digest])
    return load


def target_box(box, image):
    x1, y1 = box['x'] * image.width, box['y'] * image.height
    x2, y2 = min(image.width, x1 + box['width'] * image.width), min(image.height, y1 + box['height'] * image.height)
    return [max(0.0, x1), max(0.0, y1), x2, y2]


def check(service, stored_manifest, faces, load_image, *, size=SAMPLE_SIZE, now=None):
    """Certify this backend for the stored vectors' space, or say why not. Read-only.

    `faces` are stored faces: content_hash, normalized box (x, y, width, height), score and
    vector. `load_image(content_hash)` returns the photo decoded the canonical way.
    """
    value = backend(service.manifest)
    stored = space_id(stored_manifest)
    certificate = {'format': CERTIFICATE_FORMAT, 'space_id': stored, 'backend': value,
                   'backend_id': backend_id(value), 'reference': 'library-sample',
                   'stored_backend': recorded_backend(stored_manifest),
                   'checked_at': (now or datetime.now(timezone.utc)).isoformat(), 'passed': False,
                   'thresholds': {'min': MIN_SIMILARITY, 'median': MEDIAN_SIMILARITY, 'max_unmatched': MAX_UNMATCHED}}
    local = space_id(service.manifest)
    if local != stored:
        # Another space: its vectors can't be compared, so the model isn't run.
        return certificate | {'reason': 'different-space', 'local_space_id': local}
    samples = choose_samples(faces, size)
    certificate['sampled'] = len(samples)
    if samples:
        # How clear the weakest sampled face was, so a sample filled below MIN_SCORE shows.
        certificate['lowest_sample_score'] = min(face['score'] for face in samples)
    if len(samples) < MIN_SAMPLES:
        return certificate | {'reason': 'too-few-faces'}
    similarities, unmatched, unreadable = [], 0, 0
    for face in samples:
        try:
            image = load_image(face['content_hash'])
        except (OSError, ValueError):
            # Counted apart from faces not found, so an unmounted media disk says so.
            unreadable += 1
            continue
        try:
            found = service.embed_result(image, target_box(face['box'], image))
        except (ProviderUnavailable, ValueError):
            unmatched += 1
            continue
        similarities.append(cosine(face['vector'], found['vector']))
    certificate.update(compared=len(similarities), unmatched=unmatched, unreadable=unreadable)
    if unmatched + unreadable > MAX_UNMATCHED * len(samples) or len(similarities) < MIN_SAMPLES:
        return certificate | {'reason': 'photos-unreadable' if unreadable > unmatched else 'faces-not-found'}
    certificate.update(worst=min(similarities), median=statistics.median(similarities))
    if certificate['worst'] < MIN_SIMILARITY or certificate['median'] < MEDIAN_SIMILARITY:
        return certificate | {'reason': 'vectors-differ'}
    return certificate | {'passed': True, 'reason': 'conforms'}
