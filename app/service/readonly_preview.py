"""Explicit read-only preview entry point, separate from normal application startup."""
import copy
import json
import math
from pathlib import Path
import sys
import threading
import time

AUTH_PATHS = {'/api/login', '/api/auth/activity', '/api/auth/logout', '/api/auth/logout-all'}


def scratch_root(root):
    return (Path.home()/'AppData/LocalLow/FamilyFotoReadOnly'/Path(root).name).resolve()


class MemorySessions:
    """Preview authentication never touches production session tables."""
    rows = {}
    limits = {}
    lock = threading.RLock()

    def __init__(self, config):
        pass

    def create(self, digest, username, signature, expires):
        with self.lock:
            now=time.time()
            self.rows={k:v for k,v in self.rows.items() if v['expires']>now}
            type(self).rows=self.rows
            self.rows[digest]={'username':username,'credential_signature':signature,'expires':expires}

    def get(self, digest):
        with self.lock:
            row=self.rows.get(digest)
            return dict(row) if row and row['expires']>time.time() else None

    def revoke(self, digest):
        with self.lock:
            self.rows.pop(digest,None)

    def revoke_user(self, username):
        with self.lock:
            keys=[k for k,v in self.rows.items() if v['username']==username]
            for key in keys:self.rows.pop(key,None)
            return len(keys)

    def activity(self, digest):
        with self.lock:
            row=self.rows.get(digest)
            if not row or row['expires']<=time.time():return None
            row['expires']=max(row['expires'],time.time()+3600)
            return {'remaining':math.ceil(row['expires']-time.time())}

    def admit_login(self, account_key, address_key):
        with self.lock:
            now=time.time()
            limits=[('global',60),(address_key,20),(account_key,5)]
            for key,limit in limits:
                count,until=self.limits.get(key,(0,0))
                if until>now and count>=limit:return max(1,math.ceil(until-now))
            for key,_ in limits:
                count,until=self.limits.get(key,(0,0))
                self.limits[key]=(count+1,until) if until>now else (1,now+60)
            return 0


def allowed_request(method,path,people_enabled=False):
    if method not in ('GET','HEAD','OPTIONS'):
        return method=='POST' and path in AUTH_PATHS
    if path.startswith(('/api/people-preview','/people-preview')) and not people_enabled:
        return False
    return not path.startswith(('/api/downloads','/api/uploads'))


def resolved_write_path(path):
    import os
    decoded=os.fsdecode(path)
    # Windows cleanup APIs use extended-length paths for ordinary files.
    # Normalize the namespace before resolving junctions and containment.
    if sys.platform=='win32':
        if decoded.startswith('\\\\?\\UNC\\'):decoded='\\\\'+decoded[8:]
        elif decoded.startswith('\\\\?\\'):decoded=decoded[4:]
    return Path(decoded).resolve()


def disk_check_commands(cfg):
    """The one command macOS runs to identify the media disk. Windows reads it without a child process.

    Without it every original and download answers 503, because the guard blocks all child processes.
    """
    drive=cfg.get('destination',{}).get('drive')
    if sys.platform!='darwin' or not drive:
        return ()
    return [('/usr/sbin/diskutil','info','-plist',str(drive))]


def install_write_guard(writable, *, allowed_subprocesses=()):
    """Defense in depth alongside Windows Low Integrity and SQL privileges."""
    import os
    writable=Path(writable).resolve()

    def check(path):
        if isinstance(path,int) or path is None:return
        resolved=resolved_write_path(path)
        if not resolved.is_relative_to(writable):
            raise PermissionError('Read-only preview: filesystem modification blocked')

    def guard(event,args):
        if event=='open':
            path,mode,flags=args
            if (mode and any(c in mode for c in 'wax+')) or (flags and flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND)):
                check(path)
        elif event in ('os.remove','os.rmdir','os.mkdir','os.chmod','os.utime','os.truncate'):
            check(args[0])
        elif event in ('os.rename','os.link','os.symlink'):
            check(args[0]);check(args[1])
        elif event in ('subprocess.Popen','os.system','os.spawn','os.posix_spawn','os.exec'):
            # Explicit test-only native inference commands; preview callers use
            # the empty default and cannot launch any child process.
            if event=='subprocess.Popen':
                import subprocess
                command=args[1] if isinstance(args[1],str) else subprocess.list2cmdline(args[1])
                if any((args[0] is None or Path(args[0]).resolve()==Path(approved[0]).resolve())
                       and command==subprocess.list2cmdline(list(approved)) for approved in allowed_subprocesses):
                    return
            raise PermissionError('Read-only preview: worker/process launch blocked')
    sys.addaudithook(guard)


def people_schema(cfg):
    """The People schema this preview reads. A recovered library's catalog came from a backup, not a migration,
    so a fresh native catalog is readable there; the preview's own People routes already apply this rule."""
    from face_detect.api.readonly_people import catalog_settings
    return catalog_settings(cfg['preview']['data_root'],
                            recovery=cfg.get('recovery', {}).get('activation_required') is True)['schema']


