import time
import pytest
from contextlib import contextmanager
from unittest.mock import patch
from fastapi.testclient import TestClient
from app.api import app
from app.auth import password_hash,session_token,session_user
pytestmark = pytest.mark.usefixtures('auth_memory')

def config():
    return {'host':'127.0.0.1','browser_user':'private-admin','browser_password_hash':password_hash('test-admin'),
            'auth_secret':'fixture-secret-'*4,
            'browser_accounts':{'familyfoto':{'role':'regular','password_hash':password_hash('test-regular')},
                                'guest':{'role':'guest','password_hash':password_hash('test-guest')}}}

@pytest.mark.parametrize('preview_enabled',[False,True])
def test_roles_server_side_even_on_localhost(monkeypatch,preview_enabled):
    monkeypatch.setattr('face_detect.api.people_preview_gateway.settings',lambda:{} if preview_enabled else None)
    c=config()
    @contextmanager
    def connection():yield object()
    with patch('app.auth.api.load_config',return_value=c),patch('app.library.api.load_config',return_value=c),patch('app.library.api.connection',connection),patch('app.library.trash.change',return_value={'ok':True}) as change:
        client=TestClient(app)
        assert client.get('/api/me').status_code==401
        for name,password,role in [('private-admin','test-admin','admin'),('familyfoto','test-regular','regular'),('guest','test-guest','guest')]:
            assert client.post('/api/login',json={'username':name,'password':password}).status_code==200
            assert client.get('/api/me').json()=={'username':name,'role':role,'capabilities':{'people_preview':preview_enabled}}
            change.reset_mock()
            assert client.post('/api/media/1/trash/delete').status_code==(200 if role=='admin' else 403)
            assert change.call_count==(1 if role=='admin' else 0)
            assert client.post('/api/media/1/trash/trash').status_code==(403 if role=='guest' else 200)
            if role!='admin':
                for route in ('/api/storage/settings','/api/storage/select-folder','/api/import/select-folder','/api/import/start','/api/import/1/import/resume'):
                    assert client.post(route,json={}).status_code==403
                assert client.get('/api/storage/settings').status_code==403
            if role=='guest':
                for route in ('/api/import/1/metadata/resume','/api/import/1/conversion/resume','/api/import/1/pause','/api/import/1/options'):
                    assert client.post(route,json={'auto_metadata':True,'auto_conversion':True}).status_code==403
                for path in ['/api/import/resume','/api/import/pause','/api/conversions/resume','/api/media/1/rotate','/api/media/1/crop','/api/media/1/trash/restore']:
                    assert client.post(path,json={}).status_code==403
        assert 'private-admin' not in client.get('/login').text
        assert 'value="familyfoto"' in client.get('/login').text
        assert client.post('/api/login',json={'username':'guest','password':'wrong'}).status_code==401

def test_session_bound_to_account_role_and_password():
    c=config();token=session_token(c,int(time.time())+60,'guest')
    assert session_user(c,token)['role']=='guest'
    assert session_user(c,('a' if token[0]!='a' else 'b')+token[1:]) is None
    c['browser_accounts']['guest']['role']='regular'
    assert session_user(c,token) is None
    token=session_token(c,int(time.time())+60,'guest')
    c['browser_accounts']['guest']['password_hash']=password_hash('replacement')
    assert session_user(c,token) is None
