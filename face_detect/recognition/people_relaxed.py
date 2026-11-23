"""Explicit, persisted face suggestions. Never changes identity without review."""
from collections import defaultdict
from face_detect.recognition import automatic_people as auto
from face_detect.recognition import people

THRESHOLD = .30


def context(db):
    visible={r['id']:r for r in auto.rows(db)}
    members={r['detection_id']:r for r in db.execute('SELECT m.*,g.person_id,p.name,p.deleted_at FROM auto_people_members m JOIN auto_people_groups g ON g.id=m.group_id LEFT JOIN people p ON p.id=g.person_id').fetchall()}
    reserved={r['detection_id'] for r in db.execute('SELECT detection_id FROM people_assignments').fetchall()}
    blocks={(r['detection_id'],r['person_id']) for r in db.execute('SELECT * FROM auto_people_blocks').fetchall()}
    constraints={frozenset((r['a'],r['b'])) for r in db.execute('SELECT * FROM people_constraints').fetchall()}
    peers=defaultdict(list)
    for did,m in members.items():
        if did in visible and m['person_id'] is not None and m['deleted_at'] is None:peers[m['person_id']].append(did)
    return visible,members,reserved,blocks,constraints,peers


def eligible(candidate,ctx):
    visible,members,reserved,blocks,constraints,peers=ctx
    did,pid=candidate['detection_id'],candidate['person_id']
    if did not in visible or did not in members or members[did]['person_id'] is not None or did in reserved or (did,pid) in blocks:return False
    targets=peers.get(pid,[])
    if not targets:return False
    return not any(frozenset((did,other)) in constraints or
                   visible[did]['media_id']==visible[other]['media_id'] or
                   visible[did]['content_hash']==visible[other]['content_hash'] for other in targets)


def propose(anchors,queries,threshold=THRESHOLD):
    # Scores are similarities, not probabilities. Two independent anchor dates
    # and existing seed-quality requirements remain mandatory at the lower cutoff.
    import numpy as np
    if not anchors:return []
    matrix=np.asarray([a['vector'] for a in anchors],dtype=np.float64)
    result=[]
    for query in queries:
        if not query['seed']:continue
        scores=matrix@np.asarray(query['vector'],dtype=np.float64)
        events=defaultdict(dict)
        for anchor,score in zip(anchors,scores):
            if not anchor['seed'] or any(anchor[k]==query[k] for k in ('event_id','photo_id','content_id')):continue
            person=events[anchor['person_id']]
            previous=person.get(anchor['event_id'])
            if previous is None or score>previous[0]:person[anchor['event_id']]=(float(score),anchor['id'])
        ranked=[]
        for pid,dates in events.items():
            best=sorted(dates.values(),reverse=True)
            if len(best)>=2 and best[1][0]>=threshold:
                ranked.append({'detection_id':query['id'],'person_id':pid,'score':best[1][0],
                               'anchor_id':best[0][1],'space_id':query['space_id']})
        result.extend(sorted(ranked,key=lambda r:(-r['score'],r['person_id'])))
    return result


def generate(db,media_ids,actor,import_id=None):
    auto.guard();people._lock(db)
    ctx=context(db);_,members,reserved,_,_,_=ctx
    scope=set(media_ids);candidates={};policies={}
    for model in db.execute('SELECT DISTINCT space_id FROM auto_people_observations').fetchall():
        from face_detect.recognition.policy import legacy_model, manual_policy, manual_enabled
        policy=manual_policy(db,model['space_id'])
        if legacy_model(db,model['space_id']):threshold=THRESHOLD
        elif manual_enabled(policy) and 'suggestion_threshold' in policy:
            threshold=policy['suggestion_threshold']
        else:continue
        policies[model['space_id']]={'threshold':threshold,'review_required':True}
        items=auto.cohort(db,model['space_id'])
        anchors=[r|{'person_id':members[r['id']]['person_id']} for r in items if r['id'] in members and members[r['id']]['person_id'] is not None and members[r['id']]['deleted_at'] is None and members[r['id']]['origin'] in ('manual','named_seed')]
        queries=[r for r in items if r['photo_id'] in scope and r['id'] in members and members[r['id']]['person_id'] is None and r['id'] not in reserved]
        for candidate in propose(anchors,queries,threshold):
            if not eligible(candidate,ctx):continue
            key=(candidate['detection_id'],candidate['person_id'])
            if key not in candidates or candidate['score']>candidates[key]['score']:candidates[key]=candidate
    per_face=defaultdict(list)
    for candidate in candidates.values():per_face[candidate['detection_id']].append(candidate)
    result=[c for did in sorted(per_face) for c in sorted(per_face[did],key=lambda r:-r['score'])[:3]]
    run_id=auto.event(db,'relaxed-suggestions','manual',actor,{'policies':policies,'candidates':result,'import_id':import_id,'media_ids':sorted(scope)})
    return {'suggestions':len(result),'suggestion_run':run_id}


