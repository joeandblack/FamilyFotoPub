import io
import subprocess
import pytest
from PIL import Image, ImageFile
from app.photos import fingerprint as fp
from app.photos.duplicate_trash import preferred_copy
from test_uploads import env
from test_unexamined_photos import disposable_migrations
from test_photo_duplicates import seed, configure_worker
from app import database
from app.photos import duplicates as photos


def damaged_jpeg():
    stream=io.BytesIO();Image.new('RGB',(32,32),'red').save(stream,'JPEG')
    data=bytearray(stream.getvalue())
    data[data.index(b'\xff\xda')+6]=0x33
    return bytes(data)


def truncated_jpeg():
    stream=io.BytesIO();Image.new('RGB',(32,32),'red').save(stream,'JPEG')
    return stream.getvalue()[:-2]


@pytest.mark.parametrize('factory,message',[(damaged_jpeg,'broken data stream'),(truncated_jpeg,'image file is truncated')])
def test_only_explicit_review_uses_isolated_tolerant_decoder(factory,message):
    data=factory();before=ImageFile.LOAD_TRUNCATED_IMAGES
    with pytest.raises(OSError,match=message):fp.fingerprint(data)
    result=fp.fingerprint(data,review=True)
    assert result['evidence']['review_only']
    assert result['evidence']['decode_mode']=='tolerant-jpeg'
    assert message in result['evidence']['original_decode_error']
    assert 'Manual review required' in result['evidence']['recovery_warning']
    assert preferred_copy([result]) is None
    assert ImageFile.LOAD_TRUNCATED_IMAGES==before
    with pytest.raises(OSError,match=message):fp.fingerprint(data)


def test_unrelated_io_error_does_not_trigger_recovery(monkeypatch):
    def fail(*args,**kwargs):raise OSError('Access denied')
    monkeypatch.setattr(fp,'_fingerprint',fail)
    monkeypatch.setattr(fp.subprocess,'run',lambda *a,**k:pytest.fail('Must not retry unrelated errors'))
    with pytest.raises(OSError,match='Access denied'):fp.fingerprint(b'data',review=True)


def test_recovery_timeout_is_a_warning(monkeypatch):
    def timeout(*args,**kwargs):raise subprocess.TimeoutExpired('decoder',120)
    monkeypatch.setattr(fp.subprocess,'run',timeout)
    with pytest.raises(ValueError,match='timed out'):fp.fingerprint(damaged_jpeg(),review=True)


def test_resource_limits_still_block_recovery(monkeypatch):
    monkeypatch.setattr(fp,'available_memory',lambda:1)
    monkeypatch.setattr(fp.subprocess,'run',lambda *a,**k:pytest.fail('Must not launch decoder'))
    with pytest.raises(ValueError,match='memory'):fp.fingerprint(damaged_jpeg(),review=True)


def test_normal_review_does_not_use_tolerant_decoder(monkeypatch):
    monkeypatch.setattr(fp.subprocess,'run',lambda *a,**k:pytest.fail('No recovery needed'))
    stream=io.BytesIO();Image.new('RGB',(32,32),'red').save(stream,'JPEG')
    assert 'decode_mode' not in fp.fingerprint(stream.getvalue(),review=True)['evidence']


def test_unexamined_handler_keeps_recovered_matches_for_manual_review(env,monkeypatch):
    config,_=configure_worker(env,monkeypatch)
    original=damaged_jpeg()
    first,_,path1=seed(config,original,'damaged-one')
    second,_,path2=seed(config,original+b'different trailing metadata','damaged-two')
    for job in (first,second):
        photos.run(job)
        with database.connection() as db:
            assert photos.counts(db,job)=={'warning':1}
            photos.prepare_unexamined(db,job)
            db.execute('UPDATE jobs SET auto_trash_duplicates=true WHERE id=%s',(job,))
        photos.run(job)
    with database.connection() as db:
        group=photos.groups(db,second)['groups'][0]
        assert 'damaged JPEG decoded tolerantly' in group['method']
        assert db.execute('SELECT count(*) n FROM media_trash').fetchone()['n']==0
        assert photos.counts(db,second)=={'done':1}
    assert path1.read_bytes()==original
    assert path2.read_bytes()==original+b'different trailing metadata'


def test_unexamined_handler_recovers_truncated_jpeg_without_replacing_it(env,monkeypatch):
    config,_=configure_worker(env,monkeypatch)
    original=truncated_jpeg()
    job,mid,path=seed(config,original,'truncated')
    photos.run(job)
    with database.connection() as db:
        assert photos.counts(db,job)=={'warning':1}
        photos.prepare_unexamined(db,job)
    photos.run(job)
    with database.connection() as db:
        assert photos.counts(db,job)=={'done':1}
        evidence=db.execute('SELECT evidence FROM photo_fingerprints WHERE media_id=%s',(mid,)).fetchone()['evidence']
        assert evidence['review_only'] and evidence['decode_mode']=='tolerant-jpeg'
        assert db.execute('SELECT count(*) n FROM media_trash').fetchone()['n']==0
    assert path.read_bytes()==original
