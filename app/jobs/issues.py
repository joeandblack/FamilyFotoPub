"""Group review items without conflating unsupported formats with copy errors."""
from pathlib import PureWindowsPath


def group_import_issues(rows):
    batches={}
    for row in rows:
        batch=batches.setdefault(row['job_id'], {
            'job_id':row['job_id'], 'source_root':row['source_root'],
            'started_at':row['started_at'], 'state':row['job_state'], 'rows':[]})
        batch['rows'].append({key:row[key] for key in ('source_path','state','error')})
    result=[]
    for job_id in sorted(batches,reverse=True):
        batch=batches[job_id]
        batch.update(group_issues(batch.pop('rows')))
        batch['unsupported_count']=sum(group['count'] for group in batch['unsupported'])
        result.append(batch)
    return {'imports':result}


def group_issues(rows):
    groups={}
    errors=[]
    for row in rows:
        if row['state']=='error':
            errors.append(row)
        else:
            extension=PureWindowsPath(row['source_path']).suffix.lower() or '(no extension)'
            groups.setdefault(extension,[]).append(row)
    return {'unsupported':[{'extension':ext,'count':len(files),'files':files}
                           for ext,files in sorted(groups.items())], 'errors':errors}
