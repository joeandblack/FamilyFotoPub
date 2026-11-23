"""Opt-in AVI replacement; catalog publication always precedes AVI cleanup."""
import json
import math
from pathlib import Path
import shutil
import time
from types import SimpleNamespace
from psycopg.types.json import Jsonb
from ..config import DATA
from ..storage.disks import DiskError
from ..library.media import copy_verified, digest, safe_media_path, TrafficLimit
from ..telemetry import logger
from . import staging


def validate_conversion(before, after, decoded_counts=None):
    def streams(probe):
        return [s for s in probe['streams'] if s['codec_type']=='video'], [s for s in probe['streams'] if s['codec_type']=='audio']
    old_video, old_audio = streams(before)
    new_video, new_audio = streams(after)
    if len(old_video)!=1 or len(old_audio)>1:
        raise ValueError('Multiple streams require manual review; AVI retained')
    if len(new_video)!=1 or len(new_audio)!=len(old_audio):
        raise ValueError('Converted stream count differs')
    old, new = old_video[0], new_video[0]
    if (old['width'],old['height'])!=(new['width'],new['height']):
        raise ValueError('Conversion changed resolution')
    if new['codec_name']!='h264' or any(s['codec_name']!='aac' for s in new_audio):
        raise ValueError('Unexpected converted codecs')
    durations=[float(p['format']['duration']) for p in (before,after)]
    if any(not math.isfinite(d) or d<=0 for d in durations) or abs(durations[0]-durations[1])>0.2:
        raise ValueError('Conversion duration differs or is unavailable')
    if old.get('nb_frames') and new.get('nb_frames') and old['nb_frames']!=new['nb_frames']:
        if decoded_counts is None:
            raise ValueError('Conversion frame count differs')
        counts=decoded_counts()
        if len(counts)!=2 or any(type(n) is not int or n<=0 for n in counts) or counts[0]!=counts[1]:
            raise ValueError('Conversion decoded frame count differs or is unavailable')
        return {'source_header':old['nb_frames'],'output_header':new['nb_frames'],
                'source_decoded':counts[0],'output_decoded':counts[1]}


def decoded_video_frames(path,tools,guard):
    """Strict full decoding to null; passthrough prevents count-changing sync."""
    from .metadata import run_tool
    guard()
    output=run_tool([tools['ffmpeg'],'-nostdin','-v','error','-xerror',
        '-max_error_rate','0','-err_detect','explode','-threads','2',
        '-protocol_whitelist','file','-i',str(path),'-map','0:v:0','-an',
        '-fps_mode','passthrough','-enc_time_base','demux','-threads','2','-nostats','-stats_period','60',
        '-progress','pipe:1','-f','null','-'],timeout=3600,reject_stderr=True)
    guard()
    # Only a completed progress block is evidence, never a packet/header count.
    block={};completed=None;previous=0
    for line in output.decode('utf-8',errors='strict').splitlines():
        key,separator,value=line.partition('=')
        if not separator:continue
        block[key.strip()]=value.strip()
        if key=='progress':
            try:count=int(block['frame'])
            except (KeyError,ValueError):raise ValueError('Decoded frame count unavailable')
            if count<previous:raise ValueError('Decoded frame count regressed')
            previous=count
            completed=count if value.strip()=='end' else None
            block={}
    if completed is None or completed<=0:raise ValueError('Full video decode did not complete')
    return completed


def probe(path, tools):
    from .metadata import run_tool
    return json.loads(run_tool([tools['ffprobe'],'-v','error','-protocol_whitelist','file',
        '-show_streams','-show_format','-of','json',str(path)]))


def verify(path, expected, size, guard, io_mibps=0):
    guard()
    if path.stat().st_size!=size or digest(path,guard,TrafficLimit(io_mibps))!=expected:
        raise DiskError('Conversion file integrity check failed; nothing removed')


