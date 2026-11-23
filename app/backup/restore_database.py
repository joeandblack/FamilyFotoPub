"""Trusted archive restore into a newly created, read-only validation database.

This is not activation. PostgreSQL archives can execute code; use a separately
provisioned recovery cluster and a trusted archive, never an untrusted upload.
"""
from dataclasses import replace
from pathlib import Path
import re
import subprocess
import threading
import time
import uuid

import psycopg
from psycopg import sql

from .encrypted import read_chunks, verify_artifact
from .inventory import inspect_snapshot
from .space import Requirement, SpaceGuard
from .compatibility import preflight_target, validate_source_evidence, verify_restored_evidence


class RestoreFailed(RuntimeError):
    pass


def wait_for_archive_completion(process, guard, *, timeout=6 * 60 * 60, clock=time.monotonic):
    """Allow large indexes/constraints to finish after all archive bytes were sent.

    Byte transfer completion is not database completion. Keep capacity checks
    active and remain interruptible instead of killing healthy work after 120s.
    """
    deadline = clock() + timeout
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            raise RestoreFailed('Database finalization exceeded six hours; target remains isolated. '
                                'Inspect target storage and PostgreSQL activity before retrying.')
        guard.check()
        try:
            return process.wait(timeout=min(5, remaining))
        except subprocess.TimeoutExpired:
            continue


def restore_database(artifact, key, admin, pg_restore, *, expected_cluster_directory,
                     required_bytes, guard_factory=SpaceGuard, progress=None, record_target=None, resume_record=None,
                     allow_sanitized=False):
    """Create a random new DB on an explicitly identified recovery cluster.

    Failure leaves that new DB for inspection, with default read-only enabled.
    No existing database is dropped, cleaned, migrated, or made writable.
    """
    manifest = verify_artifact(artifact, key)
    metadata = manifest['metadata']
    if metadata.get('kind') != 'postgres-custom-dump' or not isinstance(metadata.get('tables'), list):
        raise ValueError('Not a database recovery artifact')
    validate_source_evidence(metadata.get('compatibility'))
    executable = Path(pg_restore).absolute()
    if not executable.is_file():
        raise ValueError('Explicit pg_restore executable required')
    cluster = Path(expected_cluster_directory).resolve(strict=True)
    if admin.dbname != 'postgres':
        raise ValueError('Recovery cluster administration endpoint must name postgres')
    if type(required_bytes) is not int or required_bytes <= 0:
        raise ValueError('Explicit positive database restore capacity budget required')
    guard = guard_factory([Requirement('database', cluster, required_bytes)])
    guard.check()
    options = admin.connection()
    options['options'] = '-c default_transaction_read_only=off'
    name = 'familyfoto_recovery_' + uuid.uuid4().hex
    identity = {'cluster_directory': str(cluster), 'artifact_id': manifest['id'],
                'artifact_sha256': manifest['sha256'], 'activation_allowed': False}
    if resume_record is not None:
        if (not isinstance(resume_record, dict) or record_target is None
                or any(resume_record.get(key) != value for key, value in identity.items())
                or not re.fullmatch('familyfoto_recovery_[0-9a-f]{32}', str(resume_record.get('database', '')))
                or not re.fullmatch('[0-9a-f]{32}', str(resume_record.get('ownership_token', '')))):
            raise RestoreFailed('Recovery database journal identity differs')
        name = resume_record['database']
    record = {**identity, 'database': name, 'ownership_token': uuid.uuid4().hex,
              'state': 'creation-planned', 'verified': False} if resume_record is None else dict(resume_record)
    existing = False
    sanitized = False
    with psycopg.connect(**options, autocommit=True) as db:
        actual = Path(db.execute('SHOW data_directory').fetchone()[0]).resolve(strict=True)
        if actual != cluster:
            raise ValueError('Recovery cluster identity does not match the explicit target')
        target_version = int(db.execute('SHOW server_version_num').fetchone()[0])
        if target_version // 10000 != int(metadata['server_version']) // 10000:
            raise ValueError('Cross-major database restore requires separate qualification')
        preflight_target(db, metadata['compatibility'])
        # Persist intent before CREATE: a crash or failed restore must not orphan
        # an unidentified database. Failure to journal prevents creation.
        if resume_record is not None:
            row = db.execute("SELECT oid, shobj_description(oid, 'pg_database'), "
                "datdba=(SELECT oid FROM pg_roles WHERE rolname=current_user) FROM pg_database WHERE datname=%s",
                (name,)).fetchone()
            if row:
                existing = True
                ownership = 'familyfoto-recovery:' + record['ownership_token']
                sanitized = allow_sanitized and row[1] == ownership + ':sanitized'
                if (record.get('database_oid') != row[0] or (row[1] != ownership and not sanitized)
                        or not row[2]):
                    raise RestoreFailed('Recovery database ownership differs')
                public = db.execute("SELECT EXISTS (SELECT FROM pg_database d, "
                    "LATERAL aclexplode(coalesce(d.datacl,acldefault('d',d.datdba))) a "
                    "WHERE d.oid=%s AND a.grantee=0 AND a.privilege_type='CONNECT')", (row[0],)).fetchone()[0]
                settings = db.execute('SELECT setconfig FROM pg_db_role_setting WHERE setdatabase=%s AND setrole=0',
                                      (row[0],)).fetchone()
                active = db.execute('SELECT count(*) FROM pg_stat_activity WHERE datid=%s', (row[0],)).fetchone()[0]
                if public or not settings or 'default_transaction_read_only=on' not in settings[0] or active:
                    raise RestoreFailed('Recovery database is not isolated and read-only')
            elif record.get('state') != 'creation-planned' or record.get('database_oid') is not None:
                raise RestoreFailed('Previously created recovery database is missing')
        if not existing:
            if record_target is not None:
                record_target(record)
            db.execute(sql.SQL('CREATE DATABASE {} TEMPLATE template0').format(sql.Identifier(name)))
            db.execute(sql.SQL('ALTER DATABASE {} SET default_transaction_read_only = on').format(sql.Identifier(name)))
            db.execute(sql.SQL('REVOKE CONNECT ON DATABASE {} FROM PUBLIC').format(sql.Identifier(name)))
            db.execute(sql.SQL('COMMENT ON DATABASE {} IS {}').format(
                sql.Identifier(name), sql.Literal('familyfoto-recovery:' + record['ownership_token'])))
            record.update(database_oid=db.execute('SELECT oid FROM pg_database WHERE datname=%s', (name,)).fetchone()[0],
                          state='created')
            if record_target is not None:
                record_target(record)
    target = replace(admin, dbname=name)
    populated = False
    if existing:
        with psycopg.connect(**target.connection()) as db:
            populated = db.execute("SELECT EXISTS (SELECT FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema') OR EXISTS "
                "(SELECT FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                "WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema') OR EXISTS "
                "(SELECT FROM pg_namespace WHERE nspname NOT LIKE 'pg_%' "
                "AND nspname NOT IN ('public','information_schema'))").fetchone()[0]
    if not populated and not sanitized:
        _load_archive(artifact, key, manifest, target, executable, guard, progress)
    tables = _verify_database(target, metadata, sanitized=sanitized)
    return {**record, 'verified': True, 'state': 'verified', 'activation_allowed': False,
            'default_read_only': True, 'tables': tables, 'credentials_and_jobs_sanitized': sanitized}


