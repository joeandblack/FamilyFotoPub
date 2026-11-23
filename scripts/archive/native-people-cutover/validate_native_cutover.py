"""Exercise preserved native browsing and real inference on the isolated clone."""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import sys
from contextlib import asynccontextmanager
import time
import uuid

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT));sys.dont_write_bytecode=True
folder=Path(sys.argv[1]).resolve()
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # prepare_people_migration stays in scripts/
from prepare_people_migration import save
settings=json.loads((folder.parent/'validation-connection.json').read_text())
runtime=json.loads((folder/'native-runtime.json').read_text())
if (settings['host'],settings['port'],settings['dbname'])!=('127.0.0.1',55449,'familyfoto_validation'):
    raise ValueError('Validation database only')
import psycopg
from psycopg import sql
with psycopg.connect(**settings) as db:
    if Path(db.execute('SHOW data_directory').fetchone()[0]).resolve()!=folder.parent/'validation-postgres':
        raise ValueError('Unexpected database cluster')
    db.execute('UPDATE album_collection_people SET pilot=%s WHERE pilot=%s',
               (runtime['model_id'][:32],runtime['legacy_pilot']))
from app import config
config.DATA=folder/'validation-data';config.LOGS=config.DATA/'logs';config.TEMP=config.DATA/'temp'
cfg=json.loads((ROOT/'config/local.json').read_text(encoding='utf-8'))
cfg.update(database=settings,live_existing_catalog=True,live_native_catalog=True,
           preview={'data_root':str(config.DATA)})
config.load_config=lambda:copy.deepcopy(cfg)
config.initialize_runtime=lambda:None
profile=config.DATA/'face_detect/profiles'/runtime['model_id'][:32]
profile.mkdir(parents=True,exist_ok=True)
save(profile/'runtime.json',runtime)
save(config.DATA/'people-preview.json',{'enabled':True,'backend':'native','pilot':profile.name,'port':54341})
legacy=Path(json.loads((folder/'migration.json').read_text())['source_profile'])
for parent in (legacy/'jobs').glob('import-*/import.json'):
    out=profile/'jobs'/parent.parent.name;out.mkdir(parents=True,exist_ok=True)
    shutil.copy2(parent,out/'import.json');save(out/'control.json',{'action':'pause'})
from app.service.readonly_preview import MemorySessions, install_write_guard
python=runtime['profile']['python']
install_write_guard(folder.parent,allowed_subprocesses=(
    (python,'-m','face_detect.providers.runner','versions','insightface'),
    (python,'-m','face_detect.providers.runner','analyze'),
))
from app import auth, api, database
from app.auth import api as auth_api
auth.SessionStore=MemorySessions;auth_api.SessionStore=MemorySessions
@asynccontextmanager
async def lifespan(app):yield
api.app.router.lifespan_context=lifespan
from face_detect.api.readonly_people import install
install(api.app,cfg,ROOT)
from fastapi.testclient import TestClient
checks=[]
with TestClient(api.app) as client:
    client.cookies.set('familyfoto_session',auth.session_token(cfg,int(time.time())+300))
    assert client.get('/api/me').json()['capabilities']['people_preview']
    assert 'existing-people-catalog.mjs' not in client.get('/people-preview').text
    cards=client.get('/api/people-preview/cards');assert cards.status_code==200,cards.text[:200]
    group=client.get('/api/people-preview/groups/'+str(cards.json()['groups'][0]['id']))
    assert group.status_code==200,group.text[:200]
    did=group.json()['group']['faces'][0]['detection_id']
    (profile/'thumbnails').mkdir(exist_ok=True)
    shutil.copy2(folder/'thumbnails'/f'{did}.jpg',profile/'thumbnails'/f'{did}.jpg')
    assert client.get(f'/api/people-preview/thumbnails/{did}').status_code==200
    assert client.get('/api/people-preview/review-page?page_size=100').status_code==200
    assert client.get('/api/people-preview/import-status').status_code==200
    assert client.get('/api/face-service').json()['start_with_app'] is False
    with psycopg.connect(**settings) as db:
        linked=db.execute('SELECT DISTINCT collection_id FROM album_collection_people WHERE pilot=%s',(profile.name,)).fetchall()
    for (cid,) in linked:
        linked_response=client.get(f'/api/album-collections/{cid}/people')
        assert linked_response.status_code==200,linked_response.text[:200]
        assert linked_response.json().get('warning') is None,linked_response.text[:200]
        assert any(person['available'] for person in linked_response.json()['people'])
    checks.append('Preserved People navigation, groups, thumbnails, review paging and import status')
    checks.append('Existing collection links resolve to the same preserved person IDs')

from face_detect.jobs import automatic_people_local as local
jobs=local.runtime(profile)
from face_detect.recognition.people_catalog import ImportCatalog
catalog=ImportCatalog(cfg,database.connection)
with database.connection() as db:
    before=db.execute('SELECT count(*) n FROM face_detections').fetchone()['n']
    existing=db.execute('''SELECT m.id,m.sha256 FROM media m JOIN native_preserved_photos p ON p.media_id=m.id
        JOIN public.media live ON live.id=m.id AND live.sha256=m.sha256
        WHERE NOT EXISTS(SELECT 1 FROM public.media_trash t WHERE t.media_id=m.id)
        AND NOT EXISTS(SELECT 1 FROM public.face_analyses a JOIN public.face_detections d ON d.analysis_id=a.id
            WHERE a.media_id=m.id AND (d.ignored OR d.review_state='rejected')) LIMIT 1''').fetchone()
    assert jobs.raw.scan(db,{'media_id':existing['id'],'content_hash':existing['sha256']},None)==[]
    assert db.execute('SELECT count(*) n FROM face_detections').fetchone()['n']==before
checks.append('Already migrated photo skipped without redetection or duplicate face IDs')
with database.connection(cfg['database']) as db:
    row=db.execute(sql.SQL('''SELECT m.id,m.first_import_job_id jid FROM media m JOIN jobs j ON j.id=m.first_import_job_id
        WHERE m.kind='image' AND j.state='completed-review-required' AND lower(m.name) LIKE '%%.jpg'
        AND NOT EXISTS(SELECT 1 FROM media_trash t WHERE t.media_id=m.id)
        AND NOT EXISTS(SELECT 1 FROM face_preferences p WHERE p.media_id=m.id AND p.opted_out)
        AND NOT EXISTS(SELECT 1 FROM {}.face_analyses a WHERE a.media_id=m.id)
        ORDER BY m.id LIMIT 1''').format(sql.Identifier(runtime['schema']))).fetchone()
ids=catalog.resolve('import:'+str(row['jid']));assert row['id'] in ids
user={'username':'native-cutover-validation','role':'admin'}
name='native-cutover-'+uuid.uuid4().hex[:12]
jobs.raw.create(name,[row['id']],user)
result=jobs.raw.run(name,user,max_items=1)
assert result['state']=='completed',result
assert not result.get('errors'),result
with database.connection() as db:
    assert db.execute('SELECT 1 FROM face_analyses WHERE media_id=%s AND model_id=%s',(row['id'],runtime['model_id'])).fetchone()
checks.append('New photo scanned with native model and committed through the receipt workflow on clone')
with (folder/'native-catalog.dump').open('rb') as stream:archive_sha256=hashlib.file_digest(stream,'sha256').hexdigest()
save(folder/'native-live-readiness.json',{'complete':True,'checks':checks,'sample_media_id':row['id'],'archive_sha256':archive_sha256,
                                       'native_model':runtime['model_id'],'catalog_activated':False})
print(json.dumps({'complete':True,'checks':checks}))
