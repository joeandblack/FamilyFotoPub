"""Explicitly enroll completed import 14 in the existing isolated review catalog."""
import json
import re
import psycopg
from psycopg.rows import dict_row
from app.config import load_config
from face_detect.recognition.faces import VISIBLE
from face_detect.api.people_preview_gateway import settings
from app.telemetry import write_json


def main():
    cfg=load_config();root=settings()['root'];path=root/'runtime.json'
    runtime=json.loads(path.read_text())
    if not re.fullmatch('face_native_[0-9a-f]{32}',runtime.get('schema','')):
        # The one People catalog reads the main photo list; a shadow copy would land in it.
        raise RuntimeError('Retired: the People catalog reads the photo list directly, nothing to enroll')
    with psycopg.connect(**cfg['database'],row_factory=dict_row) as live:
        if not live.execute('SELECT pg_try_advisory_lock(4340034) ok').fetchone()['ok']:
            raise RuntimeError('People worker active; pause it first')
        job=live.execute('SELECT state FROM jobs WHERE id=14').fetchone()
        if not job or job['state']!='completed-review-required':raise RuntimeError('Import 14 must be complete')
        rows=live.execute("""SELECT m.id,m.sha256,m.relative_path,m.name,m.kind,m.size,m.captured_at,m.width,m.height
            FROM media m WHERE m.kind='image' AND """+VISIBLE+"""
            AND (m.first_import_job_id=14 OR EXISTS(SELECT 1 FROM import_items i WHERE i.job_id=14 AND i.media_id=m.id))
            AND NOT EXISTS(SELECT 1 FROM face_analyses a JOIN face_detections d ON d.analysis_id=a.id
                WHERE a.media_id=m.id AND (d.ignored OR d.review_state='rejected')) ORDER BY m.id""").fetchall()
        if not 1<=len(rows)<=400:raise ValueError('Review batch outside bounded limit')
        with psycopg.connect(**{**cfg['database'],'options':'-c search_path='+runtime['schema']}) as shadow:
            shadow.execute("INSERT INTO jobs(id,source_root,state) OVERRIDING SYSTEM VALUE VALUES(14,'pilot-import:14','completed') ON CONFLICT DO NOTHING")
            for row in rows:
                shadow.execute('INSERT INTO media(id,sha256,relative_path,name,kind,size,captured_at,width,height) OVERRIDING SYSTEM VALUE VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING',tuple(row.values()))
                shadow.execute("INSERT INTO import_items(job_id,source_path,size,mtime_ns,state,media_id) VALUES(14,%s,0,0,'imported',%s) ON CONFLICT DO NOTHING",('catalog:'+str(row['id']),row['id']))
        runtime['media_ids']=sorted(set(runtime['media_ids'])|{r['id'] for r in rows})
        runtime['review_imports']=[14];write_json(path,runtime)
    print(json.dumps({'import_id':14,'enrolled_photos':len(rows),'isolated':True}))


if __name__=='__main__':main()
