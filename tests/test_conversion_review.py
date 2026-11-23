import hashlib
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from psycopg.types.json import Jsonb
from app.video import conversion_review as review
from app.video.conversion_performance import REVIEW_MESSAGE
from test_ownership import catalog, pytestmark


def seed(db, tmp_path, monkeypatch, name='original.mts', validation=None, state='warning', error=REVIEW_MESSAGE,
         converted='converted.mp4'):
    original=b'original fixture '+name.encode();output=b'converted fixture '+name.encode()
    (tmp_path/name).write_bytes(original);(tmp_path/converted).write_bytes(output)
    row=db.execute("INSERT INTO media(sha256,stored_sha256,relative_path,original_relative_path,original_size,name,kind,size,first_import_job_id,conversion_state,conversion_error) VALUES(%s,%s,%s,%s,%s,%s,'video',%s,1,%s,%s) RETURNING id",(hashlib.sha256(original).hexdigest(),hashlib.sha256(output).hexdigest(),converted,name,len(original),name,len(output),state,error)).fetchone()
    metadata={'conversion_validation':{'playback':'review-required'} if validation is None else validation}
    db.execute("INSERT INTO video_metadata(media_id,state,metadata) VALUES(%s,'done',%s)",(row['id'],Jsonb(metadata)))
    db.commit()
    monkeypatch.setattr(review,'readonly_monitor',lambda config:SimpleNamespace(check=lambda:None,close=lambda:None))
    monkeypatch.setattr(review,'DATA',tmp_path/'data')
    return row['id'],{'destination':{'root':str(tmp_path)}}


@pytest.mark.parametrize('delete',[False,True])
def test_review_selected_video_keeps_mp4_and_catalog(catalog,tmp_path,monkeypatch,delete):
    id,config=seed(catalog,tmp_path,monkeypatch)
    review.finish(catalog,1,id,delete,config);catalog.commit()
    assert (tmp_path/'converted.mp4').exists()
    assert (tmp_path/'original.mts').exists() is not delete
    row=catalog.execute('SELECT m.*,v.metadata FROM media m JOIN video_metadata v ON v.media_id=m.id WHERE m.id=%s',(id,)).fetchone()
    assert row['conversion_state']=='done' and row['conversion_error'] is None
    assert row['original_relative_path']=='original.mts' and row['sha256']
    assert row['metadata']['conversion_validation']['playback']=='user-reviewed'
    assert review.finish(catalog,1,id,delete,config)['ok']


@pytest.mark.parametrize('problem',['wrong-import','trash','bad-output','bad-original','missing-original','same-path','unreviewable'])
def test_review_refuses_unsafe_changes(catalog,tmp_path,monkeypatch,problem):
    id,config=seed(catalog,tmp_path,monkeypatch)
    if problem=='trash':catalog.execute('INSERT INTO media_trash(media_id) VALUES(%s)',(id,))
    if problem=='bad-output':(tmp_path/'converted.mp4').write_bytes(b'bad')
    if problem=='bad-original':(tmp_path/'original.mts').write_bytes(b'bad')
    if problem=='missing-original':(tmp_path/'original.mts').unlink()
    if problem=='same-path':catalog.execute("UPDATE media SET original_relative_path=relative_path WHERE id=%s",(id,))
    if problem=='unreviewable':catalog.execute("UPDATE media SET conversion_state='published' WHERE id=%s",(id,))
    catalog.commit()
    with pytest.raises((HTTPException,OSError,ValueError)):
        review.finish(catalog,2 if problem=='wrong-import' else 1,id,True,config)
    catalog.rollback()
    assert (tmp_path/'converted.mp4').exists()
    assert catalog.execute('SELECT conversion_error FROM media WHERE id=%s',(id,)).fetchone()['conversion_error']==REVIEW_MESSAGE


def test_retry_after_unlink_before_final_commit(catalog,tmp_path,monkeypatch):
    id,config=seed(catalog,tmp_path,monkeypatch)
    class Interrupted:
        def execute(self,query,args=()):
            if args and isinstance(args[0],Jsonb) and args[0].obj.get('playback')=='user-reviewed':
                raise OSError('interrupted after deletion')
            return catalog.execute(query,args)
        def commit(self):catalog.commit()
    with pytest.raises(OSError,match='interrupted'):
        review.finish(Interrupted(),1,id,True,config)
    catalog.rollback()
    assert not (tmp_path/'original.mts').exists()
    with pytest.raises(HTTPException):review.finish(catalog,1,id,False,config)
    catalog.rollback()
    review.finish(catalog,1,id,True,config);catalog.commit()
    assert (tmp_path/'converted.mp4').exists()

