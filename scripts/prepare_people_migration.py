"""Snapshot the preserved People schema into a separate local validation DB.

Never activates a catalog or mutates the source. Credentials and biometric data
stay beneath the pre-existing private backup directory.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def save(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2), encoding='utf-8')
    temporary.replace(path)


def fingerprint(db, schema):
    tables = [r[0] for r in db.execute("SELECT tablename FROM pg_tables WHERE schemaname=%s ORDER BY tablename", (schema,))]
    result = {}
    for table in tables:
        hashes = []
        with db.cursor(name='migration_fingerprint') as cursor:
            cursor.execute(sql.SQL('SELECT row_to_json(t)::text FROM {}.{} t').format(
                sql.Identifier(schema), sql.Identifier(table)))
            for (row,) in cursor:
                hashes.append(hashlib.sha256(row.encode()).digest())
        result[table] = {'rows': len(hashes), 'sha256': hashlib.sha256(b''.join(sorted(hashes))).hexdigest()}
    return result


def pg_environment(settings):
    return {**os.environ, 'PGHOST': settings['host'], 'PGPORT': str(settings['port']),
            'PGDATABASE': settings['dbname'], 'PGUSER': settings['user'],
            'PGPASSWORD': settings['password']}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--backup', type=Path, required=True)
    args = parser.parse_args()
    backup = args.backup.resolve()
    connection = json.loads((backup/'validation-connection.json').read_text())
    if (connection['host'], connection['port'], connection['dbname']) != ('127.0.0.1', 55449, 'familyfoto_validation'):
        raise ValueError('Only the separate validation cluster is permitted')
    with psycopg.connect(**connection) as db:
        if Path(db.execute('SHOW data_directory').fetchone()[0]).resolve() != backup/'validation-postgres':
            raise ValueError('Unexpected validation cluster')
    cfg = json.loads((ROOT/'config/local.json').read_text(encoding='utf-8'))
    from face_detect.api.readonly_people import catalog_settings
    catalog = catalog_settings(cfg['preview']['data_root'])
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    output = backup/('people-migration-'+stamp)
    output.mkdir()
    candidate_name = 'familyfoto_people_'+stamp.lower()
    candidate = {**connection, 'dbname': candidate_name}
    pg = Path('D:/PROJECTS/FamilyFoto/runtime/postgres/bin')
    report = {'stage': 'snapshotting', 'source_schema': catalog['schema'],
              'source_profile': str(catalog['root']), 'created_at': stamp,
              'candidate_database': candidate_name, 'activated': False}
    save(output/'migration.json', report)
    with psycopg.connect(**cfg['database']) as source:
        source.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        snapshot = source.execute('SELECT pg_export_snapshot()').fetchone()[0]
        subprocess.run([str(pg/'pg_dump.exe'), '--format=custom', '--no-owner', '--no-acl',
                        '--schema='+catalog['schema'], '--snapshot='+snapshot,
                        '--file='+str(output/'people.dump')],
                       env=pg_environment(cfg['database']), check=True)
        print('Snapshot saved; verifying every catalog table.', flush=True)
        report['source_tables'] = fingerprint(source, catalog['schema'])
    with (output/'people.dump').open('rb') as stream:
        report['archive_sha256'] = hashlib.file_digest(stream, 'sha256').hexdigest()
    with psycopg.connect(**connection, autocommit=True) as admin:
        admin.execute(sql.SQL('CREATE DATABASE {} TEMPLATE template0').format(sql.Identifier(candidate_name)))
    save(output/'candidate-connection.json', candidate)
    subprocess.run([str(pg/'pg_restore.exe'), '--no-owner', '--no-acl', '--exit-on-error',
                    '--dbname='+candidate_name, str(output/'people.dump')],
                   env=pg_environment(candidate), check=True)
    with psycopg.connect(**candidate) as db:
        copied = fingerprint(db, catalog['schema'])
        if copied != report['source_tables']:
            raise RuntimeError('Restored People catalog does not exactly match the snapshot')
    report.update(stage='preservation-verified', verified_tables=len(copied))
    save(output/'migration.json', report)
    print(json.dumps({'output': str(output), 'stage': report['stage'], 'tables': len(copied),
                      'counts': {t: copied[t]['rows'] for t in ('people', 'face_detections', 'auto_people_groups', 'people_assignments') if t in copied}}))


if __name__ == '__main__':
    main()
