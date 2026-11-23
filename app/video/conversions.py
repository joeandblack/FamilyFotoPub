"""Destination-only AVI/FLV conversion with durable checkpoints."""
import argparse
import json
from ..config import DATA, load_config
from ..database import connection
from ..imports.importer import IMPORT_LOCK
from ..telemetry import utcnow, write_json
from .metadata import background_priority, readonly_monitor, tool_paths
from .avi import convert_queue

PROFILE = {'version':1,'video_codec':'libx264','crf':20,'preset':'medium',
           'pixel_format':'yuv420p','threads':2,'audio_codec':'aac',
           'audio_bitrate':'128k','faststart':True,'io_mibps':12,
           'decoder_timeout_seconds':3600,'headroom_bytes':5*1024**3}


def job_parameters(config):
    return {'profile':dict(PROFILE),'destination':dict(config['destination']),
            'database':{k:config['database'].get(k) for k in ('host','port','dbname','user')},
            'remove_destination_avi':bool(config.get('avi_conversion',{}).get('remove_destination_avi'))}


def save_parameters(config, library_id=None):
    parameters=job_parameters(config)
    if library_id is None:
        marker=__import__('pathlib').Path(config['destination']['root']).parent/'.familyfoto-volume.json'
        library_id=json.loads(marker.read_text(encoding='utf-8'))['library_id']
    parameters['library_id']=library_id
    path=DATA/'avi-job.json'
    if path.exists():
        if json.loads(path.read_text(encoding='utf-8'))!=parameters:
            raise RuntimeError('Saved AVI settings or library identity changed; review required')
    else:
        write_json(path,parameters)
    # Preserve the existing AVI identity contract; FLV has its own profile.
    from .flv import PROFILE as flv_profile
    flv_parameters={**parameters,'profile':dict(flv_profile),'remove_destination_avi':False}
    flv_path=DATA/'flv-job.json'
    if flv_path.exists():
        if json.loads(flv_path.read_text(encoding='utf-8'))!=flv_parameters:
            raise RuntimeError('Saved FLV settings or library identity changed; review required')
    else:
        write_json(flv_path,flv_parameters)
    from .mts import PROFILE as mts_profile
    mts_parameters={**parameters,'profile':dict(mts_profile),'remove_destination_avi':False}
    mts_path=DATA/'mts-job.json'
    if mts_path.exists():
        if json.loads(mts_path.read_text(encoding='utf-8'))!=mts_parameters:
            raise RuntimeError('Saved MTS settings or library identity changed; review required')
    else:write_json(mts_path,mts_parameters)
    return parameters


def check_ready(db,job_id=None):
    if (DATA/'video-decoder-busy.json').exists():
        raise RuntimeError('Decoder lease requires review or belongs to a live worker')
    if not db.execute('SELECT pg_try_advisory_lock(%s) locked',(IMPORT_LOCK,)).fetchone()['locked']:
        raise RuntimeError('An import or maintenance worker is already running')
    if job_id is not None:
        from ..jobs.job_scope import require_import_complete,membership
        require_import_complete(db,job_id)
        if db.execute("SELECT count(*) count FROM media m LEFT JOIN video_metadata v ON v.media_id=m.id WHERE m.kind='video' AND NOT EXISTS(SELECT 1 FROM media_trash t WHERE t.media_id=m.id) AND (v.media_id IS NULL OR v.state='pending') AND "+membership(),(job_id,)).fetchone()['count']:
            raise RuntimeError('Finish video processing for this import before conversion')
        db.commit()
        return
    pending=db.execute("SELECT count(*) count FROM jobs WHERE state NOT IN ('completed-review-required','cancelled') OR EXISTS(SELECT 1 FROM import_items WHERE state IN ('pending','error'))").fetchone()['count']
    if pending:
        raise RuntimeError('Finish and review imports before AVI conversion')
    if db.execute("SELECT count(*) count FROM media m LEFT JOIN video_metadata v ON v.media_id=m.id WHERE m.kind='video' AND NOT EXISTS(SELECT 1 FROM media_trash t WHERE t.media_id=m.id) AND (v.media_id IS NULL OR v.state='pending')").fetchone()['count']:
        raise RuntimeError('Finish video metadata before AVI conversion')
    db.commit()


def run(job_id=None):
    if job_id is None:
        raise RuntimeError('Select an import group for AVI conversion')
    from ..jobs.job_scope import save_status,elapsed_base,pending_work
    with connection() as db:
        if not pending_work(db,job_id,'conversion'):return
    config=load_config()
    previous=DATA/'avi-status.json'
    if previous.exists() and json.loads(previous.read_text(encoding='utf-8')).get('state')=='paused-error':
        raise RuntimeError('Conversion fault requires review before resuming')
    if not config.get('avi_conversion',{}).get('enabled'):
        raise RuntimeError('AVI conversion has not been approved')
    with connection() as db:
        check_ready(db,job_id)
        parameters=save_parameters(config)
        background_priority()
        status={'state':'running','phase':'avi-conversion','started':utcnow(),
                'current':None,'parameters':parameters,'scope':'original-import','job_id':job_id,
                'elapsed_base':elapsed_base(db,job_id,'conversion')}
        from .conversion_performance import policy
        status['execution_policy']=policy(config)
        monitor=None
        def publish():
            status['updated']=utcnow()
            write_json(DATA/'avi-status.json',status)
            save_status(db,job_id,'conversion',status)
        try:
            monitor=readonly_monitor(config)
            if monitor.marker_id!=parameters['library_id']:
                raise RuntimeError('Saved conversion library identity changed; review required')
            convert_queue(db,config,tool_paths(),monitor,status,publish,DATA/'AVI-PAUSE',job_id)
        except Exception as error:
            db.rollback()
            status.update(state='paused-error',error=f'{type(error).__name__}: {error}')
            raise
        finally:
            status['current']=None
            status['current_step']=None
            publish()
            if monitor:
                monitor.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job',type=int,required=True)
    from ..jobs import run as pipeline_run
    pipeline_run(parser.parse_args().job,'conversion')
