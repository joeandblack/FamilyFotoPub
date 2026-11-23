import hashlib
import io
import json
from pathlib import Path
import pytest
from PIL import Image
from app.photos.fingerprint import fingerprint, jpeg_image_data, evidence_text
from app import database
from app.photos import duplicates as photos
from test_uploads import env


def jpeg(color='red', orientation=1, date='2020:01:01 00:00:00', icc=None, camera=None):
    output=io.BytesIO()
    exif=Image.Exif();exif[274]=orientation;exif[306]=date
    if camera is not None:exif[272]=camera
    image=Image.new('RGB',(24,16),color)
    image.paste('blue',(0,0,8,8))
    image.save(output,format='JPEG',exif=exif,**({'icc_profile':icc} if icc else {}))
    return output.getvalue()


def test_metadata_only_matches_and_visual_changes_do_not():
    a=jpeg();b=jpeg(date='2025:02:03 11:12:13')
    assert hashlib.sha256(a).digest()!=hashlib.sha256(b).digest()
    first,second=fingerprint(a),fingerprint(b)
    assert first['pixel_hash']==second['pixel_hash']
    assert first['image_hash']==second['image_hash']
    assert first['evidence']['date']!=second['evidence']['date']
    for changed in (jpeg('green'),jpeg(orientation=6),jpeg(icc=b'different-profile')):
        assert fingerprint(changed)['pixel_hash']!=first['pixel_hash']
    # Embedded EXIF thumbnail/metadata content is not part of the image-data key.
    extra=b'Exif\0\0arbitrary-embedded-thumbnail'
    augmented=a[:2]+b'\xff\xe1'+(len(extra)+2).to_bytes(2,'big')+extra+a[2:]
    assert jpeg_image_data(augmented)==jpeg_image_data(a)


def test_bad_jpeg_and_limits():
    for data in (b'',b'not a photo',b'\xff\xd8\xff\xe1\x00\x50'):
        with pytest.raises((ValueError,OSError)):fingerprint(data)


def test_legacy_dot_files_excluded_even_with_valid_jpeg_bytes(env,monkeypatch):
    config,data=configure_worker(env,monkeypatch)
    job,mid,path=seed(config,jpeg(),'legacy-dot')
    with database.connection() as db:
        db.execute("UPDATE media SET name='._photo.JPG' WHERE id=%s",(mid,))
        db.execute("INSERT INTO photo_duplicate_items(job_id,media_id,state,error) VALUES(%s,%s,'warning','old decode warning')",(job,mid))
        assert photos.counts(db,job)=={}
        assert photos.all_counts(db).get(job,{})=={}
        result=photos.groups(db,job)
        assert result['warnings']==[] and result['total']==0
    monkeypatch.setattr(photos,'fingerprint',lambda data:pytest.fail('Dot files must not be decoded'))
    photos.run(job)
    assert path.exists()


def test_exif_null_padding_is_removed_only_from_display_evidence():
    clean=fingerprint(jpeg(camera='Canon PowerShot S50'))
    padded=fingerprint(jpeg(camera='Canon PowerShot S50\x00\x00',date='2020:01:01\x00 00:00:00'))
    assert padded['evidence']['camera']=='Canon PowerShot S50'
    assert padded['evidence']['date']=='2020:01:01 00:00:00'
    for key in ('version','pixel_hash','image_hash'):
        assert padded[key]==clean[key]
    assert padded['evidence']['exif_sha256']!=clean['evidence']['exif_sha256']
    assert evidence_text('A\x00B\ud800C',20)=='AB?C'
    assert evidence_text('camera',3)=='cam'


def test_null_padded_exif_checkpoint_saves_to_postgres(env,monkeypatch):
    config,data=configure_worker(env,monkeypatch)
    job,mid,path=seed(config,jpeg(camera='Canon\x00 PowerShot',date='2020\x00:01:01'),'null-exif')
    original=path.read_bytes()
    photos.run(job)
    with database.connection() as db:
        assert photos.counts(db,job)=={'done':1}
        row=db.execute('SELECT evidence FROM photo_fingerprints WHERE media_id=%s',(mid,)).fetchone()
        assert row['evidence']['camera']=='Canon PowerShot'
    assert path.read_bytes()==original


def seed(config, data, suffix):
    destination=Path(config['destination']['root'])
    path=destination/(suffix+'.jpg');path.write_bytes(data)
    sha=hashlib.sha256(data).hexdigest()
    with database.connection() as db:
        job=db.execute("INSERT INTO jobs(source_root,state,inventory_complete,auto_trash_duplicates) VALUES(%s,'completed-review-required',true,false) RETURNING id",(suffix,)).fetchone()['id']
        media=db.execute("INSERT INTO media(sha256,relative_path,name,kind,size,first_import_job_id) VALUES(%s,%s,%s,'image',%s,%s) RETURNING id",(sha,path.name,path.name,len(data),job)).fetchone()['id']
    return job,media,path


