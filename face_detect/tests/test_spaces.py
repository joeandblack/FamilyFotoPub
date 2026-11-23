"""Space identity and conformance, with a deterministic fake backend: no model needed."""
import copy
import hashlib
import json
import math

from PIL import Image
import pytest

from face_detect.cli import conformance as cli
from face_detect.contracts import ProviderUnavailable
from face_detect.service import FaceService
from face_detect.spaces import conformance, identity

LANDMARKS = ((.35, .4), (.65, .4), (.5, .55), (.4, .7), (.6, .7))


def vector_for(points, image, noise=0.0):
    """A stand-in recognizer: depends only on the geometry and the pixels under it."""
    pixels = [sum(image.getpixel((min(int(x), image.width - 1), min(int(y), image.height - 1)))) for x, y in points]
    seed = sum(x * 0.013 + y * 0.007 for x, y in points) + sum(pixels) * 0.001
    return [math.sin(seed * (i + 1)) + noise * math.cos(i * 1.7) for i in range(512)]


class Backend:
    def __init__(self, noise=0.0, kind='landmarks'):
        self.noise, self.kind, self.embedded = noise, kind, 0

    def analyze(self, image, include_embeddings=True):
        w, h = image.size
        points = [[w * x, h * y] for x, y in LANDMARKS]
        face = {'box': dict(x_min=w * .25, y_min=h * .25, x_max=w * .75, y_max=h * .85, probability=.99),
                'landmarks': points}
        if include_embeddings:
            face['vector'] = vector_for(points, image, self.noise)
        return [face]

    def embed_geometry(self, image, geometry):
        self.embedded += len(geometry)
        out = []
        for item in geometry:
            if 'landmarks' in item:
                out.append(vector_for(item['landmarks'], image, self.noise))
            else:
                x1, y1, x2, y2 = item['box']
                out.append(vector_for([[x1, y1], [x2, y1], [(x1 + x2) / 2, (y1 + y2) / 2], [x1, y2], [x2, y2]], image, self.noise))
        return out

    def close(self):
        pass


