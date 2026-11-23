import json
from contextlib import nullcontext
from types import SimpleNamespace
import pytest

from PIL import Image, ImageCms
from face_detect.recognition.people_scan_warnings import decode_detail
from face_detect.recognition.people_scan_warnings import import_warnings
from face_detect.jobs.automatic_people_scan import RawJobs
from face_detect.jobs.automatic_people_scan import DecodeRejected
from face_detect.jobs.automatic_people_import import ImportJobs
from face_detect.jobs import automatic_people_jobs as workers
from app.telemetry import write_json


USER = {'username': 'fixture', 'role': 'admin'}


def test_decode_detail_is_readable_and_does_not_expose_arbitrary_paths():
    assert 'color profile' in decode_detail(ImageCms.PyCMSError('cannot build transform'))
    assert 'pixel count' in decode_detail(Image.DecompressionBombError('huge'))
    assert 'truncated' in decode_detail(OSError('image file is truncated (16 bytes not processed)'))
    assert 'broken data stream' in decode_detail(OSError('broken data stream when reading image file'))
    assert 'private' not in decode_detail(OSError('cannot open C:/private/photo.jpg'))
    assert decode_detail(ValueError('Image format GIF is not supported for face detection')).startswith('Image format GIF')


def test_warning_detail_survives_child_and_parent_checkpoints(tmp_path, monkeypatch):
    monkeypatch.setattr(workers.service, 'load_config', lambda: {'automatic_people': {'enabled': True}})
    monkeypatch.setattr(workers, 'worker_lease', lambda **kwargs: nullcontext(object()))
    monkeypatch.setattr(workers, 'background_unit', lambda: nullcontext(object()))
    monkeypatch.setattr(workers, 'foreground_busy', lambda lease: False)
    raw = RawJobs(tmp_path, tmp_path, SimpleNamespace(identity='fixture'), None, None)
    detail = 'The embedded color profile could not be converted to sRGB for face detection.'
    def rejected(*args):
        raise DecodeRejected('corrupt_or_unsafe_media', detail)
    monkeypatch.setattr(raw, 'scan', rejected)
    child = raw.job('import-1-photo-7')
    child.directory.mkdir(parents=True)
    child.command('run')
    write_json(child.directory / 'raw.json', {'model_id': 'fixture', 'state': 'queued',
        'items': [{'media_id': 7, 'state': 'pending', 'attempts': 0}]})
    parent = raw.job('import-1')
    parent.directory.mkdir()
    parent.command('run')
    write_json(parent.directory / 'import.json', {'state': 'queued', 'run_seconds': 0,
        'items': [{'media_id': 7, 'child': 'import-1-photo-7', 'state': 'pending'}]})
    jobs = ImportJobs(raw)
    result = jobs.run('import-1', USER)
    assert result['state'] == 'completed'
    assert result['errors'] == [{'media_id': 7, 'error': 'corrupt_or_unsafe_media', 'detail': detail}]
    for path in [child.directory / 'raw.json', parent.directory / 'import.json']:
        assert json.loads(path.read_text())['items'][0]['error_detail'] == detail


def test_diagnostics_resolve_names_and_preserve_legacy_unknown_reason(tmp_path):
    path = tmp_path / 'jobs' / 'import-1' / 'import.json'
    path.parent.mkdir(parents=True)
    state = {'items': [
        {'media_id': 7, 'error': 'corrupt_or_unsafe_media', 'child': 'import-1-photo-7'},
        {'media_id': 8, 'error': 'unsupported_media'},
        {'media_id': 9, 'state': 'done'},
        {'media_id': 10, 'error': 'unsupported_media', 'child': '../../outside'},
    ]}
    write_json(path, state)
    child = tmp_path / 'jobs' / 'import-1-photo-7' / 'raw.json'
    child.parent.mkdir()
    write_json(child, {'items': [{'media_id': 7, 'error_detail': 'Color profile conversion failed.'}]})
    calls = []
    class DB:
        def execute(self, sql, params):
            calls.append(params)
            return SimpleNamespace(fetchall=lambda: [{'id': 7, 'name': 'portrait.tif'}, {'id': 8, 'name': 'animation.gif'}])
    result = import_warnings(DB(), tmp_path, 1)
    assert calls == [([7, 8, 10],)]
    assert result[0]['name'] == 'portrait.tif'
    assert result[0]['detail'] == 'Color profile conversion failed.'
    assert result[1]['detail'] is None
    assert result[2]['name'] is None
    assert json.loads(path.read_text()) == state
    assert import_warnings(DB(), tmp_path, 99) == []


@pytest.mark.parametrize('role', ['admin', 'regular', 'guest'])
def test_import_endpoint_only_adds_people_details_for_admin(monkeypatch, tmp_path, role):
    from app.imports import api
    from app.imports import diagnostics as import_diagnostics
    from face_detect.api import people_preview_gateway
    from face_detect.recognition import people_scan_warnings
    monkeypatch.setattr(api, 'connection', lambda: nullcontext(object()))
    monkeypatch.setattr(import_diagnostics, 'diagnostics', lambda db, jid: [])
    monkeypatch.setattr(people_preview_gateway, 'settings', lambda: {'root': tmp_path})
    calls = []
    def warnings(db, root, jid):
        calls.append(jid)
        return [{'stage': 'Discover people'}]
    monkeypatch.setattr(people_scan_warnings, 'import_warnings', warnings)
    result = api.import_diagnostics(1, SimpleNamespace(state=SimpleNamespace(user={'role': role})))
    assert calls == ([1] if role == 'admin' else [])
    assert result == ([{'stage': 'Discover people'}] if role == 'admin' else [])
