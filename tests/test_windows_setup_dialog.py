"""The Windows setup dialog: its answer checks, the runner it uses, its progress lines, its pages."""
import importlib.util
import io
import json
import os
import time
from pathlib import Path
import sys

import pytest

from test_installation import literal, powershell

ROOT = Path(__file__).resolve().parents[1]
WINDOWS = ROOT / 'installer/windows'
spec = importlib.util.spec_from_file_location('windows_restore', WINDOWS / 'restore.py')
restore = importlib.util.module_from_spec(spec)
spec.loader.exec_module(restore)


def checks(code):
    return powershell("$ErrorActionPreference='Stop'; . " + literal(WINDOWS / 'setup-common.ps1') + '; . '
                      + literal(WINDOWS / 'setup-dialog-checks.ps1') + '; ' + code)


def sealed_backup(tmp_path, password='correct horse battery staple'):
    """A backup folder with a real password context and sealed index, as capture-library writes them."""
    from app.backup import passwords
    from app.backup.encrypted import seal_stream
    backup = tmp_path / 'backup'
    (backup / 'catalog').mkdir(parents=True)
    key = passwords.create_context(backup, passwords.BackupPassword(password))
    seal_stream(io.BytesIO(b'{"index": true}'), backup / 'library', key, estimated_bytes=64)
    (backup / 'capture-timing.json').write_text(json.dumps({'media_bytes': 5 * 1024 ** 3}))
    return backup


def password_helpers(code):
    return powershell("$ErrorActionPreference='Stop'; . " + literal(WINDOWS / 'setup-password.ps1') + '; ' + code)


def run_powershell(code, data):
    """Windows PowerShell with `data` on its stdin, as the setup window starts install.ps1."""
    import base64
    import shutil
    import subprocess
    if os.name != 'nt' or not shutil.which('powershell.exe'):
        pytest.skip('Windows PowerShell helper test')
    encoded = base64.b64encode(code.encode('utf-16le')).decode()
    return subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', encoded],
                          input=data, capture_output=True, timeout=60)


# The backup password. No key check of its own: the window prepares a private Python and runs the
# restore's own code (installer/password_check.py, tested in test_backup_passwords.py).

def test_the_window_s_frame_is_the_engine_s_frame():
    from app.backup import passwords
    text = ' Gr\u00fc\u00dfe "quoted" \\ back\tslash \u6771\u4eac '
    code = ("Add-Type -Path " + literal(WINDOWS / 'setup-run.cs') + "; "
            "[Convert]::ToBase64String([FamilyFotoSetupRun]::Frame([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('"
            + __import__('base64').b64encode(text.encode()).decode() + "'))))")
    result = powershell(code)
    assert result.returncode == 0, result.stderr
    frame = __import__('base64').b64decode(result.stdout.strip())
    assert passwords.read_frame(io.BytesIO(frame)) == text


def test_install_reads_exactly_one_frame_and_its_children_get_nothing_more(tmp_path):
    from app.backup import passwords
    frame = passwords.frame('correct horse battery staple')
    child = tmp_path / 'child.py'
    child.write_text('import sys\nprint("child", len(sys.stdin.buffer.read()))\n')
    code = (". " + literal(WINDOWS / 'setup-password.ps1') + "; $f = Read-SetupPasswordFrame; "
            "'frame ' + $f.Length; & " + literal(sys.executable) + " " + literal(child))
    result = run_powershell(code, frame)
    assert result.returncode == 0, result.stderr
    assert result.stdout.decode().split() == ['frame', str(len(frame)), 'child', '0']


@pytest.mark.parametrize('data, message', [(b'\x00\x00\x00\x10{"pass', 'cut short'), (b'\x00\x01\x00\x00', 'not valid'),
                                           (b'', 'cut short')])
def test_a_bad_frame_stops_install_before_anything(data, message):
    result = run_powershell(". " + literal(WINDOWS / 'setup-password.ps1') + "; Read-SetupPasswordFrame | Out-Null; 'read'", data)
    assert result.returncode != 0 and message in result.stderr.decode() and b'read' not in result.stdout