def model(tmp_path, provider='insightface', **changes):
    names = ('detector', 'recognizer') if provider == 'insightface' else ('recognizer', 'pnet', 'rnet', 'onet')
    assets = {}
    for name in names:
        path = tmp_path / f'{provider}-{name}'
        path.write_bytes(changes.pop(name, name).encode())
        assets[name] = path
    manifest = {'schema': 'familyfoto-native-model-v1', 'provider': provider, 'dimensions': 512, 'normalization': 'l2',
                'license': {'approved': True, 'source': 'test'}, 'matching': {'mode': 'review-only'},
                'settings': {'detection_threshold': .8, 'detector_size': 640},
                'assets': {name: {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
                           for name, path in assets.items()}}
    for key, value in changes.items():
        manifest[key] = value
    return manifest, assets


def service(tmp_path, *, device='cpu', versions=None, backend=None, provider='insightface', **changes):
    manifest, assets = model(tmp_path, provider, **changes)
    backend = backend or Backend(kind='landmarks' if provider == 'insightface' else 'box')
    if versions is not None:
        backend.versions = lambda: versions
    return FaceService({'provider': provider, 'device': device}, manifest, assets, backend)


def photos(count=6):
    images = []
    for n in range(count):
        image = Image.new('RGB', (200 + n * 10, 240))
        for x in range(0, image.width, 7):
            for y in range(0, image.height, 5):
                image.putpixel((x, y), ((x * 3 + n * 40) % 256, (y * 5) % 256, (x + y + n) % 256))
        images.append(image)
    return images


# -- space identity -------------------------------------------------------------------------

def test_another_computer_is_the_same_space_but_another_backend(tmp_path):
    windows = service(tmp_path, device='cpu', versions={'insightface': '2.1', 'onnxruntime': '1.31.1'})
    mac = service(tmp_path, device='coreml', versions={'insightface': '2.1', 'onnxruntime': '1.30.0'})
    assert windows.identity != mac.identity  # today's model identity: incompatible
    a, b = windows.describe(), mac.describe()
    assert a['space_id'] == b['space_id']
    assert a['backend_id'] != b['backend_id']
    assert a['detector_id'] == b['detector_id']


@pytest.mark.parametrize('change', [
    {'matching': {'calibrated': True, 'threshold': .6, 'margin': .1}},
    {'license': {'approved': True, 'source': 'elsewhere'}},
    {'download': {'url': 'https://example.invalid/model.zip'}},
    {'settings': {'detection_threshold': .5, 'detector_size': 320}},
    {'detector': 'another detector'},
    {'name': 'renamed'},
])
def test_what_does_not_define_vectors_never_makes_a_new_space(tmp_path, change):
    for name in ('a', 'b'):
        (tmp_path / name).mkdir()
    base = service(tmp_path / 'a')
    other = service(tmp_path / 'b', **copy.deepcopy(change))
    assert base.describe()['space_id'] == other.describe()['space_id']


def space_of(directory, **changes):
    from face_detect.configuration import identity_manifest
    directory.mkdir()
    manifest, _ = model(directory, **changes)
    return identity.space_id(identity_manifest(manifest, {'device': 'cpu'}, {}))


@pytest.mark.parametrize('change', [
    {'recognizer': 'other weights'},
    {'dimensions': 128},
    {'normalization': 'none'},
    {'embedding': {'flip_average': True}},
])
def test_what_defines_vectors_is_a_new_space(tmp_path, change):
    assert space_of(tmp_path / 'a') != space_of(tmp_path / 'b', **copy.deepcopy(change))


def test_preprocessing_and_provider_are_part_of_the_space(tmp_path):
    from face_detect.configuration import identity_manifest
    manifest, _ = model(tmp_path)
    first = identity_manifest(manifest, {'device': 'cpu'}, {})
    assert identity.space_id(first) != identity.space_id(dict(first, preprocessing='another-decoder-v2'))
    (tmp_path / 'f').mkdir()
    facenet, _ = model(tmp_path / 'f', 'facenet')
    spec = identity.space_spec(identity_manifest(facenet, {'device': 'cpu'}, {}))
    assert spec['alignment']['kind'] == 'box'
    assert identity.space_spec(first)['alignment']['kind'] == 'landmarks'


def test_the_detector_has_its_own_identity(tmp_path):
    from face_detect.configuration import identity_manifest
    manifest, _ = model(tmp_path)
    first = identity_manifest(manifest, {'device': 'cpu'}, {})
    changed = copy.deepcopy(first)
    changed['settings']['detection_threshold'] = .6
    assert identity.detector_id(first) != identity.detector_id(changed)
    assert identity.space_id(first) == identity.space_id(changed)


def test_a_manifest_without_checksums_has_no_space(tmp_path):
    from face_detect.contracts import ConfigurationError
    with pytest.raises(ConfigurationError):
        identity.space_spec({'provider': 'insightface', 'assets': {}, 'preprocessing': 'p',
                             'dimensions': 512, 'normalization': 'l2'})
    with pytest.raises(ConfigurationError):
        identity.space_spec({'provider': 'synthetic'})


# -- embedding from stored geometry ----------------------------------------------------------

def test_stored_landmarks_give_the_scan_vector(tmp_path):
    svc = service(tmp_path)
    image = photos(1)[0]
    face = svc.detect(image)[0]
    [vector] = svc.embed_geometry(image, [{'landmarks': face['landmarks']}])
    assert conformance.cosine(face['vector'], vector) == pytest.approx(1.0)
    assert sum(v * v for v in vector) == pytest.approx(1.0)


@pytest.mark.parametrize('geometry', [
    [{'box': [10, 10, 50, 50]}],                      # this space aligns by landmarks
    [{'landmarks': [[1, 1]] * 4}],                    # four points
    [{'landmarks': [[1, 1]] * 4 + [[float('nan'), 1]]}],
    [{'landmarks': [[10000, 1]] * 5}],                # far outside the photo
    [{'landmarks': [[1, 1]] * 5, 'box': [1, 1, 2, 2]}],
    [{'landmarks': [[1, 1]] * 5}] * 101,
    'not a list',
])
def test_unclear_geometry_is_refused(tmp_path, geometry):
    with pytest.raises(ProviderUnavailable):
        service(tmp_path).embed_geometry(photos(1)[0], geometry)


def test_a_box_space_takes_boxes_only(tmp_path):
    svc = service(tmp_path, provider='facenet')
    image = photos(1)[0]
    assert len(svc.embed_geometry(image, [{'box': [10, 10, 100, 120]}])[0]) == 512
    with pytest.raises(ProviderUnavailable, match='box'):
        svc.embed_geometry(image, [{'landmarks': [[20, 20]] * 5}])
    with pytest.raises(ProviderUnavailable):
        svc.embed_geometry(image, [{'box': [10, 10, 9000, 120]}])


def test_a_provider_without_geometry_embedding_says_so(tmp_path):
    class Old:
        def analyze(self, image, include_embeddings=True):
            return []

        def close(self):
            pass
    with pytest.raises(ProviderUnavailable, match='cannot embed'):
        service(tmp_path, backend=Old()).embed_geometry(photos(1)[0], [{'landmarks': [[20, 20]] * 5}])


def test_the_worker_process_forwards_geometry(tmp_path, monkeypatch):
    from face_detect.providers import process
    python = tmp_path / 'python.exe'
    python.write_bytes(b'')
    seen = {}

    def run(command, input, **kwargs):
        seen['action'], seen['request'] = command[-1], json.loads(input)
        return type('Done', (), {'returncode': 0, 'stdout': json.dumps([[1.0] + [0.0] * 511]), 'stderr': ''})()
    monkeypatch.setattr(process.subprocess, 'run', run)
    provider = process.ProcessProvider({'provider': 'insightface', 'python': str(python)}, {}, {})
    assert provider.embed_geometry(photos(1)[0], [{'landmarks': [[1, 2]] * 5}]) == [[1.0] + [0.0] * 511]
    assert seen['action'] == 'embed' and seen['request']['geometry'] == [{'landmarks': [[1, 2]] * 5}]
    assert 'python' not in seen['request']['profile']


# -- conformance -----------------------------------------------------------------------------

def golden(tmp_path, svc=None):
    svc = svc or service(tmp_path)
    report = conformance.make_golden_set(svc, photos(), tmp_path / 'golden', 'Synthetic test pattern, no faces of people')
    return svc, report


def test_a_golden_set_records_the_space_and_reproduces_scans(tmp_path):
    svc, report = golden(tmp_path)
    assert report['cases'] == 6 and report['scan_agreement_min'] == pytest.approx(1.0)
    data = json.loads((tmp_path / 'golden/GOLDEN.json').read_text())
    assert data['space_id'] == svc.describe()['space_id'] and data['source'].startswith('Synthetic')
    assert data['reference']['device'] == 'cpu'


def test_a_golden_set_needs_its_source_and_a_new_folder(tmp_path):
    svc = service(tmp_path)
    with pytest.raises(ValueError, match='licence'):
        conformance.make_golden_set(svc, photos(), tmp_path / 'g', '  ')
    (tmp_path / 'existing').mkdir()
    with pytest.raises(FileExistsError):
        conformance.make_golden_set(svc, photos(), tmp_path / 'existing', 'Synthetic')


def test_the_same_numbers_on_another_backend_conform(tmp_path):
    _, _ = golden(tmp_path)
    mac = service(tmp_path, device='coreml', versions={'onnxruntime': '1.30.0'})
    certificate = conformance.check(mac, tmp_path / 'golden', min_vectors=6)
    assert certificate['passed'] is True and certificate['reason'] == 'conforms'
    assert certificate['worst'] == pytest.approx(1.0) and certificate['vectors'] == 6
    assert conformance.certifies(certificate, mac)
    # A certificate covers that backend only.
    assert not conformance.certifies(certificate, service(tmp_path, device='cuda'))


def test_a_backend_whose_numbers_drift_does_not_conform(tmp_path):
    golden(tmp_path)
    drifting = service(tmp_path, backend=Backend(noise=0.2))
    certificate = conformance.check(drifting, tmp_path / 'golden', min_vectors=6)
    assert certificate['passed'] is False and certificate['reason'] == 'vectors-differ'
    assert certificate['worst'] < conformance.DEFAULT_TOLERANCE
    assert not conformance.certifies(certificate, drifting)


def test_another_space_is_refused_without_running_the_model(tmp_path):
    golden(tmp_path)
    (tmp_path / 'other').mkdir()
    backend = Backend()
    other = service(tmp_path / 'other', backend=backend, recognizer='different weights')
    certificate = conformance.check(other, tmp_path / 'golden', min_vectors=6)
    assert certificate['passed'] is False and certificate['reason'] == 'different-space'
    assert backend.embedded == 0


def test_too_few_vectors_prove_nothing(tmp_path):
    svc, _ = golden(tmp_path)
    certificate = conformance.check(svc, tmp_path / 'golden')
    assert certificate['passed'] is False and certificate['reason'] == 'too-few-vectors'


def test_a_changed_golden_image_is_refused(tmp_path):
    svc, _ = golden(tmp_path)
    image = tmp_path / 'golden/images/000.png'
    Image.new('RGB', Image.open(image).size, 'white').save(image)
    with pytest.raises(conformance.InvalidGoldenSet, match='changed'):
        conformance.check(svc, tmp_path / 'golden', min_vectors=6)


def test_a_golden_case_outside_its_set_is_refused(tmp_path):
    svc, _ = golden(tmp_path)
    path = tmp_path / 'golden/GOLDEN.json'
    data = json.loads(path.read_text())
    data['cases'][0]['image'] = '../elsewhere.png'
    path.write_text(json.dumps(data))
    with pytest.raises(conformance.InvalidGoldenSet, match='outside'):
        conformance.check(svc, tmp_path / 'golden', min_vectors=6)


def test_the_command_describes_and_certifies(tmp_path, capsys):
    svc, _ = golden(tmp_path)
    assert cli.main(['describe'], service_factory=lambda: service(tmp_path)) == 0
    assert json.loads(capsys.readouterr().out)['space_id'] == svc.describe()['space_id']
    out = tmp_path / 'certificate.json'
    # Too few vectors for the default bound, so the command reports a failure.
    assert cli.main(['check', '--golden', str(tmp_path / 'golden'), '--out', str(out)],
                    service_factory=lambda: service(tmp_path)) == 1
    assert json.loads(out.read_text())['reason'] == 'too-few-vectors'


# -- the library as the reference ------------------------------------------------------------

def stored_library(svc, count=14):
    """Faces as the catalog stores them: normalized box, score and the scan's vector."""
    from face_detect.spaces import library_check  # noqa: F401
    images, faces = {}, []
    for n, image in enumerate(photos(count)):
        digest = f'{n:064x}'
        images[digest] = image
        face = svc.detect(image)[0]
        b = face['box']
        faces.append({'content_hash': digest, 'score': b['probability'], 'vector': face['vector'],
                      'box': {'x': b['x_min'] / image.width, 'y': b['y_min'] / image.height,
                              'width': (b['x_max'] - b['x_min']) / image.width,
                              'height': (b['y_max'] - b['y_min']) / image.height}})
    return svc.manifest, faces, images.__getitem__


def test_the_same_model_on_another_computer_adopts_the_stored_vectors(tmp_path):
    from face_detect.spaces import library_check
    manifest, faces, load = stored_library(service(tmp_path))
    mac = service(tmp_path, device='coreml', versions={'onnxruntime': '1.30.0'})
    certificate = library_check.check(mac, manifest, faces, load)
    assert certificate['passed'] is True and certificate['reason'] == 'conforms'
    assert certificate['reference'] == 'library-sample' and certificate['sampled'] == 14
    assert certificate['worst'] == pytest.approx(1.0)
    assert certificate['stored_backend']['device'] == 'cpu' and certificate['backend']['device'] == 'coreml'
    # The stored manifest doesn't say which computer made it, so this one's isn't claimed.
    assert set(certificate['stored_backend']) == {'device', 'runtime_versions'}
    assert conformance.certifies(certificate, mac)


def test_drifting_numbers_keep_the_stored_vectors_out(tmp_path):
    from face_detect.spaces import library_check
    manifest, faces, load = stored_library(service(tmp_path))
    certificate = library_check.check(service(tmp_path, backend=Backend(noise=0.5)), manifest, faces, load)
    assert certificate['passed'] is False and certificate['reason'] == 'vectors-differ'


def test_another_space_is_refused_without_reading_photos(tmp_path):
    from face_detect.spaces import library_check
    manifest, faces, _ = stored_library(service(tmp_path))
    (tmp_path / 'other').mkdir()
    backend = Backend()
    other = service(tmp_path / 'other', backend=backend, recognizer='different weights')
    certificate = library_check.check(other, manifest, faces, lambda digest: pytest.fail('No photo may be read'))
    assert certificate['reason'] == 'different-space' and backend.embedded == 0


def test_too_few_good_faces_prove_nothing(tmp_path):
    from face_detect.spaces import library_check
    manifest, faces, load = stored_library(service(tmp_path))
    for face in faces[3:]:
        face['score'] = 0.5
    certificate = library_check.check(service(tmp_path), manifest, faces, load)
    assert certificate['passed'] is False and certificate['reason'] == 'too-few-faces' and certificate['sampled'] == 3


def test_a_library_whose_detector_never_scores_high_is_still_checked(tmp_path):
    """A library scanned only with the current detector can have no face at 0.95. Its best
    faces are sampled instead, and the certificate says how clear the weakest one was."""
    from face_detect.spaces import library_check
    manifest, faces, load = stored_library(service(tmp_path))
    for index, face in enumerate(faces):
        face['score'] = 0.86 + index * 0.005      # 0.86 to 0.925: none at MIN_SCORE
    certificate = library_check.check(service(tmp_path), manifest, faces, load)
    assert certificate['passed'] is True and certificate['sampled'] == 14
    assert certificate['lowest_sample_score'] == pytest.approx(0.86)
    assert certificate['worst'] == pytest.approx(1.0)


def test_a_wrong_model_still_fails_on_a_weaker_sample(tmp_path):
    from face_detect.spaces import library_check
    manifest, faces, load = stored_library(service(tmp_path))
    for face in faces:
        face['score'] = 0.9
    certificate = library_check.check(service(tmp_path, backend=Backend(noise=0.5)), manifest, faces, load)
    assert certificate['passed'] is False and certificate['reason'] == 'vectors-differ'


def test_faces_near_the_detectors_threshold_are_never_sampled(tmp_path):
    from face_detect.spaces import library_check
    manifest, faces, load = stored_library(service(tmp_path))
    for face in faces:
        face['score'] = 0.84
    certificate = library_check.check(service(tmp_path), manifest, faces, load)
    assert certificate['passed'] is False and certificate['reason'] == 'too-few-faces' and certificate['sampled'] == 0
    assert 'lowest_sample_score' not in certificate


def test_clear_faces_come_first_and_weaker_ones_only_fill_the_sample(tmp_path):
    from face_detect.spaces import library_check
    _, faces, _ = stored_library(service(tmp_path))
    clear = [dict(face, score=0.97) for face in faces[:10]]
    weaker = [dict(face, score=0.86 + index * 0.01) for index, face in enumerate(faces[10:])]
    # Enough clear faces: the sample is exactly what it was before, whatever else is there.
    assert library_check.choose_samples(clear + weaker, 10) == library_check.choose_samples(clear, 10)
    assert all(face['score'] == 0.97 for face in library_check.choose_samples(clear + weaker, 10))
    # Too few to be checked at all: every clear face, then the best of the rest, highest first.
    filled = library_check.choose_samples(clear + weaker, 12)
    assert filled[:10] == library_check.choose_samples(clear, 10)
    assert [face['score'] for face in filled[10:]] == sorted((face['score'] for face in weaker), reverse=True)[:2]
    assert library_check.choose_samples(list(reversed(clear + weaker)), 12) == filled
    # A photo already sampled for a clear face isn't sampled again for a weaker one.
    second = [dict(face, score=0.9, box=dict(face['box'], x=face['box']['x'] + 0.01)) for face in clear]
    assert len(library_check.choose_samples(clear + second, 24)) == 10


@pytest.mark.parametrize('clear_count', [12, 15, 23])
def test_a_library_that_could_be_checked_before_gets_exactly_its_old_sample(tmp_path, clear_count):
    """With 12 to 23 clear faces the old rule sampled just those, and the check could pass on
    them. Weaker faces are the likeliest not to be found again, so none may be added."""
    from face_detect.spaces import library_check

    def face(index, score):
        return {'content_hash': f'{index:064x}', 'score': score,
                'box': {'x': 0.1, 'y': 0.1, 'width': 0.2, 'height': 0.2}}
    clear = [face(index, 0.97) for index in range(clear_count)]
    weaker = [face(100 + index, 0.86) for index in range(30)]
    sample = library_check.choose_samples(clear + weaker)
    assert len(sample) == clear_count and all(item['score'] == 0.97 for item in sample)
    assert sample == library_check.choose_samples(clear)
    # One fewer than the minimum, and the library couldn't be checked: now it is filled.
    short = library_check.choose_samples(clear[:11] + weaker)
    assert len(short) == 24 and sum(item['score'] == 0.97 for item in short) == 11


def test_faces_that_are_not_found_again_are_refused(tmp_path):
    from face_detect.spaces import library_check
    manifest, faces, load = stored_library(service(tmp_path))
    for face in faces[:4]:
        face['box'] = {'x': 0.0, 'y': 0.0, 'width': 0.1, 'height': 0.1}
    certificate = library_check.check(service(tmp_path), manifest, faces, load)
    assert certificate['passed'] is False and certificate['reason'] == 'faces-not-found' and certificate['unmatched'] == 4


def test_the_sample_is_one_good_face_per_photo_in_a_fixed_order(tmp_path):
    from face_detect.spaces import library_check
    _, faces, _ = stored_library(service(tmp_path))
    twice = faces + [dict(face, box=dict(face['box'], x=face['box']['x'] + 0.01)) for face in faces]
    first = library_check.choose_samples(twice, 10)
    assert len({face['content_hash'] for face in first}) == 10
    assert library_check.choose_samples(list(reversed(twice)), 10) == first


def library_on_disk(tmp_path):
    """A stored library whose photos are files, as `read_library` returns it."""
    manifest, faces, load = stored_library(service(tmp_path))
    (tmp_path / 'media').mkdir()
    paths, file_sha256 = {}, {}
    for face in faces:
        path = tmp_path / 'media' / f"{face['content_hash'][-4:]}.PNG"
        load(face['content_hash']).save(path, format='PNG')
        paths[face['content_hash']] = path
        file_sha256[face['content_hash']] = hashlib.sha256(path.read_bytes()).hexdigest()
    return manifest, faces, paths, file_sha256


def test_the_command_checks_against_the_library(tmp_path, capsys):
    library = library_on_disk(tmp_path)
    out = tmp_path / 'certificate.json'
    assert cli.main(['check-library', '--out', str(out)], service_factory=lambda: service(tmp_path),
                    library=lambda cfg, data, size: library) == 0
    assert json.loads(out.read_text())['reason'] == 'conforms'
    with pytest.raises(SystemExit):
        cli.main(['check-library', '--out', str(out)], service_factory=lambda: service(tmp_path),
                 library=lambda cfg, data, size: library)


def test_a_photo_changed_since_the_catalog_read_it_is_unreadable(tmp_path, capsys):
    manifest, faces, paths, file_sha256 = library_on_disk(tmp_path)
    paths[faces[0]['content_hash']].write_bytes(b'another file')
    out = tmp_path / 'certificate.json'
    assert cli.main(['check-library', '--out', str(out)], service_factory=lambda: service(tmp_path),
                    library=lambda cfg, data, size: (manifest, faces, paths, file_sha256)) == 0
    assert json.loads(out.read_text())['unreadable'] == 1


def test_a_sample_carried_to_another_computer_certifies_it(tmp_path, capsys):
    library = library_on_disk(tmp_path)
    carried = tmp_path / 'familyfoto-sample'

    def no_model():
        raise AssertionError('Writing a sample must not load the model')
    assert cli.main(['check-library', '--export-sample', str(carried)], service_factory=no_model,
                    library=lambda cfg, data, size: library) == 0
    written = json.loads(capsys.readouterr().out)
    assert written['faces'] == 14 and written['photos'] == 14
    assert sorted(p.name for p in carried.iterdir()) == ['photos', 'sample.json']
    # Only what the check needs: no catalog ids, names or paths.
    sample = json.loads((carried / 'sample.json').read_text())
    assert set(sample['faces'][0]) == {'content_hash', 'photo', 'photo_sha256', 'box', 'score', 'vector'}
    assert all(p.suffix == '.png' for p in (carried / 'photos').iterdir())

    out = tmp_path / 'mac.json'
    mac = lambda: service(tmp_path, device='coreml', versions={'onnxruntime': '1.30.0'})  # noqa: E731
    assert cli.main(['check-library', '--sample-from', str(carried), '--out', str(out)], service_factory=mac,
                    library=lambda cfg, data, size: pytest.fail('The catalog is not read')) == 0
    certificate = json.loads(out.read_text())
    assert certificate['reason'] == 'conforms' and certificate['backend']['device'] == 'coreml'

    # The folder must be new, and a damaged sample photo is not compared.
    with pytest.raises(SystemExit):
        cli.main(['check-library', '--export-sample', str(carried)], service_factory=no_model,
                 library=lambda cfg, data, size: library)
    next(iter((carried / 'photos').iterdir())).write_bytes(b'damaged')
    out = tmp_path / 'damaged.json'
    cli.main(['check-library', '--sample-from', str(carried), '--out', str(out)], service_factory=mac)
    assert json.loads(out.read_text())['unreadable'] == 1


def test_a_sample_is_refused_if_it_was_not_finished_or_names_other_files(tmp_path):
    from face_detect.spaces import library_check
    manifest, faces, paths, file_sha256 = library_on_disk(tmp_path)
    carried = tmp_path / 'familyfoto-sample'
    library_check.write_sample(carried, manifest, faces, paths, file_sha256)
    sample = json.loads((carried / 'sample.json').read_text())
    sample['faces'][0]['photo'] = '../../secret.png'
    (carried / 'sample.json').write_text(json.dumps(sample))
    with pytest.raises(ValueError):
        library_check.read_sample(carried)
    (carried / 'sample.json').unlink()
    with pytest.raises(FileNotFoundError):
        library_check.read_sample(carried)
    # A photo that no longer matches the catalog is never copied.
    paths[faces[0]['content_hash']].write_bytes(b'changed')
    with pytest.raises(ValueError):
        library_check.write_sample(tmp_path / 'again', manifest, faces, paths, file_sha256)


def test_a_photo_that_cannot_be_opened_is_counted_as_unreadable(tmp_path):
    from face_detect.spaces import library_check
    manifest, faces, load = stored_library(service(tmp_path))
    missing = {faces[0]['content_hash']}

    def open_photo(digest):
        if digest in missing:
            raise FileNotFoundError('gone')
        return load(digest)
    certificate = library_check.check(service(tmp_path), manifest, faces, open_photo)
    assert certificate['unreadable'] == 1 and certificate['unmatched'] == 0 and certificate['passed'] is True


def test_a_media_disk_that_is_not_mounted_says_the_photos_are_unreadable(tmp_path):
    from face_detect.spaces import library_check
    manifest, faces, _ = stored_library(service(tmp_path))

    def open_photo(digest):
        raise FileNotFoundError('not mounted')
    certificate = library_check.check(service(tmp_path), manifest, faces, open_photo)
    assert certificate['passed'] is False and certificate['reason'] == 'photos-unreadable'
    assert certificate['unreadable'] == certificate['sampled'] and certificate['unmatched'] == 0


def test_a_name_spelt_another_way_on_this_disk_is_found(tmp_path):
    import unicodedata
    name = unicodedata.normalize('NFD', 'Séoul 서울.jpg')
    (tmp_path / 'trip').mkdir()
    (tmp_path / 'trip' / name).write_bytes(b'photo')
    stored = 'trip/' + unicodedata.normalize('NFC', 'Séoul 서울.jpg')
    assert cli.photo_path(tmp_path, stored).read_bytes() == b'photo'
    assert cli.photo_path(tmp_path, 'trip/absent.jpg') == tmp_path / 'trip/absent.jpg'
