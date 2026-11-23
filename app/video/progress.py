"""Read-only timing for the current metadata worker run, including legacy status."""
from datetime import datetime, timezone
from threading import Lock
from ..viewer import media_visible

_conversion_lock=Lock()
_conversion_samples={}


def conversion_timing(status, now=None):
    """Estimate from newly observed completions, never historical conversions."""
    if not status:
        return status
    result=dict(status)
    now=now or datetime.now(timezone.utc)
    try:
        start=datetime.fromisoformat(status['started'])
        end=now if status['state']=='running' else datetime.fromisoformat(status['updated'])
        if start.tzinfo is None or end.tzinfo is None:return result
    except (KeyError,TypeError,ValueError):return result
    result['run_seconds']=status.get('elapsed_base',0)+max(0,(end-start).total_seconds())
    counts=status.get('conversion_counts') or {}
    processed=counts.get('done',0)+counts.get('warning',0)+counts.get('duplicate',0)
    group=status.get('job_id')
    with _conversion_lock:
        if status['state']!='running':
            _conversion_samples.pop(group,None)
            return result
        sample=_conversion_samples.get(group)
        if sample is None or sample[0]!=status['started'] or processed<sample[2]:
            sample=_conversion_samples[group]=(status['started'],now,processed)
        result['estimate_seconds']=max(0,(now-sample[1]).total_seconds())
        result['run_processed']=processed-sample[2]
    return result


def current_details(video, db):
    if not video:
        return video
    result = dict(video)
    result['current_file'] = None
    if video.get('state') == 'running' and video.get('current'):
        row = db.execute('SELECT m.id,m.name,m.size,m.original_size,v.metadata FROM media m LEFT JOIN video_metadata v ON v.media_id=m.id WHERE m.id=%s AND '+media_visible('m'), (video['current'],)).fetchone()
        if row:
            result['current_file'] = {'name':row['name'], 'size':row['size'],
                'duration_seconds':video.get('current_duration_seconds') if video.get('current_duration_seconds') is not None else (row.get('metadata') or {}).get('duration_seconds')}
            if video.get('phase')=='avi-conversion' and row.get('original_size') is not None:
                result['current_file']['size']=row['original_size']
    return result


def timing(video, db, now=None):
    if not video or video.get('phase') == 'avi-conversion':
        return video
    result = dict(video)
    try:
        start = datetime.fromisoformat(video['started'])
        end = (now or datetime.now(timezone.utc)) if video['state']=='running' else datetime.fromisoformat(video['updated'])
        if start.tzinfo is None or end.tzinfo is None:
            return result
    except (KeyError,TypeError,ValueError):
        return result
    result['estimate_seconds'] = max(0,(end-start).total_seconds())
    result['run_seconds'] = video.get('elapsed_base',0)+result['estimate_seconds']
    from ..jobs.job_scope import membership
    scope=' AND '+membership('v') if video.get('job_id') else ''
    values=(start,end,video['job_id']) if video.get('job_id') else (start,end)
    result['run_processed'] = db.execute("SELECT count(*) count FROM video_metadata v WHERE v.state IN ('done','warning','ancillary') AND v.updated_at >= %s AND v.updated_at <= %s AND NOT EXISTS(SELECT 1 FROM media_trash t WHERE t.media_id=v.media_id)"+scope,values).fetchone()['count']
    return result
