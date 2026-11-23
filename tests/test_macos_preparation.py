"""Cross-platform contract tests. No Homebrew, services, or live config writes."""
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
from types import SimpleNamespace

import pytest

from installer import configure
from installer.macos import prepare
from app.service import folder_picker


@pytest.fixture
def package(tmp_path):
    for name in ('requirements.txt', 'installer/configure.py', 'app/api.py'):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('fixture', encoding='utf-8')
    return tmp_path


@pytest.mark.parametrize('name', ['config/local.json', '.venv', 'data', 'logs', 'temp', 'runtime'])
def test_preparation_rejects_existing_state(package, name):
    path = package / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('must remain unchanged', encoding='utf-8')
    with pytest.raises(ValueError, match='Existing installation'):
        prepare.require_fresh(package)
    assert path.read_text() == 'must remain unchanged'


def test_preflight_is_read_only(package):
    before = set(package.rglob('*'))
    prepare.require_fresh(package)
    assert set(package.rglob('*')) == before


def test_wrong_platform_cannot_write(monkeypatch, package):
    monkeypatch.setattr(prepare.sys, 'platform', 'win32')
    with pytest.raises(ValueError, match='Apple Silicon'):
        prepare.prepare(package, package, package)
    assert not (package / 'runtime').exists()


def test_busy_port_is_not_adopted():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        with pytest.raises(OSError):
            prepare.require_free_ports([listener.getsockname()[1]])


@pytest.mark.parametrize('fail_install', [False, True])
def test_preparation_receipt_tracks_completion_without_services(monkeypatch, package, fail_install):
    monkeypatch.setattr(prepare.sys, 'platform', 'darwin')
    monkeypatch.setattr(prepare.platform, 'machine', lambda: 'arm64')
    monkeypatch.setattr(prepare.platform, 'platform', lambda: 'macOS-test-fixture')
    monkeypatch.setattr(prepare.os, 'geteuid', lambda: 501, raising=False)
    monkeypatch.setattr(prepare.os, 'umask', lambda value: 0o077)
    monkeypatch.setattr(prepare, 'require_free_ports', lambda ports: None)
    tools = {name: package / name for name in ('ffmpeg', 'ffprobe', 'initdb', 'pg_ctl', 'postgres')}
    monkeypatch.setattr(prepare, 'checked_tools', lambda *args: tools)
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if fail_install and 'pip' in command:
            raise subprocess.CalledProcessError(1, command)
    monkeypatch.setattr(prepare.subprocess, 'run', run)
    if fail_install:
        with pytest.raises(subprocess.CalledProcessError):
            prepare.prepare(package, package, package)
    else:
        prepare.prepare(package, package, package)
    receipt = json.loads((package / 'runtime/macos-preparation.json').read_text())
    assert receipt['state'] == ('preparing' if fail_install else 'prepared-not-installed')
    assert receipt['native_service_tests'] == 'not-run'
    assert all(not any(str(tool) in command for tool in tools.values())
               for command in calls if 'configure.py' not in ' '.join(command))
    with pytest.raises(ValueError, match='Existing installation'):
        prepare.require_fresh(package)


def test_config_arguments_preserve_spaces(package):
    tools = {name: package / 'path with spaces' / name for name in ('ffmpeg', 'ffprobe')}
    command = prepare.configuration_command(package, tools)
    assert command[command.index('--ffmpeg') + 1] == str(tools['ffmpeg'])
    assert '--source' not in command and '--destination' not in command
    assert command[command.index('--database-port') + 1] == '5433'


def test_mac_config_has_deferred_storage_and_tool_paths(monkeypatch, tmp_path):
    tools = []
    for name in ('ffmpeg', 'ffprobe'):
        path = tmp_path / name
        path.touch()
        tools.extend(['--' + name, str(path)])
    monkeypatch.setattr(configure, 'ROOT', tmp_path)
    monkeypatch.setattr(sys, 'argv', ['configure.py', '--password-stdin', '--database-port',
                                    '5433', '--web-port', '4341', *tools])
    monkeypatch.setattr(sys, 'stdin', io.TextIOWrapper(io.BytesIO(b'testing-mac-password-only\n'), encoding='utf-8'))
    configure.main()
    config = json.loads((tmp_path / 'config/local.json').read_text())
    assert config['database']['port'] == 5433
    assert config['port'] == 4341
    assert config['source'] == config['destination'] == {}
    assert config['video_tools']['ffmpeg'] == str(tmp_path / 'ffmpeg')
    assert 'browser_password' not in config


@pytest.mark.parametrize('arguments', [
    ['--web-port', '0'], ['--database-port', '65536'],
    ['--database-port', '4340'], ['--ffmpeg', '/missing/ffmpeg'],
])
def test_invalid_config_options_write_nothing(monkeypatch, tmp_path, arguments):
    monkeypatch.setattr(configure, 'ROOT', tmp_path)
    monkeypatch.setattr(sys, 'argv', ['configure.py', *arguments])
    with pytest.raises(SystemExit):
        configure.main()
    assert not (tmp_path / 'config').exists()


@pytest.mark.parametrize('value', ['/Volumes/Family Photos/旅行/', ''])
def test_mac_picker_select_or_cancel(monkeypatch, value):
    calls = []
    monkeypatch.setattr(folder_picker.sys, 'platform', 'darwin')
    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=value.encode('utf-8'))
    monkeypatch.setattr(folder_picker.subprocess, 'run', run)
    assert folder_picker.choose_folder(storage=True) == (value or None)
    assert calls[0][0] == '/usr/bin/osascript'
    assert 'storage folder' in calls[0][-1]


def test_mac_picker_failure_unlocks(monkeypatch):
    monkeypatch.setattr(folder_picker.sys, 'platform', 'darwin')
    monkeypatch.setattr(folder_picker.subprocess, 'run',
                        lambda *a, **k: SimpleNamespace(returncode=1, stdout=b''))
    with pytest.raises(RuntimeError, match='permissions'):
        folder_picker.choose_folder()
    assert folder_picker._lock.acquire(False)
    folder_picker._lock.release()


def test_mac_picker_timeout_unlocks(monkeypatch):
    monkeypatch.setattr(folder_picker.sys, 'platform', 'darwin')
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired('osascript', 300)
    monkeypatch.setattr(folder_picker.subprocess, 'run', timeout)
    with pytest.raises(RuntimeError, match='timed out'):
        folder_picker.choose_folder()
    assert folder_picker._lock.acquire(False)
    folder_picker._lock.release()
