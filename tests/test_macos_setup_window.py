"""Bootstrap checks exercised without installing application dependencies or touching a library."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.backup import passwords
from installer.macos import setup_checks as checks
from tests.test_macos_restore_installer import restore_args


@pytest.fixture
def form(tmp_path, monkeypatch):
    args = restore_args(tmp_path)
    (args.backup / "capture-timing.json").write_text(json.dumps({"media_bytes": 100}))
    monkeypatch.setattr(checks, 'ROOT', tmp_path)
    monkeypatch.setattr(checks.prepare, 'require_fresh', lambda root: None)
    monkeypatch.setattr(checks, 'port_free', lambda port: True)
    monkeypatch.setattr(checks, 'loaded_instances', lambda: set())
    monkeypatch.setattr(checks, 'disk_protected', lambda parent: True)
    monkeypatch.setattr(checks.shutil, 'disk_usage', lambda path: SimpleNamespace(free=20 * 1024**3))
    return dict(restore=True, copy=True, parent=str(tmp_path), backup=str(args.backup),
                **checks.layout(set()))


def test_password_check_is_read_only_and_not_part_of_typing_checks(tmp_path, form, monkeypatch):
    before = {path: path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()}
    assert checks.password_problem(Path(form['backup']), 'fixture administrator password') == ''
    assert 'incorrect' in checks.password_problem(Path(form['backup']), 'wrong password')
    monkeypatch.setattr(passwords, 'unlock_context', lambda *args: pytest.fail('KDF on normal validation'))
    assert checks.validate(form) == {}
    assert before == {path: path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()}


@pytest.mark.skipif(not hasattr(checks.os, 'getuid'), reason='Mac key permissions')
def test_disk_checks(form, tmp_path, monkeypatch):
    backup = Path(form['backup'])
    assert 'backup' in checks.validate(dict(form, backup=str(tmp_path/'absent')))
    assert 'outside' in checks.validate(dict(form, parent=str(backup)))['parent']
    monkeypatch.setattr(checks, 'disk_protected', lambda parent: False)
    assert 'unprotected' in checks.validate(form)
    (backup / 'capture-timing.json').write_text(json.dumps(dict(media_bytes=20 * 1024**3)))
    assert 'more free space' in checks.validate(form)['parent']
    (tmp_path/'FFLibrary').mkdir()
    assert 'does not exist' in checks.validate(form)['parent']


def test_named_layout_and_port_checks(form, monkeypatch):
    monkeypatch.setattr(checks, 'port_free', lambda port: port != 5434)
    assert form['instance'] == '' and form['database'] == '5433'
    second = checks.layout({'restore'})
    assert (second['instance'], second['database'], second['web']) == ('restore-2', '5435', '4342')
    assert 'layout' in checks.validate(dict(form, restore=False, database='4341'))


@pytest.mark.skipif(checks.sys.platform != 'darwin', reason='SwiftUI needs macOS')
@pytest.mark.real_scrypt
def test_native_window_model_and_restore_contract(tmp_path):
    import subprocess
    binary = tmp_path / 'setup-window-test'
    subprocess.run(['swiftc', '-parse-as-library', '-D', 'SETUP_WINDOW_TEST',
                    '-module-cache-path', str(tmp_path / 'cache'),
                    'installer/macos/SetupWindow.swift', 'tests/MacSetupWindowTests.swift',
                    '-o', binary], check=True)
    subprocess.run([binary], check=True)
    # Run the real Swift arguments/frame through setup_run -> workflow -> restore.py.
    # Only the installation/Move boundary is stubbed: no database or services are touched.
    import base64
    import shutil
    import sys
    args = restore_args(tmp_path)
    result = subprocess.run([binary, '--contract', args.backup, tmp_path], capture_output=True, check=True)
    contract = json.loads(result.stdout)
    package = tmp_path / 'package'
    (package / 'installer').mkdir(parents=True)
    shutil.copytree('installer/macos', package / 'installer/macos', ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copy('installer/configure.py', package / 'installer/configure.py')
    (package / 'app').symlink_to(Path('app').resolve(), target_is_directory=True)
    (package / 'runtime').mkdir()
    (package / 'runtime/macos-database-ready.json').write_text('{}')
    (package / '.macos-setup.json').write_text(json.dumps(contract['arguments']))
    (package / '.venv/bin').mkdir(parents=True)
    engine = package / '.venv/bin/python3'
    engine.write_text('#!' + sys.executable + '\n' + r'''import sys, json
from pathlib import Path
root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root))
from installer.macos import restore
from app.migration import move_in
restore.installation = lambda port: None
move_in.make_plan = lambda *a, **k: {}
class Move:
    def __init__(self, *a, **k): pass
    def begun(self): return False
    def open(self, plan): pass
    def run(self, key, confirm, *, password):
        assert password == 'fixture administrator password'
        assert key.backup_id
        (root / 'observed.json').write_text(json.dumps({'arguments': sys.argv[2:], 'password_received': True}))
        return dict(state='done', media_files=1, url='https://127.0.0.1:4341', certificate_names=[], certificate_authority='fixture')
move_in.Move = Move
raise SystemExit(restore.main(sys.argv[2:]))
''')
    engine.chmod(0o700)
    frame = base64.b64decode(contract['frame'])
    run = subprocess.run([sys.executable, '-B', package / 'installer/macos/setup_run.py', *contract['arguments']],
                         input=frame, capture_output=True, cwd=package)
    assert run.returncode == 0, (run.stdout, run.stderr)
    observed = json.loads((package / 'observed.json').read_bytes())
    assert observed['password_received'] and '--backup-password-frame-stdin' in observed['arguments']
    assert '--key-file' not in observed['arguments']
    assert b'fixture administrator password' not in run.stdout + run.stderr
    assert not (package / '.macos-setup.json').exists()
    assert 'fixture administrator password' not in (package / '.macos-setup.log').read_text()


@pytest.mark.parametrize('timing', [None, '{', '{}', '{"media_bytes": -1}', '{"media_bytes": true}', '{"media_bytes":"12"}'])
def test_unusable_timing_checks_media_tree_size(form, timing):
    backup = Path(form['backup'])
    path = backup / 'capture-timing.json'
    if timing is None:
        path.unlink()
    else:
        path.write_text(timing)
    assert 'Cannot verify copy space' in checks.validate(form)['parent']
    tree = backup / 'media-copy/TREE-COMPLETE.json'
    tree.parent.mkdir()
    tree.write_text(json.dumps({'bytes': 20 * 1024**3}))
    assert 'more free space' in checks.validate(form)['parent']
    tree.write_text(json.dumps({'bytes': 100}))
    assert 'parent' not in checks.validate(form)


@pytest.fixture
def runner(tmp_path, monkeypatch):
    pytest.importorskip('fcntl')
    from installer.macos import setup_run
    monkeypatch.setattr(setup_run, 'ROOT', tmp_path)
    monkeypatch.setattr(setup_run, 'RECORD', tmp_path / '.macos-setup.json')
    return setup_run


def test_wrong_password_does_not_write_record_and_retry_uses_private_pipe(form, runner, tmp_path):
    args = ['restore', '--backup-password-frame-stdin', '--backup', form['backup']]
    assert runner.run(args, secret='wrong password') == runner.ENTER_PASSWORD
    assert not runner.RECORD.exists()
    script = tmp_path / 'installer/macos/workflow.py'
    script.parent.mkdir(parents=True)
    # A stopped child leaves the choices, never the password, for another attempt.
    script.write_text("import sys, json, struct\nb = sys.stdin.buffer\nn, = struct.unpack('>I', b.read(4))\nassert json.loads(b.read(n)) == {'password': 'fixture administrator password'}\nprint('private frame received')\nsys.exit(1)\n")
    assert runner.run(args, secret='fixture administrator password') == 1
    assert json.loads(runner.RECORD.read_bytes()) == args
    assert 'fixture administrator password' not in runner.RECORD.read_text()
    assert 'fixture administrator password' not in (tmp_path / '.macos-setup.log').read_text()
    assert runner.run(args) == runner.ENTER_PASSWORD
    with pytest.raises(SystemExit, match='different choices'):
        runner.run(args + ['--media', 'copy'], secret='fixture administrator password')


def test_interrupted_people_requires_inspection_after_failure_and_relaunch(runner, tmp_path):
    args = ['install', '--with-people']
    script = tmp_path / 'installer/macos/workflow.py'
    script.parent.mkdir(parents=True)
    script.write_text("""from pathlib import Path
