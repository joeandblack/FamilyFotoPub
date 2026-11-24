"""Mac adapter for the shared installer: fixture payloads only, no services or real releases."""
import io
import json
import os
import platform
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from installer.common.local_payloads import pip_arguments
from installer.common.manifest import Manifest
from installer.common.materialize import materialize
from installer.common.workflow import prepare
from installer.macos import bundled, plan, prepare as mac_prepare, setup_bootstrap, shared_adapter, workflow
from app.backup import handover
from tests.test_shared_installer import component, digest, document

ROOT = Path(__file__).resolve().parents[1]
DMG = 'https://github.com/joeandblack/FamilyFotoPub/releases/download/test-1/FamilyFotoSetup.dmg'
MAC = sys.platform == 'darwin' and platform.machine() == 'arm64'


def program_zip(files):
    """A link-free runtime ZIP whose programs carry the executable bit, as a Mac builder makes."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as archive:
        for name in files:
            info = zipfile.ZipInfo(name)
            info.external_attr = (stat.S_IFREG | 0o755) << 16
            archive.writestr(info, '#!/bin/sh\necho ' + name + '\n')
    return out.getvalue()


def application_zip():
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as archive:
        archive.writestr('FamilyFoto/installer/macos/install.sh', '# fixture only\n')
    return out.getvalue()


EXE = 'https://github.com/joeandblack/FamilyFotoPub/releases/download/test-1/FamilyFotoSetup.exe'


def mac_release(tmp_path, *, setup_url=True, runtimes=tuple(bundled.EXECUTABLES), windows=False):
    data = {'application': application_zip(), 'core-lock': b'demo==1\n', 'password-lock': b'demo==1\n',
            'wheel': b'fixture wheel bytes'}
    programs = {role: program_zip(bundled.EXECUTABLES[role]) for role in runtimes}
    items = [component(key, value) for key, value in data.items()]
    items[1].update(bundle_path='locks/core.txt', lock_role='core')
    items[2].update(bundle_path='locks/password.txt', lock_role='password')
    items[3]['bundle_path'] = 'wheels/demo-1-py3-none-any.whl'
    for role, value in programs.items():
        items.append(component('runtime-' + role, value, runtime_role=role))
        items[-1]['installed_size_estimate'] = 1 << 20
        data['runtime-' + role] = value
    doc = document(items)
    doc.update(platform='macos-arm64', minimum_os='27.0', features={'core': list(data)})
    if setup_url:
        doc['setup_url'] = DMG
    if windows:
        doc['windows_handover'] = {'release_id': 'test-1', 'setup_url': EXE}
    raw = json.dumps(doc).encode()
    release = Manifest(raw, digest(raw))
    attempt = tmp_path / 'attempt'
    prepare(release, platform='macos-arm64', os_version='27.2', features=['core'], agreement=b'terms',
            accepted_digest=digest(b'terms'), directory=attempt, mode='online',
            opener=lambda url: io.BytesIO(data[url.rsplit('/', 1)[1]]), stream=io.StringIO())
    return release, materialize(release, attempt / 'prepared.json', 'macos-arm64', '27.2'), attempt


@pytest.mark.parametrize('change, message', [
    ({'runtime_role': 'node'}, 'runtime role'),
    ({'runtime_role': 'python', 'bundle_path': 'payload/python.tar.gz'}, 'runtime role'),
])
def test_runtime_roles_are_validated(change, message):
    item = component('python')
    item.update(change)
    doc = document([item])
    raw = json.dumps(doc).encode()
    with pytest.raises(ValueError, match=message):
        Manifest(raw, digest(raw))
    duplicate = document([component('a', runtime_role='python'), component('b', runtime_role='python')])
    raw = json.dumps(duplicate).encode()
    with pytest.raises(ValueError, match='runtime role'):
        Manifest(raw, digest(raw))


def test_offline_shape_stages_application_and_unpacks_runnable_programs(tmp_path):
    release, receipt, attempt = mac_release(tmp_path)
    root = tmp_path / 'FamilyFoto'
    shared_adapter.stage_application(release, receipt, root)
    marker = json.loads((root / '.shared-installer-release.json').read_text())
    assert marker['mac_handover'] == {'release_id': 'test-1', 'setup_url': DMG}
    assert 'windows_handover' not in marker
    dirs = shared_adapter.unpack_runtimes(release, receipt, root)
    assert set(dirs) == set(bundled.EXECUTABLES)
    for role, names in bundled.EXECUTABLES.items():
        for name in names:
            path = root / 'bundled' / role / name
            assert path.is_file() and os.access(path, os.X_OK)
            if os.name != 'nt':  # POSIX modes only; Windows has no executable bit
                assert stat.S_IMODE(path.stat().st_mode) == 0o755
    if os.name != 'nt':
        result = subprocess.run([root / 'bundled/ffmpeg/bin/ffprobe'], capture_output=True, text=True)
        assert result.stdout.strip() == 'bin/ffprobe'
    # Repeating the handoff reuses the same owned programs.
    assert shared_adapter.unpack_runtimes(release, receipt, root) == dirs
    os.environ['FAMILYFOTO_PAYLOAD_RECEIPT'] = str(attempt / 'prepared.json')
    try:
        for role in ('core', 'password'):
            assert {'--no-index', '--require-hashes', '--only-binary=:all:'} <= set(pip_arguments(role))
    finally:
        del os.environ['FAMILYFOTO_PAYLOAD_RECEIPT']


def test_interrupted_unpack_is_redone_and_foreign_programs_refused(tmp_path):
    release, receipt, _ = mac_release(tmp_path)
    root = tmp_path / 'FamilyFoto'
    shared_adapter.stage_application(release, receipt, root)
    shared_adapter.unpack_runtimes(release, receipt, root)
    marker = root / 'bundled/python.json'
    record = json.loads(marker.read_text())
    marker.write_text(json.dumps(dict(record, state='unpacking')))  # stopped mid-unpack
    (root / 'bundled/python/bin/python3.12').unlink()
    shared_adapter.unpack_runtimes(release, receipt, root)
    assert (root / 'bundled/python/bin/python3.12').is_file()
    assert json.loads(marker.read_text())['state'] == 'complete'
    marker.unlink()  # a folder with no record of ours is never replaced
    with pytest.raises(ValueError, match='not from this release'):
        shared_adapter.unpack_runtimes(release, receipt, root)


def test_release_without_every_program_is_refused(tmp_path):
    release, receipt, _ = mac_release(tmp_path, runtimes=('python', 'postgres'))
    root = tmp_path / 'FamilyFoto'
    shared_adapter.stage_application(release, receipt, root)
    with pytest.raises(ValueError, match='lacks bundled ffmpeg'):
        shared_adapter.unpack_runtimes(release, receipt, root)


def test_changed_program_archive_is_refused_before_anything_is_unpacked(tmp_path):
    release, receipt, _ = mac_release(tmp_path)
    root = tmp_path / 'FamilyFoto'
    shared_adapter.stage_application(release, receipt, root)
    Path(receipt['paths']['runtime-postgres']).chmod(0o600)
    Path(receipt['paths']['runtime-postgres']).write_bytes(b'tampered')
    with pytest.raises(ValueError, match='changed after download'):
        shared_adapter.unpack_runtimes(release, receipt, root)
    assert not (root / 'bundled').exists()


def test_installer_steps_use_local_inputs_only_with_a_receipt(tmp_path, monkeypatch):
    release, receipt, attempt = mac_release(tmp_path)
    root = tmp_path / 'FamilyFoto'
    shared_adapter.stage_application(release, receipt, root)
    shared_adapter.unpack_runtimes(release, receipt, root)
    monkeypatch.delenv('FAMILYFOTO_PAYLOAD_RECEIPT', raising=False)
    assert '-r' in mac_prepare.core_dependency_command('py', root)
    assert setup_bootstrap.password_sources()[-1].endswith('password-requirements.txt')
    monkeypatch.setenv('FAMILYFOTO_PAYLOAD_RECEIPT', str(attempt / 'prepared.json'))
    assert mac_prepare.core_dependency_command('py', root) == [
        'py', '-m', 'installer.common.install_dependencies', '--role', 'core']
    sources = setup_bootstrap.password_sources()
    assert '--no-index' in sources and Path(sources[-1]).as_posix().endswith('locks/password.txt')
    monkeypatch.setattr(bundled, 'ROOT', root)
    monkeypatch.setattr(workflow.subprocess, 'run', lambda *a, **k: pytest.fail('Homebrew consulted'))
    assert workflow.tool_dirs() == (root / 'bundled/postgres/bin', root / 'bundled/ffmpeg/bin')
    monkeypatch.setattr(plan, 'port_status', lambda port: 'available (not reserved)')
    result = plan.inspect(root, {'database': 5433, 'web': 4341}, brew_root=tmp_path / 'no-homebrew')
    assert all(result['tools'].values()) and 'Homebrew' not in result['tools']
    assert not [b for b in result['blockers'] if 'installed explicitly' in b]


@pytest.mark.skipif(not MAC, reason='install.sh requires Apple Silicon macOS')
def test_install_script_runs_the_bundled_python(tmp_path):
    root = tmp_path / 'FamilyFoto'
    (root / 'installer/macos').mkdir(parents=True)
    script = root / 'installer/macos/install.sh'
    script.write_text((ROOT / 'installer/macos/install.sh').read_text())
    python = root / 'bundled/python/bin/python3.12'
    python.parent.mkdir(parents=True)
    python.write_text('#!/bin/sh\necho "bundled python: $*"\n')
    python.chmod(0o755)
    env = {**os.environ, 'FAMILYFOTO_PAYLOAD_RECEIPT': str(tmp_path / 'prepared.json')}
    for mode, expected in [('plan', 'installer/macos/plan.py'), ('install', 'installer/macos/setup_bootstrap.py install')]:
        result = subprocess.run(['/bin/bash', script, mode], capture_output=True, text=True, env=env)
        assert result.returncode == 0 and 'bundled python: -B ' in result.stdout and expected in result.stdout
    python.unlink()
    result = subprocess.run(['/bin/bash', script, 'install'], capture_output=True, text=True, env=env)
    assert result.returncode == 1 and 'bundled Python is missing' in result.stderr


def test_mac_release_backups_name_the_windows_setup(tmp_path):
    release, receipt, _ = mac_release(tmp_path, windows=True)
    root = tmp_path / 'FamilyFoto'
    shared_adapter.stage_application(release, receipt, root)
    marker = root / '.shared-installer-release.json'
    assert json.loads(marker.read_text())['windows_handover'] == {'release_id': 'test-1', 'setup_url': EXE}
    backup = tmp_path / 'backup'
    backup.mkdir()
    handover.write_handover(backup, marker)
    assert DMG in (backup / 'restore-on-mac/RELEASE.txt').read_text()
    assert sorted(p.name for p in (backup / 'restore-on-windows').iterdir()) == ['README.txt']
    text = (backup / 'restore-on-windows/README.txt').read_text()
    assert EXE in text and 'Installer release: test-1' in text


def receipt_file(tmp_path, content):
    path = tmp_path / 'marker.json'
    path.write_text(content if isinstance(content, str) else json.dumps(content))
    return path


def test_new_backups_name_the_mac_release(tmp_path):
    backup = tmp_path / 'backup'
    backup.mkdir()
    marker = receipt_file(tmp_path, {'mac_handover': {'release_id': 'test-1', 'setup_url': DMG}})
    handover.write_handover(backup, marker)
    text = (backup / 'restore-on-mac/RELEASE.txt').read_text()
    assert DMG in text and 'install.sh restore' in text and 'Do not run programs' in text
    assert 'shasum -a 256' in text and 'Open Anyway' in text
    assert sorted(p.name for p in (backup / 'restore-on-mac').iterdir()) == ['README.txt', 'RELEASE.txt']
    assert not (backup / 'restore-on-windows').exists()  # a Mac release names no Windows installer


@pytest.mark.parametrize('content', ['{', {'mac_handover': {'release_id': 'test-1'}},
                                     {'mac_handover': {'release_id': 'test-1', 'setup_url': DMG[:-3] + 'exe'}}])
def test_bad_release_record_never_fails_a_backup_and_clears_stale_link(tmp_path, content):
    backup = tmp_path / 'backup'
    (backup / 'restore-on-mac').mkdir(parents=True)
    (backup / 'restore-on-mac/RELEASE.txt').write_text('stale link from an earlier attempt')
    handover.write_handover(backup, receipt_file(tmp_path, content))
    assert not (backup / 'restore-on-mac/RELEASE.txt').exists()
    assert (backup / 'restore-on-mac/README.txt').is_file()


def test_release_backups_drop_the_terminal_launcher_left_by_an_earlier_attempt(tmp_path):
    backup = tmp_path / 'backup'
    old = backup / 'restore-on-mac/restore-familyfoto-mac.command'
    old.parent.mkdir(parents=True)
    old.write_text('#!/bin/bash\n')
    handover.write_handover(backup, receipt_file(tmp_path, {'mac_handover': {'release_id': 'test-1', 'setup_url': DMG}}))
    assert not old.exists() and (backup / 'restore-on-mac/RELEASE.txt').is_file()


def test_source_install_backups_keep_the_terminal_launcher(tmp_path):
    # No release is published to point at yet, so the source route stays usable.
    backup = tmp_path / 'backup'
    backup.mkdir()
    handover.write_handover(backup, tmp_path / 'absent.json')
    folder = backup / 'restore-on-mac'
    assert sorted(p.name for p in folder.iterdir()) == ['README.txt', 'restore-familyfoto-mac.command']
    if os.name != 'nt':  # Windows has no Unix execute bit to set or read
        assert (folder / 'restore-familyfoto-mac.command').stat().st_mode & 0o700 == 0o700
    text = (folder / 'README.txt').read_text()
    assert 'With FamilyFoto Setup' in text and 'With the Terminal launcher' in text
    # It names exactly the code that made the backup (this checkout's commit, if unchanged).
    assert f"commit='{handover.source_commit()}'\n" in (folder / 'restore-familyfoto-mac.command').read_text()


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True, check=True).stdout.strip()


def test_source_commit_only_for_an_unchanged_checkout(tmp_path):
    assert handover.source_commit(tmp_path) == ''  # neither a checkout nor a release folder
    git(tmp_path, 'init', '-q')
    (tmp_path / 'app.py').write_text('original\n')
    git(tmp_path, 'add', 'app.py')
    git(tmp_path, '-c', 'user.name=t', '-c', 'user.email=t@example.invalid', 'commit', '-q', '-m', 'one')
    head = git(tmp_path, 'rev-parse', 'HEAD')
    assert handover.source_commit(tmp_path) == head
    (tmp_path / 'notes.txt').write_text('untracked files are not code\n')
    assert handover.source_commit(tmp_path) == head
    (tmp_path / 'app.py').write_text('changed locally\n')
    assert handover.source_commit(tmp_path) == ''  # HEAD is not the code that made the backup
    git(tmp_path, 'add', 'app.py')
    assert handover.source_commit(tmp_path) == ''  # staged changes count too


def test_source_commit_for_a_release_folder_only_while_it_matches_its_manifest(tmp_path):
    from installer.release import build
    with zipfile.ZipFile(build(ROOT, tmp_path / 'release', allow_dirty=True)) as package:
        package.extractall(tmp_path / 'extracted')
    installed = tmp_path / 'extracted/FamilyFoto'
    manifest = json.loads((installed / 'release-manifest.json').read_text())
    assert handover.source_commit(installed) == manifest['source_commit']
    (installed / 'app/backup/handover.py').write_text('# edited after installing\n')
    assert handover.source_commit(installed) == ''


def test_launcher_is_written_with_one_commit_line():
    text = handover.launcher('c' * 40).decode()
    assert text.count("commit='") == 1 and f"commit='{'c' * 40}'" in text


@pytest.mark.skipif(os.name == 'nt', reason='ordinary Windows accounts cannot create symbolic links; '
                    'hard-link refusal is covered in test_backup_capture_retry')
def test_a_linked_terminal_launcher_is_refused(tmp_path):
    backup = tmp_path / 'backup'
    old = backup / 'restore-on-mac/restore-familyfoto-mac.command'
    old.parent.mkdir(parents=True)
    (tmp_path / 'elsewhere').write_text('not ours')
    old.symlink_to(tmp_path / 'elsewhere')
    with pytest.raises(ValueError, match='Linked or unexpected'):
        handover.write_handover(backup, receipt_file(tmp_path, {'mac_handover': {'release_id': 'test-1', 'setup_url': DMG}}))
    assert (tmp_path / 'elsewhere').read_text() == 'not ours'


def test_installs_without_a_release_write_no_release_file(tmp_path):
    backup = tmp_path / 'backup'
    backup.mkdir()
    handover.write_handover(backup, tmp_path / 'absent.json')
    assert not (backup / 'restore-on-mac/RELEASE.txt').exists()
    assert 'RELEASE.txt' in (backup / 'restore-on-mac/README.txt').read_text()


def test_mac_release_must_declare_supported_minimum_os(tmp_path):
    release, _, _ = mac_release(tmp_path)
    shared_adapter.require_minimum_os(release)  # macOS 27 or newer only
    for value in ('26.4', None):
        doc = dict(release.document)
        if value is None:
            doc.pop('minimum_os')
        else:
            doc['minimum_os'] = value
        raw = json.dumps(doc).encode()
        with pytest.raises(ValueError, match='minimum_os 27.0'):
            shared_adapter.require_minimum_os(Manifest(raw, digest(raw)))


@pytest.mark.parametrize('damage', ['truncate', 'delete', 'link'])
def test_resume_checks_the_unpacked_tree_not_only_the_archive(tmp_path, damage):
    release, receipt, _ = mac_release(tmp_path)
    root = tmp_path / 'FamilyFoto'
    shared_adapter.stage_application(release, receipt, root)
    shared_adapter.unpack_runtimes(release, receipt, root)
    program = root / 'bundled/postgres/bin/initdb'
    if damage == 'truncate':
        program.write_text('')
    elif damage == 'delete':
        program.unlink()
    else:
        if os.name == 'nt':
            pytest.skip('symlink creation needs privileges on Windows')
        (root / 'bundled/postgres/bin/extra').symlink_to('/bin/sh')
    with pytest.raises(ValueError, match='changed or incomplete|Unexpected link'):
        shared_adapter.unpack_runtimes(release, receipt, root)


def test_bytecode_rewritten_by_python_does_not_fail_resume(tmp_path):
    release, receipt, _ = mac_release(tmp_path)
    root = tmp_path / 'FamilyFoto'
    shared_adapter.stage_application(release, receipt, root)
    shared_adapter.unpack_runtimes(release, receipt, root)
    cache = root / 'bundled/python/lib/__pycache__'
    cache.mkdir(parents=True)
    (cache / 'site.cpython-312.pyc').write_bytes(b'rewritten by the interpreter')
    shared_adapter.unpack_runtimes(release, receipt, root)


def test_linked_stale_release_file_is_refused_like_other_handover_files(tmp_path):
    if os.name == 'nt':
        pytest.skip('symlink creation needs privileges on Windows')
    backup = tmp_path / 'backup'
    (backup / 'restore-on-mac').mkdir(parents=True)
    elsewhere = tmp_path / 'other-installation-RELEASE.txt'
    elsewhere.write_text('another installation link')
    (backup / 'restore-on-mac/RELEASE.txt').symlink_to(elsewhere)
    with pytest.raises(ValueError, match='Linked or unexpected'):
        handover.write_handover(backup, tmp_path / 'absent.json')
    assert elsewhere.read_text() == 'another installation link'


@pytest.mark.parametrize('change', ['same-length', 'extra-file'])
def test_resume_detects_same_length_tampering_and_added_files(tmp_path, change):
    release, receipt, _ = mac_release(tmp_path)
    root = tmp_path / 'FamilyFoto'
    shared_adapter.stage_application(release, receipt, root)
    shared_adapter.unpack_runtimes(release, receipt, root)
    program = root / 'bundled/postgres/bin/initdb'
    if change == 'same-length':
        program.write_bytes(b'X' * program.stat().st_size)
        match = 'changed or incomplete'
    else:
        (root / 'bundled/python/bin/sitecustomize.py').write_text('print("injected")\n')
        match = 'Unexpected file'
    with pytest.raises(ValueError, match=match):
        shared_adapter.unpack_runtimes(release, receipt, root)


def release_install(tmp_path):
    """A staged release installation with its receipt recorded, as the adapter leaves it."""
    release, receipt, attempt = mac_release(tmp_path)
    root = tmp_path / 'FamilyFoto'
    shared_adapter.stage_application(release, receipt, root)
    shared_adapter.unpack_runtimes(release, receipt, root)
    bundled.record_receipt(root, attempt / 'prepared.json', release.digest)
    return release, root, attempt


def test_recorded_receipt_is_checked_against_the_installation(tmp_path):
    release, root, attempt = release_install(tmp_path)
    assert bundled.resume_receipt(root) == (attempt / 'prepared.json').absolute()
    bundled.record_receipt(root, attempt / 'prepared.json', '0' * 64)
    with pytest.raises(ValueError, match='does not match its release record'):
        bundled.resume_receipt(root)
    bundled.record_receipt(root, attempt / 'prepared.json', release.digest)
    (attempt / 'prepared.json').unlink()
    with pytest.raises(ValueError, match='downloaded installer files are gone'):
        bundled.resume_receipt(root)


@pytest.mark.skipif(not MAC, reason='install.sh requires Apple Silicon macOS')
def test_restore_resumes_from_a_fresh_shell_without_homebrew(tmp_path):
    _, root, attempt = release_install(tmp_path)
    (root / 'installer/macos/install.sh').write_text((ROOT / 'installer/macos/install.sh').read_text())
    (root / 'installer/macos/bundled.py').write_text((ROOT / 'installer/macos/bundled.py').read_text())
    # Stand-in for the bundled interpreter: report what setup_bootstrap would receive.
    python = root / 'bundled/python/bin/python3.12'
    python.write_text('#!/bin/sh\ncase "$*" in *setup_bootstrap.py*) '
                      'echo "receipt=$FAMILYFOTO_PAYLOAD_RECEIPT args=$*"; exit 0;; esac\n'
                      f'exec {sys.executable} "$@"\n')
    clean = {'HOME': os.environ.get('HOME', str(tmp_path)), 'PATH': '/usr/bin:/bin'}  # no receipt, no Homebrew
    result = subprocess.run(['/bin/bash', root / 'installer/macos/install.sh', 'restore'],
                            capture_output=True, text=True, env=clean, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert f"receipt={(attempt / 'prepared.json').absolute()}" in result.stdout
    assert 'setup_bootstrap.py restore' in result.stdout and 'Homebrew' not in result.stderr
    (attempt / 'prepared.json').unlink()
    result = subprocess.run(['/bin/bash', root / 'installer/macos/install.sh', 'restore'],
                            capture_output=True, text=True, env=clean, cwd=tmp_path)
    assert result.returncode == 1 and 'Open FamilyFoto Setup again' in result.stderr
