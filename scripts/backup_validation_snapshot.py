"""Explicit offline-worker backup and isolated restore; never upgrades the source."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import time

import psycopg
from psycopg import sql


def run(command, *, env=None, stdout=None):
    subprocess.run([str(arg) for arg in command], check=True, env=env, stdout=stdout,
                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def counts(db):
    tables = db.execute("""SELECT n.nspname,c.relname FROM pg_class c
        JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind IN ('r','p')
        AND n.nspname NOT IN ('pg_catalog','information_schema')
        AND n.nspname NOT LIKE 'pg_toast%' ORDER BY 1,2""").fetchall()
    return {f'{schema}.{table}': db.execute(sql.SQL('SELECT count(*) FROM {}.{}').format(
        sql.Identifier(schema), sql.Identifier(table))).fetchone()[0] for schema, table in tables}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--port', type=int, default=55449)
    parser.add_argument('--resume-file-copy', action='store_true')
    args = parser.parse_args()
    source, output = args.source.resolve(), args.output.resolve()
    if (output.exists() and not args.resume_file_copy) or output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError('Use a new backup directory separate from the source installation')
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', args.port))
    cfg = json.loads((source / 'config/local.json').read_text(encoding='utf-8'))
    if cfg['database']['host'] not in ('127.0.0.1', 'localhost'):
        raise ValueError('This validation helper requires a local source database')
    if args.port == cfg['database']['port']:
        raise ValueError('Validation must use a separate port')
    if args.resume_file_copy:
        manifest = json.loads((output/'manifest.json').read_text())
        if (manifest['source'] != str(source) or manifest['backup'] != str(output)
                or 'database_dump' not in manifest['stages'] or (output/'validation-postgres').exists()):
            raise ValueError('Only an interrupted file-copy stage can be resumed')
        if digest(output/'familyfoto.dump') != manifest['stages']['database_dump']['sha256']:
            raise ValueError('Saved database archive failed checksum validation')
    output.mkdir(parents=True, exist_ok=args.resume_file_copy)
    account = subprocess.check_output(['whoami'], text=True).strip()
    run(['icacls', output, '/inheritance:r', '/grant:r', f'{account}:(OI)(CI)F',
         'SYSTEM:(OI)(CI)F', 'BUILTIN\\Administrators:(OI)(CI)F'], stdout=subprocess.DEVNULL)
    if not args.resume_file_copy:
        manifest = {'started_local': datetime.now().astimezone().isoformat(), 'stages': {},
                    'source': str(source), 'backup': str(output), 'original_media_copied': False}

    def stage(name, detail):
        manifest['stages'][name] = detail
        (output / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
        print(name + ': ' + (detail if isinstance(detail, str) else 'complete'), flush=True)

    binary = source / 'runtime/postgres/bin'
    admin = {**cfg['database'], 'user': 'postgres', 'password': cfg['postgres_admin_password']}
    env = {**os.environ, 'PGPASSWORD': admin['password'], 'PGOPTIONS': '-c default_transaction_read_only=on'}
    connect = ['-h', admin['host'], '-p', admin['port'], '-U', admin['user'], '-w']
    started = time.monotonic()
    if args.resume_file_copy:
        source_counts = manifest['stages']['database_dump']['tables']
    else:
        with psycopg.connect(**admin) as db:
            db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
            size = db.execute('SELECT pg_database_size(current_database())').fetchone()[0]
            if shutil.disk_usage(output).free < size * 3 + 5_000_000_000:
                raise RuntimeError('Insufficient room for backup and restored validation database')
            snapshot = db.execute('SELECT pg_export_snapshot()').fetchone()[0]
            stage('source_database_bytes', size)
            run([binary/'pg_dump.exe', *connect, '-d', admin['dbname'], '--format=custom',
                 '--compress=1', '--snapshot='+snapshot, '--file', output/'familyfoto.dump'], env=env)
            source_counts = counts(db)
        stage('database_dump', {'seconds': round(time.monotonic()-started, 1),
                               'bytes': (output/'familyfoto.dump').stat().st_size,
                               'sha256': digest(output/'familyfoto.dump'), 'tables': source_counts})
    run([binary/'pg_dumpall.exe', *connect, '--globals-only', '--no-role-passwords',
         '--file', output/'roles-without-passwords.sql'], env=env)
    with (output/'archive-contents.txt').open('w', encoding='utf-8') as target:
        run([binary/'pg_restore.exe', '--list', output/'familyfoto.dump'], stdout=target)
    preview = json.loads((source/'data/people-preview.json').read_text())
    pilot = preview['pilot']
    if len(pilot) != 32 or any(char not in '0123456789abcdef' for char in pilot):
        raise ValueError('Invalid existing People identity')
    # Historical catalog storage must remain discoverable for verified backups.
    profile = source/'data/compreface/raw-local'/pilot
    copied = {}
    def copy_verified(path):
        if path.is_symlink():
            raise ValueError('People backup must not follow filesystem links')
        if not path.is_file():
            return None
        relative = path.relative_to(source)
        target = output/'installation'/relative
        target.parent.mkdir(parents=True, exist_ok=True)
        checksum = digest(path)
        if not target.is_file() or digest(target) != checksum:
            shutil.copy2(path, target)
        if digest(target) != checksum:
            raise RuntimeError('People file backup verification failed')
        return relative.as_posix(), {'bytes': target.stat().st_size, 'sha256': checksum}
    paths = list(profile.rglob('*'))
    with ThreadPoolExecutor(max_workers=8) as pool:
        for offset in range(0, len(paths), 2000):
            for result in pool.map(copy_verified, paths[offset:offset+2000]):
                if result is not None:
                    copied[result[0]] = result[1]
            print(f'People file verification: {min(offset+2000,len(paths))}/{len(paths)} entries', flush=True)
    for relative in ('config/local.json', 'data/people-preview.json'):
        target = output/'installation'/relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source/relative, target)
        copied[relative] = {'bytes': target.stat().st_size, 'sha256': digest(target)}
    (output/'files.json').write_text(json.dumps(copied, indent=2), encoding='utf-8')
    stage('people_files', {'count': len(copied), 'bytes': sum(row['bytes'] for row in copied.values()),
                           'all_hashes_verified': True})

    password = secrets.token_urlsafe(40)
    pwfile = output/'validation-password.txt'
    pwfile.write_text(password, encoding='ascii')
    cluster = output/'validation-postgres'
    run([binary/'initdb.exe', '-D', cluster, '-U', 'validation_admin', '--auth=scram-sha-256',
         '--encoding=UTF8', '--locale=C', '--pwfile', pwfile], stdout=subprocess.DEVNULL)
    pwfile.unlink()
    test = {'host': '127.0.0.1', 'port': args.port, 'user': 'validation_admin',
            'password': password, 'dbname': 'familyfoto_validation'}
    (output/'validation-connection.json').write_text(json.dumps(test), encoding='utf-8')
    running = False
    try:
        run([binary/'pg_ctl.exe', '-D', cluster, '-l', output/'validation-postgres.log',
             '-o', f'-p {args.port} -h 127.0.0.1', '-w', 'start'], stdout=subprocess.DEVNULL)
        running = True
        with psycopg.connect(**{**test, 'dbname': 'postgres'}, autocommit=True) as db:
            db.execute('CREATE DATABASE familyfoto_validation')
        test_env = {**os.environ, 'PGPASSWORD': password}
        test_env.pop('PGOPTIONS', None)
        with (output/'restore.log').open('w', encoding='utf-8') as log:
            subprocess.run([str(binary/'pg_restore.exe'), '-h', '127.0.0.1', '-p', str(args.port),
                            '-U', test['user'], '-w', '-d', test['dbname'], '--no-owner', '--no-privileges',
                            '--exit-on-error', '--jobs=2', str(output/'familyfoto.dump')],
                           env=test_env, stdout=log, stderr=log, check=True,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        with psycopg.connect(**test) as db:
            restored_counts = counts(db)
        if restored_counts != source_counts:
            raise RuntimeError('Restored table counts differ from the exported snapshot')
        stage('restore_verified', {'tables': len(restored_counts), 'all_counts_match': True,
                                  'port': args.port, 'database': test['dbname']})
    finally:
        if running:
            run([binary/'pg_ctl.exe', '-D', cluster, '-m', 'fast', '-w', 'stop'], stdout=subprocess.DEVNULL)
            stage('validation_database', 'stopped')
    stage('backup_complete', True)


if __name__ == '__main__':
    main()
