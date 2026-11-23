"""Mac launcher: engine entry point, setup-window selection and release-side builds.

Fixture payloads only; nothing is installed, no database or service is created.
"""
import hashlib
import io
import json
import os
import platform
import plistlib
import stat
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from installer.common.bundle import build as build_bundle
from installer.common.manifest import Manifest
from installer.macos import bundled, build_launcher, online_setup, setup_bootstrap, shared_adapter
from tests.test_mac_shared_installer import application_zip, program_zip
from tests.test_shared_installer import component, digest

ROOT = Path(__file__).resolve().parents[1]
MAC = sys.platform == 'darwin' and platform.machine() == 'arm64'
SWIFT = MAC and Path('/usr/bin/swiftc').exists()
# The built app refuses to run below the release's minimum_os (27.0), by design.
MACOS_27 = MAC and int((platform.mac_ver()[0] or '0').split('.')[0]) >= 27
TERMS = b'Fixture terms\n'


def offline_release(tmp_path, people=False):
    """A complete offline Mac release: manifest, bundle, engine folder and descriptor."""
    data = {'application': application_zip(), 'core-lock': b'demo==1\n', 'password-lock': b'demo==1\n',
            'wheel': b'fixture wheel bytes'}
    items = [component(key, value) for key, value in data.items()]
    items[1].update(bundle_path='locks/core.txt', lock_role='core')
    items[2].update(bundle_path='locks/password.txt', lock_role='password')
    items[3]['bundle_path'] = 'wheels/demo-1-py3-none-any.whl'
    for role, names in bundled.EXECUTABLES.items():
        value = program_zip(names)
        items.append(component('runtime-' + role, value, runtime_role=role))
        items[-1]['installed_size_estimate'] = 1 << 20
        data['runtime-' + role] = value
    features = {'core': list(data)}
    if people:
        data['people-lock'] = b'demo==1\n'
        items.append(component('people-lock', data['people-lock'], lock_role='people-view'))
        items[-1]['bundle_path'] = 'locks/people-view.txt'
        features['people'] = ['people-lock']
    doc = dict(schema_version=1, installer_version=1, release_id='test-1', source_commit='a' * 40,
               platform='macos-arm64', minimum_os='27.0', agreement_path='legal/agreement.txt',
               agreement_sha256=digest(TERMS), components=items, features=features)
    raw = json.dumps(doc).encode()
    manifest = Manifest(raw, digest(raw))
    cache = tmp_path / 'cache'
    cache.mkdir()
    for item in items:
        (cache / item['sha256']).write_bytes(data[item['id']])
    build_bundle(manifest, tmp_path / 'bundle', cache, TERMS, raw)
    engine = tmp_path / 'engine'
    (engine / 'legal').mkdir(parents=True)
    (engine / 'installer-manifest.json').write_bytes(raw)
    (engine / 'legal/agreement.txt').write_bytes(TERMS)
    descriptor = tmp_path / 'bootstrap.json'
    descriptor.write_text(json.dumps({'schema_version': 1, 'mode': 'offline', 'manifest_sha256': manifest.digest}))
    return manifest, engine, descriptor


def choices(tmp_path, root, people=False, accepted=None, backup=None):
    path = tmp_path / 'choices.json'
    value = {'root': str(root), 'people': people, 'agreement_digest': accepted or digest(TERMS)}
    if backup is not None:
        value['backup'] = str(backup)
    path.write_text(json.dumps(value))
    return path


