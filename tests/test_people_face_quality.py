import pytest
from PIL import Image, ImageDraw, ImageFilter
from psycopg.types.json import Jsonb
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_automatic_people import automatic, pool, env, observations, add_face
from test_unexamined_photos import disposable_migrations
from test_automatic_people_handoff import completed
from app import database
from face_detect.recognition import people
from face_detect.recognition import automatic_people as auto
from face_detect.recognition import people_face_quality as quality


def test_quality_uses_pixels_without_mutating_image():
    image = Image.new('RGB', (96, 96), 'gray')
    draw = ImageDraw.Draw(image)
    for x in range(8, 90, 12):draw.rectangle((x, 15, x + 4, 80), fill='black')
    box = dict(x_min=0, y_min=0, x_max=96, y_max=96)
    before = image.tobytes()
    assert quality.assess(image, box)['reason'] is None
    blurred = quality.assess(image.filter(ImageFilter.GaussianBlur(8)), box)
    assert blurred['reason'] == 'blurry' and blurred['sharpness'] < quality.MIN_SHARPNESS
    assert quality.assess(image, dict(x_min=0, y_min=0, x_max=20, y_max=20))['reason'] == 'too-small'
    assert image.tobytes() == before
    assert quality.assess(image.resize((960, 960)),dict(x_min=0,y_min=0,x_max=960,y_max=960))['sample_size'] == [96,96]


def test_stricter_size_and_darkness_policy():
    import numpy as np
    box = dict(x_min=0, y_min=0, x_max=96, y_max=96)
    checker = np.indices((96, 96)).sum(axis=0) % 2
    dark = Image.fromarray((checker * 30 + 5).astype('uint8'))
    result = quality.assess(dark, box)
    assert result['policy'] == 'face-crop-quality-v2'
    assert result['sharpness'] > quality.MIN_SHARPNESS
    assert result['reason'] == 'too-dark'
    assert result['mean_luminance'] == 20 and result['p90_luminance'] == 35
    readable = Image.fromarray((checker * 100 + 25).astype('uint8'))
    assert quality.assess(readable, box)['reason'] is None
    for edge in (24, 31, 32):
        result = quality.assess(readable, dict(x_min=0,y_min=0,x_max=edge,y_max=edge))
        assert result['reason'] == ('too-small' if edge < 32 else None)


def test_sharpness_threshold_and_darkness_require_both_conditions():
    import numpy as np
    box = dict(x_min=0,y_min=0,x_max=96,y_max=96)
    checker = np.indices((96,96)).sum(axis=0) % 2
    soft = Image.fromarray((checker + 100).astype('uint8'))
    result = quality.assess(soft,box)
    assert 8 < result['sharpness'] < 20 and result['reason'] == 'blurry'
    # A low mean alone must not reject a crop with brighter detail.
    brighter = np.zeros((96,96),dtype='uint8');brighter[:,::4] = 100
    result = quality.assess(Image.fromarray(brighter),box)
    assert result['mean_luminance'] < 35 and result['p90_luminance'] >= 60
    assert result['reason'] is None
    boundary = Image.fromarray((checker * 20 + 25).astype('uint8'))
    result = quality.assess(boundary,box)
    assert result['mean_luminance'] == 35 and result['reason'] is None


@pytest.fixture(autouse=True)
def isolated_people_lock(monkeypatch):
    import uuid
    monkeypatch.setattr(people, 'CORRECTION_LOCK', uuid.uuid4().int % (2**63 - 1))


def reject(db, did, save=True):
    meta = db.execute('SELECT a.id,a.media_id,m.sha256 FROM face_detections d JOIN face_analyses a ON a.id=d.analysis_id JOIN media m ON m.id=a.media_id WHERE d.id=%s', (did,)).fetchone()
    db.execute("INSERT INTO auto_people_face_quality(detection_id,policy,metrics,state) VALUES(%s,%s,%s,'blurry')", (did, quality.POLICY, Jsonb({'reason':'blurry','sharpness':2})))
    if save:
        db.execute("INSERT INTO detector(detector_id,manifest) VALUES('synthetic-auto','{}') ON CONFLICT DO NOTHING")
        db.execute("UPDATE face_analyses SET detector_id='synthetic-auto' WHERE id=%s", (meta['id'],))
        db.execute('INSERT INTO auto_people_scan_results VALUES(%s,%s)', (meta['id'], Jsonb([{
            'input':{'id':did,'media_id':meta['media_id'],'content_hash':meta['sha256']},
            'state':'done','vector':[1.,0.],'result':{'vector':[1.,0.],'model_id':'synthetic-auto'}}])))
    return meta


def test_blurry_excluded_from_receipts_grouping_recheck_and_catalog(automatic):
    with database.connection() as db:
        did = automatic[0]
        reject(db, did, save=False)
        result = auto.ingest(db, 'quality-test', 'synthetic-auto', observations(automatic))
        assert result['excluded'] == 1
        assert not db.execute('SELECT 1 FROM embedding WHERE detection_id=%s', (did,)).fetchone()
        assert not db.execute('SELECT 1 FROM auto_people_members WHERE detection_id=%s', (did,)).fetchone()
        assert not db.execute('SELECT 1 FROM auto_people_observations WHERE detection_id=%s', (did,)).fetchone()
        assert all(r['id'] != did for r in auto.cohort(db,'synthetic-auto'))
        assert all(f['detection_id'] != did for g in auto.catalog(db)['groups'] for f in g['faces'])
        assert quality.collection(db)['items'][0]['detection_id'] == did
        assert auto.ignored_collection(db)['total'] == 0


