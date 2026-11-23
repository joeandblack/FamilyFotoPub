"""One bounded-memory, ZIP64 download for an immutable selection of catalog IDs."""
import base64
from contextlib import ExitStack
import hashlib
import hmac
import json
import os
from pathlib import PureWindowsPath
import re
import secrets
import struct
import time
import zlib
import anyio
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool
from ..config import load_config
from ..database import connection
from .names import download_name
from ..storage.disks import volume_serial
from .media import safe_media_path
from .locks import MEDIA_LOCK_NAMESPACE, content_key
from ..viewer import media_visible

router=APIRouter(prefix='/api/downloads')
CHUNK=64*1024

def selection(value):
    parts=value.split(',')
    if not 2<=len(parts)<=1000 or any(not re.fullmatch(r'[1-9][0-9]{0,15}',p) for p in parts):
        raise HTTPException(422,'Select between 2 and 1000 items for a ZIP download')
    ids=list(dict.fromkeys(map(int,parts)))
    if len(ids)<2 or any(i>9007199254740991 for i in ids):raise HTTPException(422,'Invalid selection')
    return ids

def filename(media):
    path=PureWindowsPath(download_name(media))
    stem=re.sub(r'[\x00-\x1f\x7f<>:"/\\|?*]','_',path.stem).strip(' .')[:140] or 'media'
    suffix=re.sub(r'[^.a-zA-Z0-9]','_',path.suffix)[:20]
    return f'{stem} [{media["id"]}]{suffix}'

class Archive:
    def __init__(self,ids):
        self.stack=ExitStack();self.files=[];self.closed=False
        try:
            config=load_config();destination=config['destination']
            if volume_serial(destination['drive'])!=destination['volume_serial']:raise OSError('Destination disk offline or changed')
            db=self.stack.enter_context(connection())
            rows=db.execute('SELECT id,sha256 FROM media WHERE id=ANY(%s) AND '+media_visible('media'),(ids,)).fetchall()
            if len(rows)!=len(ids):raise HTTPException(404,'Some selected items no longer exist. Refresh and select again.')
            # Match the deletion/conversion content locks, without waiting on active work.
            for key in sorted({content_key(r['sha256']) for r in rows}):
                if not db.execute('SELECT pg_try_advisory_lock_shared(%s,%s) ok',(MEDIA_LOCK_NAMESPACE,key)).fetchone()['ok']:
                    raise HTTPException(409,'A selected item is being processed. Try again when it finishes.')
            rows=db.execute('''SELECT m.*,t.deleting_at,t.deleted_at FROM media m LEFT JOIN media_trash t ON t.media_id=m.id
              WHERE m.id=ANY(%s) AND '''+media_visible('m')+''' ORDER BY m.id''',(ids,)).fetchall()
            lookup={r['id']:r for r in rows}
            for mid in ids:
                m=lookup.get(mid)
                if not m or m['deleting_at'] or m['deleted_at']:raise HTTPException(409,'A selected item is being permanently deleted. Refresh and select again.')
                path=safe_media_path(destination['root'],m['relative_path'])
                file=self.stack.enter_context(path.open('rb'));size=os.fstat(file.fileno()).st_size
                self.files.append((filename(m).encode('utf-8'),file,size))
            db.commit() # Session content locks remain until close; no long-running transaction.
            self.length=98+sum(size+148+2*len(name) for name,_,size in self.files)
        except HTTPException:
            self.close();raise
        except (OSError,ValueError):
            self.close();raise HTTPException(503,'A selected file is unavailable or the destination disk changed. No ZIP was started.')
        except BaseException:
            self.close();raise

    def close(self):
        if not self.closed:self.closed=True;self.stack.close()

    def chunks(self):
        """STORE avoids recompressing media. All entries use ZIP64 and UTF-8 names."""
        offset=0;directory=[]
        for name,file,size in self.files:
            local=struct.pack('<IHHHHHIIIHH',0x04034b50,45,0x808,0,0,33,0,0xffffffff,0xffffffff,len(name),20)+name+struct.pack('<HHQQ',1,16,size,size)
            start=offset;offset+=len(local);yield local
            crc=0;remaining=size
            while remaining:
                chunk=file.read(min(CHUNK,remaining))
                if not chunk:raise OSError('Selected file changed or became unavailable during download')
                remaining-=len(chunk);offset+=len(chunk);crc=zlib.crc32(chunk,crc);yield chunk
            descriptor=struct.pack('<IIQQ',0x08074b50,crc,size,size);offset+=len(descriptor);yield descriptor
            central=struct.pack('<IHHHHHHIIIHHHHHII',0x02014b50,45,45,0x808,0,0,33,crc,0xffffffff,0xffffffff,len(name),28,0,0,0,0,0xffffffff)+name+struct.pack('<HHQQQ',1,24,size,size,start)
            directory.append(central)
        directory_start=offset
        for entry in directory:offset+=len(entry);yield entry
        directory_size=offset-directory_start;count=len(directory)
        yield struct.pack('<IQHHIIQQQQ',0x06064b50,44,45,45,0,0,count,count,directory_size,directory_start)
        yield struct.pack('<IIQI',0x07064b50,0,offset,1)
        yield struct.pack('<IHHHHIIH',0x06054b50,0,0,0xffff,0xffff,0xffffffff,0xffffffff,0)