def stage_original(ctx):
    """Copy the original next to the encoder and verify it against the catalog hash."""
    media,guard=ctx.media,ctx.guard
    guard()
    before_stat=ctx.source.stat()
    if before_stat.st_size!=media['size']:
        raise DiskError('Conversion source size changed; original retained')
    # The staged copy is checked against the catalog hash before decoding.
    # Hashing the original separately would reread the same bytes needlessly.
    stage=DATA/'conversion-temp'/str(media['id'])
    stage.mkdir(parents=True,exist_ok=True)
    if shutil.disk_usage(stage).free < media['size']*3+5*1024**3:
        raise DiskError('D: lacks AVI staging headroom')
    local=stage/('input.'+ctx.format_name)
    ctx.report('Copying original')
    copy_verified(ctx.source,local,media['sha256'],guard,TrafficLimit(ctx.io_mibps))
    output=stage/'output.mp4'
    if output.exists():
        # Preserve prior incomplete attempts rather than overwriting them.
        output.rename(stage/f'previous-{time.time_ns()}.mp4')
    return SimpleNamespace(stage=stage,local=local,output=output,before_stat=before_stat)


def inspect_original(ctx,local):
    ctx.report('Inspecting original')
    before=probe(local,ctx.tools)
    video=[s for s in before['streams'] if s['codec_type']=='video']
    audio=[s for s in before['streams'] if s['codec_type']=='audio']
    if len(video)!=1 or len(audio)>1 or video[0].get('width',0)%2 or video[0].get('height',0)%2:
        raise ValueError('Multiple streams or odd dimensions require review; original retained')
    return before,audio


def create_mp4(ctx,files,before):
    """Run the encoder; returns the saved FLV/MTS plan, or None for AVI."""
    from .metadata import run_tool
    tools,format_name=ctx.tools,ctx.format_name
    selected=None
    if ctx.flv_handler:
        if format_name=='mts':from .mts import plan,command
        else:from .flv import plan,command
        from ..telemetry import write_json
        selected=plan(before)
        checkpoint={'sha256':ctx.media['sha256'],'plan':selected}
        plan_path=files.stage/(format_name+'-plan.json')
        if plan_path.exists() and json.loads(plan_path.read_text(encoding='utf-8'))!=checkpoint:
            raise DiskError(f'Saved {format_name.upper()} conversion plan changed; review required')
        write_json(plan_path,checkpoint)
        ctx.report('Creating MP4')
        run_tool(command(tools,files.local,files.output,selected),timeout=3600)
    else:
        ctx.report('Creating MP4')
        run_tool([tools['ffmpeg'],'-nostdin','-n','-v','error','-xerror',
        '-protocol_whitelist','file','-format_whitelist','avi','-i',str(files.local),
        '-map','0:v:0','-map','0:a:0?','-map_metadata','0','-c:v','libx264',
        '-preset','medium','-crf','20','-pix_fmt','yuv420p','-threads','2',
        '-c:a','aac','-b:a','128k','-movflags','+faststart',str(files.output)], timeout=3600)
    return selected


def validate_mp4(ctx,files,before):
    """Check the MP4 against the original; returns the frame-count evidence, if any."""
    from .metadata import run_tool
    tools,guard,quick,format_name=ctx.tools,ctx.guard,ctx.quick,ctx.format_name
    local,output=files.local,files.output
    guard()
    ctx.report('Checking MP4' if quick else 'Validating playback')
    after=probe(output,tools)
    decoded_counts=None if quick else lambda:(decoded_video_frames(local,tools,guard),decoded_video_frames(output,tools,guard))
    frame_validation=validate_conversion(before,after,decoded_counts)
    if ctx.flv_handler and not quick and not frame_validation:
        counts=(decoded_video_frames(local,tools,guard),decoded_video_frames(output,tools,guard))
        if counts[0]!=counts[1]:raise ValueError(f'{format_name.upper()} decoded frame count differs; original retained')
        frame_validation={'source_decoded':counts[0],'output_decoded':counts[1]}
    if frame_validation:
        from ..telemetry import write_json
        write_json(files.stage/'frame-validation.json',frame_validation)
    if not quick:
        run_tool([tools['ffmpeg'],'-nostdin','-v','error','-xerror','-max_error_rate','0',
            '-err_detect','explode','-threads','2','-protocol_whitelist','file',
            '-i',str(output),'-fps_mode','passthrough','-enc_time_base','demux','-threads','2','-f','null','-'],timeout=3600,reject_stderr=True)
    return frame_validation


