"""Date-disjoint calibration of native matching. Labels never enter predictions."""
from collections import defaultdict
from datetime import date
import hashlib
from face_detect.recognition.quality import quality_seed
from face_detect.recognition.suggestions import suggest
from face_detect.recognition.grouping import cluster

UNKNOWN_DATE = 'unknown-date'


def capture_event_id(value):
    """Keep missing/invalid dates out of date-disjoint evaluation, including old typos."""
    if value is None:
        return UNKNOWN_DATE
    try:
        return date.fromisoformat(str(value).strip()[:10]).isoformat()
    except ValueError:
        return UNKNOWN_DATE


def identity_map(equivalent_people):
    """Evaluation labels only; original person IDs still drive predictions."""
    identities = {}
    for group in equivalent_people:
        if len(group) < 2 or any(type(pid) is not int or pid <= 0 for pid in group):
            raise ValueError('Equivalent identities need at least two valid person IDs')
        if len(set(group)) != len(group) or any(pid in identities for pid in group):
            raise ValueError('Equivalent identity groups must be distinct and non-overlapping')
        identities.update({pid: min(group) for pid in group})
    return identities


def native_seed(result,minimum=.8):
    # Keep the frozen receipt's geometry policy intact; derive eligibility anew.
    return result['box']['probability']>=minimum and quality_seed(result|{'box':result['box']|{'probability':1.}})


def validation_predictions(anchors,queries):
    """Same two-date rule as suggest, with bounded local evaluation matrices."""
    import numpy as np
    if not anchors:
        return suggest([],queries,0,0,max_records=len(queries))
    vectors=np.asarray([r['vector'] for r in anchors],dtype=np.float64)
    result=[]
    for offset in range(0,len(queries),64):
        batch=queries[offset:offset+64]
        scores=np.asarray([r['vector'] for r in batch],dtype=np.float64) @ vectors.T
        for query,similarities in zip(batch,scores):
            events=defaultdict(dict)
            for anchor,score in zip(anchors,similarities):
                if not anchor['seed'] or any(anchor[k]==query[k] for k in ('event_id','photo_id','content_id')):continue
                dates=events[anchor['person_id']]
                dates[anchor['event_id']]=max(dates.get(anchor['event_id'],-1),float(score))
            ranked=sorted((sorted(dates.values(),reverse=True)[1],str(pid),pid)
                          for pid,dates in events.items() if len(dates)>=2)
            ranked.reverse()
            best=ranked[0][0] if ranked else None
            margin=best-ranked[1][0] if len(ranked)>1 else None
            result.append({'id':query['id'],'person_id':ranked[0][2] if ranked and query['seed'] and best>=0 else None,
                           'score':best,'margin':margin})
    return result


def assess(anchors,queries,threshold,margin,cache=None,identities=None):
    identities = identities or {}
    original_labels={r['id']:r['person_id'] for r in queries}
    labels={did:identities.get(pid,pid) for did,pid in original_labels.items()}
    key=(tuple(r['id'] for r in anchors),tuple(r['id'] for r in queries))
    predictions=None if cache is None else cache.get(key)
    if predictions is None:
        predictions=validation_predictions(anchors,[{k:v for k,v in r.items() if k!='person_id'} for r in queries])
        if cache is not None:cache[key]=predictions
    accepted=[r for r in predictions if r['person_id'] is not None and r['score']>=threshold
              and (r['margin'] is None or r['margin']>=margin)]
    # Exercise bounded cross-date groups, never silently drop held-out faces.
    ordered=sorted(queries,key=lambda r:hashlib.sha256(str(r['id']).encode()).hexdigest())
    groups=[]
    for offset in range(0,len(ordered),100):
        groups.extend(cluster([{k:v for k,v in r.items() if k!='person_id'} for r in ordered[offset:offset+100]],threshold,margin=margin))
    mixed=[g for g in groups if len({labels[i] for i in g['ids']})>1]
    return {'queries':len(queries),'accepted':len(accepted),'wrong':sum(identities.get(r['person_id'],r['person_id'])!=labels[r['id']] for r in accepted),
            'mixed_groups':len(mixed),'grouped_faces':sum(len(g['ids']) for g in groups if len(g['ids'])>1),
            'accepted_people':len({identities.get(r['person_id'],r['person_id']) for r in accepted}),
            'same_identity_cross_group_matches':sum(r['person_id']!=original_labels[r['id']] and
                identities.get(r['person_id'],r['person_id'])==labels[r['id']] for r in accepted),
            'same_identity_cross_group_clusters':sum(len({original_labels[i] for i in g['ids']})>1 and
                len({labels[i] for i in g['ids']})==1 for g in groups)}


