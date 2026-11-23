"""Copy-only, checksum-verified, resumable import of the configured source."""
import argparse
import os
import shutil
import sys
import time
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from psycopg.types.json import Jsonb
from ..config import DATA,load_config
from ..database import connection
from ..storage.disks import Monitor
from ..library.media import TrafficLimit,kind_for,digest,copy_verified,safe_media_path
from ..telemetry import logger,utcnow,write_json,StageTimings
from ..photos.thumbnails import thumbnail_path
from .ignore import should_ignore, ignored_directory, IGNORE_SQL_PATTERN
from ..library.locks import lock_content, unlock_content
from .modes import add_skipped, skip_duplicate

IMPORT_LOCK = 4340002
SINGLE_IMPORT_LOCK = 4340003


def acquire_import_locks(db):
    if not db.execute('SELECT pg_try_advisory_lock(%s) locked',(SINGLE_IMPORT_LOCK,)).fetchone()['locked']:
        raise RuntimeError('Another FamilyFoto importer is running')
    if not db.execute('SELECT pg_try_advisory_lock_shared(%s) locked',(IMPORT_LOCK,)).fetchone()['locked']:
        raise RuntimeError('Another FamilyFoto maintenance worker is running')

def publish(job_id,current=None,db=None):
    with nullcontext(db) if db is not None else connection() as db:
        job=db.execute('SELECT * FROM jobs WHERE id=%s',(job_id,)).fetchone()
        rows=db.execute("SELECT CASE WHEN source_path ~ %s THEN 'ignored' ELSE state END state,count(*) count,coalesce(sum(size),0) bytes FROM import_items WHERE job_id=%s GROUP BY 1",(IGNORE_SQL_PATTERN,job_id)).fetchall()
        db.commit()
    ignored_bytes=sum(int(row['bytes']) for row in rows if row['state']=='ignored')
    rows=[row for row in rows if row['state']!='ignored']
    job['total_bytes']=max(0,job['total_bytes']-ignored_bytes)
    counts={row['state']:row['count'] for row in rows}
    bytes_by_state={row['state']:int(row['bytes']) for row in rows}
    add_skipped(job,counts,bytes_by_state)
    payload={**job,'counts':counts,'total':sum(counts.values()),'processed':sum(v for k,v in counts.items() if k!='pending'),
             'processed_bytes':sum(bytes_by_state.get(key,0) for key in ('imported','duplicate','unsupported','skipped_duplicate')),
             'bytes_by_state':bytes_by_state,'current':current}
    for key in ('started_at','updated_at'):
        payload[key]=payload[key].isoformat()
    write_json(DATA/'import-status.json',payload)

class ProgressReporter:
    def __init__(self,db,job_id,timings):
        self.db=db
        self.job_id=job_id
        self.timings=timings
        self.last=-float('inf')

    def update(self,current=None,force=False):
        if not force and time.monotonic()-self.last<5:
            return
        with self.timings.measure('progress_reporting'):
            publish(self.job_id,current,self.db)
        write_json(DATA/'performance.json',self.timings.snapshot())
        self.last=time.monotonic()

def state(job_id,value,error=None):
    with connection() as db:
        db.execute('UPDATE jobs SET state=%s,error=%s,updated_at=now() WHERE id=%s',(value,error,job_id))
        db.execute('INSERT INTO job_events(job_id,event,details) VALUES(%s,%s,%s)',(job_id,value,error))
    publish(job_id)

