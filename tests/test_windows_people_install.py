"""Fresh People installer guards; no provider installation or live DB access."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('windows_people', ROOT / 'installer/windows/people.py')
people = importlib.util.module_from_spec(spec)
spec.loader.exec_module(people)


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    monkeypatch.delenv('FAMILYFOTO_DATA_DIR', raising=False)
    (tmp_path / 'config').mkdir()
    cfg = {'host': '127.0.0.1', 'port': 55480, 'source': {}, 'destination': {},
           'database': {'host': '127.0.0.1', 'port': 55481}}
    (tmp_path / 'config/local.json').write_text(json.dumps(cfg))
    return tmp_path, cfg


def test_own_environment_required(fresh):
    root, cfg = fresh
    assert people.local_configuration(root, root / '.venv') == cfg
    with pytest.raises(ValueError, match='own virtual'):
        people.local_configuration(root, root / 'other')


@pytest.mark.parametrize('field,value', [
    ('live_native_catalog', True), ('live_existing_catalog', True),
    ('preview', {'data_root': 'external'}), ('source', {'root': 'source'}),
    ('destination', {'root': 'destination'}), ('host', '0.0.0.0'),
])
def test_refuses_existing_or_remote_deployments(fresh, field, value):
    root, cfg = fresh
    cfg[field] = value
    (root / 'config/local.json').write_text(json.dumps(cfg))
    with pytest.raises(ValueError):
        people.local_configuration(root, root / '.venv')


def test_external_data_override_refused(fresh, monkeypatch):
    root, _ = fresh
    monkeypatch.setenv('FAMILYFOTO_DATA_DIR', str(root / 'external'))
    with pytest.raises(ValueError, match='overrides'):
        people.local_configuration(root, root / '.venv')


@pytest.mark.parametrize('state', ['data/people-preview.json', 'data/face_detect', 'data/face-service-settings.json'])
def test_existing_state_refused_before_binding_or_setup(fresh, state):
    root, cfg = fresh
    path = root / state
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    with pytest.raises(ValueError, match='Existing People'):
        people.check_fresh(root, cfg, 55482)


def test_other_cluster_refused_before_schema_verification(fresh, monkeypatch):
    root, cfg = fresh
    cfg['postgres_admin_password'] = 'test-only'
    class DB:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, query):
            assert query in ('SET TRANSACTION READ ONLY', 'SHOW data_directory')
            return self
        def fetchone(self): return [str(root / 'other-cluster')]
    monkeypatch.setattr('psycopg.connect', lambda **kw: DB())
    with pytest.raises(ValueError, match='does not belong'):
        people.verify_database(root, cfg)


def test_people_port_preserved_on_later_start(tmp_path, monkeypatch):
    from face_detect.cli import prepare
    monkeypatch.setattr(prepare, 'DATA', tmp_path)
    assert prepare.service_port() == 54341
    assert prepare.service_port(55482) == 55482
    (tmp_path / 'people-preview.json').write_text(json.dumps({'port': 55482}))
    assert prepare.service_port() == 55482
    with pytest.raises(ValueError, match='reconfigure'):
        prepare.service_port(55483)
    with pytest.raises(ValueError, match='between'):
        prepare.service_port(80)


def test_installation_lock_excludes_second_writer(fresh):
    import sys
    if sys.platform != 'win32':
        pytest.skip('Windows installation lock')
    root, _ = fresh
    with people.installation_lock(root):
        with pytest.raises(ValueError, match='in progress'):
            with people.installation_lock(root):
                pytest.fail('Concurrent writer entered')
    with people.installation_lock(root):
        pass


@pytest.mark.skipif(__import__('sys').platform != 'win32', reason='Windows venv executable layout')
def test_shared_verifier_accepts_real_windows_venv(tmp_path):
    import subprocess
    import sys
    from installer.native_people import verify_provider_paths
    env = tmp_path / 'runtime/face_detect/envs/test'
    subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(env)], check=True)
    manifest = env.parent.parent / 'manifest.json'
    manifest.write_text('{}')
    verify_provider_paths(tmp_path, {
        'python': str(env / 'Scripts/python.exe'), 'model_manifest': str(manifest),
    })


def test_shared_verifier_rejects_foreign_windows_prefix(tmp_path, monkeypatch):
    from installer import native_people
    env = tmp_path / 'runtime/face_detect/envs/test'
    monkeypatch.setattr(native_people.subprocess, 'check_output', lambda *a, **kw: str(tmp_path / 'foreign'))
    with pytest.raises(ValueError, match='private environment'):
        native_people.verify_provider_paths(tmp_path, {
            'python': str(env / 'Scripts/python.exe'),
            'model_manifest': str(env.parent.parent / 'manifest.json'),
        })


def test_shared_verifier_rejects_external_manifest_before_execution(tmp_path, monkeypatch):
    from installer import native_people
    monkeypatch.setattr(native_people.subprocess, 'check_output', lambda *a, **kw: pytest.fail('executed foreign provider'))
    with pytest.raises(ValueError, match='assets must belong'):
        native_people.verify_provider_paths(tmp_path, {
            'python': str(tmp_path / 'runtime/face_detect/envs/test/Scripts/python.exe'),
            'model_manifest': str(tmp_path / 'external.json'),
        })