def publish_mp4(ctx,output):
    """Copy the verified MP4 to its content-addressed path; returns (relative, sha, size, target)."""
    guard,root=ctx.guard,ctx.root
    sha=digest(output,guard,TrafficLimit(ctx.io_mibps)); size=output.stat().st_size
    relative=f'{sha[:2]}/{sha[2:4]}/{sha}.mp4'
    target=safe_media_path(root,relative)
    if target.exists():
        verify(target,sha,size,guard,ctx.io_mibps)
    else:
        if shutil.disk_usage(Path(root)).free < size+5*1024**3:
            raise DiskError('Destination lacks MP4 publication headroom')
        copy_verified(output,target,sha,guard,TrafficLimit(ctx.io_mibps))
    return relative,sha,size,target


def record_conversion(ctx,relative,sha,size,frame_validation,audio,selected):
    """Point the catalog at the published MP4 and record how it was validated."""
    db,media,quick,format_name=ctx.db,ctx.media,ctx.quick,ctx.format_name
    db.execute("UPDATE media SET original_relative_path=relative_path,original_size=size,relative_path=%s,size=%s,stored_sha256=%s,conversion_state='published',conversion_error=NULL WHERE id=%s",
        (relative,size,sha,media['id']))
    row=db.execute('SELECT metadata FROM video_metadata WHERE media_id=%s',(media['id'],)).fetchone()
    metadata=dict(row['metadata'] or {})
    metadata['conversion_validation']={'mode':'quick' if quick else 'full',
        'byte_integrity':'sha256-verified','playback':'review-required' if quick else 'decoded',
        'io_mibps':ctx.io_mibps,'prepared_seconds':round(time.monotonic()-ctx.started,3)}
    metadata.pop('frame_validation',None)
    if frame_validation:metadata['frame_validation']=frame_validation
    metadata.update(video_codec='h264',audio_codec='aac' if audio else None,
        format='mp4',browser_compatibility='Converted MP4 (H.264/AAC); original source AVI retained on source disk',
        conversion={'crf':20,'preset':'medium','original_sha256':media['sha256'],'stored_sha256':sha})
    if ctx.flv_handler:
        metadata['browser_compatibility']=f'Converted MP4 (H.264/AAC); destination {format_name.upper()} retained'
        metadata['conversion']={**selected,'original_sha256':media['sha256'],'stored_sha256':sha}
    db.execute('UPDATE video_metadata SET metadata=%s,updated_at=now() WHERE media_id=%s',(Jsonb(metadata),media['id']))
    db.commit()
    refreshed=db.execute('SELECT * FROM media WHERE id=%s',(media['id'],)).fetchone();db.commit()
    return refreshed


def publish_conversion(ctx):
    """Convert, validate and publish. Returns None when the output duplicates another item."""
    db,media,guard=ctx.db,ctx.media,ctx.guard
    files=stage_original(ctx)
    before,audio=inspect_original(ctx,files.local)
    selected=create_mp4(ctx,files,before)
    frame_validation=validate_mp4(ctx,files,before)
    guard()
    after_stat=ctx.source.stat()
    if (files.before_stat.st_size,files.before_stat.st_mtime_ns)!=(after_stat.st_size,after_stat.st_mtime_ns):
        raise DiskError('Original changed during conversion; nothing removed')
    ctx.report('Publishing MP4')
    relative,sha,size,target=publish_mp4(ctx,files.output)
    guard()
    if ctx.flv_handler and db.execute('SELECT 1 FROM media WHERE relative_path=%s AND id<>%s',(relative,media['id'])).fetchone():
        raise ValueError(f'{ctx.format_name.upper()} output matches an existing catalog item; review required, original retained')
    if not ctx.flv_handler and mark_output_duplicate(db,media,relative,sha,size,ctx.root,guard):
        return None  # Keep original AVI and staging evidence; never share paths.
    refreshed=record_conversion(ctx,relative,sha,size,frame_validation,audio,selected)
    return SimpleNamespace(media=refreshed,target=target,files=files)