def verify_database(cfg):
    if cfg.get('limited_metadata_edit') is True:
        from app.jobs.limited_edit import verify_database as verify_limited
        return verify_limited(cfg)
    import psycopg
    with psycopg.connect(**cfg['database']) as db:
        role=db.execute('SELECT rolsuper,rolcreaterole,rolcreatedb FROM pg_roles WHERE rolname=current_user').fetchone()
        if any(role):raise RuntimeError('Preview database role must be unprivileged')
        if db.execute('SHOW transaction_read_only').fetchone()[0]!='on':
            raise RuntimeError('Preview database transactions must default to read-only')
        if db.execute("SELECT has_schema_privilege(current_user,'public','CREATE')").fetchone()[0]:
            raise RuntimeError('Preview role must not create objects in the library schema')
        writable=db.execute("""SELECT tablename FROM pg_tables WHERE schemaname='public'
            AND (has_table_privilege(current_user,quote_ident(schemaname)||'.'||quote_ident(tablename),'INSERT,UPDATE,DELETE,TRUNCATE')) LIMIT 1""").fetchone()
        if writable:raise RuntimeError('Preview database role has write privileges')
        if cfg['preview'].get('people_readonly') is True:
            schema=people_schema(cfg)
            if db.execute("SELECT has_schema_privilege(current_user,%s,'CREATE')",(schema,)).fetchone()[0]:
                raise RuntimeError('Preview role must not create People schema objects')
            writable=db.execute("SELECT tablename FROM pg_tables WHERE schemaname=%s AND has_table_privilege(current_user,quote_ident(schemaname)||'.'||quote_ident(tablename),'INSERT,UPDATE,DELETE,TRUNCATE') LIMIT 1",(schema,)).fetchone()
            if writable:raise RuntimeError('Preview role has People write privileges')
        return db.execute('SELECT count(*) FROM media').fetchone()[0]


def build_app(cfg, *, recovery_scratch=None):
    from contextlib import asynccontextmanager
    from app import config
    root=config.ROOT
    writable=Path(cfg['preview']['writable_root']).resolve()
    if recovery_scratch is None:
        if writable!=scratch_root(root):raise RuntimeError('Preview scratch directory must be the dedicated Windows Low Integrity directory')
    elif (writable != Path(recovery_scratch).resolve() or cfg.get('host') != '127.0.0.1'
            or cfg.get('limited_metadata_edit') or cfg.get('read_only_preview') is not True
            or cfg.get('recovery', {}).get('activation_required') is not True):
        raise RuntimeError('Recovery preview must remain local and read-only')
    config.DATA=Path(cfg['preview']['data_root']).resolve()
    config.LOGS=writable/'logs'
    config.TEMP=writable/'temp'
    config.load_config=lambda:copy.deepcopy(cfg)
    config.initialize_runtime=lambda:None
    if cfg.get('limited_metadata_edit') is not True:
        # The lease is for writers, and the write guard rightly refuses it in the data folder.
        from app.backup import admission
        admission.hold_process_lease=lambda root:None
    from app import auth
    auth.SessionStore=MemorySessions
    from app import api
    from app.auth import api as auth_api
    auth_api.SessionStore=MemorySessions
    from face_detect.api import service
    service.preferences=lambda:{'start_with_app':False}
    from face_detect.api import people_preview_gateway
    people_preview_gateway.settings=lambda:None
    if cfg['preview'].get('people_readonly') is True:
        from face_detect.api.readonly_people import install
        install(api.app, cfg, root)

    @asynccontextmanager
    async def lifespan(app):
        verify_database(cfg)
        try:yield
        finally:
            from app.database.pool import close_pools
            close_pools()

    api.app.router.lifespan_context=lifespan
    from fastapi.responses import HTMLResponse,JSONResponse
    from fastapi import Request

    @api.app.middleware('http')
    async def readonly(request:Request,call_next):
        path=request.url.path
        from app.jobs.limited_edit import allowed_write
        editing=cfg.get('limited_metadata_edit') is True
        permitted=allowed_request(request.method,path,cfg['preview'].get('people_readonly') is True)
        if not (permitted or (editing and allowed_write(request.method,path))):
            message='Limited editing: media changes and background jobs are disabled.' if editing else 'Read-only preview: changes and background jobs are disabled.'
            return JSONResponse({'detail':message},403)
        if path=='/' and request.method=='GET':
            response=await call_next(request)
            if response.status_code!=200:return response
            html=(root/'web/index.html').read_text(encoding='utf-8')
            script='limited-edit-preview' if editing else 'read-only-preview'
            html=html.replace('</head>',f'<script type="module" src="/web/{script}.mjs"></script></head>')
            return HTMLResponse(html,headers={'Cache-Control':'no-store'})
        response=await call_next(request)
        response.headers['X-FamilyFoto-Read-Only']='false' if editing else 'true'
        response.headers['X-FamilyFoto-Mode']='metadata-only' if editing else 'read-only'
        return response
    return api.app


def main():
    sys.dont_write_bytecode=True
    from app.config import ROOT
    filename='limited-edit-preview.json' if '--limited-edit' in sys.argv else 'read-only-preview.json'
    cfg=json.loads((ROOT/'config'/filename).read_text(encoding='utf-8'))
    if cfg.get('read_only_preview') is not True:raise RuntimeError('Explicit read-only preview configuration required')
    # The launcher supplies a Low Integrity primary token. Verify OS isolation
    # with a disposable medium-integrity canary, never a production media file.
    canary=ROOT/'config/readonly-canary.txt'
    if canary.read_text()!='read-only-canary':raise RuntimeError('Missing isolation canary')
    try:
        with canary.open('r+'):pass
    except PermissionError:pass
    else:raise RuntimeError('OS isolation not active; refusing to serve the live library')
    install_write_guard(cfg['preview']['writable_root'])
    app=build_app(cfg)
    from app.service.server import server_options
    import uvicorn
    import asyncio

    async def serve():
        server=uvicorn.Server(uvicorn.Config(app,**server_options(cfg),timeout_graceful_shutdown=15))
        async def watch():
            while not server.should_exit:
                if (Path(cfg['preview']['writable_root'])/'STOP').exists():
                    server.should_exit=True
                    return
                await asyncio.sleep(.5)
        watcher=asyncio.create_task(watch())
        try:await server.serve()
        finally:
            watcher.cancel()
            await asyncio.gather(watcher,return_exceptions=True)
    asyncio.run(serve())


if __name__=='__main__':main()
