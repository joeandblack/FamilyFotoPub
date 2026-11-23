"""Four-level collection hierarchy; album references never mutate media contents."""
from typing import Literal
from fastapi import APIRouter, HTTPException, Depends, Request, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictBool, field_validator
from . import Details, VISIBLE, CoverFraming
from psycopg.types.json import Jsonb
from ..database import connection
from ..database.locks import ALBUM_COLLECTION_HIERARCHY
from ..viewer import media_visible
from app.auth.deps import administrator
from app.auth.deps import reader
from app.auth.deps import Identifier
from face_detect.api import people_client

router = APIRouter(prefix='/api/album-collections')


class CollectionDetails(Details):
    model_config = ConfigDict(extra='forbid')
    show_on_albums: StrictBool | None = None


class CreateCollection(CollectionDetails):
    parent_id: StrictInt | None = Field(default=None, gt=0, le=9007199254740991)


class Parent(BaseModel):
    model_config = ConfigDict(extra='forbid')
    parent_id: StrictInt | None = Field(gt=0, le=9007199254740991)


def hierarchy(db, lock=False):
    # Serialize all structural writes before reading, including concurrent moves.
    if lock:
        db.execute("SET LOCAL lock_timeout='5s'")
        db.execute('SELECT pg_advisory_xact_lock(%s)', (ALBUM_COLLECTION_HIERARCHY,))
    return {row['id']: row for row in db.execute(
        'SELECT id,name,parent_id FROM album_collections ORDER BY id').fetchall()}


def path_to(nodes, cid):
    path = []
    while cid is not None:
        if cid in {row['id'] for row in path}:
            raise HTTPException(409, 'A collection cannot contain itself or an ancestor')
        if cid not in nodes:
            raise HTTPException(404, 'Album collection not found')
        path.append(nodes[cid])
        cid = nodes[cid]['parent_id']
    return list(reversed(path))


def subtree_heights(nodes):
    heights = dict.fromkeys(nodes, 1)
    for key in nodes:
        path = path_to(nodes, key)
        for offset, ancestor in enumerate(path):
            heights[ancestor['id']] = max(heights[ancestor['id']], len(path) - offset)
    return heights


def validate_parent(nodes, cid, parent_id, heights=None):
    if parent_id is not None and parent_id not in nodes:
        raise HTTPException(404, 'Parent collection not found')
    ancestors = path_to(nodes, parent_id)
    if cid in {row['id'] for row in ancestors}:
        raise HTTPException(409, 'A collection cannot contain itself or an ancestor')
    heights = heights if heights is not None else subtree_heights(nodes)
    if len(ancestors) + heights[cid] > 4:
        raise HTTPException(409, 'Collections can be nested up to four levels. This move would place a collection too deep.')


class Members(BaseModel):
    model_config = ConfigDict(extra='forbid')
    album_ids: list[StrictInt] = Field(min_length=1, max_length=10000)

    @field_validator('album_ids')
    @classmethod
    def valid_ids(cls, values):
        if any(not 0 < value <= 9007199254740991 for value in values):
            raise ValueError('Invalid album ID')
        return list(dict.fromkeys(values))


def require(db, cid, lock=False):
    row = db.execute('SELECT * FROM album_collections WHERE id=%s' +
                     (' FOR UPDATE' if lock else ''), (cid,)).fetchone()
    if row is None:
        raise HTTPException(404, 'Album collection not found')
    return row


def list_collections(db, parent_id=None):
    where='c.show_on_albums' if parent_id is None else 'c.parent_id=%s'
    order='c.albums_sort_order' if parent_id is None else 'c.sort_order'
    return db.execute(f'''SELECT c.*,
          (SELECT count(*) FROM album_collection_members cm WHERE cm.collection_id=c.id) album_count,
          (SELECT count(*) FROM album_collection_people cp WHERE cp.collection_id=c.id) person_count,
          (SELECT count(*) FROM album_collections child WHERE child.parent_id=c.id) collection_count,
          coalesce((SELECT m.id FROM media m WHERE m.id=c.cover_media_id AND {media_visible('m')}
            AND NOT EXISTS(SELECT 1 FROM media_trash t WHERE t.media_id=m.id)),
          (SELECT ai.media_id FROM album_collection_members cm
           JOIN album_items ai ON ai.album_id=cm.album_id
           WHERE cm.collection_id=c.id AND {VISIBLE}
           ORDER BY cm.position,cm.album_id,ai.is_cover DESC,ai.position,ai.media_id LIMIT 1)) cover_id
          FROM album_collections c WHERE {where}
          ORDER BY {order} NULLS LAST,c.created_at,c.id''', () if parent_id is None else (parent_id,)).fetchall()