def run_engine(tmp_path, monkeypatch, root, *extra, people=False, accepted=None, backup=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    manifest, engine, descriptor = offline_release(tmp_path, people=people)
    monkeypatch.setattr(online_setup, 'ENGINE', engine)
    monkeypatch.setattr(shared_adapter, 'require_host', lambda: None)
    handed = {}
    monkeypatch.setattr(shared_adapter, 'handoff', lambda root, mode, receipt, env: handed.update(
        root=root, mode=mode, receipt=receipt, env=env) or 0)
    out = io.StringIO()
    monkeypatch.setattr(sys, 'stdout', out)
    code = online_setup.main(['--descriptor', str(descriptor), '--attempt', str(tmp_path / 'attempt'),
                              '--choices', str(choices(tmp_path, root, people, accepted, backup)),
                              '--bundle', str(tmp_path / 'bundle'), '--os-version', '27.2', *extra])
    return code, handed, out.getvalue(), manifest


def test_engine_prepares_offline_and_hands_over_with_launcher_flag(tmp_path, monkeypatch):
    root = tmp_path / 'FamilyFoto'
    code, handed, output, manifest = run_engine(tmp_path, monkeypatch, root)
    assert code == 0 and handed['mode'] == 'install' and handed['env'] == {'FAMILYFOTO_LAUNCHER': '1'}
    assert Path(handed['receipt']).is_absolute()
    events = [json.loads(line[3:]) for line in output.splitlines() if line.startswith('@@ ')]
    assert events and all(e['schema_version'] == 1 for e in events)
    assert events[-1]['stage'] == 'prepare' and events[-1]['state'] == 'completed'
    assert set(os.listdir(root / 'bundled')) >= set(bundled.EXECUTABLES)
    assert bundled.resume_receipt(root) == Path(handed['receipt'])
    receipt = json.loads(Path(handed['receipt']).read_text())
    assert receipt['features'] == ['core'] and receipt['stage'] == 'materialized'


def backup_folder(tmp_path):
    backup = tmp_path / 'Backup Disk/familyfoto-backup-1'
    (backup / 'catalog').mkdir(parents=True)
    return backup


def test_restore_choice_hands_over_as_a_restore_with_the_backup(tmp_path, monkeypatch):
    backup = backup_folder(tmp_path)
    code, handed, _, _ = run_engine(tmp_path, monkeypatch, tmp_path / 'FamilyFoto', backup=backup)
    assert code == 0 and handed['mode'] == 'restore'
    assert handed['env'] == {'FAMILYFOTO_LAUNCHER': '1', 'FAMILYFOTO_RESTORE_BACKUP': str(backup)}


@pytest.mark.parametrize('where', ['not-a-backup', 'relative', 'inside', 'contains'])
def test_unusable_backup_is_refused_before_downloading(tmp_path, monkeypatch, where):
    backup = backup_folder(tmp_path)
    root = tmp_path / 'FamilyFoto'
    if where == 'not-a-backup':
        backup = backup.parent
    elif where == 'relative':
        backup = Path('Backup Disk/familyfoto-backup-1')
    elif where == 'inside':
        root = backup / 'FamilyFoto'
    else:
        root = backup.parent
    with pytest.raises(ValueError, match='backup folder|outside the backup'):
        run_engine(tmp_path, monkeypatch, root, backup=backup)
    assert not (tmp_path / 'attempt').exists() and not (tmp_path / 'FamilyFoto').exists()


def test_people_choice_selects_the_people_feature(tmp_path, monkeypatch):
    code, handed, _, _ = run_engine(tmp_path, monkeypatch, tmp_path / 'FamilyFoto', people=True)
    assert code == 0 and json.loads(Path(handed['receipt']).read_text())['features'] == ['core', 'people']


def test_prepare_only_stops_before_the_installer(tmp_path, monkeypatch):
    code, handed, output, _ = run_engine(tmp_path, monkeypatch, tmp_path / 'FamilyFoto', '--prepare-only')
    assert code == 0 and handed == {} and 'Downloads verified' in output


def test_unaccepted_agreement_downloads_nothing(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match='acceptance'):
        run_engine(tmp_path, monkeypatch, tmp_path / 'FamilyFoto', accepted='0' * 64)
    assert not (tmp_path / 'attempt/cache').exists()


def test_occupied_folder_is_refused_before_any_download(tmp_path, monkeypatch):
    root = tmp_path / 'Documents'
    root.mkdir()
    (root / 'letter.txt').write_text('not ours')
    with pytest.raises(ValueError, match='already contains files'):
        run_engine(tmp_path, monkeypatch, root)
    assert not (tmp_path / 'attempt').exists() and (root / 'letter.txt').read_text() == 'not ours'


def test_linked_folder_is_refused(tmp_path, monkeypatch):
    if os.name == 'nt':
        pytest.skip('symlink creation needs privileges on Windows')
    real = tmp_path / 'real'
    real.mkdir()
    (tmp_path / 'link').symlink_to(real)
    with pytest.raises(ValueError, match='symbolic links'):
        run_engine(tmp_path, monkeypatch, tmp_path / 'link' / 'FamilyFoto')


@pytest.mark.parametrize('content', [{'root': 'relative/path', 'people': False, 'agreement_digest': 'x'},
                                     {'root': '/abs', 'people': 'yes', 'agreement_digest': 'x'}])
def test_invalid_choices_are_refused(tmp_path, content):
    path = tmp_path / 'choices.json'
    path.write_text(json.dumps(content))
    with pytest.raises(ValueError):
        online_setup.read_choices(path)


def test_setup_window_is_prebuilt_for_release_installs_and_opens_from_the_launcher(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_bootstrap, 'ROOT', tmp_path)
    monkeypatch.delenv('FAMILYFOTO_PAYLOAD_RECEIPT', raising=False)
    monkeypatch.delenv('FAMILYFOTO_LAUNCHER', raising=False)
    window = tmp_path / 'bundled/setup-window/bin/familyfoto-setup'
    window.parent.mkdir(parents=True)
    window.write_text('#!/bin/sh\n')
    window.chmod(0o755)
    assert setup_bootstrap.prebuilt_window() is None  # terminal installs keep compiling with swiftc
    monkeypatch.setenv('FAMILYFOTO_PAYLOAD_RECEIPT', str(tmp_path / 'prepared.json'))
    assert setup_bootstrap.prebuilt_window() == window
    assert setup_bootstrap.setup_window(tmp_path) == window
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: False, raising=False)
    assert not setup_bootstrap.wants_window()
    monkeypatch.setenv('FAMILYFOTO_LAUNCHER', '1')
    assert setup_bootstrap.wants_window()


