"""Isolated Python provisioning. Never run the Core installer or change a service.

The optional real-package test uses FAMILYFOTO_TEST_PYTHON_PACKAGE (a downloaded
official archive); normal tests need no download and never execute fixture code.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile

import pytest


pytestmark = pytest.mark.skipif(sys.platform != 'win32', reason='Windows runtime')
ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'installer/windows/install-python.ps1'


def literal(path):
    return "'" + str(path).replace("'", "''") + "'"


def run(body):
    return subprocess.run(['powershell.exe', '-NoProfile', '-Command',
                           f". {literal(SCRIPT)}; {body}"], capture_output=True, text=True, timeout=180)


def install(root, before=''):
    return run(f'{before}; Install-PrivatePythonRuntime {literal(root)}')


def test_existing_runtime_is_never_overwritten(tmp_path):
    target = tmp_path / 'runtime/python'
    target.mkdir(parents=True)
    sentinel = target / 'keep.txt'
    sentinel.write_text('existing')
    result = install(tmp_path)
    assert result.returncode != 0
    assert 'already exists' in result.stderr
    assert sentinel.read_text() == 'existing'
    assert not (tmp_path / 'temp').exists()


def test_corrupt_cached_package_refused_before_execution(tmp_path):
    cache = tmp_path / 'temp'
    cache.mkdir()
    (cache / 'python.3.12.10.nupkg').write_bytes(b'not the pinned package')
    result = install(tmp_path)
    assert result.returncode != 0
    assert 'checksum' in result.stderr
    assert not (tmp_path / 'runtime/python').exists()


@pytest.mark.parametrize('status,publisher', [('NotSigned', ''), ('Valid', 'O=Untrusted,')])
def test_publisher_failure_refuses_execution(tmp_path, status, publisher):
    result = run("function Get-AuthenticodeSignature { [pscustomobject]@{Status='" + status +
                 "'; SignerCertificate=[pscustomobject]@{Subject='" + publisher + "'}} }; "
                 f"Assert-PrivatePythonRuntime {literal(tmp_path)}")
    assert result.returncode != 0
    assert 'publisher verification failed' in result.stderr


def fixture_package(path):
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('tools/python.exe', 'fixture only; never execute')


@pytest.mark.parametrize('failure', ['probe', 'collision'])
def test_failed_verification_or_publication_preserves_target(tmp_path, failure):
    archive = tmp_path / 'fixture.zip'
    fixture_package(archive)
    target = tmp_path / 'runtime/python'
    validate = "throw 'probe failed'" if failure == 'probe' else (
        f"New-Item -ItemType Directory -Path {literal(target)} | Out-Null; "
        f"[IO.File]::WriteAllText({literal(target / 'keep.txt')}, 'other installer')")
    mocks = ("function Get-SetupDownload($Url,$Path,$Sha256) { "
             f"Copy-Item -LiteralPath {literal(archive)} -Destination $Path }}; "
             f"function Assert-PrivatePythonRuntime {{ {validate} }}")
    result = install(tmp_path, mocks)
    assert result.returncode != 0
    assert not list((tmp_path / 'runtime').glob('.python-stage-*'))
    if failure == 'collision':
        assert (target / 'keep.txt').read_text() == 'other installer'
        assert not (target / 'python.exe').exists()
    else:
        assert 'probe failed' in result.stderr
        assert not target.exists()
    # Retry after a pre-publication failure uses a fresh private staging folder.
    if failure == 'probe':
        result = install(tmp_path, mocks.replace("throw 'probe failed'", '$null'))
        assert result.returncode == 0, result.stderr
        assert (target / 'python.exe').read_text() == 'fixture only; never execute'


def python_registrations():
    import winreg

    def tree(key):
        children, values, _ = winreg.QueryInfoKey(key)
        return {'values': [winreg.EnumValue(key, i) for i in range(values)],
                'children': {name: child(key, name) for name in
                             (winreg.EnumKey(key, i) for i in range(children))}}

    def child(parent, path):
        try:
            with winreg.OpenKey(parent, path) as key:
                return tree(key)
        except FileNotFoundError:
            return None

    result = {name: child(hive, 'Software\\Python') for name, hive in
              [('user', winreg.HKEY_CURRENT_USER), ('machine', winreg.HKEY_LOCAL_MACHINE)]}
    # Read-only integrity check for any registered Python 3.12 executable/DLL.
    files = {}
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, r'Software\Python\PythonCore\3.12\InstallPath') as key:
                folder = Path(winreg.QueryValueEx(key, '')[0])
        except FileNotFoundError:
            continue
        for name in ('python.exe', 'python312.dll'):
            file = folder / name
            if file.is_file():
                files[str(file)] = hashlib.sha256(file.read_bytes()).hexdigest()
    return result, files


@pytest.fixture
def short_runtime_root():
    # PowerShell 5.1/.NET Framework extraction is subject to MAX_PATH; the
    # standard test-run prefix alone consumes most of that allowance.
    (ROOT / 'temp').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='py-', dir=ROOT / 'temp') as folder:
        yield Path(folder)


@pytest.mark.skipif(not os.environ.get('FAMILYFOTO_TEST_PYTHON_PACKAGE'), reason='Opt-in real runtime fixture')
def test_two_independent_runtimes_venvs_and_pip_leave_registered_python_unchanged(short_runtime_root):
    archive = Path(os.environ['FAMILYFOTO_TEST_PYTHON_PACKAGE'])
    before = python_registrations()
    runtimes = []
    for name in ('one', 'two'):
        root = short_runtime_root / name
        (root / 'temp').mkdir(parents=True)
        shutil.copyfile(archive, root / 'temp/python.3.12.10.nupkg')
        result = install(root)
        assert result.returncode == 0, result.stderr
        runtime = root / 'runtime/python'
        python = runtime / 'python.exe'
        runtimes.append(python)
        # Core and the optional People environment both require normal venv/pip.
        for env_name in ('.venv', 'people-venv'):
            env = root / env_name
            subprocess.run([str(python), '-I', '-m', 'venv', str(env)], check=True, capture_output=True)
            env_python = env / 'Scripts/python.exe'
            subprocess.run([str(env_python), '-I', '-m', 'pip', 'check'], check=True, capture_output=True)
            result = subprocess.run([str(env_python), '-I', '-c',
                                     'import json,sys,ssl,sqlite3,ctypes; print(json.dumps([sys.prefix, sys.base_prefix]))'],
                                    check=True, capture_output=True, text=True)
            prefix, base = map(Path, json.loads(result.stdout))
            assert prefix.resolve() == env.resolve()
            assert base.resolve() == runtime.resolve()
        assert not list((root / 'runtime').glob('.python-stage-*'))
    # The first runtime remains independently runnable after provisioning the second.
    subprocess.run([str(runtimes[0]), '-I', '-c', 'import ssl,sqlite3,venv,ensurepip'], check=True)
    assert python_registrations() == before
