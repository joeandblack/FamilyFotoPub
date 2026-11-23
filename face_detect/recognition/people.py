"""Shared People identity, review and historical correction helpers."""
import uuid
import unicodedata
from itertools import combinations
from psycopg.types.json import Jsonb
from face_detect.recognition.faces import VISIBLE
MAX_ITEMS=250
CORRECTION_LOCK=4340032
ELIGIBLE=f"""SELECT d.id detection_id,a.media_id,m.name,m.relative_path,m.sha256 content_hash,m.size,a.width,a.height,d.box,d.landmarks,d.landmark_schema,d.review_state FROM face_detections d JOIN face_analyses a ON a.id=d.analysis_id JOIN media m ON m.id=a.media_id WHERE d.review_state='confirmed' AND a.content_hash=m.sha256 AND m.kind='image' AND {VISIBLE} AND NOT d.ignored AND d.superseded_by IS NULL AND (d.accepted OR (NOT a.staged AND a.id=(SELECT max(a2.id) FROM face_analyses a2 WHERE a2.media_id=m.id AND a2.content_hash=m.sha256 AND NOT a2.staged)))"""

def eligible(db,ids=None):
    return db.execute(ELIGIBLE+(' AND d.id=ANY(%s)' if ids is not None else '')+' ORDER BY d.id', (ids,) if ids is not None else ()).fetchall()

def revision(db):
    return db.execute('SELECT revision FROM people_revision').fetchone()['revision']

def public_faces(rows):
    return [{k:r[k] for k in ('detection_id','media_id','name','box','review_state')} for r in rows]

def list_people(db,include_empty=False):
    rows=eligible(db);valid={r['detection_id']:r for r in rows};items=[]
    for p in db.execute('SELECT * FROM people WHERE deleted_at IS NULL ORDER BY lower(name),id').fetchall():
        faces=[valid[r['detection_id']] for r in db.execute('SELECT detection_id FROM people_assignments WHERE person_id=%s',(p['id'],)).fetchall() if r['detection_id'] in valid]
        if faces or include_empty:items.append({**p,'face_count':len(faces),'photo_count':len({r['media_id'] for r in faces})})
    return {'items':items,'revision':revision(db)}

def person(db,person_id,include_empty=False):
    p=db.execute('SELECT * FROM people WHERE id=%s AND deleted_at IS NULL',(person_id,)).fetchone()
    if not p:raise LookupError('Person not found')
    ids=[r['detection_id'] for r in db.execute('SELECT detection_id FROM people_assignments WHERE person_id=%s',(person_id,)).fetchall()]
    rows=eligible(db,ids)
    if not rows and not include_empty:raise LookupError('No currently visible confirmed faces')
    return {**p,'faces':public_faces(rows),'revision':revision(db)}

def _lock(db):
    db.execute('SELECT pg_advisory_xact_lock(%s)',(CORRECTION_LOCK,))

def _state_ids(db):
    return {r['id'] for r in db.execute('SELECT detection_id id FROM people_assignments UNION SELECT a FROM people_constraints UNION SELECT b FROM people_constraints').fetchall()}

def _snapshot(db):
    return {'people':db.execute('SELECT id,name FROM people ORDER BY id').fetchall(),'assignments':db.execute('SELECT * FROM people_assignments ORDER BY detection_id').fetchall(),'constraints':db.execute('SELECT * FROM people_constraints ORDER BY a,b').fetchall()}

def _lock_media(db,ids):
    media=[r['media_id'] for r in db.execute('SELECT DISTINCT a.media_id FROM face_analyses a JOIN face_detections d ON d.analysis_id=a.id WHERE d.id=ANY(%s) ORDER BY a.media_id',(ids,)).fetchall()]
    if media:db.execute('SELECT id FROM media WHERE id=ANY(%s) ORDER BY id FOR UPDATE',(media,)).fetchall()

def name_key(name):
    """Unicode-compatible, whitespace-normalized, case-insensitive identity label."""
    return ' '.join(unicodedata.normalize('NFKC',name).split()).casefold()

def check_name(db,name,person_id=None,allow_same=True):
    # All preview/apply/undo writers serialize through CORRECTION_LOCK. Recheck
    # on apply, so simultaneous previews cannot create duplicate names.
    key=name_key(name)
    rows=db.execute('SELECT id,name,deleted_at FROM people ORDER BY id').fetchall()
    current=next((p for p in rows if p['id']==person_id),None)
    # Existing legacy collisions remain intact; a spelling-only change to the
    # same normalized name does not create another collision.
    if allow_same and current and name_key(current['name'])==key:return
    matches=[p for p in rows if p['id']!=person_id and name_key(p['name'])==key]
    if matches:
        labels=', '.join(f"#{p['id']}" for p in matches)
        if any(p['deleted_at'] for p in matches):
            raise RuntimeError(f'This name belongs to an existing or deleted person ({labels}). Open Deleted people to restore it, or choose a different name. No changes were saved.')
        raise RuntimeError(f'A person named "{matches[0]["name"]}" already exists ({labels}). Choose Existing person and Preview assignment to existing person instead of creating another name. No changes were saved.')

