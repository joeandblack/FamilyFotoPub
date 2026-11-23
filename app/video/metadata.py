"""Destination-only metadata and posters; AVI conversion runs separately."""
import argparse
import ctypes
from datetime import datetime, timezone
import json
import io
import math
import os
from pathlib import Path
import subprocess
import sys
import time
from psycopg.types.json import Jsonb
from PIL import Image
from ..config import DATA, ROOT, load_config
from ..database import connection, migrate
from ..storage.disks import DiskError, Monitor, match_disk
from ..imports.importer import IMPORT_LOCK
from ..library.media import digest, safe_media_path, TrafficLimit
from ..telemetry import logger, utcnow, write_json
from ..photos.thumbnails import thumbnail_path

FORMATS = 'mov,avi,flv,mpegts,mpeg,mpegvideo,matroska,webm,asf,aac,mp3,wav,flac,ogg,aiff'


def is_sidecar(name, header):
    return name.startswith('._') and header in (b'\x00\x05\x16\x07',b'\x00\x05\x16\x00')


def parse_metadata(probe, extension):
    streams = probe.get('streams', [])
    video = next((s for s in streams if s.get('codec_type') == 'video' and not s.get('disposition',{}).get('attached_pic')), {})
    audio = next((s for s in streams if s.get('codec_type') == 'audio'), {})
    if not video and not audio:raise ValueError('No video stream or audio stream found')
    fmt = probe.get('format', {})
    try:
        duration = float(fmt.get('duration'))
        if not math.isfinite(duration) or duration < 0:
            duration = None
    except (TypeError, ValueError):
        duration = None
    raw = (fmt.get('tags') or {}).get('creation_time')
    provenance = 'container.creation_time'
    if not raw:
        raw = (video.get('tags') or {}).get('creation_time')
        provenance = 'video_stream.creation_time'
    captured = None
    if raw:
        try:
            date = datetime.fromisoformat(raw.replace('Z', '+00:00'))
            if 1900 <= date.year <= datetime.now().year+1:
                captured = date.astimezone(timezone.utc).replace(tzinfo=None) if date.tzinfo else date
        except (ValueError, TypeError):
            pass
    compatible = extension in ('.mp4','.m4v','.mov') and video.get('codec_name') == 'h264' and audio.get('codec_name') in (None,'aac','mp3')
    metadata = {'duration_seconds':duration, 'width':video.get('width'), 'height':video.get('height'),
                'media_type':'video' if video else 'audio',
                'video_codec':video.get('codec_name'), 'audio_codec':audio.get('codec_name'),
                'format':fmt.get('format_name'), 'capture_time_raw':raw,
                'capture_time_provenance':provenance if captured else None,
                'capture_time_caveat':'Creation tag may represent encoding time, not shooting time.' if captured else None,
                'capture_time_timezone':'UTC' if captured and date.tzinfo else 'unspecified',
                'browser_compatibility':'Likely compatible; actual browser support varies' if compatible else 'Original may need an external player; no converted video created',
                'poster':False}
    return metadata, captured


def tool_paths():
    config = load_config().get('video_tools', {})
    result = {}
    for name in ('ffmpeg','ffprobe'):
        suffix = '.exe' if os.name == 'nt' else ''
        path = Path(config.get(name, ROOT/'runtime'/'ffmpeg'/'bin'/f'{name}{suffix}'))
        if not path.is_file():
            raise FileNotFoundError(f'{name} unavailable; see docs/VIDEO.md')
        result[name] = str(path)
    return result


def run_tool(command, timeout=60, reject_stderr=False):
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=(getattr(subprocess,'CREATE_NO_WINDOW',0)|getattr(subprocess,'IDLE_PRIORITY_CLASS',0)))
    lease=DATA/'video-decoder-busy.json'
    write_json(lease, {'pid':process.pid, 'tool':Path(command[0]).name, 'state':'active', 'updated':utcnow()})
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # Do not kill a potentially blocked storage operation or start another decoder.
        write_json(lease, {'pid':process.pid, 'tool':Path(command[0]).name, 'state':'review-required', 'updated':utcnow()})
        raise DiskError(f'Video decoder exceeded {timeout} seconds; safe pause, child not killed. Review video-decoder-busy.json before continuing.')
    lease.unlink()
    if process.returncode or (reject_stderr and stderr.strip()):
        raise ValueError(stderr.decode(errors='replace')[-1200:])
    return stdout


