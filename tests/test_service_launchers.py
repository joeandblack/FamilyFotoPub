"""The server launchers live in app.service; their project-root lookups must survive the move."""
import importlib
import json
import pytest


@pytest.mark.parametrize('name', ['server', 'desktop', 'live', 'readonly_preview', 'macos_lifecycle',
                                  'windows_readonly', 'stop_watch', 'folder_picker'])
# 'macos' is left out: importing it pulls in the Mac installer, whose import-time state other tests must not inherit.
def test_every_launcher_module_imports(name):
    importlib.import_module(f'app.service.{name}')


def test_live_launcher_finds_the_project_root():
    from pathlib import Path
    from app.service import live
    assert (live.ROOT / 'pyproject.toml').is_file() and live.ROOT == Path(__file__).resolve().parents[1]


def test_live_launcher_reads_the_project_config(tmp_path, monkeypatch):
    from app.service import live
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config' / 'local.json').write_text(json.dumps({'live_existing_catalog': False}))
    monkeypatch.setattr(live, 'ROOT', tmp_path)
    with pytest.raises(RuntimeError, match='existing-catalog'):
        live.main()


def test_live_launcher_sets_the_shared_data_directory_before_config_is_loaded(tmp_path):
    """Fresh process: config.DATA is fixed on first import, so the launcher must not import it earlier."""
    import subprocess
    import sys
    from pathlib import Path
    shared = tmp_path / 'shared-state'
    shared.mkdir()
    (shared / 'people-preview.json').write_text('{}')
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config' / 'local.json').write_text(json.dumps(
        {'live_existing_catalog': True, 'preview': {'data_root': str(shared)}}))
    script = """
import os, sys, types
from pathlib import Path
from app.service import live
assert 'app.config' not in sys.modules, 'live.py imported app.config before setting FAMILYFOTO_DATA_DIR'
live.ROOT = Path(sys.argv[1])
api = types.ModuleType('app.api'); api.app = object(); sys.modules['app.api'] = api
people = types.ModuleType('face_detect.api.readonly_people'); people.install = lambda *args: None
sys.modules['face_detect.api.readonly_people'] = people
desktop = types.ModuleType('app.service.desktop'); desktop.main = lambda: None
sys.modules['app.service.desktop'] = desktop
live.main()
del sys.modules['app.service.desktop']
from app import config
from app.service import desktop as real_desktop
print(config.DATA, real_desktop.STOP, os.environ['FAMILYFOTO_DATA_DIR'])
"""
    env = {k: v for k, v in __import__('os').environ.items() if k != 'FAMILYFOTO_DATA_DIR'}
    repo = Path(__file__).resolve().parents[1]
    done = subprocess.run([sys.executable, '-c', script, str(tmp_path)], cwd=repo, env={**env, 'PYTHONPATH': str(repo)},
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    data, stop, variable = done.stdout.split()
    assert Path(data) == shared.resolve() and Path(stop) == shared.resolve() / 'DESKTOP-SERVER-STOP'
    assert Path(variable) == shared.resolve()
