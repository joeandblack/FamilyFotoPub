import io
import pytest
from PIL import Image
from test_photo_duplicates import jpeg, seed, configure_worker
from test_uploads import env
from app import database
from app.photos import fingerprint as fp
from app.photos import duplicates as photos
from app.photos.duplicate_trash import preferred_copy, apply_group


def mpo(secondary='blue'):
    data=io.BytesIO()
    Image.new('RGB',(24,16),'red').save(data,'MPO',save_all=True,
        append_images=[Image.new('RGB',(24,16),secondary)])
    return data.getvalue()


@pytest.mark.parametrize('pixels',[40_000_001,60_000_000,60_000_001])
def test_normal_scan_large_image_limits_before_decode(monkeypatch,pixels):
    class DecodeReached(Exception):pass
    class Header:
        format='JPEG'
        width=pixels
        height=1
        n_frames=1
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def getexif(self):raise DecodeReached()
    monkeypatch.setattr(fp.Image,'open',lambda *_:Header())
    monkeypatch.setattr(fp,'available_memory',lambda:8*1024**3)
    if pixels<=60_000_000:
        with pytest.raises(DecodeReached):fp.fingerprint(b'header')
        monkeypatch.setattr(fp,'available_memory',lambda:1)
        with pytest.raises(ValueError,match='memory'):fp.fingerprint(b'header')
    else:
        with pytest.raises(ValueError,match='60 megapixels'):fp.fingerprint(b'header')


def test_mpo_primary_matches_jpeg_but_never_auto_trashes():
    data=mpo()
    with pytest.raises(ValueError,match='Only JPEG'):fp.fingerprint(data)
    result=fp.fingerprint(data,review=True)
    plain=io.BytesIO();Image.new('RGB',(24,16),'red').save(plain,'JPEG')
    assert result['pixel_hash']==fp.fingerprint(plain.getvalue())['pixel_hash']
    changed=fp.fingerprint(mpo('green'),review=True)
    assert changed['pixel_hash']==result['pixel_hash']
    assert changed['image_hash']!=result['image_hash']
    assert result['evidence']['frame_count']==2
    assert result['evidence']['additional_frames_compared'] is False
    assert preferred_copy([{'evidence':result['evidence']}]) is None


def test_mpo_resource_guards_and_other_multiframe_rejection(monkeypatch):
    frames=io.BytesIO()
    Image.new('RGB',(24,16),'red').save(frames,'TIFF',save_all=True,
        append_images=[Image.new('RGB',(24,16),'blue')])
    with pytest.raises(ValueError,match='Multi-frame'):fp.fingerprint(frames.getvalue(),review=True)
    monkeypatch.setattr(fp,'REVIEW_MAX_PIXELS',100)
    with pytest.raises(ValueError,match='60 megapixels'):fp.fingerprint(mpo(),review=True)
    monkeypatch.setattr(fp,'REVIEW_MAX_PIXELS',60_000_000)
    monkeypatch.setattr(fp,'available_memory',lambda:1)
    with pytest.raises(ValueError,match='memory'):fp.fingerprint(mpo(),review=True)


def test_mpo_warning_retry_preserves_original_and_requires_review(env,monkeypatch):
    config,_=configure_worker(env,monkeypatch)
    plain=io.BytesIO();Image.new('RGB',(24,16),'red').save(plain,'JPEG')
    first,_,_=seed(config,plain.getvalue(),'plain-primary')
    job,_,path=seed(config,mpo(),'mpo-review')
    original=path.read_bytes()
    photos.run(first);photos.run(job)
    with database.connection() as db:
        assert photos.counts(db,job)=={'warning':1}
        photos.prepare_unexamined(db,job)
        db.execute('UPDATE jobs SET auto_trash_duplicates=true WHERE id=%s',(job,))
    photos.run(job)
    with database.connection() as db:
        assert photos.counts(db,job)=={'done':1}
        group=photos.groups(db,job)['groups'][0]
        assert 'additional pictures were not compared' in group['method']
        assert apply_group(db,job,group['key'])==0
        assert db.execute('SELECT count(*) n FROM media_trash').fetchone()['n']==0
    assert path.read_bytes()==original