def test_only_the_restore_child_gets_the_frame(tmp_path):
    """The frame goes to the one child it is for, on its own pipe; a child of that child reads nothing."""
    from app.backup import passwords
    script = tmp_path / 'child.py'
    script.write_text("import subprocess, sys\n"
                      "sys.path.insert(0, sys.argv[1])\n"
                      "from app.backup import passwords\n"
                      "print('child', passwords.read_frame(sys.stdin.buffer) == 'correct horse battery staple')\n"
                      "print('grandchild', subprocess.run([sys.executable, '-c', 'import sys; print(len(sys.stdin.buffer.read()))'],"
                      " capture_output=True, text=True).stdout.strip(), flush=True)\n")
    code = (". " + literal(WINDOWS / 'setup-password.ps1') + "; Add-Type -Path " + literal(WINDOWS / 'setup-run.cs') + "; "
            "$code = Invoke-SetupPasswordChild " + literal(sys.executable) + " @(" + literal(script) + ", " + literal(ROOT)
            + ") ([FamilyFotoSetupRun]::Frame('correct horse battery staple')); 'exit ' + $code")
    result = run_powershell(code, b'')
    assert result.returncode == 0, result.stderr
    assert result.stdout.decode().split() == ['child', 'True', 'grandchild', '0', 'exit', '0'], result.stderr.decode(errors='replace')
    assert b'horse' not in result.stdout + result.stderr


def test_a_hung_password_check_is_stopped_at_its_deadline(tmp_path):
    """The helper never closes its output and never exits: the check must still give up, and kill it."""
    marker = tmp_path / 'started.txt'
    code = (". " + literal(WINDOWS / 'setup-password.ps1') + "; Add-Type -Path " + literal(WINDOWS / 'setup-run.cs') + "; "
            "$frame = [FamilyFotoSetupRun]::Frame('correct horse battery staple'); "
            "try { Invoke-SetupPasswordCheck 'R' 'R' 'B' $frame 3000 @('powershell.exe', '-NoProfile', '-Command', "
            + literal("'x' | Set-Content " + str(marker).replace("'", "''") + "; Start-Sleep 300") + ") | Out-Null; 'no-error' } "
            "catch { 'stopped: ' + $_.Exception.Message }")
    started = time.monotonic()
    result = run_powershell(code, b'')
    assert result.returncode == 0, result.stderr
    assert result.stdout.decode().strip() == 'stopped: The password check did not finish.'
    assert time.monotonic() - started < 60


def test_a_chatty_password_check_does_not_deadlock():
    code = (". " + literal(WINDOWS / 'setup-password.ps1') + "; Add-Type -Path " + literal(WINDOWS / 'setup-run.cs') + "; "
            "$frame = [FamilyFotoSetupRun]::Frame('correct horse battery staple'); "
            "$answer = Invoke-SetupPasswordCheck 'R' 'R' 'B' $frame 60000 @('powershell.exe', '-NoProfile', '-Command', "
            + literal("[Console]::Error.Write('e' * 300000); '{\"ok\": true}'") + "); $answer.ok")
    result = run_powershell(code, b'')
    assert result.returncode == 0, result.stderr
    assert result.stdout.decode().strip() == 'True'


def test_install_never_passes_the_password_as_an_argument():
    script = (WINDOWS / 'install.ps1').read_text()
    first_child = min(script.index(marker) for marker in ('Invoke-SetupCommand', '& $pythonExe'))
    assert script.index('Read-SetupPasswordFrame') < first_child  # Read before any native child can inherit stdin.
    assert '-BackupPasswordFromStdin and -PasswordFromStdin are different messages' in script
    assert "Invoke-SetupPasswordChild $pythonExe ($restoreArgs + '--backup-password-frame-stdin') $backupPasswordFrame" in script
    assert 'RecoveryKeyFile' not in script and '--key-file' not in script
    # A fresh restore checks the password before Core is installed.
    assert script.index('Invoke-SetupPasswordCheck') < script.index("foreach ($step in $steps)")


