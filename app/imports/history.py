"""Catalog-only summaries; no source or destination media reads."""
from .ignore import IGNORE_SQL_PATTERN
from ..jobs.job_access import UPLOAD_VISIBLE
from ..config import DATA
from datetime import datetime,timezone
import json
import re
from ..library.media import IMAGES,VIDEOS
from .modes import add_skipped


def import_details(db,batches):
    from ..elapsed import active_seconds
    path=DATA/'import-status.json'
    live=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    events={}
    for event in db.execute('SELECT job_id,event,created_at FROM job_events WHERE job_id=ANY(%s) ORDER BY created_at,id',([b['id'] for b in batches],)).fetchall():
        events.setdefault(event['job_id'],[]).append(event)
    for batch in batches:
        c=batch['counts'];b=batch['bytes_by_state']
        status={**batch,'processed':sum(c.get(k,0) for k in ('imported','duplicate','unsupported','skipped_duplicate')),
            'processed_bytes':sum(b.get(k,0) for k in ('imported','duplicate','unsupported','skipped_duplicate')),
            'active_seconds':active_seconds(batch['started_at'],events.get(batch['id'],[]),datetime.now(timezone.utc))}
        if live.get('id')==batch['id']:
            status['current']=live.get('current')
            if status['current']:
                row=db.execute('SELECT size FROM import_items WHERE job_id=%s AND source_path=%s',(batch['id'],status['current'])).fetchone()
                status['current_size']=row['size'] if row else None
        batch['import_status']=status
    return batches


def summaries(db,viewer=None):
    """`viewer` is a username to hide other people's uploads from, or None for administrators."""
    return db.execute('SELECT j.id,j.source_root,j.source_kind,j.source_label,u.owner_name uploader,j.state,j.started_at,j.updated_at,true summary_only FROM jobs j LEFT JOIN upload_sessions u ON u.job_id=j.id'+('' if viewer is None else ' WHERE '+UPLOAD_VISIBLE)+' ORDER BY j.id DESC',() if viewer is None else (viewer,)).fetchall()