def test_engine_zip_holds_only_the_engine_with_its_pinned_manifest(tmp_path):
    manifest, engine, _ = offline_release(tmp_path)
    raw = (engine / 'installer-manifest.json').read_bytes()
    out = tmp_path / 'engine.zip'
    build_launcher.build_engine(raw, engine, out)
    with zipfile.ZipFile(out) as archive:
        names = set(archive.namelist())
        assert archive.read('installer-manifest.json') == raw and archive.read('legal/agreement.txt') == TERMS
        assert all(not stat.S_ISLNK(i.external_attr >> 16) for i in archive.infolist())
    assert {'installer/macos/online_setup.py', 'installer/common/workflow.py', 'app/migration/progress_lines.py'} <= names
    assert not any(n.startswith(('tests/', 'face_detect/')) or n.endswith('.swift') for n in names)


def test_python_runtime_is_flattened_and_escaping_links_are_refused(tmp_path):
    source = tmp_path / 'src/python'
    (source / 'bin').mkdir(parents=True)
    (source / 'lib').mkdir()
    (source / 'bin/python3.12').write_text('#!/bin/sh\n')
    (source / 'bin/python3.12').chmod(0o755)
    (source / 'lib/libpython3.12.dylib').write_bytes(b'lib')
    if os.name != 'nt':
        (source / 'bin/python3').symlink_to('python3.12')
        (source / 'lib/libpython3.dylib').symlink_to('libpython3.12.dylib')
    archive = tmp_path / 'cpython.tar.gz'
    with tarfile.open(archive, 'w:gz') as tar:
        tar.add(source, arcname='python')
    out = tmp_path / 'python.zip'
    build_launcher.build_python_runtime(archive, out)
    with zipfile.ZipFile(out) as z:
        modes = {i.filename: i.external_attr >> 16 for i in z.infolist()}
    assert not any(stat.S_ISLNK(m) for m in modes.values())
    if os.name == 'nt':
        return  # no POSIX execute bits or symlinks to check on Windows
    assert modes['bin/python3.12'] & 0o111
    if os.name != 'nt':
        assert modes['bin/python3'] & 0o111 and 'lib/libpython3.dylib' in modes
        (source / 'bin/python3').unlink()
        (source / 'bin/python3').symlink_to('/bin/sh')
        with tarfile.open(tmp_path / 'bad.tar.gz', 'w:gz') as tar:
            tar.add(source, arcname='python')
        with pytest.raises(ValueError, match='escapes'):
            build_launcher.build_python_runtime(tmp_path / 'bad.tar.gz', tmp_path / 'bad.zip')


