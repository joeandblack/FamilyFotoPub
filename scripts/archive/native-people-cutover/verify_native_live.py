"""Authenticated local go-live checks; starts the service but never a scan."""
import json
import os
from pathlib import Path
import ssl
import sys
import time
import urllib.request

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT));sys.dont_write_bytecode=True
folder=Path(sys.argv[1]).resolve()
cfg=json.loads((ROOT/'config/local.json').read_text(encoding='utf-8'))
if not cfg.get('live_native_catalog'):raise ValueError('Native deployment configuration required')
os.environ['FAMILYFOTO_DATA_DIR']=cfg['preview']['data_root']
from app.auth import session_token,revoke_session
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # prepare_people_migration stays in scripts/
from prepare_people_migration import save
token=session_token(cfg,int(time.time())+600)
host=cfg.get('host','127.0.0.1')
if host=='0.0.0.0':host='127.0.0.1'
base='https://'+host+':'+str(cfg['port'])
context=ssl._create_unverified_context()  # Local deployment's self-signed certificate.


def request(path,method='GET',body=None):
    req=urllib.request.Request(base+path,data=json.dumps(body).encode() if body is not None else None,
        method=method,headers={'Cookie':'familyfoto_session='+token,'Origin':base,'Content-Type':'application/json'})
    with urllib.request.urlopen(req,context=context,timeout=90) as response:
        payload=response.read()
        return json.loads(payload) if 'application/json' in response.headers.get('Content-Type','') else payload


try:
    assert request('/api/me')['capabilities']['people_preview']
    cards=request('/api/people-preview/cards')['groups'];assert cards
    group=request('/api/people-preview/groups/'+str(cards[0]['id']))['group']
    did=group['faces'][0]['detection_id']
    assert request(f'/api/people-preview/thumbnails/{did}').startswith(b'\xff\xd8')
    assert isinstance(request('/api/people-preview/review-page?page_size=100'),dict)
    assert isinstance(request('/api/people-preview/ignored'),dict)
    assert isinstance(request('/api/people-preview/blurry'),dict)
    capabilities=request('/api/people-preview/capabilities')
    status=request('/api/face-service');assert not status['start_with_app']
    request('/api/face-service/start','POST',{})
    for _ in range(90):
        status=request('/api/face-service')
        if status['state'] in ('ready','unavailable'):break
        time.sleep(1)
    assert status['state']=='ready',status
    assert not status['start_with_app']
    paused=request('/api/people-preview/jobs/import-1')
    assert paused['state']=='paused',paused
    report={'live':True,'named_cards':len(cards),'native_service':status['state'],
            'automatic_start':False,'import_1':paused['state'],'capabilities':capabilities,
            'saved_thumbnail_verified':True,'review_ignored_blurry_available':True}
    save(folder/'live-verification.json',report)
    activation=json.loads((folder/'activation.json').read_text())
    activation.update(server_started=True,native_service_ready=True)
    save(folder/'activation.json',activation)
    migration=json.loads((folder/'migration.json').read_text())
    migration.update(activated=True,stage='live')
    save(folder/'migration.json',migration)
    print(json.dumps(report))
finally:
    revoke_session(cfg,token)
