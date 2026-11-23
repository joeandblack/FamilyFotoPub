"""Internal handoff: verified completed native checkpoint -> receipt.

No route/CLI registration and no second queue. Only trusted local job artifacts are
inputs; this is not a signature scheme for arbitrary uploaded checkpoint JSON.
"""
import hashlib
import json
import math
from face_detect.recognition import automatic_people as service
from face_detect.recognition import people
from face_detect.recognition.quality import quality_seed
from face_detect.recognition.grouping import unit
from face_detect.jobs.checkpoint import same_space


def evidence_space(record):
    """The space a saved service observation belongs to."""
    from face_detect.contracts import ConfigurationError
    from face_detect.spaces.identity import space_id
    try:
        return space_id(record['evidence']['manifest'])
    except (ConfigurationError, KeyError, TypeError):
        return None


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,allow_nan=False).encode()).hexdigest()


def receipt_id(checkpoint):
    return 'eval-'+canonical_hash({'manifest':checkpoint['manifest_sha256'],'model':checkpoint['model_id']})


def accept_checkpoint(db,checkpoint,user,observer=None,provider=None):
    service.guard()
    if not user or user.get('role')!='admin' or not user.get('username'):
        raise PermissionError('Administrator authorization required')
    if checkpoint.get('schema') != 'familyfoto-face-job-v1' or checkpoint.get('state')!='completed':
        raise ValueError('Only completed versioned jobs can be accepted')
    items=checkpoint['items']
    if not 1<=len(items)<=100 or len({r['input']['id'] for r in items})!=len(items):raise ValueError('Bounded unique inputs required')
    if canonical_hash([r['input'] for r in items])!=checkpoint['manifest_sha256']:raise ValueError('Input manifest changed')
    if provider is None or not same_space(checkpoint, provider):
        raise ValueError('Pinned native provider required for checkpoint')
    def verify(record):
        provider.verify(record)
        provider.validate(record['stamp'])
    checked={}
    def verify_saved(record):
        # Work saved by another backend of the same space is accepted only if that backend
        # passed the library check too (decision 5); the space alone isn't enough.
        if record == provider.observe():return verify(record)
        if provider.space_id is None or evidence_space(record) != provider.space_id:
            raise ValueError('Checkpoint evidence belongs to another face model')
        from face_detect.catalog.certify import may_write
        from face_detect.spaces.identity import backend, backend_id
        value=backend(record['evidence']['manifest']);key=backend_id(value)
        if key not in checked:checked[key]=may_write(db,provider.space_id,value)
        if not checked[key]:
            raise ValueError('Checkpoint was made by a backend not checked against this library')
    observer = observer or provider.observe
    current=observer();verify(current)
    people._lock(db);people._lock_media(db,[r['input']['id'] for r in items])
    registered=db.execute("SELECT state FROM space WHERE space_id=%s",(provider.space_id,)).fetchone()
    if not registered or registered['state']=='retired':raise ValueError('Registered provider manifest differs')
    valid={r['id']:r for r in service.rows(db,[r['input']['id'] for r in items])}
    observations=[];provenance=[]
    for item in items:
        if item['state']=='excluded':continue
        if item['state']!='done':raise ValueError('Incomplete item in completed job')
        inp=item['input'];meta=valid.get(inp['id'])
        if meta and (meta['media_id']!=inp['media_id'] or meta['content_hash']!=inp['content_hash']):
            raise ValueError('Source identity changed')
        verify_saved(item['service_evidence'])
        result=item.get('result')
        produced=result.get('space_id') if result else None
        if not result or (produced!=provider.space_id if produced is not None else result.get('model_id')!=provider.identity) or result.get('quality_policy')!='geometry-seed-v1':
            raise ValueError('Checkpoint lacks versioned quality provenance; do not guess seeds')
        if result['vector']!=item['vector']:raise ValueError('Conflicting checkpoint vectors')
        _,norm=unit(result['vector'],512)
        if abs(norm-1)>.001:raise ValueError('Normalized embeddings required')
        if len(result['landmarks'])!=5 or any(len(p)!=2 or not all(math.isfinite(v) for v in p) for p in result['landmarks']):raise ValueError('Invalid stored landmarks')
        b=result['box']
        if any(not math.isfinite(b[k]) for k in ('x_min','y_min','x_max','y_max','probability')) or not (0<=b['x_min']<b['x_max'] and 0<=b['y_min']<b['y_max'] and 0<=b['probability']<=1):raise ValueError('Invalid stored box')
        size=result.get('source_size',[])
        # Older immutable checkpoints can exceed an edge by floating-point roundoff.
        # Accept only sub-nanopixel error without rewriting their receipt provenance.
        if len(size)!=2 or any(type(v) is not int or not 0<v<=6400 for v in size) or size[0]*size[1]>24_000_000 or b['x_max']-size[0]>1e-9 or b['y_max']-size[1]>1e-9:raise ValueError('Stored box outside source bounds')
        if result['seed'] is not bool(quality_seed(result)):raise ValueError('Quality metadata changed')
        observations.append({'detection_id':inp['id'],'vector':result['vector'],'seed':result['seed']})
        provenance.append({'detection_id':inp['id'],'result_sha256':canonical_hash(result),'service':item['service_evidence']})
    # A restart of identical pinned assets since inference is allowed. A change during
    # this handoff is not; fresh runtime observation never changes receipt identity.
    after=observer();verify(after)
    if after!=current:raise ValueError('Service changed during handoff')
    receipt=receipt_id(checkpoint)
    return service.ingest(db,receipt,provider.space_id,observations,user['username'],
                          provenance={'manifest':checkpoint['manifest_sha256'],'items':provenance})
