"""Opt-in recognition repository; versioned observations and atomic receipts.

Enabled only inside the separate recognition service or disposable test schemas.
"""
import hashlib
import json
from collections import defaultdict
from psycopg.types.json import Jsonb
from app.config import load_config
from face_detect.recognition import people
from face_detect.recognition import ignored_faces
from face_detect.recognition.grouping import unit
from face_detect.recognition.grouping import cluster
from face_detect.recognition.suggestions import suggest
from face_detect.recognition.people_face_quality import NOT_BLURRY

ELIGIBLE=ignored_faces.BASE+" AND m.kind='image' AND NOT d.ignored AND d.review_state<>'rejected'"+NOT_BLURRY
embedding_cache=None  # Enabled only by the single-threaded scan worker.


def guard():
    if load_config().get('automatic_people',{}).get('enabled') is not True:
        raise RuntimeError('Automatic People is disabled')


def rows(db,ids=None):
    return db.execute('SELECT d.id,d.box,a.media_id,m.sha256 content_hash,m.captured_at '+ELIGIBLE+
                      (' AND d.id=ANY(%s)' if ids is not None else '')+' ORDER BY d.id',
                      (ids,) if ids is not None else ()).fetchall()


def event(db,action,origin,actor,before):
    revision=db.execute('UPDATE people_revision SET revision=revision+1 RETURNING revision').fetchone()['revision']
    return db.execute('INSERT INTO auto_people_events(action,origin,actor,before_state,after_revision) VALUES(%s,%s,%s,%s,%s) RETURNING id',
                      (action,origin,actor,Jsonb(before),revision)).fetchone()['id']


def named_group(db,pid):
    if not db.execute('SELECT 1 FROM people WHERE id=%s AND deleted_at IS NULL',(pid,)).fetchone():raise ValueError('Person unavailable')
    found=db.execute('SELECT id FROM auto_people_groups WHERE person_id=%s',(pid,)).fetchone()
    return found['id'] if found else db.execute('INSERT INTO auto_people_groups(person_id) VALUES(%s) RETURNING id',(pid,)).fetchone()['id']


def put_member(db,did,gid,origin,model):
    db.execute('INSERT INTO auto_people_members VALUES(%s,%s,%s,%s) ON CONFLICT(detection_id) DO UPDATE SET group_id=EXCLUDED.group_id,origin=EXCLUDED.origin,space_id=EXCLUDED.space_id',(did,gid,origin,model))


def restore_manual_memberships(db,model,valid,manual,memberships):
    # Receipt replay must honor legacy corrections, but already-correct members
    # need no writes. Rechecking every manual face separately held the review
    # lock for thousands of round trips on every new scan receipt.
    repairs=[(did,pid) for did,pid in manual.items() if did in valid and (
        did not in memberships or memberships[did]['person_id']!=pid or
        memberships[did]['origin']!='manual' or memberships[did]['space_id']!=model)]
    if not repairs:return
    persons=sorted({pid for _,pid in repairs})
    db.execute('INSERT INTO auto_people_groups(person_id) SELECT pid FROM unnest(%s::bigint[]) pid ON CONFLICT(person_id) DO NOTHING',(persons,))
    groups={r['person_id']:r['id'] for r in db.execute('SELECT id,person_id FROM auto_people_groups WHERE person_id=ANY(%s)',(persons,)).fetchall()}
    db.execute('''INSERT INTO auto_people_members(detection_id,group_id,origin,space_id)
      SELECT did,gid,'manual',%s FROM unnest(%s::bigint[],%s::bigint[]) AS repaired(did,gid)
      ON CONFLICT(detection_id) DO UPDATE SET group_id=EXCLUDED.group_id,
        origin=EXCLUDED.origin,space_id=EXCLUDED.space_id''',
      (model,[did for did,_ in repairs],[groups[pid] for _,pid in repairs]))
    for did,pid in repairs:
        memberships[did]={'detection_id':did,'group_id':groups[pid],'origin':'manual',
                         'space_id':model,'person_id':pid,'deleted_at':None}


def cohort(db,model):
    valid={r['id']:r for r in rows(db)}
    records=(embedding_cache.records(db,model) if embedding_cache is not None else
             db.execute('SELECT o.*,e.vector FROM auto_people_observations o JOIN embedding e USING(detection_id,space_id) WHERE o.space_id=%s ORDER BY detection_id',(model,)).fetchall())
    from face_detect.recognition.policy import native_policy
    policy=native_policy(db,model)
    seeds={}
    if policy.get('calibrated'):
        from face_detect.recognition.calibration import native_seed
        from face_detect.catalog.evidence import reference_records
        for migrated in reference_records(db, model):
            seeds[migrated['detection_id']]=native_seed(migrated['result']['face'],policy['minimum_detection_score'])
        # Scan results name their space, or (saved before the one catalog) its old model ID.
        legacy=db.execute('SELECT legacy_model_id FROM space WHERE space_id=%s',(model,)).fetchone()
        names={model,legacy['legacy_model_id'] if legacy else None}-{None}
        for scan in db.execute('SELECT s.items FROM auto_people_scan_results s').fetchall():
            for item in scan['items']:
                if (item['result'].get('space_id') or item['result'].get('model_id')) in names:
                    seeds[item['input']['id']]=native_seed(item['result'],policy['minimum_detection_score'])
    items=[]
    for r in records:
        meta=valid.get(r['detection_id'])
        if meta:
            items.append({'id':r['detection_id'],'photo_id':meta['media_id'],'content_id':meta['content_hash'],
                          'event_id':str(meta['captured_at'])[:10] if meta['captured_at'] else 'unknown-date',
                          'space_id':model,'vector':r['vector'],'seed':seeds.get(r['detection_id'],r['seed'])})
    return items