def inventory(db,job_id,source,monitor):
    batch=[]
    def walk_error(error):
        raise error
    for directory,folders,files in os.walk(source,followlinks=False,onerror=walk_error):
        monitor.check()
        if ignored_directory(directory):
            folders[:]=[]
            continue
        folders[:]=[name for name in folders if not ignored_directory(name) and not Path(directory,name).is_symlink() and not (hasattr(Path(directory,name),'is_junction') and Path(directory,name).is_junction())]
        if sys.platform == 'darwin':
            folders[:] = [name for name in folders if Path(directory,name).stat().st_dev == source.stat().st_dev]
        for name in files:
            if should_ignore(name):
                continue
            file=Path(directory,name)
            if file.is_symlink():
                continue
            if not file.resolve().is_relative_to(source):
                raise OSError('Source file outside configured root')
            info=file.stat()
            if sys.platform == 'darwin' and info.st_dev != source.stat().st_dev:
                raise OSError('Source file crosses onto another mounted volume')
            batch.append((job_id,str(file),info.st_size,info.st_mtime_ns))
            if len(batch)>=500:
                with db.cursor() as cursor:
                    cursor.executemany('INSERT INTO import_items(job_id,source_path,size,mtime_ns) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING',batch)
                db.commit();batch=[]
                publish(job_id)
    if batch:
        with db.cursor() as cursor:
            cursor.executemany('INSERT INTO import_items(job_id,source_path,size,mtime_ns) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING',batch)
        db.commit()
    db.execute('UPDATE jobs SET inventory_complete=true,total_bytes=(SELECT coalesce(sum(size),0) FROM import_items WHERE job_id=%s) WHERE id=%s',(job_id,job_id))
    db.commit()

def resolve_paths(config,source_override,upload_job_id):
    if source_override:
        from .manual import source_folder
        source_override=str(source_folder(config,source_override))
    source_root=source_override or config['source'].get('root')
    if not upload_job_id and not source_root:
        raise ValueError('Choose a source folder in Imports before starting an import')
    source=Path(source_root).resolve() if source_root else None
    destination=Path(config['destination']['root']).resolve()
    return source,destination

def find_job(db,source,upload_job_id):
    if upload_job_id:
        return db.execute('SELECT * FROM jobs WHERE id=%s',(upload_job_id,)).fetchone()
    return db.execute('SELECT * FROM jobs WHERE source_root=%s',(str(source),)).fetchone()

def job_config(config,job,source):
    if job:
        from ..storage.config import import_config
        return import_config(config,job)
    from ..storage.config import enroll_source
    return {**config,'source':enroll_source(config,str(source))}

def open_job(db,config,source,job):
    if job is None:
        job=db.execute('INSERT INTO jobs(source_root,source_storage) VALUES(%s,%s) RETURNING *',(str(source),Jsonb(config['source']))).fetchone()
    db.execute('UPDATE jobs SET state=%s,error=NULL WHERE id=%s',('scanning' if not job['inventory_complete'] else 'running',job['id']))
    db.commit()
    return job

def next_item(ctx):
    """The next pending item, or None once the job has been parked in a final state."""
    db,job_id=ctx.db,ctx.job_id
    ctx.monitor.check()
    if ctx.uploading and db.execute('SELECT cancel_requested FROM upload_sessions WHERE job_id=%s',(job_id,)).fetchone()['cancel_requested']:
        state(job_id,'cancelled')
        return None
    if (DATA/'PAUSE').exists():
        ctx.progress.update(force=True)
        state(job_id,'paused')
        return None
    with ctx.timings.measure('database_queue'):
        item=db.execute("SELECT * FROM import_items WHERE job_id=%s AND state='pending' ORDER BY id LIMIT 1",(job_id,)).fetchone()
        db.commit()
    if not item:
        ctx.progress.update(force=True)
        state(job_id,'completed-review-required')
    return item

def require_unchanged(file,before,message):
    after=file.stat()
    if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):
        raise OSError(message)

def check_source_stat(ctx,file,item):
    before=file.stat()
    if sys.platform == 'darwin' and not ctx.uploading and before.st_dev != ctx.source.stat().st_dev:
        raise OSError('Source file volume changed after inventory')
    if before.st_size!=item['size'] or before.st_mtime_ns!=item['mtime_ns']:
        raise OSError('Source changed since inventory; review required')
    return before

