"""Logical albums: no disk operations; membership uses stable catalog identities."""
from typing import Literal
from psycopg.types.json import Jsonb
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, StrictInt, StrictBool, field_validator
from ..database import connection
from ..jobs.job_access import visible_job
from ..viewer import media_visible

router=APIRouter(prefix='/api/albums')
def seen(table):
    """SQL: the viewer may see the media of album-item rows aliased `table`."""
    return f'EXISTS(SELECT 1 FROM media seen WHERE seen.id={table}.media_id AND '+media_visible('seen')+')'

# An album item is shown to the viewer unless it is trashed or the viewer may not see its media.
VISIBLE='NOT EXISTS(SELECT 1 FROM media_trash t WHERE t.media_id=ai.media_id) AND EXISTS(SELECT 1 FROM media seen WHERE seen.id=ai.media_id AND '+media_visible('seen')+')'
MAX_ALBUM_SELECTION = 100_000

class Details(BaseModel):
    name:str=Field(max_length=200)
    description:str=Field(default='',max_length=4000)
    @field_validator('name')
    @classmethod
    def name_valid(cls,value):
        if not value.strip():raise ValueError('Enter an album name')
        return value.strip()

class Members(BaseModel):
    media_ids:list[StrictInt]=Field(default_factory=list,max_length=MAX_ALBUM_SELECTION)
    @field_validator('media_ids')
    @classmethod
    def ids_valid(cls,values):
        if any(not 0<v<=9007199254740991 for v in values):raise ValueError('Invalid media ID')
        return list(dict.fromkeys(values))

class EditDetails(Details):
    show_on_albums:StrictBool|None=None

class Create(Details,Members):
    import_job:int|None=Field(default=None,gt=0,le=9007199254740991,strict=True)

@router.post('/membership')
def membership(data:Members):
    with connection() as db:
        rows=db.execute('''SELECT a.id,a.name,count(*) item_count
          FROM album_items ai JOIN albums a ON a.id=ai.album_id
          WHERE ai.media_id=ANY(%s) AND '''+seen('ai')+''' GROUP BY a.id,a.name ORDER BY a.name,a.id''',
          (data.media_ids,)).fetchall()
        return {'albums':rows}

class Move(BaseModel):
    direction:Literal['first','previous','next','last']

class CoverFraming(BaseModel):
    media_id:StrictInt=Field(gt=0,le=9007199254740991)
    mode:Literal['fill','fit']='fill'
    zoom:float=Field(default=1,ge=1,le=4,allow_inf_nan=False)
    x:float=Field(default=0,ge=-1,le=1,allow_inf_nan=False)
    y:float=Field(default=0,ge=-1,le=1,allow_inf_nan=False)

@router.patch('/{aid}/cover-framing')
def frame_cover(aid:int,data:CoverFraming):
    with connection() as db:
        require(db,aid,True)
        if describe(db,aid)['cover_id']!=data.media_id:
            raise HTTPException(409,'The album cover changed. Reopen Adjust cover to edit the current photo.')
        db.execute('UPDATE albums SET cover_framing=%s,updated_at=now() WHERE id=%s',
                   (Jsonb(data.model_dump()),aid))
        return describe(db,aid)

class Position(BaseModel):
    target_id:StrictInt=Field(gt=0,le=9007199254740991)
    after:bool=False

class Transfer(Members):
    target_id:StrictInt|None=Field(default=None,gt=0,le=9007199254740991)
    new_album:Details|None=None

@router.post('/{aid}/transfer')
def transfer(aid:int,data:Transfer):
    if not data.media_ids:raise HTTPException(400,'Select photos or videos to move')
    if (data.target_id is None)==(data.new_album is None):
        raise HTTPException(400,'Choose a destination album or create a new one')
    if data.target_id==aid:raise HTTPException(400,'Choose a different album')
    with connection() as db:
        # Stable lock order also protects simultaneous moves in opposite directions.
        for album_id in sorted({aid,data.target_id} if data.target_id else {aid}):
            require(db,album_id,True)
        selected=db.execute('SELECT media_id FROM album_items WHERE album_id=%s AND media_id=ANY(%s) AND '+seen('album_items'),(aid,data.media_ids)).fetchall()
        if len(selected)!=len(data.media_ids):
            raise HTTPException(409,'Some selected items are no longer in this album. No items were moved.')
        target=data.target_id
        if target is None:
            target=db.execute('INSERT INTO albums(name,description) VALUES (%s,%s) RETURNING id',
              (data.new_album.name,data.new_album.description)).fetchone()['id']
        added=add(db,target,data.media_ids)
        db.execute('DELETE FROM album_items WHERE album_id=%s AND media_id=ANY(%s)',(aid,data.media_ids))
        db.execute('UPDATE albums SET updated_at=now() WHERE id=%s',(aid,))
        return {'id':target,'moved':len(data.media_ids),'added':added}

