import hashlib
import os
import time
import uuid
from pathlib import Path
import pytest
from PIL import Image
from face_detect.jobs.automatic_people_scan import decode_canonical
from face_detect.jobs.automatic_people_scan import DecodeRejected
from face_detect.jobs.automatic_people_scan import RawJobs
from face_detect.testing.provider import FixtureProvider
from face_detect.configuration import PREPROCESSING
from test_automatic_people_handoff import completed,automatic,pool,env,ADMIN
from test_unexamined_photos import disposable_migrations
from app import database
from face_detect.jobs import automatic_people_jobs as workers


def test_canonical_orientation_and_separate_model_space(tmp_path):
    path=tmp_path/'oriented.jpg';exif=Image.Exif();exif[274]=6
    Image.new('RGB',(80,40),'red').save(path,exif=exif)
    before=path.read_bytes();image,size=decode_canonical(path)
    assert image.size==size==(40,80) and path.read_bytes()==before
    assert FixtureProvider(preprocessing='crop').identity!=FixtureProvider(preprocessing='raw').identity


def test_existing_heic_decoder_preserves_original_and_display_geometry(tmp_path):
    from pillow_heif import from_pillow
    path=tmp_path/'oriented.heic';image=Image.new('RGB',(80,40),'green')
    from_pillow(image).save(path,quality=80)
    before=path.read_bytes();decoded,size=decode_canonical(path)
    assert decoded.mode=='RGB' and decoded.size==size==(80,40)
    assert path.read_bytes()==before


@pytest.mark.parametrize('kind,code',[('GIF','unsupported_media'),('corrupt','corrupt_or_unsafe_media')])
def test_clear_decode_error_codes(tmp_path,kind,code):
    path=tmp_path/'input'
    if kind=='GIF':Image.new('RGB',(10,10)).save(path,format='GIF')
    else:path.write_bytes(b'not image data')
    with pytest.raises(DecodeRejected,match=code):decode_canonical(path)


def test_untransformable_color_profile_is_rejected_per_photo(tmp_path,monkeypatch):
    from PIL import ImageCms
    path=tmp_path/'profiled.tif'
    Image.new('RGB',(20,10),'blue').save(path,icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes())
    def fail(*args,**kwargs):raise ImageCms.PyCMSError('cannot build transform')
    monkeypatch.setattr(ImageCms,'profileToProfile',fail)
    before=path.read_bytes()
    with pytest.raises(DecodeRejected,match='corrupt_or_unsafe_media') as rejected:decode_canonical(path)
    assert rejected.value.detail == 'The embedded color profile could not be converted to sRGB for face detection.'
    assert path.read_bytes()==before


def test_bad_items_do_not_block_good_raw_input(completed,tmp_path,monkeypatch):
    _,_,observer,old,_,_=completed
    provider=FixtureProvider(old._transport,preprocessing=PREPROCESSING)
    observer=provider.observe
    source=tmp_path/'media';source.mkdir();ids=[]
    for name,kind in [('valid.png','PNG'),('unsupported.gif','GIF'),('corrupt.jpg','bad')]:
        path=source/name
        if kind=='bad':path.write_bytes(b'bad fixture')
        else:Image.new('RGB',(100,100),'green').save(path,format=kind)
        sha=hashlib.sha256(path.read_bytes()).hexdigest()
        with database.connection() as db:
            ids.append(db.execute("INSERT INTO media(sha256,relative_path,name,kind,size,captured_at) VALUES(%s,%s,%s,'image',%s,'2026-01-01') RETURNING id",(sha,name,name,path.stat().st_size)).fetchone()['id'])
    monkeypatch.setattr(workers,'COMPUTE_LOCK',uuid.uuid4().int%(2**63-1))
    monkeypatch.setattr(workers,'MAINTENANCE_LOCKS',());monkeypatch.setattr(workers,'CHILD_LOCKS',())
    from app.jobs.foreground import MARKER
    MARKER.touch();os.utime(MARKER,(time.time()-10,time.time()-10))
    from people_catalog_fixture import step_aside_active_space;step_aside_active_space()
    jobs=RawJobs(tmp_path/'jobs',source,provider,observer,lambda r:None,decoder=decode_canonical)
    jobs.create('decode',ids,ADMIN);status=jobs.run('decode',ADMIN,max_items=3)
    assert status['state']=='completed' and status['receipt']['accepted']==1
    assert {r['error'] for r in status['errors']}=={'unsupported_media','corrupt_or_unsafe_media'}
    with database.connection() as db:
        rows=db.execute('SELECT d.id,d.review_state FROM face_detections d JOIN face_analyses a ON a.id=d.analysis_id WHERE a.media_id=ANY(%s)',(ids,)).fetchall()
        assert len(rows)==1 and rows[0]['review_state']=='unreviewed'
    assert (tmp_path/'thumbnails'/f"{rows[0]['id']}.jpg").exists()
    # A second job replays compact evidence without asking the detector again.
    monkeypatch.setattr(provider, 'detect', lambda image: pytest.fail('Cached photo was detected again'))
    jobs.create('decode-cached', [ids[0]], ADMIN)
    assert jobs.run('decode-cached', ADMIN)['state'] == 'completed'


