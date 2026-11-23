"""Read-only queue audit. Never dispatches, resumes, or changes a job."""
import json
from collections import Counter
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
cfg = json.loads((ROOT/'config/read-only-preview.json').read_text())
data = Path(cfg['preview']['data_root'])
report = {}
with psycopg.connect(**cfg['database'], row_factory=dict_row) as db:
    db.execute('SET TRANSACTION READ ONLY')
    report['upload_states'] = db.execute('SELECT state,count(*) n FROM upload_sessions GROUP BY state').fetchall()
    report['upload_dispatch_candidates'] = db.execute("""SELECT id::text,job_id,state,cancel_requested,
        cleaned_at IS NULL staging_not_cleaned FROM upload_sessions WHERE
        state IN ('queued','importing','cancelling','uploading') OR (state='done' AND cleaned_at IS NULL)
        ORDER BY created_at""").fetchall()
    report['import_states'] = db.execute('SELECT state,count(*) n FROM jobs GROUP BY state').fetchall()
    report['incomplete_imports'] = db.execute("""SELECT id,state,auto_metadata,auto_conversion,auto_duplicates,
        error IS NOT NULL has_error FROM jobs WHERE state NOT IN
        ('completed-review-required','completed','cancelled') ORDER BY id""").fetchall()
    report['pipeline_options'] = db.execute("""SELECT id,auto_metadata,auto_conversion,auto_duplicates
        FROM jobs WHERE auto_metadata OR auto_conversion OR auto_duplicates ORDER BY id""").fetchall()
for label, folder in [('active', data), ('new_checkout', ROOT/'data')]:
    state = {'root': str(folder), 'pause_markers': [], 'statuses': {}, 'face_jobs': [], 'native_profiles': []}
    state['pause_markers'] = [p.name for p in folder.glob('*PAUSE*') if p.is_file()]
    for name in ['pipeline-status.json','video-status.json','avi-status.json','photo-duplicate-status.json',
                 'face-service-settings.json','pipeline-handoff.json']:
        path=folder/name
        if path.exists():
            value=json.loads(path.read_text(encoding='utf-8'))
            state['statuses'][name]={k:v for k,v in value.items() if k in
                ('state','stage','job_id','import_id','updated','start_with_app','expires_at')}
    profiles=[]
    settings=folder/'people-preview.json'
    if settings.exists():
        value=json.loads(settings.read_text())
        pilot=value.get('pilot','')
        if len(pilot)==32 and all(c in '0123456789abcdef' for c in pilot):
            # Audit retained historical jobs too; this is not a service dependency.
            profiles.append(folder/'compreface/raw-local'/pilot)
    native=folder/'face_detect/profiles'
    if native.exists():
        profiles.extend(p.parent for p in native.glob('*/runtime.json'))
        state['native_profiles']=[p.name for p in profiles if p.parent==native]
    for profile in profiles:
        controls=Counter()
        for control in (profile/'jobs').glob('*/control.json'):
            controls[json.loads(control.read_text()).get('action','unknown')]+=1
        state.setdefault('all_face_control_counts',{})[profile.name]=dict(controls)
        for path in (profile/'jobs').glob('import-*/import.json'):
            value=json.loads(path.read_text())
            control=path.parent/'control.json'
            counts=Counter(item.get('state','unknown') for item in value.get('items',[]))
            state['face_jobs'].append({'profile':profile.name,'job':path.parent.name,
                'saved_state':value.get('state'),
                'command':json.loads(control.read_text()).get('action') if control.exists() else None,
                'items':dict(counts)})
    report[label]=state
print(json.dumps(report,indent=2,default=str))