def _prepare(db,payload):
    p=dict(payload);action=p['action'];ids=sorted(set(p.get('detection_ids') or []));pid=p.get('person_id');target=p.get('target_person_id')
    if action not in ('assign','unassign','merge','split','rename','reject'):raise ValueError('Unknown correction')
    if action in ('merge','split','rename') or (action=='assign' and pid):
        if not db.execute('SELECT 1 FROM people WHERE id=%s AND deleted_at IS NULL',(pid,)).fetchone():raise LookupError('Source person not found')
    if action=='merge':
        if pid==target:raise ValueError('Choose two different people')
        if not db.execute('SELECT 1 FROM people WHERE id=%s AND deleted_at IS NULL',(target,)).fetchone():raise LookupError('Destination person not found')
        ids=[r['detection_id'] for r in db.execute('SELECT detection_id FROM people_assignments WHERE person_id=ANY(%s) ORDER BY detection_id',([pid,target],)).fetchall()]
    if action=='rename':ids=[r['detection_id'] for r in db.execute('SELECT detection_id FROM people_assignments WHERE person_id=%s',(pid,)).fetchall()]
    name=p.get('name','').strip()
    if action in ('rename','split') or (action=='assign' and not pid):
        if not 1<=len(name)<=100 or any(ord(c)<32 for c in name):raise ValueError('Enter a name of 1 to 100 characters')
        check_name(db,name,pid if action=='rename' else None)
        p['name']=name
    if not ids or len(ids)>MAX_ITEMS:raise ValueError('Select 1 to 250 faces for this correction')
    _lock_media(db,ids)
    if db.execute('SELECT 1 FROM people_assignments a JOIN people p ON p.id=a.person_id WHERE a.detection_id=ANY(%s) AND p.deleted_at IS NOT NULL',(ids,)).fetchone():raise RuntimeError('Restore the deleted person before changing these assignments')
    rows=eligible(db,ids)
    if len(rows)!=len(ids):raise RuntimeError('Faces changed, hidden or superseded; refresh before correcting')
    assignments={r['detection_id']:r['person_id'] for r in db.execute('SELECT * FROM people_assignments WHERE detection_id=ANY(%s)',(ids,)).fetchall()}
    if action=='assign' and any(v!=pid for v in assignments.values()):raise RuntimeError('Already assigned faces require explicit unassign or merge first')
    if action=='split':
        if any(assignments.get(i)!=pid for i in ids):raise ValueError('Split faces must belong to source person')
        allids=[r['detection_id'] for r in db.execute('SELECT detection_id FROM people_assignments WHERE person_id=%s',(pid,)).fetchall()]
        if set(allids)<=set(ids):raise ValueError('Split must leave at least one face in the source person')
        p['remaining_ids']=sorted(set(allids)-set(ids))
    if action in ('assign','merge'):
        dest=target if action=='merge' else pid
        combined=set(ids)
        if dest:combined.update(r['detection_id'] for r in db.execute('SELECT detection_id FROM people_assignments WHERE person_id=%s',(dest,)).fetchall())
        if db.execute('SELECT 1 FROM people_constraints WHERE a=ANY(%s) AND b=ANY(%s) LIMIT 1',(list(combined),list(combined))).fetchone():raise RuntimeError('Previously separated faces cannot be merged; undo that correction first')
    if action=='reject' and len(ids)<2:raise ValueError('Select at least two suggested faces to reject their grouping')
    p['detection_ids']=ids
    return p,rows

def preview(db,payload,actor):
    if payload['action'] in ('delete','restore'):return lifecycle_preview(db,payload,actor)
    _lock(db);p,rows=_prepare(db,payload);rev=revision(db);token=uuid.uuid4().hex
    db.execute("DELETE FROM people_previews WHERE created_at<now()-interval '30 minutes'")
    db.execute('INSERT INTO people_previews(token,revision,payload,actor) VALUES(%s,%s,%s,%s)',(token,rev,Jsonb(p),actor))
    source=db.execute('SELECT id,name FROM people WHERE id=%s',(p.get('person_id'),)).fetchone()
    target=db.execute('SELECT id,name FROM people WHERE id=%s',(p.get('target_person_id'),)).fetchone()
    return {'source_person':source,'target_person':target,'remaining_faces':public_faces(eligible(db,p.get('remaining_ids',[]))),'token':token,'revision':rev,'action':p['action'],'face_count':len(rows),'photo_count':len({r['media_id'] for r in rows}),'faces':public_faces(rows),'name':p.get('name')}