def ingest(db,receipt,model,observations,actor='automatic-worker',provenance=None):
    guard();people._lock(db)
    if not receipt or len(receipt)>100 or not 0<=len(observations)<=100:raise ValueError('Bounded receipt required')
    ids=[r['detection_id'] for r in observations]
    if len(set(ids))!=len(ids):raise ValueError('Duplicate observation')
    payload={'model':model,'observations':observations}
    if provenance is not None:payload['provenance']=provenance
    digest=hashlib.sha256(json.dumps(payload,sort_keys=True,allow_nan=False).encode()).hexdigest()
    saved=db.execute('SELECT * FROM auto_people_receipts WHERE id=%s',(receipt,)).fetchone()
    if saved:
        if saved['input_hash']!=digest:raise ValueError('Receipt payload changed')
        return saved['result']
    manifest=db.execute('SELECT manifest FROM space WHERE space_id=%s',(model,)).fetchone()
    if not manifest:raise ValueError('Registered versioned model required')
    from face_detect.recognition.policy import matching_policy
    policy=matching_policy(db,model)
    review_only=policy.get('calibrated') is not True
    people._lock_media(db,ids)
    eligible={r['id'] for r in rows(db,ids)}
    for obs in observations:
        if obs['detection_id'] not in eligible:continue
        vector,norm=unit(obs['vector'],manifest['manifest']['dimensions'])
        if abs(norm-1)>.001:raise ValueError('Normalized vectors required')
        existing=db.execute('SELECT vector FROM embedding WHERE detection_id=%s AND space_id=%s',(obs['detection_id'],model)).fetchone()
        if existing and existing['vector']!=obs['vector']:raise ValueError('Embedding changed within model version')
        db.execute('INSERT INTO embedding(detection_id,space_id,vector) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING',(obs['detection_id'],model,Jsonb(obs['vector'])))
        db.execute('INSERT INTO auto_people_observations VALUES(%s,%s,%s) ON CONFLICT DO NOTHING',(obs['detection_id'],model,bool(obs['seed'])))
    items=cohort(db,model);byid={r['id']:r for r in items}
    reserved={r['detection_id'] for r in db.execute('SELECT detection_id FROM people_assignments').fetchall()}
    manual={r['detection_id']:r['person_id'] for r in db.execute('SELECT a.* FROM people_assignments a JOIN people p ON p.id=a.person_id WHERE p.deleted_at IS NULL').fetchall()}
    memberships={r['detection_id']:r for r in db.execute('SELECT m.*,g.person_id,p.deleted_at FROM auto_people_members m JOIN auto_people_groups g ON g.id=m.group_id LEFT JOIN people p ON p.id=g.person_id').fetchall()}
    restore_manual_memberships(db,model,byid,manual,memberships)
    anchors=[r|{'person_id':memberships[r['id']]['person_id']} for r in items if r['id'] in memberships and memberships[r['id']]['person_id'] is not None and memberships[r['id']]['deleted_at'] is None and memberships[r['id']]['origin'] in ('manual','named_seed')]
    pending=[r for r in items if r['id'] not in memberships and r['id'] not in reserved]
    blocks={(r['detection_id'],r['person_id']) for r in db.execute('SELECT * FROM auto_people_blocks').fetchall()}
    constraints={frozenset((r['a'],r['b'])) for r in db.execute('SELECT * FROM people_constraints').fetchall()}
    attached=[]
    for proposal in suggest(anchors,[] if review_only else pending,threshold=policy.get('threshold',1),margin=policy.get('margin',1),max_records=len(anchors)+len(pending)):
        did,pid=proposal['id'],proposal['person_id']
        if pid is None or (did,pid) in blocks:continue
        members=[r for r in items if r['id'] in memberships and memberships[r['id']]['person_id']==pid]
        if any(frozenset((did,r['id'])) in constraints or r['photo_id']==byid[did]['photo_id'] for r in members):continue
        gid=named_group(db,pid);put_member(db,did,gid,'automatic',model);attached.append(did)
        memberships[did]={'person_id':pid,'group_id':gid,'origin':'automatic','deleted_at':None}
    pending=[r for r in pending if r['id'] not in attached]
    # Incremental unnamed attachment; never merge existing group IDs or named people.
    unnamed=defaultdict(list)
    for r in items:
        m=memberships.get(r['id'])
        if m and m['person_id'] is None:unnamed[m['group_id']].append(r)
    fresh=[]
    for r in pending:
        candidates=[]
        for gid,members in ([] if review_only else unnamed.items()):
            if not r['seed'] or any(not x['seed'] for x in members):continue
            if any(x['photo_id']==r['photo_id'] or x['content_id']==r['content_id'] or frozenset((r['id'],x['id'])) in constraints for x in members):continue
            score=min(sum(a*b for a,b in zip(r['vector'],x['vector'])) for x in members)
            if score>=policy['threshold']:candidates.append((score,gid))
        candidates.sort(reverse=True)
        if candidates and (len(candidates)==1 or candidates[0][0]-candidates[1][0]>=policy['margin']):
            gid=candidates[0][1];put_member(db,r['id'],gid,'automatic',model);unnamed[gid].append(r)
        else:fresh.append(r)
    for group in ([{'ids':[r['id']]} for r in fresh] if review_only else cluster(fresh,policy['threshold'],[tuple(c) for c in constraints],margin=policy['margin'])):
        gid=db.execute('INSERT INTO auto_people_groups DEFAULT VALUES RETURNING id').fetchone()['id']
        for did in group['ids']:put_member(db,did,gid,'automatic',model)
    outcome={'accepted':len(eligible),'excluded':len(ids)-len(eligible),'known_attachments':len(attached)}
    event(db,'receipt','automatic',actor,{'receipt':receipt,'detection_ids':sorted(eligible),'space_id':model,'provenance':provenance})
    db.execute('INSERT INTO auto_people_receipts(id,input_hash,space_id,result) VALUES(%s,%s,%s,%s)',(receipt,digest,model,Jsonb(outcome)))
    return outcome


