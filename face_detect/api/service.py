"""Administrator controls for the native People process; never invokes Docker."""
import json
import threading
import time
import httpx
from fastapi import APIRouter,Depends,HTTPException
from pydantic import BaseModel,StrictBool
from app.config import DATA
from app.telemetry import write_json
from app.auth.deps import administrator
from face_detect.api import people_preview_gateway as gateway

router=APIRouter(prefix='/api/face-service',dependencies=[Depends(administrator)])
PREFERENCES=DATA/'face-service-settings.json'
_lock=threading.RLock()
_process=None
_busy=False
_status={'state':'stopped','message':'Native face detection is stopped.'}

def preferences():
    if not PREFERENCES.exists():
        return {'start_with_app': False}
    return {'start_with_app':json.loads(PREFERENCES.read_text()).get('start_with_app') is True}

def pause_jobs():
    from face_detect.jobs.checkpoint import EvaluationJob
    for control in (DATA/'face_detect/profiles').glob('*/jobs/*/control.json'):
        if json.loads(control.read_text()).get('action')=='run':
            EvaluationJob(control.parent).command('pause')

def shutdown():
    global _process
    pause_jobs()
    cfg=gateway.settings()
    if cfg:(cfg['root']/'DESKTOP-PREVIEW-STOP').touch()
    if _process:
        process,stop=_process;stop.touch()
        try:process.wait(timeout=35)
        except Exception:pass
        if process.poll() is not None:_process=None

def catalog_ready():
    """A recovered library with its People catalog can scan: the worker still checks this
    computer against the library's stored vectors before it writes one. It names the real
    schema `people`, so it is for installations; tests give their catalog another name."""
    from app import database
    from face_detect import catalog
    try:
        with database.connection() as db:
            return catalog.exists(db) and bool(db.execute('SELECT 1 FROM people.catalog_state').fetchone())
    except Exception:
        return False

def operate(action):
    global _process,_busy,_status
    try:
        if action=='stop':
            shutdown()
            _status={'state':'stopped' if _process is None else 'stopping','message':'Face jobs paused; native People service stopping.'}
        else:
            from app.config import load_config
            recovery = load_config().get('recovery', {})
            if recovery.get('state') == 'activated' and recovery.get('native_inference_qualified') is not True \
                    and not catalog_ready():
                raise RuntimeError('Recovered faces are available for viewing. Set up the face engine on this computer before starting scans: python -m app.migration.people_setup.')
            if load_config().get('live_existing_catalog') and not load_config().get('live_native_catalog'):
                raise RuntimeError('Existing People catalog is active. Native face scanning requires a separate catalog migration; your saved people are unchanged.')
            if _process is None or _process[0].poll() is not None:
                from app.config import load_config
                if not load_config().get('destination',{}).get('root'):
                    raise RuntimeError('Choose a media destination when setting up your first import, then start face detection.')
                from face_detect.cli.prepare import prepare
                # A prior app crash may have left its owned HTTP child alive.
                cfg=gateway.settings()
                if cfg:
                    (cfg['root']/'DESKTOP-PREVIEW-STOP').touch()
                    with httpx.Client(trust_env=False,timeout=1) as client:
                        for _ in range(35):
                            try:
                                response=client.get(f"http://127.0.0.1:{cfg['port']}/login")
                                if response.headers.get('x-familyfoto-pilot')!=cfg['pilot']:break
                            except httpx.HTTPError:break
                            time.sleep(1)
                        else:raise RuntimeError('Previous People process is still stopping; try again shortly.')
                prepare();_process=gateway.launch()
            cfg=gateway.settings()
            with httpx.Client(trust_env=False,timeout=2) as client:
                for _ in range(30):
                    if _process[0].poll() is not None:raise RuntimeError('Native People process exited; check its desktop-server.log.')
                    try:
                        response=client.get(f"http://127.0.0.1:{cfg['port']}/login")
                        if response.headers.get('x-familyfoto-pilot')==cfg['pilot']:break
                    except httpx.HTTPError:pass
                    time.sleep(.5)
                else:raise RuntimeError('Native People service did not become ready.')
            _status={'state':'ready','message':'Native face detection is ready. Choose an import to start scanning.'}
    except Exception as error:
        from face_detect.contracts import ConfigurationError
        message=str(error) if isinstance(error,(ConfigurationError,RuntimeError)) else 'Native setup failed; check model configuration and local diagnostics.'
        _status={'state':'unavailable','message':message}
    finally:
        with _lock:_busy=False

def enqueue(action):
    global _busy,_status
    with _lock:
        if _busy:return False
        _busy=True;_status={'state':'starting' if action=='start' else 'stopping','message':'Starting native face detection...' if action=='start' else 'Pausing face jobs...'}
    threading.Thread(target=operate,args=(action,),daemon=True).start();return True

def start_with_app():
    if preferences()['start_with_app']:enqueue('start')

@router.get('')
def status():
    with _lock:
        state=dict(_status)
        if state['state']=='ready' and (_process is None or _process[0].poll() is not None):state={'state':'stopped','message':'Native People service is stopped.'}
        return {**state,'busy':_busy,**preferences()}

class Preference(BaseModel):
    start_with_app:StrictBool

@router.post('/settings')
def save_preferences(value:Preference):
    write_json(PREFERENCES,value.model_dump());return value.model_dump()

@router.post('/{action}',status_code=202)
def command(action:str):
    if action not in ('start','stop'):raise HTTPException(404,'Unknown service action')
    if not enqueue(action):raise HTTPException(409,'A service operation is already in progress')
    return status()
