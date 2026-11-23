"""Read-only recognition preservation snapshot. Reports stay in private data/."""
import argparse
import hashlib
import json
import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from app.config import load_config, DATA
from face_detect.api.people_preview_gateway import settings
from app.telemetry import write_json


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--label',choices=['before','after'],required=True)
    args=parser.parse_args()
    root=settings()['root']
    schema=json.loads((root/'runtime.json').read_text())['schema']
    result={}
    tables=['people','people_assignments','people_constraints','auto_people_groups',
            'auto_people_members','auto_people_events','auto_people_blocks',
            'auto_people_receipts','embedding','face_detections','face_analyses']
    with psycopg.connect(**load_config()['database'],row_factory=dict_row) as db:
        db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        for table in tables:
            rows=db.execute(sql.SQL('SELECT row_to_json(t)::text value FROM {}.{} t ORDER BY row_to_json(t)::text').format(sql.Identifier(schema),sql.Identifier(table))).fetchall()
            digest=hashlib.sha256('\n'.join(r['value'] for r in rows).encode()).hexdigest()
            result[table]={'count':len(rows),'sha256':digest}
        result['active_workers']=db.execute("SELECT count(*) n FROM pg_locks WHERE locktype='advisory' AND granted AND objid IN (4340034,4340023,4340031) AND mode='ExclusiveLock'").fetchone()['n']
    reports=DATA/'reports'/'people-integration-20261020'
    reports.mkdir(parents=True,exist_ok=True)
    if args.label=='after':
        before=json.loads((reports/'before.json').read_text())
        assert all(result[t]==before[t] for t in tables),'Recognition records changed during deployment'
    write_json(reports/f'{args.label}.json',result)
    print(json.dumps({'report':str(reports/f'{args.label}.json'),'counts':{t:result[t]['count'] for t in tables},'active_workers':result['active_workers']}))


if __name__=='__main__':main()
