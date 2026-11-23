"""Read confirmed identities; save a local held-out evaluation. Never changes labels."""
import hashlib
import json
from datetime import datetime, timezone
import uuid
from app.config import DATA,load_config
from app import database
from app.telemetry import write_json
from face_detect.recognition.calibration import calibrate,native_seed,capture_event_id,identity_map
from face_detect.recognition import automatic_people as auto

def collect():
    settings=json.loads((DATA/'people-preview.json').read_text());root=DATA/'face_detect/profiles'/settings['pilot']
    cfg=load_config()
    from face_detect import catalog
    from face_detect.catalog.spaces import active_space
    with database.connection(catalog.database(cfg)) as db:
        db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        eligible={r['id'] for r in auto.rows(db)}
        active=active_space(db)
        rows=db.execute('''SELECT d.id,coalesce(pa.person_id,g.person_id) person_id,a.media_id photo_id,m.sha256 content_id,
            m.captured_at,e.space_id,e.vector,s.items FROM auto_people_members gm JOIN auto_people_groups g ON g.id=gm.group_id
            JOIN face_detections d ON d.id=gm.detection_id JOIN face_analyses a ON a.id=d.analysis_id JOIN media m ON m.id=a.media_id
            LEFT JOIN people_assignments pa ON pa.detection_id=d.id JOIN embedding e ON e.detection_id=d.id AND e.space_id=gm.space_id
            JOIN people p ON p.id=coalesce(pa.person_id,g.person_id) AND p.deleted_at IS NULL
            LEFT JOIN auto_people_scan_results s ON s.analysis_id=a.id
            WHERE e.space_id=%s AND coalesce(pa.person_id,g.person_id) IS NOT NULL
            AND (pa.person_id IS NOT NULL OR gm.origin IN ('manual','named_seed'))
            ORDER BY d.id''',(active,)).fetchall()
        from face_detect.catalog.evidence import reference_records
        transferred={r['detection_id']:r['result']['face'] for r in reference_records(db, active)}
        revision=db.execute('SELECT revision FROM people_revision').fetchone()['revision']
    with database.connection(cfg['database']) as db:
        db.execute('SET TRANSACTION READ ONLY')
        from face_detect.recognition.faces import VISIBLE
        live={r['id']:r['sha256'] for r in db.execute('SELECT m.id,m.sha256 FROM media m WHERE m.id=ANY(%s) AND '+VISIBLE,([r['photo_id'] for r in rows],)).fetchall()}
    inputs=[]
    for row in rows:
        if row['id'] not in eligible or live.get(row['photo_id'])!=row['content_id']:continue
        results=row.pop('items') or []
        result=transferred.get(row['id']) or next((i['result'] for i in results if i['input']['id']==row['id']),None)
        if result is None:continue
        row['event_id']=capture_event_id(row.pop('captured_at'));row['seed']=bool(native_seed(result));inputs.append(row)
    return root,inputs,revision


def read_equivalences(root):
    path=root/'calibration/identity-equivalences.json'
    if not path.exists():return []
    document=json.loads(path.read_text(encoding='utf-8'))
    runtime=json.loads((root/'runtime.json').read_text(encoding='utf-8'))
    # Saved before spaces, the file names the exact model; now, the space.
    names={runtime.get('model_id'),runtime.get('space_id')}-{None}
    if document.get('schema')!='familyfoto-calibration-identities-v1' or not ({document.get('model_id'),document.get('space_id')}&names):
        raise ValueError('Calibration equivalences belong to a different model or format')
    groups=document['groups']
    identity_map(groups)
    return groups


def save_equivalences(groups):
    """Persist user-confirmed evaluation labels locally; never merge catalog people."""
    settings=json.loads((DATA/'people-preview.json').read_text())
    root=DATA/'face_detect/profiles'/settings['pilot']
    runtime=json.loads((root/'runtime.json').read_text())
    identity_map(groups)
    existing=read_equivalences(root)
    # An explicitly supplied full superset can extend a confirmed identity.
    # Partial overlaps still fail rather than implicitly merging identities.
    retained=[old for old in existing if not any(set(old)<=set(new) for new in groups)]
    groups=sorted({tuple(sorted(group)) for group in [*retained,*groups]})
    ids=identity_map(groups)
    cfg=load_config()
    from face_detect import catalog
    with database.connection(catalog.database(cfg)) as db:
        db.execute('SET TRANSACTION READ ONLY')
        found={r['id'] for r in db.execute('SELECT id FROM people WHERE id=ANY(%s) AND deleted_at IS NULL',(list(ids),)).fetchall()}
    if found!=set(ids):raise ValueError('Every equivalent person must exist in this catalog')
    target=root/'calibration'
    target.mkdir(exist_ok=True)
    write_json(target/'identity-equivalences.json',{
        'schema':'familyfoto-calibration-identities-v1','model_id':runtime.get('model_id'),'space_id':runtime.get('space_id'),
        'groups':groups,'scope':'evaluation-only','source':'user-confirmed'})
    (target/'identity-equivalences.json').chmod(0o600)


