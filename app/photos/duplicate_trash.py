"""Reversible, exact-pixel duplicate cleanup. Never delete media bytes."""
from .fingerprint import VERSION
from ..library.locks import content_key, MEDIA_LOCK_NAMESPACE
from ..viewer import media_visible


def preferred_copy(members):
    """A keeper must contain all nonconflicting useful metadata in the group."""
    if any(m['evidence'].get('review_only') for m in members):return None
    def fields(m):
        e=m['evidence']
        date=str(e.get('date') or m.get('captured_at') or '').strip().replace('T',' ')
        if len(date)>=10 and date[4:5]==':' and date[7:8]==':':
            date=date[:10].replace(':','-')+date[10:]
        return (date,str(e.get('camera') or '').strip(),str(e.get('gps') or '').strip())
    values=[fields(m) for m in members]
    if any(len({v[i] for v in values if v[i]})>1 for i in range(3)):
        return None
    required=tuple(next((v[i] for v in values if v[i]),'') for i in range(3))
    candidates=[m for m,v in zip(members,values) if v==required]
    return min(candidates,key=lambda m:(m['created_at'],m['id'])) if candidates else None


def apply_group(db,job,key):
    from .duplicates import ELIGIBLE, MEMBER
    db.execute('SELECT pg_advisory_xact_lock(4340040)')
    decision=db.execute('SELECT * FROM photo_duplicate_decisions WHERE version=%s AND pixel_hash=%s',(VERSION,key)).fetchone()
    if decision and decision['decision']!='confirmed':return 0
    if db.execute('SELECT 1 FROM photo_duplicate_trash WHERE version=%s AND pixel_hash=%s AND restored_at IS NOT NULL',(VERSION,key)).fetchone():
        return 0
    hashes=db.execute('SELECT m.sha256 FROM media m JOIN photo_fingerprints f ON f.media_id=m.id WHERE f.version=%s AND f.pixel_hash=%s ORDER BY m.sha256',(VERSION,key)).fetchall()
    for sha in hashes:
        db.execute('SELECT pg_advisory_xact_lock(%s,%s)',(MEDIA_LOCK_NAMESPACE,content_key(sha['sha256'])))
    members=db.execute(f'''SELECT m.*,f.evidence,({MEMBER}) selected FROM media m
        JOIN photo_fingerprints f ON f.media_id=m.id
        WHERE f.version=%s AND f.pixel_hash=%s
        AND f.stored_sha256=coalesce(m.stored_sha256,m.sha256) AND {ELIGIBLE}
        ORDER BY m.id FOR UPDATE OF m''',(job,job,VERSION,key)).fetchall()
    if len(members)<2 or not any(m['selected'] for m in members):return 0
    keeper=preferred_copy(members)
    if decision:
        if decision['member_ids']!=sorted(m['id'] for m in members):return 0
        keeper=next((m for m in members if m['id']==decision['preferred_id']),None)
    if keeper is None:return 0
    reason=('Exact normalized pixels; manually confirmed preferred copy' if decision else
            'Exact normalized pixels; keeper retains all nonconflicting date/camera/GPS evidence')
    count=0
    for m in members:
        # Never silently remove another import's exclusive member.
        if m['id']==keeper['id'] or not m['selected']:continue
        if db.execute('SELECT 1 FROM photo_duplicate_trash WHERE media_id=%s',(m['id'],)).fetchone():continue
        db.execute('INSERT INTO media_trash(media_id) VALUES(%s)',(m['id'],))
        db.execute('''INSERT INTO photo_duplicate_trash(media_id,job_id,preferred_id,version,pixel_hash,reason)
          VALUES(%s,%s,%s,%s,%s,%s)''',(m['id'],job,keeper['id'],VERSION,key,reason))
        count+=1
    return count


def apply_pending(db,job,check,paused):
    from .duplicates import ELIGIBLE, MEMBER
    if not db.execute('SELECT auto_trash_duplicates FROM jobs WHERE id=%s',(job,)).fetchone()['auto_trash_duplicates']:
        db.commit();return True
    keys=db.execute(f'''SELECT f.pixel_hash FROM photo_fingerprints f JOIN media m ON m.id=f.media_id
      WHERE f.version=%s AND f.stored_sha256=coalesce(m.stored_sha256,m.sha256) AND {ELIGIBLE}
      GROUP BY f.pixel_hash HAVING count(*)>1 AND bool_or({MEMBER})''',(VERSION,job,job)).fetchall()
    db.commit()
    for row in keys:
        if paused():return False
        enabled=db.execute('SELECT auto_trash_duplicates FROM jobs WHERE id=%s',(job,)).fetchone()['auto_trash_duplicates']
        if not enabled:db.commit();return True
        check()
        apply_group(db,job,row['pixel_hash']);db.commit()
    return True


def review(db,job,page=1):
    where='d.job_id=%s AND t.deleted_at IS NULL AND d.restored_at IS NULL'
    total=db.execute(f'SELECT count(*) n FROM photo_duplicate_trash d JOIN media_trash t ON t.media_id=d.media_id JOIN media m ON m.id=d.media_id JOIN media k ON k.id=d.preferred_id WHERE {where} AND {media_visible("m")} AND {media_visible("k")}',(job,)).fetchone()['n']
    rows=db.execute(f'''SELECT d.media_id id,m.name,d.preferred_id,k.name preferred_name,d.reason,d.trashed_at
       FROM photo_duplicate_trash d JOIN media_trash t ON t.media_id=d.media_id
       JOIN media m ON m.id=d.media_id JOIN media k ON k.id=d.preferred_id
       WHERE {where} AND {media_visible('m')} AND {media_visible('k')} ORDER BY d.trashed_at DESC,d.media_id LIMIT 40 OFFSET %s''',(job,(page-1)*40)).fetchall()
    return dict(items=rows,total=total,page=page,pages=max(1,(total+39)//40))
