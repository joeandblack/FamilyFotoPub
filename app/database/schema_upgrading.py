"""Database schema upgrading, separate from media or application data migrations."""
from dataclasses import dataclass
from pathlib import Path
import re
from ..config import ROOT
from . import locks as locks

MIGRATION_LOCK = locks.SCHEMA_MIGRATION
MIGRATION_WORKER_LOCKS = locks.MEDIA_WORKERS
MIGRATION_FACE_LOCKS = locks.FACE_WORKERS
MIGRATION_PEOPLE_LOCKS = locks.PEOPLE_WORKERS
MIGRATION_REVIEW_LOCKS = (locks.PEOPLE_CORRECTION,)

# Preserve historical admission policies; future upgrades default to all guards.
# Version 25 only adds independent authentication tables. No worker contracts,
# media rows, or foreign keys change, so active scans need not pause.
# Version 26 adds album display order only; no media worker contracts change.
# Version 27 adds independent collection membership; no media/face rows change.
# Version 28 changes collection hierarchy only, independently of media workers.
# Version 29 adds album presentation preferences only.
# Version 30 stores collection links only, without touching recognition records.
# Version 31 adds collection presentation order, independently of workers.
# Version 32 separates collection visibility/root order from its parent membership.
# Version 33 stores album cover presentation only.
# Version 34 adds collection cover references; no media contents change.
# Version 35 adds an independent settings table; no workers are affected.
# Version 36 adds media ownership/visibility columns, so media workers must be idle.
NO_WORKER_GUARD = frozenset((*range(1, 8), 9, 10, 12, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35))
MEDIA_ONLY = frozenset((8, 11, 13, 14, 20, 21, 22, 23, 36))


@dataclass(frozen=True)
class Migration:
    version: int
    path: Path

    def guards(self):
        if self.version in NO_WORKER_GUARD:
            return ()
        if self.version in MEDIA_ONLY:
            return MIGRATION_WORKER_LOCKS
        if self.version == 15:
            return (*MIGRATION_WORKER_LOCKS, *MIGRATION_FACE_LOCKS)
        guards = (*MIGRATION_WORKER_LOCKS, *MIGRATION_PEOPLE_LOCKS)
        return guards if self.version in (16, 17) else (*guards, *MIGRATION_REVIEW_LOCKS)


def discover(directory):
    migrations = []
    for path in Path(directory).glob('*.sql'):
        match = re.fullmatch(r'(\d{3})_[a-z0-9_]+\.sql', path.name)
        if not match:
            raise ValueError(f'Invalid migration filename: {path.name}')
        migrations.append(Migration(int(match[1]), path))
    migrations.sort(key=lambda item: item.version)
    if not migrations or [m.version for m in migrations] != list(range(1, len(migrations)+1)):
        raise ValueError('Migrations must have unique, consecutive versions starting at 001')
    return migrations


def applied_versions(db):
    # Use a catalog query so a startup waiting on another transaction sees its
    # newly created version table, rather than a cached missing-name lookup.
    if not db.execute("SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_class "
                      "WHERE relname='schema_versions' AND pg_catalog.pg_table_is_visible(oid)) present").fetchone()['present']:
        return set()
    return {row['version'] for row in db.execute('SELECT version FROM schema_versions').fetchall()}


def require_catalog_backup(db, applied, pending):
    raise RuntimeError('Existing catalog upgrade requires a verified offline backup. '
                       'Stop FamilyFoto and its workers, connect the backup disk, and follow '
                       'docs/BACKUP.md. Run installer/upgrade-catalog.py --mode plan with '
                       '--config-root and --data-root first. Apply requires a recovery key and backup destination; '
                       'there is no skip-backup override. No schema changes were applied.')


def migrate(connect=None, directory=None, *, before_upgrade=require_catalog_backup):
    """Apply SQL transactionally; existing catalogs require an admission callback.

    Only the offline backup coordinator supplies a production callback. An
    injected connection does not bypass the default backup requirement.
    """
    if before_upgrade is None:
        before_upgrade = require_catalog_backup
    if connect is None:
        from . import connection
        connect = connection
    migrations = discover(directory or ROOT / 'databases')
    versions = {item.version for item in migrations}
    with connect() as db:
        applied = applied_versions(db)
        if applied - versions:
            raise RuntimeError('Database schema is newer than this application; upgrade the code')
        if applied == versions:
            return []
        db.execute("SET LOCAL lock_timeout='5s'")
        db.execute('SELECT pg_advisory_xact_lock(%s)', (MIGRATION_LOCK,))
        # Another server may have completed the upgrade while this one waited.
        applied = applied_versions(db)
        if applied - versions:
            raise RuntimeError('Database schema is newer than this application; upgrade the code')
        pending = [item for item in migrations if item.version not in applied]
        held = set()
        for item in pending:
            for key in item.guards():
                if key not in held:
                    if not db.execute('SELECT pg_try_advisory_xact_lock(%s) available', (key,)).fetchone()['available']:
                        raise RuntimeError(f'Pause media operations before applying {item.path.name}')
                    held.add(key)
        if pending and before_upgrade is not None:
            # Only the schema being migrated counts: the People catalog gets its own empty
            # schema beside the main catalog, and must not look like an upgrade of it.
            existing = bool(applied) or db.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_class c "
                "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname = current_schema() "
                "AND c.relkind IN ('r','p','v','m','S','f')) present").fetchone()['present']
            if existing:
                before_upgrade(db, applied, pending)
        for item in pending:
            db.execute(item.path.read_text(encoding='utf-8'))
            if item.version not in applied_versions(db):
                raise RuntimeError(f'Migration did not record its version: {item.path.name}')
        return [item.version for item in pending]


if __name__ == '__main__':
    print('Applied schema versions:', migrate())
