"""Metrics for already-produced clusters; labels never enter the clusterer."""
from collections import Counter, defaultdict
from itertools import combinations


def pair_scores(items, labels):
    result=[]
    for a,b in combinations(items,2):
        if a['photo_id']==b['photo_id'] or a['event_id']==b['event_id'] or a['content_id']==b['content_id']:
            continue
        result.append((sum(x*y for x,y in zip(a['vector'],b['vector'])),labels[a['id']]==labels[b['id']]))
    return result


def choose_threshold(pairs, grid=(.30,.35,.40,.45,.50,.55,.60,.65,.70,.75,.80,.85,.90,.95)):
    """Small fixed grid: lowest threshold with no observed tuning false positives.

    This empirical rule is not a guarantee. Refuse calibration with no positive or
    negative pairs, or when no grid point meets the conservative condition.
    """
    if not any(y for _,y in pairs) or not any(not y for _,y in pairs):
        return None
    return next((t for t in grid if not any(s>=t and not y for s,y in pairs)),None)


def pair_report(pairs, threshold):
    def distribution(values):
        values=sorted(values)
        return {'n':len(values),'min':values[0] if values else None,
                'median':values[len(values)//2] if values else None,
                'max':values[-1] if values else None}
    result={'same':distribution([s for s,y in pairs if y]),
            'different':distribution([s for s,y in pairs if not y])}
    if threshold is not None:
        result.update(threshold=threshold,tp=sum(s>=threshold and y for s,y in pairs),
                      fn=sum(s<threshold and y for s,y in pairs),
                      fp=sum(s>=threshold and not y for s,y in pairs),
                      tn=sum(s<threshold and not y for s,y in pairs))
    return result


def grouping_report(groups, labels):
    ids=[i for g in groups for i in g['ids']]
    if len(set(ids))!=len(ids) or set(ids)!=set(labels):
        raise ValueError('Groups must partition the evaluation labels')
    supported=[g for g in groups if g['supported']]
    counts=[Counter(labels[i] for i in g['ids']) for g in supported]
    grouped={i for g in supported for i in g['ids']}
    links={frozenset(p) for g in supported for p in combinations(g['ids'],2)}
    truth={frozenset((a,b)) for a,b in combinations(labels,2) if labels[a]==labels[b]}
    fragments=defaultdict(int)
    for g in groups:
        for person in {labels[i] for i in g['ids']}:fragments[person]+=1
    return {'faces':len(labels),'people':len(set(labels.values())),
            'supported_groups':len(supported),'supported_faces':len(grouped),
            'unsupported_faces':len(labels)-len(grouped),
            'purity_numerator':sum(max(c.values()) for c in counts),
            'purity_denominator':len(grouped),
            'false_merge_groups':sum(len(c)>1 for c in counts),
            'predicted_pairs':len(links),'correct_pairs':len(links&truth),
            'false_pairs':len(links-truth),'same_person_pairs':len(truth),
            'fragments_per_person':dict(fragments),
            'singleton_people':sum(n==1 for n in Counter(labels.values()).values())}
