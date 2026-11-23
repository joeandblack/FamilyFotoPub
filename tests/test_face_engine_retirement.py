"""Retired engines cannot be launched or reached through production APIs."""
from pathlib import Path
import ast
import pytest
from fastapi import HTTPException, Request, Response
from fastapi.testclient import TestClient
from app import api
from routers import CONFIG_MODULES, DATA_MODULES, patch_all
from face_detect.jobs import people_launch
from face_detect.jobs import people_compute
from app.auth.deps import reader
from app.auth.deps import administrator

ROOT = Path(__file__).parents[1]


def test_retired_routes_are_not_mounted():
    paths = [getattr(route, 'path', '') for route in api.app.routes]
    assert not any(path.startswith(('/api/faces/', '/api/people/', '/api/ignored-faces')) for path in paths)
    assert any(path.startswith('/api/people-preview/') for path in paths)


def test_legacy_settings_cannot_reenable_pilot_routes(monkeypatch,auth_memory):
    from test_roles import config
    settings = {**config(), 'faces': {'enabled': True}, 'people': {'enabled': True}}
    patch_all(monkeypatch, CONFIG_MODULES, 'load_config', lambda: settings)
    monkeypatch.setattr('face_detect.api.people_preview_gateway.settings', lambda: None)
    client = TestClient(api.app)
    try:
        assert client.post('/api/login', json={'username': 'private-admin', 'password': 'test-admin'}).status_code == 200
        assert client.get('/api/me').json()['capabilities'] == {'people_preview': False}
        for prefix in ('/api/faces', '/api/people', '/api/ignored-faces'):
            assert client.get(prefix+'/status').status_code == 404
            assert client.post(prefix+'/runs', json={}).status_code == 404
    finally:
        client.close()


@pytest.mark.parametrize('module', ['app.people_worker', 'app.face_worker'])
def test_retired_workers_cannot_be_launched(module, tmp_path):
    with pytest.raises(ValueError, match='Unknown worker'):
        people_launch.launch_explicit(module, [], 'retired', tmp_path/'worker.log')
    assert not (tmp_path/'worker.log').exists()


def test_people_authorization_is_preserved():
    request = Request({'type': 'http'})
    response = Response()
    with pytest.raises(HTTPException) as error:
        reader(request, response)
    assert error.value.status_code == 401
    request.state.authenticated = True
    for role in ('guest', 'regular', 'admin'):
        request.state.user = {'role': role}
        assert reader(request, response)['role'] == role
        if role != 'admin':
            with pytest.raises(HTTPException) as error:
                administrator(request.state.user)
            assert error.value.status_code == 403
        else:
            assert administrator(request.state.user)['role'] == role
    assert response.headers['Cache-Control'] == 'no-store'


def test_shared_compute_contract_is_unchanged():
    assert people_compute.COMPUTE_LOCK == 4340034
    assert people_compute.MAINTENANCE_LOCKS == (4340006, 4340005, 4340002)


def test_production_sources_do_not_import_retired_engines():
    retired = {'face_providers','embedding_providers','face_worker','people_worker',
               'face_launch','people_api','ignored_faces_api','face_additions',
               'face_telemetry','people_grouping'}
    for folder in ('app', 'scripts'):
        for path in (ROOT/folder).glob('*.py'):
            tree = ast.parse(path.read_text(encoding='utf-8-sig'))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    names = {(node.module or '').split('.')[-1]}
                    if node.module in (None, 'app'):
                        names.update(alias.name for alias in node.names)
                elif isinstance(node, ast.Import):
                    names = {alias.name.split('.')[-1] for alias in node.names}
                else:
                    continue
                assert not names & retired, (path, names & retired)


def test_old_inference_files_are_absent():
    for filename in ('face_providers.py', 'embedding_providers.py', 'face_worker.py', 'people_worker.py'):
        assert not (ROOT/'app'/filename).exists()