@router.get('')
def index():
    with connection() as db:
        return {'collections': list_collections(db)}


@router.post('')
def create(data: CreateCollection):
    with connection() as db:
        nodes = hierarchy(db, True)
        if data.parent_id is not None and len(path_to(nodes, data.parent_id)) >= 4:
            raise HTTPException(409, 'Level four collections can contain albums and people, but no more collections')
        visible=data.show_on_albums is not False
        if not visible and data.parent_id is None:
            raise HTTPException(409,'Add this collection to a parent collection before hiding it from Albums')
        return db.execute('INSERT INTO album_collections(name,description,parent_id,show_on_albums) VALUES (%s,%s,%s,%s) RETURNING *',
                          (data.name, data.description, data.parent_id,visible)).fetchone()


@router.get('/{cid}/parent-options')
def parent_options(cid: int):
    with connection() as db:
        nodes = hierarchy(db)
        if cid not in nodes:
            raise HTTPException(404, 'Album collection not found')
        choices = []
        heights = subtree_heights(nodes)
        for parent in nodes.values():
            try:
                validate_parent(nodes, cid, parent['id'], heights)
            except HTTPException:
                continue
            choices.append({**parent, 'path': [row['name'] for row in path_to(nodes, parent['id'])]})
        return {'collections': choices}


@router.get('/{cid}/child-options')
def child_options(cid: int):
    with connection() as db:
        nodes = hierarchy(db)
        require(db, cid)
        choices = []
        heights = subtree_heights(nodes)
        for child in nodes.values():
            if child['parent_id'] == cid:
                continue
            try:
                validate_parent(nodes, child['id'], cid, heights)
            except HTTPException:
                continue
            choices.append({**child, 'path': [row['name'] for row in path_to(nodes, child['id'])]})
        return {'collections': choices}


@router.post('/{cid}/parent')
def move(cid: int, data: Parent):
    with connection() as db:
        nodes = hierarchy(db, True)
        require(db, cid)
        validate_parent(nodes, cid, data.parent_id)
        return db.execute('UPDATE album_collections SET sort_order=CASE WHEN parent_id IS DISTINCT FROM %s THEN NULL ELSE sort_order END,parent_id=%s,show_on_albums=CASE WHEN %s THEN true ELSE show_on_albums END,updated_at=now() WHERE id=%s RETURNING *',
                          (data.parent_id, data.parent_id, data.parent_id is None, cid)).fetchone()


@router.get('/{cid}')
def detail(cid: int):
    with connection() as db:
        row = require(db, cid)
        row['breadcrumbs'] = path_to(hierarchy(db), cid)
        row['depth'] = len(row['breadcrumbs'])
        row['person_count'] = db.execute('SELECT count(*) n FROM album_collection_people WHERE collection_id=%s',(cid,)).fetchone()['n']
        row['collections'] = list_collections(db, cid)
        row['albums'] = db.execute(f'''SELECT a.*, stats.item_count,stats.cover_id
          FROM album_collection_members cm JOIN albums a ON a.id=cm.album_id
          CROSS JOIN LATERAL (SELECT count(*) item_count,
            (array_agg(ai.media_id ORDER BY ai.is_cover DESC,ai.position,ai.media_id))[1] cover_id
            FROM album_items ai WHERE ai.album_id=a.id AND {VISIBLE}) stats
          WHERE cm.collection_id=%s ORDER BY cm.position,a.id''', (cid,)).fetchall()
        row['cover_id']=effective_cover(db,row)
        return row


def effective_cover(db,row):
    if row['cover_media_id'] and db.execute('SELECT 1 FROM media WHERE id=%s AND '+media_visible('media')+' AND NOT EXISTS '
            '(SELECT 1 FROM media_trash WHERE media_id=%s)',(row['cover_media_id'],row['cover_media_id'])).fetchone():
        return row['cover_media_id']
    result=db.execute(f'''SELECT ai.media_id FROM album_collection_members cm
        JOIN album_items ai ON ai.album_id=cm.album_id WHERE cm.collection_id=%s AND {VISIBLE}
        ORDER BY cm.position,cm.album_id,ai.is_cover DESC,ai.position,ai.media_id LIMIT 1''',(row['id'],)).fetchone()
    return result['media_id'] if result else None


