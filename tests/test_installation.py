"""Installer tests use fake DBs and temporary paths; never install native services."""
import base64
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('verify_installation', ROOT / 'installer/verify-installation.py')
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)


@pytest.mark.parametrize('password,valid', [('test-only-password-123', True),
                                         ('Gr\u00fc\u00dfe-\u6771\u4eac-\U0001f511-password', True),
                                         ('short', False), ('', False)])
def test_unattended_password_is_hashed_not_logged(monkeypatch, tmp_path, capsys, password, valid):
    from installer import configure
    monkeypatch.setattr(configure, 'ROOT', tmp_path)
    monkeypatch.setattr(sys, 'argv', ['configure.py', '--password-stdin'])
    # Redirected Windows stdin can use a legacy code page even when the sender
    # deliberately writes UTF-8. The installer must decode the bytes itself.
    monkeypatch.setattr(sys, 'stdin', io.TextIOWrapper(io.BytesIO((password + '\n').encode('utf-8')), encoding='cp1252'))
    if valid:
        configure.main()
        config = json.loads((tmp_path / 'config/local.json').read_text())
        assert config['browser_password_hash']
        from app.auth import verify_password
        assert verify_password({'password_hash': config['browser_password_hash']}, password)
        assert password not in (tmp_path / 'config/local.json').read_text()
        assert password not in capsys.readouterr().out
        assert config['source'] == config['destination'] == {}
    else:
        with pytest.raises(SystemExit, match='Passwords'):
            configure.main()
        assert not (tmp_path / 'config/local.json').exists()


def test_unattended_password_refuses_terminal(monkeypatch, tmp_path):
    from installer import configure
    monkeypatch.setattr(configure, 'ROOT', tmp_path)
    monkeypatch.setattr(sys, 'argv', ['configure.py', '--password-stdin'])
    stream = io.StringIO('not-for-interactive-use')
    monkeypatch.setattr(stream, 'isatty', lambda: True)
    monkeypatch.setattr(sys, 'stdin', stream)
    with pytest.raises(SystemExit, match='redirected'):
        configure.main()


def test_private_python_does_not_register_shell_integration():
    script = (ROOT / 'installer/windows/install-python.ps1').read_text()
    assert 'api.nuget.org/v3-flatcontainer/python/3.12.10/python.3.12.10.nupkg' in script
    assert 'Start-Process' not in script
    assert 'python-3.12.10-amd64.exe' not in script
    assert '[IO.Directory]::Move($tools, $target)' in script
    assert 'Get-SetupDownload' in script and 'Get-AuthenticodeSignature' in script


@pytest.mark.parametrize('automated', [False, True])
def test_sandbox_package_excludes_private_files(tmp_path, automated):
    from installer.release import selected_files, verify as verify_release
    tracked = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().strip('\0').split('\0')
    # Exercise the real shared builder in a disposable clean Git checkout. The
    # previous four-file fixture no longer represents a supported release input.
    for relative in selected_files(ROOT, tracked):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    subprocess.run(['git', 'init', '--quiet', str(tmp_path)], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(tmp_path), 'add', '.'], check=True, capture_output=True)
    hooks = tmp_path / '.git' / 'empty-hooks'
    hooks.mkdir()
    subprocess.run(['git', '-C', str(tmp_path), '-c', 'user.name=Fixture',
                    '-c', 'user.email=fixture@example.invalid', '-c', 'commit.gpgsign=false',
                    '-c', f'core.hooksPath={hooks}', 'commit', '--quiet', '-m', 'Disposable release fixture'],
                   check=True, capture_output=True)
    # Private/untracked files must stay out without disabling the clean-tree gate.
    (tmp_path / 'config/local.json').write_text('private-credentials')
    (tmp_path / 'data').mkdir()
    (tmp_path / 'data/catalog.db').write_text('private-data')
    result = powershell('& ' + literal(tmp_path / 'installer/windows/prepare-sandbox-test.ps1')
                        + ' -Python ' + literal(sys.executable) + (' -RunCoreTest' if automated else ''))
    assert result.returncode == 0, result.stderr
    config_path, = (tmp_path / 'temp/installer-sandbox').glob('*/FamilyFoto-Core.wsb')
    package = config_path.parent / 'input/FamilyFoto'
    assert not (package / 'config/local.json').exists()
    assert not (package / 'data').exists()
    assert (package / 'installer/windows/prepare-sandbox-test.ps1').exists(), (
        sum(1 for _ in package.rglob('*')), result.stdout[-2000:], result.stderr[-2000:])
    assert (package / 'face_detect/api/service.py').exists()
    assert (package / 'face_detect/web/automatic-people-preview.mjs').exists()
    assert (package / 'installer/native_people.py').read_bytes() == (ROOT / 'installer/native_people.py').read_bytes()
    # Inspect the same extended-length destination used by the extractor, even
    # on hosts where the system-wide long-path policy is disabled.
    extended_package = Path('\\\\?\\' + str(package.resolve()))
    assert verify_release(extended_package)['dirty'] is False
    config = ET.parse(config_path).getroot()
    mappings = config.findall('MappedFolders/MappedFolder')
    assert len(mappings) == (2 if automated else 1)
    assert mappings[0].findtext('ReadOnly') == 'true'
    if automated:
        assert Path(mappings[1].findtext('HostFolder')) == config_path.parent / 'results'
        assert mappings[1].findtext('ReadOnly') == 'false'
        assert 'test-sandbox-core.ps1' in config.findtext('LogonCommand/Command')
    else:
        assert config.find('LogonCommand') is None


