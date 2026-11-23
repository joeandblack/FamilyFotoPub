"""Explicit read-only inventory command; deliberately not wired into startup."""
import argparse
import json
from pathlib import Path
import sys

from .inventory import database_inventory, state_files
from .policy import UnclassifiedState
from .space import InsufficientSpace, SpaceGuard, StorageUnavailable, catalog_requirements


def main(argv=None):
    parser = argparse.ArgumentParser(description="Plan catalog recovery (does not create a backup).")
    parser.add_argument("--config-root", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--dbname", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--core-schema", default="public")
    parser.add_argument("--destination", type=Path, help="Existing backup directory for capacity preflight")
    parser.add_argument("--temporary-root", type=Path, help="Existing temporary-storage directory")
    parser.add_argument("--database-budget-bytes", type=int, help="Conservative database dump budget (not row count)")
    args = parser.parse_args(argv)
    capacity_args = (args.destination, args.temporary_root, args.database_budget_bytes)
    if any(value is not None for value in capacity_args):
        if any(value is None for value in capacity_args) or args.database_budget_bytes < 0:
            parser.error("Capacity preflight needs destination, temporary-root and a non-negative database-budget-bytes")
    import psycopg
    emit = lambda record: print(json.dumps(record, ensure_ascii=True))
    emit({"format": "familyfoto-catalog-plan-v1", "kind": "header", "is_backup": False})
    counts = {"files": 0, "tables": 0, "authoritative_bytes": 0}
    try:
        # libpq can use PGPASSFILE; never pass a password on the command line or
        # load config/local.json (which prepares folders as a side effect).
        with psycopg.connect(
            host=args.host, port=args.port, dbname=args.dbname, user=args.username,
            connect_timeout=5, application_name="familyfoto-backup-plan",
        ) as connection:
            for record in database_inventory(connection, args.core_schema):
                emit(record)
                counts["tables"] += 1
        for kind, root in (("config", args.config_root), ("data", args.data_root)):
            for record in state_files(root, kind):
                emit(record)
                counts["files"] += 1
                if record["category"] == "authoritative":
                    counts["authoritative_bytes"] += record["size"]
        if args.destination is not None:
            guard = SpaceGuard(catalog_requirements(
                args.destination, args.temporary_root, state_bytes=counts["authoritative_bytes"],
                database_budget_bytes=args.database_budget_bytes,
            ))
            for capacity in guard.check():
                emit({"kind": "capacity", **capacity.report()})
    except InsufficientSpace as error:
        for capacity in error.capacities:
            emit({"kind": "capacity", **capacity.report()})
        emit({"kind": "result", "status": "blocked", "is_backup": False, "reason": "insufficient_space"})
        return 1
    except StorageUnavailable:
        emit({"kind": "result", "status": "blocked", "is_backup": False, "reason": "storage_unavailable"})
        return 1
    except UnclassifiedState as error:
        emit({"kind": "result", "status": "blocked", "is_backup": False, "reason": str(error)})
        print("Inventory blocked by unclassified state. No backup was created.", file=sys.stderr)
        return 1
    except (OSError, ValueError, psycopg.Error):
        # Driver/filesystem errors can contain connection details and full paths.
        emit({"kind": "result", "status": "blocked", "is_backup": False})
        print("Inventory blocked: unknown state, inaccessible roots, or database error. "
              "No backup was created. Inspect with the inventory API for diagnostics.", file=sys.stderr)
        return 1
    emit({"kind": "result", "status": "inventoried", "is_backup": False,
          "capacity_checked": args.destination is not None, **counts})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
