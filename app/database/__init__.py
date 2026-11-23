"""Database access; schema upgrades are implemented separately."""
from contextlib import contextmanager
import psycopg
from psycopg.rows import dict_row
from ..config import load_config, ROOT
from .pool import pooled_connection, close_pools  # noqa: F401  (close_pools is re-exported: app.database.close_pools)


@contextmanager
def connection(settings=None):
    admit_writer()
    config = load_config()['database'] if settings is None else settings
    with pooled_connection(config) as db:
        yield db


@contextmanager
def dedicated_connection(settings=None):
    """Use when backend lifetime itself is a lease, not just advisory locks."""
    admit_writer()
    config = load_config()['database'] if settings is None else settings
    with psycopg.connect(**config, row_factory=dict_row, connect_timeout=5) as db:
        yield db


def admit_writer():
    # Explicit People/search-path settings must not bypass load_config's lease.
    # Offline backup coordinators deliberately use their own direct connection.
    from ..backup.admission import hold_process_lease
    from ..config import DATA
    hold_process_lease(DATA)


def migrate():
    """Compatibility entry point for existing startup and administration scripts."""
    from .schema_upgrading import migrate as run_migrations
    from .schema_upgrading import require_catalog_backup
    return run_migrations(connect=connection, directory=ROOT / 'databases',
                          before_upgrade=require_catalog_backup)

