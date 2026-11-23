import json
from pathlib import Path
import subprocess
import sys

import pytest
from installer.macos import people, prepare

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="native POSIX ownership and locking contracts")


@pytest.fixture
def fresh(tmp_path):
    root = tmp_path / 'Family Photos'
    for name in ('config', 'data', 'runtime/postgres', '.venv'):
        (root / name).mkdir(parents=True, mode=0o700)
    (root / 'runtime').chmod(0o700)
    cfg = {'host':'127.0.0.1', 'port':55482, 'database':{'host':'127.0.0.1','port':55481}}
    path = root / 'config/local.json'
    path.write_text(json.dumps(cfg)); path.chmod(0o600)
    return root, cfg


def test_fresh_local_config_and_private_permissions(fresh):
    root, cfg = fresh
    assert people.local_configuration(root, root / '.venv') == cfg
    (root / 'config/local.json').chmod(0o644)
    with pytest.raises(ValueError, match='permissions'):
        people.local_configuration(root, root / '.venv')


@pytest.mark.parametrize('name', ['source','destination','preview','live_native_catalog','live_existing_catalog'])
def test_enrolled_or_live_state_refused(fresh, name):
    root, cfg = fresh
    cfg[name] = {'enabled':True}
    (root / 'config/local.json').write_text(json.dumps(cfg))
    with pytest.raises(ValueError, match='empty installation'):
        people.local_configuration(root, root / '.venv')


def test_external_environment_refused(fresh, monkeypatch):
    root, _ = fresh
    monkeypatch.setenv('FAMILYFOTO_DATA_DIR','/other')
    with pytest.raises(ValueError, match='overrides'):
        people.local_configuration(root, root / '.venv')


@pytest.mark.parametrize('name', ['data/people-preview.json','data/face_detect','runtime/face_detect','runtime/macos-people-install.json'])
def test_interrupted_people_state_refused(fresh, name):
    root, cfg = fresh
    (root / name).touch()
    with pytest.raises(ValueError, match='Existing People state'):
        people.check_fresh(root, cfg, 55483)


def test_concurrent_service_lock_refused(fresh):
    root, _ = fresh
    with people.installation_lock(root):
        with pytest.raises(ValueError, match='active'):
            with people.installation_lock(root):
                pytest.fail('lock acquired twice')


def test_consent_checked_before_any_write(monkeypatch):
    monkeypatch.setattr(people, 'local_configuration', lambda *a: pytest.fail('should not read config'))
    with pytest.raises(ValueError, match='consent'):
        people.install(False, 55483)


def test_database_window_stops_only_owned_child_on_failure(fresh, monkeypatch):
    root, cfg = fresh
    (root / 'logs').mkdir()
    monkeypatch.setattr(people, 'ROOT', root)
    monkeypatch.setattr(people.manage, 'validate_ready', lambda c: None)
    monkeypatch.setattr(people.prepare, 'require_free_ports', lambda p: None)
    monkeypatch.setattr(people.manage, 'wait_database', lambda *a: None)
    monkeypatch.setattr(people, 'verify_database', lambda c: (_ for _ in ()).throw(ValueError('foreign cluster')))
    actions = []
    from app import database
    monkeypatch.setattr(database, 'close_pools', lambda: actions.append('close pools'))
    class Process:
        def poll(self): return None
        def send_signal(self, sig): actions.append('stop owned child')
        def wait(self, timeout): actions.append('wait')
    monkeypatch.setattr(people.subprocess, 'Popen', lambda *a, **k: Process())
    with pytest.raises(ValueError, match='foreign cluster'):
        with people.database_window(cfg, root):
            pytest.fail('must not provision')
    assert actions == ['close pools','stop owned child','wait']


def test_custom_port_and_password_arguments_preserve_spaces(fresh):
    root, _ = fresh
    command = prepare.configuration_command(root, {'ffmpeg':root/'ffmpeg','ffprobe':root/'ffprobe'}, 55481,55482,True)
    assert command[0] == str(root/'.venv/bin/python3')
    assert command[command.index('--database-port')+1] == '55481'
    assert command[-1] == '--password-stdin'


def test_plan_does_not_write(tmp_path):
    script = Path(__file__).resolve().parents[1]/'installer/macos/install.sh'
    result = subprocess.run(['bash',str(script),'plan'],cwd=tmp_path,capture_output=True,text=True)
    assert result.returncode == 0
    assert 'review-only' in result.stdout
    assert list(tmp_path.iterdir()) == []


def test_core_rejects_external_data_before_creating_state(tmp_path, monkeypatch):
    monkeypatch.setenv('FAMILYFOTO_DATA_DIR', str(tmp_path/'elsewhere'))
    with pytest.raises(ValueError, match='overrides'):
        prepare.require_fresh(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_private_venv_symlink_is_valid_but_foreign_interpreter_is_not(tmp_path):
    from installer.native_people import verify_provider_paths
    base = tmp_path/'runtime/face_detect'
    env = base/'envs/test'
    subprocess.run([sys.executable,'-m','venv','--without-pip',str(env)],check=True)
    manifest = base/'manifest.json'
    manifest.write_text('{}')
    profile = {'python':str(env/'bin/python'), 'model_manifest':str(manifest)}
    verify_provider_paths(tmp_path,profile)
    foreign = base/'foreign/bin'
    foreign.mkdir(parents=True)
    (foreign/'python').symlink_to(sys.executable)
    profile['python'] = str(foreign/'python')
    with pytest.raises(ValueError,match='private environment'):
        verify_provider_paths(tmp_path,profile)


def test_closed_listener_time_wait_does_not_block_restart():
    import socket
    with socket.socket() as server:
        server.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        server.bind(('127.0.0.1',0)); server.listen()
        port=server.getsockname()[1]
        with socket.create_connection(('127.0.0.1',port)) as client:
            connection,_=server.accept()
            connection.close()
            assert client.recv(1)==b''
    prepare.require_free_ports([port])
