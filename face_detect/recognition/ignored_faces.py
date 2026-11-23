"""Shared face-level visibility and reversible ignore decisions."""
from face_detect.recognition import faces
from face_detect.recognition import people

# Staged candidates never displace the current ordinary analysis. Only an
# explicitly accepted replacement participates alongside that analysis.
CURRENT="""d.superseded_by IS NULL AND (d.accepted OR (NOT a.staged AND a.id=(
 SELECT max(a2.id) FROM face_analyses a2 WHERE a2.media_id=m.id
 AND a2.content_hash=m.sha256 AND NOT a2.staged)))"""
BASE=f"""FROM face_detections d JOIN face_analyses a ON a.id=d.analysis_id
 JOIN media m ON m.id=a.media_id WHERE a.content_hash=m.sha256 AND {faces.VISIBLE} AND {CURRENT}"""

def ids_checked(ids):
    if not ids or len(ids)>250 or any(type(i) is not int or not 0<i<=9007199254740991 for i in ids):
        raise ValueError('Select 1 to 250 faces')
    return sorted(set(ids))

def collection(db,state='ignored',search='',page=1):
    if state not in ('ignored','available'):raise ValueError('Unknown collection filter')
    search=search.strip()[:200]
    pattern='%'+search.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
    where=BASE+' AND d.ignored=%s AND (m.name ILIKE %s OR m.id::text=%s)'
    args=(state=='ignored',pattern,search)
    total=db.execute('SELECT count(*) n '+where,args).fetchone()['n']
    photos=db.execute('SELECT count(DISTINCT m.id) n '+where,args).fetchone()['n']
    rows=db.execute('SELECT d.id detection_id,a.media_id,m.name,d.box,d.review_state,d.ignored '+where+' ORDER BY d.id DESC LIMIT 50 OFFSET %s',args+((page-1)*50,)).fetchall()
    return {'items':rows,'total':total,'photo_count':photos,'page':page,'page_size':50,'revision':people.revision(db)}

def locked_rows(db,ids,revision):
    ids=ids_checked(ids)
    people._lock_media(db,ids);people._lock(db)
    if people.revision(db)!=revision:raise RuntimeError('Faces changed; refresh and select again')
    rows=db.execute('SELECT d.*,a.media_id,a.width,a.height,a.staged,a.run_id,m.sha256 content_hash '+BASE+' AND d.id=ANY(%s) ORDER BY d.id',(ids,)).fetchall()
    if len(rows)!=len(ids):raise ValueError('Selection includes hidden, deleted or superseded faces; refresh')
    return rows

def changed(db):
    db.execute('DELETE FROM people_previews')
    db.execute('UPDATE people_revision SET revision=revision+1')
    return people.revision(db)

def set_ignored(db,ids,enabled,revision,actor):
    rows=locked_rows(db,ids,revision)
    for row in rows:
        db.execute('UPDATE face_detections SET ignored=%s,ignore_history=ignore_history OR %s WHERE id=%s',(enabled,enabled,row['id']))
        db.execute('INSERT INTO face_ignore_events(detection_id,actor,action) VALUES(%s,%s,%s)',(row['id'],actor,'ignore' if enabled else 'unignore'))
    return {'ok':True,'face_count':len(rows),'photo_count':len({r['media_id'] for r in rows}),'revision':changed(db)}
