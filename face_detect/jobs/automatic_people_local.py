"""Native face provider service with a separate durable recognition schema.

The desktop server owns the HTTP process. Only explicit import Start/Resume
launches inference; media paths come from the catalog, never browser input.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import os
from app.config import ROOT, DATA
from app.config import load_config
from app import database
from face_detect.recognition import automatic_people as service
from face_detect.jobs.automatic_people_scan import RawJobs
from face_detect.jobs.automatic_people_scan import decode_canonical
from face_detect.recognition.faces import VISIBLE
from app.viewer import media_visible
from app.telemetry import write_json
from face_detect.service import create_service
from face_detect.jobs.checkpoint import Excluded

PRIVATE=DATA/'face_detect/profiles'

def ensure_face_quality(db):
    """Make the face-quality table only where it is missing.

    The People catalog already has it. Asking to create it again needs permission to create
    in the schema even though nothing would be made, and a recovered library's database
    login deliberately has none (app/migration/credentials.py). So look first.
    """
    present=db.execute("SELECT to_regclass('auto_people_face_quality') IS NOT NULL AS present").fetchone()
    if not (present['present'] if isinstance(present,dict) else present[0]):
        db.execute((ROOT/'face_detect/storage/face_quality.sql').read_text())


def runtime(root,cache_embeddings=False):
    root=Path(root).resolve()
    if not root.is_relative_to(PRIVATE.resolve()):raise ValueError('Private evaluation root required')
    settings=json.loads((root/'runtime.json').read_text())
    from face_detect import catalog
    if settings.get('schema')!=catalog.SCHEMA:
        raise ValueError('This People catalog has not been moved to the one catalog yet; run the catalog migration')
    production=load_config();evaluation={**production,'database':catalog.database(production)}
    # Keep reviewed recognition IDs in their existing schema. Live visibility is
    # checked in SQL for every result, including when media is later trashed.
    with database.connection(production['database']) as live:
        schema=live.execute('SELECT current_schema() name').fetchone()['name']
    if not re.fullmatch('[a-zA-Z_][a-zA-Z0-9_]*',schema):raise ValueError('Invalid library schema')
    visibility=f''' AND EXISTS(SELECT 1 FROM "{schema}".media live
        WHERE live.id=m.id AND live.sha256=m.sha256 AND live.kind='image' AND left(live.name,1)<>'.'
        AND "{schema}".familyfoto_can_see(live.owner_name,live.visibility)
        AND NOT EXISTS(SELECT 1 FROM "{schema}".media_trash t WHERE t.media_id=live.id)
        AND NOT EXISTS(SELECT 1 FROM "{schema}".face_preferences p WHERE p.media_id=live.id AND p.opted_out))'''
    from face_detect.recognition import ignored_faces
    from face_detect.recognition import people_review_scope
    people_review_scope.LIVE_VISIBILITY=visibility
    if visibility not in ignored_faces.BASE:ignored_faces.BASE+=visibility
    from face_detect.recognition.people_face_quality import NOT_BLURRY
    from face_detect.recognition.people_face_quality import assess
    service.ELIGIBLE=ignored_faces.BASE+" AND m.kind='image' AND NOT d.ignored AND d.review_state<>'rejected'"+NOT_BLURRY
    database.load_config=lambda:evaluation
    with database.connection() as db:
        ensure_face_quality(db)
    service.load_config=lambda:{'automatic_people':{'enabled':True}}
    if cache_embeddings:
        from face_detect.recognition.people_embedding_cache import EmbeddingCache
        service.embedding_cache=EmbeddingCache()
    provider=create_service({'face_detection':{'active_profile':'p','profiles':{'p':settings['profile']}}})
    # A computer may scan into the catalog's space, whatever its exact model identity: another
    # computer with the same model is the same space. It may not start a second space.
    with database.connection() as db:
        from face_detect.catalog.spaces import active_space
        active=active_space(db)
    if provider.space_id is None or (active is not None and provider.space_id!=active):
        raise ValueError("This face model is not the library's active model; switching models is a separate step")
    class LocalJobs(RawJobs):
        def scan(self, lease, item, job):
            # The catalog lists the photos whose faces October carried over. That list goes
            # with the library, so a Mac that adopts it skips them too.
            with database.connection() as db:
                row=self.source(db,item['media_id'],item['content_hash'])
                saved=db.execute('SELECT 1 FROM native_preserved_photos WHERE media_id=%s AND content_hash=%s',
                                 (row['id'],row['sha256'])).fetchone()
            if saved:
                # Preserve reviewed IDs even when the native model could not
                # locate their faces. Never redetect these photos implicitly.
                from app.library.media import safe_media_path
                path=safe_media_path(str(self.source_root),row['relative_path'])
                with path.open('rb') as stream:
                    if hashlib.file_digest(stream,'sha256').hexdigest()!=row['sha256']:
                        raise Excluded('Preserved source changed')
                return []
            return super().scan(lease,item,job)

        def validate_selection(self,db,name,ids):
            if not re.fullmatch(r'import-[1-9][0-9]*',name):raise ValueError('Import-scoped job required')
            jid=int(name.removeprefix('import-'))
            count=db.execute('SELECT count(DISTINCT media_id) n FROM import_items WHERE job_id=%s AND media_id=ANY(%s)',(jid,ids)).fetchone()['n']
            if count!=len(ids):raise ValueError('Job and selected import do not match')
        def source(self,db,mid,expected=None,lock=False):
            row=super().source(db,mid,expected,lock)
            # Recheck current live catalog exclusions read-only, not just the copied
            # snapshot. Exclude a whole sample photo if any existing face is ignored.
            with database.connection(production['database']) as live:
                live.execute('SET TRANSACTION READ ONLY')
                valid=live.execute('SELECT m.sha256 FROM media m WHERE m.id=%s AND '+VISIBLE+
                    ' AND NOT EXISTS(SELECT 1 FROM face_analyses a JOIN face_detections d ON d.analysis_id=a.id '
                    "WHERE a.media_id=m.id AND (d.ignored OR d.review_state='rejected'))",(mid,)).fetchone()
            if not valid or valid['sha256']!=row['sha256']:raise Excluded('Live source changed or excluded')
            return row
    jobs=LocalJobs(root/'jobs',Path(production['destination']['root']),
        provider,provider.observe,provider.verify,decoder=decode_canonical,quality_filter=assess,
        thumbnails=DATA/'people/thumbnails')
    from face_detect.jobs.automatic_people_import import ImportJobs
    return ImportJobs(jobs)


def desktop_application(root):
    from face_detect.api.application import application
    from face_detect.jobs.people_launch import launch_explicit
    from app.auth.api import protect
    from app.auth.api import login
    from app.auth.api import login_page
    from fastapi.responses import FileResponse
    cfg=load_config();root=Path(root).resolve();json.loads((root/'runtime.json').read_text())
    jobs=runtime(root)
    current_media_ids=set()
    for path in (root/'jobs').glob('import-*/import.json'):
        state=json.loads(path.read_text())
        if state.get('state') not in ('completed','cancelled'):
            current_media_ids.update(i['media_id'] for i in state.get('items',[]))
    with database.connection() as db:
        service.initialize_review_sections(db,current_media_ids)
    from face_detect.recognition.people_catalog import ImportCatalog
    catalog=ImportCatalog(cfg,database.connection)
    def dispatch(name,user):
        return launch_explicit('face_detect.jobs.automatic_people_local',
            ['--root',str(root),'--worker',name,'--max-items','0'],
            ('local-desktop',str(root),name),root/'desktop-worker.log')
    app=application(root,jobs=jobs,dispatch=dispatch,protect=protect,desktop=True,catalog=catalog)
    from fastapi import Depends,HTTPException
    from app.auth.deps import reader
    from app.auth.deps import Identifier
    @app.get('/api/automatic-people/import-status')
    def import_status(user=Depends(reader)):
        from face_detect.api.import_status import import_statuses
        return import_statuses(catalog,jobs,root,user)
    @app.get('/api/automatic-people/groups/{gid}/photos')
    def group_photos(gid:Identifier,user=Depends(reader)):
        with database.connection() as db:
            try:group=service.detail(db,gid)['group']
            except LookupError:raise HTTPException(404,'Group unavailable') from None
            ids=sorted({f['media_id'] for f in group['faces']})
            expected={r['id']:r['sha256'] for r in db.execute('SELECT id,sha256 FROM media WHERE id=ANY(%s)',(ids,)).fetchall()}
        # Viewer metadata comes from the live catalog, never stale preview copies.
        with database.connection(load_config()['database']) as live:
            live.execute('SET TRANSACTION READ ONLY')
            photos=live.execute("SELECT m.*,a.crop,coalesce(a.rotation,0) rotation FROM media m LEFT JOIN media_adjustments a ON a.media_id=m.id WHERE m.id=ANY(%s) AND m.kind='image' AND left(m.name,1)<>'.' AND "+VISIBLE+" AND "+media_visible('m')+" ORDER BY m.captured_at DESC NULLS LAST,m.id DESC",(ids,)).fetchall()
        return {'items':[m for m in photos if m['sha256']==expected.get(m['id'])]}
    @app.get('/api/automatic-people/people/{pid}/photos')
    def person_photos(pid:Identifier,user=Depends(reader)):
        with database.connection() as db:
            person=db.execute('SELECT name FROM people WHERE id=%s AND deleted_at IS NULL',(pid,)).fetchone()
            if not person:raise HTTPException(404,'This person is no longer available')
            sources=db.execute('SELECT DISTINCT media_id,sha256 FROM ('+service.named_faces_sql()+') f WHERE person_id=%s',(pid,)).fetchall()
            expected={row['media_id']:row['sha256'] for row in sources}
        with database.connection(cfg['database']) as live:
            live.execute('SET TRANSACTION READ ONLY')
            photos=live.execute("SELECT m.*,a.crop,coalesce(a.rotation,0) rotation FROM media m LEFT JOIN media_adjustments a ON a.media_id=m.id WHERE m.id=ANY(%s) AND "+VISIBLE+" AND "+media_visible('m')+" ORDER BY m.captured_at DESC NULLS LAST,m.id DESC",(list(expected),)).fetchall()
        return {'name':person['name'],'items':[m for m in photos if m['sha256']==expected.get(m['id'])]}
    @app.middleware('http')
    async def pilot_identity(request, call_next):
        response=await call_next(request)
        response.headers['X-FamilyFoto-Pilot']=root.name
        return response
    from app.jobs.foreground import ForegroundActivityMiddleware
    class DesktopActivity(ForegroundActivityMiddleware):
        async def __call__(self,scope,receive,send):
            if scope.get('method')=='GET' or scope.get('path','').startswith('/api/automatic-people/jobs'):
                return await self.app(scope,receive,send)
            return await super().__call__(scope,receive,send)
    app.add_middleware(DesktopActivity)
    app.add_api_route('/api/login',login,methods=['POST'])
    app.add_api_route('/login',login_page,methods=['GET'])
    def fixed_style(path):
        def serve():return FileResponse(path,media_type='text/css')
        return serve
    for name in ('styles.css','daisyui-5.7.50.css'):
        app.add_api_route('/web/'+name,fixed_style(ROOT/'web'/name),methods=['GET'])
    return app


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--worker');parser.add_argument('--port',type=int);parser.add_argument('--max-items',type=int,default=1)
    args=parser.parse_args()
    if args.port:
        if args.worker or args.port<49152:raise ValueError('Separate high loopback port required')
        import uvicorn
        import asyncio
        from app.service.stop_watch import watch_stop
        async def serve():
            server=uvicorn.Server(uvicorn.Config(desktop_application(args.root),host='127.0.0.1',
                port=args.port,log_level='warning',proxy_headers=False,timeout_graceful_shutdown=30))
            watcher=asyncio.create_task(watch_stop(server,args.root/'DESKTOP-PREVIEW-STOP'))
            try:await server.serve()
            finally:
                watcher.cancel()
                await asyncio.gather(watcher,return_exceptions=True)
        asyncio.run(serve())
        return
    if not args.worker:raise ValueError('Explicit worker or desktop port required')
    if not 0<=args.max_items<=400:raise ValueError('Invalid worker slice')
    jobs=runtime(args.root,cache_embeddings=True)
    write_json(jobs.job(args.worker).directory/'process.json',{'pid':os.getpid()})
    # The first scan on a computer checks it against the library before it writes a vector.
    from face_detect.catalog.certify import ensure_certified
    from face_detect.cli.conformance import check_this_library
    ensure_certified(database.connection,jobs.provider,lambda:check_this_library(jobs.provider,load_config()))
    from app.storage.disks import Monitor
    monitor=Monitor(load_config(),roles=('destination',))
    if not monitor.marker.exists():raise ValueError('Existing destination library marker required')
    monitor.prepare()
    observer=jobs.observer
    def checked_observer():monitor.check();return observer()
    jobs.observer=checked_observer
    try:jobs.run(args.worker,{'username':'discover-people','role':'admin'},max_items=args.max_items or None)
    finally:monitor.close()



if __name__=='__main__':main()
