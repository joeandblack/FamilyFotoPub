# scripts/

Operator tools. Run from the repository root with the project's Python, for
example `.venv/bin/python scripts/<name>.py` (`.venv\Scripts\python.exe` on
Windows). Most take `--help` and are explicit about what they will and will
not touch; read the docstring first. Nothing here is imported by the app.

## Recurring tools

| Script | Purpose |
|---|---|
| `audit_pending_work.py` | Read-only queue audit. Never dispatches, resumes or changes a job. |
| `rebuild_missing_thumbnails.py` | Audit or repair missing photo thumbnails; originals are never modified. |
| `upgrade_auth.py` | Atomic upgrade of private browser credentials. Stop HTTP services first. |
| `start.ps1`, `import.ps1` | Windows launchers for the web server and a server-folder import. |
| `desktop-control.ps1`, `desktop-server.ps1` | Windows desktop launcher; installed by `installer/windows/install-desktop-launcher.ps1`. |

## People and matching (Windows native)

| Script | Purpose |
|---|---|
| `calibrate_native_live.py` | Local calibration; starts no scans and edits no groups. |
| `enable_native_manual_matching.py` | Enable an explicitly requested manual trial without running recognition. |
| `evaluate_offline_grouping.py` | Metrics for already-produced clusters; labels never enter the clusterer. |
| `prepare_people_migration.py` | Snapshot the preserved People schema into a separate validation database. |
| `migrate_people_embeddings.py` | Resumable native embedding staging on a verified, isolated snapshot. |

Both migration scripts are documented in
[docs/PEOPLE.md](../docs/PEOPLE.md).

## Backup, restore and write validation (Windows)

Described in
[docs/BACKUP.md](../docs/BACKUP.md).

`backup_validation_snapshot.py`, `launch_readonly_preview.py`,
`prepare_limited_edit.py`, `validate_limited_app.py`,
`validate_restored_edits.py`, `validate_restored_regressions.py`.

## One-time migrations

| Script | Purpose |
|---|---|
| `consolidate_import_membership.py` | Single-import-policy migration (release notes 2026-10-24). Back up first; all workers idle. |

## Archive

`archive/native-people-cutover/` holds the completed Windows native People
cutover scripts, kept as a historical record: `activate_native_catalog`,
`finalize_people_migration`, `prepare_live_deployment`,
`publish_people_candidate`, `validate_live_app`, `validate_native_cutover`,
`verify_native_live`, `verify_people_migration`. They were moved here with only
path adjustments (repository root, and the `prepare_people_migration` sibling
import); behavior is unchanged. No document references them; the migration
doc above covers only the `prepare_people_migration` and
`migrate_people_embeddings` steps. Do not rerun against a live library without
reading each script's docstring and taking a backup.
