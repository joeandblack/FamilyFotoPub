"""Destination-only photo duplicate scans with optional reversible cleanup."""
import hashlib
import math
import time
from psycopg.types.json import Jsonb
from ..config import DATA, load_config
from ..database import connection
from ..storage.disks import Monitor
from ..library.media import safe_media_path
from .fingerprint import VERSION, MAX_BYTES, fingerprint, readable_warning
from ..telemetry import utcnow, write_json
from ..viewer import media_visible

ELIGIBLE = """m.kind='image' AND lower(m.relative_path) ~ '\\.(jpg|jpeg)$'
 AND left(m.name,1)<>'.'
 AND NOT EXISTS(SELECT 1 FROM media_trash t WHERE t.media_id=m.id)
 AND NOT EXISTS(SELECT 1 FROM media_adjustments a WHERE a.media_id=m.id
                AND (a.rotation<>0 OR a.crop IS NOT NULL))"""
MEMBER = """(m.first_import_job_id=%s OR EXISTS(SELECT 1 FROM import_items i
 WHERE i.job_id=%s AND i.media_id=m.id AND i.state IN ('imported','duplicate')))"""


def saved_active_seconds(status):
    """Resume from a durable active-time checkpoint, never from wall-clock age."""
    values=[status.get(key) for key in ('active_seconds','run_seconds')]
    return max([0]+[value for value in values if isinstance(value,(int,float))
                   and not isinstance(value,bool) and math.isfinite(value) and value>=0])


def counts(db, job):
    rows = db.execute(f"""SELECT CASE WHEN q.state='skipped' THEN 'skipped' WHEN q.state='warning' THEN 'warning'
       WHEN f.media_id IS NOT NULL AND f.version=%s AND f.stored_sha256=coalesce(m.stored_sha256,m.sha256)
       THEN 'done' ELSE 'pending' END state,count(*) n
       FROM media m LEFT JOIN photo_duplicate_items q ON q.media_id=m.id AND q.job_id=%s
       LEFT JOIN photo_fingerprints f ON f.media_id=m.id WHERE {ELIGIBLE} AND {MEMBER}
       GROUP BY 1""", (VERSION, job, job, job)).fetchall()
    return {r['state']: r['n'] for r in rows}


def all_counts(db):
    rows=db.execute(f"""WITH eligible AS MATERIALIZED (
      SELECT m.id,m.first_import_job_id,coalesce(m.stored_sha256,m.sha256) sha FROM media m WHERE {ELIGIBLE}
    ), memberships AS (
      SELECT first_import_job_id job_id,id media_id FROM eligible WHERE first_import_job_id IS NOT NULL
      UNION SELECT i.job_id,i.media_id FROM import_items i JOIN eligible e ON e.id=i.media_id
      WHERE i.state IN ('imported','duplicate')
    ) SELECT s.job_id,CASE WHEN q.state='skipped' THEN 'skipped' WHEN q.state='warning' THEN 'warning'
      WHEN f.version=%s AND f.stored_sha256=e.sha THEN 'done' ELSE 'pending' END state,count(*) n
      FROM memberships s JOIN eligible e ON e.id=s.media_id
      LEFT JOIN photo_fingerprints f ON f.media_id=s.media_id
      LEFT JOIN photo_duplicate_items q ON q.media_id=s.media_id AND q.job_id=s.job_id GROUP BY 1,2""",(VERSION,)).fetchall()
    result={}
    for r in rows:result.setdefault(r['job_id'],{})[r['state']]=r['n']
    return result


def ready(db, job):
    from ..jobs.job_scope import require_import_complete
    require_import_complete(db, job)
    status = db.execute('SELECT duplicates_status FROM jobs WHERE id=%s', (job,)).fetchone()['duplicates_status']
    if status and status.get('state') == 'paused-error':
        raise RuntimeError('Photo scan disk/integrity fault requires review before resuming')


