from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from pydantic import StrictBool
from ..database import connection
from . import duplicates as photos

router=APIRouter()

@router.post('/api/import/{job_id}/photo-duplicates/large-images/skip')
def skip_large_images(job_id:int):
    from ..jobs import idle
    try:
        with connection() as db:
            idle(db)
            photos.ready(db,job_id)
            rows=db.execute("""UPDATE photo_duplicate_items SET state='skipped',
                error='Reviewed and intentionally excluded from duplicate comparison: ' || error
                WHERE job_id=%s AND state='warning' AND starts_with(error,'Large image scan failed: ')
                RETURNING media_id""",(job_id,)).fetchall()
            if not rows:raise ValueError('Reviewed skip is available only after a large-image scan fails')
        return {'message':f'{len(rows)} photos marked reviewed and skipped. No duplicate comparison performed.'}
    except (RuntimeError,ValueError,OSError) as error:raise HTTPException(409,str(error))

@router.post('/api/import/{job_id}/photo-duplicates/large-images')
def scan_large_images(job_id:int):
    from ..jobs import launch
    try:return launch(job_id,'duplicates',large_review=True)
    except (RuntimeError,ValueError,OSError) as error:raise HTTPException(409,str(error))

@router.post('/api/import/{job_id}/photo-duplicates/unexamined')
def scan_unexamined(job_id:int):
    from ..jobs import launch
    try:return launch(job_id,'duplicates',unexamined=True)
    except (RuntimeError,ValueError,OSError) as error:raise HTTPException(409,str(error))

class Options(BaseModel):
    enabled:StrictBool

@router.post('/api/import/{job_id}/photo-duplicates/auto-trash')
def auto_trash(job_id:int,body:Options):
    with connection() as db:
        if not db.execute("UPDATE jobs SET auto_trash_duplicates=%s WHERE id=%s AND state<>'cancelled' RETURNING id",(body.enabled,job_id)).fetchone():
            raise HTTPException(404,'Import unavailable')
    return {'ok':True}

@router.get('/api/import/{job_id}/photo-duplicates/trashed')
def trashed(job_id:int,page:int=Query(1,ge=1)):
    from .duplicate_trash import review
    with connection() as db:return review(db,job_id,page)

@router.post('/api/import/{job_id}/photo-duplicates/trashed/{media_id}/restore')
def restore(job_id:int,media_id:int):
    from ..library.trash import change
    from ..config import load_config
    with connection() as db:
        if not db.execute('SELECT 1 FROM photo_duplicate_trash WHERE job_id=%s AND media_id=%s',(job_id,media_id)).fetchone():
            raise HTTPException(404,'Trashed duplicate unavailable')
        return change(db,media_id,'restore',load_config())

@router.get('/api/import/{job_id}/photo-duplicates')
def review(job_id:int,page:int=Query(1,ge=1)):
    with connection() as db:
        return photos.groups(db,job_id,page)

class Decision(BaseModel):
    key:str
    decision:str
    preferred_id:int
    revision:int
    member_ids:list[int]

@router.post('/api/import/{job_id}/photo-duplicates/decision')
def decide(job_id:int,body:Decision):
    try:
        with connection() as db:
            photos.decide(db,job_id,body.key,body.decision,body.preferred_id,body.revision,body.member_ids)
        return {'ok':True}
    except ValueError as error:
        raise HTTPException(409,str(error))
