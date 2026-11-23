import hashlib
import pytest
from PIL import Image
from app.photos.thumbnails import thumbnail_path, find_thumbnail, migrate_one


def test_migration_and_resume(tmp_path):
    sha = 'ab12' + '0'*60
    flat = tmp_path/f'{sha}.jpg'
    Image.new('RGB',(30,20),'green').save(flat)
    before = hashlib.sha256(flat.read_bytes()).hexdigest()
    assert find_thumbnail(tmp_path,sha) == flat
    target = migrate_one(tmp_path,sha)
    assert target == tmp_path/'ab'/'12'/f'{sha}.jpg'
    assert not flat.exists()
    assert hashlib.sha256(target.read_bytes()).hexdigest() == before
    assert find_thumbnail(tmp_path,sha) == target
    assert migrate_one(tmp_path,sha) == target


def test_conflicting_thumbnail_not_overwritten(tmp_path):
    sha = 'c'*64
    flat = tmp_path/f'{sha}.jpg'
    Image.new('RGB',(30,20),'green').save(flat)
    target = thumbnail_path(tmp_path,sha)
    target.parent.mkdir(parents=True)
    target.write_bytes(b'conflicting data')
    with pytest.raises(OSError,match='Conflicting'):
        migrate_one(tmp_path,sha)
    assert flat.is_file()
    assert target.read_bytes() == b'conflicting data'
    with pytest.raises(ValueError):
        thumbnail_path(tmp_path,'../bad')