def configure_worker(env,monkeypatch):
    _,config,data=env
    marker=data/'marker';marker.touch()
    class Monitor:
        def __init__(self,*a,**kw):self.marker=marker
        def prepare(self):pass
        def check(self):pass
        def close(self):pass
    monkeypatch.setattr(photos,'Monitor',Monitor)
    monkeypatch.setattr(photos,'load_config',lambda:config)
    monkeypatch.setattr(photos,'DATA',data)
    return config,data


def test_cross_import_checkpoint_review_and_exclusion(env,monkeypatch):
    config,data=configure_worker(env,monkeypatch)
    job1,m1,p1=seed(config,jpeg(),'first')
    job2,m2,p2=seed(config,jpeg(date='2022:01:01'),'second')
    photos.run(job1)
    with database.connection() as db:assert photos.groups(db,job1)['total']==0
    photos.run(job2)
    with database.connection() as db:
        result=photos.groups(db,job2);assert result['total']==1
        group=result['groups'][0];assert group['preferred_id']==m1
        photos.decide(db,job2,group['key'],'confirmed',m2,0,[m1,m2])
    with database.connection() as db:
        group=photos.groups(db,job1)['groups'][0];assert group['decision']=='confirmed'
        with pytest.raises(ValueError):photos.decide(db,job1,group['key'],'separate',m1,0,[m1,m2])
        db.rollback()
        photos.decide(db,job1,group['key'],'candidate',m1,1,[m1,m2])
    monkeypatch.setattr(photos,'fingerprint',lambda data:pytest.fail('Checkpoint must not decode again'))
    photos.run(job2)
    with database.connection() as db:
        assert photos.counts(db,job2)=={'done':1}
        assert db.execute('SELECT count(*) n FROM media').fetchone()['n']==2
        db.execute('INSERT INTO media_adjustments(media_id,rotation) VALUES(%s,90)',(m2,))
        assert photos.groups(db,job1)['total']==0
    assert p1.read_bytes()==jpeg()
    assert p2.read_bytes()==jpeg(date='2022:01:01')


def test_pause_resume_integrity_and_no_recovery(env,monkeypatch):
    config,data=configure_worker(env,monkeypatch)
    job,mid,path=seed(config,jpeg(),'pause')
    (data/'PHOTO-DUPLICATE-PAUSE').touch()
    photos.run(job)
    with database.connection() as db:assert photos.counts(db,job)=={'pending':1}
    (data/'PHOTO-DUPLICATE-PAUSE').unlink()
    content=bytearray(path.read_bytes());content[-1]^=1;path.write_bytes(content)
    with pytest.raises(RuntimeError,match='Checksum'):photos.run(job)
    with database.connection() as db:
        assert photos.counts(db,job)=={'pending':1}
        with pytest.raises(RuntimeError,match='requires review'):photos.ready(db,job)


def test_membership_and_stale_member_confirmation(env,monkeypatch):
    config,data=configure_worker(env,monkeypatch)
    j1,m1,_=seed(config,jpeg(),'one')
    j2,m2,_=seed(config,jpeg(date='2022:01:01'),'two')
    photos.run(j1);photos.run(j2)
    with database.connection() as db:
        old=photos.groups(db,j1)['groups'][0]
    j3,m3,_=seed(config,jpeg(date='2023:01:01'),'three')
    photos.run(j3)
    with database.connection() as db:
        with pytest.raises(ValueError,match='membership changed'):
            photos.decide(db,j1,old['key'],'confirmed',m1,0,[m1,m2])
        db.rollback()
        j4=db.execute("INSERT INTO jobs(source_root,state) VALUES('reference','completed-review-required') RETURNING id").fetchone()['id']
        db.execute("INSERT INTO import_items(job_id,source_path,size,mtime_ns,state,media_id) VALUES(%s,'renamed.jpg',1,0,'duplicate',%s)",(j4,m1))
        assert photos.counts(db,j4)=={'done':1}
        assert photos.groups(db,j4)['total']==1


def test_scan_refuses_shared_worker_lock(env,monkeypatch):
    from app import jobs as pipeline
    config,_=configure_worker(env,monkeypatch)
    job,_,_=seed(config,jpeg(),'locked')
    with database.connection() as db:
        db.execute('SELECT pg_advisory_lock_shared(%s)',(pipeline.WORKER_LOCK,))
        with pytest.raises(RuntimeError,match='worker is active'):photos.run(job)


def test_decode_warning_and_api_review(env,monkeypatch):
    config,_=configure_worker(env,monkeypatch)
    client,_,_=env
    job,_,_=seed(config,b'not a valid jpeg','invalid')
    photos.run(job)
    response=client.get(f'/api/import/{job}/photo-duplicates')
    assert response.status_code==200
    assert response.json()['warnings'][0]['name']=='invalid.jpg'
    with database.connection() as db:assert photos.counts(db,job)=={'warning':1}
    assert client.post(f'/api/import/{job}/photo-duplicates/decision',json={'key':'missing','decision':'confirmed','preferred_id':1,'revision':0,'member_ids':[1]}).status_code==409
