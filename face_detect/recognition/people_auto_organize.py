"""Explicit, reversible organization of cached faces; never runs detection."""
from collections import Counter, defaultdict

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components
from psycopg.types.json import Jsonb

from face_detect.recognition import automatic_people as auto
from app import database
from face_detect.recognition import people

POLICY = {'version': 1, 'named_best': .44, 'named_support': .36,
          'named_single': .60, 'named_margin': .07, 'group_edge': .55,
          'group_centroid': .75, 'group_mean': .45, 'neighbors': 12, 'max_group': 200}


def snapshot(db,import_id=None):
    auto.guard()
    members = {r['detection_id']: dict(r) for r in db.execute(
        'SELECT * FROM auto_people_members ORDER BY detection_id').fetchall()}
    groups = {r['id']: r for r in db.execute(
        'SELECT g.*,p.deleted_at FROM auto_people_groups g LEFT JOIN people p ON p.id=g.person_id').fetchall()}
    assignments = {r['detection_id']: r['person_id'] for r in db.execute('SELECT * FROM people_assignments').fetchall()}
    visible = {r['id']: r for r in auto.rows(db)}
    # Keep Review later decisions out of the automatic backlog operation.
    review_ids = {f['detection_id'] for g in auto.catalog(db,import_id=import_id)['groups']
                  if g['section'] == 'review' for f in g['faces']}
    from face_detect.recognition.policy import manual_policy, manual_enabled
    policies={r['id']:manual_policy(db,r['id']) for r in db.execute("SELECT space_id id FROM space WHERE manifest->>'schema'='familyfoto-native-model-v1'").fetchall()}
    from face_detect.recognition.policy import legacy_model
    legacy_models={r['id']:legacy_model(db,r['id']) for r in db.execute('SELECT space_id id FROM space').fetchall()}
    native_seeds={model:{r['id']:r['seed'] for r in auto.cohort(db,model)} for model,p in policies.items() if manual_enabled(p)}
    models = defaultdict(list)
    for r in db.execute('SELECT o.*,e.vector FROM auto_people_observations o JOIN embedding e USING(detection_id,space_id) ORDER BY detection_id').fetchall():
        if not legacy_models.get(r['space_id']) and not manual_enabled(policies.get(r['space_id'],{})):continue
        did = r['detection_id']
        if did not in visible or did not in members or members[did]['space_id'] != r['space_id']:
            continue
        group = groups[members[did]['group_id']]
        pid = assignments.get(did, group['person_id'])
        if group['deleted_at'] is not None:
            continue
        meta = visible[did]
        models[r['space_id']].append({'id': did, 'person_id': pid,
            'trusted': pid is not None and (did in assignments or members[did]['origin'] in ('manual', 'named_seed')),
            'query': did in review_ids and did not in assignments and pid is None,
            'photo': meta['media_id'], 'content': meta['content_hash'], 'vector': r['vector'],
            'event_id':str(meta['captured_at'])[:10] if meta['captured_at'] else 'unknown-date',
            'group': members[did]['group_id'], 'seed': native_seeds.get(r['space_id'],{}).get(did,r['seed'])})
    return {'native_policies':policies,'models': dict(models), 'members': members, 'revision': people.revision(db), 'import_id':import_id,
            'blocks': {(r['detection_id'], r['person_id']) for r in db.execute('SELECT * FROM auto_people_blocks').fetchall()},
            'constraints': {frozenset((r['a'], r['b'])) for r in db.execute('SELECT * FROM people_constraints').fetchall()}}


def matrix(rows):
    values = np.asarray([r['vector'] for r in rows], dtype=np.float32)
    norms = np.linalg.norm(values, axis=1, keepdims=True)
    if not np.isfinite(values).all() or (norms < 1e-8).any():
        raise ValueError('Invalid saved face vectors')
    return values / norms


def constraint_map(pairs):
    constraints = defaultdict(set)
    for pair in pairs:
        a, b = tuple(pair); constraints[a].add(b); constraints[b].add(a)
    return constraints