def finish_conversion(ctx,media,target,staged):
    """Retention checks and cleanup shared by fresh publication and resume.

    `staged` is the evidence from this call's own publication, None on resume.
    """
    db,guard,source,io_mibps,flv_handler=ctx.db,ctx.guard,ctx.source,ctx.io_mibps,ctx.flv_handler
    hold=DATA/'conversion-recovery'/str(media['id'])/'retain-original.json'
    preserve_original=hold.exists()
    if preserve_original and json.loads(hold.read_text())!={'media_id':media['id'],'sha256':media['sha256']}:
        raise DiskError('Original-retention recovery record does not match media; cleanup stopped')
    if flv_handler:
        # Fresh publication already verified the staged original against its hash.
        # Resume has no such in-memory evidence and must verify the retained file.
        if staged is None:
            verify(source,media['sha256'],media['original_size'],guard,io_mibps)
        else:
            guard()
            current_stat=source.stat()
            before_stat=staged.files.before_stat
            if (before_stat.st_size,before_stat.st_mtime_ns)!=(current_stat.st_size,current_stat.st_mtime_ns):
                raise DiskError('Original changed during publication; review required')
    if not flv_handler and ctx.config['avi_conversion'].get('remove_destination_avi') and not preserve_original:
        # Recheck both exact files immediately before deleting only the enrolled E: copy.
        verify(target,media['stored_sha256'],media['size'],guard,io_mibps)
        if source.exists():
            verify(source,media['sha256'],media['original_size'],guard,io_mibps)
            if source.resolve()==target.resolve():
                raise DiskError('Refusing to remove active MP4')
            guard()
            source.unlink()
    staging.cleanup(DATA, media['id'], guard)
    db.execute("UPDATE media SET conversion_state='done',conversion_error=NULL WHERE id=%s",(media['id'],))
    db.commit()


def convert_one(db, media, config, tools, monitor, flv_handler=False,format_handler=None):
    from .conversion_performance import policy
    execution=policy(config)
    io_mibps=execution['io_mibps']
    started=time.monotonic()
    guard=monitor.check
    format_name=format_handler or ('flv' if flv_handler else 'avi')
    if format_name not in ('avi','flv','mts'):raise ValueError('Unknown conversion handler')
    flv_handler=format_name!='avi'  # Retained-original lifecycle, also used by MTS.
    quick=flv_handler and execution['retained_original_validation']=='quick'
    report=getattr(monitor,'conversion_progress',lambda step:None)
    # Terminal even after a user restores the retained original AVI from Trash.
    if media.get('conversion_state') in ('done','warning','duplicate'):
        return
    root=config['destination']['root']
    source=safe_media_path(root,media.get('original_relative_path') or media['relative_path'])
    if source.suffix.lower()!='.'+format_name:
        raise ValueError('Input does not match the selected conversion handler')
    ctx=SimpleNamespace(db=db,media=media,config=config,tools=tools,guard=guard,report=report,
                        format_name=format_name,flv_handler=flv_handler,quick=quick,
                        io_mibps=io_mibps,started=started,root=root,source=source)
    staged=None
    if media.get('conversion_state')=='published':
        report('Checking published MP4')
        target=safe_media_path(root,media['relative_path'])
        verify(target,media['stored_sha256'],media['size'],guard,io_mibps)
    else:
        try:
            staged=publish_conversion(ctx)
        except ValueError:
            # A completed decoder/validation failure is retryable from the original.
            # DiskError, interrupted publication and live decoder leases retain evidence.
            verify(source, media['sha256'], media['size'], guard, io_mibps)
            staging.cleanup(DATA, media['id'], guard)
            raise
        if staged is None:
            return
        media,target=staged.media,staged.target
    finish_conversion(ctx,media,target,staged)


