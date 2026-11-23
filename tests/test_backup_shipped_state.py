"""Backups must accept what a real installation holds, which test folders otherwise never contain."""
from pathlib import Path
import shutil

import pytest

from app.backup.policy import classify_file, UnclassifiedState
from app.backup.state_files import state_inventory
from tests.test_backup_postgres import postgres, ample  # noqa: F401
from tests.test_backup_upgrade import add_upgrade, upgrade_case  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


def install_like(config, data):
    """The shipped settings template, and a database where the Windows installer puts it."""
    shutil.copy(ROOT / 'config/example.json', config / 'example.json')
    (data / 'postgres/base/1').mkdir(parents=True)
    (data / 'postgres/base/1/1234').write_bytes(b'x')
    (data / 'postgres/PG_VERSION').write_text('17')


def test_the_release_still_ships_the_template():
    assert 'config/example.json' in (ROOT / 'installer/release-files.json').read_text(encoding='utf-8')


def test_an_installed_layout_passes_and_only_settings_are_captured(tmp_path):
    config, data = tmp_path / 'config', tmp_path / 'data'
    config.mkdir()
    data.mkdir()
    (config / 'local.json').write_text('{}')
    install_like(config, data)
    assert [row['path'] for row in state_inventory({'config': config, 'data': data})] == ['local.json']


@pytest.mark.parametrize('kind, path, category', [
    ('config', 'example.json', 'excluded'),
    ('data', 'postgres/PG_VERSION', 'excluded'),
    ('data', 'postgres/base/1/1234', 'excluded'),
    ('data', 'video-handoff.json', 'transient'),
    ('data', 'reports/people-integration-20261020/before.json', 'excluded'),
    ('data', f"face_detect/profiles/{'a' * 32}/desktop-server.log", 'excluded'),
])
def test_files_a_running_server_holds_have_rules(kind, path, category):
    assert classify_file(kind, path).category == category


@pytest.mark.parametrize('kind, path', [
    ('data', 'postgresql/x'), ('data', 'postgres'), ('data', 'example.json'),
    ('config', 'other-example.json'), ('config', 'postgres/x'),
    ('data', 'reports/loose.json'), ('data', 'reports/a/b/c.json'), ('data', 'reports/a/key.pem'),
])
def test_neighbouring_names_stay_unknown(kind, path):
    with pytest.raises(UnclassifiedState):
        classify_file(kind, path)


def test_a_schema_upgrade_can_take_its_required_backup_on_an_installed_layout(upgrade_case):
    run, _, scripts, data, backup, _ = upgrade_case
    run()
    install_like(data.parent / 'config', data)
    add_upgrade(scripts)
    report = run()
    assert report['backup_created'] and report['applied'] == [2]
    assert backup.is_dir()
