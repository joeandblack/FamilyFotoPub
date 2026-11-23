"""Explicit bounded import review, one durable photo receipt at a time."""
import json
import time
from face_detect.jobs import automatic_people_jobs as workers
from app import database
from face_detect.recognition import people
from app.telemetry import write_json


class ImportJobs:
    import_scoped=True

    def __init__(self, raw):
        self.raw=raw

    def __getattr__(self,name):return getattr(self.raw,name)

    @property
    def observer(self):return self.raw.observer

    @observer.setter
    def observer(self,value):self.raw.observer=value

    def path(self,name):return self.job(name).directory/'import.json'

    def create(self,name,ids,user):
        workers.authorize(user)
        if not ids or any(type(mid) is not int or mid<=0 for mid in ids) or len(set(ids))!=len(ids):raise ValueError('Select unique positive photo IDs')
        with database.connection() as db:
            people._lock(db)
            if hasattr(self.raw,'validate_selection'):self.raw.validate_selection(db,name,ids)
            else:
                for mid in ids:self.source(db,mid)
            self.job(name).directory.mkdir(parents=True,exist_ok=False)
            write_json(self.path(name),{'state':'queued','actor':user['username'],'run_seconds':0,'items':[
                {'media_id':mid,'child':f'{name[:35]}-photo-{mid}','state':'pending'} for mid in ids]})
            self.job(name).command('run')
        return self.status(name,user)

    def status(self,name,user):
        if not self.path(name).exists():return self.raw.status(name,user)
        workers.authorize(user,False)
        state=json.loads(self.path(name).read_text())
        command=json.loads(self.job(name).control.read_text())['action']
        phase=state['state']
        if phase!='completed' and command!='run':phase='paused' if command=='pause' else 'cancelled'
        return {'id':name,'state':phase,'unit':'photos','total':len(state['items']),
                'run_seconds':None if state.get('timing_partial') else state.get('run_seconds'),
                'done':sum(i['state']=='done' for i in state['items']),
                'excluded':sum(i['state']=='excluded' for i in state['items']),
                'errors':[{'media_id':i['media_id'],'error':i['error'],
                           **({'detail':i['error_detail']} if i.get('error_detail') else {})}
                          for i in state['items'] if i.get('error')],
                'current_media_id':state.get('current_media_id')}

    def control(self,name,action,user):
        if not self.path(name).exists():return self.raw.control(name,action,user)
        workers.authorize(user)
        if action not in ('pause','resume','cancel'):raise ValueError('Unknown control')
        with database.connection() as db:
            people._lock(db)
            if self.status(name,user)['state'] in ('completed','cancelled'):raise RuntimeError('Job is terminal')
            if action=='resume':
                state=json.loads(self.path(name).read_text())
                # Service shutdown pauses both the import and its photo jobs.
                for item in state['items']:
                    if item['state'] in ('done','excluded'):continue
                    child=self.raw.job(item['child'])
                    if child.control.exists() and json.loads(child.control.read_text()).get('action')=='pause':
                        child.command('run')
            self.job(name).command('run' if action=='resume' else action)
        return self.status(name,user)

    def run(self,name,user,max_items=None):
        if not self.path(name).exists():return self.raw.run(name,user,max_items or 1)
        workers.authorize(user)
        state=json.loads(self.path(name).read_text());processed=0
        if state['state']=='completed':return self.status(name,user)
        base=state.get('run_seconds',0);started=None
        if 'run_seconds' not in state:state['timing_partial']=True
        def save():
            if started is not None:state['run_seconds']=base+max(0,time.monotonic()-started)
            write_json(self.path(name),state)
        try:
            with workers.worker_lease(maintenance=False):
                for item in state['items']:
                    if item['state'] in ('done','excluded'):continue
                    if json.loads(self.job(name).control.read_text())['action']!='run':break
                    if max_items is not None and processed>=max_items:state['state']='paused';break
                    child=item['child']
                    result=None
                    while result is None or result['state'] in ('awaiting-receipt','waiting-for-foreground'):
                        if json.loads(self.job(name).control.read_text())['action']!='run':
                            return self.status(name,user)
                        # Never sleep with a photo/content/maintenance reservation held.
                        with workers.background_unit() as lease:
                            if lease is not None:
                                started=time.monotonic()
                                state.update(state='running',current_media_id=item['media_id']);save()
                                try:
                                    if not (self.raw.job(child).directory/'raw.json').exists():
                                        from face_detect.jobs.checkpoint import Excluded
                                        try:self.raw.create(child,[item['media_id']],user)
                                        except Excluded:
                                            item['state']='excluded'
                                            result={'state':'completed','excluded':1}
                                    if result is None or result['state']!='completed':
                                        result=self.raw.run(child,user,max_items=1,lease=lease)
                                finally:
                                    save();base=state['run_seconds'];started=None
                        if result is not None and result['state'] not in ('awaiting-receipt','waiting-for-foreground'):break
                        state['state']='waiting-for-foreground';save()
                        time.sleep(1)
                    if result['state']!='completed':
                        state['state']=result['state'];save();return self.status(name,user)
                    item['state']='excluded' if result.get('excluded') else 'done'
                    if result.get('errors'):
                        warning=result['errors'][0]
                        item['error']=warning['error']
                        if warning.get('detail'):item['error_detail']=warning['detail']
                    processed+=1;save()
                if all(i['state'] in ('done','excluded') for i in state['items']):state['state']='completed'
                elif state['state'] in ('running','waiting-for-foreground'):state['state']='paused'
                state['current_media_id']=None;save()
        except Exception:
            state['state']='blocked';save();raise
        return self.status(name,user)

    def recheck(self,name,revision,user,progress=None,import_id=None):
        workers.authorize(user)
        progress=progress or (lambda *args: None)
        if name!='completed-imports' and not self.path(name).exists():raise RuntimeError('Open a completed import review; older preview jobs cannot be rechecked')
        with workers.manual_lease(progress):
            with database.connection() as db:
                people._lock(db)
                # Recheck chooses current eligible unnamed faces, not a client selection.
                progress('comparing',0,None)
                if import_id is not None:
                    from face_detect.recognition.people_review_scope import media_ids as scoped_media
                    media_ids=sorted(scoped_media(db,import_id))
                elif name=='completed-imports':
                    paths=[p for p in self.raw.root.glob('import-*/import.json') if p.parent.name.removeprefix('import-').isdigit()]
                    states=[json.loads(p.read_text()) for p in paths if self.status(p.parent.name,user)['state']=='completed']
                    if not states:raise RuntimeError('Finish Discover people for at least one import first')
                    media_ids=sorted({i['media_id'] for state in states for i in state['items'] if i['state']=='done'})
                else:
                    if self.status(name,user)['state']!='completed':raise RuntimeError('Finish Discover people first')
                    state=json.loads(self.path(name).read_text())
                    media_ids=[i['media_id'] for i in state['items']]
                from face_detect.recognition.automatic_people import recheck_unnamed
                from face_detect.recognition.people_relaxed import generate
                result=recheck_unnamed(db,media_ids,user['username'])
                return result|generate(db,media_ids,user['username'],import_id=import_id)