def test_release_is_local_idempotent_and_not_blocked_by_scan_revisions(automatic):
    with database.connection() as db:
        did = automatic[0]
        reject(db, did)
        auto.event(db,'receipt','automatic','worker',{})
        result = quality.release(db,did,'admin')
        assert result['released']
        assert quality.release(db,did,'admin') == {'released':False}
        group = auto.detail(db,result['group_id'])['group']
        assert group['section'] == 'review' and group['person_id'] is None
        assert [f['detection_id'] for f in group['faces']] == [did]
        assert quality.collection(db)['total'] == 0
        assert db.execute('SELECT seed FROM auto_people_observations WHERE detection_id=%s',(did,)).fetchone()['seed'] is False
        auto.set_aside_groups(db,[group['id']],'admin')
        assert auto.detail(db,group['id'])['group']['section'] == 'other'


def test_paging_hidden_media_and_no_evidence_leak(automatic):
    with database.connection() as db:
        ids=[add_face(db,30000+i)[0] for i in range(103)]
        for did in ids:reject(db,did,save=False)
        first=quality.collection(db)
        assert first['total']==103 and len(first['items'])==100 and first['pages']==2
        assert len(quality.collection(db,2)['items'])==3
        assert quality.collection(db,100)['page']==2
        assert 'vector' not in str(first) and 'content_hash' not in str(first)
        mid=db.execute('SELECT a.media_id FROM face_detections d JOIN face_analyses a ON a.id=d.analysis_id WHERE d.id=%s',(ids[0],)).fetchone()['media_id']
        db.execute('INSERT INTO media_trash(media_id) VALUES(%s)',(mid,))
        assert quality.collection(db)['total']==102
        with pytest.raises(LookupError):quality.release(db,ids[0],'admin')


def test_routes_require_authentication_and_admin_to_release(automatic):
    from face_detect.api.automatic_people_api import router
    with database.connection() as db:reject(db,automatic[0])
    app=FastAPI()
    @app.middleware('http')
    async def identity(request,call_next):
        role=request.headers.get('x-role','admin')
        request.state.authenticated=role!='none'
        request.state.user={'username':'test','role':role}
        return await call_next(request)
    app.include_router(router)
    with TestClient(app) as client:
        url='/api/automatic-people/blurry'
        assert client.get(url,headers={'x-role':'none'}).status_code==401
        assert client.get(url,headers={'x-role':'regular'}).status_code==200
        assert client.get(url+'?page=0').status_code==422
        action=f'{url}/{automatic[0]}/review'
        assert client.post(action,headers={'x-role':'regular'},json={}).status_code==403
        assert client.post(action,json={}).json()['released'] is True
        assert client.post(action,json={}).json()['released'] is False


@pytest.mark.parametrize('filter_new',[False,True])
def test_raw_scan_filters_only_new_detections_and_replay_preserves_old(completed,tmp_path,monkeypatch,filter_new):
    import hashlib
    from face_detect.jobs.automatic_people_scan import RawJobs
    from face_detect.jobs import automatic_people_jobs as workers
    from test_automatic_people_handoff import ADMIN
    _,_,observer,provider,_,_=completed
    monkeypatch.setattr(workers,'foreground_busy',lambda db:False)
    source=tmp_path/'media';source.mkdir()
    path=source/'soft.png';Image.new('RGB',(100,100),'gray').save(path)
    original=path.read_bytes()
    with database.connection() as db:
        mid=db.execute("INSERT INTO media(sha256,relative_path,name,kind,size) VALUES(%s,'soft.png','soft.png','image',%s) RETURNING id",(hashlib.sha256(original).hexdigest(),len(original))).fetchone()['id']
    jobs=RawJobs(tmp_path/'jobs',source,provider,observer,lambda r:None,quality_filter=quality.assess if filter_new else None)
    jobs.create('soft',[mid],ADMIN)
    with database.connection() as lease:
        result=jobs.run('soft',ADMIN,lease=lease)
    assert result['state']=='completed' and result['receipt']['accepted']==(0 if filter_new else 1)
    with database.connection() as db:
        did=db.execute('SELECT d.id FROM face_detections d JOIN face_analyses a ON a.id=d.analysis_id WHERE a.media_id=%s',(mid,)).fetchone()['id']
        if filter_new:
            assert not auto.rows(db,[did])
            quality.release(db,did,'admin')
        else:assert quality.collection(db)['total']==0
        original_group=auto.detail(db,db.execute('SELECT group_id FROM auto_people_members WHERE detection_id=%s',(did,)).fetchone()['group_id'])['group']
    assert (tmp_path/'thumbnails'/f'{did}.jpg').exists() and path.read_bytes()==original
    jobs.quality_filter=lambda *args:pytest.fail('Cached detections must never be reclassified')
    provider._transport=lambda *args:pytest.fail('Cached detections must not infer again')
    jobs.create('replay',[mid],ADMIN)
    with database.connection() as lease:assert jobs.run('replay',ADMIN,lease=lease)['state']=='completed'
    with database.connection() as db:
        assert quality.collection(db)['total']==0
        assert auto.detail(db,original_group['id'])['group']==original_group