def descriptor_doc(**changes):
    d = digest(b'x')
    doc = dict(schema_version=1, mode='offline', release_id='test-1', minimum_os='27.0', manifest_sha256=d,
               bootstrap_notices='notices', python=dict(url='https://example.com/p.zip', sha256=d, bundle_path='bootstrap/python.zip'),
               engine=dict(url='https://example.com/e.zip', sha256=d, bundle_path='bootstrap/engine.zip'))
    doc.update(changes)
    return doc


@pytest.mark.parametrize('changes', [dict(mode='sideload'), dict(minimum_os='fourteen'), dict(bootstrap_notices=''),
                                     dict(python=dict(url='http://x/p.zip', sha256=digest(b'x'), bundle_path='p.zip'))])
def test_descriptor_rules(changes):
    build_launcher.validate_descriptor(descriptor_doc())
    with pytest.raises((ValueError, KeyError)):
        build_launcher.validate_descriptor(descriptor_doc(**changes))


@pytest.mark.skipif(not SWIFT, reason='building the app needs Apple Silicon macOS with swiftc')
@pytest.mark.skipif(SWIFT and not MACOS_27, reason='the built app runs only on macOS 27 or newer')
@pytest.mark.ci_swift
def test_built_app_is_signed_and_its_check_verifies_the_bootstrap(tmp_path, monkeypatch):
    """Build the real app with an offline fixture bundle and run its headless `--check`."""
    manifest, engine, _ = offline_release(tmp_path)
    raw = (engine / 'installer-manifest.json').read_bytes()
    bundle = tmp_path / 'bundle'
    (bundle / 'bootstrap').mkdir()
    build_launcher.build_engine(raw, engine, bundle / 'bootstrap/engine.zip')
    with zipfile.ZipFile(bundle / 'bootstrap/python.zip', 'w') as z:  # stand-in bootstrap interpreter
        info = zipfile.ZipInfo('bin/python3.12')
        info.external_attr = (stat.S_IFREG | 0o755) << 16
        z.writestr(info, f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    doc = descriptor_doc(manifest_sha256=manifest.digest,
                         python=dict(url='https://example.invalid/p.zip', sha256=sha(bundle / 'bootstrap/python.zip'), bundle_path='bootstrap/python.zip'),
                         engine=dict(url='https://example.invalid/e.zip', sha256=sha(bundle / 'bootstrap/engine.zip'), bundle_path='bootstrap/engine.zip'))
    (tmp_path / 'bootstrap.json').write_text(json.dumps(doc))
    app = tmp_path / 'FamilyFoto Setup.app'
    build_launcher.build_app(tmp_path / 'bootstrap.json', app, bundle)
    info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
    assert info['LSMinimumSystemVersion'] == '27.0' and info['CFBundleIdentifier'] == build_launcher.BUNDLE_ID
    assert json.loads((app / 'Contents/Resources/bootstrap.json').read_text()) == doc
    signature = subprocess.run(['/usr/bin/codesign', '-dv', str(app)], capture_output=True, text=True).stderr
    assert 'Signature=adhoc' in signature
    home = tmp_path / 'home'  # keep the launcher's cache out of the real Application Support
    result = subprocess.run([str(app / 'Contents/MacOS' / build_launcher.APP_NAME), '--check'],
                            capture_output=True, text=True, env={**os.environ, 'HOME': str(home), 'CFFIXED_USER_HOME': str(home)})
    assert result.returncode == 0, result.stderr
    assert f'agreement {digest(TERMS)}' in result.stdout and 'release test-1' in result.stdout
    cache = home / 'Library/Application Support/FamilyFoto/Setup' / manifest.digest
    assert sorted(p.name for p in cache.iterdir()) == ['engine.zip', 'python.zip']  # session cleaned up
    # A damaged cached download is refused, never reused.
    (cache / 'engine.zip').write_bytes(b'damaged')
    result = subprocess.run([str(app / 'Contents/MacOS' / build_launcher.APP_NAME), '--check'],
                            capture_output=True, text=True, env={**os.environ, 'HOME': str(home), 'CFFIXED_USER_HOME': str(home)})
    assert result.returncode == 1 and 'damaged' in result.stderr


@pytest.mark.skipif(not SWIFT, reason='the Swift launcher tests need Apple Silicon macOS with swiftc')
@pytest.mark.ci_swift
def test_swift_launcher_unit_tests(tmp_path):
    binary = tmp_path / 'launcher-tests'
    subprocess.run(['/usr/bin/swiftc', '-parse-as-library', '-D', 'LAUNCHER_TEST', '-module-cache-path', str(tmp_path / 'cache'),
                    str(ROOT / 'installer/macos/launcher/Launcher.swift'), str(ROOT / 'tests/MacLauncherTests.swift'),
                    '-o', str(binary)], check=True)
    result = subprocess.run([binary], capture_output=True, text=True)
    assert result.returncode == 0 and 'launcher tests passed' in result.stdout, result.stderr


def test_release_files_need_no_swift_for_release_installs():
    # Release installs use the prebuilt setup window; only git checkouts compile Swift.
    names = json.loads((ROOT / 'installer/release-files.json').read_text())['trees']['installer']
    assert '.command' in names and '.swift' not in names


def test_too_little_space_for_install_plus_working_space_downloads_nothing(tmp_path, monkeypatch):
    free = {'value': 9 * 1024**3}  # enough for the fixture install, not for the setup window's 10 GB
    monkeypatch.setattr(online_setup.shutil, 'disk_usage', lambda path: type('Usage', (), {'free': free['value']})())
    with pytest.raises(OSError, match=r'needs 10\.\d GB .* plus 10 GB to work in\) and 9\.0 GB is free\. Nothing was downloaded'):
        run_engine(tmp_path / 'a', monkeypatch, tmp_path / 'a' / 'FamilyFoto')
    assert not (tmp_path / 'a/attempt/cache').exists()
    free['value'] = 11 * 1024**3
    code, handed, _, _ = run_engine(tmp_path / 'b', monkeypatch, tmp_path / 'b' / 'FamilyFoto')
    assert code == 0 and handed


def test_people_terms_say_the_release_carries_the_model(monkeypatch):
    from installer.macos import workflow
    monkeypatch.delenv('FAMILYFOTO_PAYLOAD_RECEIPT', raising=False)
    assert workflow.people_terms() == workflow.PEOPLE_TERMS and 'downloaded during installation' in workflow.people_terms()
    monkeypatch.setenv('FAMILYFOTO_PAYLOAD_RECEIPT', '/tmp/prepared.json')
    text = workflow.people_terms()
    assert 'included with this installer' in text and 'downloaded' not in text and 'non-commercial research' in text
