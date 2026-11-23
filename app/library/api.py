"""Library listing, media files and per-photo edits."""
from typing import Literal
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator
from fastapi.responses import FileResponse, Response
from ..config import DATA, load_config
from ..database import connection
from .media import safe_media_path
from ..photos.thumbnails import find_thumbnail
from ..telemetry import logger
from ..viewer import media_visible
from ..jobs.job_access import upload_clause, visible_job

log=logger('web')
router=APIRouter()

@router.get('/api/media')
def library(page:int=1,kind:str='all',search:str='',year:str='all',import_job:str='all',album:int|None=None,page_size:int=Query(default=1000,ge=1,le=1000)):
    return library_data(page,kind,search,year,import_job,album,page_size=page_size)

def library_conditions(kind='all',search='',year='all',import_job='all',album=None):
    clauses=[media_visible('m'),"EXISTS(SELECT 1 FROM media_trash t WHERE t.media_id=m.id AND t.deleted_at IS NULL)" if kind=='trash' else "NOT EXISTS(SELECT 1 FROM media_trash t WHERE t.media_id=m.id)"]; values=[]
    if kind!='trash':clauses.append("left(m.name,1)<>'.'")
    if album is not None:
        clauses.append('EXISTS(SELECT 1 FROM album_items ai WHERE ai.album_id=%s AND ai.media_id=m.id)');values.append(album)
    if kind in ('image','video'):
        clauses.append('m.kind=%s');values.append(kind)
    if import_job!='all':
        if len(import_job)>16 or not import_job.isascii() or not import_job.isdecimal() or not 0<int(import_job)<=9007199254740991:
            raise HTTPException(422,'Import must be a positive import ID or all')
        clauses.append('EXISTS(SELECT 1 FROM import_items i WHERE i.media_id=m.id AND i.job_id=%s)')
        values.append(int(import_job))
    if search:
        clauses.append('(m.name ILIKE %s OR EXISTS(SELECT 1 FROM import_items i WHERE i.media_id=m.id AND i.source_path ILIKE %s))')
        values.extend(['%'+search+'%']*2)
    if year=='unknown':
        clauses.append('m.captured_at IS NULL')
    elif len(year)==4 and year.isdigit():
        clauses.append('m.captured_at >= %s AND m.captured_at < %s');values.extend([f'{year}-01-01',f'{int(year)+1}-01-01'])
    where=' WHERE '+' AND '.join(clauses) if clauses else ''
    return where,values

