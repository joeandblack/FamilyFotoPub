"""Explicit encrypted database snapshots, independent of live app configuration.

Not a complete catalog recovery point: file-side state and write admission still
need integration. No installer, schema upgrade or service lifecycle is invoked.
"""
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import subprocess
import threading

from cryptography.fernet import Fernet
import psycopg
from psycopg import sql

from .encrypted import seal_stream, storage_budget
from .inventory import inspect_snapshot
from .space import Requirement, SpaceGuard
from .compatibility import database_evidence
from ..release import metadata as release_metadata


class CaptureFailed(RuntimeError):
    pass


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: int
    dbname: str
    user: str
    password: str | None = field(default=None, repr=False)

    def __post_init__(self):
        if self.host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Initial database capture supports loopback only")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("Invalid database port")
        if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]{0,62}", self.dbname):
            raise ValueError("Explicit database name required, not a connection string")
        if not self.user or "\0" in self.user:
            raise ValueError("Explicit database user required")

    def connection(self):
        value = {"host": self.host, "port": self.port, "dbname": self.dbname, "user": self.user,
                 "connect_timeout": 5, "options": "-c default_transaction_read_only=on"}
        if self.password is not None:
            value["password"] = self.password
        return value

    def arguments(self):
        return [f"--host={self.host}", f"--port={self.port}", f"--username={self.user}",
                f"--dbname={self.dbname}", "--no-password"]

    def environment(self):
        # Keep an explicitly supplied password file, not inherited service/host/
        # option overrides. No credential ever appears in subprocess arguments.
        env = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
        if "PGPASSFILE" in os.environ:
            env["PGPASSFILE"] = os.environ["PGPASSFILE"]
        if self.password is not None:
            env["PGPASSWORD"] = self.password
        env["PGOPTIONS"] = "-c default_transaction_read_only=on"
        return env


def capture_database(endpoint, pg_dump, directory, key, *, estimated_bytes, core_schema="public",
                     guard_factory=SpaceGuard, progress=None):
    """Export one snapshot with exact table counts, encrypt, read back, then publish.

    Failed/partial pg_dump output is never a completed artifact. Restart a failed
    dump in a new directory with a fresh snapshot; never append across snapshots.
    """
    pg_dump = Path(pg_dump).absolute()
    if not pg_dump.is_file():
        raise ValueError("An explicit pg_dump executable is required")
    Fernet(key)
    directory = Path(os.path.abspath(directory))
    if directory.exists() or directory.is_symlink():
        raise FileExistsError("Database capture requires a new artifact directory")
    guard_factory([Requirement("destination", directory.parent, storage_budget(estimated_bytes))]).check()
    with psycopg.connect(**endpoint.connection()) as db:
        db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        db.execute("SET LOCAL statement_timeout = '60s'")
        db.execute("SET LOCAL lock_timeout = '2s'")
        tables = list(inspect_snapshot(db, core_schema))
        compatibility = database_evidence(db, tables)
        counts = []
        for table in tables:
            count = db.execute(sql.SQL("SELECT count(*) FROM {}.{}").format(
                sql.Identifier(table["schema"]), sql.Identifier(table["name"]))).fetchone()[0]
            counts.append({"schema": table["schema"], "table": table["name"], "rows": count})
        snapshot = db.execute("SELECT pg_export_snapshot()").fetchone()[0]
        version = db.execute("SHOW server_version_num").fetchone()[0]
        command = [str(pg_dump), *endpoint.arguments(), "--format=custom", "--compress=0",
                   "--lock-wait-timeout=10000", f"--snapshot={snapshot}"]
        from .diagnostics import ProcessDiagnostics
        diagnostics = ProcessDiagnostics()
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                              env=endpoint.environment(), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) as process:
            reader = threading.Thread(target=diagnostics.drain, args=(process.stderr,), daemon=True)
            reader.start()

            def finish():
                result = process.wait(timeout=30)
                reader.join(timeout=5)
                if result or reader.is_alive() or diagnostics.has_output:
                    raise diagnostics.failure(CaptureFailed, "Database export failed or reported a warning; artifact was not published")

            try:
                return seal_stream(process.stdout, directory, key, estimated_bytes=estimated_bytes,
                                   guard_factory=guard_factory, finish=finish, progress=progress,
                                   metadata={"kind": "postgres-custom-dump", "server_version": version,
                                             "tables": counts, "media_included": False,
                                             "compatibility": compatibility, "application_release": release_metadata(),
                                             "file_state_included": False})
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=10)
                reader.join(timeout=5)
