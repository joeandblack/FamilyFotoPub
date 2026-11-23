"""A library moved to a new computer keeps its own settings and takes the computer's."""
from pathlib import Path
import re

import pytest

from app.migration import settings_rules
from app.migration.settings_rules import RULES, UnknownSetting, merge

ROOT = Path(__file__).resolve().parents[1]
# Keys read from dictionaries that happen to be called cfg or config but aren't local.json:
# face profiles, People pilot settings, storage bindings, and the setup launcher's bootstrap.json
# (installer/*/online_setup.py reads its `mode` and `schema_version`).
NOT_SETTINGS = {'pilot', 'root', 'profiles', 'platform_defaults', 'dbname', 'active_profile',
                'mode', 'schema_version'}


def restored():
    return {'browser_user': 'family', 'browser_password_hash': 'old-hash', 'browser_accounts': [{'name': 'kid'}],
            'io_mibps': 60, 'avi_conversion': {'enabled': True}, 'host': '192.0.2.204', 'port': 4340,
            'video_tools': {'ffmpeg': 'C:\\FamilyFoto\\runtime\\ffmpeg\\bin\\ffmpeg.exe'},
            'face_detection': {'active_profile': 'windows-gpu'}, 'live_existing_catalog': True,
            'database': {'dbname': 'familyfoto_recovery_x', 'user': 'familyfoto_app_x'},
            'destination': {'root': '/Volumes/Photos/library'}, 'source': {}, 'tls': {'certfile': '/restore/tls/server.pem'},
            'recovery': {'state': 'activated', 'activation_required': False},
            'auth_secret': 'old-secret', 'session_epoch': 'old-epoch'}


def installation():
    return {'host': '127.0.0.1', 'port': 4341, 'postgres_admin_password': 'local-admin',
            'video_tools': {'ffmpeg': '/opt/homebrew/bin/ffmpeg'}, 'database': {'dbname': 'familyfoto'},
            'browser_user': 'family', 'browser_password_hash': 'unused', 'auth_secret': 'x', 'io_mibps': 30,
            'source': {}, 'destination': {}, 'tls': {'certfile': '/installer/tls/server.pem'}}


def test_the_library_keeps_its_own_and_takes_the_computers():
    merged = merge(restored(), installation(), extra={'backup': {'pg_dump': '/opt/homebrew/bin/pg_dump'}})
    # The library's: accounts and preferences.
    assert merged['browser_password_hash'] == 'old-hash' and merged['browser_accounts'] == [{'name': 'kid'}]
    assert merged['io_mibps'] == 60 and merged['avi_conversion'] == {'enabled': True}
    # The computer's: the rehearsal's two failures, the address and FFmpeg, come from here.
    assert merged['host'] == '127.0.0.1' and merged['port'] == 4341
    assert merged['video_tools'] == {'ffmpeg': '/opt/homebrew/bin/ffmpeg'}
    assert merged['postgres_admin_password'] == 'local-admin' and merged['backup'] == {'pg_dump': '/opt/homebrew/bin/pg_dump'}
    # Made by the restore for this computer, never the installer's empty database.
    assert merged['database'] == {'dbname': 'familyfoto_recovery_x', 'user': 'familyfoto_app_x'}
    assert merged['destination'] == {'root': '/Volumes/Photos/library'} and merged['recovery']['state'] == 'activated'
    # The old engine's paths and deployment flags are left out; sign-ins start again.
    assert 'face_detection' not in merged and 'live_existing_catalog' not in merged
    assert merged['auth_secret'] not in ('old-secret', 'x') and merged['session_epoch'] != 'old-epoch'


@pytest.mark.parametrize('side', ['restored', 'installation', 'extra'])
def test_a_setting_without_a_rule_stops_the_move(side):
    values = {'restored': restored(), 'installation': installation(), 'extra': {}}
    values[side]['brand_new_setting'] = 1
    with pytest.raises(UnknownSetting, match='brand_new_setting'):
        merge(values['restored'], values['installation'], extra=values['extra'])


def test_the_report_names_settings_never_values():
    merged = merge(restored(), installation())
    report = settings_rules.explain(merged, restored())
    assert 'browser_password_hash' in report['backup'] and 'video_tools' in report['installation']
    assert {'face_detection', 'live_existing_catalog', 'video_tools', 'host'} <= set(report['left_out'])
    assert 'old-hash' not in repr(report) and 'local-admin' not in repr(report)


def test_every_setting_the_code_reads_has_a_rule():
    """A new setting can't quietly carry an old computer's path: give it a rule first."""
    pattern = re.compile(r"\b(?:cfg|config)(?:\.get\(|\[)'([a-z_]+)'")
    found = set()
    for folder in ('app', 'face_detect', 'installer', 'scripts'):
        for path in (ROOT / folder).rglob('*.py'):
            if 'tests' not in path.parts:
                found.update(pattern.findall(path.read_text(encoding='utf-8')))
    assert sorted(found - NOT_SETTINGS - set(RULES)) == []


def test_a_computer_setting_is_added_to_the_installations_group_not_replacing_it():
    computer = {**installation(), 'backup': {'pg_dump': '/old/pg_dump', 'keep': 'this'}}
    merged = merge(restored(), computer, extra={'backup': {'pg_dump': '/opt/homebrew/bin/pg_dump'}})
    assert merged['backup'] == {'pg_dump': '/opt/homebrew/bin/pg_dump', 'keep': 'this'}


def test_retired_face_model_settings_do_not_block_a_move_or_carry_windows_paths():
    backup = {**restored(),
              'faces': {'enabled': True, 'manifest_path': r'D:\FamilyFotoLive\models\yunet.json',
                        'comparison_manifest_path': r'D:\FamilyFotoLive\models\comparison.json'},
              'people': {'enabled': True, 'manifest_path': r'D:\FamilyFotoLive\models\sface.json'}}
    computer = {**installation(), 'face_detection': {'active_profile': 'mac-insightface'},
                'faces': {'enabled': False}, 'people': {'enabled': False}}
    merged = merge(backup, computer)
    assert 'faces' not in merged and 'people' not in merged
    assert merged['face_detection'] == computer['face_detection']
    assert merged['database'] == backup['database']
    assert merged['browser_accounts'] == backup['browser_accounts']
    assert {'faces', 'people'} <= set(settings_rules.explain(merged, backup)['left_out'])
