"""A second FamilyFoto installation on one Windows computer, and restoring a backup into it."""
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.service import instance
from test_installation import helpers, literal, powershell

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('windows_restore', ROOT / 'installer/windows/restore.py')
restore = importlib.util.module_from_spec(spec)
spec.loader.exec_module(restore)


# The names an installation owns.

def test_the_original_installation_keeps_its_names():
    assert instance.names({}) == instance.DEFAULT
    assert instance.names({'instance': None})['database_service'] == 'FamilyFotoPostgres'


def test_a_named_installation_has_its_own_names():
    names = instance.names({'instance': 'restore-test'})
    assert names == {'database_service': 'FamilyFotoPostgres-restore-test',
                     'server_task': 'FamilyFoto Desktop Server (restore-test)',
                     'shortcut': 'FamilyFoto Server (restore-test)',
                     'session_cookie': 'familyfoto_session_restore_test',
                     'backup_cookie': 'familyfoto_backup_run_restore_test'}


def test_a_backup_s_progress_page_answers_only_its_own_installation_s_cookie(tmp_path, monkeypatch):
    """Browsers keep cookies by host, not port: one installation's backup must not take over the other's."""
    from fastapi.testclient import TestClient
    from app.backup import admin_runner, admin_runs
    monkeypatch.setattr(admin_runs, 'RUNS', tmp_path)
    run = {'id': '20261113T000000Z-00000000', 'state': 'capturing', 'started_at': admin_runs.now(),
           'token_sha256': admin_runs.token_digest('token')}
    admin_runs.write_run(run)
    named = instance.backup_cookie({'instance': 'restore-test'})
    client = TestClient(admin_runner.status_app(admin_runner.Run(run['id']), named), base_url='https://testserver')
    client.cookies.set('familyfoto_backup_run', 'token')  # The other installation's backup.
    assert client.get('/api/backup-run').json()['authorized'] is False
    client.cookies.set(named, 'token')
    assert client.get('/api/backup-run').json()['authorized'] is True


@pytest.mark.parametrize('bad', ['', 'Upper', '-lead', 'a' * 25, "it's", 'a b', 'x;y', 3])
def test_an_unsafe_name_is_refused(bad):
    with pytest.raises(ValueError):
        instance.names({'instance': bad})


@pytest.mark.parametrize('name', [None, 'restore-test', 'b2'])
def test_powershell_and_python_derive_the_same_names(name):
    argument = f"'{name}'" if name else "''"
    result = powershell(f". {literal(ROOT / 'scripts/instance-names.ps1')}; "
                        f"$n=Get-FamilyFotoNames {argument}; @($n.DatabaseService,$n.ServerTask,$n.Shortcut) -join '|'")
    assert result.returncode == 0, result.stderr
    expected = instance.names({'instance': name} if name else {})
    assert result.stdout.strip() == '|'.join((expected['database_service'], expected['server_task'], expected['shortcut']))


def test_powershell_refuses_an_unsafe_name():
    result = powershell(f". {literal(ROOT / 'scripts/instance-names.ps1')}; Get-FamilyFotoNames 'Bad Name'")
    assert result.returncode != 0


def test_the_name_is_a_setting_of_this_computer_in_a_move():
    from app.migration import settings_rules
    assert settings_rules.RULES['instance'] == settings_rules.INSTALLATION
    merged = settings_rules.merge({'browser_user': 'family', 'instance': 'old-name'}, {'instance': 'restore-test'})
    assert merged['instance'] == 'restore-test'


# Ports.

def layout(arguments):
    return helpers(f"$l=Resolve-SetupLayout {arguments}; "
                   "@($l.Names.DatabaseService,$l.Database,$l.Web,$l.People) -join '|'")


def test_the_original_installation_keeps_its_ports():
    result = layout("'' 0 0 0")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'FamilyFotoPostgres|5432|4340|54341'


def test_a_named_installation_uses_the_ports_it_was_given():
    result = layout("'restore-test' 5434 4342 54344")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'FamilyFotoPostgres-restore-test|5434|4342|54344'


@pytest.mark.parametrize('arguments, message', [
    ("'' 5434 0 0", 'add -Instance'),
    ("'restore-test' 5434 4342 0", 'needs its own ports'),
    ("'restore-test' 5432 4342 54344", 'original installation uses'),
    ("'restore-test' 5434 4340 54344", 'original installation uses'),
    ("'restore-test' 5434 5434 54344", 'must differ'),
    ("'restore-test' 5434 4342 5000", 'People port between'),
])
def test_ports_that_would_clash_are_refused(arguments, message):
    result = layout(arguments)
    assert result.returncode != 0 and message in result.stderr


