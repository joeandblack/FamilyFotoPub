"""Deletion tests use synthetic files and disposable database schemas only."""
import hashlib
import threading
from pathlib import Path
from types import SimpleNamespace
import pytest
from app import database
from app.library import trash
from app.photos import heif as heif_images
from app.photos.thumbnails import cache_files, thumbnail_path, remove_cached_media
from test_uploads import env


def caches(data, sha):
    paths = cache_files(data, sha)
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'fixture cache')
    partial = thumbnail_path(data/'previews', sha).with_name(sha+'.jpg.fixture.partial')
    partial.write_bytes(b'partial')
    return [*paths, partial]


def test_exact_keys_both_layouts_missing_and_unrelated(tmp_path):
    sha = 'abcd'+'1'*60
    owned = caches(tmp_path, sha)
    other = caches(tmp_path, 'abcd'+'2'*60)
    remove_cached_media(tmp_path, sha)
    remove_cached_media(tmp_path, sha)
    assert not any(p.exists() for p in owned)
    assert all(p.exists() for p in other)
    with pytest.raises(ValueError):remove_cached_media(tmp_path, '../bad')


def test_redirected_cache_refused(tmp_path, monkeypatch):
    # lstat is portable; Windows symlink creation otherwise requires privileges.
    sha = 'a'*64
    target = thumbnail_path(tmp_path/'previews', sha)
    real = Path.lstat
    def redirected(path, *a, **kw):
        if path == target.parent:
            return SimpleNamespace(st_mode=0o040755, st_file_attributes=0x400)
        return real(path,*a,**kw)
    monkeypatch.setattr(Path, 'lstat', redirected)
    with pytest.raises(ValueError, match='redirected'):remove_cached_media(tmp_path, sha)


def test_cache_enumeration_failure_is_not_silently_ignored(tmp_path, monkeypatch):
    from app.photos import thumbnails
    def denied(*args):raise PermissionError('Synthetic enumeration failure')
    monkeypatch.setattr(thumbnails.os,'scandir',denied)
    with pytest.raises(PermissionError,match='enumeration'):remove_cached_media(tmp_path,'a'*64)


@pytest.fixture
def photo(env, tmp_path, monkeypatch):
    client, config, data = env
    payload=b'isolated permanent deletion fixture'
    sha=hashlib.sha256(payload).hexdigest()
    source=Path(config['destination']['root'])/'synthetic.jpg'
    source.write_bytes(payload)
    with database.connection() as db:
        mid=db.execute("INSERT INTO media(sha256,relative_path,name,kind,size) VALUES (%s,'synthetic.jpg','synthetic.jpg','image',%s) RETURNING id",(sha,len(payload))).fetchone()['id']
    marker=tmp_path/'marker.json';marker.write_text('{"library_id":"fixture"}',encoding='utf-8')
    monkeypatch.setattr(trash,'DATA',data)
    monkeypatch.setattr(trash,'Monitor',lambda *a,**kw:SimpleNamespace(marker=marker,verify=lambda:None))
    monkeypatch.setattr(trash,'match_disk',lambda *a:None)
    monkeypatch.setattr(trash,'volume_serial',lambda *a:'FIXTURE')
    config['destination'].update(drive='fixture',volume_serial='FIXTURE')
    return client,config,data,mid,sha,source


