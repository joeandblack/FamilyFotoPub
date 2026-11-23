"""Versioned, provider-neutral face geometry policy."""
import math

def quality_seed(result):
    """Frozen geometry heuristic, not a calibrated recognition confidence."""
    b=result['box'];p=result['landmarks'];eye=math.dist(p[0],p[1])
    ex,ey=p[1][0]-p[0][0],p[1][1]-p[0][1]
    mx,my=(p[3][0]+p[4][0]-p[0][0]-p[1][0])/2,(p[3][1]+p[4][1]-p[0][1]-p[1][1])/2
    return (not result.get('warnings') and b['probability']>=.95
            and min(b['x_max']-b['x_min'],b['y_max']-b['y_min'])>=64
            and eye>=20 and ex*my-ey*mx>eye*2 and math.dist(p[3],p[4])>=2)