def service_check(tmp_path, registered, name=None):
    """Assert-SetupDatabaseService against a stand-in service list holding one service."""
    (tmp_path / 'data/postgres').mkdir(parents=True)
    (tmp_path / 'data/postgres/PG_VERSION').write_text('17\n')
    exe, cluster = tmp_path / 'runtime/postgres/bin/pg_ctl.exe', tmp_path / 'data/postgres'
    path_name = f'"{exe}" runservice -N "{registered}" -D "{cluster}" -w'
    stand_in = ("function Get-CimInstance { param($ClassName, $Filter) "
                f"if ($Filter -eq \"Name='{registered}'\") {{ [pscustomobject]@{{ PathName = '{path_name}' }} }} }}; ")
    call = f"Assert-SetupDatabaseService {literal(tmp_path)}" + (f" '{name}'" if name else '')
    return helpers(stand_in + call + "; Write-Output 'service ok'")


def test_the_service_check_passes_for_this_installation_s_own_service(tmp_path):
    """It once named its parameter $Service, which turned the $service object into text."""
    result = service_check(tmp_path, 'FamilyFotoPostgres')
    assert result.returncode == 0, result.stderr
    assert 'service ok' in result.stdout


def test_the_service_check_passes_for_a_named_installation_s_service(tmp_path):
    result = service_check(tmp_path, 'FamilyFotoPostgres-restore-test', 'FamilyFotoPostgres-restore-test')
    assert result.returncode == 0, result.stderr


def test_the_service_check_refuses_another_installation_s_service(tmp_path):
    result = service_check(tmp_path, 'FamilyFotoPostgres', 'FamilyFotoPostgres-restore-test')
    assert result.returncode != 0 and 'does not belong to this installation' in result.stderr


def test_the_second_installation_never_runs_postgresql_s_own_installer():
    """That installer keeps one PostgreSQL 17 record per computer; the first installation owns it."""
    script = (ROOT / 'installer/windows/install-postgres.ps1').read_text()
    named = script[script.index('# A second installation'):]
    assert '$Installer' not in named and 'pg_ctl.exe' in named and "'register'" in named
    assert 'PostgreSQL 17 is already installed' in script
    common = (ROOT / 'installer/windows/setup-common.ps1').read_text()
    assert 'postgresql-17.11-5-windows-x64-binaries.zip' in common


def test_no_installer_or_runtime_script_names_the_original_service_or_task_directly():
    for path in ('installer/windows/install.ps1', 'installer/windows/install-desktop-launcher.ps1',
                 'installer/windows/install-postgres.ps1', 'scripts/start.ps1', 'scripts/desktop-control.ps1'):
        text = (ROOT / path).read_text()
        assert "'FamilyFotoPostgres'" not in text and 'FamilyFotoPostgres ' not in text, path
        assert "'FamilyFoto Desktop Server'" not in text, path


def test_runtime_scripts_parse_without_execution():
    result = powershell(
        "$ErrorActionPreference='Stop'; Get-ChildItem -LiteralPath " + literal(ROOT / 'scripts')
        + " -Filter '*.ps1' | ForEach-Object { $tokens=$null; $errors=$null; "
        "[void][System.Management.Automation.Language.Parser]::ParseFile($_.FullName,"
        "[ref]$tokens,[ref]$errors); if($errors.Count){throw ($errors | Out-String)} }")
    assert result.returncode == 0, result.stderr


def test_plan_describes_a_second_installation_without_changing_anything(tmp_path):
    result = powershell('& ' + literal(ROOT / 'installer/windows/install.ps1')
                        + ' -Mode Plan -Instance restore-test -DatabasePort 5434 -WebPort 4342 -PeoplePort 54344')
    assert result.returncode == 0, result.stderr
    assert "Second installation 'restore-test'" in result.stdout
    assert 'FamilyFotoPostgres-restore-test on port 5434' in result.stdout


def test_restore_options_need_a_restore():
    result = powershell('& ' + literal(ROOT / 'installer/windows/install.ps1') + ' -Mode Resume -Media copy')
    assert result.returncode != 0 and 'use -Mode Restore' in result.stderr


def test_a_configuration_records_the_installation_name():
    spec = importlib.util.spec_from_file_location('configure', ROOT / 'installer/configure.py')
    configure = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(configure)
    assert configure.instance_name('restore-test') == 'restore-test'
    with pytest.raises(Exception):
        configure.instance_name('Restore Test')


# The Windows restore.