def ensure_copy(ctx,item,file,sha,media,target):
    """Verify the existing canonical copy, or restore/create it."""
    db,monitor,limit,timings=ctx.db,ctx.monitor,ctx.limit,ctx.timings
    if target.exists():
        with timings.measure('existing_copy_verify'):
            if digest(target,monitor.check,limit)!=(media.get('stored_sha256') if media and media.get('stored_sha256') else sha):
                raise OSError('Existing destination file has incorrect checksum; not overwritten')
        return
    if media and media.get('stored_sha256'):
        raise OSError('Converted MP4 missing; manual review required, AVI not substituted')
    # Recover a missing canonical copy without transferring its
    # ownership or relabeling a later exact duplicate as original.
    space=shutil.disk_usage(ctx.destination).free
    if space < item['size']+5*1024**3:
        raise OSError('Destination needs at least file size plus 5 GiB free')
    if media:
        db.execute('UPDATE import_items SET recovery_attempted=true WHERE id=%s',(item['id'],))
        db.commit()
    copy_verified(file,target,sha,monitor.check,limit,timings)
    if media:
        ctx.log.info('restored-missing-copy item=%s hash=%s',item['id'],sha)

def read_image_metadata(ctx,target,sha):
    captured=width=height=metadata_error=None
    try:
        with ctx.timings.measure('thumbnail_metadata'):
            thumbnail=thumbnail_path(DATA/'thumbnails',sha)
            thumbnail.parent.mkdir(parents=True,exist_ok=True)
            from ..photos.thumbnail_recovery import create_thumbnail
            preview=thumbnail_path(DATA/'previews',sha) if target.suffix.lower() in ('.heic','.heif') else None
            captured,width,height,metadata_error=create_thumbnail(target,thumbnail,preview)
    except Exception as error:
        metadata_error=f'{type(error).__name__}: {error}'
        ctx.log.warning('thumbnail-unavailable hash=%s error=%s',sha,metadata_error)
    return captured,width,height,metadata_error

def catalog_item(ctx,item,sha,relative,display,kind,media,metadata):
    db,timings=ctx.db,ctx.timings
    commit_started=time.perf_counter()
    if not media:
        captured,width,height,metadata_error=metadata
        media=db.execute("INSERT INTO media(sha256,relative_path,name,kind,size,captured_at,width,height,metadata_error,first_import_job_id,first_import_basis,owner_name,visibility) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'catalog-insert',%s,%s) RETURNING *",
                         (sha,relative,display.name,kind,item['size'],captured,width,height,metadata_error,ctx.job_id,ctx.owner,ctx.visibility)).fetchone()
    db.execute("UPDATE import_items SET state='imported',media_id=%s,error=NULL WHERE id=%s",(media['id'],item['id']))
    db.execute('UPDATE jobs SET updated_at=now() WHERE id=%s',(ctx.job_id,))
    db.commit()
    timings.seconds['database_commit']=timings.seconds.get('database_commit',0)+time.perf_counter()-commit_started
    timings.calls['database_commit']=timings.calls.get('database_commit',0)+1