def rank_people(query, refs, person_refs, scores, blocks, constraints):
    """People ordered by how well their reference faces match one query face."""
    ranked = []
    for pid, indices in person_refs.items():
        if (query['id'], pid) in blocks:
            continue
        events = {}
        for index in indices:
            anchor = refs[index]
            if anchor['photo'] == query['photo'] or anchor['content'] == query['content'] or anchor['id'] in constraints[query['id']]:
                continue
            events[anchor['content']] = max(events.get(anchor['content'], -1), float(scores[index]))
        best = sorted(events.values(), reverse=True)
        if best:
            support = best[1] if len(best) > 1 else -1
            rank = (best[0]+support)/2 if support >= 0 else best[0]-.08
            ranked.append((rank, pid, best[0], support))
    ranked.sort(reverse=True)
    return ranked


def match_candidates(queries, refs, person_refs, blocks, constraints, progress, diagnostics):
    """Queries with one clear, strong named match: (rank, query, pid, best, support)."""
    qmat = matrix(queries)
    refmat = matrix(refs) if refs else None
    candidates = []
    for offset in range(0, len(queries), 128):
        progress('matching', offset, len(queries))
        batch = queries[offset:offset+128]
        scores = qmat[offset:offset+128] @ refmat.T if refs else None
        for local, query in enumerate(batch):
            ranked = rank_people(query, refs, person_refs, scores[local] if refs else None, blocks, constraints)
            if not ranked:
                diagnostics['no_reference'] += 1; continue
            rank, pid, best, support = ranked[0]
            if best < POLICY['named_best'] or (support < POLICY['named_support'] and best < POLICY['named_single']):
                diagnostics['weak_match'] += 1; continue
            if len(ranked)>1 and rank-ranked[1][0] < POLICY['named_margin']:
                diagnostics['ambiguous'] += 1; continue
            candidates.append((rank, query, pid, best, support))
    return candidates


def match_named(data, model, rows, queries, constraints, progress, diagnostics, moves, evidence):
    refs = [r for r in rows if r['trusted']]
    peers = defaultdict(list)
    for r in rows:
        if r['person_id'] is not None: peers[r['person_id']].append(r)
    person_refs = defaultdict(list)
    for index, r in enumerate(refs): person_refs[r['person_id']].append(index)
    candidates = match_candidates(queries, refs, person_refs, data['blocks'], constraints, progress, diagnostics)
    # Strongest first, including same-photo constraints between new proposals.
    for rank, query, pid, best, support in sorted(candidates, key=lambda r: -r[0]):
        if any(p['photo']==query['photo'] or p['id'] in constraints[query['id']] for p in peers[pid]):
            diagnostics['conflict'] += 1; continue
        moves[query['id']] = {'person_id': pid, 'space_id': model}
        peers[pid].append(query)
        evidence.append({'id': query['id'], 'person_id': pid, 'best': round(best,4), 'support': round(support,4)})


def group_components(remaining, vectors, by_group, constraints, progress):
    """Existing groups joined into components by strong cross-group neighbours."""
    edges_a, edges_b = [], []
    k = min(POLICY['neighbors']+1, len(remaining))
    for offset in range(0,len(remaining),128):
        scores = vectors[offset:offset+128] @ vectors.T
        nearest = np.argpartition(scores, -k, axis=1)[:,-k:]
        for local, neighbors in enumerate(nearest):
            i = offset+local
            for j in neighbors:
                if i==j or scores[local,j]<POLICY['group_edge']:continue
                a,b=remaining[i],remaining[j]
                if a['photo']==b['photo'] or b['id'] in constraints[a['id']]:continue
                edges_a.append(i);edges_b.append(int(j))
        progress('grouping',min(offset+128,len(remaining)),len(remaining))
    # Existing groups are indivisible; do not fragment them during consolidation.
    for indices in by_group.values():
        for i in indices[1:]:edges_a.append(indices[0]);edges_b.append(i)
    graph=coo_matrix((np.ones(len(edges_a)),(edges_a,edges_b)),shape=(len(remaining),len(remaining))).tocsr()
    _,labels=connected_components(graph,directed=False)
    components=defaultdict(list)
    for gid,indices in by_group.items():components[int(labels[indices[0]])].append(indices)
    return components


