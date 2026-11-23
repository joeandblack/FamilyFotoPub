import hashlib
import json
from datetime import datetime
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from app.library import trash


class DB:
    def __init__(self, available=True, row=None, kind='video', locked_kind=None):
        self.available=available; self.row=row; self.queries=[]; self.commits=0
        self.kind=kind; self.locked_kind=locked_kind or kind
    def execute(self, query, args=()):
        self.queries.append(query)
        value=({'available':self.available} if 'pg_try' in query else
               {'id':1,'sha256':'a'*64,'kind':self.locked_kind if 'FOR UPDATE' in query else self.kind} if 'FROM media WHERE' in query else self.row)
        return SimpleNamespace(fetchone=lambda:value, fetchall=lambda:[])
    def commit(self): self.commits+=1


def test_worker_blocks_mutations(tmp_path,monkeypatch):
    monkeypatch.setattr(trash,'DATA',tmp_path)
    for action in ('trash','restore','delete'):
        with pytest.raises(HTTPException) as error:
            trash.change(DB(False),1,action,{})
        assert error.value.status_code==409


def test_only_photo_visibility_bypasses_maintenance(tmp_path,monkeypatch):
    monkeypatch.setattr(trash,'DATA',tmp_path)
    (tmp_path/'video-decoder-busy.json').touch()
    for action in ('trash','restore'):
        db=DB(False,kind='image');trash.change(db,1,action,{})
        assert any('pg_advisory_lock(' in q for q in db.queries)
        assert any('FOR UPDATE' in q for q in db.queries)
        assert not any('pg_try' in q for q in db.queries)
        with pytest.raises(HTTPException):
            trash.change(DB(False,kind='image',locked_kind='video'),1,action,{})
    with pytest.raises(HTTPException):
        trash.change(DB(False,kind='image'),1,'delete',{})
    for action,row in [('restore',{'deleting_at':datetime.now(),'deleted_at':None}),('trash',{'deleted_at':datetime.now()})]:
        with pytest.raises(HTTPException):
            trash.change(DB(False,row=row,kind='image'),1,action,{})


def test_trash_restore_and_deleted_guards(tmp_path,monkeypatch):
    monkeypatch.setattr(trash,'DATA',tmp_path)
    for action,query in [('trash','INSERT INTO'),('restore','DELETE FROM')]:
        db=DB();trash.change(db,1,action,{})
        assert any(query in q for q in db.queries)
    for action,row in [('delete',None),('restore',{'deleting_at':datetime.now(),'deleted_at':None}),('trash',{'deleted_at':datetime.now()})]:
        with pytest.raises(HTTPException):trash.change(DB(row=row),1,action,{})
    (tmp_path/'video-decoder-busy.json').touch()
    with pytest.raises(HTTPException):trash.change(DB(),1,'trash',{})


def fixture(tmp_path,monkeypatch):
    monkeypatch.setattr(trash,'DATA',tmp_path/'data')
    root=tmp_path/'destination';root.mkdir()
    marker=tmp_path/'marker.json';marker.write_text(json.dumps({'library_id':'test'}))
    monkeypatch.setattr(trash,'Monitor',lambda *a,**k:SimpleNamespace(marker=marker,verify=lambda:None))
    monkeypatch.setattr(trash,'match_disk',lambda *a:None)
    monkeypatch.setattr(trash,'volume_serial',lambda *a:'TEST')
    config={'destination':{'root':str(root),'drive':'X','volume_serial':'TEST'},'io_mibps':0}
    payload=b'destination-only fixture'
    (root/'test.jpg').write_bytes(payload)
    media={'id':1,'relative_path':'test.jpg','sha256':hashlib.sha256(payload).hexdigest()}
    return root,config,media


