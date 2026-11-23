"""Bounded label-free clustering over replaceable, normalized embedding adapters.

No database, photo decoding, identity labels, or production side effects. Event IDs
must conservatively collapse bursts; content IDs collapse duplicate source photos.
"""
import math


def unit(vector, dimensions):
    if len(vector) != dimensions or not all(math.isfinite(v) for v in vector):
        raise ValueError('Expected finite embedding dimensions')
    norm = math.sqrt(sum(v*v for v in vector))
    if norm < 1e-8:
        raise ValueError('Degenerate embedding')
    return [v/norm for v in vector], norm


def cluster(items, threshold, cannot_link=(), margin=.05, anchors=None, max_records=100):
    """Quality seeds, complete-link merges, then unambiguous supported attachment.

    Return every item, including isolated outliers. Only clusters supported by at
    least two distinct events are considered supported automatic groups.
    """
    if type(max_records) is not int or not 1<=max_records<=500 or len(items) > max_records or not 0 <= threshold <= 1 or not 0 <= margin <= 1:
        raise ValueError('Invalid bounded clustering configuration')
    if not items:
        return []
    items = sorted(items, key=lambda x: str(x['id']))
    if len({x['id'] for x in items}) != len(items):
        raise ValueError('Duplicate item ID')
    # Optional trusted existing assignments are constraints, never predictions.
    # Evaluation labels MUST NOT be supplied here when measuring held-out quality.
    anchors = dict(anchors or {})
    if not set(anchors).issubset({x['id'] for x in items}):
        raise ValueError('Anchor references unknown item')
    if len({x['space_id'] for x in items}) != 1:
        raise ValueError('Cannot mix model spaces')
    dim = len(items[0]['vector'])
    for x in items:
        _, norm = unit(x['vector'], dim)
        if abs(norm-1) > .001:
            raise ValueError('Unit vectors required')
        if not all(x.get(k) is not None for k in ('photo_id', 'content_id', 'event_id')):
            raise ValueError('Photo, content and event IDs required')
    forbidden = {frozenset(p) for p in cannot_link}
    def similarity(a, b):
        if (a['photo_id'] == b['photo_id'] or a['content_id'] == b['content_id']
                or (a['id'] in anchors and b['id'] in anchors
                    and anchors[a['id']] != anchors[b['id']])
                or frozenset((a['id'], b['id'])) in forbidden):
            return -2
        return max(-1, min(1, sum(v*w for v,w in zip(a['vector'], b['vector']))))
    scores = {(i,j): similarity(a,b) for i,a in enumerate(items)
              for j,b in enumerate(items) if i < j}
    def score(a,b):
        return min(scores[min(i,j),max(i,j)] for i in a for j in b)
    def support(g):
        return len({items[i]['event_id'] for i in g}) >= 2
    groups = [[i] for i,x in enumerate(items) if x.get('seed', False)]
    while True:
        choices = [(score(a,b),i,j) for i,a in enumerate(groups)
                   for j,b in enumerate(groups) if i < j]
        if not choices:
            break
        best = max(choices, key=lambda x:(x[0],-x[1],-x[2]))
        value,i,j = best
        if value < threshold:
            break
        groups[i] = sorted(groups[i]+groups[j]); del groups[j]
    for i,x in enumerate(items):
        if x.get('seed', False):
            continue
        choices = sorted([(score([i],g),j) for j,g in enumerate(groups)
                          if support(g)], reverse=True)
        if (choices and choices[0][0] >= threshold and
                (len(choices)==1 or choices[0][0]-choices[1][0] >= margin)):
            groups[choices[0][1]].append(i)
        else:
            groups.append([i])
    return [{'ids':[items[i]['id'] for i in g], 'supported':support(g),
             'events':len({items[i]['event_id'] for i in g})}
            for g in groups]
