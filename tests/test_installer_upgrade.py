import importlib.util
import json
from pathlib import Path

import pytest

from app.auth import password_hash
from tests.backup_keys import ADMIN_PASSWORD


spec = importlib.util.spec_from_file_location('installer_upgrade',
    Path(__file__).resolve().parents[1] / 'installer/upgrade-catalog.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.fixture
def source(tmp_path):
    config, data = tmp_path / 'config', tmp_path / 'data'
    config.mkdir()
    data.mkdir()
    settings = {'database': {'host': '127.0.0.1', 'port': 55489, 'dbname': 'fixture',
                             'user': 'fixture', 'password': 'NEVER_PRINT_THIS'},
                'browser_user': 'family', 'browser_password_hash': password_hash(ADMIN_PASSWORD)}
    (config / 'local.json').write_text(json.dumps(settings))
    return ['--config-root', str(config), '--data-root', str(data)]


def test_default_plan_does_not_invoke_upgrade_or_create_paths(source, tmp_path, monkeypatch, capsys):
    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob('*'))
    def plan(endpoint, scripts):
        assert endpoint.password == 'NEVER_PRINT_THIS'
        assert scripts == module.ROOT / 'databases'
        return {'state': 'pending', 'backup_required': True}
    monkeypatch.setattr(module, 'plan_upgrade', plan)
    monkeypatch.setattr(module, 'upgrade_catalog', lambda *a, **k: pytest.fail('Plan must not apply'))
    assert module.main(source) == 0
    output = capsys.readouterr().out
    assert 'NEVER_PRINT_THIS' not in output
    assert json.loads(output)['writes_performed'] is False
    assert sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob('*')) == before


def test_apply_passes_explicit_paths_without_exposing_password(source, tmp_path, monkeypatch, capsys):
    from app.backup import passwords
    monkeypatch.setattr(module.passwords, 'from_arguments', lambda args: ADMIN_PASSWORD)

    def upgrade(endpoint, dump, config, data, target, secret, **kwargs):
        assert endpoint.password == 'NEVER_PRINT_THIS'
        assert isinstance(secret, passwords.BackupKey) and not target.exists()  # A new set, published by the capture.
        assert config == tmp_path / 'config' and data == tmp_path / 'data'
        assert target == tmp_path / 'backup'
        assert kwargs['offline_confirmed'] and kwargs['catalog_only_confirmed']
        assert kwargs['migrations_directory'] == module.ROOT / 'databases'
        return {'state': 'complete', 'jobs_resumed': False}
    monkeypatch.setattr(module, 'upgrade_catalog', upgrade)
    assert module.main(source + ['--mode', 'apply', '--offline', '--catalog-only-upgrade',
        '--backup-directory', str(tmp_path / 'backup'), '--backup-password-stdin',
        '--pg-dump', 'fixture-tool', '--database-budget-bytes', '1048576']) == 0
    output = capsys.readouterr().out
    assert 'NEVER_PRINT_THIS' not in output
    assert json.loads(output)['jobs_resumed'] is False


@pytest.mark.parametrize('flags', [[], ['--offline'], ['--offline', '--catalog-only-upgrade']])
def test_apply_refuses_incomplete_inputs_before_upgrade(source, flags, monkeypatch, capsys):
    monkeypatch.setattr(module, 'upgrade_catalog', lambda *a, **k: pytest.fail('Unsafe apply'))
    assert module.main(source + ['--mode', 'apply'] + flags) == 1
    assert json.loads(capsys.readouterr().out)['reason'] == 'ValueError'


def test_driver_error_is_redacted(source, monkeypatch, capsys):
    def fail(*args):
        raise RuntimeError('NEVER_PRINT_THIS')
    monkeypatch.setattr(module, 'plan_upgrade', fail)
    assert module.main(source) == 1
    assert 'NEVER_PRINT_THIS' not in capsys.readouterr().out


def test_custom_connection_options_not_silently_discarded(source, tmp_path, monkeypatch, capsys):
    path = tmp_path / 'config/local.json'
    config = json.loads(path.read_text())
    config['database']['options'] = '-c search_path=another_catalog'
    path.write_text(json.dumps(config))
    monkeypatch.setattr(module, 'plan_upgrade', lambda *args: pytest.fail('Unexpected plan'))
    assert module.main(source) == 1
    assert json.loads(capsys.readouterr().out)['reason'] == 'ValueError'


def test_the_pre_upgrade_backup_needs_the_administrator_password(source, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(module.passwords, 'from_arguments', lambda args: 'not the administrator password')
    monkeypatch.setattr(module, 'upgrade_catalog', lambda *a, **k: pytest.fail('Upgraded without the password'))
    assert module.main(source + ['--mode', 'apply', '--offline', '--catalog-only-upgrade',
        '--backup-directory', str(tmp_path / 'backup'), '--backup-password-stdin',
        '--pg-dump', 'fixture-tool', '--database-budget-bytes', '1048576']) == 1
    report = json.loads(capsys.readouterr().out)
    assert report['reason'] == 'not-administrator-password' and 'not the administrator' not in report['message']


@pytest.mark.parametrize('apply', [False, True])
def test_windows_wrapper_dispatches_without_installing(tmp_path, apply):
    from tests.test_installation import ROOT, powershell, literal
    scripts = tmp_path / 'installer/windows'
    scripts.mkdir(parents=True)
    (scripts / 'install.ps1').write_bytes((ROOT / 'installer/windows/install.ps1').read_bytes())
    (scripts / 'setup-common.ps1').write_text(
        "function Get-SetupSteps { @() }\n"
        "function Invoke-SetupCommand($Executable, $Arguments) { "
        "ConvertTo-Json -Compress -InputObject @($Executable, $Arguments) }\n")
    python = tmp_path / '.venv/Scripts/python.exe'
    python.parent.mkdir(parents=True)
    python.touch()  # Never executed: dispatch helper above is a test double.
    flags = (' -Offline -CatalogOnlyUpgrade -BackupDirectory backup'
             ' -PgDump dump -DatabaseBudgetBytes 12345') if apply else ''
    result = powershell('& ' + literal(scripts / 'install.ps1') + ' -Mode Upgrade' + flags)
    assert result.returncode == 0, result.stderr
    executable, arguments = json.loads(result.stdout)
    assert Path(executable) == python
    assert Path(arguments[0]) == tmp_path / 'installer/upgrade-catalog.py'
    assert arguments[1:5] == ['--config-root', str(tmp_path / 'config'), '--data-root', str(tmp_path / 'data')]
    if apply:
        assert arguments[5:9] == ['--mode', 'apply', '--offline', '--catalog-only-upgrade']
        assert arguments[-2:] == ['--database-budget-bytes', '12345']
    else:
        assert len(arguments) == 5  # Shared command defaults to plan.
    assert not (tmp_path / 'data').exists()


def test_legacy_membership_script_cannot_bypass_verified_backup(monkeypatch):
    from scripts import consolidate_import_membership as legacy
    def forbidden():
        pytest.fail('Legacy apply must refuse before database access')
    monkeypatch.setattr(legacy, 'connection', forbidden)
    with pytest.raises(RuntimeError, match='verified backup'):
        legacy.run(apply=True)
