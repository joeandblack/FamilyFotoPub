"""Private resumable upload staging. No client path is ever a filesystem path."""
import asyncio
import hashlib
import json
import os
from pathlib import PurePosixPath
import shutil
import threading
import time
import uuid
from psycopg.types.json import Jsonb
from .. import app_settings
from ..config import DATA, load_config
from ..database import connection
from ..imports.ignore import should_ignore
from ..library.media import kind_for

QUOTA_LOCK = 4340010
DISPATCH_LOCK = 4340011
DEFAULTS = dict(max_files=5000, max_total_bytes=20*1024**3,
                max_file_bytes=5*1024**3, staging_bytes=40*1024**3,
                chunk_bytes=8*1024**2, reserve_bytes=5*1024**3,
                expiry_hours=48, max_active=1, upload_mibps=12)


# Administrator-editable ranges (stored units). Config files and tests may use
# smaller values; these bounds apply only to what the Settings page accepts.
EDITABLE = dict(max_files=(1, 100_000), max_total_bytes=(1024**2, 2*1024**4),
                max_file_bytes=(1024**2, 2*1024**4), staging_bytes=(1024**3, 20*1024**4),
                reserve_bytes=(100*1024**2, 1024**4), chunk_bytes=(1024**2, 16*1024**2),
                expiry_hours=(1, 24*30), upload_mibps=(1, 1000))
SETTING_KEY = 'uploads'
_CACHE_SECONDS = 5
_cache = {}


def checked(values):
    if set(values)!=set(DEFAULTS) or any(type(v) is not int or v<=0 for v in values.values()):
        raise ValueError('Invalid upload configuration')
    if values['chunk_bytes']>16*1024**2 or values['max_active']!=1:
        raise ValueError('Uploads require chunks at most 16 MiB and one active transfer')
    return values


def saved_limits():
    """Administrator overrides from the database, cached briefly; {} if unavailable."""
    key = json.dumps(load_config()['database'], sort_keys=True, default=str)
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now-hit[0] < _CACHE_SECONDS:
        return hit[1]
    try:
        with connection() as db:
            value = app_settings.get(db, SETTING_KEY) or {}
    except Exception:
        return hit[1] if hit else {}
    _cache[key] = (now, value)
    return value


def limits():
    result={**DEFAULTS, **load_config().get('uploads',{}), **saved_limits()}
    return checked(result)


# Transfer settings an upload keeps from the moment it starts, so changing Settings
# never breaks or retroactively expires a transfer that is already under way.
ACCEPTED=('chunk_bytes','reserve_bytes','upload_mibps','expiry_hours')
# Sessions started before accepted limits were recorded fall back to the current expiry.
EXPIRED="updated_at<now()-make_interval(hours=>coalesce((options->'limits'->>'expiry_hours')::int,%s))"


def job_owner(db,job_id):
    """(uploader, visibility) for the media an upload job creates."""
    row=db.execute('SELECT owner_name,options FROM upload_sessions WHERE job_id=%s',(job_id,)).fetchone()
    if not row:raise ValueError('Upload session unavailable')
    return row['owner_name'],row['options'].get('visibility','shared')


def session_limits(row):
    return {**limits(), **(row['options'].get('limits') or {})}


def validate_settings(values):
    """Check a Settings-page submission; returns the editable values to store."""
    if not isinstance(values, dict) or set(values)!=set(EDITABLE):
        raise ValueError('Every upload limit is required')
    for name, (low, high) in EDITABLE.items():
        value = values[name]
        if type(value) is not int or not low<=value<=high:
            raise ValueError(f'{name} must be a whole number from {low} to {high}')
    if not values['max_file_bytes']<=values['max_total_bytes']<=values['staging_bytes']:
        raise ValueError('Limits must satisfy: largest file <= batch total <= staging space')
    return {name: values[name] for name in EDITABLE}


def save_limits(db, values, actor):
    app_settings.put(db, SETTING_KEY, validate_settings(values), actor)
    _cache.clear()


def reset_limits(db):
    app_settings.delete(db, SETTING_KEY)
    _cache.clear()


def settings_view():
    return {'values': {name: limits()[name] for name in EDITABLE},
            'defaults': {name: DEFAULTS[name] for name in EDITABLE},
            'bounds': {name: list(bounds) for name, bounds in EDITABLE.items()}}


