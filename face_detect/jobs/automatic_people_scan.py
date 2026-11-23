"""Raw-media stage feeding the existing versioned receipt contract.

No directory enumeration: server-selected catalog IDs, at most ten images per job.
The provider detects and returns embeddings in one request. Detection IDs and their
evidence commit together, so process recovery never replaces reviewed/ignored faces.
"""
import hashlib
import io
import json
from contextlib import nullcontext
from pathlib import Path
from PIL import Image, ImageCms
from psycopg.types.json import Jsonb
from face_detect.jobs import automatic_people_jobs as workers
from app import database
from face_detect.recognition import people
from face_detect.jobs.automatic_people_handoff import accept_checkpoint
from face_detect.jobs.automatic_people_handoff import canonical_hash
from face_detect.recognition.faces import VISIBLE
from app.library.media import safe_media_path
from app.library.locks import content_key
from app.library.locks import MEDIA_LOCK_NAMESPACE
from app.telemetry import write_json
from face_detect.recognition.quality import quality_seed
from face_detect.contracts import ProviderTransient
from face_detect.catalog.spaces import register_space
from face_detect.jobs.checkpoint import same_space
from face_detect.catalog.evidence import compact_items, expand_items


class DecodeRejected(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.detail = detail


def decode_canonical(path):
    from face_detect.imaging.face_decode import decode_primary
    try:
        image,width,height=decode_primary(path,max_long_edge=4096)
        return image,(width,height)
    # PyCMSError (an embedded color profile LittleCMS cannot transform) derives only from
    # Exception; uncaught, one such photo stops the whole Discover people job.
    except (ValueError,OSError,Image.DecompressionBombError,ImageCms.PyCMSError) as error:
        message=str(error).lower()
        code='unsupported_media' if any(s in message for s in ('not supported','multiframe','ambiguous mpo')) else 'corrupt_or_unsafe_media'
        from face_detect.recognition.people_scan_warnings import decode_detail
        raise DecodeRejected(code, decode_detail(error)) from None
from face_detect.jobs.checkpoint import Excluded  # noqa: E402


class RawJobs(workers.Jobs):
    def __init__(self, root, source_root, provider, observer, verify, decoder=None, quality_filter=None,
                 thumbnails=None):
        super().__init__(root, provider, observer, None)
        self.source_root, self.verify = source_root, verify
        # The installation keeps face thumbnails in data/people/thumbnails; a test, beside its jobs.
        self.thumbnail_dir=Path(thumbnails) if thumbnails is not None else self.root.parent/'thumbnails'
        self.decoder=decoder
        self.quality_filter=quality_filter
        if decoder is not None:
            from face_detect.configuration import PREPROCESSING
            if provider.manifest['preprocessing'] != PREPROCESSING:raise ValueError('Canonical decoder requires separate model space')

    def source(self, db, mid, expected=None, lock=False):
        row=db.execute('SELECT m.id,m.sha256,m.relative_path FROM media m WHERE m.id=%s '
                       "AND m.kind='image' AND "+VISIBLE+(' FOR UPDATE OF m' if lock else ''),(mid,)).fetchone()
        if not row or (expected is not None and row['sha256']!=expected):
            raise Excluded('Source no longer eligible')
        return row

    def create(self, name, ids, user):
        workers.authorize(user)
        if not 1<=len(ids)<=10 or any(type(i) is not int or i<=0 for i in ids) or len(set(ids))!=len(ids):
            raise ValueError('Select 1..10 unique media IDs')
        job=self.job(name)
        with database.connection() as db:
            people._lock(db)
            rows=[self.source(db,i) for i in ids]
            job.directory.mkdir(parents=True,exist_ok=False)
            write_json(job.directory/'raw.json',{'model_id':self.provider.identity,'space_id':self.provider.space_id,'actor':user['username'],
                'state':'queued','items':[{'media_id':r['id'],'content_hash':r['sha256'],'state':'pending','attempts':0} for r in rows]})
            job.command('run')
        return self.status(name,user)

    def status(self, name, user):
        workers.authorize(user,False);job=self.job(name)
        raw=json.loads((job.directory/'raw.json').read_text())
        errors=[{'media_id':i['media_id'],'error':i['error'],
                 **({'detail':i['error_detail']} if i.get('error_detail') else {})}
                for i in raw['items'] if i.get('error')]
        if job.path.exists():
            status=super().status(name,user)
            if status['state']=='awaiting-receipt' and raw['state'] in ('blocked','waiting-service','waiting-for-foreground'):
                status['state']=raw['state']
            return status|{'errors':errors}
        control=json.loads(job.control.read_text())['action']
        stage=raw['state']
        if control=='cancel':stage='cancelled'
        elif control=='pause':stage='paused'
        return {'id':name,'state':stage,'total':len(raw['items']),
                'done':sum(i['state']=='done' for i in raw['items']),
                'excluded':sum(i['state']=='excluded' for i in raw['items']),
                'receipt':None,'unit':'photos','errors':errors}

    def run(self, name, user, max_items=10, lease=None):
        workers.authorize(user);job=self.job(name)
        with (workers.worker_lease() if lease is None else nullcontext(lease)) as lease:
            if self.status(name,user)['state'] in ('completed','cancelled'):return self.status(name,user)
            raw=json.loads((job.directory/'raw.json').read_text())
            # A job pins a space, not a computer: any backend of that space may carry it on.
            if not same_space(raw,self.provider):
                raw['state']='blocked';write_json(job.directory/'raw.json',raw)
                raise ValueError('Checkpoint model differs')
            try:
                if not job.path.exists():
                    processed=0
                    for item in raw['items']:
                        if json.loads(job.control.read_text())['action']!='run':break
                        if workers.foreground_busy(lease):
                            raw['state']='waiting-for-foreground';break
                        if item['state'] in ('done','excluded','error'):continue
                        if processed>=max_items:raw['state']='paused';break
                        try:
                            item['state']='running';raw['state']='running'
                            if item['attempts']>=3:raise ValueError('Retry budget exhausted')
                            item['attempts']+=1;write_json(job.directory/'raw.json',raw)
                            item['items']=self.scan(lease,item,job)
                            item['state']='done';processed+=1
                        except DecodeRejected as error:
                            item.update(state='error',error=str(error))
                            if error.detail:item['error_detail']=error.detail
                        except Excluded:item.update(state='excluded',error='source_ineligible_or_changed')
                        write_json(job.directory/'raw.json',raw)
                    if all(i['state'] in ('done','excluded','error') for i in raw['items']):
                        records=[r for i in raw['items'] if i['state']=='done' for r in i['items']]
                        if len(records)>100:raise ValueError('Bounded face cohort exceeded')
                        if records:
                            write_json(job.path,{'schema':'familyfoto-face-job-v1','model_id':self.provider.identity,'space_id':self.provider.space_id,
                                'manifest_sha256':canonical_hash([r['input'] for r in records]),'state':'completed','items':records})
                        else:raw['state']='completed'
                if job.path.exists():
                    with database.connection() as db:
                        people._lock(db)
                        if json.loads(job.control.read_text())['action']=='run' and not workers.foreground_busy(lease):
                            # A completed checkpoint may wait across a process death
                            # or pause. Recheck actual original bytes, not only the
                            # catalog hash, before its first receipt commit.
                            for item in raw['items']:
                                if item['state']!='done':continue
                                try:row=self.source(db,item['media_id'],item['content_hash'],lock=True)
                                except Excluded:continue # receipt eligibility excludes it
                                if not lease.execute('SELECT pg_try_advisory_lock_shared(%s,%s) ok',
                                    (MEDIA_LOCK_NAMESPACE,content_key(row['sha256']))).fetchone()['ok']:
                                    raise ProviderTransient('Source reserved before receipt')
                                path=safe_media_path(str(self.source_root),row['relative_path'])
                                if path.stat().st_size>128*1024*1024:raise ValueError('Source changed before receipt')
                                with path.open('rb') as stream:
                                    if hashlib.file_digest(stream,'sha256').hexdigest()!=row['sha256']:
                                        raise ValueError('Source changed before receipt')
                            if workers.foreground_busy(lease):raise ProviderTransient('Foreground work active')
                            accept_checkpoint(db,job.read(),user,self.observer,provider=self.provider)
                        elif json.loads(job.control.read_text())['action']=='run':
                            raw['state']='waiting-for-foreground'
            except ProviderTransient as error:
                raw['state']='waiting-for-foreground' if str(error)=='Foreground work active' else 'waiting-service'
                raw['wait_reason']=str(error)
            except (ValueError,RuntimeError,OSError):
                raw['state']='blocked';write_json(job.directory/'raw.json',raw);raise
            write_json(job.directory/'raw.json',raw)
        return self.status(name,user)

    def scan(self, lease, item, job):
        with database.connection() as db:row=self.source(db,item['media_id'],item['content_hash'])
        path=safe_media_path(str(self.source_root),row['relative_path'])
        # Read-only access. Server catalog derives every path; HTTP never accepts one.
        if path.stat().st_size>128*1024*1024:raise Excluded('Source exceeds byte bound')
        if not lease.execute('SELECT pg_try_advisory_lock_shared(%s,%s) ok',
            (MEDIA_LOCK_NAMESPACE,content_key(row['sha256']))).fetchone()['ok']:
            raise ProviderTransient('Source reserved')
        payload=path.read_bytes()
        if hashlib.sha256(payload).hexdigest()!=row['sha256']:raise ValueError('Source checksum changed')
        with database.connection() as db:
            cached=db.execute('SELECT s.items FROM auto_people_scan_results s JOIN face_analyses a ON a.id=s.analysis_id '
                              'WHERE a.media_id=%s AND a.content_hash=%s AND a.detector_id=%s',
                              (row['id'],row['sha256'],self.provider.detector_id)).fetchone()
            if cached:
                cached['items'] = expand_items(db, cached['items'])
        if self.decoder:
            image,canonical_size=self.decoder(path)
        else:
          with Image.open(io.BytesIO(payload)) as decoded:
            # This disabled slice has only been validated on plain synthetic RGB
            # PNGs. Do not silently invent orientation/color/primary-frame policy
            # or change the frozen provider preprocessing space for real imports.
            if decoded.format!='PNG' or decoded.mode!='RGB' or decoded.info.get('icc_profile') or decoded.getexif() or getattr(decoded,'n_frames',1)!=1:
                raise ValueError('Raw slice currently requires plain single-frame RGB PNG')
            if max(decoded.size)>6400 or decoded.width*decoded.height>24_000_000:raise Excluded('Source exceeds pixel bound')
            image=decoded.convert('RGB')
          canonical_size=image.size
        if cached:
            self.thumbnails(image,cached['items'])
            return cached['items']
        before=self.observer();self.verify(before);self.provider.validate(before['stamp'])
        results=self.provider.detect(image)
        after=self.observer();self.verify(after)
        if before!=after:raise ValueError('Service changed during inference')
        if len(results)>100:raise ValueError('At most 100 faces per photo in this bounded review')
        if hashlib.sha256(path.read_bytes()).hexdigest()!=row['sha256']:raise ValueError('Source checksum changed')
        with database.connection() as db:
            people._lock(db);self.source(db,row['id'],row['sha256'],lock=True)
            if json.loads(job.control.read_text())['action']=='cancel':raise Excluded('Job cancelled')
            # Never silently replace another analysis/model or its manual decisions.
            if db.execute('SELECT 1 FROM face_analyses WHERE media_id=%s AND content_hash=%s',
                          (row['id'],row['sha256'])).fetchone():raise ValueError('Existing analysis requires explicit reconciliation')
            register_space(db,self.provider)
            aid=db.execute('INSERT INTO face_analyses(media_id,content_hash,detector_id,width,height,outcome) '
                "VALUES(%s,%s,%s,%s,%s,'done') RETURNING id",
                (row['id'],row['sha256'],self.provider.detector_id,*canonical_size)).fetchone()['id']
            items=[]
            for face in results:
                result=face|{'seed':bool(quality_seed(face)),'quality_policy':'geometry-seed-v1','source_size':list(image.size),'canonical_size':list(canonical_size)}
                quality=self.quality_filter(image,face['box']) if self.quality_filter else None
                if quality is not None:result['face_quality']=quality
                b=face['box'];box=[b[k] for k in ('x_min','y_min','x_max','y_max')]
                did=db.execute('INSERT INTO face_detections(analysis_id,box,landmarks,landmark_schema,score) '
                    "VALUES(%s,%s,%s,'five-point-v1',%s) RETURNING id",
                    (aid,Jsonb({'x':box[0]/image.width,'y':box[1]/image.height,'width':(box[2]-box[0])/image.width,'height':(box[3]-box[1])/image.height}),
                     Jsonb([[x/image.width,y/image.height] for x,y in face['landmarks']]),b['probability'])).fetchone()['id']
                if quality and quality['reason']:
                    db.execute("INSERT INTO auto_people_face_quality(detection_id,policy,metrics,state) VALUES(%s,%s,%s,'blurry')",
                               (did,quality['policy'],Jsonb(quality)))
                items.append({'input':{'id':did,'media_id':row['id'],'content_hash':row['sha256'],
                    'path':'catalog:'+str(row['id']),'sha256':row['sha256'],'target':box},
                    'state':'done','attempts':1,'vector':face['vector'],'result':result,'service_evidence':after})
            db.execute('INSERT INTO auto_people_scan_results VALUES(%s,%s)',(aid,Jsonb(compact_items(db, items))))
        self.thumbnails(image,items)
        return items

    def thumbnails(self,image,items):
        directory=self.thumbnail_dir;directory.mkdir(parents=True,exist_ok=True)
        for item in items:
            x1,y1,x2,y2=item['input']['target'];pad=max(x2-x1,y2-y1)*.15
            crop=image.crop((max(0,int(x1-pad)),max(0,int(y1-pad)),min(image.width,int(x2+pad)),min(image.height,int(y2+pad))))
            crop.thumbnail((160,160));path=directory/(str(item['input']['id'])+'.jpg')
            temporary=path.with_suffix('.tmp');crop.save(temporary,format='JPEG',quality=85);temporary.replace(path)