class ArchiveResponse(StreamingResponse):
    def __init__(self,archive):
        self.archive=archive
        super().__init__(archive.chunks(),media_type='application/zip',headers={
            'Content-Disposition':f'attachment; filename="FamilyFoto-{len(archive.files)}-items-{secrets.token_hex(4)}.zip"',
            'Content-Length':str(archive.length),'Cache-Control':'no-store'})
    async def __call__(self,scope,receive,send):
        try:await super().__call__(scope,receive,send)
        finally:
            with anyio.CancelScope(shield=True):await run_in_threadpool(self.archive.close)

def sign(payload):
    from ..auth import signing_key
    key=signing_key(load_config())
    return hmac.new(key,payload.encode(),hashlib.sha256).hexdigest()

def token_for(ids,user):
    payload=base64.urlsafe_b64encode(json.dumps({'ids':ids,'user':user['username'],'role':user['role'],'expires':int(time.time())+600,'nonce':secrets.token_hex(8)},separators=(',',':')).encode()).decode()
    return payload+'.'+sign(payload)

def token_ids(token,user):
    try:
        if len(token)>24000:raise ValueError()
        payload,signature=token.split('.')
        if not hmac.compare_digest(signature,sign(payload)):raise ValueError()
        data=json.loads(base64.urlsafe_b64decode(payload))
        if data['expires']<time.time() or data['user']!=user['username'] or data['role']!=user['role']:raise ValueError()
        return selection(','.join(map(str,data['ids'])))
    except (ValueError,KeyError,TypeError):raise HTTPException(403,'Download expired. Select the items and prepare a new ZIP.')

@router.get('/prepare')
def prepare(request:Request,ids:str):
    chosen=selection(ids);archive=Archive(chosen)
    try:return {'url':'/api/downloads/archive?token='+token_for(chosen,request.state.user),'count':len(chosen),'bytes':sum(s for _,_,s in archive.files)}
    finally:archive.close()

@router.get('/archive')
async def download(request:Request,token:str):
    try:
        chosen=token_ids(token,request.state.user)
        archive=await run_in_threadpool(Archive,chosen)
        return ArchiveResponse(archive)
    except HTTPException as error:
        # A failed attachment loads in the initiating iframe; surface the reason in its dialog.
        payload=json.dumps({'type':'familyfoto-download-error','message':error.detail}).replace('<','\\u003c')
        return HTMLResponse('<!doctype html><title>Download unavailable</title><p>Download unavailable. Return to FamilyFoto and try again.</p><script>parent.postMessage('+payload+',location.origin)</script>',status_code=error.status_code)