class CollectionCover(BaseModel):
    kind:Literal['album','person']
    source_id:StrictInt=Field(gt=0,le=9007199254740991)
    media_id:StrictInt=Field(gt=0,le=9007199254740991)
    pilot:str|None=Field(default=None,pattern=r'^[0-9a-f]{32}$')


@router.post('/{cid}/cover')
async def set_collection_cover(cid:Identifier,data:CollectionCover,request:Request,user=Depends(reader)):
    if data.kind=='person':
        if user.get('role')!='admin':raise HTTPException(403,'Administrator access required')
        pilot,result=await people_read(request,'named-people',data.pilot)
        if not any(p['person_id']==data.source_id and p.get('cover_media_id')==data.media_id for p in result['people']):
            raise HTTPException(409,'This person cover changed. Reopen Choose cover.')
    with connection() as db:
        require(db,cid,True)
        if data.kind=='album':
            found=db.execute(f'''SELECT ai.media_id FROM album_collection_members cm
              JOIN album_items ai ON ai.album_id=cm.album_id WHERE cm.collection_id=%s AND cm.album_id=%s AND {VISIBLE}
              ORDER BY ai.is_cover DESC,ai.position,ai.media_id LIMIT 1''',(cid,data.source_id)).fetchone()
            valid=found and found['media_id']==data.media_id
        else:
            valid=db.execute('SELECT 1 FROM album_collection_people WHERE collection_id=%s AND person_id=%s AND pilot=%s',
                             (cid,data.source_id,pilot)).fetchone()
        if not valid:raise HTTPException(409,'The cover source changed. Reopen Choose cover.')
        if not db.execute('SELECT id FROM media WHERE id=%s AND '+media_visible('media')+' FOR SHARE',(data.media_id,)).fetchone():
            raise HTTPException(404,'Photo no longer available')
        if db.execute('SELECT 1 FROM media_trash WHERE media_id=%s',(data.media_id,)).fetchone():
            raise HTTPException(409,'Restore the photo before using it as a cover')
        db.execute("UPDATE album_collections SET cover_media_id=%s,cover_framing=CASE WHEN cover_media_id=%s THEN cover_framing ELSE '{}'::jsonb END,updated_at=now() WHERE id=%s",
                   (data.media_id,data.media_id,cid))
    return detail(cid)


@router.patch('/{cid}/cover-framing')
def frame_collection_cover(cid:Identifier,data:CoverFraming):
    with connection() as db:
        row=require(db,cid,True)
        if effective_cover(db,row)!=data.media_id:
            raise HTTPException(409,'The collection cover changed. Reopen Adjust cover.')
        # Adjusting an automatic cover pins it, independent of album membership.
        db.execute('UPDATE album_collections SET cover_media_id=%s,cover_framing=%s,updated_at=now() WHERE id=%s',
                   (data.media_id,Jsonb(data.model_dump()),cid))
    return detail(cid)


@router.patch('/{cid}')
def edit(cid: int, data: CollectionDetails):
    with connection() as db:
        hierarchy(db,True)
        current=require(db, cid, True)
        if data.show_on_albums is False and current['parent_id'] is None:
            raise HTTPException(409,'Add this collection to a parent collection before hiding it from Albums')
        if data.show_on_albums is not None:
            db.execute('UPDATE album_collections SET show_on_albums=%s WHERE id=%s',(data.show_on_albums,cid))
        return db.execute('UPDATE album_collections SET name=%s,description=%s,updated_at=now() '
                          'WHERE id=%s RETURNING *', (data.name, data.description, cid)).fetchone()


@router.delete('/{cid}')
def delete(cid: int):
    with connection() as db:
        hierarchy(db, True)
        # Lock children before albums: deleting the parent promotes these rows.
        db.execute('SELECT id FROM album_collections WHERE id=%s OR parent_id=%s ORDER BY id FOR UPDATE',(cid,cid)).fetchall()
        require(db, cid, True)
        ids=[row['album_id'] for row in db.execute('SELECT album_id FROM album_collection_members WHERE collection_id=%s',(cid,)).fetchall()]
        lock_albums(db,ids)
        db.execute('UPDATE album_collections SET sort_order=NULL,show_on_albums=true WHERE parent_id=%s',(cid,))
        db.execute('DELETE FROM album_collections WHERE id=%s', (cid,))
        restore_uncontained(db,ids)
    return {'ok': True}


def lock_albums(db,ids):
    db.execute('SELECT id FROM albums WHERE id=ANY(%s) ORDER BY id FOR UPDATE',(ids,)).fetchall()