def test_permission_failure_retry_and_trash_restore(photo, monkeypatch):
    client,config,data,mid,sha,source=photo
    owned=caches(data,sha)
    unrelated=caches(data,'abcd'+'7'*60)
    assert client.post(f'/api/media/{mid}/trash/trash').status_code==200
    assert all(p.exists() for p in owned)
    assert client.post(f'/api/media/{mid}/trash/restore').status_code==200
    assert all(p.exists() for p in owned)
    assert client.post(f'/api/media/{mid}/trash/trash').status_code==200
    blocked=thumbnail_path(data/'previews',sha)
    real=Path.unlink
    def denied(path,*a,**kw):
        if path==blocked:raise PermissionError('Synthetic cache permission failure')
        return real(path,*a,**kw)
    with monkeypatch.context() as patch:
        patch.setattr(Path,'unlink',denied)
        response=client.post(f'/api/media/{mid}/trash/delete')
    assert response.status_code==409 and 'permission failure' in response.text
    assert not source.exists() and blocked.exists()
    with database.connection() as db:
        row=db.execute('SELECT * FROM media_trash WHERE media_id=%s',(mid,)).fetchone()
        assert row['deleting_at'] and not row['deleted_at']
    assert client.post(f'/api/media/{mid}/trash/restore').status_code==409
    assert client.get(f'/api/media/{mid}/thumbnail').status_code==404
    assert client.post(f'/api/media/{mid}/trash/delete').status_code==200
    assert not any(p.exists() for p in owned)
    assert all(p.exists() for p in unrelated)
    with database.connection() as db:
        assert db.execute('SELECT deleted_at FROM media_trash WHERE media_id=%s',(mid,)).fetchone()['deleted_at']


def test_repair_child_lease_survives_owner_exit_and_delete_waits(photo, monkeypatch):
    client,config,data,mid,sha,source=photo
    entered=threading.Event();release=threading.Event();deleted=threading.Event();errors=[]
    thumb=thumbnail_path(data/'thumbnails',sha)
    preview=thumbnail_path(data/'previews',sha)
    def generate(*args):
        entered.set()
        assert release.wait(10)
        caches(data,sha)
        return None,10,10
    monkeypatch.setattr(heif_images,'create_derivatives',generate)
    def run(fn):
        try:fn()
        except BaseException as error:errors.append(error)
    with database.dedicated_connection() as owner:
        request={'database':config['database'],'owner':owner.execute('SELECT pg_backend_pid() pid').fetchone()['pid'],
                 'media_id':mid,'sha':sha,'source':str(source),'thumbnail':str(thumb),'preview':str(preview)}
        child=threading.Thread(target=lambda:run(lambda:heif_images.guarded_derivatives(request)))
        child.start()
        assert entered.wait(5)
    # Parent connection gone, child still owns its independent shared lease.
    from app.library.locks import MEDIA_LOCK_NAMESPACE, content_key
    with database.connection() as probe:
        assert not probe.execute('SELECT pg_try_advisory_lock(%s,%s) ok',
                                 (MEDIA_LOCK_NAMESPACE,content_key(sha))).fetchone()['ok']
    def delete():
        with database.connection() as db:trash.change(db,mid,'trash',config)
        with database.connection() as db:trash.change(db,mid,'delete',config)
        deleted.set()
    worker=threading.Thread(target=lambda:run(delete));worker.start()
    try:
        assert not deleted.wait(.2)
    finally:
        release.set();child.join(10);worker.join(10)
    assert not errors and deleted.is_set()
    assert not source.exists() and not any(p.exists() for p in cache_files(data,sha))
    with database.dedicated_connection() as owner:
        request['owner']=owner.execute('SELECT pg_backend_pid() pid').fetchone()['pid']
        with pytest.raises(RuntimeError,match='eligible'):heif_images.guarded_derivatives(request)
    with pytest.raises(RuntimeError,match='owner exited'):heif_images.guarded_derivatives(request)


def test_thumbnail_migration_skips_permanently_deleted_item(photo, monkeypatch):
    from app import config as config_module
    from app.photos import thumbnails
    client,config,data,mid,sha,source=photo
    caches(data,sha)
    assert client.post(f'/api/media/{mid}/trash/trash').status_code==200
    assert client.post(f'/api/media/{mid}/trash/delete').status_code==200
    monkeypatch.setattr(config_module,'DATA',data)
    (data/'post-import-review.json').write_text('{"state":"passed"}',encoding='utf-8')
    thumbnails.migrate()
    assert not any(p.exists() for p in cache_files(data,sha))