def relative_path(value):
    if not isinstance(value,str) or not value or len(value.encode('utf-8'))>4096:
        raise ValueError('Invalid relative path')
    if value.startswith('/') or '\\' in value or ':' in value or any(ord(c)<32 or ord(c)==127 for c in value):
        raise ValueError('Only forward-slash relative paths are accepted')
    parts=value.split('/')
    if len(parts)<2 or len(parts)>64 or any(p in ('','.','..') or len(p.encode('utf-8'))>255 for p in parts):
        raise ValueError('Invalid folder-relative path')
    return value  # Preserve Unicode spelling; never map names onto Windows storage.


def manifest(payload, cap):
    if not isinstance(payload,dict) or not isinstance(payload.get('files'),list):raise ValueError('File manifest required')
    files=payload['files']
    if not 0<len(files)<=cap['max_files']:raise ValueError('File count limit exceeded')
    options={k:payload.get(k,False) for k in ('auto_metadata','auto_conversion','auto_duplicates')}
    if any(type(v) is not bool for v in options.values()):raise ValueError('Invalid processing options')
    options['visibility']=payload.get('visibility','shared')
    if options['visibility'] not in ('shared','private'):raise ValueError('Visibility must be shared or private')
    result=[];seen=set();root=None;total=0
    for index,f in enumerate(files):
        if not isinstance(f,dict):raise ValueError('Invalid file manifest')
        path=relative_path(f.get('path'));parts=path.split('/')
        if path in seen:raise ValueError('Repeated relative path')
        seen.add(path)
        if root is not None and root!=parts[0]:raise ValueError('Select one folder')
        root=parts[0];size=f.get('size');modified=f.get('modified',0)
        if type(size) is not int or not 0<=size<2**63 or type(modified) is not int or not 0<=modified<2**53:raise ValueError('Invalid file size or modification time')
        disposition='ignored' if should_ignore(path) or any(p.startswith('.') for p in parts[:-1]) else 'unsupported' if not kind_for(path) else 'accepted'
        if disposition=='accepted':
            if size>cap['max_file_bytes']:raise ValueError('Per-file upload limit exceeded')
            total+=size
        result.append(dict(file_id=index,relative_path=path,size=size,modified_ms=modified,disposition=disposition))
    if total>cap['max_total_bytes']:raise ValueError('Total upload limit exceeded')
    if not any(f['disposition']=='accepted' for f in result):raise ValueError('No supported media files selected')
    fingerprint=hashlib.sha256(json.dumps([result,options],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    return root,result,total,options,fingerprint


def folder(session_id):
    sid=str(uuid.UUID(str(session_id)))
    root=DATA/'uploads'
    path=root/sid
    # Refuse links at every existing ancestor; even cleanup stays inside this root.
    for p in (path,root,*root.parents):
        if p.is_symlink() or (hasattr(p,'is_junction') and p.is_junction()):raise ValueError('Linked upload storage is not allowed')
    if not path.resolve().is_relative_to(root.resolve()):raise ValueError('Invalid staging path')
    return path


def file_path(session_id,file_id,path):
    if type(file_id) is not int or file_id<0:raise ValueError('Invalid file id')
    suffix=PurePosixPath(path).suffix.lower()
    if not kind_for('file'+suffix):raise ValueError('Unsupported staged file')
    target=folder(session_id)/(str(file_id)+suffix)
    if target.is_symlink() or (hasattr(target,'is_junction') and target.is_junction()):raise ValueError('Linked upload file')
    return target


def owned(db,sid,owner,lock=False,admin=False):
    """The caller's own upload; administrators may also reach anyone's (to view, cancel or retry)."""
    row=db.execute('SELECT * FROM upload_sessions WHERE id=%s'+(' FOR UPDATE NOWAIT' if lock else ''),(uuid.UUID(str(sid)),)).fetchone()
    if not row or (row['owner_name']!=owner and not admin):raise ValueError('Upload unavailable')
    return row


def current_visibility(db,row):
    """Who can see the batch's items now. `requested_visibility` is only the choice made at upload time;
    `visibility` follows later changes: shared, private, mixed, or None while nothing has been imported yet."""
    counts={'shared':0,'private':0}
    if row['job_id']:
        for r in db.execute('SELECT visibility,count(*) n FROM media WHERE first_import_job_id=%s GROUP BY visibility',(row['job_id'],)).fetchall():
            counts[r['visibility']]=r['n']
    level=None if not any(counts.values()) else 'mixed' if all(counts.values()) else 'private' if counts['private'] else 'shared'
    return {'requested_visibility':row['options'].get('visibility','shared'),'visibility':level,'visibility_counts':counts}


def status(db,row):
    files=db.execute('SELECT file_id,relative_path,size,modified_ms,received,sha256,verified,disposition FROM upload_files WHERE session_id=%s ORDER BY file_id',(row['id'],)).fetchall()
    handoff=bool(row['job_id']) and all(f['verified'] for f in files if f['disposition']=='accepted')
    return {**current_visibility(db,row),**{k:row[k] for k in ('state','label','total_bytes','job_id','error','cancel_requested')},'id':str(row['id']),'files':files,'limits':{k:session_limits(row)[k] for k in ACCEPTED},'owner':row['owner_name'],'handoff_ready':handoff,'staging_cleaned':row['cleaned_at'] is not None}


def create(owner,payload):
    from ..config import require_destination
    require_destination(load_config())
    cap=limits();label,files,total,options,fingerprint=manifest(payload,cap)
    key=uuid.UUID(str(payload.get('request_key')))
    with connection() as db:
        db.execute('SELECT pg_advisory_xact_lock(%s)',(QUOTA_LOCK,))
        old=db.execute('SELECT * FROM upload_sessions WHERE owner_name=%s AND request_key=%s',(owner,key)).fetchone()
        if old:
            if old['manifest_hash']!=fingerprint:raise ValueError('Start key belongs to another selection')
            return status(db,old)
        if db.execute("SELECT count(*) n FROM upload_sessions WHERE state='uploading'").fetchone()['n']>=cap['max_active']:
            raise ValueError('Another upload is active; finish or cancel it first')
        reserved=db.execute('SELECT coalesce(sum(total_bytes),0) n FROM upload_sessions WHERE cleaned_at IS NULL').fetchone()['n']
        if reserved+total>cap['staging_bytes']:raise ValueError('Upload staging quota is full')
        root=DATA/'uploads';folder(uuid.uuid4())  # Validate ancestors before creating anything.
        root.mkdir(exist_ok=True)
        # Reserve space for outstanding bytes as well as already-written staging.
        remaining=db.execute("SELECT coalesce(sum(f.size-f.received),0) n FROM upload_files f JOIN upload_sessions s ON s.id=f.session_id WHERE s.cleaned_at IS NULL AND f.disposition='accepted'").fetchone()['n']
        if shutil.disk_usage(root).free<total+remaining+cap['reserve_bytes']:raise ValueError('Upload disk lacks reserved free space')
        config=load_config()
        if shutil.disk_usage(config['destination']['root']).free<reserved+total+cap['reserve_bytes']:raise ValueError('Destination lacks reserved import space')
        sid=uuid.uuid4()
        row=db.execute('INSERT INTO upload_sessions(id,owner_name,request_key,manifest_hash,label,options,total_bytes) VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING *',(sid,owner,key,fingerprint,label,Jsonb({**options,'limits':{k:cap[k] for k in ACCEPTED}}),total)).fetchone()
        for f in files:
            db.execute('INSERT INTO upload_files(session_id,file_id,relative_path,size,modified_ms,disposition) VALUES(%s,%s,%s,%s,%s,%s)',(sid,f['file_id'],f['relative_path'],f['size'],f['modified_ms'],f['disposition']))
        return status(db,row)


async def chunk(owner,sid,file_id,offset,expected,stream):
    if len(expected)!=64 or any(c not in '0123456789abcdef' for c in expected):raise ValueError('Chunk SHA-256 required')
    with connection() as db:
        session=owned(db,sid,owner,True)
        cap=session_limits(session)
        if session['state']!='uploading' or session['cancel_requested']:raise ValueError('Upload is not writable')
        row=db.execute('SELECT * FROM upload_files WHERE session_id=%s AND file_id=%s FOR UPDATE',(session['id'],file_id)).fetchone()
        if not row or row['disposition']!='accepted' or row['verified']:raise ValueError('File is not writable')
        if offset!=row['received']:raise ValueError('Offset changed; refresh upload status')
        path=file_path(sid,file_id,row['relative_path']);path.parent.mkdir(exist_ok=True)
        if shutil.disk_usage(path.parent).free<cap['chunk_bytes']+cap['reserve_bytes']:raise ValueError('Upload disk is full')
        count=0;digest=hashlib.sha256();started=time.monotonic()
        with path.open('r+b' if path.exists() else 'w+b') as out:
            if out.seek(0,2)<offset:raise ValueError('Staged file is incomplete; reselect and reset it')
            out.seek(offset);out.truncate()
            try:
                async for part in stream:
                    count+=len(part)
                    if count>cap['chunk_bytes'] or offset+count>row['size']:raise ValueError('Chunk exceeds declared limits')
                    out.write(part);digest.update(part)
                    await asyncio.sleep(max(0,count/(cap['upload_mibps']*1024**2)-(time.monotonic()-started)))
                if digest.hexdigest()!=expected:raise ValueError('Chunk checksum mismatch')
                out.flush();os.fsync(out.fileno())
            except BaseException:
                out.seek(offset);out.truncate();raise
        db.execute('UPDATE upload_files SET received=%s WHERE session_id=%s AND file_id=%s',(offset+count,session['id'],file_id))
        db.execute('UPDATE upload_sessions SET updated_at=now(),error=NULL WHERE id=%s',(session['id'],))
        return {'received':offset+count}


def verify_file(owner,sid,file_id,expected):
    with connection() as db:
        session=owned(db,sid,owner,True)
        if session['state']!='uploading' or session['cancel_requested']:raise ValueError('Upload is not writable')
        row=db.execute('SELECT * FROM upload_files WHERE session_id=%s AND file_id=%s',(session['id'],file_id)).fetchone()
        if not row or row['disposition']!='accepted' or row['received']!=row['size']:raise ValueError('File upload is incomplete')
        path=file_path(sid,file_id,row['relative_path'])
        if row['size']==0 and not path.exists():path.parent.mkdir(exist_ok=True);path.touch()
        with path.open('rb') as stream:actual=hashlib.file_digest(stream,'sha256').hexdigest()
        if path.stat().st_size!=row['size'] or actual!=expected:raise ValueError('File checksum differs; reset and re-upload this file')
        db.execute('UPDATE upload_files SET sha256=%s,verified=true WHERE session_id=%s AND file_id=%s',(actual,session['id'],file_id))
        db.execute('UPDATE upload_sessions SET updated_at=now() WHERE id=%s',(session['id'],))
        return {'verified':True}


def reset_file(owner,sid,file_id):
    with connection() as db:
        session=owned(db,sid,owner,True)
        if session['state']!='uploading':raise ValueError('Upload cannot be reset after submission')
        row=db.execute('SELECT * FROM upload_files WHERE session_id=%s AND file_id=%s',(session['id'],file_id)).fetchone()
        if not row or row['disposition']!='accepted':raise ValueError('File unavailable')
        file_path(sid,file_id,row['relative_path']).unlink(missing_ok=True)
        db.execute('UPDATE upload_files SET received=0,sha256=NULL,verified=false WHERE session_id=%s AND file_id=%s',(session['id'],file_id))
        return {'received':0}


def finalize(owner,sid):
    with connection() as db:
        session=owned(db,sid,owner,True)
        if session['job_id']:return status(db,session)
        if session['state']!='uploading':raise ValueError('Upload unavailable')
        files=db.execute('SELECT * FROM upload_files WHERE session_id=%s ORDER BY file_id',(session['id'],)).fetchall()
        if any(not f['verified'] for f in files if f['disposition']=='accepted'):raise ValueError('Verify every accepted file before importing')
        options=session['options']
        job=db.execute("INSERT INTO jobs(source_root,source_kind,source_label,source_storage,state,inventory_complete,total_bytes,auto_metadata,auto_conversion,auto_duplicates) VALUES(%s,'upload',%s,%s,'queued',true,%s,%s,%s,%s) RETURNING id",('upload:'+str(session['id']),session['label'],Jsonb({'kind':'upload','session_id':str(session['id'])}),session['total_bytes'],options['auto_metadata'],options['auto_conversion'],options.get('auto_duplicates',False))).fetchone()
        for f in files:
            if f['disposition']=='ignored':continue
            mtime=file_path(sid,f['file_id'],f['relative_path']).stat().st_mtime_ns if f['disposition']=='accepted' else 0
            db.execute('INSERT INTO import_items(job_id,source_path,size,mtime_ns,state) VALUES(%s,%s,%s,%s,%s)',(job['id'],f['relative_path'],f['size'],mtime,'pending' if f['disposition']=='accepted' else 'unsupported'))
        session=db.execute("UPDATE upload_sessions SET job_id=%s,state='queued',updated_at=now() WHERE id=%s RETURNING *",(job['id'],session['id'])).fetchone()
        return status(db,session)


def staged_file(db,job,item):
    session=db.execute('SELECT * FROM upload_sessions WHERE job_id=%s',(job['id'],)).fetchone()
    if not session or session['cancel_requested']:raise ValueError('Upload cancelled')
    f=db.execute('SELECT * FROM upload_files WHERE session_id=%s AND relative_path=%s',(session['id'],item['source_path'])).fetchone()
    if not f or not f['verified']:raise ValueError('Verified upload source missing')
    return file_path(session['id'],f['file_id'],f['relative_path']),f['sha256']


def cancel(owner,sid,admin=False):
    with connection() as db:
        row=owned(db,sid,owner,True,admin)
        if row['state']=='done':return status(db,row)
        state='cancelling' if row['state']=='importing' else 'cancelled'
        row=db.execute('UPDATE upload_sessions SET cancel_requested=true,state=%s,updated_at=now() WHERE id=%s RETURNING *',(state,row['id'])).fetchone()
        if row['job_id'] and state=='cancelled':db.execute("UPDATE jobs SET state='cancelled' WHERE id=%s AND state<>'completed-review-required'",(row['job_id'],))
        result=status(db,row)
    if state=='cancelling' and row['job_id']:
        from ..jobs import pause
        try:pause(row['job_id'])
        except RuntimeError:pass  # Dispatcher/importer also checks the durable flag.
    return result


def retry(owner,sid,admin=False):
    with connection() as db:
        row=owned(db,sid,owner,True,admin)
        if row['state']=='queued':return status(db,row)
        if row['state'] not in ('paused','failed') or row['cleaned_at'] or row['cancel_requested']:raise ValueError('Upload cannot be retried')
        row=db.execute("UPDATE upload_sessions SET state='queued',error=NULL,updated_at=now() WHERE id=%s RETURNING *",(row['id'],)).fetchone()
        return status(db,row)


def cleanup(db,row):
    """Caller holds pipeline locks. Keep any source still needed for recovery."""
    if row['state']!='done' or not row['job_id']:return False
    job=db.execute('SELECT * FROM jobs WHERE id=%s',(row['job_id'],)).fetchone()
    if not job or job['state']!='completed-review-required':return False
    from ..jobs.job_scope import pending_work
    if job['auto_metadata'] and pending_work(db,job['id'],'metadata'):return False
    if job['auto_metadata'] and job['auto_conversion'] and pending_work(db,job['id'],'conversion'):return False
    files=db.execute("SELECT f.*,i.state import_state,m.relative_path stored_path,m.sha256 original_sha,m.stored_sha256,m.size stored_size FROM upload_files f LEFT JOIN import_items i ON i.job_id=%s AND i.source_path=f.relative_path LEFT JOIN media m ON m.id=i.media_id OR (i.state='duplicate' AND i.media_id IS NULL AND m.sha256=f.sha256) WHERE f.session_id=%s AND f.disposition='accepted' ORDER BY f.file_id",(job['id'],row['id'])).fetchall()
    if any(not f['verified'] or f['import_state'] not in ('imported','duplicate') or not f['stored_path'] for f in files):return False
    from ..storage.disks import Monitor
    from ..library.media import digest,TrafficLimit,safe_media_path
    config=load_config();monitor=Monitor(config,roles=('destination',))
    try:
        monitor.prepare()
        # Verify every destination before deleting ANY staging file. The normal
        # importer already verified publication; this protects later recovery too.
        for f in files:
            target=safe_media_path(config['destination']['root'],f['stored_path'])
            monitor.check()
            if target.stat().st_size!=f['stored_size'] or digest(target,monitor.check,TrafficLimit(12))!=(f['stored_sha256'] or f['original_sha']):
                raise OSError('Destination verification failed; staged originals retained')
    finally:
        if hasattr(monitor,'thread'):monitor.close()
    path=folder(row['id'])
    expected={file_path(row['id'],f['file_id'],f['relative_path']) for f in files}
    if path.exists() and any(p not in expected for p in path.iterdir()):raise OSError('Unexpected staging contents; cleanup stopped')
    for staged in expected:staged.unlink(missing_ok=True)
    if path.exists():path.rmdir()  # Unexpected contents stop cleanup, never recurse.
    db.execute("UPDATE upload_sessions SET cleaned_at=now(),error=CASE WHEN error LIKE 'Staging retained:%%' THEN NULL ELSE error END WHERE id=%s",(row['id'],))
    return True


def dispatch_once():
    from ..service.macos_lifecycle import stopping
    if stopping():return
    from .. import jobs
    with connection() as leader:
        if not leader.execute('SELECT pg_try_advisory_lock(%s) ok',(DISPATCH_LOCK,)).fetchone()['ok']:return
        # An empty scheduler poll must not interrupt per-file Trash operations.
        hours=limits()['expiry_hours']
        if not leader.execute(f"""SELECT EXISTS(SELECT 1 FROM upload_sessions WHERE
            state IN ('importing','cancelling') OR (state='queued' AND NOT cancel_requested)
            OR (state='done' AND cleaned_at IS NULL)
            OR (state='uploading' AND {EXPIRED})) ready""",(hours,)).fetchone()['ready']:
            return
        # All transitions/cleanup wait for existing media work and decoder review.
        with connection() as db:
            try:jobs.idle(db)
            except RuntimeError:
                if jobs.duplicate_active() and db.execute("SELECT EXISTS(SELECT 1 FROM upload_sessions WHERE state='queued' AND NOT cancel_requested) ready").fetchone()['ready']:
                    (DATA/'PHOTO-DUPLICATE-PAUSE').touch()
                    (DATA/'PIPELINE-PAUSE').touch()
                return
            for name in ('video-status.json','avi-status.json','photo-duplicate-status.json'):
                p=DATA/name
                if p.exists() and json.loads(p.read_text()).get('state')=='paused-error':return
            db.execute(f"UPDATE upload_sessions SET state='cancelled',cancel_requested=true WHERE state='uploading' AND {EXPIRED}",(hours,))
            # Advisory locks and decoder review are clear. Recover only interrupted
            # normal imports; explicit pauses/cancellation and faults stay stopped.
            for session in db.execute("SELECT * FROM upload_sessions WHERE state IN ('importing','cancelling') FOR UPDATE").fetchall():
                job=db.execute('SELECT state,error FROM jobs WHERE id=%s',(session['job_id'],)).fetchone()
                if session['cancel_requested']:state='cancelled'
                elif job['state']=='completed-review-required':state='done'
                elif job['state'] in ('running','queued') and not job['error'] and not any((DATA/p).exists() for p in ('PAUSE','PIPELINE-PAUSE')):state='queued'
                else:state='paused'
                db.execute('UPDATE upload_sessions SET state=%s,error=%s WHERE id=%s',(state,'Server stopped; review and resume this upload import' if state=='paused' else None,session['id']))
            for row in db.execute("SELECT * FROM upload_sessions WHERE state='done' AND cleaned_at IS NULL FOR UPDATE").fetchall():
                try:cleanup(db,row)
                except OSError as error:
                    db.execute('UPDATE upload_sessions SET error=%s WHERE id=%s',('Staging retained: '+str(error)[:900],row['id']))
            row=db.execute("SELECT * FROM upload_sessions WHERE state='queued' AND NOT cancel_requested ORDER BY created_at LIMIT 1 FOR UPDATE").fetchone()
            if not row:return
            db.execute("UPDATE upload_sessions SET state='importing',updated_at=now() WHERE id=%s",(row['id'],))
        # jobs.run acquires the same locks; if another launch wins, leave queued.
        try:
            jobs.launch(row['job_id'],'import',inline=True)
        except Exception as error:
            with connection() as db:
                job=db.execute('SELECT state FROM jobs WHERE id=%s',(row['job_id'],)).fetchone()
                # Upload/import may have finished before an optional video stage
                # failed. Its retry belongs to that stage, not to the upload.
                state='done' if job['state']=='completed-review-required' else 'queued' if job['state']=='queued' else 'failed'
                db.execute('UPDATE upload_sessions SET state=%s,error=%s WHERE id=%s',(state,str(error)[:1000],row['id']))
        else:
            with connection() as db:
                session=db.execute('SELECT * FROM upload_sessions WHERE id=%s FOR UPDATE',(row['id'],)).fetchone()
                job=db.execute('SELECT state,error FROM jobs WHERE id=%s',(row['job_id'],)).fetchone()
                state='cancelled' if session['cancel_requested'] else 'done' if job['state']=='completed-review-required' else 'paused'
                db.execute('UPDATE upload_sessions SET state=%s,error=%s,updated_at=now() WHERE id=%s',(state,job['error'],row['id']))


def start_dispatcher():
    stop=threading.Event()
    def work():
        from ..telemetry import logger
        while not stop.wait(3):
            try:dispatch_once()
            except Exception:logger('uploads').exception('upload-dispatch-failed')
    thread=threading.Thread(target=work,daemon=True,name='upload-queue');stop.worker_thread=thread;thread.start()
    return stop
