"""Release checks never execute installers, database upgrades, or live services."""
import json
import ast
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import zipfile

import pytest

from app.release import metadata, display_version, schema_compatibility
from installer.release import build, selected_files, validate, verify

ROOT = Path(__file__).resolve().parents[1]


def test_schema_compatibility():
    target = metadata()['core_schema_version']
    assert schema_compatibility([]) == 'upgrade-required'
    assert schema_compatibility(range(1, target)) == 'upgrade-required'
    assert schema_compatibility(range(1, target + 1)) == 'compatible'
    assert schema_compatibility(range(1, target + 2)) == 'newer-database-refused'
    with pytest.raises(ValueError, match='gaps'):
        schema_compatibility([1, 3])
    with pytest.raises(ValueError):
        schema_compatibility([True])


def test_legacy_api_version_without_project_metadata(tmp_path):
    assert display_version(tmp_path) == 'unknown'
    with pytest.raises(FileNotFoundError):
        metadata(tmp_path)


def test_manifest_excludes_private_and_test_files():
    tracked = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().strip('\0').split('\0')
    names = selected_files(ROOT, tracked + ['config/local.json', 'data/private.jpg',
                                          'temp/private.py', '.env', 'face_detect/tests/private.py'])
    assert not any(name.startswith(('data/', 'temp/', 'face_detect/tests/', 'face_detect/testing/')) for name in names)
    assert 'config/local.json' not in names
    assert 'installer/macos/install.sh' in names
    assert 'web/lucide-LICENSE.txt' in names
    # Every backup copies these from the installation (app/backup/handover.py), so a release
    # installation without them could not finish a backup.
    from app.backup import handover
    for name in handover.FILES:
        assert f'installer/macos/handover/{name}' in names
    for script in ('upgrade_auth', 'rebuild_missing_thumbnails', 'launch_readonly_preview',
                   'backup_validation_snapshot'):
        assert f'scripts/{script}.py' in names
    assert not any(name.startswith('scripts/archive/') for name in names)
    validate(ROOT, names)


def test_test_dependencies_are_not_runtime():
    config = tomllib.loads((ROOT / 'pyproject.toml').read_text())
    assert not any(line.startswith('pytest') for line in config['project']['dependencies'])
    assert config['project']['optional-dependencies']['test'] == ['pytest==8.3.5']
    assert config['project']['optional-dependencies']['lint'] == ['ruff==0.8.6']
    assert (ROOT / 'requirements-dev.txt').read_text().splitlines() == ['-r requirements.txt', 'pytest==8.3.5', 'ruff==0.8.6']


def test_release_contains_fastbackup_runtime_without_tests(tmp_path):
    archive = build(ROOT, tmp_path / 'release', allow_dirty=True)
    tracked = subprocess.check_output(['git', 'ls-files', '-z', 'fastbackup'], cwd=ROOT).decode().split('\0')
    runtime = {name for name in tracked if name.endswith('.py') and '/tests/' not in name}
    assert 'fastbackup/__main__.py' in runtime
    assert 'fastbackup/capture.py' in runtime
    assert 'fastbackup/copy_worker.py' in runtime
    with zipfile.ZipFile(archive) as package:
        names = set(package.namelist())
        assert {'FamilyFoto/' + name for name in runtime} <= names
        assert not any(name.startswith('FamilyFoto/fastbackup/tests/') for name in names)
        assert not any(name.startswith('FamilyFoto/fastbackup/') and not name.endswith('.py') for name in names)
        manifest = json.loads(package.read('FamilyFoto/release-manifest.json'))
        assert runtime <= manifest['files'].keys()


def test_builder_refuses_dirty_checkout_without_override(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, 'check_output',
                        lambda command: b' M app/api.py' if 'status' in command else b'app/api.py\0')
    with pytest.raises(ValueError, match='Commit tracked changes'):
        build(ROOT, tmp_path / 'release')
    assert not (tmp_path / 'release').exists()


def test_builder_rejects_missing_required_file():
    with pytest.raises(ValueError, match='Missing required release files'):
        selected_files(ROOT, [])


def test_builder_rejects_path_traversal():
    with pytest.raises(ValueError, match='Unsafe tracked filename'):
        selected_files(ROOT, ['../outside.py'])


