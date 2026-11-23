"""Local synthetic-only integration harness; never imported by production startup.

Uses an already-created disposable schema and existing local DB credentials, never
prints/copies credentials. Both the HTTP fixture and explicit child use this setup.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import time
from app.config import ROOT
from app.config import load_config
from app import database
from face_detect.recognition import automatic_people as service
from face_detect.jobs import automatic_people_jobs as workers
from face_detect.recognition import people
from face_detect.jobs.automatic_people_scan import RawJobs
from app.telemetry import write_json
from face_detect.testing.provider import FixtureProvider


def runtime(root):
    root=Path(root).resolve()
    if not root.is_relative_to((ROOT/'temp').resolve()):raise ValueError('Fixture root must be under project temp')
    settings=json.loads((root/'fixture.json').read_text())
    schema=settings['schema']
    if not re.fullmatch('uploadtest_[0-9a-f]{32}',schema):raise ValueError('Disposable fixture schema required')
    # The fixture's People catalog sits beside its main schema, as `people` beside `public`.
    people_schema=settings.get('catalog')
    if people_schema is not None and not re.fullmatch('peopletest_[0-9a-f]{32}',people_schema):raise ValueError('Disposable fixture catalog required')
    path=(people_schema+','+schema) if people_schema else schema
    cfg=load_config();cfg['database']={**cfg['database'],'options':'-c search_path='+path}
    database.load_config=lambda:cfg
    service.load_config=lambda:{'automatic_people':{'enabled':True}}
    key=int(hashlib.sha256(schema.encode()).hexdigest()[:14],16)
    # Foreground probes compare classid=0 OIDs, so fixture keys must fit 32 bits.
    key=key%(2**31-4)
    workers.COMPUTE_LOCK=key;workers.MAINTENANCE_LOCKS=(key+1,);workers.CHILD_LOCKS=(key+2,)
    people.CORRECTION_LOCK=key+3
    from app.jobs import foreground
    from app.jobs import work_admission
    foreground.MARKER=root/'foreground';work_admission.PATH=root/'import-handoff.json'
    def transport(image):
        time.sleep(min(2,max(0,settings.get('delay_seconds',1))))
        return FixtureProvider.faces(image)
    provider=FixtureProvider(transport)
    jobs=RawJobs(root/'jobs',root/'media',provider,provider.observe,provider.verify)
    return jobs


def application(root,jobs=None,dispatch=None,protect=None,desktop=False,catalog=None):
    from fastapi import FastAPI,Depends,HTTPException
    from fastapi.responses import FileResponse
    from face_detect.api.automatic_people_api import router
    from face_detect.api.automatic_people_api import job_router
    from face_detect.jobs.people_launch import launch_explicit
    root=Path(root).resolve();jobs=jobs or runtime(root);app=FastAPI()
    async def fixture_identity(request,call_next):
        role=request.headers.get('x-fixture-role','admin')
        request.state.authenticated=role!='none'
        request.state.user={'username':'synthetic-fixture','role':role}
        return await call_next(request)
    app.middleware('http')(protect or fixture_identity)
    def fixture_dispatch(name,user):
        return launch_explicit('face_detect.testing.automatic_people_fixture',['--root',str(root),'--worker',name],
                               ('automatic-fixture',str(root),name),root/'worker.log')
    app.include_router(router)
    from app.auth.deps import reader
    from app.auth.deps import Identifier
    def selections():
        if catalog is not None:return catalog.selections()
        with database.connection() as db:
            rows=db.execute('SELECT EXTRACT(YEAR FROM captured_at)::int AS photo_year,count(*) n FROM media WHERE captured_at IS NOT NULL GROUP BY photo_year ORDER BY photo_year').fetchall()
            imports=db.execute('SELECT job_id,count(DISTINCT media_id) n FROM import_items WHERE media_id IN (SELECT id FROM media) GROUP BY job_id ORDER BY job_id').fetchall()
        return ([{'id':'year:'+str(r['photo_year']),'label':str(r['photo_year'])+' · '+str(r['n'])+' pilot photos'} for r in rows]
            +[{'id':'import:'+str(r['job_id']),'label':'Import '+str(r['job_id'])+' · '+str(r['n'])+' pilot photos'} for r in imports])
    def resolve_selection(value):
        if catalog is not None:return catalog.resolve(value)
        if value not in {r['id'] for r in selections()}:raise ValueError('Choose an available pilot year or import')
        with database.connection() as db:
            kind,number=value.split(':');number=int(number)
            query=('SELECT id FROM media WHERE EXTRACT(YEAR FROM captured_at)::int=%s ORDER BY id' if kind=='year'
                else 'SELECT DISTINCT media_id id FROM import_items WHERE job_id=%s AND media_id IN (SELECT id FROM media) ORDER BY id')
            ids=[r['id'] for r in db.execute(query,(number,)).fetchall()]
            for mid in ids:jobs.source(db,mid)
        return ids
    app.include_router(job_router(jobs,resolve_selection if desktop else lambda ids:ids,
        dispatch or fixture_dispatch,raw_media=True,server_selection=desktop))
    @app.get('/api/automatic-people/selections')
    def selection_list(user=Depends(reader)):return selections()
    @app.get('/api/automatic-people/thumbnails/{did}')
    def thumbnail(did:Identifier,user=Depends(reader)):
        with database.connection() as db:
            rows=service.rows(db,[did])
            if not rows:raise HTTPException(404,'Face unavailable')
            try:jobs.source(db,rows[0]['media_id'])
            except ValueError:raise HTTPException(404,'Photo unavailable') from None
        path=root/'thumbnails'/f'{did}.jpg'
        if not path.exists():raise HTTPException(404,'Thumbnail unavailable')
        return FileResponse(path,media_type='image/jpeg',headers={'Cache-Control':'no-store'})
    @app.get('/api/automatic-people/ignored/{did}/thumbnail')
    def ignored_thumbnail(did:Identifier,user=Depends(reader)):
        from face_detect.recognition import ignored_faces
        service.guard()
        with database.connection() as db:
            row=db.execute('SELECT a.media_id '+ignored_faces.BASE+""" AND d.id=%s AND d.ignored
                AND EXISTS(SELECT 1 FROM auto_people_members member WHERE member.detection_id=d.id)""",(did,)).fetchone()
            if not row:raise HTTPException(404,'Ignored face unavailable')
            try:jobs.source(db,row['media_id'])
            except ValueError:raise HTTPException(404,'Photo unavailable') from None
        path=root/'thumbnails'/f'{did}.jpg'
        if not path.exists():raise HTTPException(404,'Thumbnail unavailable')
        return FileResponse(path,media_type='image/jpeg',headers={'Cache-Control':'no-store'})
    @app.get('/api/automatic-people/blurry/{did}/thumbnail')
    def blurry_thumbnail(did:Identifier,user=Depends(reader)):
        from face_detect.recognition.people_face_quality import visible_sql
        service.guard()
        with database.connection() as db:
            row=db.execute('SELECT a.media_id '+visible_sql()+' AND d.id=%s',(did,)).fetchone()
            if not row:raise HTTPException(404,'Blurry face unavailable')
            try:jobs.source(db,row['media_id'])
            except ValueError:raise HTTPException(404,'Photo unavailable') from None
        path=root/'thumbnails'/f'{did}.jpg'
        if not path.exists():raise HTTPException(404,'Thumbnail unavailable')
        return FileResponse(path,media_type='image/jpeg',headers={'Cache-Control':'no-store'})
    @app.get('/')
    def page():return FileResponse(ROOT/'face_detect/web/automatic-people-preview.html')
    @app.get('/automatic-people-preview.mjs')
    @app.get('/web/automatic-people-preview.mjs')
    def script():return FileResponse(ROOT/'face_detect/web/automatic-people-preview.mjs',media_type='text/javascript')
    @app.get('/web/lucide.min.js')
    def icons():return FileResponse(ROOT/'web/lucide.min.js',media_type='text/javascript')
    @app.get('/api/automatic-people/fixture-media')
    def media():
        from face_detect.recognition.faces import VISIBLE
        with database.connection() as db:return {'media_ids':[r['id'] for r in db.execute("SELECT m.id FROM media m WHERE m.kind='image' AND "+VISIBLE+' ORDER BY m.id LIMIT 10').fetchall()]}
    from fastapi.staticfiles import StaticFiles
    app.mount('/web',StaticFiles(directory=ROOT/'web'),name='fixture-web')
    app.mount('/face_detect/web',StaticFiles(directory=ROOT/'face_detect/web'),name='face-assets')
    return app


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--worker');parser.add_argument('--port',type=int)
    args=parser.parse_args()
    if args.worker:
        jobs=runtime(args.root);job=jobs.job(args.worker)
        write_json(job.directory/'process.json',{'pid':os.getpid()})
        raw=json.loads((job.directory/'raw.json').read_text())
        try:jobs.run(args.worker,{'username':raw['actor'],'role':'admin'})
        except RuntimeError:
            # Compute admission can fail before RawJobs enters its run body. Keep
            # this explicit request visibly recoverable; never auto-retry a queue.
            raw=json.loads((job.directory/'raw.json').read_text())
            if raw['state'] in ('queued','running','paused'):
                raw['state']='waiting-worker';write_json(job.directory/'raw.json',raw)
            raise
    else:
        import uvicorn
        if not args.port or args.port==8001:raise ValueError('Separate fixture port required')
        uvicorn.run(application(args.root),host='127.0.0.1',port=args.port,log_level='warning')


if __name__=='__main__':main()