def prepare_unexamined(db, job, large_review=False):
    """Called only while the dispatcher holds all operation reservations."""
    ready(db, job)
    if counts(db,job).get('pending'):
        raise RuntimeError('Finish or resume the existing photo duplicate scan before scanning warning photos')
    previous=db.execute('SELECT duplicates_status FROM jobs WHERE id=%s',(job,)).fetchone()['duplicates_status'] or {}
    rows=db.execute(f"""SELECT q.media_id,q.error FROM photo_duplicate_items q JOIN media m ON m.id=q.media_id
        WHERE q.job_id=%s AND q.state='warning' AND {ELIGIBLE} AND {MEMBER}""",(job,job,job)).fetchall()
    if large_review:
        rows=[r for r in rows if r['error']=='Image exceeds manual review limit of 60 megapixels' or (r['error'] or '').startswith('Large image scan failed: ')]
    if not rows:raise RuntimeError('No unexamined photo warnings eligible for this action remain in this import')
    if large_review:
        previous['large_review_ids']=sorted(set(previous.get('large_review_ids',[])) | {r['media_id'] for r in rows})
    ids=sorted(set(previous.get('unexamined_ids',[])) | {r['media_id'] for r in rows})
    previous.update(unexamined_ids=ids,review_policy='full-resolution-v1-60mp',state='paused',error=None)
    db.execute('UPDATE jobs SET duplicates_status=%s WHERE id=%s',(Jsonb(previous),job))
    db.execute("UPDATE photo_duplicate_items SET state='pending',error=NULL WHERE job_id=%s AND media_id=ANY(%s)",(job,[r['media_id'] for r in rows]))