def test_kept_original_can_be_deleted_later(catalog,tmp_path,monkeypatch):
    id,config=seed(catalog,tmp_path,monkeypatch)
    review.finish(catalog,1,id,False,config);catalog.commit()
    review.finish(catalog,1,id,True,config);catalog.commit()
    assert not (tmp_path/'original.mts').exists()
    assert (tmp_path/'converted.mp4').exists()


@pytest.mark.parametrize('name,validation',[
    ('original.avi',{}),                                    # AVI converted with cleanup off, no validation record
    ('original.avi',{'mode':'full','playback':'decoded'}),
    ('original.flv',{'mode':'full','playback':'decoded'}),  # Full validation, never in the quick review
    ('original.mts',{}),                                    # Converted before validation was recorded
])
def test_delete_any_converted_original_keeps_mp4_and_identity(catalog,tmp_path,monkeypatch,name,validation):
    id,config=seed(catalog,tmp_path,monkeypatch,name,validation,state='done',error=None)
    assert review.retained_originals(catalog,1,config)==[id]
    review.finish(catalog,1,id,True,config);catalog.commit()
    assert not (tmp_path/name).exists() and (tmp_path/'converted.mp4').exists()
    row=catalog.execute('SELECT m.*,v.metadata FROM media m JOIN video_metadata v ON v.media_id=m.id WHERE m.id=%s',(id,)).fetchone()
    # The original's identity stays, so importing the same file again is a duplicate.
    assert row['sha256']==hashlib.sha256(b'original fixture '+name.encode()).hexdigest()
    assert row['original_relative_path']==name and row['relative_path']=='converted.mp4'
    result=row['metadata']['conversion_validation']
    assert result['original_deleted_at'] and result['original_action']=='delete'
    assert result['playback']==('decoded' if validation.get('playback')=='decoded' else 'user-reviewed')
    assert review.retained_originals(catalog,1,config)==[]
    assert review.finish(catalog,1,id,True,config)['ok']


def test_keep_without_playback_review_is_refused(catalog,tmp_path,monkeypatch):
    id,config=seed(catalog,tmp_path,monkeypatch,'original.avi',{'playback':'decoded'},state='done',error=None)
    with pytest.raises(HTTPException):
        review.finish(catalog,1,id,False,config)


def test_recovery_hold_keeps_avi_original(catalog,tmp_path,monkeypatch):
    id,config=seed(catalog,tmp_path,monkeypatch,'original.avi',{},state='done',error=None)
    hold=tmp_path/'data'/'conversion-recovery'/str(id);hold.mkdir(parents=True)
    (hold/'retain-original.json').write_text('{}')
    assert review.retained_originals(catalog,1,config)==[]
    with pytest.raises(HTTPException,match='recovery hold'):
        review.finish(catalog,1,id,True,config)
    catalog.rollback()
    assert (tmp_path/'original.avi').exists()


def test_list_skips_missing_trashed_other_imports_and_unfinished(catalog,tmp_path,monkeypatch):
    def add(name,**options):
        return seed(catalog,tmp_path,monkeypatch,name,converted=name+'.mp4',**options)
    kept,config=add('kept.avi',validation={},state='done',error=None)
    gone,_=add('gone.avi',validation={},state='done',error=None)
    (tmp_path/'gone.avi').unlink()  # Legacy AVI cleanup left no receipt
    trashed,_=add('trashed.mts')
    catalog.execute('INSERT INTO media_trash(media_id) VALUES(%s)',(trashed,))
    other,_=add('other.flv')
    catalog.execute('UPDATE media SET first_import_job_id=2 WHERE id=%s',(other,))
    add('published.mts',state='published',error=None)
    catalog.commit()
    assert review.retained_originals(catalog,1,config)==[kept]
    assert review.retained_originals(catalog,2,config)==[other]