def tuning_sample(rows,limit=150):
    """Deterministic identity/date balance, without using any similarity scores."""
    buckets=defaultdict(list)
    for row in sorted(rows,key=lambda r:hashlib.sha256(str(r['id']).encode()).hexdigest()):
        buckets[(row['person_id'],row['event_id'])].append(row)
    persons=defaultdict(list)
    for (pid,_day),items in buckets.items():persons[pid].append(items)
    selected=[]
    while len(selected)<limit and any(persons.values()):
        for pid in sorted(persons):
            dates=persons[pid]
            if not dates:continue
            items=dates.pop(0);selected.append(items.pop(0))
            if items:dates.append(items)
            if len(selected)==limit:break
    return selected


def calibrate(rows,equivalent_people=()):
    rows=[{**r,'event_id':capture_event_id(r.get('event_id'))} for r in rows]
    identities=identity_map(equivalent_people)
    if len({r['space_id'] for r in rows})!=1:raise ValueError('Exactly one embedding space required')
    dates=sorted({r['event_id'] for r in rows if r['event_id']!='unknown-date'})
    if len(dates)<6:raise ValueError('At least six labeled dates required for separate tuning and validation')
    holdout=set(dates[-max(2,len(dates)//3):])
    available=[r for r in rows if r['event_id'] not in holdout and r['event_id']!='unknown-date']
    train=tuning_sample(available)
    training_content={r['content_id'] for r in available}
    test=[r for r in rows if r['event_id'] in holdout and r['content_id'] not in training_content]
    cache={}
    # Tune with leave-one-date-out predictions within the training dates only.
    candidates=[]
    for threshold in (.4,.45,.5,.55,.6,.65,.7,.75,.8):
        for margin in (.05,.1,.15,.2):
            parts=[assess([r for r in train if r['event_id']!=date],[r for r in train if r['event_id']==date],threshold,margin,cache,identities)
                   for date in sorted({r['event_id'] for r in train})]
            totals={key:sum(p[key] for p in parts) for key in ('queries','accepted','wrong','mixed_groups','grouped_faces')}
            # Also test grouping across dates, not just within a date.
            groups=assess([],train,threshold,margin,cache,identities)
            if not totals['wrong'] and not totals['mixed_groups'] and not groups['mixed_groups']:
                candidates.append((totals['accepted'],threshold,margin,totals))
    if not candidates:raise ValueError('No error-free candidate on training labels')
    _,threshold,margin,tuning=max(candidates,key=lambda c:(c[0],c[1],c[2]))
    validation=assess(available,test,threshold,margin,identities=identities)
    # Test rejection when a person's identity has no anchors at all.
    unknown_trials=[]
    for pid in sorted({identities.get(r['person_id'],r['person_id']) for r in test}):
        unknown_trials.append(assess(
            [r for r in available if identities.get(r['person_id'],r['person_id'])!=pid],
            [r for r in test if identities.get(r['person_id'],r['person_id'])==pid],
            threshold,margin,identities=identities))
    validation['unknown_identity_false_matches']=sum(r['accepted'] for r in unknown_trials)
    qualified=validation['unknown_identity_false_matches']==0 and validation['accepted']>=10 and validation['accepted_people']>=3 and validation['wrong']==0 and validation['mixed_groups']==0
    return {'schema':'familyfoto-calibration-v2','space_id':rows[0]['space_id'],'labels':len(rows),
            'people':len({identities.get(r['person_id'],r['person_id']) for r in rows}),
            'person_groups':len({r['person_id'] for r in rows}),
            'equivalent_people':[sorted(group) for group in equivalent_people],
            'unknown_date_faces_excluded':sum(r['event_id']==UNKNOWN_DATE for r in rows),
            'training_dates':len({r['event_id'] for r in train}),'validation_dates':len(holdout),
            'available_training_labels':len(available),'sampled_training_labels':len(train),
            'grouping_validation_batch_size':100,'training':tuning,'validation':validation,
            'policy':{'calibrated':qualified,'threshold':threshold,'margin':margin,'minimum_detection_score':.8,'quality_policy':'native-geometry-v1'},
            'qualified':qualified,'limitation':'Small local validation sample; zero observed errors is not a guarantee. Keep ambiguous faces for review.'}
