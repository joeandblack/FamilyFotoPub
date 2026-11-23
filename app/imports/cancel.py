"""Cancel an unprocessed scan only, without touching source or destination media."""
def cancel_scan(db, job_id, expected_source):
    if not db.execute('SELECT pg_try_advisory_lock(4340002) available').fetchone()['available']:
        raise RuntimeError('A media worker is active; cancellation refused')
    job=db.execute('SELECT * FROM jobs WHERE id=%s FOR UPDATE',(job_id,)).fetchone()
    if not job or job['source_root']!=expected_source or job['state'] not in ('paused','paused-error'):
        raise RuntimeError('Expected paused source job not found')
    counts=db.execute('SELECT state,count(*) count FROM import_items WHERE job_id=%s GROUP BY state',(job_id,)).fetchall()
    if db.execute("SELECT 1 FROM import_items WHERE job_id=%s AND (media_id IS NOT NULL OR state NOT IN ('pending','error')) LIMIT 1",(job_id,)).fetchone():
        raise RuntimeError('Scan has processed media; manual review required')
    db.execute('DELETE FROM import_items WHERE job_id=%s',(job_id,))
    db.execute("UPDATE jobs SET state='cancelled',error=NULL,total_bytes=0,updated_at=now() WHERE id=%s",(job_id,))
    db.execute("INSERT INTO job_events(job_id,event,details) VALUES(%s,'cancelled',%s)",(job_id,'User cancelled mistaken source scan; unprocessed queue cleared.'))
    return {'job_id':job_id,'source_root':expected_source,'previous_error':job['error'],'cleared_counts':counts}
