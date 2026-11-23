import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.migration.launch import configure_environment


@pytest.fixture
def recovered(tmp_path, monkeypatch):
    for name in ('FAMILYFOTO_CONFIG_DIR', 'FAMILYFOTO_DATA_DIR', 'FAMILYFOTO_LOG_DIR', 'FAMILYFOTO_TEMP_DIR'):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv(name, '')  # restore the caller's environment after each test
    files = tmp_path / 'catalog/files'
    (files / 'config').mkdir(parents=True)
    (files / 'data/.recovery-admission').mkdir(parents=True)
    (files / 'config/local.json').write_text(json.dumps({'recovery': {'state': 'activated', 'activation_required': False}}))
    return tmp_path, files


def test_launch_selects_recovery_paths_not_code_checkout(recovered):
    directory, files = recovered
    configure_environment(directory)
    assert os.environ['FAMILYFOTO_CONFIG_DIR'] == str(files / 'config')
    assert os.environ['FAMILYFOTO_DATA_DIR'] == str(files / 'data')
    assert os.environ['FAMILYFOTO_LOG_DIR'] == str(directory / 'logs')
    assert not (directory / 'logs').exists()  # preparation does not start or write


@pytest.mark.parametrize('marker', ['maintenance.json', 'retired.json'])
def test_launch_refuses_incomplete_or_retired_target(recovered, marker):
    directory, files = recovered
    (files / 'data/.recovery-admission' / marker).write_text('{}')
    with pytest.raises(ValueError, match='maintenance'):
        configure_environment(directory)


def test_launch_refuses_validation_only_candidate(recovered):
    directory, files = recovered
    (files / 'config/local.json').write_text('{"recovery":{"activation_required":true}}')
    with pytest.raises(ValueError, match='activation'):
        configure_environment(directory)


def test_new_interpreter_imports_runtime_paths_after_recovery_selection(recovered):
    directory, files = recovered
    code = (
        'import json,sys\nfrom app.migration.launch import configure_environment\n'
        'configure_environment(sys.argv[1])\n'
        'from app.config import DATA,LOGS,TEMP,configuration_root\n'
        'from app.storage.config import CONFIG_PATH\n'
        'print(json.dumps(list(map(str,[DATA,LOGS,TEMP,configuration_root(),CONFIG_PATH]))))'
    )
    result = subprocess.run([sys.executable, '-B', '-c', code, str(directory)],
                            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == list(map(str, [files / 'data', directory / 'logs', directory / 'temp',
                                                      files / 'config', files / 'config/local.json']))