def restore_uncontained(db,ids):
    db.execute('''UPDATE albums a SET show_on_albums=true WHERE a.id=ANY(%s)
        AND NOT a.show_on_albums AND NOT EXISTS
        (SELECT 1 FROM album_collection_members cm WHERE cm.album_id=a.id)''',(ids,))


@router.post('/{cid}/albums')
def add(cid: int, data: Members):
    with connection() as db:
        require(db, cid, True)
        rows = db.execute('SELECT id FROM albums WHERE id=ANY(%s) ORDER BY id FOR KEY SHARE',
                          (data.album_ids,)).fetchall()
        if len(rows) != len(data.album_ids):
            raise HTTPException(404, 'One or more albums no longer exist')
        position = db.execute('SELECT coalesce(max(position),0) p FROM album_collection_members '
                              'WHERE collection_id=%s', (cid,)).fetchone()['p']
        added = db.execute('''INSERT INTO album_collection_members(collection_id,album_id,position)
          SELECT %s,id,%s+ordinal FROM unnest(%s::bigint[]) WITH ORDINALITY AS source(id,ordinal)
          ON CONFLICT DO NOTHING''', (cid, position, data.album_ids)).rowcount
        db.execute('UPDATE album_collections SET updated_at=now() WHERE id=%s', (cid,))
        return {'added': added}


@router.post('/{cid}/remove')
def remove(cid: int, data: Members):
    with connection() as db:
        require(db, cid, True)
        lock_albums(db,data.album_ids)
        removed = db.execute('DELETE FROM album_collection_members WHERE collection_id=%s '
                             'AND album_id=ANY(%s)', (cid, data.album_ids)).rowcount
        restore_uncontained(db,data.album_ids)
        db.execute('UPDATE album_collections SET updated_at=now() WHERE id=%s', (cid,))
        return {'removed': removed}


class PersonLinks(BaseModel):
    model_config = ConfigDict(extra='forbid')
    pilot: str = Field(pattern=r'^[0-9a-f]{32}$')
    person_ids: list[StrictInt] = Field(min_length=1,max_length=10000)

    @field_validator('person_ids')
    @classmethod
    def valid_people(cls,values):
        if any(not 0<value<=9007199254740991 for value in values):
            raise ValueError('Invalid person ID')
        return list(dict.fromkeys(values))


async def people_read(request,path,pilot=None):
    # Stable patch point: read-only previews and tests replace this name.
    return await people_client.read(request,path,pilot)


@router.get('/{cid}/person-options')
async def person_options(cid:Identifier,request:Request,user=Depends(administrator)):
    with connection() as db:
        require(db,cid)
        links=db.execute('SELECT pilot,person_id FROM album_collection_people WHERE collection_id=%s',(cid,)).fetchall()
    pilot,result=await people_read(request,'named-people')
    return {'pilot':pilot,'included':[p['person_id'] for p in links if p['pilot']==pilot],**result}


@router.get('/{cid}/people')
async def linked_people(cid:Identifier,request:Request,user=Depends(administrator)):
    with connection() as db:
        require(db,cid)
        links=db.execute('SELECT pilot,person_id FROM album_collection_people WHERE collection_id=%s ORDER BY position,person_id',(cid,)).fetchall()
    if not links:return {'people':[]}
    warning=None
    try:pilot,result=await people_read(request,'named-people')
    except HTTPException as error:pilot,result=None,{'people':[]};warning=error.detail
    people={p['person_id']:p for p in result['people']}
    return {'people':[{**link,**(people.get(link['person_id'],{}) if link['pilot']==pilot else {}),
                      'available':link['pilot']==pilot and link['person_id'] in people} for link in links],
            'warning':warning}


@router.post('/{cid}/people')
async def add_people(cid:Identifier,data:PersonLinks,request:Request,user=Depends(administrator)):
    _,result=await people_read(request,'named-people',data.pilot)
    if not set(data.person_ids)<={p['person_id'] for p in result['people']}:
        raise HTTPException(409,'A selected person is no longer available. Reopen Add people to choose again.')
    with connection() as db:
        require(db,cid,True)
        position=db.execute('SELECT coalesce(max(position),0) p FROM album_collection_people WHERE collection_id=%s',(cid,)).fetchone()['p']
        added=db.execute('''INSERT INTO album_collection_people(collection_id,pilot,person_id,position)
          SELECT %s,%s,id,%s+ordinal FROM unnest(%s::bigint[]) WITH ORDINALITY AS source(id,ordinal)
          ON CONFLICT DO NOTHING''',(cid,data.pilot,position,data.person_ids)).rowcount
    return {'added':added}


