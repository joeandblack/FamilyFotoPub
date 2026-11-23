"""Read-only planning inventory, not a backup or a consistent recovery point."""
from dataclasses import asdict
import os
from pathlib import Path
import stat

from .policy import UnclassifiedState, check_directory, classify_file, classify_table, schema_kind


def state_files(root: Path, root_kind: str, *, on_unclassified=None):
    """Stream metadata without following links or reading sensitive file contents.

    Callers must treat every emitted record as provisional until iteration ends.
    An active writer can change files during planning; capture needs a separate gate.
    """
    if root_kind not in {"config", "data"}:
        raise UnclassifiedState(f"Unclassified state root: {root_kind}")
    root = Path(os.path.abspath(root))
    for parent in (*reversed(root.parents), root):
        if parent.is_symlink() or parent.is_junction():
            raise ValueError("Linked state roots are not supported")
    if not root.is_dir():
        raise ValueError(f"Missing state root: {root_kind}")
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                info = entry.stat(follow_symlinks=False)
                if entry.is_symlink() or path.is_junction():
                    raise ValueError("Linked state entries are not supported")
                if stat.S_ISDIR(info.st_mode):
                    relative = path.relative_to(root).as_posix()
                    try:
                        check_directory(root_kind, relative)
                    except UnclassifiedState:
                        if on_unclassified is None:
                            raise
                        on_unclassified(root_kind, relative)
                    pending.append(path)
                    continue
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("Special state files are not supported")
                relative = path.relative_to(root).as_posix()
                try:
                    treatment = classify_file(root_kind, relative)
                except UnclassifiedState:
                    if on_unclassified is None:
                        raise
                    on_unclassified(root_kind, relative)
                    continue
                yield {
                    "kind": "file", "root": root_kind, "path": relative,
                    "size": info.st_size, "mtime_ns": info.st_mtime_ns,
                    **asdict(treatment),
                }


def database_inventory(connection, core_schema: str = "public"):
    """Inspect metadata in a fresh connection, using a read-only transaction.

    No application configuration, startup, schema upgrade, row payload or media
    lookup is used. Counts are estimates, not a restore-validation baseline.
    """
    # Never join an existing write transaction supplied by an application caller.
    from psycopg.pq import TransactionStatus
    if connection.info.transaction_status != TransactionStatus.IDLE:
        raise ValueError("Inventory requires an idle dedicated database connection")
    with connection.transaction():
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        connection.execute("SET LOCAL statement_timeout = '30s'")
        connection.execute("SET LOCAL lock_timeout = '2s'")
        yield from inspect_snapshot(connection, core_schema)


def inspect_snapshot(connection, core_schema="public"):
    """Internal metadata inspection inside the caller's read-only snapshot."""
    schemas = connection.execute("""
            SELECT nspname FROM pg_catalog.pg_namespace
            WHERE nspname NOT LIKE 'pg\\_%' ESCAPE '\\'
              AND nspname <> 'information_schema' ORDER BY nspname
    """).fetchall()
    for (schema,) in schemas:
        schema_kind(schema, core_schema)
    rows = connection.execute("""
            SELECT n.nspname, c.relname, c.relkind, c.reltuples::bigint
            FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname NOT LIKE 'pg\\_%' ESCAPE '\\'
              AND n.nspname <> 'information_schema'
              AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
            ORDER BY n.nspname, c.relname
    """).fetchall()
    for schema, name, kind, estimate in rows:
        if kind not in {"r", "p"}:
            raise UnclassifiedState(f"Unclassified database relation: {schema}.{name} ({kind})")
        treatment = classify_table(schema, name, core_schema)
        yield {
            "kind": "table", "schema": schema, "name": name,
            "estimated_rows": max(0, estimate), **asdict(treatment),
        }