def test_the_temporary_runtime_is_pinned_and_registers_nothing():
    source = (WINDOWS / 'setup-password.ps1').read_text()
    for pinned in ('cryptography==44.0.3', 'cffi==2.1.1', '--require-hashes', '--no-index', '--no-cache-dir', '--isolated',
                   '--only-binary', 'Expand-PrivatePythonRuntime', '$PythonNuGetSha256'):
        assert pinned in source, pinned
    assert 'python-3.12' not in source.lower() and '.exe' not in source.replace('python.exe', '')  # No python.org installer.


def test_only_our_abandoned_runtimes_are_removed(tmp_path):
    temp = tmp_path / 'temp'
    ours, alive, other = (temp / ('password-check-' + 'a' * 32), temp / ('password-check-' + 'b' * 32), temp / 'password-check-x')
    for folder in (ours, alive, other):
        folder.mkdir(parents=True)
    (ours / 'owner.txt').write_text('999999')  # No such process.
    (alive / 'owner.txt').write_text(str(os.getpid()))
    result = password_helpers(f"Clear-SetupPasswordRuntimes {literal(tmp_path)}; "
                              f"try {{ Remove-SetupPasswordRuntime {literal(tmp_path)} {literal(tmp_path / 'backup')} }} catch {{ 'refused' }}")
    (tmp_path / 'backup').mkdir()
    assert result.returncode == 0, result.stderr
    assert not ours.exists() and alive.exists() and other.exists()
    result = password_helpers(f"try {{ Remove-SetupPasswordRuntime {literal(tmp_path)} {literal(tmp_path / 'backup')} }} catch {{ 'refused' }}")
    assert result.stdout.strip() == 'refused' and (tmp_path / 'backup').exists()


# The other answers.

def test_only_a_finished_password_backup_folder_will_do(tmp_path):
    import shutil
    backup = sealed_backup(tmp_path)
    (tmp_path / 'unfinished/catalog').mkdir(parents=True)
    old = tmp_path / 'key-file-backup'
    shutil.copytree(backup, old)
    (old / 'password.json').unlink()
    result = checks(f"'[' + (Test-SetupBackupFolder {literal(backup)}) + ']'; Test-SetupBackupFolder {literal(tmp_path)}; "
                    f"Test-SetupBackupFolder {literal(tmp_path / 'unfinished')}; Test-SetupBackupFolder ''; "
                    f"Test-SetupBackupFolder {literal(old)}")
    lines = result.stdout.strip().splitlines()
    assert lines[0] == '[]'
    assert 'no "catalog" folder' in lines[1] and 'not a finished backup' in lines[2] and 'Choose the backup folder' in lines[3]
    assert 'This backup format is not supported' in lines[4]


def media(backup, kind, parent, size='$null'):
    return checks(f"$r = Test-SetupMediaFolder {literal(backup)} '{kind}' {literal(parent)} {size}; "
                  "'problem=' + $r.Problem; 'folder=' + $r.Folder; 'unprotected=' + $r.Unprotected")


def test_a_copy_makes_a_new_library_folder_where_chosen(tmp_path):
    backup = sealed_backup(tmp_path)
    (tmp_path / 'photos').mkdir()
    result = media(backup, 'copy', tmp_path / 'photos', '1024')
    assert 'problem=\n' in result.stdout + '\n' or 'problem=\r' in result.stdout
    assert f"folder={tmp_path / 'photos' / 'FFLibrary'}" in result.stdout


@pytest.mark.parametrize('case, message', [
    ('exists', 'already exists'),
    ('inside', 'outside the backup'),
    ('room', 'Choose a folder on a disk with more room'),
    ('missing', 'is not a folder'),
])
def test_a_library_folder_that_will_not_do_says_why(tmp_path, case, message):
    backup = sealed_backup(tmp_path)
    parent, size = tmp_path / 'photos', '$null'
    parent.mkdir()
    if case == 'exists':
        (parent / 'FFLibrary').mkdir()
    elif case == 'inside':
        parent = backup
    elif case == 'room':
        size = str(10 ** 18)
    elif case == 'missing':
        parent = tmp_path / 'nowhere'
    assert message in media(backup, 'copy', parent, size).stdout