def extract(path, sha, tools, guard, progress=None):
    guard()
    probe = json.loads(run_tool([tools['ffprobe'],'-v','error','-protocol_whitelist','file','-format_whitelist',FORMATS,
        '-probesize','2097152','-analyzeduration','5000000','-show_entries',
        'format=duration,format_name:format_tags=creation_time:stream=codec_type,codec_name,width,height:stream_disposition=attached_pic:stream_tags=creation_time',
        '-of','json',str(path)]))
    guard()
    metadata, captured = parse_metadata(probe, path.suffix.lower())
    if progress:
        progress(metadata)
    if metadata['media_type']=='audio':return metadata,captured,None
    target = thumbnail_path(DATA/'thumbnails', sha)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.stem+'.video-partial.jpg')
    failure = 'No decodable poster frame'
    for fallback in (False, True):
        guard()
        partial.unlink(missing_ok=True)  # Never accept an earlier attempt's output.
        command = [tools['ffmpeg'],'-nostdin','-v','error','-y','-protocol_whitelist','file','-format_whitelist',FORMATS,
            '-threads','1','-probesize','2097152','-analyzeduration','5000000',
            *(['-ignore_editlist','1'] if fallback and path.suffix.lower() in ('.mp4','.mov','.m4v') else []),
            '-i',str(path),'-map','0:v:0','-frames:v','1','-an','-vf',
            'scale=640:480:force_original_aspect_ratio=decrease',
            *(['-fps_mode','passthrough'] if fallback else []),'-threads','1',str(partial)]
        try:
            run_tool(command)
        except ValueError as error:
            guard(); path.stat(); target.parent.stat()
            if any(message in str(error).casefold() for message in ('permission denied','access is denied','no space left','input/output error','error opening output','read-only file system')):
                raise DiskError(f'Poster storage failure: {error}') from error
            failure = str(error)
            continue
        guard()
        try:
            payload = partial.read_bytes()
        except FileNotFoundError:
            # Only absent decoder output is recoverable, not lost source/storage.
            path.stat(); target.parent.stat(); guard()
            failure = 'Decoder produced no poster frame'
            continue
        try:
            with Image.open(io.BytesIO(payload)) as image:
                image.verify()
        except (OSError, ValueError, SyntaxError) as error:
            # Decode bytes already read successfully: real filesystem errors above propagate.
            failure = f'Invalid poster image: {error}'
            continue
        with partial.open('r+b') as stream:
            os.fsync(stream.fileno())
        partial.replace(target)
        metadata['poster'] = True
        return metadata, captured, None
    guard()
    partial.unlink(missing_ok=True)
    return metadata, captured, f'Poster unavailable: {failure}'


def readonly_monitor(config):
    monitor = Monitor(config, roles=('destination',))
    monitor.verify()
    marker = json.loads(monitor.marker.read_text(encoding='utf-8'))
    match_disk(config['destination'], marker)
    monitor.marker_id = marker['library_id']
    monitor.last_ok = time.monotonic()
    import threading
    monitor.thread = threading.Thread(target=monitor.run,daemon=True)
    monitor.thread.start()
    return monitor


def background_priority():
    if os.name == 'nt':
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.GetCurrentProcess.restype=wintypes.HANDLE
        kernel.SetPriorityClass.argtypes=[wintypes.HANDLE,wintypes.DWORD]
        kernel.SetPriorityClass.restype=wintypes.BOOL
        if not kernel.SetPriorityClass(kernel.GetCurrentProcess(),0x00100000):
            error=ctypes.get_last_error()
            # Automatic stages share a process; metadata may have set this
            # already. Keep background mode and reject every other failure.
            if error!=402:  # ERROR_PROCESS_MODE_ALREADY_BACKGROUND
                raise RuntimeError(f'Unable to enter Windows background priority: {error}')


