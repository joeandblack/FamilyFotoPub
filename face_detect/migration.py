"""Conservative correspondence for retaining reviewed face IDs across models.

This module never assigns people or changes review decisions. A native face must
agree geometrically and uniquely with an existing box before its vector is usable.
"""
import math


def overlap(a, b):
    intersection = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(
        0, min(a[3], b[3]) - max(a[1], b[1]))
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - intersection
    return intersection / union if union > 0 else 0


def correspond(existing, detected, size, minimum=.6, margin=.2):
    width, height = size
    if width <= 0 or height <= 0:
        raise ValueError('Positive canonical dimensions required')
    boxes = []
    for face in detected:
        b = face['box']
        box = [b[k] for k in ('x_min', 'y_min', 'x_max', 'y_max')]
        if not all(math.isfinite(v) for v in box) or not (
            0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height
        ):
            raise ValueError('Invalid native face box')
        boxes.append(box)
    proposals = {}
    scores = {}
    for face in existing:
        b = face['box']
        values = [b[k] for k in ('x', 'y', 'width', 'height')]
        if not all(math.isfinite(v) for v in values) or not (
            0 <= b['x'] < 1 and 0 <= b['y'] < 1 and b['width'] > 0 and b['height'] > 0
            and b['x'] + b['width'] <= 1.000001 and b['y'] + b['height'] <= 1.000001
        ):
            continue
        box = [b['x'] * width, b['y'] * height,
               (b['x'] + b['width']) * width, (b['y'] + b['height']) * height]
        ranked = sorted(((overlap(box, other), index) for index, other in enumerate(boxes)), reverse=True)
        if not ranked or ranked[0][0] < minimum:
            continue
        if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < margin:
            continue
        score, index = ranked[0]
        proposals.setdefault(index, []).append(face['id'])
        scores[face['id']] = score
    # Even a better score must not steal a detection from another retained face.
    return {ids[0]: {'index': index, 'overlap': scores[ids[0]]}
            for index, ids in proposals.items() if len(ids) == 1}