def test_delete_and_retry_preserve_catalog(tmp_path,monkeypatch):
    root,config,media=fixture(tmp_path,monkeypatch)
    db=DB(row={'deleting_at':None})
    stage=trash.DATA/'conversion-temp/1'
    stage.mkdir(parents=True)
    for name in ('input.avi','output.mp4','previous-123.mp4','frame-validation.json'):
        (stage/name).write_bytes(b'leftover')
    trash.delete_files(db,media,config)
    assert not stage.exists()
    assert not (root/'test.jpg').exists() and db.commits==1
    assert not any('DELETE FROM media' in q for q in db.queries)
    trash.delete_files(DB(row={'deleting_at':datetime.now()}),media,config)


def test_permanent_delete_removes_the_photos_faces_in_the_same_transaction(tmp_path,monkeypatch):
    root,config,media=fixture(tmp_path,monkeypatch)
    calls=[]
    def forget(db,media_id):
        calls.append(('forget',media_id,any('deleted_at=now()' in q for q in db.queries if isinstance(q,str))))
        return {'face_native_'+'a'*32:[7]}
    monkeypatch.setattr(trash,'forget_faces',forget)
    monkeypatch.setattr(trash,'remove_face_thumbnails',lambda data,removed:calls.append(('thumbnails',removed)))
    db=DB(row={'deleting_at':None})
    trash.delete_files(db,media,config)
    # Faces go before the photo is marked deleted, in the same transaction; thumbnails after.
    assert calls==[('forget',1,False),('thumbnails',{'face_native_'+'a'*32:[7]})]
    assert any('deleted_at=now()' in q for q in db.queries if isinstance(q,str))


def test_corruption_missing_and_traversal_stop_delete(tmp_path,monkeypatch):
    root,config,media=fixture(tmp_path,monkeypatch)
    db=DB(row={'deleting_at':None})
    (root/'test.jpg').write_bytes(b'corrupt')
    with pytest.raises(OSError,match='Checksum'):trash.delete_files(db,media,config)
    assert (root/'test.jpg').read_bytes()==b'corrupt' and db.commits==0
    (root/'test.jpg').unlink()
    with pytest.raises(OSError,match='missing'):trash.delete_files(db,media,config)
    with pytest.raises(ValueError,match='outside'):trash.delete_files(db,{**media,'relative_path':'../other'},config)


def test_wrong_disk_stops_delete(tmp_path,monkeypatch):
    root,config,media=fixture(tmp_path,monkeypatch)
    monkeypatch.setattr(trash,'volume_serial',lambda *a:'WRONG')
    with pytest.raises(OSError,match='volume'):trash.delete_files(DB(row={'deleting_at':None}),media,config)
    assert (root/'test.jpg').exists()


def test_unsafe_staging_blocks_before_media_delete(tmp_path,monkeypatch):
    root,config,media=fixture(tmp_path,monkeypatch)
    stage=trash.DATA/'conversion-temp/1'
    stage.mkdir(parents=True)
    (stage/'unrecognized.bin').write_bytes(b'keep')
    db=DB(row={'deleting_at':None})
    with pytest.raises(OSError,match='Unexpected'):trash.delete_files(db,media,config)
    assert (root/'test.jpg').exists() and db.commits==0


def test_staging_cleanup_failure_leaves_deletion_retryable(tmp_path,monkeypatch):
    root,config,media=fixture(tmp_path,monkeypatch)
    stage=trash.DATA/'conversion-temp/1'
    stage.mkdir(parents=True)
    (stage/'output.mp4').write_bytes(b'partial')
    real=trash.staging.cleanup
    def failure(*args):raise OSError('Cleanup interrupted')
    monkeypatch.setattr(trash.staging,'cleanup',failure)
    db=DB(row={'deleting_at':None})
    with pytest.raises(OSError,match='interrupted'):trash.delete_files(db,media,config)
    assert db.commits==1 and not any('deleted_at=now()' in q for q in db.queries)
    assert stage.exists()
    monkeypatch.setattr(trash.staging,'cleanup',real)
    trash.delete_files(DB(row={'deleting_at':datetime.now()}),media,config)
    assert not stage.exists()
