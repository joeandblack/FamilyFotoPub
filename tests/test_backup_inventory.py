"""Inventory tests use synthetic files and a fake database, never live settings."""
import json
from pathlib import Path
import re
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from psycopg.pq import TransactionStatus

from app.backup.inventory import database_inventory, state_files
from app.backup.policy import (
    CORE_TABLES, PEOPLE_TABLES, UnclassifiedState, classify_file, classify_table,
)

ROOT = Path(__file__).resolve().parents[1]
PROFILE = "face_native_" + "a" * 32


def test_sql_tables_require_explicit_policy():
    # Discovery here is a regression tripwire, not the runtime classification
    # source. PostgreSQL metadata is authoritative at runtime.
    pattern = r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-z_]+)"
    for source in (ROOT / "databases").glob("*.sql"):
        for table in re.findall(pattern, source.read_text(), re.I):
            assert table in CORE_TABLES, (source.name, table)
    for source in (ROOT / "face_detect/storage").glob("*.sql"):
        for table in re.findall(pattern, source.read_text(), re.I):
            assert table in PEOPLE_TABLES, (source.name, table)


@pytest.mark.parametrize("schema,table", [
    ("public", "new_feature"), (PROFILE, "new_feature"),
    ("unregistered", "media"), ("face_native_not_a_hash", "people"),
    ("public", "native_matching_policies"),
])
def test_unknown_database_state_blocks(schema, table):
    with pytest.raises(UnclassifiedState):
        classify_table(schema, table)


@pytest.mark.parametrize('name', ['read-only-preview.json', 'limited-edit-preview.json', 'readonly-canary.txt'])
def test_files_the_previews_leave_in_settings_are_not_captured(tmp_path, name):
    assert classify_file('config', name).category == 'transient'
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config/local.json').write_text('{}')
    (tmp_path / 'config' / name).write_text('{}')
    assert [row['path'] for row in state_files(tmp_path / 'config', 'config') if row['category'] == 'authoritative'] == ['local.json']


def test_windows_setup_records_pass_preflight_and_stay_out_of_capture(tmp_path):
    from app.backup.admin_runs import unclassified_state
    from app.backup.state_files import state_inventory

    roots = {kind: tmp_path / kind for kind in ('data', 'config')}
    roots['config'].mkdir()
    (roots['config'] / 'local.json').write_text('{}')
    setup = roots['data'] / 'setup'
    setup.mkdir(parents=True)
    for name in ('install.json', 'install.json.previous', 'install.json.new', 'install.lock'):
        (setup / name).touch()
    assert unclassified_state(roots) == []
    assert [(row['root'], row['path']) for row in state_inventory(roots)] == [('config', 'local.json')]
    (setup / 'future.json').touch()
    assert unclassified_state(roots) == [('data/setup', 1)]
    with pytest.raises(UnclassifiedState):
        state_inventory(roots)


def test_other_settings_files_stay_unknown():
    for name in ('other-preview.json', 'read-only-preview.json.bak', 'preview/read-only-preview.json'):
        with pytest.raises(UnclassifiedState):
            classify_file('config', name)


def test_files_a_stop_or_a_thumbnail_repair_leaves_have_rules():
    # Every server stop leaves the first, after a plan made while it ran could have seen it.
    stop = classify_file('data', f'face_detect/profiles/{"a" * 32}/DESKTOP-PREVIEW-STOP')
    assert stop.category == 'transient'
    assert classify_file('data', 'thumbnail-repair-2026-11-05T01-51-44+00-00.json').category == 'excluded'


def test_native_and_preserved_catalog_tables_are_kept():
    for schema in (PROFILE, "auto_eval_" + "a" * 32):
        for table in CORE_TABLES | PEOPLE_TABLES:
            assert classify_table(schema, table).category == "authoritative"
    assert "invalidate" in classify_table("public", "auth_sessions").restore


@pytest.mark.parametrize("path", ["../local.json", "/local.json", "tls/../local.json",
                                 "tls\\ca.pem", "C:/local.json", "local.json:secret"])
def test_unsafe_names_rejected(path):
    with pytest.raises(UnclassifiedState):
        classify_file("config", path)