class FakeDB:
    def __init__(self, *, populated=False, versions=(1,), elevated=False):
        self.populated = populated
        self.versions = versions
        self.elevated = elevated
        self.query = ''
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, query):
        assert query.startswith(('SELECT', 'SET TRANSACTION READ ONLY'))
        self.queries.append(query)
        self.query = query
        return self

    def fetchone(self):
        if 'current_setting' in self.query:
            return ('familyfoto', 'familyfoto', 170011, '127.0.0.1')
        if 'rolsuper' in self.query:
            return (self.elevated, False, False)
        return (self.populated,)

    def fetchall(self):
        return [(v,) for v in self.versions]


@pytest.fixture
def install_config(tmp_path):
    (tmp_path / 'databases').mkdir()
    (tmp_path / 'databases/001_initial.sql').write_text('not executed')
    return {'database': {'host': '127.0.0.1', 'dbname': 'familyfoto', 'user': 'familyfoto'}}


@pytest.mark.parametrize('before_schema', [False, True])
def test_verifier_reads_only(monkeypatch, tmp_path, install_config, before_schema):
    db = FakeDB()
    monkeypatch.setattr('psycopg.connect', lambda **kw: db)
    verify.database_checks(install_config, tmp_path, before_schema=before_schema)
    assert db.queries[0] == 'SET TRANSACTION READ ONLY'
    assert any('schema_versions' in q for q in db.queries) != before_schema


@pytest.mark.parametrize('options,reason', [
    ({'populated': True}, 'already contains data'),
    ({'versions': (1, 2)}, 'schema'),
    ({'elevated': True}, 'privileges'),
])
def test_verifier_rejects_unsafe_fresh_state(monkeypatch, tmp_path, install_config, options, reason):
    monkeypatch.setattr('psycopg.connect', lambda **kw: FakeDB(**options))
    with pytest.raises(ValueError, match=reason):
        verify.database_checks(install_config, tmp_path, require_empty=True)


def test_verifier_does_not_echo_connection_secrets(monkeypatch, capsys):
    monkeypatch.setattr(sys, 'argv', ['verify-installation.py'])
    def fail(*args, **kwargs):
        raise RuntimeError('password=must-not-be-printed')
    monkeypatch.setattr(verify, 'verify', fail)
    assert verify.main() == 1
    assert 'must-not-be-printed' not in capsys.readouterr().err


def powershell(code):
    if os.name != 'nt' or not shutil.which('powershell.exe'):
        pytest.skip('Windows PowerShell helper test')
    encoded = base64.b64encode(code.encode('utf-16le')).decode()
    return subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                           '-EncodedCommand', encoded], capture_output=True, text=True, errors='replace', timeout=30)


def literal(path):
    return "'" + str(path).replace("'", "''") + "'"


