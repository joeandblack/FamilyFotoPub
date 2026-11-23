"""Read-only bootstrap validation; never download or invoke the real installer."""
from pathlib import Path
import subprocess
import sys

import pytest


pytestmark = pytest.mark.skipif(sys.platform != 'win32', reason='Windows PowerShell')
SCRIPT = Path(__file__).resolve().parents[1] / 'installer/windows/handover/restore-familyfoto-windows.ps1'


def quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def check(path, launcher):
    return subprocess.run(
        ['powershell.exe', '-NoProfile', '-Command',
         f". {quote(SCRIPT)}; Assert-FreshRestoreFolder {quote(path)} {quote(launcher)}"],
        capture_output=True, text=True,
    )


def test_empty_and_launcher_only(tmp_path):
    target = tmp_path / 'fresh'
    launcher = target / 'restore-familyfoto-windows.ps1'
    assert check(target, launcher).returncode == 0
    assert not target.exists()
    target.mkdir()
    launcher.write_text('fixture')
    assert check(target, launcher).returncode == 0


def test_refuses_existing_install_and_hidden_files(tmp_path):
    (tmp_path / '.git').mkdir()
    result = check(tmp_path, tmp_path / 'launcher.ps1')
    assert result.returncode != 0
    assert 'not empty' in result.stderr
    assert (tmp_path / '.git').is_dir()


def test_refuses_spaces(tmp_path):
    assert check(tmp_path / 'not supported', tmp_path / 'launcher.ps1').returncode != 0


def test_refuses_file_as_directory(tmp_path):
    target = tmp_path / 'file'
    target.write_text('keep')
    assert check(target, tmp_path / 'launcher.ps1').returncode != 0
    assert target.read_text() == 'keep'


@pytest.mark.parametrize('gh', ['', 'C:/Program Files/GitHub CLI/gh.exe'])
def test_https_credential_arguments(gh):
    import json
    result = subprocess.run(
        ['powershell.exe', '-NoProfile', '-Command',
         f". {quote(SCRIPT)}; ConvertTo-Json -Compress -InputObject @(Get-RestoreGitArguments {quote(gh)})"],
        capture_output=True, text=True, check=True,
    )
    options = json.loads(result.stdout)
    assert options[:4] == ['-c', 'credential.helper=', '-c', 'core.askPass=']
    assert options[4:] == (['-c', f'credential.helper=!"{gh}" auth git-credential'] if gh else [])


def test_download_copies_hidden_readonly_git_files_and_refuses_collision(tmp_path):
    import tempfile
    with tempfile.TemporaryDirectory() as staging:
        source = Path(staging)
        target = tmp_path / 'destination'
        target.mkdir()
        git = source / '.git'
        git.mkdir()
        pack = git / 'fixture.pack'
        pack.write_bytes(b'pack fixture')
        (source / 'README.md').write_text('release')
        command = (f". {quote(SCRIPT)}; "
                   f"(Get-Item -Force {quote(git)}).Attributes = 'Hidden,Directory'; "
                   f"(Get-Item -Force {quote(pack)}).IsReadOnly = $true; "
                   f"Copy-RestoreDownload {quote(source)} {quote(target)}")
        try:
            subprocess.run(['powershell.exe', '-NoProfile', '-Command', command], check=True, capture_output=True)
            assert (target / '.git/fixture.pack').read_bytes() == b'pack fixture'
            assert (target / 'README.md').read_text() == 'release'
            result = subprocess.run(['powershell.exe', '-NoProfile', '-Command',
                                     f". {quote(SCRIPT)}; Copy-RestoreDownload {quote(source)} {quote(target)}"],
                                    capture_output=True, text=True)
            assert result.returncode != 0
            assert 'Destination changed' in result.stderr
        finally:
            pack.chmod(0o600)
            copied = target / '.git/fixture.pack'
            if copied.exists():
                copied.chmod(0o600)
