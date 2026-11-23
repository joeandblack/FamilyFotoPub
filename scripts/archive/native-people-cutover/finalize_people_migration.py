"""Finalize only the isolated migration candidate; never changes live settings."""
import argparse
import json
import math
from pathlib import Path
import sys

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # prepare_people_migration stays in scripts/
from prepare_people_migration import fingerprint, save


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--migration',type=Path,required=True)
    args=parser.parse_args();folder=args.migration.resolve()
    report=json.loads((folder/'migration.json').read_text())
    progress=json.loads((folder/'embedding-progress.json').read_text())
    cfg=json.loads((folder/'candidate-connection.json').read_text())
    if (cfg['host'],cfg['port'],cfg['dbname'])!=('127.0.0.1',55449,report['candidate_database']):
        raise ValueError('Isolated candidate required')
    if progress['state']!='staging-complete' or progress['completed_photos']!=progress['total_photos']:
        raise ValueError('Embedding staging must be complete')
    if (folder/'native-runtime.json').exists():
        raise ValueError('Candidate already finalized; do not reapply')
    from face_detect.service import create_service
    provider=create_service({'face_detection':{'active_profile':'m','profiles':{'m':progress['profile']}}})
    try:
        if provider.identity!=progress['model_id']:raise ValueError('Pinned model changed')
        model=provider.identity;schema='face_native_'+model[:32]
        with psycopg.connect(**cfg,row_factory=dict_row) as db:
            if Path(db.execute('SHOW data_directory').fetchone()['data_directory']).resolve()!=folder.parent/'validation-postgres':
                raise ValueError('Unexpected cluster')
            db.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(report['source_schema'])))
            rows=db.execute("SELECT detection_id,result FROM native_migration_faces WHERE state='embedded' AND model_id=%s",(model,)).fetchall()
            if len(rows)!=progress['face_results']['embedded']:raise ValueError('Embedding count changed')
            for row in rows:
                v=row['result']['face']['vector']
                if len(v)!=512 or not all(math.isfinite(x) for x in v) or abs(sum(x*x for x in v)-1)>.001:
                    raise ValueError('Invalid staged embedding')
            for table in ('people_models','face_models'):
                db.execute(sql.SQL('INSERT INTO {}(id,manifest) VALUES(%s,%s)').format(sql.Identifier(table)),(model,Jsonb(provider.manifest)))
            db.execute('''INSERT INTO people_embeddings(detection_id,model_id,vector)
                SELECT detection_id,model_id,result->'face'->'vector' FROM native_migration_faces WHERE state='embedded' ''')
            db.execute('''INSERT INTO auto_people_observations(detection_id,model_id,seed)
                SELECT detection_id,model_id,(result->>'seed')::boolean FROM native_migration_faces WHERE state='embedded' ''')
            db.execute('UPDATE auto_people_members SET model_id=%s',(model,))
            db.execute('''CREATE TABLE native_catalog_state(singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
                model_id text NOT NULL REFERENCES people_models(id),legacy_pilot text NOT NULL)''')
            legacy=report['source_schema'].removeprefix('auto_eval_')
            db.execute('INSERT INTO native_catalog_state VALUES(true,%s,%s)',(model,legacy))
            db.execute('''CREATE TABLE native_preserved_photos AS SELECT DISTINCT media_id,content_hash FROM face_analyses''')
            db.execute('CREATE UNIQUE INDEX native_preserved_photos_identity ON native_preserved_photos(media_id,content_hash)')
            # Do not offer pre-migration Undo snapshots that contain the old model.
            db.execute('UPDATE people_revision SET revision=revision+1')
        with psycopg.connect(**cfg) as db:
            actual=fingerprint(db,report['source_schema'])
        changed={'people_models','face_models','people_embeddings','auto_people_observations','auto_people_members','people_revision'}
        for table,value in report['source_tables'].items():
            if table not in changed and actual.get(table)!=value:
                raise RuntimeError('Preserved data changed: '+table)
        with psycopg.connect(**cfg) as db:
            db.execute(sql.SQL('ALTER SCHEMA {} RENAME TO {}').format(sql.Identifier(report['source_schema']),sql.Identifier(schema)))
        from app.database.schema_upgrading import migrate
        from app.database import connection
        scoped={**cfg,'options':'-c search_path='+schema}
        migrate(connect=lambda:connection(scoped),directory=ROOT/'databases')
        runtime={'schema':schema,'profile':progress['profile'],'model_id':model,'media_ids':[],
                 'review_imports':[],'preserved_catalog':True,'legacy_pilot':legacy}
        save(folder/'native-runtime.json',runtime)
        save(folder/'finalization.json',{'preserved_decision_tables_verified':True,
             'native_embeddings':len(rows),'schema':schema,'activated':False,
             'unmatched_faces_retained':progress['face_results']['review'],
             'photo_errors_retained':progress['photo_results']['error']})
        print(json.dumps({'schema':schema,'native_embeddings':len(rows),'decisions_preserved':True}))
    finally:provider.close()


if __name__=='__main__':main()