def write_config(root, **changes):
    (root / 'config').mkdir(parents=True, exist_ok=True)
    config = {'port': 4342, 'database': {'port': 5434}, 'postgres_admin_password': 'p', 'instance': 'restore-test',
              **changes}
    (root / 'config/local.json').write_text(json.dumps(config), encoding='utf-8')
    return config


def test_the_restore_uses_this_installation_and_keeps_its_own_data_folders(tmp_path, monkeypatch):
    monkeypatch.setattr(restore, 'ROOT', tmp_path)
    write_config(tmp_path)
    started = []
    monkeypatch.setattr(restore, 'start', lambda config, names: started.append(names['server_task']))
    made = restore.installation(54344)
    assert made.keep_data == ('postgres', 'setup')
    assert made.web_port == 4342 and made.people_port == 54344 and made.admin.port == 5434
    assert made.pg_restore == tmp_path / 'runtime/postgres/bin/pg_restore.exe'
    made.start()
    assert started == ['FamilyFoto Desktop Server (restore-test)']


def test_the_database_service_started_is_this_installation_s(tmp_path, monkeypatch):
    commands = []
    monkeypatch.setattr(restore, 'powershell', lambda command, timeout=120: commands.append(command) or SimpleNamespace(returncode=0))
    monkeypatch.setattr(restore, 'listening', lambda port: port == 5434)
    config = {'port': 4342, 'database': {'port': 5434}}
    with restore.database(config, instance.names({'instance': 'restore-test'})):
        pass
    assert commands == ["Start-Service -Name 'FamilyFotoPostgres-restore-test'"]


def test_the_restore_refuses_while_this_installation_s_web_server_runs(monkeypatch):
    monkeypatch.setattr(restore, 'listening', lambda port: True)
    with pytest.raises(SystemExit, match='already using web port 4342'):
        with restore.database({'port': 4342, 'database': {'port': 5434}}, instance.DEFAULT):
            pass


def test_the_certificate_names_are_windows_names(monkeypatch):
    monkeypatch.setattr(restore.socket, 'gethostname', lambda: 'FAMILY-PC')
    assert restore.certificate_names() == ['127.0.0.1', 'localhost', 'family-pc']


@pytest.fixture
def any_key(monkeypatch):
    """For tests of the other questions: the password is asked separately, after the folders."""


def ask_args(tmp_path, **changes):
    backup = tmp_path / 'backup'
    (backup / 'catalog').mkdir(parents=True, exist_ok=True)
    values = dict(backup=backup, media=None, media_folder=None,
                  allow_unprotected_media=False, unattended=False)
    values.update(changes)
    return SimpleNamespace(**values)


def test_a_folder_that_will_not_do_is_asked_for_again(tmp_path, monkeypatch, any_key):
    from app.migration import media_folder
    (tmp_path / 'other').mkdir()
    (tmp_path / 'good').mkdir()
    checks = []

    def check(backup, media, folder, allow_unprotected=False):
        checks.append((media, folder.parent.name))
        if folder.parent.name == 'other':
            raise media_folder.UnsuitableMediaFolder('is on another disk')
    monkeypatch.setattr(media_folder, 'check_media_folder', check)
    answers = iter(['1', '2'])
    folders = iter([tmp_path / 'other', tmp_path / 'good'])
    args = restore.ask(ask_args(tmp_path), ask_input=lambda prompt: next(answers), choose=lambda kind, title: next(folders))
    assert checks == [('in-place', 'other'), ('copy', 'good')]
    assert args.media == 'copy' and args.media_folder == tmp_path / 'good/FFLibrary'


def test_an_unprotected_disk_is_used_only_on_a_yes(tmp_path, monkeypatch, any_key):
    from app.migration import media_folder

    def check(backup, media, folder, allow_unprotected=False):
        if not allow_unprotected:
            raise media_folder.UnsuitableMediaFolder('cannot keep files private', unprotected=True)
    monkeypatch.setattr(media_folder, 'check_media_folder', check)
    args = restore.ask(ask_args(tmp_path, media='copy', media_folder=tmp_path / 'lib'), ask_input=lambda prompt: 'yes',
                       choose=lambda kind, title: pytest.fail('nothing to choose'))
    assert args.allow_unprotected_media is True


def test_unattended_a_folder_that_will_not_do_stops(tmp_path, monkeypatch, any_key):
    from app.migration import media_folder

    def check(backup, media, folder, allow_unprotected=False):
        raise media_folder.UnsuitableMediaFolder('is on another disk')
    monkeypatch.setattr(media_folder, 'check_media_folder', check)
    with pytest.raises(media_folder.UnsuitableMediaFolder):
        restore.ask(ask_args(tmp_path, media='in-place', media_folder=tmp_path / 'lib', unattended=True),
                    ask_input=lambda prompt: 'no', choose=lambda kind, title: None)


