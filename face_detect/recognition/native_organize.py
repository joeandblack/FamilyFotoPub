"""Conservative calibrated named matches and singleton regrouping, with undo."""
from collections import Counter
from face_detect.recognition.suggestions import suggest
from face_detect.recognition.grouping import cluster


def plan_model(data,model,rows):
    policy=data['native_policies'][model];moves={}
    def record(r):
        return {'id':r['id'],'photo_id':r['photo'],'content_id':r['content'],'event_id':r['event_id'],
                'space_id':model,'vector':r['vector'],'seed':r['seed'] and r['event_id']!='unknown-date'}
    anchors=[record(r)|{'person_id':r['person_id']} for r in rows if r['trusted']]
    queries=[r for r in rows if r['query']]
    byid={r['id']:r for r in rows}
    targets={pid:[r for r in rows if r['person_id']==pid] for pid in {r['person_id'] for r in rows if r['person_id'] is not None}}
    proposals=suggest(anchors,[record(r) for r in queries],policy['threshold'],policy['margin'],max_records=len(anchors)+len(queries))
    for p in sorted(proposals,key=lambda p:-(p['score'] if p['score'] is not None else -2)):
        did,pid=p['id'],p['person_id'];r=byid[did]
        if pid is None or (did,pid) in data['blocks']:continue
        if any(other['photo']==r['photo'] or other['content']==r['content'] or frozenset((did,other['id'])) in data['constraints'] for other in targets[pid]):continue
        moves[did]={'person_id':pid,'space_id':model};targets[pid].append(r)
    # Existing non-singleton groups and manual decisions are not split/rebuilt.
    counts=Counter(m['group_id'] for m in data['members'].values())
    fresh=[r for r in queries if r['id'] not in moves and counts[r['group']]==1 and data['members'][r['id']]['origin']=='automatic']
    # Bound quadratic work; additional singleton candidates remain for the next run.
    fresh=sorted(fresh,key=lambda r:r['id'])[:500]
    for group in cluster([record(r) for r in fresh],policy['threshold'],[tuple(p) for p in data['constraints']],margin=policy['margin'],max_records=500):
        if len(group['ids'])<2:continue
        target=min(byid[i]['group'] for i in group['ids'])
        for did in group['ids']:
            if byid[did]['group']!=target:moves[did]={'group_id':target,'space_id':model}
    return moves