def test_file_inventory_never_reads_contents(tmp_path, monkeypatch):
    tls = tmp_path / "tls"
    tls.mkdir()
    (tls / "ca-key.pem").write_text("DO NOT EXPORT THIS SECRET")
    (tmp_path / "local.json").write_text('{"password":"SECRET"}')
    monkeypatch.setattr(Path, "read_text", lambda *a, **k: pytest.fail("Read secret contents"))
    rows = list(state_files(tmp_path, "config"))
    assert len(rows) == 2
    assert all(row["category"] == "authoritative" for row in rows)
    assert "SECRET" not in json.dumps(rows)
    assert str(tmp_path) not in json.dumps(rows)


@pytest.mark.parametrize("directory", [False, True])
def test_unknown_file_or_empty_directory_blocks(tmp_path, directory):
    path = tmp_path / "future-feature"
    path.mkdir() if directory else path.write_text("unknown")
    with pytest.raises(UnclassifiedState):
        list(state_files(tmp_path, "data"))


def test_runtime_commands_not_replayed(tmp_path):
    job = tmp_path / "face_detect/profiles" / ("a" * 32) / "jobs/import-25"
    job.mkdir(parents=True)
    (job / "checkpoint.json").write_text("{}")
    (job / "control.json").write_text('{"action":"run"}')
    rows = {row["path"].split("/")[-1]: row for row in state_files(tmp_path, "data")}
    assert rows["checkpoint.json"]["category"] == "authoritative"
    assert rows["control.json"]["restore"] == "never replay automatically"


def test_missing_root_not_treated_as_empty(tmp_path):
    with pytest.raises(ValueError, match="Missing state root"):
        list(state_files(tmp_path / "missing", "data"))


@pytest.mark.parametrize('path', [
    'conversion-recovery/23/new-decision.json',
    'conversion-recovery/0/retain-original.json',
    'face_detect/profiles/' + 'a' * 32 + '/calibration/unknown.json',
    'face_detect/profiles/' + 'a' * 32 + '/calibration/evaluation-random/labeled-inputs.json',
])
def test_new_recovery_state_rules_still_reject_unknown_files(path):
    with pytest.raises(UnclassifiedState):
        classify_file('data', path)


