import json
import pytest
from face_detect.api import service


@pytest.mark.parametrize('saved', [None, {}, {'start_with_app':False}, {'start_with_app':'true'}])
def test_native_service_startup_defaults_off(tmp_path, monkeypatch, saved):
    path=tmp_path/'settings.json'
    if saved is not None:path.write_text(json.dumps(saved))
    monkeypatch.setattr(service,'PREFERENCES',path)
    monkeypatch.setattr(service,'enqueue',lambda action:pytest.fail('Must not start automatically'))
    assert service.preferences()=={'start_with_app':False}
    service.start_with_app()


def test_native_service_requires_explicit_true(tmp_path, monkeypatch):
    path=tmp_path/'settings.json'
    path.write_text(json.dumps({'start_with_app':True}))
    monkeypatch.setattr(service,'PREFERENCES',path)
    calls=[]
    monkeypatch.setattr(service,'enqueue',calls.append)
    service.start_with_app()
    assert calls==['start']
