"""Tripwire: new production entry points require a backup-admission review.

This inventory complements runtime subprocess tests; it does not prove that an
arbitrary future edit preserves the ordering of every write.
"""
import ast
from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINTS = {
    'app/backup/__main__.py': 'offline coordinator',
    'fastbackup/__main__.py': 'offline coordinator: capture holds the maintenance gate; verify, restore and cleanup write only to new or owned folders',
    'app/backup/admin_restore.py': 'isolated recovery folder only; holds no lease and avoids load_config',
    'app/backup/admin_runner.py': 'offline coordinator; holds no lease and avoids load_config',
    'app/backup/manage.py': 'offline coordinator',
    'app/backup/state_files.py': 'archive stream only',
    'app/backup/verify.py': 'read only',
    'app/certificates.py': 'guard_application_path',
    'app/database/__main__.py': 'database.connection',
    'app/database/schema_upgrading.py': 'database.connection; verified upgrade coordinator',
    'app/imports/importer.py': 'load_config',
    'app/jobs/__main__.py': 'jobs.run/load_config',
    'app/jobs/review.py': 'database.connection',
    'app/migration/launch.py': 'desktop.main/hold_process_lease',
    'app/migration/people_setup.py': 'database.connection; adopts the existing people catalog, writes only face-engine setup files and one certificate',
    'app/migration/preview_server.py': 'isolated read-only recovery',
    'app/photos/heif.py': 'hold_process_lease',
    'app/photos/recovery.py': 'stdin/stdout decoder only',
    'app/photos/repair_heif.py': 'database.connection',
    'app/photos/thumbnail_recovery.py': 'stdin/stdout decoder only',
    'app/photos/thumbnails.py': 'database.connection',
    'app/release.py': 'read only',
    'app/service/desktop.py': 'hold_process_lease',
    'app/service/live.py': 'load_config',
    'app/service/macos.py': 'load_config',
    'app/service/readonly_preview.py': 'isolated preview; edits use database.connection',
    'app/service/server.py': 'load_config',
    'app/video/conversions.py': 'load_config',
    'app/video/handoff.py': 'retired entry point refuses execution',
    'app/video/metadata.py': 'database.connection',
    'face_detect/cli/calibrate.py': 'load_config',
    'face_detect/cli/catalog.py': 'database.connection; creates the people schema once, leaves the source schema unchanged; writes only data/people/thumbnails',
    'face_detect/cli/check_people_integration.py': 'load_config',
    'face_detect/cli/conformance.py': 'load_config; reads the People catalog in a read-only transaction; writes only a new golden-set or library-sample folder or certificate file',
    'face_detect/cli/doctor.py': 'load_config',
    'face_detect/cli/prepare.py': 'load_config',
    'face_detect/cli/prepare_people_import14.py': 'load_config',
    'face_detect/cli/setup.py': 'hold_process_lease',
    'face_detect/jobs/automatic_people_local.py': 'runtime/load_config',
    'face_detect/providers/runner.py': 'stdin/stdout inference only',
}

# Standalone historical validation tools are deliberately not admitted to an
# offline backup session. They must be stopped with other external DB clients.
SCRIPTS = {
    'ci_tests.py': 'CI-only source inspection and pytest selection; writes only requested test reports and GitHub job outputs',
    'audit_pending_work.py': 'read-only direct database audit',
    'backup_validation_snapshot.py': 'historical offline validation coordinator',
    'calibrate_native_live.py': 'calibrate/load_config',
    'consolidate_import_membership.py': 'read-only; legacy schema application refused',
    'enable_native_manual_matching.py': 'database.connection',
    'evaluate_offline_grouping.py': 'pure scoring functions',
    'forget_deleted_photo_faces.py': 'read-only report; --apply uses database.connection',
    'launch_readonly_preview.py': 'readonly_preview launcher',
    'migrate_people_embeddings.py': 'isolated validation cluster only; external maintenance',
    'prepare_limited_edit.py': 'hold_process_lease on installation and preview state',
    'prepare_people_migration.py': 'isolated validation cluster only; external maintenance',
    'probe_backup_filesystem.py': 'synthetic files in its own new folder; no library or database',
    'rebuild_missing_thumbnails.py': 'load_config',
    'upgrade_auth.py': 'guard_application_path',
    'validate_limited_app.py': 'isolated validation cluster only; external maintenance',
    'validate_restored_edits.py': 'isolated validation cluster only; external maintenance',
    'validate_restored_regressions.py': 'isolated validation cluster only; external maintenance',
}


def test_new_standalone_scripts_require_admission_review():
    assert {path.name for path in (ROOT / 'scripts').glob('*.py')} == SCRIPTS.keys()


def test_supported_distribution_launchers_are_classified():
    declared = tomllib.loads((ROOT / 'pyproject.toml').read_text())['tool']['familyfoto']['entrypoints']
    assert declared['asgi'] == ['app.api:app']  # API startup/load_config and shared DB admission.
    for module in declared['modules']:
        path = module.replace('.', '/')
        assert (path + '.py' in ENTRYPOINTS or path + '/__main__.py' in ENTRYPOINTS
                or module == 'app.video.avi'), module
    wrappers = {
        'scripts/start.ps1', 'scripts/desktop-server.ps1', 'scripts/desktop-control.ps1',
        'scripts/import.ps1', 'installer/windows/install.ps1', 'installer/macos/install.sh',
        'installer/macos/manage.py', 'installer/configure.py',
        'installer/init-database.py', 'installer/verify-installation.py',
    }
    for path in declared['scripts']:
        assert path in wrappers or path.removeprefix('scripts/') in SCRIPTS, path


def test_new_production_entrypoints_require_writer_review():
    found = set()
    for folder in ('app', 'face_detect', 'fastbackup'):
        for path in (ROOT / folder).rglob('*.py'):
            if any(part in {'tests', 'testing', '__pycache__'} for part in path.parts):
                continue
            tree = ast.parse(path.read_text(encoding='utf-8-sig'))
            if path.name == '__main__.py' or any(
                    isinstance(node, ast.If) and '__name__' in ast.unparse(node.test)
                    and '__main__' in ast.unparse(node.test) for node in tree.body):
                found.add(path.relative_to(ROOT).as_posix())
    assert found == ENTRYPOINTS.keys(), 'Classify new/removed entry points and test their admission before writes'


def test_primary_writer_guards_remain_present():
    guards = {
        'app/config.py': 'hold_process_lease(DATA)',
        'app/database/__init__.py': 'hold_process_lease(DATA)',
        'app/service/desktop.py': 'hold_process_lease(DATA)',
        'app/photos/heif.py': 'hold_process_lease(DATA)',
        'face_detect/cli/setup.py': 'hold_process_lease(DATA)',
        'scripts/upgrade_auth.py': 'guard_application_path(path)',
        'installer/configure.py': "hold_process_lease(ROOT / 'data')",
        'installer/init-database.py': "hold_process_lease(root / 'data')",
        'scripts/prepare_limited_edit.py': "hold_process_lease(Path(cfg['preview']['data_root']))",
    }
    for relative, guard in guards.items():
        assert guard in (ROOT / relative).read_text(encoding='utf-8'), relative
