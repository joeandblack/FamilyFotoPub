import io
from pathlib import Path
import subprocess
import pytest
from PIL import Image, ImageFile
from app import database
from app.photos import thumbnail_recovery as repair
from app.photos import duplicates as photos
from app.imports import importer
from app.photos.thumbnails import find_thumbnail
from test_uploads import env
from test_unexamined_photos import disposable_migrations
from test_photo_duplicates import seed, configure_worker
from test_photo_recovery import truncated_jpeg, damaged_jpeg
from test_import_modes import create_job


@pytest.mark.parametrize('factory', [truncated_jpeg, damaged_jpeg])
def test_tolerant_thumbnail_is_isolated_and_original_unchanged(tmp_path, factory):
    source = tmp_path/'source.jpg';source.write_bytes(factory())
    target = tmp_path/'cache'/'thumb.jpg'
    before = ImageFile.LOAD_TRUNCATED_IMAGES
    captured, width, height, warning = repair.create_thumbnail(source, target)
    assert width == height == 32 and 'visual defects' in warning
    assert ImageFile.LOAD_TRUNCATED_IMAGES == before
    with Image.open(target) as image:image.load();assert image.size == (32,32)
    assert source.read_bytes() == factory()
    with Image.open(source) as image:
        with pytest.raises(OSError):image.load()


def test_valid_png_uses_actual_format_and_strict_decode(tmp_path, monkeypatch):
    source = tmp_path/'named.jpg'
    Image.new('RGBA',(20,10),(20,40,60,0)).save(source,'PNG')
    monkeypatch.setattr(repair.subprocess,'run',lambda *a,**kw:pytest.fail('No tolerant retry'))
    assert repair.create_thumbnail(source,tmp_path/'thumb.jpg') == (None,20,10,None)


@pytest.mark.parametrize('extension', ['.png', '.jpg'])
def test_truncated_png_recovers_by_actual_format(tmp_path, extension):
    output=io.BytesIO()
    Image.new('RGBA',(40,30),(20,40,60,128)).save(output,'PNG')
    data=output.getvalue()
    start=data.index(b'IDAT')+4
    source=tmp_path/('source'+extension)
    source.write_bytes(data[:start+8])
    target=tmp_path/'thumb.jpg'
    with Image.open(source) as image:
        with pytest.raises(OSError):image.load()
    result=repair.create_thumbnail(source,target)
    assert 'damaged PNG' in result[3]
    with Image.open(target) as image:image.load();assert image.size==(40,30)
    assert not ImageFile.LOAD_TRUNCATED_IMAGES
    assert source.read_bytes()==data[:start+8]


def test_recovery_timeout_leaves_no_partial_thumbnail(tmp_path, monkeypatch):
    source=tmp_path/'source.jpg';source.write_bytes(truncated_jpeg())
    target=tmp_path/'thumb.jpg'
    def timeout(*a,**kw):raise subprocess.TimeoutExpired('decoder',120)
    monkeypatch.setattr(repair.subprocess,'run',timeout)
    with pytest.raises(ValueError,match='timed out'):repair.create_thumbnail(source,target)
    assert not target.exists()
    assert not list(tmp_path.glob('*.partial'))


def test_unrecognized_file_is_not_tolerantly_recovered(tmp_path, monkeypatch):
    source=tmp_path/'source.jpg';source.write_bytes(b'not an image')
    monkeypatch.setattr(repair.subprocess,'run',lambda *a,**kw:pytest.fail('No unrelated retry'))
    with pytest.raises(OSError):repair.create_thumbnail(source,tmp_path/'thumb.jpg')


def test_import_recovers_thumbnail_but_preserves_damage_warning(env):
    _,config,data=env
    source=Path(config['source']['root'])
    (source/'damaged.jpg').write_bytes(truncated_jpeg())
    create_job(config,source)
    importer.run(str(source))
    with database.connection() as db:
        row=db.execute('SELECT * FROM media').fetchone()
        assert 'Thumbnail recovered' in row['metadata_error']
        assert row['width']==32
        assert db.execute('SELECT state FROM import_items').fetchone()['state']=='imported'
    assert find_thumbnail(data/'thumbnails',row['sha256']).is_file()
    assert (Path(config['destination']['root'])/row['relative_path']).read_bytes()==truncated_jpeg()


def test_review_scan_rebuilds_missing_thumbnail_and_keeps_manual_review(env,monkeypatch):
    config,data=configure_worker(env,monkeypatch)
    job,mid,path=seed(config,truncated_jpeg(),'recover-review')
    photos.run(job)
    with database.connection() as db:photos.prepare_unexamined(db,job)
    photos.run(job)
    with database.connection() as db:
        row=db.execute('SELECT * FROM media WHERE id=%s',(mid,)).fetchone()
        assert 'Thumbnail recovered' in row['metadata_error']
        assert db.execute('SELECT evidence FROM photo_fingerprints WHERE media_id=%s',(mid,)).fetchone()['evidence']['review_only']
    assert find_thumbnail(data/'thumbnails',row['sha256']).is_file()
    assert path.read_bytes()==truncated_jpeg()


def test_rebuild_checks_original_hash_and_skips_existing_thumbnail(env):
    _,config,data=env
    _,mid,path=seed(config,truncated_jpeg(),'repair-hash')
    with database.connection() as db:
        row=db.execute('SELECT * FROM media WHERE id=%s',(mid,)).fetchone()
        path.write_bytes(b'x'*row['size'])
        with pytest.raises(OSError,match='hash differs'):repair.rebuild_missing(db,row,config,data)
        assert not find_thumbnail(data/'thumbnails',row['sha256']).exists()
        path.write_bytes(truncated_jpeg())
        assert repair.rebuild_missing(db,row,config,data)=='recovered'
        thumb=find_thumbnail(data/'thumbnails',row['sha256']);before=thumb.read_bytes()
        assert repair.rebuild_missing(db,row,config,data)=='exists'
        assert thumb.read_bytes()==before


def test_thumbnail_failure_does_not_discard_review_comparison(env,monkeypatch):
    config,data=configure_worker(env,monkeypatch)
    job,mid,path=seed(config,truncated_jpeg(),'recover-failed-thumbnail')
    photos.run(job)
    with database.connection() as db:photos.prepare_unexamined(db,job)
    def fail(*a,**kw):raise ValueError('Thumbnail fixture failure')
    monkeypatch.setattr(repair,'create_thumbnail',fail)
    photos.run(job)
    with database.connection() as db:
        assert photos.counts(db,job)=={'done':1}
        assert 'Thumbnail fixture failure' in db.execute('SELECT metadata_error FROM media WHERE id=%s',(mid,)).fetchone()['metadata_error']
    assert path.read_bytes()==truncated_jpeg()