def test_metadata_must_match_schema_and_requirements(tmp_path):
    project = (ROOT / 'pyproject.toml').read_text()
    (tmp_path / 'pyproject.toml').write_text(project)
    (tmp_path / 'requirements.txt').write_text('wrong==1')
    with pytest.raises(ValueError, match='Runtime dependencies differ'):
        validate(tmp_path, [])
    (tmp_path / 'requirements.txt').write_text((ROOT / 'requirements.txt').read_text())
    with pytest.raises(ValueError, match='schema version'):
        validate(tmp_path, ['databases/001_initial.sql'])
    with pytest.raises(ValueError, match='Invalid schema resource filename'):
        validate(tmp_path, ['databases/broken.sql'])


def test_failed_build_is_retryable_and_existing_checksum_is_preserved(monkeypatch, tmp_path):
    original = zipfile.ZipFile.writestr
    def fail(*args, **kwargs):
        raise OSError('simulated failed write')
    with monkeypatch.context() as patch:
        patch.setattr(zipfile.ZipFile, 'writestr', fail)
        with pytest.raises(OSError, match='simulated failed write'):
            build(ROOT, tmp_path, allow_dirty=True)
    assert not list(tmp_path.iterdir())
    archive = build(ROOT, tmp_path, allow_dirty=True)
    assert zipfile.ZipFile.writestr is original
    checksum = archive.with_suffix('.zip.sha256')
    content = checksum.read_bytes()
    archive.unlink()
    with pytest.raises(FileExistsError):
        build(ROOT, tmp_path, allow_dirty=True)
    assert checksum.read_bytes() == content
    assert not archive.exists()


def test_literal_python_subprocess_entrypoints_are_in_inventory():
    document = tomllib.loads((ROOT / 'pyproject.toml').read_text())
    inventory = set(document['tool']['familyfoto']['entrypoints']['modules'])
    for directory in ('app', 'face_detect', 'installer'):
        for path in (ROOT / directory).rglob('*.py'):
            if 'tests' in path.parts or 'testing' in path.parts:
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8-sig'))):
                if not isinstance(node, (ast.List, ast.Tuple)):
                    continue
                for index, item in enumerate(node.elts[:-1]):
                    following = node.elts[index + 1]
                    if (isinstance(item, ast.Constant) and item.value == '-m'
                            and isinstance(following, ast.Constant)
                            and isinstance(following.value, str)
                            and following.value.startswith(('app.', 'face_detect.'))):
                        assert following.value in inventory, (path, following.value)


def test_extracted_release_outside_checkout():
    # Use OS temp rather than pytest's repository-local basetemp. A caller can set
    # FAMILYFOTO_PACKAGE_TEST_ROOT to another disposable drive for relocation tests.
    base = os.environ.get('FAMILYFOTO_PACKAGE_TEST_ROOT')
    with tempfile.TemporaryDirectory(prefix='FamilyFoto package test ', dir=base) as temporary:
        outer = Path(temporary).resolve()
        assert not outer.is_relative_to(ROOT)
        archive = build(ROOT, outer / 'artifacts', allow_dirty=True)
        with pytest.raises(FileExistsError):
            build(ROOT, outer / 'artifacts', allow_dirty=True)
        destination = outer / 'Different name with spaces'
        with zipfile.ZipFile(archive) as package:
            package.extractall(destination)
        installed = destination / 'FamilyFoto'
        manifest = verify(installed)
        assert manifest['application_version'] == metadata()['application_version']
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(('FAMILYFOTO_', 'PYTHON'))}
        result = subprocess.run([sys.executable, '-I', '-B', str(installed / 'installer/smoke_release.py')],
                                cwd=outer, env=env, text=True, capture_output=True, timeout=90)
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads(result.stdout)['ok'] is True
        # Exercise function-local imports through real decoder entry points,
        # using synthetic bytes only, never catalog input.
        from PIL import Image
        sample = io.BytesIO()
        Image.new('RGB', (32, 24), (100, 120, 150)).save(sample, format='JPEG')
        for module in ('app.photos.recovery', 'app.photos.thumbnail_recovery'):
            child = subprocess.run([sys.executable, '-B', '-m', module], cwd=installed,
                                   env=env, input=sample.getvalue(), capture_output=True, timeout=30)
            assert child.returncode == 0, child.stderr.decode(errors='replace')
            assert isinstance(json.loads(child.stdout), dict)
        # Detect asset corruption rather than accepting an import-only success.
        (installed / 'web/app.js').write_text('corrupted')
        with pytest.raises(ValueError, match='Release file differs'):
            verify(installed)
