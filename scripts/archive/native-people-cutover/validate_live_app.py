"""Normal startup, authentication and writable routes on the restored clone."""
import copy
import json
from pathlib import Path
import secrets
import sys

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT))
sys.dont_write_bytecode=True
backup=Path(sys.argv[1]).resolve()
settings=json.loads((backup/'validation-connection.json').read_text())
if (settings['host'],settings['port'],settings['dbname'])!=('127.0.0.1',55449,'familyfoto_validation'):
    raise RuntimeError('Clone only')
import psycopg
with psycopg.connect(**settings) as db:
    if Path(db.execute('SHOW data_directory').fetchone()[0]).resolve()!=backup/'validation-postgres':
        raise RuntimeError('Unexpected cluster')
from app import config
config.DATA=backup/'installation/data'
config.LOGS=backup/'test-state/logs'
config.TEMP=backup/'test-state/temp'
password=secrets.token_urlsafe(24)
cfg={'database':settings,'destination':{'root':str(backup/'empty-media')},'source':{},
     'browser_user':'validation','auth_secret':secrets.token_urlsafe(40),
     'live_existing_catalog':True,'preview':{'data_root':str(config.DATA)}}
config.load_config=lambda:copy.deepcopy(cfg)
config.initialize_runtime=lambda:None
from app.auth import password_hash
cfg['browser_password_hash']=password_hash(password)
from app.service.readonly_preview import install_write_guard
install_write_guard(backup)
from app import api
from face_detect.api.readonly_people import install
from face_detect.api import service
service.PREFERENCES=backup/'test-state/nonexistent-face-service-settings.json'
assert service.preferences()=={'start_with_app':False}
install(api.app,cfg,ROOT)
from fastapi.testclient import TestClient
with TestClient(api.app) as client:
    assert client.post('/api/login',json={'username':'validation','password':password}).status_code==200
    assert client.get('/api/me').json()['capabilities']['people_preview'] is True
    assert client.get('/api/people-preview/cards').status_code==200
    assert client.get('/api/people-preview/import-status').status_code==200
    assert client.get('/api/face-service').json()['start_with_app'] is False
    made=client.post('/api/albums',json={'name':'Live startup test'})
    assert made.status_code==200,made.text[:200]
    assert client.delete('/api/albums/'+str(made.json()['id'])).status_code==200
    response=client.post('/api/media/9007199254740990/trash/trash',json={})
    assert response.status_code==404,response.text[:200]
    assert 'limited-edit-preview.mjs' not in client.get('/people-preview').text
    assert client.get('/api/face-service').json()['state']=='stopped'
(backup/'live-app-validation.json').write_text(json.dumps({'complete':True,'checks':[
    'normal lifespan and auth','existing People and import progress','writable album routes',
    'trash route unblocked with nonexistent test ID','face service remains stopped']}))
print('Normal writable app startup passed on clone; no face service started.')
