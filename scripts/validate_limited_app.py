"""Validate the exact limited-mode app/middleware on the isolated clone."""
import copy
import json
from pathlib import Path
import secrets
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.dont_write_bytecode=True
backup=Path(sys.argv[1]).resolve()
cfg=json.loads((backup/'limited-edit-test.json').read_text())
if (cfg['database']['host'],cfg['database']['port'],cfg['database']['dbname']) != ('127.0.0.1',55449,'familyfoto_validation'):
    raise RuntimeError('Clone only')
cfg['preview']['data_root']=str(backup/'installation/data')
cfg['destination']={'root':str(backup/'empty-media')}
cfg['browser_user']='validation'
cfg['auth_secret']=secrets.token_urlsafe(40)
from app import config
config.load_config=lambda:copy.deepcopy(cfg)
config.initialize_runtime=lambda:None
from app.service import readonly_preview
from app.service.readonly_preview import build_app,install_write_guard
cfg['preview']['writable_root']=str(backup/'test-state')
readonly_preview.scratch_root=lambda root:backup/'test-state'
from app.auth import password_hash,session_token
cfg['browser_password_hash']=password_hash(secrets.token_urlsafe(40))
install_write_guard(backup)
app=build_app(cfg)
from fastapi.testclient import TestClient
with TestClient(app) as client:
    assert client.get('/api/albums').status_code==401
    client.cookies.set('familyfoto_session',session_token(cfg,int(time.time())+3600))
    assert client.get('/api/albums').status_code==200
    cards=client.get('/api/people-preview/cards')
    assert cards.status_code==200,cards.text[:200]
    assert cards.headers['X-FamilyFoto-Mode']=='metadata-only'
    response=client.post('/api/albums',json={'name':'Limited app integration test'})
    assert response.status_code==200,response.text[:200]
    aid=response.json()['id']
    try:
        assert client.patch(f'/api/albums/{aid}',json={'name':'Limited app renamed'}).status_code==200
        assert client.get(f'/api/albums/{aid}').json()['name']=='Limited app renamed'
    finally:
        assert client.delete(f'/api/albums/{aid}').status_code==200
    for path in ['/api/import/start','/api/face-service/start','/api/media/1/trash/trash',
                 '/api/people-preview/auto-organize/stream','/api/people-preview/jobs',
                 '/api/people-preview/blurry/1/review']:
        assert client.post(path,json={}).status_code==403,path
    page=client.get('/people-preview')
    assert 'limited-edit-preview.mjs' in page.text
    assert 'read-only-preview.mjs' not in page.text
(backup/'limited-app-validation.json').write_text(json.dumps({'complete':True,
    'checks':['authentication required','legacy People read adapter','album create/edit/delete',
              'media/worker mutation HTTP guards','limited UI mode']}))
print('Limited-mode app, authentication and blocked-action checks passed on clone.')