def recheck_unnamed(db,media_ids,actor):
    guard();people._lock(db)
    scope=set(media_ids);matched=0;checked=0
    memberships={r['detection_id']:r for r in db.execute('SELECT m.*,g.person_id,p.deleted_at FROM auto_people_members m JOIN auto_people_groups g ON g.id=m.group_id LEFT JOIN people p ON p.id=g.person_id').fetchall()}
    reserved={r['detection_id'] for r in db.execute('SELECT detection_id FROM people_assignments').fetchall()}
    blocks={(r['detection_id'],r['person_id']) for r in db.execute('SELECT * FROM auto_people_blocks').fetchall()}
    constraints={frozenset((r['a'],r['b'])) for r in db.execute('SELECT * FROM people_constraints').fetchall()}
    visible={r['id']:r for r in rows(db)}
    people._lock_media(db,sorted(visible))
    for model in db.execute('SELECT DISTINCT space_id FROM auto_people_observations').fetchall():
        from face_detect.recognition.policy import matching_policy
        policy=matching_policy(db,model['space_id'])
        if not policy.get('calibrated'):continue
        items=cohort(db,model['space_id'])
        anchors=[r|{'person_id':memberships[r['id']]['person_id']} for r in items if r['id'] in memberships and memberships[r['id']]['person_id'] is not None and memberships[r['id']]['deleted_at'] is None and memberships[r['id']]['origin'] in ('manual','named_seed')]
        queries=[r for r in items if r['photo_id'] in scope and r['id'] in memberships and memberships[r['id']]['person_id'] is None and r['id'] not in reserved]
        checked+=len(queries)
        for proposal in suggest(anchors,queries,threshold=policy['threshold'],margin=policy['margin'],max_records=len(anchors)+len(queries)):
            did,pid=proposal['id'],proposal['person_id']
            if pid is None or (did,pid) in blocks:continue
            peers=[other for other,m in memberships.items() if m['person_id']==pid and other in visible]
            if any(frozenset((did,other)) in constraints or visible[did]['media_id']==visible[other]['media_id'] for other in peers):continue
            gid=named_group(db,pid)
            put_member(db,did,gid,'automatic',model['space_id'])
            memberships[did]['person_id']=pid
            matched+=1
    event(db,'recheck','automatic',actor,{'media_ids':sorted(scope),'checked':checked,'matched':matched})
    return {'checked':checked,'matched':matched,'remaining':checked-matched}


def review_section(group, baseline):
    if group['person_id'] is not None:return 'people'
    if baseline is None:return 'other' if group['optional_outlier'] else 'review'
    previous=set(baseline.get(str(group['id']),[]))
    return 'other' if previous and all(f['detection_id'] in previous for f in group['faces']) else 'review'


def initialize_review_sections(db, current_media_ids):
    """One-time presentation baseline; never change memberships or recognition."""
    guard();people._lock(db)
    if db.execute("SELECT 1 FROM auto_people_events WHERE action='review-sections-baseline'").fetchone():return
    current=set(current_media_ids)
    legacy={str(g['id']):[f['detection_id'] for f in g['faces']]
            for g in catalog(db)['groups'] if g['optional_outlier']
            and not any(f['media_id'] in current for f in g['faces'])}
    event(db,'review-sections-baseline','manual','desktop-upgrade',{'legacy':legacy})


def named_faces_sql():
    # Share all live-media, superseded, ignored and quality exclusions with review.
    return '''WITH eligible AS (SELECT d.id,a.media_id,m.sha256 ''' + ELIGIBLE + ''')
      SELECT e.*,g.id group_id,p.id person_id,p.name FROM eligible e
      JOIN auto_people_members gm ON gm.detection_id=e.id
      JOIN auto_people_groups g ON g.id=gm.group_id
      LEFT JOIN people_assignments pa ON pa.detection_id=e.id
      JOIN people p ON p.id=coalesce(pa.person_id,g.person_id)
      WHERE p.deleted_at IS NULL'''


def named_people(db,import_id=None):
    """Compact live cards, without rebuilding unnamed groups or loading embeddings."""
    guard()
    from face_detect.recognition.people_review_scope import media_ids
    scope=media_ids(db,import_id)
    face_sql=named_faces_sql() if scope is None else 'SELECT * FROM ('+named_faces_sql()+') scoped WHERE media_id=ANY(%s)'
    choices=db.execute('''WITH faces AS (''' + face_sql + '''),
      covers AS (SELECT DISTINCT ON (before_state->>'person_id')
        before_state->>'person_id' pid,before_state->>'detection_id' did
        FROM auto_people_events WHERE action='cover' ORDER BY before_state->>'person_id',id DESC)
      SELECT p.id person_id,p.name,count(DISTINCT f.media_id) photo_count,
        count(f.id) face_count,(array_agg(f.group_id ORDER BY f.id))[1] group_id,
        coalesce(min(f.id) FILTER (WHERE f.id::text=c.did),min(f.id)) cover_detection_id,
        (array_agg(f.media_id ORDER BY (f.id::text=c.did) DESC NULLS LAST,f.id))[1] cover_media_id
      FROM people p LEFT JOIN faces f ON f.person_id=p.id
      LEFT JOIN covers c ON c.pid=p.id::text WHERE p.deleted_at IS NULL
      GROUP BY p.id,p.name ORDER BY lower(p.name),p.id''',() if scope is None else (list(scope),)).fetchall()
    people=ordered_people(db,choices)
    from app.viewer import current
    if current() is not None:
        # Someone whose every photo is hidden from this viewer must not be listed by name.
        people=[p for p in people if p['photo_count']]
    return {'people':people}


def named_cards(db,import_id=None):
    """Main People page: one cover per person, never full face membership."""
    choices=named_people(db,import_id)['people']
    groups=[{'id':p['group_id'],'person_id':p['person_id'],'name':p['name'],
             'photo_count':p['photo_count'],'face_count':p['face_count'],
             'cover_detection_id':p['cover_detection_id'],'section':'people',
             'optional_outlier':False,'summary_only':True,
             'faces':[{'detection_id':p['cover_detection_id'],'media_id':p['cover_media_id']}]
             } for p in choices if p['photo_count']]
    return {'groups':groups,'people':choices,'outlier_faces':0,'summary_only':True,
            'revision':people.revision(db),'order_revision':order_revision(db)}


