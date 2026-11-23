"""Conformance: a backend proves, on fixed reference faces, that it computes a space's vectors.

A golden set is a folder made once on a reference backend:

    GOLDEN.json     space, reference backend, where the images came from, and each case
    images/NNN.png  the canonical decoded pixels of each photo

Images are kept as decoded pixels, so this proves the model and its alignment, not image
decoding, which has its own rules and checks. Family photos must never be used: `source`
records where the images came from and their licence.

`check` re-embeds every case from its stored geometry and compares each vector with the
reference. A backend conforms only if every one has cosine similarity of at least the
tolerance. The result is a certificate for this space and this backend. It never covers
another backend, and it proves nothing about another space.
"""
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import statistics

from PIL import Image

from .identity import backend, backend_id, space_id, space_spec

GOLDEN_FORMAT = 'familyfoto-face-golden-v1'
CERTIFICATE_FORMAT = 'familyfoto-face-certificate-v1'
GOLDEN_FILE = 'GOLDEN.json'
# A guess until real backends are measured on Windows and a Mac (see #62). Far above any
# matching threshold, and not bit equality.
DEFAULT_TOLERANCE = 0.999
MIN_VECTORS = 20
MAX_GOLDEN_BYTES = 64 * 1024 * 1024
MAX_CASES = 500


class InvalidGoldenSet(ValueError):
    pass


def unit(vector):
    norm = math.sqrt(sum(v * v for v in vector))
    if not norm:
        raise InvalidGoldenSet('Zero reference vector')
    return [v / norm for v in vector]


def cosine(left, right):
    return sum(a * b for a, b in zip(unit(left), unit(right), strict=True))


def file_sha256(path):
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def geometry_of(face, kind):
    if kind == 'landmarks':
        return {'landmarks': face['landmarks']}
    box = face['box']
    return {'box': [box['x_min'], box['y_min'], box['x_max'], box['y_max']]}


def make_golden_set(service, images, directory, source, *, min_score=0.9, faces_per_image=4, now=None):
    """Write a golden set from `images`, a list of canonical RGB PIL images.

    Faces come from this backend's detector. Only clear ones are kept. Each vector is made
    from the stored geometry, the path a conforming backend must reproduce. The report says
    how closely those agree with the vectors a scan gives, a check of the geometry path.
    """
    if not isinstance(source, str) or not source.strip():
        raise ValueError('Record where the images came from and their licence')
    directory = Path(os.path.abspath(directory))
    directory.mkdir()
    (directory / 'images').mkdir()
    kind = space_spec(service.manifest)['alignment']['kind']
    cases, agreement = [], []
    for image in images:
        faces = [face for face in service.analyze(image, True) if face['box']['probability'] >= min_score]
        faces = sorted(faces, key=lambda face: -face['box']['probability'])[:faces_per_image]
        if not faces:
            continue
        geometry = [geometry_of(face, kind) for face in faces]
        vectors = service.embed_geometry(image, geometry)
        agreement += [cosine(face['vector'], vector) for face, vector in zip(faces, vectors, strict=True)]
        name = f'images/{len(cases):03d}.png'
        image.save(directory / name, format='PNG')
        cases.append({'image': name, 'sha256': file_sha256(directory / name), 'size': list(image.size),
                      'geometry': geometry, 'vectors': vectors})
    if not cases:
        raise ValueError('No clear faces found in these images')
    golden = {'format': GOLDEN_FORMAT, 'space_id': space_id(service.manifest), 'space': space_spec(service.manifest),
              'reference': backend(service.manifest), 'source': source.strip(),
              'created_at': (now or datetime.now(timezone.utc)).isoformat(), 'cases': cases}
    (directory / GOLDEN_FILE).write_text(json.dumps(golden, allow_nan=False), encoding='utf-8')
    return {'directory': str(directory), 'space_id': golden['space_id'], 'cases': len(cases),
            'vectors': sum(len(case['vectors']) for case in cases),
            'scan_agreement_min': min(agreement), 'scan_agreement_median': statistics.median(agreement)}


def read_golden_set(directory):
    directory = Path(os.path.abspath(directory))
    path = directory / GOLDEN_FILE
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_GOLDEN_BYTES:
        raise InvalidGoldenSet('Golden set file missing, linked or too large')
    raw = path.read_bytes()
    try:
        golden = json.loads(raw)
    except ValueError:
        raise InvalidGoldenSet('Golden set is not valid JSON') from None
    if (not isinstance(golden, dict) or golden.get('format') != GOLDEN_FORMAT
            or not isinstance(golden.get('space_id'), str) or not isinstance(golden.get('source'), str)
            or not golden['source'].strip()
            or not isinstance(golden.get('cases'), list) or not 0 < len(golden['cases']) <= MAX_CASES):
        raise InvalidGoldenSet('Not a golden set')
    for case in golden['cases']:
        if not isinstance(case, dict) or not isinstance(case.get('image'), str):
            raise InvalidGoldenSet('Invalid golden case')
        image = (directory / case['image']).resolve()
        if not image.is_relative_to(directory.resolve()) or image.parent.name != 'images':
            raise InvalidGoldenSet('Golden image outside its set')
        if (not isinstance(case.get('geometry'), list) or not isinstance(case.get('vectors'), list)
                or len(case['geometry']) != len(case['vectors']) or not case['geometry']):
            raise InvalidGoldenSet('Golden case geometry and vectors differ')
    return golden, hashlib.sha256(raw).hexdigest(), directory


def check(service, directory, *, tolerance=DEFAULT_TOLERANCE, min_vectors=MIN_VECTORS, now=None):
    """Certify this backend for the golden set's space, or say why not. Read-only."""
    golden, golden_sha256, directory = read_golden_set(directory)
    local = space_id(service.manifest)
    value = backend(service.manifest)
    certificate = {'format': CERTIFICATE_FORMAT, 'space_id': golden['space_id'], 'backend': value,
                   'backend_id': backend_id(value), 'golden_sha256': golden_sha256, 'tolerance': tolerance,
                   'checked_at': (now or datetime.now(timezone.utc)).isoformat(), 'passed': False}
    if local != golden['space_id']:
        # Another space: comparing its vectors would mean nothing, so the model isn't run.
        return certificate | {'reason': 'different-space', 'local_space_id': local}
    similarities = []
    for case in golden['cases']:
        path = directory / case['image']
        if path.is_symlink() or file_sha256(path) != case.get('sha256'):
            raise InvalidGoldenSet('A golden image changed')
        with Image.open(path) as opened:
            if opened.format != 'PNG' or opened.mode != 'RGB' or list(opened.size) != case.get('size'):
                raise InvalidGoldenSet('A golden image is not the canonical pixels it records')
            image = opened.copy()
        vectors = service.embed_geometry(image, case['geometry'])
        similarities += [cosine(reference, vector) for reference, vector in zip(case['vectors'], vectors, strict=True)]
    result = {'vectors': len(similarities), 'worst': min(similarities), 'median': statistics.median(similarities)}
    if len(similarities) < min_vectors:
        return certificate | result | {'reason': 'too-few-vectors'}
    if result['worst'] < tolerance:
        return certificate | result | {'reason': 'vectors-differ'}
    return certificate | result | {'passed': True, 'reason': 'conforms'}


def certifies(certificate, service):
    """Whether a saved certificate covers this space on this exact backend."""
    value = backend(service.manifest)
    return (isinstance(certificate, dict) and certificate.get('format') == CERTIFICATE_FORMAT
            and certificate.get('passed') is True and certificate.get('space_id') == space_id(service.manifest)
            and certificate.get('backend_id') == backend_id(value) and certificate.get('backend') == value)