def history(db,job_id=None,viewer=None):
    columns=','.join('j.'+name for name in 'id,source_root,source_kind,source_label,state,error,started_at,updated_at,inventory_complete,auto_metadata,auto_conversion,auto_duplicates,auto_trash_duplicates,metadata_status,conversion_status,duplicates_status,skipped_duplicate_files,skipped_duplicate_bytes'.split(','))
    clauses=(['j.id=%s'] if job_id is not None else [])+([UPLOAD_VISIBLE] if viewer is not None else [])
    params=tuple(([job_id] if job_id is not None else [])+([viewer] if viewer is not None else []))
    jobs=db.execute(f'SELECT {columns},u.owner_name uploader FROM jobs j LEFT JOIN upload_sessions u ON u.job_id=j.id'+(' WHERE '+' AND '.join(clauses) if clauses else '')+' ORDER BY j.id DESC',params).fetchall()
    photo_pattern=r'(?i)('+ '|'.join(re.escape(e) for e in sorted(IMAGES))+r')$'
    video_pattern=r'(?i)('+ '|'.join(re.escape(e) for e in sorted(VIDEOS))+r')$'
    rows=db.execute("""SELECT job_id,state,CASE WHEN source_path ~ %s THEN 'photo'
        WHEN source_path ~ %s THEN 'video' ELSE 'other' END media_kind,
        count(*) count,coalesce(sum(size),0) bytes FROM import_items
        WHERE source_path !~ %s"""+(' AND job_id=%s' if job_id is not None else '')+' GROUP BY job_id,state,media_kind',
        (photo_pattern,video_pattern,IGNORE_SQL_PATTERN)+(() if job_id is None else (job_id,))).fetchall()
    grouped={};media_totals={}
    for row in rows:
        counts=grouped.setdefault(row['job_id'],{}).setdefault(row['state'],{'count':0,'bytes':0})
        counts['count']+=row['count'];counts['bytes']+=int(row['bytes'])
        totals=media_totals.setdefault(row['job_id'],{}).setdefault(row.get('media_kind','other'),{'files':0,'bytes':0})
        totals['files']+=row['count'];totals['bytes']+=int(row['bytes'])
    videos=db.execute("""SELECT m.first_import_job_id job_id,CASE WHEN m.conversion_state='duplicate' THEN NULL ELSE coalesce(v.state,'pending') END metadata_state,
        CASE WHEN lower(right(m.relative_path,4)) IN ('.avi','.flv','.mts') OR m.original_relative_path IS NOT NULL
             THEN CASE WHEN m.conversion_state IN ('done','warning','duplicate') THEN m.conversion_state ELSE 'pending' END END conversion_state,
        lower(right(coalesce(m.original_relative_path,m.relative_path),4)) conversion_format,
        count(*) count,
        count(*) FILTER (WHERE lower(right(coalesce(m.original_relative_path,m.relative_path),4)) IN ('.avi','.flv','.mts')
            AND (m.conversion_state IS NULL OR m.conversion_state='published')
            AND v.state<>'ancillary') conversion_pending
        FROM media m LEFT JOIN video_metadata v ON v.media_id=m.id
        WHERE m.kind='video' AND m.first_import_job_id IS NOT NULL AND (m.conversion_state='duplicate' OR NOT EXISTS(SELECT 1 FROM media_trash t WHERE t.media_id=m.id))
        """+(' AND m.first_import_job_id=%s' if job_id is not None else '')+' GROUP BY 1,2,3,4',() if job_id is None else (job_id,)).fetchall()
    video_counts={};pending_formats={}
    for row in videos:
        formats=pending_formats.setdefault(row['job_id'],{'AVI':0,'FLV':0,'MTS':0})
        format_name=(row.get('conversion_format') or '').lstrip('.').upper()
        if row.get('conversion_pending'):
            formats[format_name]=formats.get(format_name,0)+row['conversion_pending']
        stages=video_counts.setdefault(row['job_id'],{'metadata':{},'conversion':{}})
        for stage,key in (('metadata','metadata_state'),('conversion','conversion_state')):
            state=row[key]
            if state and not (stage=='conversion' and row['metadata_state']=='ancillary'):
                stages[stage][state]=stages[stage].get(state,0)+row['count']
    from ..photos.duplicates import all_counts,counts as duplicate_counts
    photo_counts=all_counts(db) if job_id is None else {job_id:duplicate_counts(db,job_id)}
    result=[]
    for job in jobs:
        video_counts.setdefault(job['id'],{'metadata':{},'conversion':{}})['duplicates']=photo_counts.get(job['id'],{})
        counts=grouped.get(job['id'],{})
        skipped_counts={};skipped_bytes={}
        add_skipped(job,skipped_counts,skipped_bytes)
        counts.update({key:{'count':value,'bytes':skipped_bytes[key]} for key,value in skipped_counts.items()})
        from ..video.progress import timing,conversion_timing,current_details
        from ..jobs.job_scope import original_status
        job['metadata_status']=current_details(timing(original_status(job.get('metadata_status')),db),db)
        job['conversion_status']=current_details(conversion_timing(original_status(job.get('conversion_status'))),db)
        totals=media_totals.get(job['id'],{})
        # Missing-only duplicates deliberately have no per-file record/type.
        totals['unclassified_duplicates']={'files':job.get('skipped_duplicate_files',0),'bytes':job.get('skipped_duplicate_bytes',0)}
        formats=pending_formats.get(job['id'],{'AVI':0,'FLV':0,'MTS':0})
        result.append({**job,'mts_pending':formats['MTS'],'conversion_pending':sum(formats.values()),'conversion_pending_formats':formats,'media_totals':totals,'counts':{key:value['count'] for key,value in counts.items()},
            'stages':video_counts.get(job['id'],{'metadata':{},'conversion':{}}),
            'bytes_by_state':{key:value['bytes'] for key,value in counts.items()},
            'total':sum(value['count'] for value in counts.values()),
            'total_bytes':sum(value['bytes'] for value in counts.values())})
    return result
