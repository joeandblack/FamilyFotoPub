"""Short-lived database access for explicit previews of owned recovery candidates."""
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import psycopg
from psycopg import sql

from ..backup.catalog import freeze_database
from ..backup.encrypted import InvalidArtifact, read_bounded
from ..backup.media_copy import copy_lock
from ..backup.recovery import save_private
from ..backup.space import directory_identity
from ..backup.volumes import volume_identity
from .credentials import FORMAT, owned_target, verify_staged_role


def preview_identity(admin, record, directory, cluster):
    identity = json.loads(read_bounded(directory / 'credentials.json', 65536))
    report = json.loads(read_bounded(directory / 'ROLE-REPORT.json', 65536))
    if (identity.get('format') != FORMAT or identity.get('access') != 'preview-select-only'
            or identity.get('database') != record.get('database')
            or identity.get('database_oid') != record.get('database_oid')
            or identity.get('database_token') != record.get('ownership_token')
            or identity.get('endpoint') != [admin.host, admin.port, admin.user]
            or identity.get('cluster_directory') != str(cluster)
            or identity.get('directory') != list(directory_identity(directory))
            or identity.get('volume') != volume_identity(directory)
            or record.get('cluster_directory') != str(cluster)):
        raise InvalidArtifact('Preview credentials do not belong to this recovery candidate')
    return identity, report


def revoke_access(target, record, identity, report, cluster):
    """Revoke only the exact owned preview role; never terminate unrelated clients."""
    options = target.connection()
    options['options'] = '-c default_transaction_read_only=off'
    with psycopg.connect(**options) as db:
        owned_target(db, record, cluster)
        row = db.execute("SELECT oid, shobj_description(oid,'pg_authid') FROM pg_roles WHERE rolname=%s",
                         (identity['role'],)).fetchone()
        if not row or row != (report['role_oid'], FORMAT + ':' + identity['token']):
            raise InvalidArtifact('Preview role ownership changed; refusing to revoke another role')
        db.execute(sql.SQL('ALTER ROLE {} NOLOGIN VALID UNTIL {}').format(
            sql.Identifier(identity['role']), sql.Literal(datetime.now(timezone.utc).isoformat())))
    # NOLOGIN alone leaves existing connections alive. Revoke first, then close
    # sessions for this dedicated random role on this exact candidate only.
    with psycopg.connect(**options) as db:
        owned_target(db, record, cluster)
        db.execute('SELECT pg_terminate_backend(pid) FROM pg_stat_activity '
                   'WHERE datid=%s AND usesysid=%s AND pid<>pg_backend_pid()',
                   (record['database_oid'], report['role_oid']))


@contextmanager
def preview_database_access(admin, record, directory, *, expected_cluster_directory):
    """Caller validates the full candidate first and closes its server before exit.

    This API starts no listener. Its password is returned only in process memory.
    Crash recovery uses close_preview_access; expiry is defense in depth for
    password authentication, not a substitute for revocation or closing sessions.
    """
    directory = Path(directory).absolute()
    cluster = Path(expected_cluster_directory).resolve(strict=True)
    with copy_lock(directory):
        identity, report = preview_identity(admin, record, directory, cluster)
        target = replace(admin, dbname=identity['database'])
        expires = datetime.now(timezone.utc) + timedelta(hours=1)
        intent = {'format': 'familyfoto-preview-access-v1', 'role_oid': report['role_oid'],
                  'database_oid': record['database_oid'], 'expires_at': expires.isoformat(),
                  'state': 'opening', 'activation_allowed': False}
        with freeze_database(target) as db:
            owned_target(db, record, cluster)
            verify_staged_role(db, identity, report)
            save_private(directory / 'PREVIEW-ACCESS.json', intent)
            db.execute(sql.SQL('ALTER ROLE {} LOGIN VALID UNTIL {}').format(
                sql.Identifier(identity['role']), sql.Literal(expires.isoformat())))
        try:
            save_private(directory / 'PREVIEW-ACCESS.json', {**intent, 'state': 'open'})
            yield {'host': admin.host, 'port': admin.port, 'dbname': identity['database'],
                   'user': identity['role'], 'password': identity['password'],
                   'options': '-c default_transaction_read_only=on'}
        finally:
            revoke_access(target, record, identity, report, cluster)
            save_private(directory / 'PREVIEW-ACCESS.json', {**intent, 'state': 'closed'})


def close_preview_access(admin, record, directory, *, expected_cluster_directory):
    """Explicit cleanup after a killed preview; never enables a login or server."""
    directory = Path(directory).absolute()
    cluster = Path(expected_cluster_directory).resolve(strict=True)
    with copy_lock(directory):
        identity, report = preview_identity(admin, record, directory, cluster)
        revoke_access(replace(admin, dbname=identity['database']), record, identity, report, cluster)
        save_private(directory / 'PREVIEW-ACCESS.json', {'format': 'familyfoto-preview-access-v1',
                     'state': 'closed', 'role_oid': report['role_oid'], 'database_oid': record['database_oid'],
                     'activation_allowed': False})
    return {'state': 'closed', 'activation_allowed': False, 'services_started': False}
