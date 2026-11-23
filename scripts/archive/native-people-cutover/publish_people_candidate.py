"""Export the finalized catalog and restore to validation or a new live schema."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

import psycopg

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # prepare_people_migration stays in scripts/
from prepare_people_migration import ROOT, pg_environment, save


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--migration',type=Path,required=True)
    parser.add_argument('--target',choices=('validation','live'),required=True)
    args=parser.parse_args();folder=args.migration.resolve()
    runtime=json.loads((folder/'native-runtime.json').read_text())
    candidate=json.loads((folder/'candidate-connection.json').read_text())
    target=(json.loads((folder.parent/'validation-connection.json').read_text()) if args.target=='validation'
            else json.loads((ROOT/'config/local.json').read_text(encoding='utf-8'))['database'])
    if candidate['host']!='127.0.0.1' or candidate['port']!=55449 or not candidate['dbname'].startswith('familyfoto_people_'):
        raise ValueError('Isolated candidate required')
    if args.target=='live':
        readiness=json.loads((folder/'native-live-readiness.json').read_text())
        edits=json.loads((folder/'native-write-validation.json').read_text())
        if not readiness.get('complete') or not edits.get('complete') or readiness['native_model']!=runtime['model_id']:
            raise ValueError('Successful isolated lifecycle and edit validation required')
    with psycopg.connect(**target) as db:
        if db.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s',(runtime['schema'],)).fetchone():
            raise ValueError('Target catalog already exists; never overwrite it')
    pg=Path('D:/PROJECTS/FamilyFoto/runtime/postgres/bin')
    archive=folder/'native-catalog.dump'
    if args.target=='validation':
        subprocess.run([str(pg/'pg_dump.exe'),'--format=custom','--no-owner','--no-acl',
                        '--schema='+runtime['schema'],'--file='+str(archive)],
                       env=pg_environment(candidate),check=True)
    with archive.open('rb') as stream:checksum=hashlib.file_digest(stream,'sha256').hexdigest()
    if args.target=='live' and checksum!=readiness['archive_sha256']:
        raise ValueError('Validated archive changed')
    subprocess.run([str(pg/'pg_restore.exe'),'--no-owner','--no-acl','--exit-on-error','--single-transaction',
                    '--dbname='+target['dbname'],str(archive)],env=pg_environment(target),check=True)
    save(folder/('published-'+args.target+'.json'),{'schema':runtime['schema'],'database':target['dbname'],'complete':True,'sha256':checksum})
    print('Published new isolated schema to '+args.target+'; application settings unchanged.')


if __name__=='__main__':main()