def ordered_people(db,choices):
    order=saved_order(db)
    # Older order entries can predate naming a group. Match the same identities
    # as catalog cards, retaining the first matching position, not the last.
    group_people={row['id']:row['person_id'] for row in db.execute(
        'SELECT id,person_id FROM auto_people_groups WHERE person_id IS NOT NULL').fetchall()}
    ranks={}
    for index,item in enumerate(order):
        for pid in (item.get('person_id'),group_people.get(item.get('group_id'))):
            if pid is not None:ranks.setdefault(pid,index)
    if all('photo_count' in person for person in choices):
        visible={person['person_id'] for person in choices if person['photo_count']}
    else:
        # Global visibility, not the currently selected import's subset.
        visible={row['person_id'] for row in db.execute(
            'SELECT DISTINCT person_id FROM ('+named_faces_sql()+') visible_people').fetchall()}
    return sorted(choices,key=lambda person:(person['person_id'] not in visible,
                                             ranks.get(person['person_id'],len(order))))


def catalog(db,gid=None,review_faces=None,import_id=None,group_ids=None):
    guard();groups={}
    where='' if gid is None else ' WHERE g.id=%s OR coalesce(a.person_id,g.person_id)=(SELECT person_id FROM auto_people_groups WHERE id=%s)'
    parameters=() if gid is None else (gid,gid)
    if group_ids is not None:
        where=' WHERE g.id=ANY(%s)';parameters=(group_ids,)
    scope=None
    if import_id is not None:
        from face_detect.recognition.people_review_scope import media_ids
        scope=media_ids(db,import_id)
        where=(' WHERE ('+where.removeprefix(' WHERE ')+') AND ' if where else ' WHERE ')+'''m.detection_id IN
          (SELECT d.id FROM face_detections d JOIN face_analyses fa ON fa.id=d.analysis_id WHERE fa.media_id=ANY(%s))'''
        parameters=(*parameters,list(scope))
    members=db.execute('SELECT m.*,g.person_id,p.name,p.deleted_at,a.person_id manual_person FROM auto_people_members m JOIN auto_people_groups g ON g.id=m.group_id LEFT JOIN people p ON p.id=g.person_id LEFT JOIN people_assignments a ON a.detection_id=m.detection_id'+where+' ORDER BY m.detection_id',parameters).fetchall()
    valid={r['id']:r for r in rows(db,None if gid is None and import_id is None and group_ids is None else [r['detection_id'] for r in members])}
    person_ids=list({r['manual_person'] or r['person_id'] for r in members}-{None})
    persons={r['id']:r for r in db.execute('SELECT id,name,deleted_at FROM people'+('' if gid is None else ' WHERE id=ANY(%s)'),() if gid is None else (person_ids,)).fetchall()}
    for r in members:
        if r['detection_id'] not in valid:continue
        pid=r['manual_person'] or r['person_id']
        person=persons.get(pid)
        if person and person['deleted_at'] is not None:continue
        key=('person',pid) if pid else ('group',r['group_id'])
        group=groups.setdefault(key,{'id':r['group_id'],'person_id':pid,'name':person['name'] if person else None,'faces':[]})
        group['faces'].append({'detection_id':r['detection_id'],'media_id':valid[r['detection_id']]['media_id'],'origin':'manual' if r['manual_person'] else r['origin']})
    if import_id is not None:
        for g in groups.values():g['faces']=[f for f in g['faces'] if f['media_id'] in scope]
        groups={key:g for key,g in groups.items() if g['faces']}
    if review_faces is not None:
        groups={key:g for key,g in groups.items() if str(g['id']) in review_faces}
        for g in groups.values():
            selected=set(review_faces[str(g['id'])])
            g['faces']=[f for f in g['faces'] if f['detection_id'] in selected]
        groups={key:g for key,g in groups.items() if g['faces']}
    covers={}
    cover_filter='' if gid is None else " AND (before_state->>'group_id'=%s OR before_state->>'person_id'=ANY(%s))"
    for saved in db.execute("SELECT before_state FROM auto_people_events WHERE action='cover'"+cover_filter+" ORDER BY id DESC",() if gid is None else (str(gid),list(map(str,person_ids)))).fetchall():
        choice=saved['before_state'];key=('person',choice['person_id']) if choice.get('person_id') else ('group',choice['group_id'])
        covers.setdefault(key,choice['detection_id'])
    for key,g in groups.items():
        eligible={f['detection_id'] for f in g['faces']}
        g['cover_detection_id']=covers.get(key) if covers.get(key) in eligible else g['faces'][0]['detection_id']
        dates={str(valid[f['detection_id']]['captured_at'])[:10] for f in g['faces']}
        g['optional_outlier']=g['person_id'] is None and len(dates)<2
    order=saved_order(db) if gid is None else []
    if gid is None:
        saved=db.execute("SELECT before_state FROM auto_people_events WHERE action='review-sections-baseline' ORDER BY id LIMIT 1").fetchone()
        baseline=saved['before_state']['legacy'] if saved else None
    else:
        saved=db.execute("SELECT before_state->'legacy'->%s members FROM auto_people_events WHERE action='review-sections-baseline' ORDER BY id LIMIT 1",(str(gid),)).fetchone()
        baseline={str(gid):saved['members'] or []} if saved else None
    deferred={}
    if gid is None:
        for saved in db.execute("SELECT before_state FROM auto_people_events WHERE action='review-set-aside' ORDER BY id").fetchall():
            deferred.update(saved['before_state']['groups'])
    else:
        saved=db.execute("SELECT before_state->'groups'->%s members FROM auto_people_events WHERE action='review-set-aside' AND (before_state->'groups') ? %s ORDER BY id DESC LIMIT 1",(str(gid),str(gid))).fetchone()
        if saved:deferred[str(gid)]=saved['members']
    released_groups={r['group_id'] for r in db.execute("SELECT DISTINCT m.group_id FROM auto_people_face_quality q JOIN auto_people_members m ON m.detection_id=q.detection_id WHERE q.state='released'"+(' AND m.group_id=%s' if gid is not None else ''), (gid,) if gid is not None else ()).fetchall()} if baseline is None else set()
    for g in groups.values():
        g['section']=review_section(g,deferred if str(g['id']) in deferred else baseline)
        if g['person_id'] is None and g['id'] in released_groups and str(g['id']) not in deferred:g['section']='review'
        g['review_version']=review_version(g)
    ordered=sorted(groups.values(),key=lambda g:next((i for i,item in enumerate(order) if order_matches(item,g)),len(order)))
    return {'groups':ordered,'people':ordered_people(db,[{'person_id':p['id'],'name':p['name']} for p in persons.values() if p['deleted_at'] is None]),
            'outlier_faces':sum(len(g['faces']) for g in groups.values() if g['section']=='other'),
            'revision':people.revision(db),'order_revision':order_revision(db)}


