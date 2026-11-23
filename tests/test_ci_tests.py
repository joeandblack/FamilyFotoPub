"""Changing CI selection must not silently omit expensive regression coverage."""
import json
import subprocess
import sys
import configparser

import pytest

from scripts.ci_tests import ROOT, database_modules, group_for, plan, shard_for


@pytest.mark.parametrize('paths,expected', [
    (['docs/README.md'], set()),
    (['web/styles.css'], set()),
    (['web/settings.mjs'], set()),
    (['app/auth/api.py'], {'database'}),
    (['databases/038.sql'], {'database'}),
    (['installer/macos/SetupWindow.swift'], {'database', 'swift', 'apfs'}),
    (['app/backup/media_tree.py'], {'database', 'swift', 'apfs'}),
    (['app/fastbackup/capture.py'], {'database', 'apfs'}),
    (['tests/test_backup_restore_remount.py'], {'apfs'}),
    (['requirements.txt'], {'database', 'swift', 'apfs'}),
    (['.github/workflows/tests.yml'], {'database', 'swift', 'apfs'}),
    (['tests/backup_keys.py'], {'database', 'swift', 'apfs'}),
    (['new_component/worker.py'], {'database', 'swift', 'apfs'}),
])
def test_change_selection(paths, expected):
    assert {group for group, enabled in plan(paths).items() if enabled} == expected


def test_release_runs_every_group_even_without_changes():
    assert all(plan([], full=True).values())


def test_shared_database_fixtures_follow_imports():
    # albums imports the uploads fixture; auth_security imports isolated_db by
    # its bare test module name. Both must stay out of no-database runners.
    assert {'tests/test_uploads.py', 'tests/test_albums.py', 'tests/test_auth_security.py'} <= database_modules()
    assert group_for('tests/test_albums.py::test_example') == 'database'
    assert group_for('tests/test_ci_tests.py::test_example') == 'core'
    assert group_for('app/fastbackup/tests/test_full_capture_postgres.py::test_example') == 'database'
    assert group_for('app/fastbackup/tests/test_copy_worker.py::test_example') == 'core'


def test_relocated_fast_tests_are_discovered_by_default_and_windows_ci():
    config = configparser.ConfigParser()
    config.read(ROOT / 'pytest.ini')
    assert 'app/fastbackup/tests' in config['pytest']['testpaths'].split()
    workflow = (ROOT / '.github/workflows/tests.yml').read_text()
    assert 'python -m pytest tests face_detect/tests app/fastbackup/tests -q' in workflow


def test_native_markers_win_over_module_group_and_shards_are_stable():
    node = 'tests/test_albums.py::test_example'
    assert group_for(node, {'ci_swift'}) == 'swift'
    assert group_for(node, {'ci_apfs'}) == 'apfs'
    assert group_for('tests/test_mac_release.py::test_postgres', {'ci_database'}) == 'database'
    assert shard_for(node, 2) == shard_for('tests/test_albums.py::test_another[param]', 2)
    assert shard_for(node, 2) in (0, 1)


def test_missing_git_history_falls_back_to_full(tmp_path):
    result = subprocess.run([sys.executable, '-m', 'scripts.ci_tests', '--base', 'not-an-existing-ref'],
                            cwd=ROOT, capture_output=True, text=True, check=True)
    value = json.loads(result.stdout)
    assert value['full'] is True and all(value['groups'].values())
