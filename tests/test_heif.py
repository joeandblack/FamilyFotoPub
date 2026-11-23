import hashlib
from io import BytesIO
from datetime import datetime
import pytest
from PIL import Image, ImageCms, UnidentifiedImageError
from app.photos import heif as heif_images
from app.library.media import image_metadata
from test_uploads import env, start, transfer


def sample(path, orientation=1):
    image = Image.new('RGB', (120, 80), (90, 170, 45))
    exif = Image.Exif();exif[274] = orientation;exif[306] = '2021:06:07 08:09:10'
    image.save(path, 'HEIF', exif=exif.tobytes(), icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes())
    return path.read_bytes()


def test_heif_derivatives_orientation_color_and_original_preservation(tmp_path):
    source = tmp_path/'source.HEIC';before = sample(source, 6)
    thumb = tmp_path/'thumb.jpg';preview = tmp_path/'preview.jpg'
    captured, width, height = image_metadata(source, thumb, preview)
    assert captured == datetime(2021, 6, 7, 8, 9, 10)
    assert (width, height) == (80, 120)
    assert source.read_bytes() == before
    for target in (thumb, preview):
        with Image.open(target) as image:
            image.load()
            assert image.format == 'JPEG' and image.size == (80, 120)
            assert 'sRGB' in ImageCms.getProfileDescription(ImageCms.ImageCmsProfile(BytesIO(image.info['icc_profile'])))
            assert image.getexif().get(274, 1) == 1


def test_heif_corruption_and_resource_bounds_leave_no_derivative(tmp_path, monkeypatch):
    source = tmp_path/'bad.heic';source.write_bytes(b'not an image')
    thumb = tmp_path/'thumb.jpg'
    with pytest.raises(UnidentifiedImageError):image_metadata(source, thumb)
    assert not thumb.exists()
    sample(source)
    monkeypatch.setattr(heif_images, 'MAX_PIXELS', 100)
    with pytest.raises(ValueError, match='megapixel'):image_metadata(source, thumb)
    monkeypatch.setattr(heif_images, 'MAX_BYTES', 10)
    with pytest.raises(ValueError, match='MiB'):image_metadata(source, thumb)
    assert not thumb.exists()


def test_failed_cache_publish_preserves_existing_derivative(tmp_path, monkeypatch):
    target = tmp_path/'thumbnail.jpg';target.write_bytes(b'existing cache')
    monkeypatch.setattr(heif_images.os, 'replace', lambda *args: (_ for _ in ()).throw(OSError('Synthetic write fault')))
    with pytest.raises(OSError):heif_images.atomic_jpeg(Image.new('RGB', (10,10)), target, 80)
    assert target.read_bytes() == b'existing cache'
    assert not list(tmp_path.glob('*.partial'))


def test_heif_upload_preview_download_and_duplicate_provenance(env, tmp_path, monkeypatch):
    from app import uploads, database
    client, config, data = env
    raw = sample(tmp_path/'fixture.heic')
    session, _ = start(client, ['HEIC/a.heic','HEIC/copy.heic'], raw)
    transfer(client, session, raw)
    client.post('/api/uploads/'+session['id']+'/finalize');uploads.dispatch_once()
    with database.connection() as db:
        media = db.execute('SELECT * FROM media').fetchall()
        assert len(media) == 1 and media[0]['metadata_error'] is None
        assert sorted(r['state'] for r in db.execute('SELECT state FROM import_items').fetchall()) == ['duplicate','imported']
    item = media[0]
    for variant in ('thumbnail','preview'):
        response = client.get(f"/api/media/{item['id']}/{variant}")
        assert response.status_code == 200 and response.headers['content-type'] == 'image/jpeg'
        with Image.open(BytesIO(response.content)) as image:assert image.size == (120,80)
    config['destination'].update(drive='fixture',volume_serial='fixture')
    monkeypatch.setattr('app.storage.disks.volume_serial',lambda _: 'fixture')
    download = client.get(f"/api/media/{item['id']}/download")
    assert download.content == raw
    assert hashlib.sha256(download.content).hexdigest() == item['sha256']
    assert client.get(f"/api/media/{item['id']}/original").content == raw


def test_scoped_repair_checks_hashes_and_preserves_existing_metadata(env, tmp_path, monkeypatch):
    from app import database
    from app.photos import repair_heif
    client, config, data = env
    source = tmp_path/'destination'/'repair.heic';raw = sample(source)
    sha = hashlib.sha256(raw).hexdigest()
    with database.connection() as db:
        mid = db.execute("INSERT INTO media(sha256,relative_path,name,kind,size,captured_at,metadata_error) VALUES (%s,'repair.heic','repair.heic','image',%s,'2001-01-01','old decoder warning') RETURNING id",(sha,len(raw))).fetchone()['id']
    class Monitor:
        marker = tmp_path/'marker'
        marker.touch()
        def __init__(self,*a,**kw):pass
        def prepare(self):pass
        def check(self):pass
        def close(self):pass
    monkeypatch.setattr(repair_heif,'Monitor',Monitor)
    monkeypatch.setattr(repair_heif,'load_config',lambda:config)
    monkeypatch.setattr(repair_heif,'DATA',data)
    result = repair_heif.repair([mid])
    assert result['repaired']==[mid] and not result['failed']
    assert source.read_bytes()==raw
    with database.connection() as db:
        item=db.execute('SELECT * FROM media WHERE id=%s',(mid,)).fetchone()
        assert item['metadata_error'] is None and item['captured_at'].year==2001
        db.execute("UPDATE media SET metadata_error='keep warning on hash mismatch' WHERE id=%s",(mid,))
    source.write_bytes(b'changed fixture')
    result=repair_heif.repair([mid])
    assert not result['repaired'] and result['failed'][0]['id']==mid
    with database.connection() as db:
        assert db.execute('SELECT metadata_error FROM media WHERE id=%s',(mid,)).fetchone()['metadata_error']=='keep warning on hash mismatch'
    start(client,['Pending/a.heic'],raw)
    with pytest.raises(RuntimeError,match='Upload active'):repair_heif.repair([mid])