def helpers(code):
    return powershell("$ErrorActionPreference='Stop'; . " + literal(ROOT / 'installer/windows/setup-common.ps1') + '; ' + code)


def skeleton_installer(tmp_path):
    """The installer's own scripts and the names they share with the runtime scripts, nothing else."""
    scripts = tmp_path / 'installer/windows'
    scripts.mkdir(parents=True)
    for name in ('install.ps1', 'setup-common.ps1'):
        shutil.copyfile(ROOT / 'installer/windows' / name, scripts / name)
    (tmp_path / 'scripts').mkdir()
    shutil.copyfile(ROOT / 'scripts/instance-names.ps1', tmp_path / 'scripts/instance-names.ps1')
    return scripts


def test_plan_has_no_side_effects(tmp_path):
    # Preview succeeds even in a skeleton directory with no Python/config/DB.
    scripts = skeleton_installer(tmp_path)
    before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob('*'))
    result = powershell('& ' + literal(scripts / 'install.ps1') + ' -Mode Plan')
    assert result.returncode == 0, result.stderr
    assert 'postgres-install' in result.stdout and 'People/Docker' in result.stdout
    assert f'FamilyFoto Core setup plan: {tmp_path}' in result.stdout
    assert sorted(p.relative_to(tmp_path) for p in tmp_path.rglob('*')) == before


def test_windows_scripts_parse_without_execution():
    result = powershell(
        "$ErrorActionPreference='Stop'; Get-ChildItem -LiteralPath "
        + literal(ROOT / 'installer/windows')
        + " -Filter '*.ps1' | ForEach-Object { $tokens=$null; $errors=$null; "
        "[void][System.Management.Automation.Language.Parser]::ParseFile($_.FullName,"
        "[ref]$tokens,[ref]$errors); if($errors.Count){throw ($errors | Out-String)} }")
    assert result.returncode == 0, result.stderr


def test_relocated_fingerprint_resolves_shared_and_windows_files():
    result = helpers('Get-SetupFingerprint ' + literal(ROOT))
    assert result.returncode == 0, result.stderr
    assert len(result.stdout.strip()) == 64


def test_verify_routes_to_shared_helper():
    script = (ROOT / 'installer/windows/install.ps1').read_text()
    assert "Join-Path $root 'installer/verify-installation.py'" in script
    assert "Join-Path $PSScriptRoot 'verify-installation.py'" not in script


def test_people_requires_terms_before_core_changes():
    result = powershell('& ' + literal(ROOT / 'installer/windows/install.ps1')
                        + ' -Mode Install -WithPeople')
    assert result.returncode != 0
    assert 'model terms approval' in result.stderr


@pytest.mark.parametrize('change', ["$j.owner='other'", "$j.root='C:\\other'", "$j.fingerprint='changed'",
                                    "$j.complete=$true", "$j.completed=@('schema')"])
def test_resume_rejects_foreign_or_invalid_journal(change):
    code = "$j=[pscustomobject]@{version=1;root='C:\\app';owner='user';fingerprint='abc';complete=$false;completed=@('python')}; "
    code += change + "; Assert-SetupJournal $j 'C:\\app' 'user' 'abc'"
    assert helpers(code).returncode != 0


def test_journal_roundtrip_and_valid_resume(tmp_path):
    path = literal(tmp_path / 'install.json')
    result = helpers("$j=[pscustomobject]@{version=1;root='C:\\app';owner='user';fingerprint='abc';complete=$false;completed=@('python')}; "
                     f"Save-SetupJournal {path} $j; $j.completed=@('python','dependencies'); Save-SetupJournal {path} $j; "
                     f"$read=Get-Content -Raw -LiteralPath {path}|ConvertFrom-Json; Assert-SetupJournal $read 'C:\\app' 'user' 'abc'")
    assert result.returncode == 0, result.stderr
    assert json.loads((tmp_path / 'install.json').read_text())['completed'] == ['python', 'dependencies']


def test_native_failure_stops_execution():
    result = helpers(f"Invoke-SetupCommand {literal(sys.executable)} @('-c','exit(7)'); Write-Output 'UNSAFE-NEXT-STEP'")
    assert result.returncode != 0
    assert 'UNSAFE-NEXT-STEP' not in result.stdout


