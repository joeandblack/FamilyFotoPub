"""Upload API for administrators and regular accounts; bounded manifests and streamed chunk bodies."""
import asyncio
import json
from fastapi import APIRouter, Request, HTTPException
from psycopg.errors import LockNotAvailable
from .. import uploads

router=APIRouter(prefix='/api/uploads')
settings_router=APIRouter(prefix='/api/upload-settings')


def owner(request):
    user=request.state.user
    if not user or user['role'] not in ('admin','regular'):raise HTTPException(403,'Read-only accounts cannot upload')
    if request.method!='GET' and request.headers.get('x-familyfoto-upload')!='1':
        raise HTTPException(403,'Upload request header required')
    return user['username']


def is_admin(request):
    return request.state.user['role']=='admin'


async def payload(request,maximum=8*1024**2):
    data=bytearray()
    async for chunk in request.stream():
        if len(data)+len(chunk)>maximum:raise HTTPException(413,'Upload manifest too large')
        data.extend(chunk)
    try:return json.loads(data)
    except (ValueError,UnicodeError):raise HTTPException(422,'JSON object required')


async def call(fn,*args):
    try:return await asyncio.to_thread(fn,*args)
    except (ValueError,OSError,RuntimeError,LockNotAvailable) as error:raise HTTPException(409,str(error))


@router.get('')
async def list_uploads(request:Request):
    who=owner(request);every=is_admin(request)
    def read():
        with uploads.connection() as db:
            # Administrators see everyone's batches; everyone else only their own.
            rows=db.execute('SELECT * FROM upload_sessions'+('' if every else ' WHERE owner_name=%s')+' ORDER BY created_at DESC LIMIT 50',() if every else (who,)).fetchall()
            sessions=[]
            for row in rows:
                summary=uploads.status(db,row)
                summary['counts']={key:sum(f['disposition']==key for f in summary['files']) for key in ('accepted','ignored','unsupported')}
                summary['received']=sum(f['received'] for f in summary.pop('files'))
                sessions.append(summary)
            return {'limits':uploads.limits(),'sessions':sessions}
    return await call(read)


@router.post('')
async def start(request:Request):
    who=owner(request)
    return await call(uploads.create,who,await payload(request))


@router.get('/{sid}')
async def get(sid:str,request:Request):
    who=owner(request);every=is_admin(request)
    def read():
        with uploads.connection() as db:return uploads.status(db,uploads.owned(db,sid,who,admin=every))
    return await call(read)


@router.put('/{sid}/files/{file_id}')
async def chunk(sid:str,file_id:int,request:Request,offset:int):
    who=owner(request)
    try:
        return await asyncio.wait_for(uploads.chunk(who,sid,file_id,offset,request.headers.get('x-content-sha256',''),request.stream()),timeout=120)
    except asyncio.TimeoutError:raise HTTPException(408,'Chunk timed out; refresh status before retrying')
    except (ValueError,OSError,RuntimeError,LockNotAvailable) as error:raise HTTPException(409,str(error))


@router.post('/{sid}/files/{file_id}/verify')
async def verify(sid:str,file_id:int,request:Request):
    who=owner(request);data=await payload(request,1024)
    if not isinstance(data,dict):raise HTTPException(422,'Checksum object required')
    return await call(uploads.verify_file,who,sid,file_id,data.get('sha256'))


@router.post('/{sid}/files/{file_id}/reset')
async def reset(sid:str,file_id:int,request:Request):
    return await call(uploads.reset_file,owner(request),sid,file_id)


@router.post('/{sid}/finalize')
async def finalize(sid:str,request:Request):return await call(uploads.finalize,owner(request),sid)


@router.post('/{sid}/cancel')
async def cancel(sid:str,request:Request):return await call(uploads.cancel,owner(request),sid,is_admin(request))


@router.post('/{sid}/retry')
async def retry(sid:str,request:Request):return await call(uploads.retry,owner(request),sid,is_admin(request))


def administrator(request):
    user=request.state.user
    if not user or user['role']!='admin':raise HTTPException(403,'Administrator required')
    return user['username']


@settings_router.get('')
async def read_settings(request:Request):
    administrator(request)
    return await call(uploads.settings_view)


@settings_router.put('')
async def save_settings(request:Request):
    who=administrator(request)
    values=await payload(request,4096)
    def write():
        with uploads.connection() as db:uploads.save_limits(db,values,who)
        return uploads.settings_view()
    return await call(write)


@settings_router.delete('')
async def reset_settings(request:Request):
    administrator(request)
    def write():
        with uploads.connection() as db:uploads.reset_limits(db)
        return uploads.settings_view()
    return await call(write)
