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


@pytest.mark.parametrize('commit', ['', 'main', 'a' * 39, 'A' * 40, "';exit;"])
def test_reject_unpinned_commit(commit):
    result = subprocess.run(['powershell.exe', '-NoProfile', '-Command',
        f". {quote(SCRIPT)}; Assert-RestoreCommit {quote(commit)}"], capture_output=True, text=True)
    assert result.returncode != 0
    assert 'no verified code version' in result.stderr


def test_fetch_exact_commit_not_branch_tip(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    def git(*args):
        return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()
    git('init', '-q')
    (repo / 'version.txt').write_text('first')
    git('add', '.')
    git('-c', 'user.name=test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'first')
    commit = git('rev-parse', 'HEAD')
    (repo / 'version.txt').write_text('second')
    git('add', '.')
    git('-c', 'user.name=test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'second')
    target = tmp_path / 'download'
    rewrite = f'url.{repo.as_uri()}.insteadOf=https://github.com/joeandblack/FamilyFotoPub.git'
    command = (f". {quote(SCRIPT)}; Get-RestoreSource git @('-c',{quote(rewrite)}) "
               f"{quote(target)} {quote(commit)}")
    result = subprocess.run(['powershell.exe', '-NoProfile', '-Command', command], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert (target / 'version.txt').read_text() == 'first'
    assert subprocess.check_output(['git', '-C', str(target), 'rev-parse', 'HEAD'], text=True).strip() == commit


@pytest.mark.parametrize('failure', ['init', 'fetch', 'checkout', 'rev-parse', 'mismatch'])
def test_download_errors_never_fall_back(tmp_path, failure):
    command = (f". {quote(SCRIPT)}; function FakeGit {{ "
               f"if ($args -contains '{failure}') {{$global:LASTEXITCODE=1}} "
               "else {$global:LASTEXITCODE=0}; "
               "if ($args -contains 'rev-parse') {Write-Output ('b'*40)} }; "
               f"Get-RestoreSource FakeGit @() {quote(tmp_path / 'unused')} {quote('a'*40)}")
    result = subprocess.run(['powershell.exe', '-NoProfile', '-Command', command], capture_output=True, text=True)
    assert result.returncode != 0
    assert not (tmp_path / 'unused').exists()


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
