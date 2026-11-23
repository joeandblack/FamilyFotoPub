import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from face_detect.api import people_preview_gateway as gateway


@pytest.fixture
def client(monkeypatch):
    app=FastAPI()
    @app.middleware('http')
    async def identity(request,next):
        role=request.headers.get('x-role','admin')
        request.state.authenticated=role!='none'
        request.state.user={'username':'fixture','role':role}
        return await next(request)
    app.include_router(gateway.router)
    monkeypatch.setattr(gateway,'settings',lambda:{'port':49591,'pilot':'a'*32})
    with TestClient(app) as client:yield client


def test_preview_is_readable_but_changes_are_admin_only(client):
    assert client.get('/people-preview').status_code==200
    for role in ('regular', 'guest'):
        response = client.get('/people-preview',headers={'x-role':role})
        assert response.status_code == 200
        assert '/web/read-only-preview.mjs' in response.text
        assert client.post('/api/people-preview/review/match',json={},headers={'x-role':role}).status_code==403
    assert client.get('/people-preview',headers={'x-role':'none'}).status_code==401
    assert client.get('/api/people-preview',headers={'x-role':'none'}).status_code==401


def test_proxy_is_fixed_loopback_and_checks_pilot(client,monkeypatch):
    import httpx
    calls=[]
    identity=['a'*32]
    class Fake:
        def __init__(self,**kwargs):assert kwargs['trust_env'] is False and kwargs['follow_redirects'] is False
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def request(self,method,url,**kwargs):
            calls.append((method,url,kwargs))
            return httpx.Response(200,json={'ok':True},headers={'x-familyfoto-pilot':identity[0]})
    monkeypatch.setattr(gateway.httpx,'AsyncClient',Fake)
    for role in ('regular', 'guest'):
        assert client.get('/api/people-preview/cards',headers={'x-role':role}).status_code == 200
    calls.clear()
    assert client.post('/api/people-preview/jobs/completed-imports/recheck',json={'revision':0}).json()=={'ok':True}
    method,url,args=calls[0]
    assert url=='http://127.0.0.1:49591/api/automatic-people/jobs/completed-imports/recheck'
    assert args['headers']['origin']=='http://127.0.0.1:49591'
    identity[0]='wrong'
    assert client.get('/api/people-preview').status_code==503
    assert client.post('/api/people-preview',content='x'*65537).status_code==413


def test_config_is_opt_in_and_bounded(tmp_path,monkeypatch):
    import json
    path=tmp_path/'settings.json';monkeypatch.setattr(gateway,'SETTINGS',path)
    assert gateway.settings() is None
    path.write_text(json.dumps({'enabled':True,'backend':'native','pilot':'../outside','port':49591}))
    with pytest.raises(ValueError):gateway.settings()
    path.write_text(json.dumps({'enabled':True,'backend':'native','pilot':'b'*32,'port':4340}))
    with pytest.raises(ValueError):gateway.settings()


@pytest.mark.parametrize('failure,path,code,phrase',[
    ('ReadTimeout','jobs/completed-imports/recheck',504,'may still be running or may have completed'),
    ('ReadTimeout','groups/1',504,'People took too long to respond'),
    ('ConnectError','jobs/completed-imports/recheck',503,'Cannot connect to the People service'),
    ('RemoteProtocolError','jobs/completed-imports/recheck',502,'connection to the People service was interrupted'),
])
def test_proxy_failures_give_specific_guidance(client,monkeypatch,failure,path,code,phrase):
    import httpx
    class Fake:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def request(self,*args,**kwargs):raise getattr(httpx,failure)('fixture')
    monkeypatch.setattr(gateway.httpx,'AsyncClient',Fake)
    response=client.post('/api/people-preview/'+path,json={})
    assert response.status_code==code
    assert phrase in response.json()['detail']
    assert 'Restart FamilyFoto Server' not in response.json()['detail']


@pytest.mark.parametrize('identity',['a'*32,'wrong'])
@pytest.mark.parametrize('path',['review-actions/stream','jobs/completed-imports/recheck/stream','auto-organize/stream'])
def test_bulk_progress_is_forwarded_without_buffering_or_reexecution(client,monkeypatch,identity,path):
    from contextlib import asynccontextmanager
    import json
    calls=[]
    class Response:
        headers={'x-familyfoto-pilot':identity}
        is_success=True
        async def aiter_bytes(self):
            yield b'{"state":"running","phase":"waiting"}\n'
            yield b'{"state":"completed","result":{"matched":72}}\n'
    class Fake:
        def __init__(self,**kwargs):assert kwargs['timeout']==30
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        @asynccontextmanager
        async def stream(self,method,url,**kwargs):
            calls.append((method,url,kwargs));yield Response()
    monkeypatch.setattr(gateway.httpx,'AsyncClient',Fake)
    response=client.post('/api/people-preview/'+path,content='x'*(70000 if path=='review-actions/stream' else 20))
    messages=[json.loads(line) for line in response.text.splitlines()]
    assert len(calls)==1 and calls[0][1].endswith('/api/automatic-people/'+path)
    assert response.headers['content-type'].startswith('application/x-ndjson')
    assert messages[-1]['state']==('completed' if identity=='a'*32 else 'failed')
    if identity=='a'*32:assert messages[0]['phase']=='waiting'