def require(db,aid,lock=False):
    row=db.execute('SELECT * FROM albums WHERE id=%s'+(' FOR UPDATE' if lock else ''),(aid,)).fetchone()
    if not row:raise HTTPException(404,'Album not found')
    return row

def describe(db,aid):
    row=require(db,aid)
    row['collection_count']=db.execute('SELECT count(*) n FROM album_collection_members WHERE album_id=%s',(aid,)).fetchone()['n']
    row.update(db.execute(f'''SELECT count(*) item_count,
      (array_agg(ai.media_id ORDER BY ai.is_cover DESC,ai.position,ai.media_id))[1] cover_id
      FROM album_items ai WHERE ai.album_id=%s AND {VISIBLE}''',(aid,)).fetchone())
    return row

def add(db,aid,ids):
    if not ids:return 0
    # Share row locks serialize with Trash/permanent deletion, in stable ID order.
    found=db.execute('SELECT id FROM media WHERE id=ANY(%s) AND '+media_visible('media')+' ORDER BY id FOR SHARE',(ids,)).fetchall()
    if len(found)!=len(ids):raise HTTPException(404,'One or more media items no longer exist')
    if db.execute('SELECT 1 FROM media_trash WHERE media_id=ANY(%s)',(ids,)).fetchone():
        raise HTTPException(409,'Restore items from Recently Deleted before adding them')
    position=db.execute('SELECT coalesce(max(position),0) p FROM album_items WHERE album_id=%s',(aid,)).fetchone()['p']
    added=db.execute('''INSERT INTO album_items(album_id,media_id,position)
      SELECT %s,mid,%s+ordinal FROM unnest(%s::bigint[]) WITH ORDINALITY AS source(mid,ordinal)
      ON CONFLICT DO NOTHING''',(aid,position,ids)).rowcount
    db.execute('UPDATE albums SET updated_at=now() WHERE id=%s',(aid,))
    return added

@router.get('')
def index():
    with connection() as db:
        return {'albums':db.execute(f'''SELECT a.*, c.item_count,c.cover_id,
         (SELECT count(*) FROM album_collection_members cm WHERE cm.album_id=a.id) collection_count FROM albums a
         CROSS JOIN LATERAL (SELECT count(*) item_count,
          (array_agg(ai.media_id ORDER BY ai.is_cover DESC,ai.position,ai.media_id))[1] cover_id
          FROM album_items ai WHERE ai.album_id=a.id AND {VISIBLE}) c
         ORDER BY a.sort_order NULLS LAST,a.updated_at DESC,a.id DESC''').fetchall()}

@router.post('/{aid}/position')
def position(aid:int,data:Position):
    with connection() as db:
        # Serialize reorders with album creation/deletion; never lock media rows.
        db.execute("SET LOCAL lock_timeout='5s'")
        db.execute('LOCK TABLE albums IN SHARE ROW EXCLUSIVE MODE')
        require(db,aid);require(db,data.target_id)
        ids=[row['id'] for row in db.execute('SELECT id FROM albums ORDER BY sort_order NULLS LAST,updated_at DESC,id DESC').fetchall()]
        if aid!=data.target_id:
            ids.remove(aid)
            ids.insert(ids.index(data.target_id)+int(data.after),aid)
            db.execute('''UPDATE albums a SET sort_order=ordered.position
                FROM unnest(%s::bigint[]) WITH ORDINALITY AS ordered(id,position)
                WHERE a.id=ordered.id AND a.sort_order IS DISTINCT FROM ordered.position''',(ids,))
        return {'order':ids}

@router.post('')
def create(data:Create):
    with connection() as db:
        ids=data.media_ids
        if data.import_job is not None:
            if ids:raise HTTPException(422,'Choose an import or selected items, not both')
            if not visible_job(db,data.import_job):raise HTTPException(404,'Import not found')
            ids=[r['id'] for r in db.execute('''SELECT m.id FROM media m WHERE EXISTS
              (SELECT 1 FROM import_items i WHERE i.job_id=%s AND i.media_id=m.id)
              AND NOT EXISTS(SELECT 1 FROM media_trash t WHERE t.media_id=m.id)
              AND '''+media_visible('m')+'''
              ORDER BY m.captured_at DESC NULLS LAST,m.id DESC''',(data.import_job,)).fetchall()]
        aid=db.execute('INSERT INTO albums(name,description) VALUES (%s,%s) RETURNING id',(data.name,data.description)).fetchone()['id']
        add(db,aid,ids)
        return describe(db,aid)

@router.get('/{aid}')
def detail(aid:int):
    with connection() as db:return describe(db,aid)

