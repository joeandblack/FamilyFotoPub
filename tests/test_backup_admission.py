import json
import os
import subprocess
import sys

import pytest

from app.backup.admission import (
    MaintenanceBlocked, acknowledge_failed_maintenance, maintenance, participant,
)


def test_participant_blocks_maintenance(tmp_path):
    with participant(tmp_path):
        with pytest.raises(MaintenanceBlocked, match='Stop application'):
            with maintenance(tmp_path, 'backup'):
                pytest.fail('Admitted concurrent backup')
    with maintenance(tmp_path, 'backup'):
        assert (tmp_path / '.recovery-admission/maintenance.json').exists()
    assert not (tmp_path / '.recovery-admission/maintenance.json').exists()


def test_failure_requires_explicit_matching_acknowledgement(tmp_path):
    with pytest.raises(RuntimeError):
        with maintenance(tmp_path, 'backup') as identity:
            raise RuntimeError('injected capture failure')
    with pytest.raises(MaintenanceBlocked):
        with participant(tmp_path):
            pass
    with pytest.raises(MaintenanceBlocked):
        acknowledge_failed_maintenance(tmp_path, 'wrong')
    acknowledge_failed_maintenance(tmp_path, identity)
    with participant(tmp_path):
        pass


def test_registration_latch_blocks_new_process(tmp_path):
    code = "from app.backup.admission import participant; import sys\nwith participant(sys.argv[1]): pass"
    with maintenance(tmp_path, 'backup'):
        result = subprocess.run([sys.executable, '-c', code, str(tmp_path)], capture_output=True, timeout=15)
        assert result.returncode != 0


def test_config_runtime_enrolls_before_creating_worker_directories(tmp_path):
    data = tmp_path / 'data'
    data.mkdir()
    code = (
        "import sys; from pathlib import Path; from app import config\n"
        "config.DATA=Path(sys.argv[1]); config.LOGS=config.DATA/'logs'; config.TEMP=config.DATA/'temp'\n"
        "config.initialize_runtime()\n"
    )
    with maintenance(data, 'backup'):
        result = subprocess.run([sys.executable, '-c', code, str(data)], capture_output=True, timeout=15)
        assert result.returncode != 0
        assert not (data / 'logs').exists()
        assert not (data / 'temp').exists()
    result = subprocess.run([sys.executable, '-c', code, str(data)], capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr.decode(errors='replace')
    with maintenance(data, 'backup'):
        pass


@pytest.mark.parametrize('entry', ['face-setup', 'upgrade-auth', 'configure', 'connection', 'dedicated_connection'])
def test_maintenance_tools_admit_before_first_write(tmp_path, entry):
    data = tmp_path / 'data'
    data.mkdir()
    config = tmp_path / 'config'
    config.mkdir()
    if entry == 'face-setup':
        code = (
            'from face_detect.cli import setup\n'
            "setup.platform_spec=lambda: ('windows-amd64','windows','Scripts/python.exe')\n"
            "setup.setup('insightface', research_only=True)\n"
        )
    elif entry == 'upgrade-auth':
        code = 'from scripts.upgrade_auth import upgrade\nfrom app.config import configuration_root\nupgrade(configuration_root()/"local.json")'
    elif entry in {'connection', 'dedicated_connection'}:
        code = f'from app.database import {entry}\nwith {entry}({{"host":"must-not-connect.invalid"}}): pass'
    else:
        code = (
            'import runpy,sys\nfrom pathlib import Path\n'
            "module=runpy.run_path('installer/configure.py')\n"
            "module['main'].__globals__['ROOT']=Path(sys.argv[1])\n"
            "sys.argv=['configure.py','--password-stdin']\nmodule['main']()"
        )
    env = {**os.environ, 'FAMILYFOTO_DATA_DIR': str(data), 'FAMILYFOTO_CONFIG_DIR': str(config)}
    with pytest.raises(RuntimeError, match='interrupted'):
        with maintenance(data, 'backup'):
            raise RuntimeError('interrupted')
    result = subprocess.run([sys.executable, '-c', code, str(tmp_path)], env=env,
                            capture_output=True, timeout=15, input=b'not-used-password\n')
    assert result.returncode != 0
    assert b'MaintenanceBlocked' in result.stderr, result.stderr.decode(errors='replace')
    assert list(config.iterdir()) == []
    assert not (tmp_path / 'runtime').exists()


@pytest.mark.parametrize('mode', ['participant', 'maintenance'])
def test_process_crash_releases_os_lock_but_preserves_maintenance(tmp_path, mode):
    call = 'participant(sys.argv[1])' if mode == 'participant' else "maintenance(sys.argv[1], 'backup')"
    code = f"from app.backup.admission import participant,maintenance; import sys,os\nwith {call}: os._exit(27)"
    result = subprocess.run([sys.executable, '-c', code, str(tmp_path)], capture_output=True, timeout=15,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    assert result.returncode == 27
    if mode == 'maintenance':
        marker = tmp_path / '.recovery-admission/maintenance.json'
        identity = json.loads(marker.read_text())['id']
        acknowledge_failed_maintenance(tmp_path, identity)
    with maintenance(tmp_path, 'backup'):
        pass


def sddl_names(sid):
    """How icacls /save may write this account: its SID, or the SDDL alias of a built-in one
    (the GitHub Windows runner runs as the built-in Administrator, saved as LA)."""
    return {sid} | ({'LA'} if sid.endswith('-500') else set()) | ({'LG'} if sid.endswith('-501') else set())


@pytest.mark.skipif(os.name != 'nt', reason='Windows ACLs')
def test_lease_grants_installer_account_on_gate_and_private_job(tmp_path):
    import csv

    identity = subprocess.check_output(['whoami', '/user', '/fo', 'csv', '/nh'])
    sid = next(csv.reader(identity.decode(errors='replace').splitlines()))[1]
    with participant(tmp_path):
        gate = tmp_path / '.recovery-admission'
        job = next(path for path in gate.iterdir() if path.is_dir())
        for path in (gate, job, job / 'copy.lock'):
            saved = tmp_path / 'acl'
            subprocess.run(['icacls', path, '/save', saved], check=True)
            acl = saved.read_text(encoding='utf-16-le')
            assert any(f';FA;;;{name})' in acl for name in sddl_names(sid)), acl
