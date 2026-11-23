"""One selected import, one reserved operation, optional same-import transitions."""
import json
import subprocess
import sys
import threading
import time
from .. import config
from ..config import DATA,ROOT
from ..database import connection
from ..telemetry import write_json,utcnow

LOCK=4340006
HANDOFF_LOCK=4340005
WORKER_LOCK=4340002
STAGES=('import','metadata','conversion','duplicates')


def console_log():
    """Where a started operation's own output goes: this installation's log folder.

    Not the code's folder. An installation whose logs live elsewhere, such as a recovered
    library, may have no `logs` beside the code at all, and nothing but the web server's
    first request makes the log folder. So make it here.
    """
    config.LOGS.mkdir(parents=True,exist_ok=True)
    return config.LOGS/'pipeline-console.log'
PAUSES={'import':'PAUSE','metadata':'VIDEO-PAUSE','conversion':'AVI-PAUSE','duplicates':'PHOTO-DUPLICATE-PAUSE'}


def snapshot(db):
    busy=db.execute('SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype=\'advisory\' AND classid=0 AND objid IN (4340002,4340005,4340006) AND granted AND pid<>pg_backend_pid()) busy').fetchone()['busy']
    path=DATA/'pipeline-status.json'
    status=json.loads(path.read_text()) if path.exists() else {}
    from . import work_admission
    return {**status,'busy':busy,'pause_requested':(DATA/'PIPELINE-PAUSE').exists(),
            'handoff':work_admission.snapshot(),
            'duplicate_busy':bool(busy and status.get('stage')=='duplicates'),
            'face_busy':bool(busy and work_admission.face_owns_reservation(db))}


class ReservationBusy(RuntimeError):
    pass


def idle(db):
    for key in (LOCK,HANDOFF_LOCK,WORKER_LOCK):
        if not db.execute('SELECT pg_try_advisory_lock(%s) available',(key,)).fetchone()['available']:
            raise ReservationBusy('Another import or video operation is active; pause it first')
    if (DATA/'video-decoder-busy.json').exists():
        raise RuntimeError('Decoder lease must finish or be reviewed first')


def next_stage(stage,completed,options,paused):
    if not completed or paused:return None
    if stage=='import' and (options.get('auto_metadata') or options.get('auto_conversion')):return 'metadata'
    if stage=='metadata' and options.get('auto_conversion'):return 'conversion'
    if stage in ('import','metadata','conversion') and options.get('auto_duplicates'):return 'duplicates'
    return None


def _wait_handoff(request):
    from . import work_admission
    try:
        while True:
            work_admission.validate(request['token'])
            try:
                launch(request['job_id'], request['stage'], _handoff=request['token'])
                return
            except HandoffBusy:
                time.sleep(.25)
    except Exception as error:
        with connection() as db:
            work_admission.finish(db, request['token'], 'failed', str(error)[:300])


class HandoffBusy(RuntimeError):
    pass


def duplicate_active():
    path=DATA/'pipeline-status.json'
    return path.exists() and json.loads(path.read_text()).get('stage')=='duplicates'


def _queue_handoff(db, job_id, stage):
    from . import work_admission
    job=db.execute('SELECT * FROM jobs WHERE id=%s',(job_id,)).fetchone()
    if not job or job['state']=='cancelled':raise RuntimeError('Import unavailable')
    if job.get('source_kind')=='upload':raise RuntimeError('Use the upload session controls')
    request, created=work_admission.reserve(db,job_id,stage)
    db.commit()
    if created:
        threading.Thread(target=_wait_handoff,args=(request,),daemon=True).start()
    return {'message':f'{stage.capitalize()} queued after the current background item for import #{job_id}.',
            'handoff':request}