def test_in_place_needs_the_backup_s_disk(tmp_path):
    backup = sealed_backup(tmp_path)
    other = 'C:\\' if not str(tmp_path).upper().startswith('C:') else 'D:\\'
    if not Path(other).exists():
        pytest.skip('Needs a second disk')
    assert "on the backup's disk" in media(backup, 'in-place', other).stdout.replace('backup&#39;s', "backup's") or \
        'library folder must be on' in media(backup, 'in-place', other).stdout


def test_the_photo_size_comes_from_the_backup_s_timing_record(tmp_path):
    backup = sealed_backup(tmp_path)
    assert checks(f"Get-SetupBackupMediaBytes {literal(backup)}").stdout.strip() == str(5 * 1024 ** 3)


def test_the_password_rules():
    result = checks("Test-SetupPassword 'short' 'short'; Test-SetupPassword 'abcdefghijkl' 'abcdefghijkL'; "
                    "'[' + (Test-SetupPassword 'abcdefghijkl' 'abcdefghijkl') + ']'")
    lines = result.stdout.strip().splitlines()
    assert 'at least 12' in lines[0] and 'not the same' in lines[1] and lines[2] == '[]'


def test_the_suggested_layout_is_one_the_installer_accepts():
    result = checks("$l = Find-SetupLayout; $r = Resolve-SetupLayout $l.Instance $l.Database $l.Web $l.People; "
                    "@($r.Names.DatabaseService, $r.Database, $r.Web, $r.People) -join '|'")
    assert result.returncode == 0, result.stderr
    service, *ports = result.stdout.strip().split('|')
    assert service.startswith('FamilyFotoPostgres') and len(set(ports)) == 3


@pytest.mark.parametrize('line, expected', [
    ('Installing: postgres-install', 'core-step postgres-install'),
    ('Already completed: python', 'core-step python'),
    ('Core installation complete. Remote/LAN access remains unconfigured.', 'core-done '),
    ('@@ {"stage": "restored", "state": "running"}', 'stage restored running'),
    ('@@ {"phase": "copy", "files": 3, "total_files": 10, "bytes": 30, "total_bytes": 100}', 'count 100'),
    ('@@ not json', 'text '),
    ('Restore stopped: something', 'text '),
])
def test_progress_lines_are_understood(line, expected):
    result = checks("$p = ConvertFrom-SetupProgressLine " + "'" + line.replace("'", "''") + "'; "
                    "$detail = switch ($p.Kind) { 'core-step' { $p.Step } 'stage' { \"$($p.Stage) $($p.State)\" } "
                    "'count' { $p.Event.total_bytes } default { '' } }; \"$($p.Kind) $detail\"")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected.strip()


# The runner.

def test_the_runner_passes_the_password_on_standard_input_and_reads_all_output(tmp_path):
    child = tmp_path / 'child.py'
    child.write_text(
        'import sys, os\n'
        'line = sys.stdin.readline().rstrip("\\n")\n'
        'print("got", len(line), "characters", flush=True)\n'
        'print("@@ {\\"stage\\": \\"restored\\", \\"state\\": \\"done\\"}", flush=True)\n'
        'print("args", sys.argv[1:], flush=True)\n'
        'print("encoding", os.environ.get("PYTHONIOENCODING"), flush=True)\n'
        'print("名前 Bibliothèque", flush=True)\n'
        'print("a problem", file=sys.stderr, flush=True)\n'
        'sys.exit(3)\n', encoding='utf-8')
    log = tmp_path / 'run.log'
    code = (f"Add-Type -Path {literal(WINDOWS / 'setup-run.cs')}; $r = New-Object FamilyFotoSetupRun; "
            f"$r.Start({literal(sys.executable)}, [string[]]@({literal(child)}, 'D:\\with space\\', 'plain'), "
            f"{literal(tmp_path)}, 'secret password!', {literal(log)}); "
            "while (-not $r.Finished) { Start-Sleep -Milliseconds 100 }; Start-Sleep -Milliseconds 200; $r.Close(); "
            "$lines = @(); $line = $null; while ($r.Lines.TryDequeue([ref]$line)) { $lines += $line }; "
            "'exit=' + $r.ExitCode; $lines")
    result = powershell(code)
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert 'exit=3' in out and 'got 16 characters' in out
    assert "args ['D:\\\\with space\\\\', 'plain']" in out  # A trailing backslash survives the quoting.
    assert 'encoding utf-8' in out and '! a problem' in out
    text = log.read_text(encoding='utf-8')
    assert '名前 Bibliothèque' in text and 'secret password' not in text and 'secret password' not in out