def review_version(group):
    """Only changes affecting a review decision invalidate its snapshot."""
    value=[group['id'],group['person_id'],group['section'],
           sorted((f['detection_id'],f['media_id'],f['origin']) for f in group['faces'])]
    return hashlib.sha256(json.dumps(value,separators=(',',':')).encode()).hexdigest()


def validate_review_groups(db,ids,versions,faces=None,reconcile=False,import_id=None):
    if set(versions)!=set(map(str,ids)):
        raise ValueError('Review snapshot must include exactly the selected groups')
    if faces is not None and (set(faces)!=set(versions) or any(
            not values or len(values)!=len(set(values)) or any(type(i) is not int or i<=0 for i in values)
            for values in faces.values())):
        raise ValueError('Review snapshot must include distinct faces for exactly the selected groups')
    # Rebuild the viewed subset from current server records, not client-supplied metadata.
    # Added faces are left out; moved/ignored/changed faces still invalidate the hash.
    groups=catalog(db,ids[0] if len(ids)==1 else None,review_faces=faces,import_id=import_id)['groups']
    current={str(g['id']):g['review_version'] for g in groups}
    changed={int(key) for key,value in versions.items() if current.get(key)!=value}
    if faces is not None:
        changed.update(g['id'] for g in groups if len(g['faces'])!=len(faces[str(g['id'])]))
    if changed and not reconcile:
        raise RuntimeError('Selected groups changed; refresh People and review the selection again')
    return {g['id']:g for g in groups if g['id'] in ids and g['id'] not in changed}


def review_updates(db,ids,faces,pid=None,import_id=None):
    """Return affected cards, including new destinations, in the committing transaction."""
    view=catalog(db,import_id=import_id)
    selected={did for members in faces.values() for did in members}
    groups=[g for g in view['groups'] if g['id'] in ids or
            (pid is not None and g['person_id']==pid) or
            any(f['detection_id'] in selected for f in g['faces'])]
    present={g['id'] for g in groups}
    return {'groups':groups,'removed_group_ids':[gid for gid in ids if gid not in present],
            'revision':view['revision'],'people':view['people']}


def set_aside_groups(db,ids,actor,selected_groups=None,progress=None):
    guard();people._lock(db)
    if not ids or len(ids)>5000 or len(set(ids))!=len(ids) or any(type(i) is not int or i<=0 for i in ids):
        raise ValueError('Select between 1 and 5000 distinct groups')
    current=selected_groups if selected_groups is not None else {g['id']:g for g in catalog(db)['groups']}
    if any(i not in current or current[i]['section']!='review' or current[i]['person_id'] is not None for i in ids):
        raise ValueError('Selected groups changed; refresh and select unnamed review groups')
    snapshot={}
    live={g['id']:g for g in catalog(db)['groups']} if selected_groups is not None else current
    faces=sorted({f['detection_id'] for i in ids for f in current[i]['faces']})
    if progress:progress('processing',0,len(faces))
    people._lock_media(db,faces)
    done=0
    with db.transaction():
        for i in ids:
            members=[f['detection_id'] for f in current[i]['faces']]
            target=i
            if len(members)!=len(live[i]['faces']):
                target=db.execute('INSERT INTO auto_people_groups DEFAULT VALUES RETURNING id').fetchone()['id']
                db.execute("UPDATE auto_people_members SET group_id=%s,origin='manual' WHERE detection_id=ANY(%s)",(target,members))
            snapshot[str(target)]=members
            done+=len(members)
            if progress:progress('processing',done,len(faces))
        event(db,'review-set-aside','manual',actor,{'groups':snapshot})
    return {'moved':len(ids)}


def saved_order(db):
    row=db.execute("SELECT before_state FROM auto_people_events WHERE action='order' ORDER BY id DESC LIMIT 1").fetchone()
    return row['before_state']['order'] if row else []


def order_revision(db):
    return db.execute("SELECT coalesce(max(id),0) id FROM auto_people_events WHERE action='order'").fetchone()['id']


def match_review_groups(db,ids,pid,actor,section='review',selected_groups=None,progress=None):
    guard();people._lock(db)
    if not ids or len(ids)>5000 or len(set(ids))!=len(ids) or any(type(i) is not int or i<=0 for i in ids):
        raise ValueError('Select between 1 and 5000 distinct groups')
    current=selected_groups if selected_groups is not None else {g['id']:g for g in catalog(db)['groups']}
    if not db.execute('SELECT 1 FROM people WHERE id=%s AND deleted_at IS NULL',(pid,)).fetchone():
        raise ValueError('Choose an existing named person; refresh')
    if section not in ('review','other') or any(i not in current or current[i]['section']!=section or current[i]['person_id'] is not None for i in ids):
        raise ValueError('Selected groups changed; refresh and select unnamed groups in this section')
    faces=sorted({f['detection_id'] for i in ids for f in current[i]['faces']})
    if progress:progress('processing',0,len(faces))
    people._lock_media(db,faces)
    with db.transaction():
        for index,did in enumerate(faces,1):
            correct(db,did,pid,actor)
            if progress:progress('processing',index,len(faces))
    return {'matched':len(faces),'groups':len(ids),'person_id':pid}