def test_link_rejected(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks requires privilege on this Windows host")
    with pytest.raises(ValueError, match="Linked state roots"):
        list(state_files(link, "data"))


class FakeDatabase:
    def __init__(self, schemas=None, rows=None):
        self.info = SimpleNamespace(transaction_status=TransactionStatus.IDLE)
        self.schemas = schemas if schemas is not None else [("public",), (PROFILE,)]
        self.rows = rows if rows is not None else [("public", "media", "r", -1)]
        self.commands = []
        self.finished = False

    @contextmanager
    def transaction(self):
        try:
            yield
        finally:
            self.finished = True

    def execute(self, statement):
        self.commands.append(statement)
        rows = self.rows if "pg_class" in statement else self.schemas
        return SimpleNamespace(fetchall=lambda: rows)


def test_database_readonly_and_estimates():
    db = FakeDatabase()
    rows = list(database_inventory(db))
    assert rows[0]["estimated_rows"] == 0
    assert db.commands[0] == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
    assert db.finished
    assert not any("CREATE" in query or "UPDATE" in query for query in db.commands)


def test_existing_transaction_rejected():
    db = FakeDatabase()
    db.info.transaction_status = TransactionStatus.INTRANS
    with pytest.raises(ValueError, match="idle dedicated"):
        list(database_inventory(db))
    assert db.commands == []


def test_unknown_empty_schema_blocks():
    db = FakeDatabase(schemas=[("public",), ("new_feature",)])
    with pytest.raises(UnclassifiedState):
        list(database_inventory(db))
    assert db.finished


@pytest.mark.parametrize("kind", ["v", "m", "f"])
def test_unclassified_relation_kind_blocks(kind):
    with pytest.raises(UnclassifiedState):
        list(database_inventory(FakeDatabase(rows=[("public", "media", kind, 0)])))


def test_cli_reports_blocked_without_leaking_driver_error(tmp_path, monkeypatch, capsys):
    import psycopg
    from app.backup.__main__ import main

    def fail(**kwargs):
        raise psycopg.OperationalError("SECRET database password")

    monkeypatch.setattr(psycopg, "connect", fail)
    result = main(["--config-root", str(tmp_path), "--data-root", str(tmp_path),
                   "--host", "localhost", "--port", "55499", "--dbname", "test", "--username", "test"])
    output = capsys.readouterr()
    assert result == 1
    assert "SECRET" not in output.out + output.err
    assert json.loads(output.out.splitlines()[-1])["status"] == "blocked"


@pytest.mark.parametrize("unknown", [False, True])
def test_cli_terminal_result_is_not_a_backup(tmp_path, monkeypatch, capsys, unknown):
    import psycopg
    from app.backup import __main__ as cli

    @contextmanager
    def connect(**kwargs):
        yield FakeDatabase()

    monkeypatch.setattr(psycopg, "connect", connect)
    config = tmp_path / "config"
    data = tmp_path / "data"
    config.mkdir()
    data.mkdir()
    (config / "local.json").write_text("secret")
    if unknown:
        (data / "new-state.json").write_text("secret")
    status = cli.main(["--config-root", str(config), "--data-root", str(data),
                       "--host", "localhost", "--port", "55499", "--dbname", "test", "--username", "test"])
    result = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert result["is_backup"] is False
    assert status == int(unknown)
    if unknown:
        assert result["status"] == "blocked"
        assert result["reason"] == "Unclassified state file: data/new-state.json"
    else:
        assert result["status"] == "inventoried"
        assert result["authoritative_bytes"] == 6


def test_cli_capacity_preflight_blocks_full_disk(tmp_path, monkeypatch, capsys):
    import psycopg
    from app.backup import __main__ as cli
    from app.backup.space import SpaceGuard

    @contextmanager
    def connect(**kwargs):
        yield FakeDatabase()

    monkeypatch.setattr(psycopg, "connect", connect)
    monkeypatch.setattr(cli, "SpaceGuard", lambda requirements: SpaceGuard(
        requirements, disk_usage=lambda path: SimpleNamespace(free=0)))
    config = tmp_path / "config"
    data = tmp_path / "data"
    config.mkdir()
    data.mkdir()
    status = cli.main(["--config-root", str(config), "--data-root", str(data),
                       "--host", "localhost", "--port", "55499", "--dbname", "test", "--username", "test",
                       "--destination", str(tmp_path), "--temporary-root", str(tmp_path),
                       "--database-budget-bytes", "1000"])
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert status == 1
    capacity, = [row for row in records if row["kind"] == "capacity"]
    assert capacity["remaining_bytes"] == 2500 + 64 * 1024
    assert capacity["shortfall_bytes"] > capacity["remaining_bytes"]
    assert records[-1]["reason"] == "insufficient_space"


def test_disposable_postgres_inventory():
    """Opt-in only, and verify the cluster directory before creating any objects."""
    import os
    import uuid
    import psycopg
    from psycopg import sql

    port = os.environ.get("FAMILYFOTO_BACKUP_TEST_PORT")
    if not port:
        pytest.skip("Requires an explicitly started disposable backup-plan-pg cluster")
    assert int(port) != 5432
    params = dict(host="127.0.0.1", port=int(port), dbname="postgres", user="backup_test")
    with psycopg.connect(**params, autocommit=True) as admin:
        actual = Path(admin.execute("SHOW data_directory").fetchone()[0]).resolve()
        assert actual == (ROOT / "temp/backup-plan-pg").resolve(), "Not the disposable cluster"
        name = "backup_inventory_" + uuid.uuid4().hex
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            with psycopg.connect(**{**params, "dbname": name}) as db:
                db.execute("CREATE TABLE media(id integer)")
                db.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(PROFILE)))
                db.execute(sql.SQL("CREATE TABLE {}.native_matching_policies(id integer)").format(sql.Identifier(PROFILE)))
                db.commit()
                rows = list(database_inventory(db))
                assert {(row["schema"], row["name"]) for row in rows} == {
                    ("public", "media"), (PROFILE, "native_matching_policies"),
                }
                db.execute("CREATE SCHEMA unexpected_state")
                db.commit()
                with pytest.raises(UnclassifiedState, match="unexpected_state"):
                    list(database_inventory(db))
        finally:
            admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))
