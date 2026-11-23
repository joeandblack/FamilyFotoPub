"""Stage a non-login application role on an owned isolated recovery database.

The private credential journal precedes transactional role creation. No existing
role is adopted, no production configuration changes, and login stays disabled.
"""
from dataclasses import replace
import json
import os
from pathlib import Path
import re
import secrets
import uuid

from psycopg import sql

from ..backup.catalog import freeze_database
from ..backup.encrypted import read_bounded
from ..backup.media_copy import copy_lock
from ..backup.private_storage import create_private_directory
from ..backup.recovery import save_private
from ..backup.restore_database import RestoreFailed
from ..backup.space import directory_identity
from ..backup.volumes import volume_identity


FORMAT = 'familyfoto-recovery-role-v1'


def verify_staged_role(db, identity, report):
    """Read-only pre-activation verification; never repairs grants or enables login."""
    role = identity['role']
    row = db.execute("SELECT oid, shobj_description(oid, 'pg_authid'), rolcanlogin, rolsuper, "
        "rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, rolinherit "
        "FROM pg_roles WHERE rolname=%s", (role,)).fetchone()
    if (not row or row[0] != report.get('role_oid') or row[1] != FORMAT + ':' + identity['token']
            or any(row[2:]) or report.get('role') != role or report.get('database') != identity['database']
            or report.get('format') != FORMAT or report.get('state') != 'staged-no-login'
            or report.get('activation_allowed') is not False):
        raise RestoreFailed('Staged role identity or privileges changed')
    if db.execute('SELECT EXISTS (SELECT FROM pg_auth_members WHERE member=%s OR roleid=%s)',
                  (row[0], row[0])).fetchone()[0]:
        raise RestoreFailed('Staged role memberships changed')
    if db.execute('SELECT EXISTS (SELECT FROM pg_db_role_setting WHERE setrole=%s)', (row[0],)).fetchone()[0]:
        raise RestoreFailed('Staged role settings changed')
    if db.execute("SELECT EXISTS (SELECT FROM pg_shdepend WHERE refobjid=%s "
                  "AND refclassid='pg_authid'::regclass AND deptype='o')", (row[0],)).fetchone()[0]:
        raise RestoreFailed('Staged role unexpectedly owns database objects')
    if not db.execute('SELECT has_database_privilege(%s,current_database(),%s)', (role, 'CONNECT')).fetchone()[0]:
        raise RestoreFailed('Staged role cannot connect to its target database')
    if identity.get('access') not in (None, 'preview-select-only') or report.get('access') != identity.get('access'):
        raise RestoreFailed('Staged role access mode changed')
    schemas = [r[0] for r in db.execute("SELECT nspname FROM pg_namespace "
               "WHERE nspname NOT LIKE 'pg_%' AND nspname <> 'information_schema'")]
    if sorted(schemas) != sorted(report.get('schemas', [])):
        raise RestoreFailed('Staged database schemas changed')
    if identity.get('access') == 'preview-select-only':
        return {**verify_preview_privileges(db, role, schemas), 'role_oid': row[0], 'role': role,
                'login_disabled': True, 'activation_allowed': False}
    for schema in schemas:
        usage, create = db.execute('SELECT has_schema_privilege(%s,%s,%s), has_schema_privilege(%s,%s,%s)',
                                   (role, schema, 'USAGE', role, schema, 'CREATE')).fetchone()
        if not usage or create:
            raise RestoreFailed('Staged schema permissions changed')
    tables = db.execute("SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                        "WHERE n.nspname=ANY(%s) AND c.relkind IN ('r','p','v','m','f')", (schemas,)).fetchall()
    for (oid,) in tables:
        for permission in ('SELECT', 'INSERT', 'UPDATE', 'DELETE'):
            if not db.execute('SELECT has_table_privilege(%s,%s,%s)', (role, oid, permission)).fetchone()[0]:
                raise RestoreFailed('Staged table permissions changed')
    for (oid,) in db.execute("SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                            "WHERE n.nspname=ANY(%s) AND c.relkind='S'", (schemas,)).fetchall():
        for permission in ('USAGE', 'SELECT', 'UPDATE'):
            if not db.execute('SELECT has_sequence_privilege(%s,%s,%s)', (role, oid, permission)).fetchone()[0]:
                raise RestoreFailed('Staged sequence permissions changed')
    for (oid,) in db.execute("SELECT p.oid FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                            "WHERE n.nspname=ANY(%s) AND p.prokind<>'p'", (schemas,)).fetchall():
        if not db.execute('SELECT has_function_privilege(%s,%s,%s)', (role, oid, 'EXECUTE')).fetchone()[0]:
            raise RestoreFailed('Staged function permissions changed')
    return {'role_oid': row[0], 'role': role, 'login_disabled': True, 'activation_allowed': False}


def owned_target(db, record, cluster):
    """Bind grants to the exact sanitized database, never just its supplied name."""
    if Path(db.execute('SHOW data_directory').fetchone()[0]).resolve(strict=True) != cluster:
        raise RestoreFailed('Recovery cluster identity changed')
    row = db.execute("SELECT oid, shobj_description(oid, 'pg_database'), "
                     "datdba=(SELECT oid FROM pg_roles WHERE rolname=current_user) "
                     "FROM pg_database WHERE datname=current_database()").fetchone()
    if (not row or row[0] != record['database_oid'] or not row[2]
            or row[1] != 'familyfoto-recovery:' + record['ownership_token'] + ':sanitized'):
        raise RestoreFailed('Recovery database ownership or sanitization differs')
    settings = db.execute('SELECT setconfig FROM pg_db_role_setting WHERE setdatabase=%s AND setrole=0',
                          (row[0],)).fetchone()
    public = db.execute("SELECT EXISTS (SELECT FROM pg_database d, "
        "LATERAL aclexplode(coalesce(d.datacl,acldefault('d',d.datdba))) a "
        "WHERE d.oid=%s AND a.grantee=0 AND a.privilege_type='CONNECT')", (row[0],)).fetchone()[0]
    if not settings or 'default_transaction_read_only=on' not in settings[0] or public:
        raise RestoreFailed('Target is no longer an isolated read-only recovery database')


def stage_role(db, identity):
    role = identity['role']
    existing = db.execute("SELECT oid, shobj_description(oid, 'pg_authid'), rolcanlogin, rolsuper, "
        "rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, rolinherit FROM pg_roles WHERE rolname=%s", (role,)).fetchone()
    comment = FORMAT + ':' + identity['token']
    if existing:
        memberships = db.execute('SELECT count(*) FROM pg_auth_members WHERE member=%s OR roleid=%s',
                                 (existing[0], existing[0])).fetchone()[0]
        settings = db.execute('SELECT count(*) FROM pg_db_role_setting WHERE setrole=%s', (existing[0],)).fetchone()[0]
        owns_objects = db.execute("SELECT EXISTS (SELECT FROM pg_shdepend WHERE refobjid=%s "
                                  "AND refclassid='pg_authid'::regclass AND deptype='o')", (existing[0],)).fetchone()[0]
        if existing[1] != comment or any(existing[2:]) or memberships or settings or owns_objects:
            raise RestoreFailed('Staged application role changed; refusing to adopt it')
    else:
        db.execute(sql.SQL('CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
                           'NOREPLICATION NOBYPASSRLS NOINHERIT PASSWORD {}').format(
                               sql.Identifier(role), sql.Literal(identity['password'])))
        db.execute(sql.SQL('COMMENT ON ROLE {} IS {}').format(sql.Identifier(role), sql.Literal(comment)))
    # Reapply the owned staged password after a retry, but never enable login.
    db.execute(sql.SQL('ALTER ROLE {} PASSWORD {}').format(sql.Identifier(role), sql.Literal(identity['password'])))
    db.execute(sql.SQL('REVOKE ALL PRIVILEGES ON DATABASE {} FROM {}').format(
        sql.Identifier(identity['database']), sql.Identifier(role)))
    db.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(
        sql.Identifier(identity['database']), sql.Identifier(role)))
    schemas = [row[0] for row in db.execute("SELECT nspname FROM pg_namespace "
               "WHERE nspname NOT LIKE 'pg_%' AND nspname <> 'information_schema'")]
    for schema in schemas:
        arguments = (sql.Identifier(schema), sql.Identifier(role))
        # Older PostgreSQL defaults permit PUBLIC schema creation. A restored
        # candidate must not inherit that path to DDL through the application role.
        db.execute(sql.SQL('REVOKE CREATE ON SCHEMA {} FROM PUBLIC').format(sql.Identifier(schema)))
        db.execute(sql.SQL('REVOKE ALL PRIVILEGES ON SCHEMA {} FROM {}').format(*arguments))
        db.execute(sql.SQL('REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA {} FROM {}').format(*arguments))
        db.execute(sql.SQL('REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA {} FROM {}').format(*arguments))
        db.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO {}').format(*arguments))
        db.execute(sql.SQL('GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {} TO {}').format(*arguments))
        db.execute(sql.SQL('GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA {} TO {}').format(*arguments))
        db.execute(sql.SQL('GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA {} TO {}').format(*arguments))
    return schemas


def restrict_preview_role(db, role, schemas):
    """Remove effective write paths on an isolated recovery database, including PUBLIC grants."""
    database = db.execute('SELECT current_database()').fetchone()[0]
    db.execute(sql.SQL('REVOKE CREATE, TEMPORARY ON DATABASE {} FROM PUBLIC').format(sql.Identifier(database)))
    for schema in schemas:
        schema_name, role_name = sql.Identifier(schema), sql.Identifier(role)
        for objects in ('TABLES', 'SEQUENCES', 'ROUTINES'):
            for grantee in (sql.SQL('PUBLIC'), role_name):
                db.execute(sql.SQL('REVOKE ALL PRIVILEGES ON ALL {} IN SCHEMA {} FROM {}').format(
                    sql.SQL(objects), schema_name, grantee))
        db.execute(sql.SQL('GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}').format(schema_name, role_name))
    # The shared visibility predicate executes with caller privileges. Do not
    # grant arbitrary restored routines, particularly security-definer functions.
    helper = db.execute("SELECT p.oid, p.prosecdef FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                        "WHERE n.nspname='public' AND p.proname='familyfoto_can_see' "
                        "AND p.proargtypes='25 25'::oidvector AND p.prokind='f'").fetchone()
    if helper:
        if helper[1]:
            raise RestoreFailed('Preview visibility helper must use caller privileges')
        db.execute(sql.SQL('GRANT EXECUTE ON FUNCTION public.familyfoto_can_see(text,text) TO {}').format(sql.Identifier(role)))


def verify_preview_privileges(db, role, schemas):
    """Effective privileges, not just ACL entries: PUBLIC must not grant writes."""
    if db.execute('SELECT has_database_privilege(%s,current_database(),%s)', (role, 'CREATE,TEMP')).fetchone()[0]:
        raise RestoreFailed('Preview role can create database objects')
    for schema in schemas:
        usage, create = db.execute('SELECT has_schema_privilege(%s,%s,%s),has_schema_privilege(%s,%s,%s)',
                                   (role, schema, 'USAGE', role, schema, 'CREATE')).fetchone()
        if not usage or create:
            raise RestoreFailed('Preview schema permissions changed')
    if db.execute("SELECT EXISTS (SELECT FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                  "WHERE n.nspname=ANY(%s) AND c.relkind IN ('r','p','v','m','f') AND "
                  "(NOT has_table_privilege(%s,c.oid,'SELECT') OR "
                  "has_table_privilege(%s,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')))",
                  (schemas, role, role)).fetchone()[0]:
        raise RestoreFailed('Preview table privileges are not SELECT-only')
    if db.execute("SELECT EXISTS (SELECT FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                  "WHERE n.nspname=ANY(%s) AND CASE WHEN c.relkind='S' "
                  "THEN has_sequence_privilege(%s,c.oid,'USAGE,SELECT,UPDATE') ELSE false END)",
                  (schemas, role)).fetchone()[0]:
        raise RestoreFailed('Preview role can use sequences')
    if db.execute("SELECT EXISTS (SELECT FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                  "WHERE n.nspname=ANY(%s) AND has_function_privilege(%s,p.oid,'EXECUTE') AND NOT "
                  "(n.nspname='public' AND p.proname='familyfoto_can_see' AND p.proargtypes='25 25'::oidvector "
                  "AND p.prokind='f' AND NOT p.prosecdef))", (schemas, role)).fetchone()[0]:
        raise RestoreFailed('Preview role can execute an unapproved routine')
    return {'select_only': True, 'services_started': False}


def prepare_recovery_role(admin, record, directory, *, expected_cluster_directory, resume=False, preview=False):
    """Provision credentials only; caller must explicitly activate later.

    Archives are trusted executable schemas. Grants do not sandbox restored
    security-definer functions; use the separately provisioned recovery cluster.
    """
    if (admin.dbname != 'postgres' or not isinstance(record, dict) or record.get('verified') is not True
            or record.get('activation_allowed') is not False
            or not re.fullmatch('familyfoto_recovery_[0-9a-f]{32}', str(record.get('database', '')))
            or not re.fullmatch('[0-9a-f]{32}', str(record.get('ownership_token', '')))
            or type(record.get('database_oid')) is not int):
        raise RestoreFailed('A verified owned recovery database record is required')
    cluster = Path(expected_cluster_directory).resolve(strict=True)
    if record.get('cluster_directory') != str(cluster):
        raise RestoreFailed('Recovery record belongs to a different cluster')
    directory = Path(os.path.abspath(directory))
    if directory.is_relative_to(cluster) or cluster.is_relative_to(directory):
        raise ValueError('Credential staging must be separate from PostgreSQL files')
    target = replace(admin, dbname=record['database'])
    if not resume:
        create_private_directory(directory)
    directory_identity(directory)
    with copy_lock(directory):
        with freeze_database(target) as db:
            owned_target(db, record, cluster)
            expected = {'format': FORMAT, 'database': record['database'], 'database_oid': record['database_oid'],
                        'database_token': record['ownership_token'], 'cluster_directory': str(cluster),
                        'endpoint': [admin.host, admin.port, admin.user],
                        'directory': list(directory_identity(directory)), 'volume': volume_identity(directory)}
            if preview:
                expected['access'] = 'preview-select-only'
            journal = directory / 'credentials.json'
            if resume:
                identity = json.loads(read_bounded(journal, 65536))
                if (any(identity.get(k) != v for k, v in expected.items())
                        or identity.get('access') != expected.get('access')
                        or not re.fullmatch('familyfoto_app_[0-9a-f]{32}', str(identity.get('role', '')))
                        or not re.fullmatch('[0-9a-f]{32}', str(identity.get('token', '')))
                        or not isinstance(identity.get('password'), str) or len(identity['password']) < 48):
                    raise RestoreFailed('Credential staging ownership differs')
            else:
                identity = {**expected, 'role': 'familyfoto_app_' + uuid.uuid4().hex,
                            'token': uuid.uuid4().hex, 'password': secrets.token_urlsafe(48)}
                save_private(journal, identity)
            schemas = stage_role(db, identity)
            if preview:
                restrict_preview_role(db, identity['role'], schemas)
                verify_preview_privileges(db, identity['role'], schemas)
            role_oid = db.execute('SELECT oid FROM pg_roles WHERE rolname=%s', (identity['role'],)).fetchone()[0]
            # Commit before publishing success; failure keeps a retryable NOLOGIN
            # role, or rolls back role creation without losing its prior intent.
        report = {'format': FORMAT, 'state': 'staged-no-login', 'role': identity['role'],
                  'role_oid': role_oid, 'database': record['database'], 'schemas': schemas, 'activation_allowed': False}
        if preview:
            report['access'] = 'preview-select-only'
        if json.loads(read_bounded(directory / 'credentials.json', 65536)) != identity:
            raise RestoreFailed('Credential journal changed before completion')
        save_private(directory / 'ROLE-REPORT.json', report)
    return report