def apply(db,token,actor):
    _lock(db)
    saved=db.execute("SELECT * FROM people_previews WHERE token=%s AND actor=%s AND created_at>now()-interval '30 minutes'",(token,actor)).fetchone()
    if not saved:raise RuntimeError('Preview expired or already applied; preview again')
    if saved['payload']['action'] in ('delete','restore'):return lifecycle_apply(db,saved,actor)
    if saved['revision']!=revision(db):
        payload=saved['payload'];action=payload['action']
        if action in ('rename','split') or (action=='assign' and not payload.get('person_id')):
            check_name(db,payload['name'],payload.get('person_id') if action=='rename' else None)
        raise RuntimeError('People changed since preview; preview again')
    _lock_media(db,sorted(_state_ids(db)|set(saved['payload'].get('detection_ids',[]))))
    p,rows=_prepare(db,saved['payload'])
    if not db.execute('SELECT 1 FROM people_previews WHERE token=%s',(token,)).fetchone() or saved['revision']!=revision(db):raise RuntimeError('Faces changed since preview; preview again')
    if p!=saved['payload']:raise RuntimeError('Selection changed; preview again')
    before=_snapshot(db);action=p['action'];ids=p['detection_ids'];pid=p.get('person_id')
    if action=='rename':db.execute('UPDATE people SET name=%s WHERE id=%s',(p['name'],pid))
    elif action in ('assign','split'):
        if action=='split' or not pid:pid=db.execute('INSERT INTO people(name) VALUES(%s) RETURNING id',(p['name'],)).fetchone()['id']
        for i in ids:db.execute('INSERT INTO people_assignments VALUES(%s,%s) ON CONFLICT(detection_id) DO UPDATE SET person_id=EXCLUDED.person_id',(i,pid))
        if action=='split':
            for a in ids:
                for b in p['remaining_ids']:db.execute('INSERT INTO people_constraints VALUES(%s,%s) ON CONFLICT DO NOTHING',tuple(sorted((a,b))))
    elif action=='unassign':db.execute('DELETE FROM people_assignments WHERE detection_id=ANY(%s)',(ids,))
    elif action=='merge':
        db.execute('UPDATE people_assignments SET person_id=%s WHERE person_id=%s',(p['target_person_id'],pid));pid=p['target_person_id']
    elif action=='reject':
        for pair in combinations(ids,2):db.execute('INSERT INTO people_constraints VALUES(%s,%s) ON CONFLICT DO NOTHING',pair)
    rev=db.execute('UPDATE people_revision SET revision=revision+1 RETURNING revision').fetchone()['revision']
    event=db.execute('INSERT INTO people_events(action,actor,before_state,after_revision) VALUES(%s,%s,%s,%s) RETURNING id',(action,actor,Jsonb(before),rev)).fetchone()['id']
    db.execute('DELETE FROM people_previews WHERE token=%s',(token,))
    return {'ok':True,'event_id':event,'revision':rev,'person_id':pid}

def undo(db,event_id,expected_revision,actor):
    _lock(db);rev=revision(db)
    event=db.execute('SELECT * FROM people_events WHERE id=%s',(event_id,)).fetchone()
    if not event:raise LookupError('Correction not found')
    if event['undone'] or rev!=expected_revision or event['after_revision']!=rev:raise RuntimeError('Undo conflicts with newer corrections; refresh before proceeding')
    state=event['before_state'];ids=[r['detection_id'] for r in state['assignments']]
    lock_ids=set(ids)|_state_ids(db)|{i for pair in state['constraints'] for i in (pair['a'],pair['b'])}
    _lock_media(db,sorted(lock_ids))
    if not db.execute('SELECT 1 FROM people_events WHERE id=%s',(event_id,)).fetchone() or revision(db)!=rev:raise RuntimeError('Undo conflicts with deleted faces')
    if len(eligible(db,ids))!=len(ids):raise RuntimeError('Undo conflicts with deleted, hidden or superseded faces')
    existing={r['id'] for r in db.execute('SELECT id FROM face_detections').fetchall()}
    if any(r['a'] not in existing or r['b'] not in existing for r in state['constraints']):raise RuntimeError('Undo conflicts with deleted faces')
    db.execute('DELETE FROM people_assignments');db.execute('DELETE FROM people_constraints')
    for p in state['people']:db.execute('UPDATE people SET name=%s WHERE id=%s',(p['name'],p['id']))
    for r in state['assignments']:db.execute('INSERT INTO people_assignments VALUES(%s,%s)',(r['detection_id'],r['person_id']))
    for r in state['constraints']:db.execute('INSERT INTO people_constraints VALUES(%s,%s)',(r['a'],r['b']))
    db.execute('UPDATE people_events SET undone=true WHERE id=%s',(event_id,))
    rev=db.execute('UPDATE people_revision SET revision=revision+1 RETURNING revision').fetchone()['revision']
    return {'ok':True,'revision':rev}

