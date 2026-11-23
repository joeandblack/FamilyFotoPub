"""Import, video and conversion controls; import history and diagnostics."""
from fastapi import APIRouter, HTTPException, Request
from ..config import DATA, load_config
from ..database import connection
from .ignore import IGNORE_SQL_PATTERN, IGNORE_PATTERNS, IGNORE_NO_EXTENSION, IGNORE_DIRECTORIES

router=APIRouter()

@router.post('/api/import/pause')
def pause():
    (DATA/'PIPELINE-PAUSE').touch()
    (DATA/'PAUSE').touch()
    return {'message':'Pause requested at the next file boundary.'}

@router.post('/api/videos/pause')
def pause_videos():
    (DATA/'PIPELINE-PAUSE').touch()
    (DATA/'VIDEO-PAUSE').touch()
    return {'message':'Video pause requested at the next file boundary.'}

@router.post('/api/import/{job_id}/playback-review/{media_id}/{action}')
def playback_review(job_id:int,media_id:int,action:str,request:Request):
    from ..video.conversion_review import finish
    if action not in ('keep','delete'):
        raise HTTPException(422,'Unknown playback review action')
    if action=='delete' and request.state.user['role']!='admin':
        raise HTTPException(403,'Only an administrator can permanently delete files')
    with connection() as db:
        try:
            return finish(db,job_id,media_id,action=='delete',load_config())
        except (OSError,ValueError) as error:
            raise HTTPException(409,'Playback review stopped safely: '+str(error))

@router.post('/api/videos/resume')
def resume_videos():
    raise HTTPException(409,'Select an import group in Imports')

@router.post('/api/import/resume')
def resume():
    raise HTTPException(409,'Select an import group in Imports')

@router.post('/api/import/select-folder')
def select_import_folder():
    from ..service.folder_picker import choose_folder
    from .manual import source_folder
    try:
        selected=choose_folder()
        if selected is None:
            return {'cancelled':True}
        return {'cancelled':False,'path':str(source_folder(load_config(),selected))}
    except (ValueError,OSError,RuntimeError) as error:
        raise HTTPException(409,str(error))


@router.post('/api/import/start')
async def start_import(request:Request):
    payload=await request.json()
    if not isinstance(payload,dict) or not isinstance(payload.get('source'),str):
        raise HTTPException(422,'Source folder is required')
    options={key:payload.get(key,False) for key in ('auto_metadata','auto_conversion','auto_duplicates')}
    if any(type(value) is not bool for value in options.values()):raise HTTPException(422,'Automatic stage options must be boolean')
    from .modes import validate_mode
    try:validate_mode(payload.get('import_mode','missing-only'))
    except ValueError as error:raise HTTPException(422,str(error))
    return start_import_worker(payload['source'],options)


def start_import_worker(source,options=None):
    from .manual import source_folder
    from ..storage.config import enroll_source,import_config
    from psycopg.types.json import Jsonb
    from ..storage.disks import Monitor
    if (DATA/'video-decoder-busy.json').exists():
        raise HTTPException(409,'Decoder lease must finish or be reviewed before importing')
    with connection() as db:
        try:
            from ..jobs import idle, ReservationBusy, duplicate_active
            try:
                idle(db)
            except ReservationBusy:
                if duplicate_active():
                    (DATA/'PHOTO-DUPLICATE-PAUSE').touch()
                    (DATA/'PIPELINE-PAUSE').touch()
                    raise RuntimeError('Photo scan is pausing at the file boundary. Click Start again once paused.')
                raise
            config=load_config()
            from ..config import require_destination
            require_destination(config)
            source=str(source_folder(config,source))
            previous=db.execute('SELECT * FROM jobs WHERE source_root=%s',(source,)).fetchone()
            if previous and previous['state'] in ('completed-review-required','cancelled'):
                state='completed' if previous['state']=='completed-review-required' else 'cancelled'
                raise RuntimeError(f"Import not started: this folder path belongs to {state} Import #{previous['id']}: {source}. This is a folder-path conflict, not a comparison of its current files. If this folder now contains different files, rename it or choose a different folder, then start again.")
            config=import_config(config,previous) if previous else {**config,'source':enroll_source(config,source)}
            Monitor(config).verify()
        except (ValueError,OSError,RuntimeError) as error:
            raise HTTPException(409,str(error))
        job=previous or db.execute('INSERT INTO jobs(source_root,source_storage) VALUES(%s,%s) RETURNING id',(source,Jsonb(config['source']))).fetchone()
        if options is not None:
            db.execute('UPDATE jobs SET auto_metadata=%s,auto_conversion=%s,auto_duplicates=%s WHERE id=%s',(options['auto_metadata'] or options['auto_conversion'],options['auto_conversion'],options.get('auto_duplicates',False),job['id']))
    return resume_group(job['id'],'import')