def ignore_review_groups(db,ids,actor,section='review',selected_groups=None,progress=None):
    guard();people._lock(db)
    if not ids or len(ids)>5000 or len(set(ids))!=len(ids) or any(type(i) is not int or i<=0 for i in ids):
        raise ValueError('Select between 1 and 5000 distinct groups')
    current=selected_groups if selected_groups is not None else {g['id']:g for g in catalog(db)['groups']}
    if section not in ('review','other') or any(i not in current or current[i]['section']!=section or current[i]['person_id'] is not None for i in ids):
        raise ValueError('Selected groups changed; refresh and select unnamed groups in this section')
    faces=sorted({f['detection_id'] for i in ids for f in current[i]['faces']})
    if progress:progress('processing',0,len(faces))
    people._lock_media(db,faces)
    # Respect the face service batch limit, but commit the whole selection atomically.
    with db.transaction():
        for offset in range(0,len(faces),250):
            ignored_faces.set_ignored(db,faces[offset:offset+250],True,people.revision(db),actor)
            if progress:progress('processing',min(offset+250,len(faces)),len(faces))
        event(db,'ignore-review-groups','manual',actor,{'group_ids':ids,'detection_ids':faces})
    return {'ignored':len(faces),'groups':len(ids)}


def order_matches(item,group):
    return (item.get('person_id') is not None and item['person_id']==group['person_id']) or item['group_id']==group['id']


def move_group(db,gid,direction,actor):
    guard();people._lock(db)
    if direction not in ('up','down'):raise ValueError('Choose up or down')
    groups=catalog(db)['groups']
    selected=next((g for g in groups if g['id']==gid),None)
    if selected is None:raise LookupError('Group unavailable; refresh')
    peers=[g for g in groups if g['section']==selected['section']]
    index=next(i for i,g in enumerate(peers) if g['id']==gid)
    target=index+(-1 if direction=='up' else 1)
    if not 0<=target<len(peers):return {'moved':False}
    # Retain hidden entries so restoring a face does not discard its saved position.
    order=saved_order(db)
    for group in groups:
        if not any(order_matches(item,group) for item in order):
            order.append({'group_id':group['id'],'person_id':group['person_id']})
    a=next(i for i,item in enumerate(order) if order_matches(item,selected))
    b=next(i for i,item in enumerate(order) if order_matches(item,peers[target]))
    order[a],order[b]=order[b],order[a]
    event(db,'order','manual',actor,{'group_id':gid,'direction':direction,'order':order})
    return {'moved':True}


def place_named_group(db,gid,target_id,after,actor,expected_order_revision=None):
    guard();people._lock(db)
    if expected_order_revision is not None and order_revision(db)!=expected_order_revision:
        raise RuntimeError('People order changed; refresh before reordering')
    groups=catalog(db)['groups']
    named={g['id']:g for g in groups if g['person_id'] is not None and g['name']}
    if gid not in named or target_id not in named:raise ValueError('People changed; refresh before reordering')
    if gid==target_id:return {'moved':False}
    order=saved_order(db)
    for group in groups:
        if not any(order_matches(item,group) for item in order):order.append({'group_id':group['id'],'person_id':group['person_id']})
    entry=order.pop(next(i for i,item in enumerate(order) if order_matches(item,named[gid])))
    index=next(i for i,item in enumerate(order) if order_matches(item,named[target_id]))
    order.insert(index+int(after),entry)
    event(db,'order','manual',actor,{'group_id':gid,'target_id':target_id,'after':after,'order':order})
    return {'moved':True}


def name_group(db,gid,name,actor,import_id=None):
    guard();people._lock(db);name=name.strip()
    if not 1<=len(name)<=100 or any(ord(c)<32 for c in name):raise ValueError('Valid name required')
    group=db.execute('SELECT * FROM auto_people_groups WHERE id=%s',(gid,)).fetchone()
    if not group:raise LookupError('Group missing')
    if group['person_id'] is not None:raise ValueError('Group already named; no automatic merge')
    visible=next((g for g in catalog(db,import_id=import_id)['groups'] if g['id']==gid and g['person_id'] is None),None)
    if not visible:raise ValueError('No eligible group members')
    people.check_name(db,name)
    ids=[f['detection_id'] for f in visible['faces']]
    if import_id is not None:
        # Never name hidden members from another import, including ineligible faces.
        gid=db.execute('INSERT INTO auto_people_groups DEFAULT VALUES RETURNING id').fetchone()['id']
        db.execute('UPDATE auto_people_members SET group_id=%s WHERE detection_id=ANY(%s)',(gid,ids))
    pid=db.execute('INSERT INTO people(name) VALUES(%s) RETURNING id',(name,)).fetchone()['id']
    db.execute('UPDATE auto_people_groups SET person_id=%s WHERE id=%s',(pid,gid))
    ids=[f['detection_id'] for f in visible['faces']]
    db.execute("UPDATE auto_people_members SET origin='named_seed' WHERE group_id=%s AND detection_id=ANY(%s)",(gid,ids))
    eid=event(db,'name','manual',actor,{'group_id':gid,'person_id':pid,'seed_ids':ids})
    return {'person_id':pid,'event_id':eid}


def set_cover(db,gid,did,actor,expected_cover_id=None):
    guard();people._lock(db);people._lock_media(db,[did])
    group=next((g for g in catalog(db,gid)['groups'] if g['id']==gid),None)
    if not group or did not in {f['detection_id'] for f in group['faces']}:
        raise ValueError('Choose an eligible face from this group')
    if expected_cover_id is not None and group['cover_detection_id']!=expected_cover_id:
        raise RuntimeError('This group cover changed; reopen the group before choosing a cover')
    event(db,'cover','manual',actor,{'group_id':gid,'person_id':group['person_id'],'detection_id':did})
    return {'cover_detection_id':did}