import sys
Path('runtime').mkdir()
Path('runtime/macos-database-ready.json').touch()
Path('runtime/macos-people-install.json').write_text('{"state":"installing"}')
sys.exit(1)
""")
    runner.save_record(args)
    assert runner.run(args) == runner.RECOVER_PEOPLE
    script.unlink()
    assert runner.run(args) == runner.RECOVER_PEOPLE
    assert json.loads((tmp_path / 'runtime/macos-people-install.json').read_bytes()) == {'state': 'installing'}


def test_resume_after_thumbnails_needs_neither_password_nor_backup(runner, tmp_path):
    args = ['restore', '--backup-password-frame-stdin', '--backup', str(tmp_path / 'disconnected')]
    runner.save_record(args)
    journal = tmp_path / 'restore/MOVE-IN.json'
    journal.parent.mkdir()
    journal.write_text(json.dumps({'stages': {'thumbnails': {'done': True}}}))
    script = tmp_path / 'installer/macos/workflow.py'
    script.parent.mkdir(parents=True)
    script.write_text("import sys\nassert sys.stdin.buffer.read() == b''\n")
    assert runner.run(args) == 0
    assert not runner.RECORD.exists()


@pytest.mark.skipif(checks.sys.platform != 'darwin', reason='Mac runtime ownership')
@pytest.mark.parametrize('exit_code', [0, 1])
def test_bootstrap_cleans_temporary_runtime_on_success_and_failure(tmp_path, monkeypatch, exit_code):
    from installer.macos import setup_bootstrap as bootstrap
    folders = []
    def prepare(folder):
        folders.append(folder.parent)
        folder.mkdir()
        return folder / 'bin/python3'
    monkeypatch.setattr(bootstrap, 'prepare_runtime', prepare)
    monkeypatch.setattr(bootstrap, 'run_owned', lambda *a, **k: exit_code)
    assert bootstrap.main(['restore', '--no-dialog']) == exit_code
    assert len(folders) == 1 and not folders[0].exists()


def test_bootstrap_children_cannot_read_password_pipe(tmp_path, monkeypatch):
    from installer.macos import setup_bootstrap as bootstrap
    commands = []
    def run(command, folder, **kwargs):
        assert kwargs['stdin'] == bootstrap.subprocess.DEVNULL
        assert 'PYTHONPATH' not in kwargs['env'] and 'PYTHONHOME' not in kwargs['env']
        commands.append(list(map(str, command)))
    monkeypatch.setenv('PYTHONPATH', '/untrusted')
    monkeypatch.setenv('PYTHONHOME', '/untrusted')
    monkeypatch.setattr(bootstrap, 'checked_owned', run)
    bootstrap.prepare_runtime(tmp_path / 'python')
    assert '--require-hashes' in commands[1] and '--only-binary=:all:' in commands[1]


def test_late_resume_workflow_does_not_read_password_pipe(tmp_path, monkeypatch):
    from installer.macos import workflow
    journal = tmp_path / 'restore/MOVE-IN.json'
    journal.parent.mkdir()
    journal.write_text(json.dumps({'stages': {'thumbnails': 'done'}}))
    monkeypatch.setattr(workflow, 'ROOT', tmp_path)
    monkeypatch.setattr(workflow.sys, 'argv', ['workflow.py', 'restore', '--backup-password-frame-stdin'])
    monkeypatch.setattr(workflow.sys, 'platform', 'darwin')
    monkeypatch.setattr(workflow.platform, 'machine', lambda: 'arm64')
    monkeypatch.setattr(workflow.os, 'geteuid', lambda: 501, raising=False)
    monkeypatch.setattr(workflow, 'consume_password', lambda *a: pytest.fail('Read unused password'))
    monkeypatch.setattr(workflow, 'close_input', lambda: None)
    received = []
    monkeypatch.setattr(workflow, 'run_restore', lambda args, people, secret: received.append(secret) or 0)
    assert workflow.main() == 0 and received == [None]


@pytest.mark.skipif(checks.sys.platform != 'darwin', reason='Mac temporary runtime ownership')
def test_crash_cleanup_keeps_live_unknown_and_linked_folders(tmp_path, monkeypatch):
    import os
    from installer.macos import setup_bootstrap as bootstrap
    monkeypatch.setattr(bootstrap.tempfile, 'gettempdir', lambda: str(tmp_path))
    for name in ('dead', 'live', 'unknown'):
        folder = tmp_path / ('familyfoto-setup-' + name)
        folder.mkdir(mode=0o700)
        if name != 'unknown':
            (folder / 'owners.json').write_text(json.dumps([12345 if name == 'dead' else os.getpid()]))
    (tmp_path / 'familyfoto-setup-link').symlink_to(tmp_path / 'familyfoto-setup-unknown')
    def kill(pid, signal):
        if pid == 12345:
            raise ProcessLookupError()
    monkeypatch.setattr(bootstrap.os, 'kill', kill)
    bootstrap.clean_abandoned_runtimes()
    assert not (tmp_path / 'familyfoto-setup-dead').exists()
    assert all((tmp_path / ('familyfoto-setup-' + name)).exists() for name in ('live', 'unknown', 'link'))


@pytest.mark.skipif(checks.sys.platform != 'darwin', reason='Mac temporary runtime ownership')
def test_runtime_owner_registration_preserves_descendants(tmp_path, monkeypatch):
    from installer.macos import setup_bootstrap as bootstrap
    marker = tmp_path / 'owners.json'
    monkeypatch.setenv('FAMILYFOTO_SETUP_OWNERS', str(marker))
    bootstrap.register_owner(pid=101)
    bootstrap.register_owner(pid=202)
    bootstrap.register_owner(pid=101)
    assert json.loads(marker.read_text()) == [101, 202]


@pytest.mark.skipif(checks.sys.platform != 'darwin', reason='Mac runtime ownership')
def test_window_exit_preserves_runtime_until_surviving_child_exits(tmp_path, monkeypatch):
    import subprocess
    import sys
    from installer.macos import setup_bootstrap as bootstrap
    monkeypatch.setattr(bootstrap.tempfile, 'gettempdir', lambda: str(tmp_path))
    child = None
    try:
        with bootstrap.temporary_runtime() as folder:
            (folder / 'fixture_dependency.py').write_text('answer = 42\n')
            child = subprocess.Popen([sys.executable, '-c',
                'import sys; sys.stdin.read(1); from fixture_dependency import answer; print(answer)'],
                cwd=folder, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            bootstrap.register_owner(folder / 'owners.json', child.pid)
        # Simulate the window's exit while its restore child remains alive, then import a
        # dependency from that runtime. The old TemporaryDirectory deleted it here.
        assert folder.exists() and child.poll() is None
        bootstrap.clean_abandoned_runtimes()
        assert folder.exists()
        out, err = child.communicate(b'x', timeout=10)
        assert child.returncode == 0 and out.strip() == b'42', err
        bootstrap.clean_abandoned_runtimes()
        assert not folder.exists()
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.communicate()


@pytest.mark.parametrize('stages, needed', [({}, True), ({'restored': 'done'}, True),
    ({'switched': 'done'}, True), ({'thumbnails': 'done'}, False), ({'people': 'done'}, False)])
def test_preflight_and_engine_share_backup_dependency(tmp_path, stages, needed):
    from installer.macos.restore_input import needs_backup
    from app.migration.move_in import Move
    journal = tmp_path / 'restore/MOVE-IN.json'
    journal.parent.mkdir()
    journal.write_text(json.dumps({'stages': stages}))
    move = object.__new__(Move)
    move.journal = {'stages': stages}
    assert needs_backup(tmp_path) == move.needs_backup() == needed


@pytest.mark.parametrize('raw', ['{', 'null', '[]', '{}', '{"stages":[]}'])
def test_damaged_resume_journal_has_actionable_error(tmp_path, raw):
    from installer.macos.restore_input import needs_backup
    journal = tmp_path / 'restore/MOVE-IN.json'
    journal.parent.mkdir()
    journal.write_text(raw)
    with pytest.raises(passwords.PasswordProblem, match='Keep this installation folder'):
        needs_backup(tmp_path)
    assert journal.read_text() == raw
