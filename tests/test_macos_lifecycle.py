"""Mac lifecycle contracts; native integration is checked separately."""
import plistlib
from pathlib import Path
import pytest
from installer.macos import manage


def test_plist_on_demand_and_paths_with_spaces(monkeypatch, tmp_path):
    root = tmp_path / 'Family Photos'
    monkeypatch.setattr(manage, 'ROOT', root)
    monkeypatch.setattr(manage, 'PYTHON', root / '.venv/bin/python3')
    result = plistlib.loads(plistlib.dumps(manage.plist()))
    assert result['ProgramArguments'][0] == str(root / '.venv/bin/python3')
    assert result['KeepAlive'] is False
    assert result['ExitTimeOut'] == 0
    assert result['EnvironmentVariables']['LC_ALL'] == 'C'
    assert 'StartInterval' not in result


def test_postgres_uses_private_cluster_no_socket(monkeypatch, tmp_path):
    monkeypatch.setattr(manage, 'CLUSTER', tmp_path / 'private postgres')
    args = manage.postgres_args({'database':{'port':5433}}, Path('/tools with spaces'))
    assert args[:3] == [str(Path('/tools with spaces')/'postgres'), '-D', str(tmp_path / 'private postgres')]
    assert args[args.index('-h') + 1] == '127.0.0.1'
    assert args[args.index('-k') + 1] == ''


def test_existing_cluster_is_never_reinitialized(monkeypatch, tmp_path):
    monkeypatch.setattr(manage, 'CLUSTER', tmp_path)
    monkeypatch.setattr(manage, 'settings', lambda: ({}, tmp_path))
    with pytest.raises(ValueError, match='already exists'):
        manage.initialize()


def test_identity_mismatch_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(manage, 'RUNTIME', tmp_path)
    (tmp_path / 'macos-database-ready.json').write_text('{"cluster":"wrong", "port":5433}')
    with pytest.raises(ValueError, match='identity'):
        manage.validate_ready({'database':{'port':5433}})


def test_mac_allows_native_media_and_face_services():
    import subprocess
    import sys
    result=subprocess.run([sys.executable,'-c',"from app.service.macos import allowed; assert allowed('POST','/api/face-service/start'); assert allowed('POST','/api/import/start')"],capture_output=True,text=True)
    assert result.returncode==0,result.stderr


@pytest.fixture
def pause(monkeypatch, tmp_path):
    from app.service import macos_lifecycle as lifecycle
    monkeypatch.setattr(lifecycle, 'PAUSE_WEB', tmp_path / 'macos-web-pause')
    monkeypatch.setattr(lifecycle, 'STOP', tmp_path / 'macos-stop')
    monkeypatch.setattr(lifecycle, 'GATE', tmp_path / 'maintenance.json')
    monkeypatch.setattr(lifecycle, 'DATA', tmp_path / 'data')
    (tmp_path / 'data').mkdir()
    return lifecycle


def test_the_web_server_runs_when_no_backup_asks_for_it_to_stop(pause):
    assert pause.web_paused() is False and pause.backup_running() is False


def test_a_live_backup_keeps_the_web_server_down_and_the_service_running(pause):
    import os
    import json
    pause.PAUSE_WEB.write_text(json.dumps({'run': 'x', 'pid': os.getpid()}))
    assert pause.web_paused() is True and pause.backup_running() is True
    assert pause.PAUSE_WEB.exists()


def test_a_gone_backup_releases_the_web_server_unless_it_holds_the_gate(pause):
    import json
    pause.PAUSE_WEB.write_text(json.dumps({'run': 'x', 'pid': 2 ** 22 + 12345}))
    pause.GATE.write_text('{}')
    assert pause.web_paused() is True and pause.backup_running() is False
    assert pause.PAUSE_WEB.exists(), 'a held gate stays for its owner'
    pause.GATE.unlink()
    assert pause.web_paused() is False
    assert not pause.PAUSE_WEB.exists(), 'nothing is working on a backup, so the web server comes back'


@pytest.mark.parametrize('content', ['', '{', '[]', '{"pid": "7"}', '{"pid": true}'])
def test_an_unreadable_marker_never_keeps_the_application_down_for_good(pause, content):
    pause.PAUSE_WEB.write_text(content)
    assert pause.web_paused() is False
    assert not pause.PAUSE_WEB.exists()


def test_a_backup_pause_stops_only_the_web_server(pause):
    pause.request_pause(stop=False)
    assert not pause.STOP.exists(), 'the stop marker would take the database down too'
    assert all((pause.DATA / name).exists() for name in pause.PAUSES)
    pause.request_pause()
    assert pause.STOP.exists()


def test_stop_is_refused_while_a_backup_is_running(monkeypatch, pause):
    import os
    import json
    pause.PAUSE_WEB.write_text(json.dumps({'run': 'x', 'pid': os.getpid()}))
    monkeypatch.setattr(manage, 'loaded', lambda: True)
    monkeypatch.setattr(manage, 'STOP', pause.STOP)
    with pytest.raises(manage.BackupRunning, match='backup is running'):
        manage.stop()
    assert not pause.STOP.exists()


def test_start_waits_for_a_running_backup_instead_of_touching_the_service(monkeypatch, pause):
    import os
    import json
    pause.PAUSE_WEB.write_text(json.dumps({'run': 'x', 'pid': os.getpid()}))
    monkeypatch.setattr(manage, 'settings', lambda: ({'port': 1}, Path('/tools')))
    monkeypatch.setattr(manage, 'validate_ready', lambda c: None)
    monkeypatch.setattr(manage, 'run', lambda *a, **k: pytest.fail('Nothing may be started or opened'))
    with pytest.raises(manage.BackupRunning, match='starts again by itself'):
        manage.start()


def test_only_our_own_backup_messages_are_shown_by_the_command(monkeypatch, capsys):
    for failure, shown in ((manage.BackupRunning('A backup is running. Wait.'), 'A backup is running. Wait.'),
                           (RuntimeError('password=secret'), 'Mac lifecycle failed (RuntimeError)')):
        def fail(failure=failure):
            raise failure
        monkeypatch.setattr(manage, 'main', fail)
        assert manage.cli() == 1
        error = capsys.readouterr().err
        assert shown in error and 'secret' not in error