def check_ready(db,job_id=None):
    previous = DATA/'video-status.json'
    if previous.exists() and json.loads(previous.read_text(encoding='utf-8')).get('state') == 'paused-error':
        raise RuntimeError('Video processing fault requires review before resuming')
    if (DATA/'video-decoder-busy.json').exists():
        raise RuntimeError('Previous decoder needs review; no worker started')
    if not db.execute('SELECT pg_try_advisory_lock(%s) locked',(IMPORT_LOCK,)).fetchone()['locked']:
        raise RuntimeError('Another importer/maintenance worker is active')
    if job_id is not None:
        from ..jobs.job_scope import require_import_complete
        require_import_complete(db,job_id)
        db.commit()
        return
    if db.execute("SELECT count(*) count FROM jobs WHERE state NOT IN ('completed-review-required','cancelled') OR EXISTS(SELECT 1 FROM import_items WHERE state IN ('pending','error'))").fetchone()['count']:
        raise RuntimeError('Import queue must be complete first')
    db.commit()


def legacy_library():
    """Whether this is the original Windows library, which had to migrate its thumbnails.

    It kept thumbnails flat, as thumbnails/<sha>.jpg, and its review and migration left a
    report each. A fresh install has neither a report nor a flat thumbnail.
    """
    if any((DATA/name).exists() for name in ('post-import-review.json', 'thumbnail-migration.json')):
        return True
    try:
        with os.scandir(DATA/'thumbnails') as entries:
            return any(entry.name.endswith('.jpg') and entry.is_file() for entry in entries)
    except FileNotFoundError:
        return False


def check_legacy_prerequisites():
    if sys.platform != 'darwin' and legacy_library():
        # Legacy Windows library migration prerequisites. Fresh imports use the
        # current thumbnail layout and are checked by scoped check_ready below.
        review = json.loads((DATA/'post-import-review.json').read_text())
        migration = json.loads((DATA/'thumbnail-migration.json').read_text())
        if review['state'] != 'passed' or migration['state'] != 'completed':
            raise RuntimeError('Completed import review and thumbnail migration required')


def publish_status(db, job_id, status):
    from ..jobs.job_scope import membership,save_status
    status['counts'] = {r['state']:r['count'] for r in db.execute('SELECT state,count(*) count FROM video_metadata v WHERE NOT EXISTS(SELECT 1 FROM media_trash t WHERE t.media_id=v.media_id) AND '+membership('v')+' GROUP BY state',(job_id,)).fetchall()}
    rows=db.execute("SELECT m.first_import_job_id job_id,v.state,count(*) count FROM media m JOIN video_metadata v ON v.media_id=m.id WHERE m.first_import_job_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM media_trash t WHERE t.media_id=v.media_id) GROUP BY m.first_import_job_id,v.state").fetchall()
    status['job_counts']={}
    for row in rows:
        status['job_counts'].setdefault(str(row['job_id']),{})[row['state']]=row['count']
    db.commit()
    status['updated'] = utcnow()
    write_json(DATA/'video-status.json',status)
    save_status(db,job_id,'metadata',status)


def next_pending(db, job_id, ids):
    from ..jobs.job_scope import membership
    query = "SELECT m.* FROM media m JOIN video_metadata v ON v.media_id=m.id WHERE v.state='pending' AND NOT EXISTS(SELECT 1 FROM media_trash t WHERE t.media_id=m.id)"
    query += ' AND '+membership()
    params = [job_id]
    if ids:
        query += ' AND m.id=ANY(%s)'; params.append(ids)
    media = db.execute(query+' ORDER BY m.id LIMIT 1', params).fetchone()
    db.commit()
    return media


def verify_copy(path, media, monitor):
    before = path.stat()
    if before.st_size != media['size'] or digest(path,monitor.check,TrafficLimit(0)) != (media.get('stored_sha256') or media['sha256']):
        raise DiskError(f'Verified video copy changed: media {media["id"]}. Nothing overwritten.')
    return before


def read_metadata(path, media, tools, monitor, status, publish):
    """Returns (state, metadata, captured, error) for one verified video."""
    metadata={}; captured=None; error=None
    with path.open('rb') as stream:
        ancillary = is_sidecar(media['name'],stream.read(4))
    state = 'ancillary' if ancillary else 'done'
    if not ancillary:
        try:
            status['current_stage'] = 'Reading metadata'; publish()
            def probed(metadata):
                status['current_duration_seconds'] = metadata.get('duration_seconds')
                status['current_stage'] = 'Creating thumbnail'
                publish()
            metadata,captured,error=extract(path,media['sha256'],tools,monitor.check,probed)
        except ValueError as failure:
            error=f'Decoder limitation: {failure}'
        if error: state='warning'
    else:
        metadata={'classification':'AppleDouble metadata sidecar','poster':False}
    return state, metadata, captured, error


