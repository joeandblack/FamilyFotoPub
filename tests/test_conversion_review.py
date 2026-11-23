import hashlib
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from psycopg.types.json import Jsonb
from app.video import conversion_review as review
from app.video.conversion_performance import REVIEW_MESSAGE
from test_ownership import catalog, pytestmark


def seed(db, tmp_path, monkeypatch):
    original=b'original fixture';output=b'converted fixture'
    (tmp_path/'original.mts').write_bytes(original);(tmp_path/'converted.mp4').write_bytes(output)
    row=db.execute("INSERT INTO media(sha256,stored_sha256,relative_path,original_relative_path,original_size,name,kind,size,first_import_job_id,conversion_state,conversion_error) VALUES(%s,%s,'converted.mp4','original.mts',%s,'original.mts','video',%s,1,'warning',%s) RETURNING id",(hashlib.sha256(original).hexdigest(),hashlib.sha256(output).hexdigest(),len(original),len(output),REVIEW_MESSAGE)).fetchone()
    db.execute("INSERT INTO video_metadata(media_id,state,metadata) VALUES(%s,'done',%s)",(row['id'],Jsonb({'conversion_validation':{'playback':'review-required'}})))
    db.commit()
    monkeypatch.setattr(review,'readonly_monitor',lambda config:SimpleNamespace(check=lambda:None,close=lambda:None))
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
