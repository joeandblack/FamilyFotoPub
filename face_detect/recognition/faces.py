"""Shared face visibility and historical review/opt-out protections."""
VISIBLE = "NOT EXISTS(SELECT 1 FROM media_trash t WHERE t.media_id=m.id) AND NOT EXISTS(SELECT 1 FROM face_preferences p WHERE p.media_id=m.id AND p.opted_out)"

def review_detection(db,detection_id,decision,actor):
    if decision not in ('confirmed','rejected','unreviewed'):
        raise ValueError('Unknown review decision')
    row = db.execute(f'SELECT a.media_id,a.staged,d.accepted FROM face_detections d JOIN face_analyses a ON a.id=d.analysis_id JOIN media m ON m.id=a.media_id WHERE d.id=%s AND {VISIBLE} FOR UPDATE OF m',(detection_id,)).fetchone()
    if not row: raise LookupError('Detection not found')
    if row['staged'] and not row['accepted'] and decision=='confirmed':
        raise RuntimeError('Use the staged addition preview and confirmation')
    from face_detect.recognition import people
    people._lock(db)
    db.execute('UPDATE face_detections SET review_state=%s WHERE id=%s',(decision,detection_id))
    db.execute('DELETE FROM people_previews')
    db.execute('UPDATE people_revision SET revision=revision+1')
    db.execute('INSERT INTO face_review_events(media_id,actor,action) VALUES (%s,%s,%s)',(row['media_id'],str(actor),f'detection:{detection_id}:{decision}'))
    return {'ok':True}

def opt_out(db,media_id,enabled,actor):
    if not db.execute('SELECT id FROM media WHERE id=%s FOR UPDATE',(media_id,)).fetchone():
        raise LookupError('Media not found')
    db.execute('''INSERT INTO face_preferences(media_id,opted_out,actor) VALUES (%s,%s,%s)
        ON CONFLICT(media_id) DO UPDATE SET opted_out=EXCLUDED.opted_out,actor=EXCLUDED.actor,
        revision=face_preferences.revision+1,updated_at=now()''',(media_id,enabled,str(actor)))
    if enabled:
        db.execute('UPDATE face_preferences SET missed_face=false WHERE media_id=%s',(media_id,))
        db.execute('DELETE FROM face_analyses WHERE media_id=%s',(media_id,))
        db.execute("UPDATE face_work SET state='skipped-opt-out',generation=generation+1 WHERE media_id=%s",(media_id,))
    db.execute('INSERT INTO face_review_events(media_id,actor,action) VALUES (%s,%s,%s)',(media_id,str(actor),'opt-out' if enabled else 'opt-in'))
    return {'ok':True,'opted_out':enabled}