def record_result(db, media, state, metadata, captured, error, verified_at):
    db.execute('UPDATE video_metadata SET state=%s,metadata=%s,error=%s,verified_at=%s,updated_at=now() WHERE media_id=%s',
        (state,Jsonb(metadata),error,verified_at,media['id']))
    if captured:
        db.execute('UPDATE media SET captured_at=coalesce(captured_at,%s),width=%s,height=%s WHERE id=%s',
            (captured,metadata['width'],metadata['height'],media['id']))
    else:
        db.execute('UPDATE media SET width=coalesce(%s,width),height=coalesce(%s,height) WHERE id=%s',
            (metadata.get('width'),metadata.get('height'),media['id']))
    db.commit()


def process_media(db, config, tools, monitor, media, status, publish, log):
    status['current'] = media['id']
    status['current_duration_seconds'] = None
    status['current_stage'] = 'Verifying checksum'
    publish()
    path = safe_media_path(config['destination']['root'],media['relative_path'])
    before = verify_copy(path,media,monitor)
    verified_at = utcnow()
    state, metadata, captured, error = read_metadata(path,media,tools,monitor,status,publish)
    monitor.check()
    after = path.stat()
    if (after.st_size,after.st_mtime_ns) != (before.st_size,before.st_mtime_ns):
        raise DiskError('Destination changed during metadata read')
    record_result(db,media,state,metadata,captured,error,verified_at)
    log.info('video-processed id=%s state=%s error=%s',media['id'],state,error)


def write_sample_review(ids):
    with connection() as db:
        samples=db.execute('SELECT media_id,state,metadata,error,verified_at FROM video_metadata WHERE media_id=ANY(%s) ORDER BY media_id',(ids,)).fetchall()
    for sample in samples:
        sample['verified_at']=sample['verified_at'].isoformat() if sample['verified_at'] else None
    write_json(DATA/'video-sample-review.json',{'updated':utcnow(),'samples':samples})


def run(max_items=0, ids=None, job_id=None):
    if job_id is None:
        raise RuntimeError('Select an import group for video processing')
    from ..jobs.job_scope import membership,elapsed_base,pending_work
    with connection() as db:
        if not pending_work(db,job_id,'metadata'):return
    if (DATA/'video-decoder-busy.json').exists():
        raise RuntimeError('Previous decoder needs review; no worker started')
    config = load_config()
    tools = tool_paths()
    check_legacy_prerequisites()
    migrate()
    log = logger('video')
    background_priority()
    monitor = None
    status = {'state':'running','phase':'metadata','scope':'original-import','job_id':job_id,'started':utcnow(),'current':None}
    with connection() as db:
        check_ready(db,job_id)
        status['elapsed_base']=elapsed_base(db,job_id,'metadata')
        db.execute("INSERT INTO video_metadata(media_id) SELECT m.id FROM media m WHERE kind='video' AND "+membership()+" ON CONFLICT DO NOTHING",(job_id,))
        db.commit()
        def publish():
            publish_status(db,job_id,status)
        try:
            monitor = readonly_monitor(config)
            processed = 0
            while True:
                publish()
                monitor.check()
                if (DATA/'VIDEO-PAUSE').exists():
                    status['state']='paused'; break
                media = next_pending(db,job_id,ids)
                if media is None:
                    status['state'] = 'sample-completed' if ids else 'completed-review-required'; break
                process_media(db,config,tools,monitor,media,status,publish,log)
                processed += 1
                if max_items and processed>=max_items:
                    status['state']='sample-completed'; break
        except Exception as error:
            db.rollback()
            status['state']='paused-error';status['error']=f'{type(error).__name__}: {error}'
            log.exception('video-paused')
            raise
        finally:
            status['current']=None
            status['current_duration_seconds']=None
            status['current_stage']=None
            publish()
            if monitor: monitor.close()
    print(status,flush=True)
    if ids:
        write_sample_review(ids)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description='Destination-only video metadata and posters; conversion is separate.')
    parser.add_argument('--job',type=int,required=True)
    args=parser.parse_args()
    from ..jobs import run as pipeline_run
    pipeline_run(args.job,'metadata')