def change_selected_faces(db,gid,ids,action,pid,actor,validated_group=None,import_id=None):
    guard();people._lock(db)
    if not ids or len(ids)>5000 or len(set(ids))!=len(ids) or any(type(i) is not int or i<=0 for i in ids):
        raise ValueError('Select between 1 and 5000 distinct faces')
    group=validated_group if validated_group is not None else detail(db,gid)['group']
    if not set(ids)<={f['detection_id'] for f in group['faces']}:
        raise ValueError('Selected faces changed; reopen the group')
    if action not in ('move','ignore','review-later') or (action=='move' and (pid is None or pid==group['person_id'])):
        raise ValueError('Choose a different existing person')
    people._lock_media(db,sorted(ids))
    with db.transaction():
        if action=='move':
            correct_many(db,sorted(ids),pid,actor)
        elif action=='review-later':
            target=gid
            if import_id is not None or group['person_id'] is not None or len(ids)!=len(group['faces']):
                target=db.execute('INSERT INTO auto_people_groups DEFAULT VALUES RETURNING id').fetchone()['id']
                db.execute("UPDATE auto_people_members SET group_id=%s,origin='manual' WHERE detection_id=ANY(%s)",(target,ids))
            db.execute('DELETE FROM people_assignments WHERE detection_id=ANY(%s)',(ids,))
            if group['person_id'] is not None:
                for did in ids:db.execute('INSERT INTO auto_people_blocks VALUES(%s,%s) ON CONFLICT DO NOTHING',(did,group['person_id']))
            event(db,'review-set-aside','manual',actor,{'groups':{str(target):ids},'source_group_id':gid})
            return {'changed':len(ids),'group_ids':[target]}
        else:
            for offset in range(0,len(ids),250):
                ignored_faces.set_ignored(db,ids[offset:offset+250],True,people.revision(db),actor)
            event(db,'ignore-selected-faces','manual',actor,{'group_id':gid,'detection_ids':ids})
    return {'changed':len(ids)}


def match_group(db,gid,pid,actor,import_id=None):
    guard();people._lock(db)
    group=next((g for g in catalog(db,import_id=import_id)['groups'] if g['id']==gid),None)
    if not group:raise ValueError('Group unavailable; refresh')
    if group['person_id']==pid:raise ValueError('Choose a different person')
    ids=sorted(f['detection_id'] for f in group['faces'])
    people._lock_media(db,ids)
    # All corrections share one transaction: a conflict rolls back the whole group.
    with db.transaction():
        for did in ids:correct(db,did,pid,actor)
    return {'matched':len(ids),'person_id':pid}


def correct_many(db,ids,pid,actor):
    """Batch writes under the caller's correction/media locks and transaction.

    Preserve one ordered correction event per face so existing Undo semantics
    and history readers remain compatible with single-face corrections.
    """
    if len(rows(db,ids))!=len(ids):raise ValueError('Face is ineligible')
    members=db.execute('SELECT m.*,g.person_id FROM auto_people_members m JOIN auto_people_groups g ON g.id=m.group_id WHERE detection_id=ANY(%s) ORDER BY detection_id',(ids,)).fetchall()
    if len(members)!=len(ids):raise LookupError('Membership missing')
    assignments={r['detection_id']:r for r in db.execute('SELECT * FROM people_assignments WHERE detection_id=ANY(%s)',(ids,)).fetchall()}
    blocks=defaultdict(list)
    for row in db.execute('SELECT detection_id,person_id FROM auto_people_blocks WHERE detection_id=ANY(%s)',(ids,)).fetchall():
        blocks[row['detection_id']].append({'person_id':row['person_id']})
    gid=named_group(db,pid)
    target_ids=[r['detection_id'] for r in db.execute('SELECT detection_id FROM auto_people_members WHERE group_id=%s',(gid,)).fetchall()]
    # Include pairs within the selection: the old sequential path checked these
    # after the first member of the pair had entered the destination group.
    combined=list(set(target_ids)|set(ids))
    if db.execute('SELECT 1 FROM people_constraints WHERE (a=ANY(%s) AND b=ANY(%s)) OR (b=ANY(%s) AND a=ANY(%s)) LIMIT 1',(ids,combined,ids,combined)).fetchone():
        raise ValueError('Existing separation constraint must be undone first')
    snapshots=[{'member':m,'assignment':assignments.get(m['detection_id']),
                'blocks':blocks[m['detection_id']],'target_group_id':gid} for m in members]
    db.execute('''INSERT INTO auto_people_blocks(detection_id,person_id)
      SELECT m.detection_id,g.person_id FROM auto_people_members m
      JOIN auto_people_groups g ON g.id=m.group_id
      WHERE m.detection_id=ANY(%s) AND g.person_id IS NOT NULL AND g.person_id<>%s
      ON CONFLICT DO NOTHING''',(ids,pid))
    db.execute('''INSERT INTO people_assignments(detection_id,person_id)
      SELECT did,%s FROM unnest(%s::bigint[]) did
      ON CONFLICT(detection_id) DO UPDATE SET person_id=EXCLUDED.person_id''',(pid,ids))
    db.execute("UPDATE auto_people_members SET group_id=%s,origin='manual' WHERE detection_id=ANY(%s)",(gid,ids))
    revision=db.execute('UPDATE people_revision SET revision=revision+%s RETURNING revision',(len(ids),)).fetchone()['revision']
    db.execute('''INSERT INTO auto_people_events(action,origin,actor,before_state,after_revision)
      SELECT 'correct','manual',%s,value,%s+ordinal
      FROM jsonb_array_elements(%s::jsonb) WITH ORDINALITY AS snapshots(value,ordinal)
      ORDER BY ordinal''',(actor,revision-len(ids),Jsonb(snapshots)))


def correct(db,did,pid,actor):
    guard();people._lock(db);people._lock_media(db,[did])
    if not rows(db,[did]):raise ValueError('Face is ineligible')
    before=db.execute('SELECT m.*,g.person_id FROM auto_people_members m JOIN auto_people_groups g ON g.id=m.group_id WHERE detection_id=%s',(did,)).fetchone()
    if not before:raise LookupError('Membership missing')
    assignment=db.execute('SELECT * FROM people_assignments WHERE detection_id=%s',(did,)).fetchone()
    blocks=db.execute('SELECT person_id FROM auto_people_blocks WHERE detection_id=%s',(did,)).fetchall()
    gid=named_group(db,pid)
    target_ids=[r['detection_id'] for r in db.execute('SELECT detection_id FROM auto_people_members WHERE group_id=%s',(gid,)).fetchall()]
    if target_ids and db.execute('SELECT 1 FROM people_constraints WHERE (a=%s AND b=ANY(%s)) OR (b=%s AND a=ANY(%s)) LIMIT 1',(did,target_ids,did,target_ids)).fetchone():
        raise ValueError('Existing separation constraint must be undone first')
    if before['person_id'] and before['person_id']!=pid:
        db.execute('INSERT INTO auto_people_blocks VALUES(%s,%s) ON CONFLICT DO NOTHING',(did,before['person_id']))
    db.execute('INSERT INTO people_assignments VALUES(%s,%s) ON CONFLICT(detection_id) DO UPDATE SET person_id=EXCLUDED.person_id',(did,pid))
    put_member(db,did,gid,'manual',before['space_id'])
    return {'event_id':event(db,'correct','manual',actor,{'member':before,'assignment':assignment,'blocks':blocks,'target_group_id':gid})}