@pytest.mark.parametrize('face_count',[0,11])
def test_raw_image_face_count_bound(completed,tmp_path,monkeypatch,face_count):
    _,_,observer,old,_,_=completed
    provider=FixtureProvider(old._transport if face_count else lambda image:[],preprocessing=PREPROCESSING)
    observer=provider.observe
    if face_count:
        detect=provider.detect
        monkeypatch.setattr(provider,'detect',lambda image:detect(image)*face_count)
    path=tmp_path/'blank.png';Image.new('RGB',(100,100),'white').save(path)
    with database.connection() as db:
        mid=db.execute("INSERT INTO media(sha256,relative_path,name,kind,size) VALUES(%s,'blank.png','blank.png','image',%s) RETURNING id",(hashlib.sha256(path.read_bytes()).hexdigest(),path.stat().st_size)).fetchone()['id']
    monkeypatch.setattr(workers,'COMPUTE_LOCK',uuid.uuid4().int%(2**63-1))
    monkeypatch.setattr(workers,'MAINTENANCE_LOCKS',());monkeypatch.setattr(workers,'CHILD_LOCKS',())
    from app.jobs.foreground import MARKER
    MARKER.touch();os.utime(MARKER,(time.time()-10,time.time()-10))
    from people_catalog_fixture import step_aside_active_space;step_aside_active_space()
    jobs=RawJobs(tmp_path/'jobs',tmp_path,provider,observer,lambda r:None,decoder=decode_canonical)
    jobs.create('blank',[mid],ADMIN);assert jobs.run('blank',ADMIN)['state']=='completed'
    with database.connection() as db:
        assert db.execute('SELECT 1 FROM face_analyses WHERE media_id=%s',(mid,)).fetchone()
        assert db.execute('SELECT count(*) n FROM face_detections d JOIN face_analyses a ON a.id=d.analysis_id WHERE a.media_id=%s',(mid,)).fetchone()['n']==face_count
        if not face_count:assert not db.execute('SELECT 1 FROM auto_people_members').fetchone()


def test_native_raw_pipeline_and_review_only_groups(automatic,tmp_path,monkeypatch):
    from face_detect.service import FaceService
    from face_detect.configuration import PREPROCESSING
    class Backend:
        def analyze(self,image,include_embeddings=True):
            return [{'box':dict(x_min=10,y_min=10,x_max=90,y_max=90,probability=.99),
                     'landmarks':[[25,30],[75,30],[50,50],[30,70],[70,70]],'vector':[1.]+[0.]*511}]
        def close(self):pass
    provider=FaceService({'provider':'insightface','device':'cpu'},
        {'schema':'familyfoto-native-model-v1','dimensions':512,'assets':{},'matching':{'mode':'review-only'}},{},Backend())
    # No asset checksums here, so no computed space; name one for this synthetic model.
    provider.space_id,provider.detector_id='native-test-space','native-test-detector'
    monkeypatch.setattr(workers,'COMPUTE_LOCK',uuid.uuid4().int%(2**63-1))
    monkeypatch.setattr(workers,'MAINTENANCE_LOCKS',());monkeypatch.setattr(workers,'CHILD_LOCKS',())
    ids=[]
    for n,color in enumerate(('red','blue')):
        path=tmp_path/f'native-{n}.png';Image.new('RGB',(100,100),color).save(path)
        with database.connection() as db:
            ids.append(db.execute("INSERT INTO media(sha256,relative_path,name,kind,size,captured_at) VALUES(%s,%s,%s,'image',%s,%s) RETURNING id",(hashlib.sha256(path.read_bytes()).hexdigest(),path.name,path.name,path.stat().st_size,f'2026-01-0{n+1}')).fetchone()['id'])
    from people_catalog_fixture import step_aside_active_space;step_aside_active_space()
    jobs=RawJobs(tmp_path/'jobs',tmp_path,provider,provider.observe,provider.verify,decoder=decode_canonical)
    admin={'username':'native-test','role':'admin'}
    jobs.create('native',ids,admin)
    result=jobs.run('native',admin,max_items=2)
    assert result['state']=='completed' and result['receipt']['accepted']==2
    with database.connection() as db:
        assert db.execute('SELECT count(DISTINCT group_id) n FROM auto_people_members WHERE space_id=%s',(provider.space_id,)).fetchone()['n']==2
        before=db.execute('SELECT count(*) n FROM auto_people_receipts').fetchone()['n']
    jobs.run('native',admin,max_items=2)
    with database.connection() as db:
        assert db.execute('SELECT count(*) n FROM auto_people_receipts').fetchone()['n']==before
