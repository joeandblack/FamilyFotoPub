"""Status, storage and page-shell routes."""
import json
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from .config import DATA, ROOT, load_config
from .database import connection

router=APIRouter()

@router.get('/api/status')
def status():
    config=load_config()
    def read(name):
        try:
            return json.loads((DATA/name).read_text(encoding='utf-8'))
        except FileNotFoundError:
            return None
    job=read('import-status.json')
    if job:
        from .elapsed import active_seconds
        with connection() as db:
            events=db.execute('SELECT event,created_at FROM job_events WHERE job_id=%s ORDER BY created_at,id',(job['id'],)).fetchall()
            current=db.execute('SELECT size FROM import_items WHERE job_id=%s AND source_path=%s',(job['id'],job['current'])).fetchone() if job.get('current') else None
            job['current_size']=current['size'] if current else None
        job['active_seconds']=active_seconds(datetime.fromisoformat(job['started_at']),events,datetime.now(timezone.utc))
    from .jobs.job_scope import original_status
    video=original_status(read('video-status.json'))
    conversion=original_status(read('avi-status.json'))
    if video:
        from .video.progress import timing, current_details
        with connection() as db:
            video=current_details(timing(video,db),db)
    if conversion:
        from .video.progress import conversion_timing, current_details
        with connection() as db:
            conversion=current_details(conversion_timing(conversion),db)
    duplicates=read('photo-duplicate-status.json')
    from .jobs.job_access import hide_other_uploads
    with connection() as db:
        for status,key in ((job,'id'),(video,'job_id'),(conversion,'job_id'),(duplicates,'job_id')):
            hide_other_uploads(db,status,key)
    return {'job':job,'monitor':read('monitor.json'),'video':video,'conversion':conversion,'duplicates':duplicates,'video_handoff':read('video-handoff.json'),
            'source':{**{k:v for k,v in config['source'].items() if k!='volume_id'},'root':(job or {}).get('source_root') or config['source'].get('root')},
            'destination':{k:v for k,v in config['destination'].items() if k!='volume_id'},
            'io_mibps':config['io_mibps']}

@router.get('/api/storage')
def storage():
    from .storage import capacity
    from .storage.config import latest_source
    config=load_config()
    with connection() as db:config={**config,'source':latest_source(db,config)}
    return capacity(config)

@router.get('/api/storage/settings')
def storage_settings(request:Request):
    if request.state.user['role']!='admin':raise HTTPException(403,'Administrator required')
    from .storage.config import settings
    with connection() as db:return settings(db)

@router.post('/api/storage/settings')
async def save_storage_settings(request:Request):
    from .storage.config import save
    payload=await request.json()
    if not isinstance(payload,dict):raise HTTPException(422,'Settings object required')
    try:
        with connection() as db:return save(db,payload)
    except (ValueError,OSError,RuntimeError) as error:
        raise HTTPException(409,str(error))

@router.post('/api/storage/select-folder')
def select_storage_folder():
    from .service.folder_picker import choose_folder
    try:
        selected=choose_folder(storage=True)
        return {'cancelled':selected is None,'path':selected}
    except (ValueError,OSError,RuntimeError) as error:
        raise HTTPException(409,str(error))

@router.get('/')
def index():
    return FileResponse(ROOT/'web'/'index.html')
