"""Private durable evaluation jobs; reuse application atomic checkpoints and leases.

Production state is never written. Callers supply the existing compute/content
reservation context, eligibility loader and independently observed service stamp.
"""
import hashlib
import json
import time
from app.telemetry import write_json
from face_detect.contracts import ProviderTransient
from face_detect.contracts import ProviderUnavailable


class Excluded(ValueError):pass
class IdentityMismatch(ValueError):pass


def same_space(state, provider):
    """A job pins a space, not a computer. One saved before spaces names only the exact model,
    and resumes only on it."""
    if state.get('space_id') is not None:
        return state['space_id'] == getattr(provider, 'space_id', None)
    return state.get('model_id') == provider.identity


class EvaluationJob:
    def __init__(self, directory):
        self.directory=directory
        self.path=directory/'checkpoint.json'
        self.control=directory/'control.json'

    def create(self, items, model_id, schema='familyfoto-face-job-v1', space_id=None):
        if not 1<=len(items)<=100 or len({r['id'] for r in items})!=len(items):
            raise ValueError('Require 1..100 unique work items')
        for row in items:
            if set(row)!={'id','media_id','content_hash','path','sha256','target'}:
                raise ValueError('Unexpected manifest fields')
            if type(row['id']) is not int or type(row['media_id']) is not int:
                raise ValueError('Integer source IDs required')
        self.directory.mkdir(parents=True,exist_ok=False)
        manifest_hash=hashlib.sha256(json.dumps(items,sort_keys=True).encode()).hexdigest()
        state={'schema':schema,'model_id':model_id,'space_id':space_id,'manifest_sha256':manifest_hash,
               'state':'queued','items':[{'input':r,'state':'pending','attempts':0} for r in items]}
        write_json(self.path,state);self.command('run')

    def command(self, action):
        if action not in ('run','pause','cancel'):raise ValueError('Unknown control')
        write_json(self.control,{'action':action})

    def read(self):return json.loads(self.path.read_text(encoding='utf-8'))

    def run(self, provider, observe, load, lease, max_items=100, seconds=300):
        if not 1<=max_items<=100 or not 1<=seconds<=600:raise ValueError('Invalid run budget')
        with lease():
            state=self.read()
            if state['state']=='cancelled':return state
            if not same_space(state,provider):raise IdentityMismatch('Checkpoint model differs')
            original=[r['input'] for r in state['items']]
            if hashlib.sha256(json.dumps(original,sort_keys=True).encode()).hexdigest()!=state['manifest_sha256']:
                raise IdentityMismatch('Checkpoint manifest changed')
            start=time.monotonic();processed=0
            for item in state['items']:
                action=json.loads(self.control.read_text())['action']
                if action not in ('run','pause','cancel'):raise ValueError('Invalid persisted control')
                if action!='run':
                    state['state']='cancelled' if action=='cancel' else 'paused';break
                if processed>=max_items or time.monotonic()-start>=seconds:
                    state['state']='paused';break
                if item['state'] in ('excluded','failed'):
                    if item['state']=='failed':state['state']='blocked';break
                    continue
                # Revalidate even completed items on resume; do not re-embed them.
                try:
                    with load(item['input']) as (image,target):
                        if item['state']=='done':continue
                        if item['attempts']>=3:
                            item['state']='failed';state['state']='blocked';break
                        before=observe();provider.validate(before['stamp'])
                        item['state']='running';item['attempts']+=1;state['state']='running'
                        write_json(self.path,state)
                        result=provider.embed_result(image,target) if hasattr(provider,'embed_result') else None
                        vector=result['vector'] if result is not None else provider.embed(image,target)
                        after=observe()
                        if before!=after:raise IdentityMismatch('Service changed during request')
                    # Context exit rechecks source eligibility before checkpoint commit.
                    action=json.loads(self.control.read_text())['action']
                    if action=='cancel':
                        item['state']='pending';state['state']='cancelled';break
                    item.update(state='done',vector=vector,service_evidence=after)
                    if result is not None:item['result']=result
                    item.pop('error',None);processed+=1
                    write_json(self.path,state)
                except Excluded:
                    item.update(state='excluded',error='source_ineligible');item.pop('vector',None);item.pop('result',None)
                    write_json(self.path,state)
                except ProviderTransient:
                    item.update(state='pending',error='transient_service');state['state']='waiting-service';break
                except (IdentityMismatch,ProviderUnavailable,ValueError):
                    item.update(state='failed',error='identity_or_result_validation');state['state']='blocked';break
            else:state['state']='completed'
            write_json(self.path,state)
            return state
