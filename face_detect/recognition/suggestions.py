"""Offline proposals only. Query labels are forbidden; no production writes."""
from face_detect.recognition.grouping import unit


def suggest(anchors, queries, threshold=.4, margin=.05, max_records=100):
    rows=anchors+queries
    if len(rows)>max_records or not 0<=threshold<=1 or not 0<=margin<=1:
        raise ValueError('Invalid bounded configuration')
    if any('person_id' in q for q in queries):raise ValueError('Query labels must be withheld')
    if len({r['id'] for r in rows})!=len(rows):raise ValueError('Duplicate item')
    if len({r['space_id'] for r in rows})>1:raise ValueError('Mixed spaces')
    dim=len(rows[0]['vector']) if rows else 0
    for row in rows:
        _,norm=unit(row['vector'],dim)
        if abs(norm-1)>.001:raise ValueError('Unit vectors required')
        if any(row.get(k) is None for k in ('event_id','photo_id','content_id')):
            raise ValueError('Eligibility/duplicate context required')
    if any(a.get('person_id') is None for a in anchors):raise ValueError('Trusted anchor labels required')
    result=[]
    for query in queries:
        by_person={}
        for a in anchors:
            if not a.get('seed',False) or any(a[k]==query[k] for k in ('event_id','photo_id','content_id')):continue
            score=sum(v*w for v,w in zip(a['vector'],query['vector']))
            events=by_person.setdefault(a['person_id'],{})
            events[a['event_id']]=max(events.get(a['event_id'],-1),score)
        ranked=sorted([(sorted(events.values(),reverse=True)[1],str(person),person)
                       for person,events in by_person.items() if len(events)>=2],reverse=True)
        best=ranked[0][0] if ranked else None
        gap=best-ranked[1][0] if len(ranked)>1 else None
        reason=('low_quality' if not query.get('seed',False) else
                'insufficient_anchor_events' if not ranked else
                'below_threshold' if best<threshold else
                'ambiguous' if gap is not None and gap<margin else 'suggested')
        result.append({'id':query['id'],'person_id':ranked[0][2] if reason=='suggested' else None,
                       'reason':reason,'score':best,'margin':gap})
    return result