def library_data(page=1,kind='all',search='',year='all',import_job='all',album=None,*,page_size=1000):
    page=max(1,page)
    where,values=library_conditions(kind,search,year,import_job,album)
    with connection() as db:
        if album is not None:
            from ..albums import require
            require(db,album)
        hide,hide_values=upload_clause()
        imports=db.execute('SELECT j.id,j.source_label,j.source_root,j.source_kind FROM jobs j LEFT JOIN upload_sessions u ON u.job_id=j.id'+(' WHERE '+hide if hide else '')+' ORDER BY j.id DESC',hide_values).fetchall()
        if import_job!='all' and not any(j['id']==int(import_job) for j in imports):
            raise HTTPException(404,'Import not found')
        total=db.execute('SELECT count(*) total FROM media m'+where,values).fetchone()['total']
        pages=max(1,(total+page_size-1)//page_size);page=min(page,pages)
        items=db.execute('SELECT m.*, (SELECT crop FROM media_adjustments a WHERE a.media_id=m.id) crop, coalesce((SELECT rotation FROM media_adjustments a WHERE a.media_id=m.id),0) rotation, (SELECT trashed_at FROM media_trash t WHERE t.media_id=m.id) trashed_at, (SELECT deleting_at FROM media_trash t WHERE t.media_id=m.id) deleting_at, v.state video_state,v.metadata video_metadata,v.error video_error, (SELECT source_path FROM import_items WHERE media_id=m.id ORDER BY id LIMIT 1) source_path FROM media m LEFT JOIN video_metadata v ON v.media_id=m.id'+where+(' ORDER BY (SELECT position FROM album_items ai WHERE ai.album_id='+str(int(album))+' AND ai.media_id=m.id),m.id' if album is not None else ' ORDER BY trashed_at DESC,m.id DESC' if kind=='trash' else ' ORDER BY m.captured_at DESC NULLS LAST,m.id DESC')+' LIMIT %s OFFSET %s',values+[page_size,(page-1)*page_size]).fetchall()
        stats=db.execute('SELECT count(*) total,coalesce(sum(size),0) bytes FROM media m'+where,values).fetchone()
        years=db.execute('SELECT DISTINCT extract(year from captured_at)::integer AS capture_year FROM media m WHERE captured_at IS NOT NULL AND '+media_visible('m')+' AND NOT EXISTS(SELECT 1 FROM media_trash t WHERE t.media_id=m.id) ORDER BY capture_year DESC').fetchall()
    choices=[{'id':j['id'],'label':j['source_label'] or j['source_root'].replace('\\','/').rstrip('/').split('/')[-1],'source_kind':j['source_kind']} for j in imports]
    return {'items':items,'total':total,'page':page,'pages':pages,'page_size':page_size,'stats':stats,'years':[r['capture_year'] for r in years],'imports':choices}

def recovered_photo_thumbnail(config,media):
    """A backup leaves out thumbnails, and a read-only preview can't save new ones, so draw them in memory."""
    from ..migration.people_thumbnail import crop_thumbnail
    try:
        path=safe_media_path(config['destination']['root'],media['relative_path'])
        payload=crop_thumbnail(path,media['sha256'],media['crop'] or {'x':0,'y':0,'width':1,'height':1})
    except (OSError,ValueError,KeyError):
        raise HTTPException(404,'Preview unavailable; original retained') from None
    return Response(payload,media_type='image/jpeg',headers={'Cache-Control':'no-store'})

@router.get('/api/media/{id}/{variant}')
def media_file(id:int,variant:str,disambiguate:bool=False):
    with connection() as db:
        media=db.execute('SELECT media.*, (SELECT crop FROM media_adjustments a WHERE a.media_id=media.id) crop FROM media WHERE id=%s AND '+media_visible('media')+' AND NOT EXISTS(SELECT 1 FROM media_trash t WHERE t.media_id=media.id AND t.deleting_at IS NOT NULL)',(id,)).fetchone()
    if not media:
        raise HTTPException(404,'Media not found')
    if variant=='thumbnail':
        path=find_thumbnail(DATA/'thumbnails',media['sha256'])
        if not path.is_file():
            config=load_config()
            if config.get('read_only_preview') is True and media['kind']=='image':
                return recovered_photo_thumbnail(config,media)
            raise HTTPException(404,'Preview unavailable; original retained')
        if media['crop']:
            from PIL import Image
            from io import BytesIO
            from ..photos.crop import pixel_box
            with Image.open(path) as image:
                cropped=image.crop(pixel_box(media['crop'],*image.size)).convert('RGB')
                output=BytesIO();cropped.save(output,format='JPEG',quality=90)
            return Response(output.getvalue(),media_type='image/jpeg')
    elif variant=='preview':
        from ..photos.heif import is_heif
        if media['kind']!='image' or not is_heif(media['relative_path']):
            raise HTTPException(404,'No converted preview for this media')
        path=find_thumbnail(DATA/'previews',media['sha256'])
        if not path.is_file():
            raise HTTPException(404,'Preview unavailable; original retained')
    elif variant in ('original','download'):
        config=load_config()
        from ..storage.disks import volume_serial
        try:
            if volume_serial(config['destination']['drive']) != config['destination']['volume_serial']:
                raise OSError('Wrong destination volume')
            path=safe_media_path(config['destination']['root'],media['relative_path'])
            if not path.is_file():
                raise OSError('Media unavailable')
        except OSError:
            raise HTTPException(503,'Destination disk offline or changed')
    else:
        raise HTTPException(404,'Unknown media variant')
    if variant=='download':
        from pathlib import PureWindowsPath
        from .names import download_name
        name=download_name(media)
        if disambiguate:
            original=PureWindowsPath(name)
            name=f'{original.stem} [{id}]{original.suffix}'
        return FileResponse(path,filename=name,content_disposition_type='attachment')
    if variant=='original' and media['kind']=='video':
        with connection() as db:
            audio=db.execute("SELECT metadata FROM video_metadata WHERE media_id=%s AND metadata->>'media_type'='audio'",(id,)).fetchone()
        if audio:
            fmt=audio['metadata'].get('format','').split(',')[0]
            mime={'aac':'audio/aac','mp3':'audio/mpeg','mov':'audio/mp4','wav':'audio/wav','flac':'audio/flac','ogg':'audio/ogg','aiff':'audio/aiff','asf':'audio/x-ms-wma'}.get(fmt,'application/octet-stream')
            return FileResponse(path,media_type=mime)
    return FileResponse(path)

@router.post('/api/media/{id}/rotate')
def rotate(id:int):
    with connection() as db:
        media=db.execute('SELECT kind FROM media WHERE id=%s AND '+media_visible('media')+' FOR UPDATE',(id,)).fetchone()
        if not media:
            raise HTTPException(404,'Media not found')
        if media['kind'] not in ('image','video'):
            raise HTTPException(400,'Only photos and videos can be rotated')
        if db.execute('SELECT 1 FROM media_trash WHERE media_id=%s AND deleting_at IS NOT NULL',(id,)).fetchone():
            raise HTTPException(409,'Deletion has already started')
        return db.execute('INSERT INTO media_adjustments(media_id,rotation) VALUES (%s,90) ON CONFLICT(media_id) DO UPDATE SET rotation=(media_adjustments.rotation+90)%%360 RETURNING rotation',(id,)).fetchone()

@router.post('/api/media/{id}/crop')
async def crop_image(id:int,request:Request):
    from ..photos.crop import validate_crop
    from psycopg.types.json import Jsonb
    try:
        crop=validate_crop(await request.json())
    except (ValueError,TypeError) as error:
        raise HTTPException(422,str(error))
    with connection() as db:
        media=db.execute('SELECT kind FROM media WHERE id=%s AND '+media_visible('media')+' FOR UPDATE',(id,)).fetchone()
        if not media:
            raise HTTPException(404,'Media not found')
        if media['kind']!='image':
            raise HTTPException(400,'Only photos can be cropped')
        if db.execute('SELECT 1 FROM media_trash WHERE media_id=%s AND deleting_at IS NOT NULL',(id,)).fetchone():
            raise HTTPException(409,'Deletion has already started')
        db.execute('INSERT INTO media_adjustments(media_id,crop) VALUES (%s,%s) ON CONFLICT(media_id) DO UPDATE SET crop=excluded.crop',(id,Jsonb(crop) if crop else None))
    return {'crop':crop}

@router.post('/api/media/{id}/trash/{action}')
def trash_action(id:int,action:str,request:Request):
    from .trash import change
    with connection() as db:
        try:
            return change(db,id,action,load_config(),deleted_by=request.state.user['username'])
        except (OSError,ValueError) as error:
            log.exception('trash-safety-fault media=%s',id)
            raise HTTPException(409, 'Deletion stopped for review: '+str(error))


class VisibilityChange(BaseModel):
    """Either chosen items or every photo and video one import/upload brought in."""
    model_config=ConfigDict(extra='forbid')
    visibility:Literal['shared','private']
    media_ids:list[StrictInt]|None=Field(default=None,min_length=1,max_length=10000)
    import_job:StrictInt|None=Field(default=None,gt=0)
    @model_validator(mode='after')
    def one_target(self):
        if (self.media_ids is None)==(self.import_job is None):raise ValueError('Choose items or one import, not both')
        if self.media_ids is not None and any(not 0<i<=9007199254740991 for i in self.media_ids):raise ValueError('Invalid media ID')
        return self

@router.post('/api/media/visibility')
def change_visibility(data:VisibilityChange,request:Request):
    """Owners change their own items; administrators may change any, including imports."""
    user=request.state.user
    everyone=user['role']=='admin'
    with connection() as db:
        if data.import_job is not None:
            if not visible_job(db,data.import_job):raise HTTPException(404,'Import not found')
            where='m.first_import_job_id=%s';target=data.import_job
        else:
            where='m.id=ANY(%s)';target=list(dict.fromkeys(data.media_ids))
        rows=db.execute(f'SELECT m.id,m.owner_name FROM media m WHERE {where} AND '+media_visible('m')+' FOR UPDATE',(target,)).fetchall()
        if data.media_ids is not None and len(rows)!=len(target):raise HTTPException(404,'One or more items are unavailable')
        if not everyone and any(r['owner_name']!=user['username'] for r in rows):
            raise HTTPException(403,'You can only change the visibility of your own photos and videos')
        ids=[r['id'] for r in rows]
        # An owner-less (imported) item made private is kept by the administrator who hid it.
        db.execute("UPDATE media SET visibility=%s,owner_name=CASE WHEN %s='private' THEN coalesce(owner_name,%s) ELSE owner_name END WHERE id=ANY(%s)",(data.visibility,data.visibility,user['username'],ids))
    return {'updated':len(ids),'visibility':data.visibility}