@pytest.fixture(autouse=True)
def disposable_migrations(monkeypatch):
    # Run schema SQL only inside the unique fixture schema. Production migration
    # reservations protect production tables, not these unrelated disposable ones.
    def migrate():
        with database.connection() as db:
            schema=db.execute('SELECT current_schema() name').fetchone()['name']
            assert schema.startswith('uploadtest_')
            present=db.execute("SELECT to_regclass('schema_versions') name").fetchone()['name']
            applied={r['version'] for r in db.execute('SELECT version FROM schema_versions')} if present else set()
            for path in sorted((database.ROOT/'databases').glob('*.sql')):
                if int(path.name.split('_')[0]) not in applied:
                    db.execute(path.read_text(encoding='utf-8'))
    monkeypatch.setattr(database,'migrate',migrate)


def test_invalid_orientation_is_review_only_and_matches_unrotated_pixels():
    with pytest.raises(ValueError,match='orientation'):fp.fingerprint(jpeg(orientation=0))
    result=fp.fingerprint(jpeg(orientation=0),review=True)
    assert result['pixel_hash']==fp.fingerprint(jpeg())['pixel_hash']
    assert result['evidence']['original_orientation']=='0'
    assert preferred_copy([{'evidence':result['evidence']}]) is None


def test_explicit_large_review_preserves_memory_guard_and_manual_review(monkeypatch):
    monkeypatch.setattr(fp,'REVIEW_MAX_PIXELS',100)
    with pytest.raises(ValueError,match='60 megapixels'):fp.fingerprint(jpeg(),review=True)
    result=fp.fingerprint(jpeg(),review=True,large_review=True)
    assert result['evidence']['large_image_review']
    assert result['evidence']['review_only']
    assert preferred_copy([result]) is None
    monkeypatch.setattr(fp,'available_memory',lambda:1)
    with pytest.raises(ValueError,match='memory'):fp.fingerprint(jpeg(),review=True,large_review=True)
    with pytest.raises(ValueError,match='requires manual review'):fp.fingerprint(jpeg(),large_review=True)


def test_large_warning_skip_is_durable_and_not_a_fingerprint(env,monkeypatch):
    from app.photos.duplicates_api import skip_large_images
    config,_=configure_worker(env,monkeypatch)
    job,mid,path=seed(config,jpeg(),'large-skip')
    original=path.read_bytes()
    with database.connection() as db:
        db.execute("INSERT INTO photo_duplicate_items(job_id,media_id,state,error) VALUES(%s,%s,'warning','Image exceeds manual review limit of 60 megapixels')",(job,mid))
    from fastapi import HTTPException
    with pytest.raises(HTTPException,match='only after'):skip_large_images(job)
    with database.connection() as db:photos.prepare_unexamined(db,job,large_review=True)
    monkeypatch.setattr(fp,'available_memory',lambda:1)
    photos.run(job)
    with database.connection() as db:
        assert db.execute('SELECT error FROM photo_duplicate_items WHERE job_id=%s AND media_id=%s',(job,mid)).fetchone()['error'].startswith('Large image scan failed: ')
    assert 'skipped' in skip_large_images(job)['message']
    photos.run(job)
    with database.connection() as db:
        assert photos.counts(db,job)=={'skipped':1}
        assert photos.all_counts(db)[job]=={'skipped':1}
        assert db.execute('SELECT count(*) n FROM photo_fingerprints').fetchone()['n']==0
    assert path.read_bytes()==original


def test_large_review_targets_only_large_warnings_and_survives_run(env,monkeypatch):
    config,_=configure_worker(env,monkeypatch)
    job,mid,path=seed(config,jpeg(),'large-review')
    original=path.read_bytes()
    with database.connection() as db:
        db.execute("INSERT INTO photo_duplicate_items(job_id,media_id,state,error) VALUES(%s,%s,'warning','Image exceeds manual review limit of 60 megapixels')",(job,mid))
        photos.prepare_unexamined(db,job,large_review=True)
    photos.run(job)
    with database.connection() as db:
        assert photos.counts(db,job)=={'done':1}
        evidence=db.execute('SELECT evidence FROM photo_fingerprints WHERE media_id=%s',(mid,)).fetchone()['evidence']
        assert evidence['large_image_review'] and evidence['review_only']
        assert db.execute('SELECT duplicates_status FROM jobs WHERE id=%s',(job,)).fetchone()['duplicates_status']['large_review_ids']==[mid]
    assert path.read_bytes()==original