class Scan:
    """One run of the photo duplicate scan over an import; owns the progress state."""

    def __init__(self, db, config, monitor, job_id, previous):
        self.db = db
        self.config = config
        self.monitor = monitor
        self.job_id = job_id
        self.unexamined = set(previous.get('unexamined_ids',[]))
        self.large_review = set(previous.get('large_review_ids',[]))
        self.base = saved_active_seconds(previous)
        self.begun = time.monotonic()
        self.started = utcnow()
        self.processed = 0
        self.current = None
        self.progress = counts(db, job_id)

    def paused(self):
        from ..jobs import work_admission
        return (DATA/'PHOTO-DUPLICATE-PAUSE').exists() or (DATA/'PIPELINE-PAUSE').exists() or work_admission.pending()

    def cleanup(self):
        from .duplicate_trash import apply_pending
        completed=apply_pending(self.db,self.job_id,self.monitor.check,self.paused)
        self.progress=counts(self.db,self.job_id)
        return completed

    def publish(self, state, error=None):
        db, job_id, monitor = self.db, self.job_id, self.monitor
        c = self.progress.copy()
        seconds = time.monotonic()-self.begun
        status = dict(state=state, job_id=job_id, started=self.started, updated=utcnow(), counts=c,
                      active_seconds=self.base+seconds, run_seconds=self.base+seconds,
                      estimate_seconds=seconds, run_processed=self.processed,
                      current_file=self.current, version=VERSION, error=error,
                      library_id=getattr(monitor,'marker_id',None),destination=self.config['destination'])
        if self.unexamined:status.update(unexamined_ids=sorted(self.unexamined),review_policy='full-resolution-v1-60mp')
        if self.large_review:status['large_review_ids']=sorted(self.large_review)
        status['trashed_duplicates']=db.execute('SELECT count(*) n FROM photo_duplicate_trash WHERE job_id=%s AND restored_at IS NULL',(job_id,)).fetchone()['n']
        db.execute('UPDATE jobs SET duplicates_status=%s WHERE id=%s', (Jsonb(status), job_id))
        db.commit()
        write_json(DATA/'photo-duplicate-status.json', status)

    def seed_items(self, previous):
        db, job_id = self.db, self.job_id
        if previous.get('destination') and previous['destination']!=self.config['destination']:
            raise RuntimeError('Photo scan destination identity changed')
        if previous.get('library_id') and previous['library_id']!=self.monitor.marker_id:
            raise RuntimeError('Photo scan library identity changed')
        db.execute(f"""INSERT INTO photo_duplicate_items(job_id,media_id)
           SELECT %s,m.id FROM media m WHERE {ELIGIBLE} AND {MEMBER}
           ON CONFLICT DO NOTHING""", (job_id, job_id, job_id))
        db.execute('''UPDATE photo_duplicate_items q SET state=CASE WHEN EXISTS(
            SELECT 1 FROM photo_fingerprints f JOIN media m ON m.id=f.media_id
            WHERE f.media_id=q.media_id AND f.version=%s
            AND f.stored_sha256=coalesce(m.stored_sha256,m.sha256)) THEN 'done' ELSE 'pending' END
            WHERE q.job_id=%s AND q.state NOT IN ('warning','skipped') ''',(VERSION,job_id))
        db.commit()

    def next_media(self):
        m = self.db.execute(f"""SELECT m.* FROM media m
          JOIN photo_duplicate_items q ON q.media_id=m.id AND q.job_id=%s
          WHERE {ELIGIBLE} AND q.state='pending'
          ORDER BY m.id LIMIT 1""", (self.job_id,)).fetchone()
        self.db.commit()
        return m

    def count(self, outcome):
        self.processed += 1
        self.progress['pending']-=1;self.progress[outcome]=self.progress.get(outcome,0)+1

    def skip_oversize(self, m):
        self.db.execute("UPDATE photo_duplicate_items SET state='warning',error='JPEG exceeds 80 MB scan limit' WHERE job_id=%s AND media_id=%s", (self.job_id,m['id']))
        self.db.commit();self.count('warning')

    def read_verified(self, path, m):
        """The file's bytes, checked against the catalog checksum; returns (data, expected)."""
        monitor = self.monitor
        data = bytearray()
        with path.open('rb') as stream:
            while True:
                monitor.check()
                chunk = stream.read(1024*1024)
                if not chunk:break
                data.extend(chunk)
                if len(data)>MAX_BYTES:raise RuntimeError('File changed during read')
        expected = m['stored_sha256'] or m['sha256']
        if hashlib.sha256(data).hexdigest()!=expected:
            raise RuntimeError(f"Checksum mismatch for media #{m['id']}")
        return data, expected

    def scan_media(self, m, data, expected):
        """Fingerprint one photo and record it; returns 'done' or 'warning'."""
        db, job_id, monitor = self.db, self.job_id, self.monitor
        if m['id'] in self.unexamined:
            from .thumbnail_recovery import rebuild_missing
            try:
                rebuild_missing(db,m,self.config,DATA,monitor.check)
            except (ValueError,OSError) as error:
                db.execute('UPDATE media SET metadata_error=%s WHERE id=%s',
                    ('Thumbnail unavailable; original retained: '+str(error)[:300],m['id']))
        try:
            if m['id'] in self.large_review:result=fingerprint(data,review=True,large_review=True)
            else:result = fingerprint(data,review=True) if m['id'] in self.unexamined else fingerprint(data)
        except (ValueError, OSError) as error:
            reason=('Large image scan failed: ' if m['id'] in self.large_review else '')+str(error)
            db.execute("UPDATE photo_duplicate_items SET state='warning',error=%s WHERE job_id=%s AND media_id=%s", (reason[:300],job_id,m['id']))
            return 'warning'
        monitor.check()
        db.execute('''INSERT INTO photo_fingerprints(media_id,version,stored_sha256,pixel_hash,image_hash,evidence)
           VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(media_id) DO UPDATE SET
           version=excluded.version,stored_sha256=excluded.stored_sha256,pixel_hash=excluded.pixel_hash,
           image_hash=excluded.image_hash,evidence=excluded.evidence,created_at=now()''',
           (m['id'],VERSION,expected,result['pixel_hash'],result['image_hash'],Jsonb(result['evidence'])))
        db.execute("UPDATE photo_duplicate_items SET state='done',error=NULL WHERE job_id=%s AND media_id=%s", (job_id,m['id']))
        return 'done'

    def scan_all(self):
        """Scan pending photos until finished, paused or failed."""
        db, monitor = self.db, self.monitor
        while True:
            if self.paused():
                self.publish('paused');return
            monitor.check()
            m = self.next_media()
            if not m:
                if not self.cleanup():self.publish('paused');return
                self.current = None;self.publish('completed-review-required');return
            self.current = {'name': m['name'], 'size': m['size']}
            self.publish('running')
            path = safe_media_path(self.config['destination']['root'], m['relative_path'])
            monitor.check()
            if path.stat().st_size != m['size']:
                raise RuntimeError(f"Size mismatch for media #{m['id']}")
            if m['size'] > MAX_BYTES:
                self.skip_oversize(m)
                self.publish('running');continue
            data, expected = self.read_verified(path, m)
            outcome = self.scan_media(m, data, expected)
            db.commit();self.count(outcome)
            if self.processed%100==0 and not self.cleanup():self.publish('paused');return
            self.publish('running')


def run(job_id):
    config = load_config()
    monitor = Monitor(config, roles=('destination',))
    # Maintenance must not create a new library marker on an unexpected disk.
    if not monitor.marker.is_file():
        raise RuntimeError('Destination library marker is missing')
    with connection() as db:
        from ..jobs import WORKER_LOCK
        if not db.execute('SELECT pg_try_advisory_lock(%s) ok', (WORKER_LOCK,)).fetchone()['ok']:
            raise RuntimeError('Another media worker is active')
        ready(db, job_id)
        previous = db.execute('SELECT duplicates_status FROM jobs WHERE id=%s', (job_id,)).fetchone()['duplicates_status'] or {}
        scan = Scan(db, config, monitor, job_id, previous)
        prepared = False
        try:
            monitor.prepare();prepared = True
            scan.seed_items(previous)
            if not scan.cleanup():scan.publish('paused');return
            scan.scan_all()
        except Exception as error:
            db.rollback();scan.publish('paused-error', str(error)[:400]);raise
        finally:
            if prepared:monitor.close()