# The restore's progress lines.

def test_the_restore_uses_the_engine_s_progress_lines():
    """One printer, in the engine, for both platforms' windows (app.migration.progress_lines)."""
    source = (WINDOWS / 'restore.py').read_text()
    assert 'ProgressLines() if args.progress_lines else Progress()' in source
    assert "'@@ '" not in source  # The format lives in the engine only.


def test_the_installation_s_own_disk_is_checked(tmp_path):
    result = checks(f"'[' + (Test-SetupInstallationDisk {literal(tmp_path)}) + ']'")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == '[]'  # This test disk is NTFS with room.


def test_a_restore_asks_no_new_account_password():
    """A restored library brings its own accounts: the empty installation gets a random one. The one
    password a restore asks for is the old administrator password that opens the backup."""
    script = (WINDOWS / 'install.ps1').read_text()
    configuration = script[script.index("'configuration' {"):script.index("'postgres-download' {")]
    assert 'RandomNumberGenerator' in configuration and "'--password-stdin'" in configuration
    assert configuration.index('if ($restoring)') < configuration.index('RandomNumberGenerator')
    dialog = (WINDOWS / 'setup-dialog.ps1').read_text()
    assert "StartFramed($shellPath, [string[]]$state.Arguments, $Root, [FamilyFotoSetupRun]::Frame($backupPassword.Text)" in dialog
    assert "Start($shellPath, [string[]]$state.Arguments, $Root, $password.Text" in dialog
    assert "'-Mode', 'Install', '-Start', '-PasswordFromStdin'" in dialog


def test_the_installer_hands_the_dialog_s_options_on():
    script = (WINDOWS / 'install.ps1').read_text()
    assert "if ($ProgressLines) { $restoreArgs += '--progress-lines' }" in script
    assert "'setup-dialog.ps1'" in script and '-not $NoDialog' in script and '-not $existingJournal' in script
    dialog = (WINDOWS / 'setup-dialog.ps1').read_text()
    for option in ('-PasswordFromStdin', '-ProgressLines', '-NoDialog', '-OldLibraryStopped', '-BackupDirectory',
                   '-BackupPasswordFromStdin', '-PasswordCheckRuntime', '-MediaFolder', '-AllowUnprotectedMedia', '-WithoutPeople', '-ResearchOnly'):
        assert f"'{option}'" in dialog, option
    assert '$password.Text' in dialog and "'-Password'" not in dialog  # Only ever on standard input.
    assert 'RecoveryKeyFile' not in dialog and 'keyBox' not in dialog


# The pages.

@pytest.mark.skipif(os.name != 'nt', reason='Windows Forms')
def test_every_page_draws(tmp_path):
    result = powershell('& ' + literal(WINDOWS / 'setup-dialog.ps1') + f' -Root {literal(ROOT)} -Restore -RenderTo {literal(tmp_path)}')
    assert result.returncode == 0, result.stderr
    pages = sorted(path.name for path in tmp_path.glob('*.png'))
    assert pages == ['page1-questions-lower.png', 'page1-questions.png', 'page2-progress.png', 'page3-done.png',
                     'page3-stopped.png']
    assert all(path.stat().st_size > 10_000 for path in tmp_path.glob('*.png'))  # Drawn, not blank.
