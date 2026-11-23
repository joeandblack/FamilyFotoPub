"""Provision a narrowly scoped metadata login, first on the verified clone."""
import argparse
import json
from pathlib import Path
import secrets
import subprocess
import sys

import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.jobs.limited_edit import PUBLIC_TABLES, PEOPLE_TABLES, verify_database
from face_detect.api.readonly_people import catalog_settings


def provision(admin, dbname, schema):
    role = 'ff_metadata_' + secrets.token_hex(6)
    password = secrets.token_urlsafe(36)
    with psycopg.connect(**admin) as db:
        db.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB '
                           'NOCREATEROLE NOINHERIT NOREPLICATION VALID UNTIL {}').format(
                           sql.Identifier(role), sql.Literal(password), sql.Literal('2026-11-06')))
        db.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(sql.Identifier(dbname), sql.Identifier(role)))
        for scope, tables in [('public', PUBLIC_TABLES), (schema, PEOPLE_TABLES)]:
            db.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO {}').format(sql.Identifier(scope), sql.Identifier(role)))
            db.execute(sql.SQL('GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}').format(sql.Identifier(scope), sql.Identifier(role)))
            for table in sorted(tables):
                db.execute(sql.SQL('GRANT INSERT,UPDATE,DELETE ON {}.{} TO {}').format(
                    sql.Identifier(scope), sql.Identifier(table), sql.Identifier(role)))
            db.execute(sql.SQL('GRANT UPDATE(name) ON {}.media TO {}').format(sql.Identifier(scope), sql.Identifier(role)))
            sequences = db.execute('''SELECT DISTINCT seq.relname FROM pg_class seq
                JOIN pg_depend dep ON dep.objid=seq.oid
                JOIN pg_class tab ON tab.oid=dep.refobjid
                JOIN pg_namespace ns ON ns.oid=tab.relnamespace
                WHERE seq.relkind='S' AND ns.nspname=%s AND tab.relname=ANY(%s)''', (scope, list(tables))).fetchall()
            for (sequence,) in sequences:
                db.execute(sql.SQL('GRANT USAGE,SELECT ON SEQUENCE {}.{} TO {}').format(
                    sql.Identifier(scope), sql.Identifier(sequence), sql.Identifier(role)))
        db.execute(sql.SQL('GRANT UPDATE(ignored,ignore_history) ON {}.face_detections TO {}').format(
            sql.Identifier(schema), sql.Identifier(role)))
        db.execute(sql.SQL('ALTER ROLE {} SET statement_timeout={}').format(sql.Identifier(role), sql.Literal('120s')))
        db.execute(sql.SQL('ALTER ROLE {} SET lock_timeout={}').format(sql.Identifier(role), sql.Literal('15s')))
    return {**admin, 'user': role, 'password': password}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--backup', type=Path, required=True)
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    backup = args.backup.resolve()
    if not json.loads((backup/'manifest.json').read_text())['stages'].get('backup_complete'):
        raise RuntimeError('Verified backup required')
    cfg = json.loads((ROOT/'config/read-only-preview.json').read_text())
    from app.backup.admission import hold_process_lease
    hold_process_lease(ROOT / 'data')
    hold_process_lease(Path(cfg['preview']['data_root']))
    schema = catalog_settings(cfg['preview']['data_root'])['schema']
    if args.live:
        checks = json.loads((backup/'limited-role-validation.json').read_text())
        if checks.get('complete') is not True:
            raise RuntimeError('Restricted-role clone tests must pass first')
        if json.loads((backup/'limited-app-validation.json').read_text()).get('complete') is not True:
            raise RuntimeError('Exact limited-mode app tests must pass first')
        original = json.loads(Path('D:/PROJECTS/FamilyFoto/config/local.json').read_text())
        if any(original['database'].get(key) != cfg['database'].get(key) for key in ('host', 'port', 'dbname')):
            raise RuntimeError('Live database must match the existing protected preview')
        admin = {**original['database'], 'user': 'postgres', 'password': original['postgres_admin_password']}
        output = ROOT/'config/limited-edit-preview.json'
    else:
        admin = json.loads((backup/'validation-connection.json').read_text())
        output = backup/'limited-edit-test.json'
        with psycopg.connect(**admin) as db:
            if Path(db.execute('SHOW data_directory').fetchone()[0]).resolve() != backup/'validation-postgres':
                raise RuntimeError('Unexpected validation cluster')
    if output.exists():
        raise RuntimeError('Existing limited-edit configuration must be reviewed, not overwritten')
    cfg['database'] = provision(admin, admin['dbname'], schema)
    cfg['limited_metadata_edit'] = True
    verify_database(cfg)
    output.write_text(json.dumps(cfg, indent=2), encoding='utf-8')
    # Same private ACL as the backup, but keep read access for the Low Integrity
    # child (it runs as this Windows account, with filesystem writes restricted).
    user = subprocess.check_output(['whoami'], text=True).strip()
    subprocess.run(['icacls', str(output), '/inheritance:r', '/grant:r',
                    user+':(F)', '*S-1-5-18:(F)', '*S-1-5-32-544:(F)'], check=True, capture_output=True)
    print('Prepared verified metadata-only login; credentials saved privately.')


if __name__ == '__main__':
    main()
