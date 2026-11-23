"""Duplicate-only audit records never establish library membership."""
MODES = ('normal', 'missing-only')


def validate_mode(value):
    if value not in MODES:
        raise ValueError('Import mode must be normal or missing-only')
    return 'missing-only'  # Accept old clients, but there is only one behavior.


def skip_duplicate(db, job_id, item_id):
    # Keep inventory totals/recovery evidence, but no second link to the media.
    return db.execute("""UPDATE import_items i
        SET state='duplicate',media_id=NULL,error=NULL FROM jobs j
        WHERE i.id=%s AND i.job_id=%s AND j.id=i.job_id
          AND i.state='pending' AND j.inventory_complete
        RETURNING i.id""",(item_id,job_id)).fetchone() is not None


def add_skipped(job, counts, bytes_by_state):
    count = int(job.get('skipped_duplicate_files', 0))
    if count:
        counts['skipped_duplicate'] = count
        bytes_by_state['skipped_duplicate'] = int(job['skipped_duplicate_bytes'])