def events(db):
    return {'items':db.execute('SELECT id,action,actor,created_at,undone,(NOT undone AND after_revision=(SELECT revision FROM people_revision)) can_undo FROM people_events ORDER BY id DESC LIMIT 30').fetchall(),'revision':revision(db)}

def unassigned(db):
    rows=db.execute(ELIGIBLE+" AND NOT EXISTS(SELECT 1 FROM people_assignments pa WHERE pa.detection_id=d.id) ORDER BY d.id LIMIT %s",(MAX_ITEMS,)).fetchall()
    return {'faces':public_faces(rows),'revision':revision(db),'max_items':MAX_ITEMS}

def lifecycle_data(db,payload):
    pid=payload.get('person_id');action=payload['action']
    person=db.execute('SELECT * FROM people WHERE id=%s',(pid,)).fetchone()
    if not person:raise LookupError('Person not found')
    deleted=person['deleted_at'] is not None
    if (action=='delete' and deleted) or (action=='restore' and not deleted):
        raise RuntimeError('Person is already deleted' if deleted else 'Person is already active')
    ids=[r['detection_id'] for r in db.execute('SELECT detection_id FROM people_assignments WHERE person_id=%s',(pid,)).fetchall()]
    _lock_media(db,ids)
    rows=eligible(db,ids)
    name=payload.get('name',person['name']).strip()
    if action=='restore':
        if not 1<=len(name)<=100 or any(ord(c)<32 for c in name):raise ValueError('Enter a name of 1 to 100 characters')
        check_name(db,name,pid,allow_same=False)
    else:name=person['name']
    state={'name':person['name'],'deleted_at':str(person['deleted_at']) if deleted else None,'stored_face_count':len(ids),'face_count':len(rows),'photo_count':len({r['media_id'] for r in rows})}
    return person,name,state,rows

def lifecycle_preview(db,payload,actor):
    _lock(db);person,name,state,rows=lifecycle_data(db,payload)
    token=uuid.uuid4().hex;rev=revision(db)
    body={'action':payload['action'],'person_id':person['id'],'name':name,'expected':state}
    db.execute('INSERT INTO people_previews(token,revision,payload,actor) VALUES(%s,%s,%s,%s)',(token,rev,Jsonb(body),actor))
    return {'token':token,'revision':rev,'action':payload['action'],'source_person':{'id':person['id'],'name':person['name']},**state,'name':name,'faces':public_faces(rows),'preservation':'Photos, files, albums and detection reviews are preserved. Assignments stay reserved for restoration. Restore cannot recover permanently removed faces.'}

def lifecycle_apply(db,saved,actor):
    if saved['revision']!=revision(db):raise RuntimeError('People changed since preview; preview again')
    payload=saved['payload'];person,name,state,rows=lifecycle_data(db,payload)
    if saved['revision']!=revision(db) or not db.execute('SELECT 1 FROM people_previews WHERE token=%s',(saved['token'],)).fetchone():raise RuntimeError('People changed since preview; preview again')
    if state!=payload['expected']:raise RuntimeError('Person or visible faces changed since preview; preview again')
    if payload['action']=='delete':db.execute('UPDATE people SET deleted_at=now() WHERE id=%s',(person['id'],))
    else:db.execute('UPDATE people SET deleted_at=NULL,name=%s WHERE id=%s',(name,person['id']))
    db.execute('INSERT INTO people_lifecycle_events(person_id,action,actor,old_name,new_name) VALUES(%s,%s,%s,%s,%s)',(person['id'],payload['action'],actor,person['name'],name))
    rev=db.execute('UPDATE people_revision SET revision=revision+1 RETURNING revision').fetchone()['revision']
    db.execute('DELETE FROM people_previews WHERE token=%s',(saved['token'],))
    return {'ok':True,'revision':rev,'person_id':person['id'],'action':payload['action']}

def deleted_people(db):
    items=[]
    for p in db.execute('SELECT id,name,deleted_at FROM people WHERE deleted_at IS NOT NULL ORDER BY deleted_at DESC,id').fetchall():
        ids=[r['detection_id'] for r in db.execute('SELECT detection_id FROM people_assignments WHERE person_id=%s',(p['id'],)).fetchall()]
        rows=eligible(db,ids)
        items.append({**p,'stored_face_count':len(ids),'face_count':len(rows),'photo_count':len({r['media_id'] for r in rows})})
    return {'items':items,'revision':revision(db)}