def detail(db,gid,import_id=None):
    guard();view=catalog(db,gid,import_id=import_id)
    group=next((g for g in view['groups'] if g['id']==gid),None)
    if not group:
        raw=db.execute('SELECT g.id,g.person_id,p.name FROM auto_people_groups g LEFT JOIN people p ON p.id=g.person_id WHERE g.id=%s AND p.deleted_at IS NULL',(gid,)).fetchone()
        if not raw:raise LookupError('Group unavailable')
        group=dict(raw,faces=[],cover_detection_id=None,optional_outlier=False,section='people' if raw['person_id'] else 'review')
        group['review_version']=review_version(group)
    ids={f['detection_id'] for f in group['faces']};history=[]
    for row in db.execute('SELECT * FROM auto_people_events ORDER BY id DESC LIMIT 100').fetchall():
        before=row['before_state'];member=before.get('member',{})
        did=member.get('detection_id',before.get('detection_id'))
        if did not in ids and member.get('group_id')!=gid and before.get('target_group_id')!=gid:continue
        history.append({'id':row['id'],'action':row['action'],'actor':row['actor'],'created_at':row['created_at'],
            'detection_id':did,'undone':row['undone'],'can_undo':row['action']=='correct' and not row['undone']
            and correction_is_current(db,row) and bool(rows(db,[did]))})
    return {'group':group,'revision':view['revision'],'history':history[:20]}


def correction_is_current(db,saved):
    # Automatic scan receipts do not invalidate a manual correction.
    latest=db.execute("SELECT max(id) id FROM auto_people_events WHERE origin='manual'").fetchone()['id']
    if saved['id']!=latest:return False
    before=saved['before_state'];did=before['member']['detection_id']
    current=db.execute('SELECT m.group_id,m.origin,g.person_id,a.person_id assigned FROM auto_people_members m JOIN auto_people_groups g ON g.id=m.group_id LEFT JOIN people_assignments a ON a.detection_id=m.detection_id WHERE m.detection_id=%s',(did,)).fetchone()
    return bool(current and current['group_id']==before['target_group_id'] and current['origin']=='manual' and current['assigned']==current['person_id'])


def undo_correction(db,eid,actor):
    guard();people._lock(db)
    saved=db.execute('SELECT * FROM auto_people_events WHERE id=%s',(eid,)).fetchone()
    if not saved or saved['action']!='correct' or saved['undone'] or not correction_is_current(db,saved):raise RuntimeError('This correction changed or a newer manual operation was made; Undo is no longer available')
    before=saved['before_state'];m=before['member'];did=m['detection_id']
    people._lock_media(db,[did])
    if not rows(db,[did]):raise ValueError('Undo face is ineligible')
    # A correction saved before the one catalog names its space by the old model ID.
    from face_detect.catalog.spaces import space_key
    put_member(db,did,m['group_id'],m['origin'],m.get('space_id') or space_key(db,m.get('model_id')))
    db.execute('DELETE FROM people_assignments WHERE detection_id=%s',(did,))
    if before['assignment']:db.execute('INSERT INTO people_assignments VALUES(%s,%s)',(did,before['assignment']['person_id']))
    db.execute('DELETE FROM auto_people_blocks WHERE detection_id=%s',(did,))
    for block in before['blocks']:db.execute('INSERT INTO auto_people_blocks VALUES(%s,%s)',(did,block['person_id']))
    db.execute('UPDATE auto_people_events SET undone=true WHERE id=%s',(eid,))
    event(db,'undo-correction','manual',actor,{'event_id':eid,'detection_id':did})
    return {'ok':True,'group_ids':[m['group_id'],before['target_group_id']]}


def ignore(db,did,enabled,actor):
    guard()
    result=ignored_faces.set_ignored(db,[did],enabled,people.revision(db),actor)
    event(db,'ignore' if enabled else 'unignore','manual',actor,{'detection_id':did})
    return result


def rename_person(db,pid,name,actor,expected_name=None):
    guard();people._lock(db);name=name.strip()
    if not 1<=len(name)<=100 or any(ord(c)<32 for c in name):
        raise ValueError('Valid name required')
    visible=next((g for g in catalog(db)['groups'] if g['person_id']==pid),None)
    if not visible:raise LookupError('Person unavailable')
    if expected_name is not None and visible['name']!=expected_name:
        raise RuntimeError('This person was renamed; reopen the group before saving a new name')
    people.check_name(db,name,pid)
    if visible['name']==name:return {'person_id':pid,'unchanged':True}
    db.execute('UPDATE people SET name=%s WHERE id=%s',(name,pid))
    eid=event(db,'rename','manual',actor,{'person_id':pid,'old_name':visible['name'],'new_name':name})
    return {'person_id':pid,'event_id':eid}


def ignored_collection(db,page=1):
    guard()
    where=ignored_faces.BASE+""" AND d.ignored AND m.kind='image'
        AND EXISTS(SELECT 1 FROM auto_people_members member WHERE member.detection_id=d.id)"""
    total=db.execute('SELECT count(*) n '+where).fetchone()['n']
    items=db.execute('SELECT d.id detection_id,a.media_id,m.name '+where+
        ' ORDER BY d.id DESC LIMIT 100 OFFSET %s',((page-1)*100,)).fetchall()
    return {'items':items,'total':total,'page':page,'pages':max(1,(total+99)//100),
            'revision':people.revision(db)}