def test_cached_download_hash_checked_without_network(tmp_path):
    import hashlib
    artifact = tmp_path / 'installer.exe'
    artifact.write_bytes(b'fixture-not-executable')
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    good = helpers(f"Get-SetupDownload 'https://invalid.example/never-used' {literal(artifact)} '{digest}'")
    assert good.returncode == 0, good.stderr
    bad = helpers(f"Get-SetupDownload 'https://invalid.example/never-used' {literal(artifact)} '{'0' * 64}'")
    assert bad.returncode != 0
    assert artifact.read_bytes() == b'fixture-not-executable'


def test_private_directory_acl(tmp_path):
    target = literal(tmp_path / 'private')
    result = helpers(f"Set-SetupPrivateDirectory {target}; $acl=Get-Acl -LiteralPath {target}; "
                     "if(-not $acl.AreAccessRulesProtected){throw 'ACL still inherited'}; "
                     "if(@($acl.Access).Count -ne 3){throw 'Unexpected access rules'}")
    assert result.returncode == 0, result.stderr


# The People question: no silent default.

def people_choice(args, answers=()):
    """Resolve-SetupPeopleChoice with a fake person answering in turn."""
    queue = "@(" + ",".join("'" + a + "'" for a in answers) + ")"
    code = (f"$script:answers=[System.Collections.ArrayList]{queue}; $script:asked=0; "
            "$ask={ param($terms) $script:asked++; if($terms -notmatch 'non-commercial research use only'){throw 'terms not shown'}; "
            "$a=$script:answers[0]; $script:answers.RemoveAt(0); $a }; "
            f"$r=Resolve-SetupPeopleChoice {args} $ask; Write-Output \"$r asked=$script:asked\"")
    return helpers(code)


@pytest.mark.parametrize('args, expected', [
    ('$true $false $true $null $false', 'install asked=0'),
    ('$false $true $false $null $false', 'skip asked=0'),
    ('$false $false $false $null $true', None),
])
def test_a_flag_decides_without_asking(args, expected):
    result = people_choice(args, ['yes'])
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == (expected or 'install asked=1')


@pytest.mark.parametrize('answers, expected', [(['yes'], 'install asked=1'), (['No'], 'skip asked=1'),
                                               (['maybe', 'y'], 'install asked=2')])
def test_the_person_is_asked_after_seeing_the_terms(answers, expected):
    result = people_choice('$false $false $false $null $true', answers)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


def test_three_unclear_answers_stop_the_install():
    result = people_choice('$false $false $false $null $true', ['', 'later', '?'])
    assert result.returncode != 0 and 'answer yes or no' in result.stderr


def test_an_unattended_install_must_choose():
    result = people_choice('$false $false $false $null $false')
    assert result.returncode != 0 and '-WithoutPeople' in result.stderr


@pytest.mark.parametrize('args, message', [
    ('$true $true $true $null $true', 'not both'),
    ('$false $false $true $null $true', 'goes with -WithPeople'),
    ('$true $false $false $null $true', 'model terms approval'),
])
def test_contradicting_flags_are_refused(args, message):
    result = people_choice(args)
    assert result.returncode != 0 and message in result.stderr


@pytest.mark.parametrize('args, expected', [
    ("$false $false $false 'install' $true", 'install asked=0'),
    ("$false $true $false 'skip' $false", 'skip asked=0'),
])
def test_a_resumed_install_keeps_its_answer(args, expected):
    result = people_choice(args)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


def test_a_resumed_install_refuses_a_different_answer():
    result = people_choice("$false $true $false 'install' $false")
    assert result.returncode != 0 and 'same choice' in result.stderr


def test_without_people_is_refused_together_with_with_people_before_any_change():
    result = powershell('& ' + literal(ROOT / 'installer/windows/install.ps1')
                        + ' -Mode Install -WithPeople -ResearchOnly -WithoutPeople')
    assert result.returncode != 0 and 'not both' in result.stderr


def test_the_plan_says_the_install_will_ask(tmp_path):
    scripts = skeleton_installer(tmp_path)
    result = powershell('& ' + literal(scripts / 'install.ps1') + ' -Mode Plan')
    assert result.returncode == 0, result.stderr
    assert 'Install asks whether to install People' in result.stdout