def _load_archive(artifact, key, manifest, target, executable, guard, progress):
    from .diagnostics import ProcessDiagnostics
    diagnostics = ProcessDiagnostics()
    env = target.environment()
    env['PGOPTIONS'] = '-c default_transaction_read_only=off'
    command = [str(executable), *target.arguments(), '--no-owner', '--no-privileges',
               '--single-transaction', '--exit-on-error']
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                          stderr=subprocess.PIPE, env=env,
                          creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)) as process:
        reader = threading.Thread(target=diagnostics.drain, args=(process.stderr,), daemon=True)
        reader.start()
        try:
            sent = 0
            for block in read_chunks(artifact, key, manifest):
                guard.check()
                if process.stdin.write(block) != len(block):
                    raise OSError('Short database restore pipe write')
                sent += len(block)
                if progress:
                    progress(sent)
            process.stdin.close()
            result = wait_for_archive_completion(process, guard)
            reader.join(timeout=5)
            if result or diagnostics.has_output or reader.is_alive():
                raise diagnostics.failure(RestoreFailed, 'Database restore failed; isolated target was not activated')
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
            reader.join(timeout=5)


def _verify_database(target, metadata, *, sanitized=False):
    with psycopg.connect(**target.connection()) as db:
        db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        restored_inventory = list(inspect_snapshot(db))
        actual_tables = {(item['schema'], item['name']) for item in restored_inventory}
        expected_tables = {(item['schema'], item['table']) for item in metadata['tables']}
        if actual_tables != expected_tables:
            raise RestoreFailed('Restored table inventory does not match the backup')
        verify_restored_evidence(db, restored_inventory, metadata['compatibility'])
        for item in metadata['tables']:
            count = db.execute(sql.SQL('SELECT count(*) FROM {}.{}').format(
                sql.Identifier(item['schema']), sql.Identifier(item['table']))).fetchone()[0]
            expected = 0 if sanitized and item['schema'] == 'public' and item['table'] in {
                'auth_sessions', 'auth_login_limits'} else item['rows']
            if count != expected:
                raise RestoreFailed('Restored row count does not match the backup')
    return len(expected_tables)
