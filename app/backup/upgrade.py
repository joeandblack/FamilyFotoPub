"""Explicit offline schema upgrades protected by a freshly verified catalog set."""
from contextlib import contextmanager
import hashlib
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from .admission import maintenance, MaintenanceBlocked
from .catalog import catalog_capture_scope, verify_catalog
from .space import SpaceGuard
from ..database.schema_upgrading import applied_versions, discover, migrate


def plan_upgrade(endpoint, migrations_directory):
    """Read schema state in a read-only transaction; never initialize app paths."""
    hashes = {item.version: hashlib.sha256(item.path.read_bytes()).hexdigest()
              for item in discover(migrations_directory)}
    with psycopg.connect(**endpoint.connection(), row_factory=dict_row) as db:
        applied = applied_versions(db)
        if applied - hashes.keys():
            raise ValueError('Database is newer than the supplied application')
        existing = db.execute("SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n "
            "ON n.oid=c.relnamespace WHERE n.nspname NOT IN ('pg_catalog','information_schema') "
            "AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp%' "
            "AND c.relkind IN ('r','p','v','m','S','f')) present").fetchone()['present']
    pending = sorted(hashes.keys() - applied)
    return {'state': 'pending' if pending else 'unchanged', 'applied_versions': sorted(applied),
            'pending_versions': pending, 'script_hashes': hashes,
            'existing_catalog': bool(applied or existing),
            'backup_required': bool(pending and (applied or existing))}


def upgrade_catalog(endpoint, pg_dump, config_root, data_root, directory, key, *,
                    migrations_directory, database_budget, offline_confirmed=False,
                    catalog_only_confirmed=False, guard_factory=SpaceGuard, progress=None):
    """Never start services; callers must stop all application/external writers.

    The catalog-only acknowledgement excludes upgrades that alter physical media.
    Those need a separately designed full-media protection and upgrade procedure.
    """
    if offline_confirmed is not True or catalog_only_confirmed is not True:
        raise MaintenanceBlocked('Offline and catalog-only upgrade confirmation are required')
    migrations_directory = Path(migrations_directory)
    plan = plan_upgrade(endpoint, migrations_directory)
    hashes = plan['script_hashes']
    applied = set(plan['applied_versions'])
    if not plan['pending_versions']:
        return {'state': 'unchanged', 'applied': [], 'backup_created': False}

    @contextmanager
    def connect():
        options = endpoint.connection()
        options['options'] = '-c default_transaction_read_only=off'
        with psycopg.connect(**options, row_factory=dict_row) as db:
            yield db

    def gate(db, current, pending):
        if current != applied:
            raise MaintenanceBlocked('Schema changed since upgrade planning; make a new recovery point')
        if db.execute("SELECT count(*) AS count FROM pg_stat_activity WHERE datname=current_database() "
                      "AND pid<>pg_backend_pid() AND backend_type='client backend'").fetchone()['count']:
            raise MaintenanceBlocked('Stop all other database clients before applying upgrades')
        if {item.version: hashlib.sha256(item.path.read_bytes()).hexdigest()
                for item in discover(migrations_directory)} != hashes:
            raise MaintenanceBlocked('Upgrade files changed after planning')
        if verify_catalog(directory, key) != captured:
            raise MaintenanceBlocked('Pre-upgrade recovery point changed')

    if not plan['existing_catalog']:
        with maintenance(data_root, 'upgrade'):
            def refuse_changed(db, current, pending):
                raise MaintenanceBlocked('Fresh database changed; a catalog backup is now required')
            changes = migrate(connect, migrations_directory, before_upgrade=refuse_changed)
        return {'state': 'complete', 'applied': changes, 'backup_created': False}
    with catalog_capture_scope(endpoint, pg_dump, config_root, data_root, directory, key,
            database_budget=database_budget, offline_confirmed=True,
            guard_factory=guard_factory, progress=progress) as captured:
        if progress:
            progress({'phase': 'upgrade'})
        changes = migrate(connect, migrations_directory, before_upgrade=gate)
    return {'state': 'complete', 'applied': changes, 'backup_created': True,
            'capture_id': captured['id'], 'media_included': False, 'jobs_resumed': False}