def groups(db, job, page=1):
    # All members must still represent the scanned, unedited, visible original.
    rows = db.execute(f"""SELECT m.id,m.name,m.size,m.captured_at,m.created_at,
      f.pixel_hash,f.image_hash,f.evidence,({MEMBER}) selected,{media_visible('m')} seen
      FROM photo_fingerprints f JOIN media m ON m.id=f.media_id
      WHERE f.version=%s AND f.stored_sha256=coalesce(m.stored_sha256,m.sha256)
      AND {ELIGIBLE} ORDER BY m.created_at,m.id""", (job,job,VERSION)).fetchall()
    grouped = {}
    for row in rows:grouped.setdefault(row['pixel_hash'],[]).append(row)
    # A group with a copy this viewer may not see is hidden entirely: showing it would
    # reveal someone else's private file, and a partial group cannot be decided safely.
    matches = [(key,items) for key,items in grouped.items() if len(items)>1 and any(m['selected'] for m in items) and all(m['seen'] for m in items)]
    decisions = {d['pixel_hash']:d for d in db.execute('SELECT * FROM photo_duplicate_decisions WHERE version=%s',(VERSION,)).fetchall()}
    result = []
    for key, items in matches[(page-1)*20:page*20]:
        d = decisions.get(key)
        ids = sorted(m['id'] for m in items)
        valid = d and d['member_ids']==ids
        result.append(dict(key=key,members=items,decision=d['decision'] if valid else 'candidate',
                           preferred_id=d['preferred_id'] if valid else items[0]['id'],revision=d['revision'] if d else 0,
                           method='Manual review required: damaged JPEG decoded tolerantly; matching pixels may include reconstructed or missing data' if any(m['evidence'].get('decode_mode')=='tolerant-jpeg' for m in items) else 'Manual review required: MPO primary image matches; additional pictures were not compared' if any(m['evidence'].get('comparison_scope')=='primary-image-only' for m in items) else 'Manual review required: extended scan; invalid orientation treated as unrotated where present' if any(m['evidence'].get('review_only') for m in items) else 'JPEG image data and normalized pixels' if len({m['image_hash'] for m in items})==1 else 'Normalized pixels (same color profile)'))
    warnings=db.execute(f"SELECT q.media_id,m.name,q.error FROM photo_duplicate_items q JOIN media m ON m.id=q.media_id WHERE q.job_id=%s AND q.state='warning' AND {ELIGIBLE} AND {media_visible('m')} ORDER BY q.media_id LIMIT 100",(job,)).fetchall()
    warnings=[dict(row,error=readable_warning(row['error'])) for row in warnings]
    return dict(groups=result,total=len(matches),page=page,pages=max(1,(len(matches)+19)//20),warnings=warnings)


def decide(db,job,key,decision,preferred,revision,member_ids):
    if decision not in ('candidate','confirmed','separate'):
        raise ValueError('Invalid review decision')
    # Serialize decisions only; no media ownership or file mutations.
    db.execute('SELECT pg_advisory_xact_lock(4340040)')
    group = None
    page = 1
    while True:
        result=groups(db,job,page)
        group=next((g for g in result['groups'] if g['key']==key),None)
        if group or page>=result['pages']:break
        page+=1
    if not group or preferred not in [m['id'] for m in group['members']]:
        raise ValueError('Candidate changed or preferred copy unavailable')
    if sorted(member_ids)!=sorted(m['id'] for m in group['members']):
        raise ValueError('Group membership changed; refresh before saving')
    if group['revision']!=revision:raise ValueError('Review changed; refresh before saving')
    db.execute('''INSERT INTO photo_duplicate_decisions(version,pixel_hash,decision,preferred_id,member_ids,revision)
       VALUES(%s,%s,%s,%s,%s,1) ON CONFLICT(version,pixel_hash) DO UPDATE SET
       decision=excluded.decision,preferred_id=excluded.preferred_id,member_ids=excluded.member_ids,
       revision=photo_duplicate_decisions.revision+1,updated_at=now()''',
       (VERSION,key,decision,preferred,Jsonb(sorted(m['id'] for m in group['members']))))