def launch(job_id,stage,inline=False,_handoff=None,unexamined=False,large_review=False):
    from ..service.macos_lifecycle import require_running
    require_running()
    if large_review:unexamined=True
    if stage not in STAGES:raise ValueError('Unknown stage')
    if unexamined and stage!='duplicates':raise ValueError('Unexamined scan requires photo duplicate stage')
    with connection() as db:
        from . import work_admission
        if _handoff:
            work_admission.validate(_handoff)
        try:
            idle(db)
        except ReservationBusy:
            if unexamined:raise
            if not inline and (work_admission.face_owns_reservation(db) or (stage=='import' and duplicate_active())):
                if _handoff:
                    raise HandoffBusy('Waiting for the current face item')
                return _queue_handoff(db,job_id,stage)
            raise
        # Even between face units, retain priority through the subprocess startup gap.
        if not inline and not _handoff and work_admission.face_owns_reservation(db):
            if unexamined:raise ReservationBusy('Pause the current operation first')
            return _queue_handoff(db,job_id,stage)
        job=db.execute('SELECT * FROM jobs WHERE id=%s',(job_id,)).fetchone()
        if not job or job['state']=='cancelled':raise RuntimeError('Import unavailable')
        if job.get('source_kind')=='upload' and stage=='import' and not inline:
            raise RuntimeError('Use Resume import in the upload session to queue this import')
        if stage=='import':
            if job['state']=='completed-review-required':raise RuntimeError('Import already complete')
            from ..config import load_config
            from ..storage.disks import Monitor
            from ..storage.config import import_config
            config=import_config(load_config(),job);Monitor(config,roles=('destination',) if job.get('source_kind')=='upload' else ('source','destination')).verify()
        else:
            from .job_scope import require_import_complete,pending_work
            require_import_complete(db,job_id)
            if unexamined:
                from ..photos.duplicates import prepare_unexamined
                prepare_unexamined(db,job_id,large_review=large_review)
            if not pending_work(db,job_id,stage) and not (stage=='duplicates' and job.get('auto_trash_duplicates')):raise RuntimeError('No original videos need this stage in this import; duplicates need no post-processing')
            from ..video import metadata as videos,conversions
            if stage=='duplicates':
                from ..photos.duplicates import ready
                ready(db,job_id)
            elif stage=='metadata':videos.check_ready(db,job_id)
            else:
                conversions.check_ready(db,job_id)
                from ..config import load_config
                config=load_config()
                if not config.get('avi_conversion',{}).get('enabled'):raise RuntimeError('AVI conversion not approved')
                conversions.save_parameters(config)
        for name in ('video-status.json','avi-status.json','photo-duplicate-status.json'):
            path=DATA/name
            if path.exists() and json.loads(path.read_text())['state']=='paused-error':
                raise RuntimeError('A video fault requires review before another operation')
        # Reserve foreground priority before releasing dispatcher locks. A People
        # worker may start during the subprocess startup gap, even if none existed
        # when admission was checked.
        if not inline and not _handoff:
            request,created=work_admission.reserve(db,job_id,stage)
            db.commit()
            if not created:return {'message':f'{stage.capitalize()} is already queued for import #{job_id}.','handoff':request}
            _handoff=request['token']
        for name in ('PIPELINE-PAUSE',PAUSES[stage]):(DATA/name).unlink(missing_ok=True)
        if stage=='import' and job['auto_metadata']:(DATA/'VIDEO-PAUSE').unlink(missing_ok=True)
        if stage in ('import','metadata') and job['auto_conversion']:(DATA/'AVI-PAUSE').unlink(missing_ok=True)
        if stage in ('import','metadata','conversion') and job.get('auto_duplicates'):(DATA/'PHOTO-DUPLICATE-PAUSE').unlink(missing_ok=True)
    require_running()
    if inline:return run(job_id,stage)
    with console_log().open('ab') as output:
        command=[sys.executable,'-m','app.jobs','--job',str(job_id),'--stage',stage]
        if _handoff:command.extend(['--handoff',_handoff])
        subprocess.Popen(command,cwd=ROOT,
            stdout=output,stderr=output,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    return {'message':f'{stage.capitalize()} requested for import #{job_id}.'}


def pause(job_id):
    with connection() as db:
        from . import work_admission
        db.execute('SELECT pg_advisory_xact_lock(%s)',(work_admission.LOCK,))
        request=work_admission.read()
        if request.get('job_id')==job_id and request.get('state')=='pending':
            work_admission.finish(db,request['token'],'cancelled')
            return {'message':'Pending import handoff cancelled; no new operation will start.'}
        available=db.execute('SELECT pg_try_advisory_lock(%s) available',(LOCK,)).fetchone()['available']
        if available:raise RuntimeError('No active grouped operation')
        status_path=DATA/'pipeline-status.json'
        status=json.loads(status_path.read_text()) if status_path.exists() else {}
        if status.get('job_id')!=job_id and request.get('job_id')==job_id and request.get('state')=='started':
            (DATA/'PIPELINE-PAUSE').touch()
            (DATA/PAUSES[request['stage']]).touch()
            return {'message':'Pause requested at the next file boundary; automatic continuation stopped.'}
        if status.get('job_id')!=job_id:raise RuntimeError('Another import group is active')
        (DATA/'PIPELINE-PAUSE').touch()
        (DATA/PAUSES[status['stage']]).touch()
    return {'message':'Pause requested at the next file boundary; automatic continuation stopped.'}


def acquire_startup(db,timeout=10):
    # A dispatcher can briefly hold reservations between launch and child startup.
    deadline=time.monotonic()+timeout
    while True:
        try:
            idle(db)
            return
        except ReservationBusy:
            db.execute('SELECT pg_advisory_unlock_all()')
            db.commit()
            if time.monotonic()>=deadline:raise
            time.sleep(.25)


def run(job_id,stage,handoff=None):
    from ..service.macos_lifecycle import require_running
    require_running()
    with connection() as control:
        try:
            acquire_startup(control)
        except RuntimeError as error:
            control.execute('SELECT pg_advisory_unlock_all()')
            if stage=='import':
                control.execute("""UPDATE jobs SET state='paused-error',error=%s,updated_at=now()
                    WHERE id=%s AND state='scanning' AND NOT inventory_complete
                    AND NOT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory'
                    AND classid=0 AND objid=4340006 AND granted AND pid<>pg_backend_pid())""",('Import worker could not start: '+str(error),job_id))
                control.commit()
            raise
        if handoff:
            from . import work_admission
            control.execute('SELECT pg_advisory_xact_lock(%s)',(work_admission.LOCK,))
            request=work_admission.validate(handoff)
            if (request['job_id'],request['stage'])!=(job_id,stage):
                raise RuntimeError('Import handoff scope mismatch')
            job=control.execute('SELECT * FROM jobs WHERE id=%s FOR SHARE',(job_id,)).fetchone()
            if not job or job['state']=='cancelled' or (stage=='import' and job['state']=='completed-review-required'):
                work_admission.finish(control,handoff,'failed','Requested import is no longer available')
                control.commit()
                return
            work_admission.finish(control,handoff,'started')
        # Retain the pipeline reservation; individual workers acquire their own I/O lock.
        control.execute('SELECT pg_advisory_unlock(%s)',(WORKER_LOCK,))
        control.commit()
        status={'job_id':job_id,'stage':stage,'state':'running','started':utcnow()}
        def publish(state):
            status.update(state=state,updated=utcnow());write_json(DATA/'pipeline-status.json',status)
        try:
            while stage:
                status['stage']=stage
                publish('running')
                if (DATA/'PIPELINE-PAUSE').exists():publish('paused');return
                job=control.execute('SELECT * FROM jobs WHERE id=%s',(job_id,)).fetchone();control.commit()
                if job.get('source_kind')=='upload':
                    cancelled=control.execute('SELECT cancel_requested FROM upload_sessions WHERE job_id=%s',(job_id,)).fetchone();control.commit()
                    if cancelled and cancelled['cancel_requested']:
                        control.execute("UPDATE jobs SET state='cancelled' WHERE id=%s AND state<>'completed-review-required'",(job_id,));control.commit()
                        publish('cancelled');return
                if stage=='import':
                    from ..imports.importer import run as import_run
                    if job.get('source_kind')=='upload':import_run(upload_job_id=job_id)
                    else:import_run(job['source_root'])
                    result=control.execute('SELECT state FROM jobs WHERE id=%s',(job_id,)).fetchone();control.commit()
                else:
                    from .job_scope import require_import_complete,pending_work
                    require_import_complete(control,job_id)
                    has_work=pending_work(control,job_id,stage);control.commit()
                    if not has_work and not (stage=='duplicates' and job.get('auto_trash_duplicates')):
                        result={'state':'completed-review-required'}
                    elif stage=='duplicates':
                        from ..photos.duplicates import run as duplicate_run
                        duplicate_run(job_id)
                        result=json.loads((DATA/'photo-duplicate-status.json').read_text())
                    elif stage=='metadata':
                        from ..video.metadata import run as metadata_run
                        metadata_run(job_id=job_id)
                        result=json.loads((DATA/'video-status.json').read_text())
                    else:
                        from ..video.conversions import run as conversion_run
                        conversion_run(job_id=job_id)
                        result=json.loads((DATA/'avi-status.json').read_text())
                completed=result['state']=='completed-review-required'
                options=control.execute('SELECT auto_metadata,auto_conversion,auto_duplicates FROM jobs WHERE id=%s',(job_id,)).fetchone();control.commit()
                stage=next_stage(stage,completed,options,(DATA/'PIPELINE-PAUSE').exists())
                if stage and not (DATA/'PIPELINE-PAUSE').exists():
                    # A newly enabled successor may retain a marker from an older run.
                    (DATA/PAUSES[stage]).unlink(missing_ok=True)
                if not stage:publish('completed' if completed else result['state'])
        except Exception as error:
            status['error']=f'{type(error).__name__}: {error}';publish('paused-error');raise