@router.post('/{cid}/remove-people')
def remove_people(cid:Identifier,data:PersonLinks,user=Depends(administrator)):
    with connection() as db:
        require(db,cid,True)
        removed=db.execute('DELETE FROM album_collection_people WHERE collection_id=%s AND pilot=%s AND person_id=ANY(%s)',
                           (cid,data.pilot,data.person_ids)).rowcount
    return {'removed':removed}


@router.get('/{cid}/people/{pid}/photos')
async def linked_photos(cid:Identifier,pid:Identifier,request:Request,
                        pilot:str=Query(pattern=r'^[0-9a-f]{32}$'),user=Depends(administrator)):
    with connection() as db:
        require(db,cid)
        if not db.execute('SELECT 1 FROM album_collection_people WHERE collection_id=%s AND pilot=%s AND person_id=%s',(cid,pilot,pid)).fetchone():
            raise HTTPException(404,'This person is no longer in the collection')
    _,result=await people_read(request,f'people/{pid}/photos',pilot)
    return result


class CardPosition(BaseModel):
    model_config=ConfigDict(extra='forbid')
    kind:Literal['album','collection','person']
    id:StrictInt=Field(gt=0,le=9007199254740991)
    target_id:StrictInt=Field(gt=0,le=9007199254740991)
    after:StrictBool=False
    pilot:str|None=Field(default=None,pattern=r'^[0-9a-f]{32}$')
    target_pilot:str|None=Field(default=None,pattern=r'^[0-9a-f]{32}$')


def reorder_cards(db,cid,data):
    if data.kind=='collection':
        hierarchy(db,True)
    if cid is not None:require(db,cid,True)
    if data.kind=='collection':
        if cid is None:
            rows=db.execute('SELECT id FROM album_collections WHERE show_on_albums ORDER BY albums_sort_order NULLS LAST,created_at,id').fetchall()
        else:
            rows=db.execute('SELECT id FROM album_collections WHERE parent_id=%s ORDER BY sort_order NULLS LAST,created_at,id',(cid,)).fetchall()
    elif data.kind=='album':
        rows=db.execute('SELECT album_id id FROM album_collection_members WHERE collection_id=%s ORDER BY position,album_id',(cid,)).fetchall()
    else:
        if not data.pilot or not data.target_pilot:raise HTTPException(422,'People catalog identity is required')
        rows=db.execute('SELECT person_id id,pilot FROM album_collection_people WHERE collection_id=%s ORDER BY position,person_id,pilot',(cid,)).fetchall()
    source={'id':data.id};target={'id':data.target_id}
    if data.kind=='person':source['pilot']=data.pilot;target['pilot']=data.target_pilot
    if source not in rows or target not in rows:
        raise HTTPException(409,'An item is no longer in this collection. Reopen the collection and try again.')
    if source!=target:
        rows.remove(source);rows.insert(rows.index(target)+int(data.after),source)
        if data.kind=='person':
            db.execute('''UPDATE album_collection_people p SET position=o.position
              FROM unnest(%s::bigint[],%s::text[]) WITH ORDINALITY AS o(id,pilot,position)
              WHERE p.collection_id=%s AND p.person_id=o.id AND p.pilot=o.pilot''',
              ([r['id'] for r in rows],[r['pilot'] for r in rows],cid))
        elif data.kind=='album':
            db.execute('''UPDATE album_collection_members m SET position=o.position
              FROM unnest(%s::bigint[]) WITH ORDINALITY AS o(id,position)
              WHERE m.collection_id=%s AND m.album_id=o.id''',([r['id'] for r in rows],cid))
        else:
            column='albums_sort_order' if cid is None else 'sort_order'
            db.execute(f'''UPDATE album_collections c SET {column}=o.position
              FROM unnest(%s::bigint[]) WITH ORDINALITY AS o(id,position)
              WHERE c.id=o.id''',([r['id'] for r in rows],))
    return {'order':rows}


@router.post('/order/root')
def order_root(data:CardPosition,user=Depends(reader)):
    if data.kind!='collection':raise HTTPException(422,'Only top-level collections can be ordered here')
    with connection() as db:return reorder_cards(db,None,data)


@router.post('/{cid}/order')
def order_contents(cid:Identifier,data:CardPosition,user=Depends(reader)):
    if data.kind=='person' and user['role']!='admin':raise HTTPException(403,'People requires administrator access')
    with connection() as db:return reorder_cards(db,cid,data)
