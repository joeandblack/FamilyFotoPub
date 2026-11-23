"""Provider-neutral errors and result validation; no ML imports."""
import math
from typing import Protocol

class ProviderUnavailable(RuntimeError): pass
class ProviderTransient(ProviderUnavailable): pass
class ConfigurationError(ValueError): pass

class Provider(Protocol):
    def analyze(self, image, include_embeddings=True): ...
    def close(self): ...


def validate_faces(faces, size, dimensions, include_embeddings=True):
    width,height=size
    if not isinstance(faces,list) or len(faces)>100:
        raise ProviderUnavailable('Provider must return at most 100 detections')
    output=[]
    for face in faces:
        box=dict(face['box'])
        if not all(type(box.get(k)) in (int,float) and math.isfinite(box[k]) for k in ('x_min','y_min','x_max','y_max','probability')):
            raise ProviderUnavailable('Invalid face box or score')
        if not (0<=box['x_min']<box['x_max']<=width and 0<=box['y_min']<box['y_max']<=height and 0<=box['probability']<=1):
            raise ProviderUnavailable('Face coordinates outside canonical input')
        points=face.get('landmarks')
        if include_embeddings and points is None:raise ProviderUnavailable('Recognition requires landmarks')
        if points is not None and (len(points)!=5 or any(len(p)!=2 or not all(math.isfinite(float(v)) for v in p) for p in points)):
            raise ProviderUnavailable('Expected five finite ordered landmarks')
        row={'box':box,'landmarks':points,'warnings':list(face.get('warnings',[]))}
        if include_embeddings:
            vector=face.get('vector')
            if not isinstance(vector,(list,tuple)) or len(vector)!=dimensions or not all(math.isfinite(float(v)) for v in vector):
                raise ProviderUnavailable('Invalid embedding dimension or values')
            norm=math.sqrt(sum(float(v)**2 for v in vector))
            if norm<=1e-12:raise ProviderUnavailable('Zero embedding')
            row['vector']=[float(v)/norm for v in vector]
        output.append(row)
    return output


def _number(value):
    return type(value) in (int,float) and math.isfinite(value)


def validate_geometry(geometry,size,kind):
    """Stored face geometry for embedding: only the kind the space aligns by, in pixels.
    Landmarks of a face at the edge may fall a little outside the photo, as detectors give them."""
    width,height=size
    if not isinstance(geometry,list) or len(geometry)>100:
        raise ProviderUnavailable('Embed at most 100 faces per request')
    output=[]
    for item in geometry:
        if not isinstance(item,dict) or set(item)!={kind}:
            raise ProviderUnavailable(f'This space embeds from {kind} geometry only')
        if kind=='landmarks':
            points=item['landmarks']
            if (not isinstance(points,(list,tuple)) or len(points)!=5
                    or any(not isinstance(p,(list,tuple)) or len(p)!=2 or not all(_number(v) for v in p) for p in points)
                    or any(not -width<=x<=2*width or not -height<=y<=2*height for x,y in points)):
                raise ProviderUnavailable('Expected five finite ordered landmarks near the photo')
            output.append({'landmarks':[[float(x),float(y)] for x,y in points]})
        else:
            box=item['box']
            if (not isinstance(box,(list,tuple)) or len(box)!=4 or not all(_number(v) for v in box)
                    or not (0<=box[0]<box[2]<=width and 0<=box[1]<box[3]<=height)):
                raise ProviderUnavailable('Face box outside canonical input')
            output.append({'box':[float(v) for v in box]})
    return output


def validate_vectors(vectors,count,dimensions):
    if not isinstance(vectors,list) or len(vectors)!=count:
        raise ProviderUnavailable('Provider returned the wrong number of vectors')
    output=[]
    for vector in vectors:
        if not isinstance(vector,(list,tuple)) or len(vector)!=dimensions or not all(_number(float(v)) for v in vector):
            raise ProviderUnavailable('Invalid embedding dimension or values')
        norm=math.sqrt(sum(float(v)**2 for v in vector))
        if norm<=1e-12:raise ProviderUnavailable('Zero embedding')
        output.append([float(v)/norm for v in vector])
    return output