@router.post('/api/conversions/pause')
def pause_conversions():
    (DATA/'PIPELINE-PAUSE').touch()
    (DATA/'AVI-PAUSE').touch()
    return {'message':'AVI conversion pause requested at the next file boundary.'}

@router.post('/api/conversions/resume')
def resume_conversions():
    raise HTTPException(409,'Select an import group in Imports')

@router.get('/api/issues')
def issues(request:Request,job_id:int|None=None):
    from ..jobs.issues import group_import_issues
    from ..jobs.job_access import UPLOAD_VISIBLE
    user=request.state.user
    viewer=None if user['role']=='admin' else user['username']
    with connection() as db:
        rows=db.execute("SELECT i.job_id,j.source_root,j.started_at,j.state job_state,i.source_path,i.state,i.error FROM import_items i JOIN jobs j ON j.id=i.job_id LEFT JOIN upload_sessions u ON u.job_id=j.id WHERE i.state IN ('error','unsupported') AND i.source_path !~ %s"+(' AND i.job_id=%s' if job_id is not None else '')+('' if viewer is None else ' AND '+UPLOAD_VISIBLE)+' ORDER BY j.id DESC,i.id DESC',(IGNORE_SQL_PATTERN,)+(() if job_id is None else (job_id,))+(() if viewer is None else (viewer,))).fetchall()
    return group_import_issues(rows)


@router.get('/api/import/history')
def import_history(request:Request,summary:bool=False,job_id:int|None=None):
    from .history import history,import_details,summaries
    from ..jobs import snapshot
    user=request.state.user
    viewer=None if user['role']=='admin' else user['username']
    with connection() as db:
        unresolved=db.execute("SELECT count(*) count FROM media m WHERE m.kind='video' AND m.first_import_job_id IS NULL AND NOT EXISTS(SELECT 1 FROM media_trash t WHERE t.media_id=m.id)").fetchone()['count']
        if summary:return {'imports':summaries(db,viewer),'pipeline':snapshot(db),'ownership_review_count':unresolved}
        return {'imports':import_details(db,history(db,job_id,viewer)),'pipeline':snapshot(db),'ownership_review_count':unresolved}


@router.get('/api/import/{job_id}/diagnostics')
def import_diagnostics(job_id:int,request:Request):
    from .diagnostics import diagnostics
    with connection() as db:
        result=diagnostics(db,job_id)
        if result is not None and request.state.user['role']=='admin':
            from face_detect.api.people_preview_gateway import settings
            from face_detect.recognition.people_scan_warnings import import_warnings
            cfg=settings()
            if cfg:result.extend(import_warnings(db,cfg['root'],job_id))
    if result is None:raise HTTPException(404,'Import not found')
    return result


@router.post('/api/import/{job_id}/options')
async def import_options(job_id:int,request:Request):
    payload=await request.json()
    if not isinstance(payload,dict) or set(payload) not in ({'auto_metadata','auto_conversion'},{'auto_metadata','auto_conversion','auto_duplicates'}) or any(type(v) is not bool for v in payload.values()):
        raise HTTPException(422,'Automatic stage options must be boolean')
    with connection() as db:
        row=db.execute("UPDATE jobs SET auto_metadata=%s,auto_conversion=%s,auto_duplicates=coalesce(%s,auto_duplicates) WHERE id=%s AND state<>'cancelled' RETURNING id",(payload['auto_metadata'] or payload['auto_conversion'],payload['auto_conversion'],payload.get('auto_duplicates'),job_id)).fetchone()
        if not row:raise HTTPException(404,'Import unavailable')
    return {'message':'Automatic stages saved for this import.'}


@router.post('/api/import/{job_id}/{stage}/resume')
def resume_group(job_id:int,stage:str):
    from ..jobs import launch
    try:return launch(job_id,stage)
    except (RuntimeError,ValueError,OSError) as error:raise HTTPException(409,str(error))


@router.post('/api/import/{job_id}/pause')
def pause_group(job_id:int):
    from ..jobs import pause
    try:return pause(job_id)
    except (RuntimeError,ValueError,OSError) as error:raise HTTPException(409,str(error))

@router.get('/api/import/ignore-patterns')
def ignore_patterns():
    return {'patterns':IGNORE_PATTERNS+tuple('**/'+name+'/**' for name in IGNORE_DIRECTORIES),'no_extension':IGNORE_NO_EXTENSION}
