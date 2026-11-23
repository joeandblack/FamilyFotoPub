import hashlib
from io import BytesIO
import json

from PIL import Image
import pytest

from app.migration.people_thumbnail import crop_thumbnail
from face_detect.api.readonly_people import catalog_settings


def test_fresh_native_catalog_is_recovery_readable(tmp_path):
    pilot = 'a' * 32
    root = tmp_path / 'face_detect/profiles' / pilot
    root.mkdir(parents=True)
    (tmp_path / 'people-preview.json').write_text(json.dumps(
        {'enabled': True, 'backend': 'native', 'pilot': pilot}))
    (root / 'runtime.json').write_text(json.dumps({'schema': 'people'}))
    assert catalog_settings(tmp_path, recovery=True)['native'] is True
    assert catalog_settings(tmp_path, recovery=True)['legacy_pilot'] is None
    # The one catalog needs no per-computer 'preserved' mark: a Mac that adopts it opens it too.
    assert catalog_settings(tmp_path)['schema'] == 'people'
    # A library restored from before the one catalog says when its People appear.
    (root / 'runtime.json').write_text(json.dumps({'schema': 'face_native_' + pilot}))
    with pytest.raises(ValueError, match='run the catalog migration'):
        catalog_settings(tmp_path, recovery=True)
    (root / 'runtime.json').write_text(json.dumps({'schema': 'public'}))
    with pytest.raises(ValueError):
        catalog_settings(tmp_path, recovery=True)


def test_rebuild_is_memory_only(tmp_path):
    path = tmp_path / 'photo.png'
    Image.new('RGB', (400, 300), 'red').save(path)
    original = path.read_bytes()
    result = crop_thumbnail(path, hashlib.sha256(original).hexdigest(),
                            {'x': .2, 'y': .2, 'width': .3, 'height': .3})
    with Image.open(BytesIO(result)) as image:
        assert max(image.size) <= 160
        assert image.format == 'JPEG'
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]
    with pytest.raises(ValueError, match='checksum'):
        crop_thumbnail(path, '0' * 64, {'x': 0, 'y': 0, 'width': 1, 'height': 1})


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -1, 2, '0.1', True])
def test_bad_bounds_refused(tmp_path, value):
    with pytest.raises(ValueError):
        crop_thumbnail(tmp_path / 'absent', '', {'x': value, 'y': 0, 'width': .2, 'height': .2})


@pytest.mark.parametrize('kind', ['group', 'ignored', 'blurry'])
def test_recovery_checks_visibility_before_reading_photo(tmp_path, monkeypatch, kind):
    from contextlib import contextmanager
    from fastapi import HTTPException
    from app.migration.people_thumbnail import recovered_thumbnail
    from face_detect.api import readonly_people

    class Database:
        def execute(self, query, params):
            assert query.startswith('SELECT d.box,a.media_id,m.sha256 ')
            assert params == (1,)
            return self

        def fetchone(self):
            return {'media_id': 2, 'sha256': 'a' * 64, 'box': {}}

    @contextmanager
    def connect():
        yield Database()

    monkeypatch.setattr(readonly_people, 'live_photos', lambda cfg, expected: [])
    with pytest.raises(HTTPException) as error:
        recovered_thumbnail({'destination': {'root': str(tmp_path)}}, connect, 1, kind)
    assert error.value.status_code == 404
    assert error.value.detail == 'Photo unavailable'


@pytest.mark.parametrize('suffix', ['thumbnails/1', 'ignored/1/thumbnail', 'blurry/1/thumbnail'])
def test_missing_cache_uses_recovery_fallback_only(tmp_path, monkeypatch, suffix):
    from contextlib import contextmanager
    from fastapi import APIRouter, FastAPI
    from fastapi.testclient import TestClient
    from fastapi.responses import Response
    from app.auth.deps import reader
    from app.migration import people_thumbnail
    from face_detect.api.readonly_people import add_thumbnail_routes

    class Database:
        def execute(self, *args):
            return self

        def fetchone(self):
            return {'id': 1}

    @contextmanager
    def connect():
        yield Database()

    calls = []

    def render(cfg, connect, did, kind):
        calls.append((did, kind))
        return Response(b'fixture-image', media_type='image/jpeg')

    monkeypatch.setattr(people_thumbnail, 'recovered_thumbnail', render)
    for recovery in (False, True):
        app = FastAPI()
        app.dependency_overrides[reader] = lambda: {'role': 'admin'}
        router = APIRouter()
        add_thumbnail_routes(router, {'root': tmp_path}, connect,
                             {'recovery': {'activation_required': recovery}})
        app.include_router(router)
        with TestClient(app) as client:
            result = client.get('/' + suffix)
        assert result.status_code == (200 if recovery else 404)
    assert len(calls) == 1
