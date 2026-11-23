"""Transactional database half of recovery activation under offline admission.

The filesystem coordinator must hold the matching maintenance lease for this
entire transaction. These helpers never open a connection or commit a transaction.
"""
from pathlib import Path

from psycopg import sql

from ..backup.identity import read_record
from ..backup.restore_database import RestoreFailed
from ..backup.retirement import successor_id
from .credentials import FORMAT as ROLE_FORMAT, owned_target, verify_staged_role


def activation_comment(database, intent_id):
    return 'familyfoto-recovery:' + database['ownership_token'] + ':activated:' + successor_id(intent_id)


def require_activation_gate(data_root, maintenance_id):
    successor_id(maintenance_id)
    try:
        gate = read_record(Path(data_root) / '.recovery-admission/maintenance.json')
    except (OSError, ValueError) as error:
        raise RestoreFailed('Recovery activation maintenance is missing or invalid') from error
    if gate != {'id': maintenance_id, 'operation': 'restore', 'state': 'in-progress'}:
        raise RestoreFailed('Recovery activation maintenance ownership changed')


def activate_database(db, database, credentials, role_report, cluster, intent_id, *,
                      data_root, maintenance_id):
    require_activation_gate(data_root, maintenance_id)
    successor_id(intent_id)
    owned_target(db, database, cluster)
    verify_staged_role(db, credentials, role_report)
    if credentials.get('access') is not None:
        raise RestoreFailed('SELECT-only preview credentials cannot activate recovery')
    if (credentials['database'] != database['database'] or credentials['database_oid'] != database['database_oid']
            or credentials['database_token'] != database['ownership_token']):
        raise RestoreFailed('Activation credentials belong to a different database')
    db.execute(sql.SQL('ALTER DATABASE {} SET default_transaction_read_only=off').format(
        sql.Identifier(database['database'])))
    db.execute(sql.SQL("ALTER ROLE {} LOGIN VALID UNTIL 'infinity'").format(sql.Identifier(credentials['role'])))
    db.execute(sql.SQL('COMMENT ON DATABASE {} IS {}').format(
        sql.Identifier(database['database']), sql.Literal(activation_comment(database, intent_id))))
    require_activation_gate(data_root, maintenance_id)


def rollback_database(db, database, credentials, role_report, cluster, intent_id, *,
                      data_root, maintenance_id):
    """Return an interrupted activation to NOLOGIN before any target writes.

    Only the exact still-gated activation is accepted. Do not call this after
    admission was released: that is a new recovery, not a rollback.
    """
    require_activation_gate(data_root, maintenance_id)
    if Path(db.execute('SHOW data_directory').fetchone()[0]).resolve(strict=True) != cluster:
        raise RestoreFailed('Recovery cluster identity changed')
    original = 'familyfoto-recovery:' + database['ownership_token'] + ':sanitized'
    row = db.execute("SELECT oid, shobj_description(oid, 'pg_database'), "
                     "datdba=(SELECT oid FROM pg_roles WHERE rolname=current_user) "
                     "FROM pg_database WHERE datname=current_database()").fetchone()
    if not row or row[0] != database['database_oid'] or not row[2]:
        raise RestoreFailed('Recovery database ownership changed')
    if row[1] == original:
        owned_target(db, database, cluster)
        verify_staged_role(db, credentials, role_report)
        return
    if row[1] != activation_comment(database, intent_id):
        raise RestoreFailed('Database does not belong to this interrupted activation')
    role = db.execute("SELECT oid, shobj_description(oid, 'pg_authid'), rolsuper, rolcreatedb, "
                      "rolcreaterole, rolreplication, rolbypassrls, rolinherit FROM pg_roles WHERE rolname=%s",
                      (credentials['role'],)).fetchone()
    if (not role or role[0] != role_report['role_oid']
            or role[1] != ROLE_FORMAT + ':' + credentials['token'] or any(role[2:])):
        raise RestoreFailed('Activation role identity changed')
    db.execute(sql.SQL('ALTER ROLE {} NOLOGIN').format(sql.Identifier(credentials['role'])))
    db.execute(sql.SQL('ALTER DATABASE {} SET default_transaction_read_only=on').format(
        sql.Identifier(database['database'])))
    db.execute(sql.SQL('COMMENT ON DATABASE {} IS {}').format(
        sql.Identifier(database['database']), sql.Literal(original)))
    owned_target(db, database, cluster)
    verify_staged_role(db, credentials, role_report)
    require_activation_gate(data_root, maintenance_id)