def mark_output_duplicate(db,media,relative,sha,size,root,guard):
    """Atomically link and trash a later output without changing original identity."""
    current=db.execute('SELECT * FROM media WHERE id=%s FOR UPDATE',(media['id'],)).fetchone()
    if current['conversion_state']=='duplicate':return True
    canonical=db.execute('SELECT m.*,t.media_id trashed FROM media m LEFT JOIN media_trash t ON t.media_id=m.id WHERE m.relative_path=%s AND m.id<>%s FOR UPDATE OF m',(relative,media['id'])).fetchone()
    if not canonical:return False
    if canonical['trashed'] or canonical['conversion_state'] not in ('published','done') or canonical['stored_sha256']!=sha or canonical['size']!=size:
        raise DiskError('Converted output path belongs to an unavailable or unverified catalog item; originals retained for review')
    verify(safe_media_path(root,relative),sha,size,guard)
    # Duplicate retains its OWN AVI path, size, sha256, ownership and metadata.
    verify(safe_media_path(root,current['relative_path']),current['sha256'],current['size'],guard)
    if current.get('original_relative_path') or current.get('stored_sha256'):
        raise DiskError('Duplicate candidate already published; manual review required')
    db.execute("UPDATE media SET conversion_state='duplicate',conversion_duplicate_of=%s,duplicate_output_sha256=%s,conversion_error=NULL WHERE id=%s",(canonical['id'],sha,media['id']))
    db.execute('INSERT INTO media_trash(media_id) VALUES (%s) ON CONFLICT DO NOTHING',(media['id'],))
    db.commit()
    return True


def convert_queue(db, config, tools, monitor, status, publish, pause_path=None,job_id=None):
    from ..jobs.job_scope import membership
    if job_id is None:
        raise RuntimeError('Conversion requires an import group')
    scope=' AND '+membership()
    log=logger('avi-conversion')
    def progress(step):
        status['current_step']=step
        publish()
    monitor.conversion_progress=progress
    status['state']='running';status['phase']='avi-conversion'
    while True:
        counts=db.execute("SELECT count(*) FILTER(WHERE conversion_state='done') done,count(*) FILTER(WHERE conversion_state='warning') warning,count(*) FILTER(WHERE conversion_state='duplicate') duplicate,count(*) FILTER(WHERE conversion_state IS NULL OR conversion_state='published') pending FROM media m JOIN video_metadata v ON v.media_id=m.id WHERE (lower(right(m.relative_path,4)) IN ('.avi','.flv','.mts') OR m.original_relative_path IS NOT NULL) AND v.state<>'ancillary' AND (conversion_state='duplicate' OR NOT EXISTS(SELECT 1 FROM media_trash t WHERE t.media_id=m.id))"+scope,(job_id,)).fetchone()
        db.commit();status['conversion_counts']=counts;publish();monitor.check()
        if (pause_path or DATA/'VIDEO-PAUSE').exists():
            status['state']='paused';return
        media=db.execute("SELECT m.* FROM media m JOIN video_metadata v ON v.media_id=m.id WHERE (lower(right(m.relative_path,4)) IN ('.avi','.flv','.mts') OR m.original_relative_path IS NOT NULL) AND (conversion_state IS NULL OR conversion_state='published') AND v.state<>'ancillary' AND NOT EXISTS(SELECT 1 FROM media_trash t WHERE t.media_id=m.id)"+scope+" ORDER BY m.id LIMIT 1",(job_id,)).fetchone();db.commit()
        if media is None:
            status['state']='completed-review-required';return
        status['current']=media['id'];status['current_step']=None;publish()
        try:
            from .converter import convert_one as convert_media
            convert_media(db,media,config,tools,monitor)
            log.info('converted-and-published media=%s',media['id'])
        except ValueError as error:
            db.rollback()
            db.execute("UPDATE media SET conversion_state='warning',conversion_error=%s WHERE id=%s",(str(error),media['id']));db.commit()
            log.warning('AVI retained media=%s reason=%s',media['id'],error)