def merge_units(units, remaining, vectors, constraints, tick):
    """Pack one component's groups into coherent buckets; returns the buckets."""
    bins=[]
    for indices in sorted(units,key=lambda v:(-len(v),remaining[v[0]]['group'])):
        chosen=None;best_score=-1
        for bucket in bins:
            combined=bucket+indices
            if len(combined)>POLICY['max_group']:continue
            old=[remaining[i] for i in bucket];new=[remaining[i] for i in indices]
            if any(a['photo']==b['photo'] or b['id'] in constraints[a['id']] for a in old for b in new):continue
            centroid=vectors[combined].mean(axis=0);centroid/=max(np.linalg.norm(centroid),1e-8)
            coherence=float(np.min(vectors[combined] @ centroid))
            # Require a real strong cross-group edge, not only a shared graph component.
            cross_scores=vectors[bucket] @ vectors[indices].T
            cross=float(np.max(cross_scores));mean=float(np.mean(cross_scores))
            if coherence>=POLICY['group_centroid'] and cross>=POLICY['group_edge'] and mean>=POLICY['group_mean'] and coherence>best_score:
                chosen=bucket;best_score=coherence
        if chosen is None:bins.append(list(indices))
        else:chosen.extend(indices)
        tick(len(indices))
    return bins


def consolidate(model, remaining, constraints, progress, moves):
    if len(remaining)<2:
        return
    progress('grouping', 0, len(remaining))
    vectors = matrix(remaining)
    by_group = defaultdict(list)
    for i,r in enumerate(remaining): by_group[r['group']].append(i)
    components = group_components(remaining, vectors, by_group, constraints, progress)
    consolidated = 0
    progress('consolidating', 0, len(remaining))
    def tick(count):
        nonlocal consolidated
        consolidated += count
        if consolidated % 25 < count:
            progress('consolidating', consolidated, len(remaining))
    for units in components.values():
        for bucket in merge_units(units, remaining, vectors, constraints, tick):
            target=min(remaining[i]['group'] for i in bucket)
            for i in bucket:
                row=remaining[i]
                if row['group']!=target:moves[row['id']]={'group_id':target,'space_id':model}
    progress('consolidating', len(remaining), len(remaining))


def summarize(data, moves, diagnostics, original_groups):
    after_groups=set()
    for rows in data['models'].values():
        for r in rows:
            if not r['query']:continue
            change=moves.get(r['id'],{})
            if 'person_id' not in change:after_groups.add(change.get('group_id',r['group']))
    return {**diagnostics,'matched_faces':sum('person_id' in m for m in moves.values()),
            'regrouped_faces':sum('group_id' in m for m in moves.values()),
            'groups_before':len(original_groups),'groups_after':len(after_groups)}


def plan(data, progress=lambda *args: None):
    moves, evidence, diagnostics = {}, [], Counter()
    constraints = constraint_map(data['constraints'])
    original_groups = set()
    for model, rows in data['models'].items():
        queries = [r for r in rows if r['query']]
        if not queries:
            continue
        original_groups.update(r['group'] for r in queries)
        diagnostics['faces_examined'] += len(queries)
        diagnostics['non_seed_faces'] += sum(not r['seed'] for r in queries)
        from face_detect.recognition.policy import manual_enabled
        if manual_enabled(data.get('native_policies',{}).get(model,{})):
            from face_detect.recognition.native_organize import plan_model
            moves.update(plan_model(data,model,rows))
            continue
        match_named(data, model, rows, queries, constraints, progress, diagnostics, moves, evidence)
        remaining = [r for r in queries if r['id'] not in moves]
        consolidate(model, remaining, constraints, progress, moves)
    summary = summarize(data, moves, diagnostics, original_groups)
    return {'policy':{'legacy':POLICY,'native':data['native_policies']} if data.get('native_policies') else POLICY,'revision':data['revision'],'moves':moves,'summary':summary,'evidence':evidence}