@router.patch('/{aid}')
def edit(aid:int,data:EditDetails):
    with connection() as db:
        require(db,aid,True)
        if data.show_on_albums is not None:
            if not data.show_on_albums and not db.execute('SELECT 1 FROM album_collection_members WHERE album_id=%s LIMIT 1',(aid,)).fetchone():
                raise HTTPException(409,'Add this album to a collection before hiding it from Albums')
            db.execute('UPDATE albums SET show_on_albums=%s WHERE id=%s',(data.show_on_albums,aid))
        db.execute('UPDATE albums SET name=%s,description=%s,updated_at=now() WHERE id=%s',(data.name,data.description,aid))
        return describe(db,aid)

@router.delete('/{aid}')
def delete(aid:int):
    with connection() as db:
        require(db,aid,True)
        db.execute('DELETE FROM albums WHERE id=%s',(aid,))
    return {'ok':True}

@router.post('/{aid}/items')
def add_items(aid:int,data:Members):
    with connection() as db:
        require(db,aid,True)
        return {'added':add(db,aid,data.media_ids)}

@router.post('/{aid}/remove')
def remove(aid:int,data:Members):
    with connection() as db:
        require(db,aid,True)
        count=db.execute('DELETE FROM album_items WHERE album_id=%s AND media_id=ANY(%s) AND '+seen('album_items'),(aid,data.media_ids)).rowcount
        db.execute('UPDATE albums SET updated_at=now() WHERE id=%s',(aid,))
        return {'removed':count}

@router.post('/{aid}/items/{mid}/cover')
def cover(aid:int,mid:int):
    with connection() as db:
        require(db,aid,True)
        if not db.execute(f'SELECT 1 FROM album_items ai WHERE album_id=%s AND media_id=%s AND {VISIBLE}',(aid,mid)).fetchone():raise HTTPException(404,'Visible album item not found')
        if describe(db,aid)['cover_id']!=mid:
            db.execute("UPDATE albums SET cover_framing='{}' WHERE id=%s",(aid,))
        db.execute('UPDATE album_items SET is_cover=false WHERE album_id=%s AND is_cover',(aid,))
        db.execute('UPDATE album_items SET is_cover=true WHERE album_id=%s AND media_id=%s',(aid,mid))
        return describe(db,aid)

@router.post('/{aid}/items/{mid}/move')
def move(aid:int,mid:int,data:Move):
    with connection() as db:
        require(db,aid,True)
        rows=db.execute('SELECT ai.media_id,'+VISIBLE+' visible FROM album_items ai WHERE album_id=%s ORDER BY position,media_id',(aid,)).fetchall()
        visible=[r['media_id'] for r in rows if r['visible']]
        if mid not in visible:raise HTTPException(404,'Visible album item not found')
        index=visible.index(mid)
        target={'first':0,'previous':max(0,index-1),'next':min(len(visible)-1,index+1),'last':len(visible)-1}[data.direction]
        visible.pop(index);visible.insert(target,mid)
        iterator=iter(visible)
        ordered=[next(iterator) if row['visible'] else row['media_id'] for row in rows]
        db.execute('''UPDATE album_items ai SET position=ordered.position
          FROM unnest(%s::bigint[]) WITH ORDINALITY AS ordered(media_id,position)
          WHERE ai.album_id=%s AND ai.media_id=ordered.media_id AND ai.position<>ordered.position''',(ordered,aid))
        db.execute('UPDATE albums SET updated_at=now() WHERE id=%s',(aid,))
        return {'ok':True}

@router.post('/{aid}/items/{mid}/position')
def position_item(aid:int,mid:int,data:Position):
    with connection() as db:
        require(db,aid,True)
        rows=db.execute('SELECT ai.media_id,'+VISIBLE+' visible FROM album_items ai WHERE album_id=%s ORDER BY position,media_id',(aid,)).fetchall()
        visible=[row['media_id'] for row in rows if row['visible']]
        if mid not in visible or data.target_id not in visible:
            raise HTTPException(404,'Visible album item not found')
        if mid!=data.target_id:
            visible.remove(mid)
            visible.insert(visible.index(data.target_id)+int(data.after),mid)
            iterator=iter(visible)
            ordered=[next(iterator) if row['visible'] else row['media_id'] for row in rows]
            db.execute('''UPDATE album_items ai SET position=ordered.position
              FROM unnest(%s::bigint[]) WITH ORDINALITY AS ordered(media_id,position)
              WHERE ai.album_id=%s AND ai.media_id=ordered.media_id AND ai.position<>ordered.position''',(ordered,aid))
            db.execute('UPDATE albums SET updated_at=now() WHERE id=%s',(aid,))
        return {'order':visible}