def test_actual_format_and_resource_guards(monkeypatch):
    data=io.BytesIO();Image.new('RGB',(24,16),'red').save(data,'PNG')
    with pytest.raises(ValueError,match='Only JPEG'):fp.fingerprint(data.getvalue())
    assert fp.fingerprint(data.getvalue(),review=True)['evidence']['actual_format']=='PNG'
    monkeypatch.setattr(fp,'MAX_PIXELS',100)
    with pytest.raises(ValueError,match='60 megapixels'):fp.fingerprint(jpeg())
    assert fp.fingerprint(jpeg(),review=True)['pixel_hash']
    monkeypatch.setattr(fp,'REVIEW_MAX_PIXELS',100)
    with pytest.raises(ValueError,match='60 megapixels'):fp.fingerprint(jpeg(),review=True)
    monkeypatch.setattr(fp,'REVIEW_MAX_PIXELS',60_000_000)
    monkeypatch.setattr(fp,'available_memory',lambda:1)
    with pytest.raises(ValueError,match='memory'):fp.fingerprint(jpeg(),review=True)


def test_warning_retry_pause_resume_and_no_automatic_trash(env,monkeypatch):
    config,data=configure_worker(env,monkeypatch)
    first,keeper,_=seed(config,jpeg(),'normal')
    job,mid,path=seed(config,jpeg(orientation=0),'orientation-zero')
    before=path.read_bytes()
    photos.run(first);photos.run(job)
    with database.connection() as db:
        assert photos.counts(db,job)=={'warning':1}
        photos.prepare_unexamined(db,job)
        db.execute('UPDATE jobs SET auto_trash_duplicates=true WHERE id=%s',(job,))
    (data/'PHOTO-DUPLICATE-PAUSE').touch();photos.run(job)
    with database.connection() as db:
        assert photos.counts(db,job)=={'pending':1}
        assert mid in db.execute('SELECT duplicates_status FROM jobs WHERE id=%s',(job,)).fetchone()['duplicates_status']['unexamined_ids']
    (data/'PHOTO-DUPLICATE-PAUSE').unlink();photos.run(job)
    with database.connection() as db:
        assert photos.counts(db,job)=={'done':1}
        group=photos.groups(db,job)['groups'][0]
        assert 'Manual review required' in group['method']
        assert apply_group(db,job,group['key'])==0
        assert db.execute('SELECT count(*) n FROM media_trash').fetchone()['n']==0
        with pytest.raises(RuntimeError,match='No unexamined'):photos.prepare_unexamined(db,job)
    monkeypatch.setattr(photos,'fingerprint',lambda *a,**kw:pytest.fail('Completed checkpoint must not decode again'))
    photos.run(job)
    assert path.read_bytes()==before


def test_endpoint_launch_and_busy_reservation(env,monkeypatch):
    from app import jobs as pipeline
    client,_,_=env
    config,_=configure_worker(env,monkeypatch)
    job,mid,_=seed(config,jpeg(orientation=0),'blocked-warning')
    photos.run(job)
    with database.connection() as held:
        held.execute('SELECT pg_advisory_lock(%s)',(pipeline.WORKER_LOCK,))
        response=client.post(f'/api/import/{job}/photo-duplicates/unexamined')
        assert response.status_code==409
    with database.connection() as db:assert photos.counts(db,job)=={'warning':1}
    calls=[]
    monkeypatch.setattr(pipeline,'launch',lambda *args,**kwargs:calls.append((args,kwargs)) or {'message':'requested'})
    assert client.post(f'/api/import/{job}/photo-duplicates/unexamined').status_code==200
    assert calls==[((job,'duplicates'),{'unexamined':True})]


def test_pending_scan_cannot_be_requeued(env,monkeypatch):
    config,_=configure_worker(env,monkeypatch)
    job,_,_=seed(config,jpeg(),'pending-review')
    with database.connection() as db:
        with pytest.raises(RuntimeError,match='existing photo duplicate scan'):photos.prepare_unexamined(db,job)