def collection(db,page=1,import_id=None):
    auto.guard()
    runs=db.execute("SELECT DISTINCT ON (coalesce(before_state->>'import_id','')) id,before_state FROM auto_people_events WHERE action='relaxed-suggestions' ORDER BY coalesce(before_state->>'import_id',''),id DESC").fetchall()
    runs=sorted(runs,key=lambda r:r['id'],reverse=True)
    if not any(r['before_state']['candidates'] for r in runs):
        return {'items':[],'total':0,'page':1,'pages':1,'run_id':runs[0]['id'] if runs else None,'revision':people.revision(db)}
    ctx=context(db);visible,members,_,_,_,peers=ctx
    candidates=[];seen=set();covered=set()
    for saved in runs:
        for c in saved['before_state']['candidates']:
            key=(c['detection_id'],c['person_id'])
            if key not in seen and eligible(c,ctx) and visible[c['detection_id']]['media_id'] not in covered:
                candidates.append(c|{'run_id':saved['id']});seen.add(key)
        covered.update(saved['before_state'].get('media_ids',[]))
    if import_id is not None:
        from face_detect.recognition.people_review_scope import media_ids
        scope=media_ids(db,import_id)
        candidates=[c for c in candidates if visible[c['detection_id']]['media_id'] in scope]
    pages=max(1,(len(candidates)+19)//20);page=min(max(1,page),pages)
    items=[]
    for c in candidates[(page-1)*20:page*20]:
        anchor=c['anchor_id'] if c['anchor_id'] in peers[c['person_id']] else peers[c['person_id']][0]
        items.append(c|{'anchor_id':anchor,'name':members[anchor]['name'],'group_id':members[c['detection_id']]['group_id']})
    return {'items':items,'total':len(candidates),'page':page,'pages':pages,'run_id':runs[0]['id'] if runs else None,'revision':people.revision(db)}


def decide(db,run_id,did,pid,decision,actor):
    auto.guard();people._lock(db)
    saved=db.execute("SELECT id,before_state FROM auto_people_events WHERE action='relaxed-suggestions' AND id=%s",(run_id,)).fetchone()
    if not saved or saved['id']!=run_id:raise RuntimeError('Suggestions changed; refresh')
    scope=saved['before_state'].get('import_id')
    latest=db.execute("SELECT max(id) id FROM auto_people_events WHERE action='relaxed-suggestions' AND (before_state->>'import_id') IS NOT DISTINCT FROM %s",(str(scope) if scope else None,)).fetchone()['id']
    if latest!=run_id:raise RuntimeError('Suggestions changed; refresh')
    candidate=next((c for c in saved['before_state']['candidates'] if c['detection_id']==did and c['person_id']==pid),None)
    if candidate is None:raise ValueError('Suggestion unavailable')
    people._lock_media(db,[did])
    if not eligible(candidate,context(db)):raise RuntimeError('This suggestion is no longer eligible; refresh')
    if decision=='confirm':return auto.correct(db,did,pid,actor)
    if decision!='reject':raise ValueError('Unknown decision')
    db.execute('INSERT INTO auto_people_blocks VALUES(%s,%s) ON CONFLICT DO NOTHING',(did,pid))
    auto.event(db,'reject-relaxed-suggestion','manual',actor,{'detection_id':did,'person_id':pid,'run_id':run_id})
    return {'ok':True}