def test_unattended_an_unprotected_disk_names_the_flag_and_asks_nothing(tmp_path, monkeypatch, any_key):
    from app.migration import media_folder

    def check(backup, media, folder, allow_unprotected=False):
        raise media_folder.UnsuitableMediaFolder('cannot keep files private', unprotected=True)
    monkeypatch.setattr(media_folder, 'check_media_folder', check)
    with pytest.raises(media_folder.UnsuitableMediaFolder, match='add -AllowUnprotectedMedia'):
        restore.ask(ask_args(tmp_path, media='copy', media_folder=tmp_path / 'lib', unattended=True),
                    ask_input=lambda prompt: pytest.fail('asked with no one there'), choose=lambda kind, title: None)


def test_an_unattended_restore_needs_every_choice(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(restore, 'ROOT', tmp_path)
    write_config(tmp_path)
    assert restore.main(['--people-port', '54344', '--people', 'skip', '--unattended', '--backup', str(tmp_path)]) == 1
    assert 'unattended restore needs' in capsys.readouterr().err
    assert not (tmp_path / 'restore').exists()


def test_unattended_the_switch_waits_for_its_flag(capsys):
    confirm = restore.confirm_switch(False, False)
    assert confirm({'media_files': 3}) is False
    assert restore.confirm_switch(True, False)({'media_files': 3}) is True


@pytest.mark.skipif(os.name != 'nt', reason='Windows desktop server')
def test_a_moved_library_s_people_are_served_by_the_windows_server(monkeypatch):
    from app.service import desktop
    from app.migration import launch
    served = []
    monkeypatch.setattr(launch, 'restored_people', lambda app, config, data, root: served.append(config['recovery']))
    desktop.serve_restored_people({'recovery': {'state': 'activated'}})
    desktop.serve_restored_people({})
    assert served == [{'state': 'activated'}]


def test_a_wrong_password_is_asked_for_again_at_the_console(tmp_path, monkeypatch, capsys):
    from app.migration import move_in
    tried = []

    def unlock(backup, password):
        tried.append(password)
        if password == 'wrong password':
            raise move_in.WrongBackupPassword('The password is incorrect')
        return 'the key'
    monkeypatch.setattr(move_in, 'unlock_backup', unlock)
    answers = iter(['right password'])
    password, key = restore.working_password(tmp_path, 'wrong password', True, ask_for=lambda: next(answers))
    assert tried == ['wrong password', 'right password'] and (password, key) == ('right password', 'the key')
    assert 'Type it again' in capsys.readouterr().out


def test_a_password_from_a_pipe_is_final_and_unattended_never_asks(tmp_path, monkeypatch):
    from app.backup.diagnostics import ExplainedFailure
    from app.migration import move_in

    def unlock(backup, password):
        raise move_in.WrongBackupPassword('The password is incorrect')
    monkeypatch.setattr(move_in, 'unlock_backup', unlock)
    with pytest.raises(move_in.WrongBackupPassword):
        restore.working_password(tmp_path, 'from the pipe', False, ask_for=lambda: pytest.fail('asked'))
    with pytest.raises(ExplainedFailure, match='needs the administrator password on stdin'):
        restore.working_password(tmp_path, None, False, ask_for=lambda: pytest.fail('asked'))


def test_the_restore_reads_the_password_only_from_stdin():
    args = restore.parser().parse_args(['--people-port', '54343', '--backup-password-frame-stdin'])
    assert args.backup_password_frame_stdin and not args.backup_password_stdin
    with pytest.raises(SystemExit):
        restore.parser().parse_args(['--people-port', '54343', '--key-file', 'k'])
    with pytest.raises(SystemExit):
        restore.parser().parse_args(['--people-port', '54343', '--backup-password-stdin', '--backup-password-frame-stdin'])


def test_a_folder_that_is_not_a_backup_is_asked_for_again(tmp_path, any_key):
    backup = tmp_path / 'backup'
    (backup / 'catalog').mkdir(parents=True)
    choices = iter([backup, tmp_path / 'lib-parent'])
    (tmp_path / 'lib-parent').mkdir()
    from app.migration import media_folder
    args = ask_args(tmp_path, backup=tmp_path / 'not-a-backup', media='copy')
    import unittest.mock
    with unittest.mock.patch.object(media_folder, 'check_media_folder', lambda *a, **k: None):
        args = restore.ask(args, ask_input=lambda prompt: '2', choose=lambda kind, title: next(choices))
    assert args.backup == backup