def apply(db,data,proposal,actor):
    auto.guard();people._lock(db)
    if people.revision(db)!=data['revision']:
        raise RuntimeError('People changed during preparation. Run Auto-organize again; no changes were saved.')
    ids=sorted(proposal['moves'])
    if not ids:return {**proposal['summary'],'event_id':None}
    if set(ids)-{r['id'] for r in auto.rows(db,ids)}:
        raise RuntimeError('Some photos became unavailable. Run Auto-organize again; no changes were saved.')
    before=[data['members'][did] for did in ids]
    targets={pid:auto.named_group(db,pid) for pid in {m['person_id'] for m in proposal['moves'].values() if 'person_id' in m}}
    # A filtered merge must not inherit hidden members of its destination group.
    regroup_targets={}
    if data.get('import_id') is not None:
        destinations={m['group_id'] for m in proposal['moves'].values() if 'group_id' in m}
        scoped={r['id']:r for rows in data['models'].values() for r in rows if r['query']}
        for target in destinations:
            new=db.execute('INSERT INTO auto_people_groups DEFAULT VALUES RETURNING id').fetchone()['id']
            regroup_targets[target]=new
            for did,row in scoped.items():
                if row['group']==target and did not in proposal['moves']:
                    before.append(data['members'][did])
                    proposal['moves'][did]={'group_id':target,'space_id':data['members'][did]['space_id']}
    after=[{'detection_id':did,'group_id':targets[m['person_id']] if 'person_id' in m else regroup_targets.get(m['group_id'],m['group_id']),
            'origin':'automatic','space_id':m['space_id']} for did,m in proposal['moves'].items()]
    db.execute('''UPDATE auto_people_members m SET group_id=r.group_id,origin=r.origin,space_id=r.space_id
      FROM jsonb_to_recordset(%s) AS r(detection_id bigint,group_id bigint,origin text,space_id text)
      WHERE m.detection_id=r.detection_id''',(Jsonb(after),))
    eid=auto.event(db,'auto-organize','manual',actor,{'before':before,'after':after,'summary':proposal['summary'],'policy':proposal['policy'],'import_id':data.get('import_id')})
    return {**proposal['summary'],'event_id':eid}


def undo(db,eid,actor,import_id=None):
    auto.guard();people._lock(db)
    saved=db.execute("SELECT * FROM auto_people_events WHERE id=%s AND action='auto-organize' AND NOT undone FOR UPDATE",(eid,)).fetchone()
    if not saved:raise ValueError('This Auto-organize run has already been undone or is unavailable')
    if import_id is not None and saved['before_state'].get('import_id')!=import_id:
        raise ValueError('This Auto-organize run belongs to a different import filter')
    from face_detect.catalog.spaces import space_key
    spaces={}
    def current_names(r):
        # A run saved before the one catalog names its space by the old model ID.
        if 'space_id' in r or 'model_id' not in r:return r
        old=r['model_id']
        if old not in spaces:spaces[old]=space_key(db,old)
        return {k:v for k,v in r.items() if k!='model_id'}|{'space_id':spaces[old]}
    before={r['detection_id']:current_names(r) for r in saved['before_state']['before']}
    current={r['detection_id']:dict(r) for r in db.execute('SELECT * FROM auto_people_members WHERE detection_id=ANY(%s)',(list(before),)).fetchall()}
    assigned={r['detection_id'] for r in db.execute('SELECT detection_id FROM people_assignments WHERE detection_id=ANY(%s)',(list(before),)).fetchall()}
    restore=[before[r['detection_id']] for r in map(current_names,saved['before_state']['after'])
             if current.get(r['detection_id'])==r and r['detection_id'] not in assigned]
    db.execute('''UPDATE auto_people_members m SET group_id=r.group_id,origin=r.origin,space_id=r.space_id
      FROM jsonb_to_recordset(%s) AS r(detection_id bigint,group_id bigint,origin text,space_id text)
      WHERE m.detection_id=r.detection_id''',(Jsonb(restore),))
    db.execute('UPDATE auto_people_events SET undone=true WHERE id=%s',(eid,))
    auto.event(db,'undo-auto-organize','manual',actor,{'event_id':eid,'restored':len(restore)})
    return {'restored':len(restore),'kept_later_changes':len(before)-len(restore)}


def run(actor,progress=lambda *args:None,import_id=None,connect=None):
    from face_detect.jobs.automatic_people_jobs import manual_lease
    connect=connect or database.connection
    with manual_lease(progress):
        progress('loading',0,None)
        with connect() as db:data=snapshot(db,import_id)
        proposal=plan(data,progress)
        progress('saving',0,None)
        with connect() as db:return apply(db,data,proposal,actor)
