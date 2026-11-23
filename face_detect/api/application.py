from pathlib import Path
from app.config import ROOT
from app import database
from face_detect.recognition import automatic_people as service

def application(root,*,jobs,dispatch,protect,desktop=False,catalog=None):
    from fastapi import FastAPI,Depends,HTTPException
    from fastapi.responses import FileResponse
    from face_detect.api.automatic_people_api import router
    from face_detect.api.automatic_people_api import job_router
    root=Path(root).resolve();app=FastAPI()
    app.middleware('http')(protect)
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
        dispatch,raw_media=True,server_selection=desktop,
        job_visible=catalog.job_name_visible if catalog is not None else None))
    @app.get('/api/automatic-people/selections')
    def selection_list(user=Depends(reader)):return selections()
    # The scan worker's folder: data/people/thumbnails in an installation.
    thumbnails=Path(getattr(jobs,'thumbnail_dir',root/'thumbnails'))
    def saved_thumbnail(did):
        path=thumbnails/f'{did}.jpg'
        if path.exists():return path
        # An installation's cache (data/people/thumbnails) is rebuilt from the photo when a
        # face's thumbnail is missing, as after a restore; a test folder is left as it is.
        if thumbnails.parts[-2:]!=('people','thumbnails') or not hasattr(jobs,'source_root'):return None
        from face_detect.catalog.thumbnails import on_demand
        return on_demand(database.connection,jobs.source_root,thumbnails.parent.parent,did)
    @app.get('/api/automatic-people/thumbnails/{did}')
    def thumbnail(did:Identifier,user=Depends(reader)):
        with database.connection() as db:
            rows=service.rows(db,[did])
            if not rows:raise HTTPException(404,'Face unavailable')
            try:jobs.source(db,rows[0]['media_id'])
            except ValueError:raise HTTPException(404,'Photo unavailable') from None
        path=saved_thumbnail(did)
        if path is None:raise HTTPException(404,'Thumbnail unavailable')
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
        path=saved_thumbnail(did)
        if path is None:raise HTTPException(404,'Thumbnail unavailable')
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
        path=saved_thumbnail(did)
        if path is None:raise HTTPException(404,'Thumbnail unavailable')
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