def process_item(ctx,item):
    """Import one queued file. Returns the content lock still held, if any."""
    db,monitor,timings,limit,log=ctx.db,ctx.monitor,ctx.timings,ctx.limit,ctx.log
    expected_upload_sha=None
    if ctx.uploading:
        from ..uploads import staged_file
        file,expected_upload_sha=staged_file(db,ctx.job,item)
    else:file=Path(item['source_path'])
    display=Path(item['source_path'])
    if should_ignore(display):
        # Preserve old inventory rows for audit, but never read these files.
        db.execute("UPDATE import_items SET state='unsupported',error=NULL WHERE id=%s",(item['id'],));db.commit();return None
    ctx.progress.update(item['source_path'])
    if not ctx.uploading and not file.resolve().is_relative_to(ctx.source):
        raise OSError('Queued path outside configured source')
    kind=kind_for(file)
    if not kind:
        db.execute("UPDATE import_items SET state='unsupported' WHERE id=%s",(item['id'],));db.commit();return None
    before=check_source_stat(ctx,file,item)
    with timings.measure('source_hash'):
        sha=digest(file,monitor.check,limit)
        if expected_upload_sha and sha!=expected_upload_sha:raise OSError('Verified upload content changed')
    with timings.measure('database_lookup'):
        content_lock=lock_content(db,sha)
        media=db.execute('SELECT * FROM media WHERE sha256=%s',(sha,)).fetchone();db.commit()
    duplicate=media is not None
    if media and db.execute('SELECT 1 FROM media_trash WHERE media_id=%s AND deleting_at IS NOT NULL',(media['id'],)).fetchone():
        require_unchanged(file,before,'Source changed during reading; review required')
        if not skip_duplicate(db,ctx.job_id,item['id']):
            raise RuntimeError('Duplicate queue completion rejected')
        db.commit()
        log.info('skipped-deleted-duplicate item=%s hash=%s',item['id'],sha)
        ctx.progress.update(str(file))
        return content_lock
    relative=media['relative_path'] if media else f'{sha[:2]}/{sha[2:4]}/{sha}{file.suffix.lower()}'
    target=safe_media_path(ctx.destination,relative)
    monitor.check()
    ensure_copy(ctx,item,file,sha,media,target)
    require_unchanged(file,before,'Source changed during reading; copy not cataloged')
    if duplicate:
        if not skip_duplicate(db,ctx.job_id,item['id']):
            raise RuntimeError('Duplicate queue completion rejected')
        db.commit()
        ctx.progress.update()
        return content_lock
    metadata=read_image_metadata(ctx,target,sha) if kind=='image' else (None,None,None,None)
    monitor.check()
    catalog_item(ctx,item,sha,relative,display,kind,media,metadata)
    with timings.measure('completion_logging'):
        log.info('verified-copy item=%s hash=%s size=%s duplicate=%s',item['id'],sha,item['size'],duplicate)
    ctx.progress.update(str(file))
    return content_lock

def run(source_override=None,upload_job_id=None):
    config=load_config()
    from ..config import require_destination
    require_destination(config)
    source,destination=resolve_paths(config,source_override,upload_job_id)
    log=logger('import')
    if (DATA/'video-decoder-busy.json').exists():
        raise RuntimeError('Decoder lease must finish or be reviewed before importing')
    monitor=None
    job_id=None
    with connection() as db:
        acquire_import_locks(db)
        db.commit()
        try:
            job=find_job(db,source,upload_job_id)
            uploading=bool(upload_job_id)
            if uploading and (not job or job.get('source_kind')!='upload'):raise ValueError('Upload job unavailable')
            config=job_config(config,job,source)
            monitor=Monitor(config,roles=('destination',)) if uploading else Monitor(config)
            monitor.prepare()
            if not uploading and not source.is_dir():
                raise OSError('Configured source folder is missing')
            if not uploading:job=find_job(db,source,None)
            job=open_job(db,config,source,job)
            job_id=job['id']
            if not job['inventory_complete']:
                publish(job_id)
                inventory(db,job_id,source,monitor)
            db.execute("UPDATE import_items SET state='pending',error=NULL WHERE job_id=%s AND state='error'",(job_id,))
            db.commit()
            state(job_id,'running')
            timings=StageTimings()
            from ..uploads import job_owner
            owner,visibility=job_owner(db,job_id) if uploading else (None,'shared')  # imports are always shared
            ctx=SimpleNamespace(db=db,job=job,job_id=job_id,source=source,destination=destination,owner=owner,visibility=visibility,
                                uploading=uploading,monitor=monitor,timings=timings,log=log,
                                progress=ProgressReporter(db,job_id,timings),
                                limit=TrafficLimit(config['io_mibps'],timings))
            content_lock=None
            while True:
                if content_lock is not None:
                    unlock_content(db,content_lock)
                    db.commit()
                    content_lock=None
                item=next_item(ctx)
                if not item:break
                content_lock=process_item(ctx,item)
        except Exception as error:
            db.rollback()
            log.exception('import-paused')
            if job_id:
                state(job_id,'paused-error',f'{type(error).__name__}: {error}')
            else:
                write_json(DATA/'import-status.json',{'state':'paused-error','error':f'{type(error).__name__}: {error}','updated_at':utcnow(),'counts':{},'total':0})
            raise
        finally:
            if hasattr(monitor,'thread'):
                monitor.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description='Import an identity-bound source folder.')
    parser.add_argument('--source')
    args=parser.parse_args()
    from .api import start_import_worker
    start_import_worker(args.source or load_config()['source']['root'])