def evaluate():
    root,inputs,revision=collect()
    equivalents=read_equivalences(root)
    report=calibrate(inputs,equivalents);report['review_revision']=revision;report['eligible_quality_faces']=sum(r['seed'] for r in inputs)
    report['input_sha256']=hashlib.sha256(json.dumps(inputs,sort_keys=True).encode()).hexdigest()
    report['equivalence_sha256']=hashlib.sha256(json.dumps(equivalents,sort_keys=True).encode()).hexdigest()
    report['activation_performed']=False
    run=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8]
    target=root/'calibration'/('evaluation-'+run);target.mkdir(parents=True)
    write_json(target/'evaluation.json',report)
    write_json(target/'labeled-inputs.json',inputs)
    write_json(root/'calibration/latest-evaluation.json',{'directory':target.name,'report':report})
    for path in target.glob('*.json'):path.chmod(0o600)
    return report

def activate(report, organize_existing=False):
    if not report['qualified']:raise ValueError('Held-out validation did not qualify this policy')
    from psycopg.types.json import Jsonb
    from face_detect.jobs.automatic_people_local import runtime
    from face_detect.jobs.automatic_people_jobs import manual_lease
    from face_detect.recognition import people,people_auto_organize as organize
    settings=json.loads((DATA/'people-preview.json').read_text());root=DATA/'face_detect/profiles'/settings['pilot']
    jobs=runtime(root)
    if jobs.provider.space_id!=report['space_id']:raise ValueError('Model changed during calibration')
    with manual_lease(lambda *args:None):
        with database.connection() as db:
            people._lock(db)
            if people.revision(db)!=report['review_revision']:raise ValueError('Labels changed; evaluate again before activation')
            previous=db.execute('SELECT policy FROM policy WHERE space_id=%s',(report['space_id'],)).fetchone()
            policy=dict(report['policy'])
            # Suggestions remain reviewed, even after automatic matching qualifies.
            old=previous['policy'] if previous else {}
            policy['suggestion_threshold']=old.get('manual_trial',{}).get('suggestion_threshold',policy['threshold'])
            policy_id=hashlib.sha256((report['space_id']+':'+json.dumps(policy,sort_keys=True)).encode()).hexdigest()
            db.execute('INSERT INTO policy(policy_id,space_id,policy,evidence) VALUES(%s,%s,%s,%s) ON CONFLICT(space_id) DO UPDATE SET policy_id=EXCLUDED.policy_id,policy=EXCLUDED.policy,evidence=EXCLUDED.evidence,activated_at=now()',
                       (policy_id,report['space_id'],Jsonb(policy),Jsonb(report)))
            auto.event(db,'calibration-activate','manual','calibration',{'space_id':report['space_id'],'input_sha256':report['input_sha256'],'policy':policy,'previous_policy':old})
            result={'policy_enabled':True,'existing_groups_changed':False,'jobs_started':0}
            if organize_existing:
                data=organize.snapshot(db)
                proposal=organize.plan(data)
                result=organize.apply(db,data,proposal,'calibration')
    write_json(root/'calibration/activation.json',{'evaluation':report,'result':result})
    (root/'calibration/activation.json').chmod(0o600)
    return result

if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('--activate',action='store_true')
    p.add_argument('--equivalent-person-ids',nargs='+',type=int,action='append')
    p.add_argument('--organize-existing',action='store_true');args=p.parse_args()
    if args.equivalent_person_ids:save_equivalences(args.equivalent_person_ids)
    report=evaluate();print(json.dumps(report))
    if args.activate:print(json.dumps({'activation':activate(report,args.organize_existing)}))
