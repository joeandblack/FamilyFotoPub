"""The one-stop restore's order, its stop at the switch warning, and carrying on after a stop.

The steps it runs (restore, validation, activation, People setup) have their own tests; here
they are stand-ins that record the order and leave what the real ones leave behind.
"""
from contextlib import contextmanager
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.backup.diagnostics import ExplainedFailure
from app.migration import move_in
from app.migration.move_in import GATE, Installation, Move, make_plan

# Stand-in backups have no sealed index: their key is this, for backup 'b' * 32.
KEY = SimpleNamespace(backup_id='b' * 32)
PASSWORD = 'test administrator password'


@pytest.fixture
def world(tmp_path, monkeypatch):
    # These backups are stand-ins with no sealed index; tests of the password check itself use real ones.
    monkeypatch.setattr(move_in, 'unlock_backup', lambda backup, password: KEY)
    root = tmp_path / 'FamilyFoto'
    for name in ('config/tls', 'data', 'logs'):
        (root / name).mkdir(parents=True)
    (root / 'config/tls/server.pem').write_text('installer certificate')
    (root / 'data/setup-marker').write_text('installer data')
    (root / 'config/local.json').write_text(json.dumps({
        'host': '127.0.0.1', 'port': 4342, 'postgres_admin_password': 'pw', 'database': {'dbname': 'familyfoto'},
        'video_tools': {'ffmpeg': '/opt/homebrew/bin/ffmpeg'}, 'browser_user': 'family', 'browser_password_hash': 'h',
        'auth_secret': 's', 'io_mibps': 30, 'source': {}, 'destination': {}, 'tls': {}}))
    backup = tmp_path / 'backup'
    (backup / 'catalog').mkdir(parents=True)
    calls, database = [], []

    @contextmanager
    def running():
        database.append('up')
        yield
        database.append('down')

    installation = Installation(root=root, admin=SimpleNamespace(host='127.0.0.1', port=5434, user='postgres'),
                                cluster=root / 'runtime/postgres', pg_restore=Path('/pg/pg_restore'),
                                pg_dump=Path('/pg/pg_dump'), web_port=4342, people_port=54343,
                                database=running, start=lambda: calls.append('start'))
    stand_ins(monkeypatch, calls)
    (tmp_path / 'photos').mkdir()
    plan = make_plan(backup, PASSWORD, 'copy', tmp_path / 'photos/library', people='skip', names=['127.0.0.1'],
                     people_port=54343)
    return SimpleNamespace(root=root, installation=installation, plan=plan, calls=calls, database=database)


def stand_ins(monkeypatch, calls):
    from app.backup import admin_restore, library, library_restore, thumbnails, encrypted
    from app.migration import activation, activation_commit, credentials, tls, validation

    def restore_library(backup, key, target, *args, **kwargs):
        calls.append(('restore', kwargs['in_place'], kwargs['media_directory'].name))
        files = target / 'catalog/files'
        (files / 'config/tls').mkdir(parents=True)
        (files / 'data/people').mkdir(parents=True)
        (files / 'data/people-preview.json').write_text(json.dumps({'enabled': True, 'backend': 'native', 'port': 54341}))
        (target / 'catalog/database-target.json').write_text('{}')
        (files / 'config/local.json').write_text(json.dumps({
            'browser_user': 'family', 'browser_password_hash': 'old-hash', 'host': '192.0.2.204', 'port': 4340,
            'video_tools': {'ffmpeg': 'C:\\ffmpeg.exe'}, 'face_detection': {'active_profile': 'gpu'},
            'database': {'dbname': 'familyfoto_recovery_x', 'user': 'familyfoto_recovery_disabled'},
            'destination': {}, 'source': {}, 'tls': {}, 'auth_secret': 'a', 'session_epoch': 'e',
            'recovery': {'state': 'validation-only', 'activation_required': True}}))
        return {'media_files': 318, 'media_bytes': 3315962224, 'media_root': '/photos/library/media-copy/media',
                'thumbnails': 'verified'}

    def role(admin, record, directory, **kwargs):
        calls.append('role')
        directory.mkdir(exist_ok=True)

    def certificate(source, target, names):
        calls.append(('tls', tuple(names)))
        target.mkdir()
        (target / 'server.pem').write_text('restored authority, new leaf')
        (target / 'TLS-REPORT.json').write_text('{}')

    def validate(*args, **kwargs):
        calls.append(('validate', kwargs['fresh_restore']))
        return {'activation_allowed': False, 'services_started': False, 'library_id': 'L', 'restoration_id': 'R'}

    def prepare(backup, key, target, admin, role_directory, tls_directory, output, **kwargs):
        calls.append(('prepare-activation', tuple(kwargs['accepted']), kwargs['source_unavailable'], kwargs['port']))
        output.mkdir()

    def commit(intent, key, admin, **kwargs):
        calls.append(('commit', kwargs['resume']))
        settings = intent.parent / 'library/catalog/files/config/local.json'
        value = json.loads(settings.read_text())
        value.update(database={'dbname': 'familyfoto_recovery_x', 'user': 'familyfoto_app_x', 'password': 'p'},
                     destination={'root': '/photos/library/media-copy/media'},
                     recovery={'state': 'activated', 'activation_required': False})
        settings.write_text(json.dumps(value))
        (intent / 'ACTIVATION-REPORT.json').write_text('{}')

    monkeypatch.setattr(library_restore, 'restore_library', restore_library)
    monkeypatch.setattr(library, 'verify_library_preflight', lambda backup, key: {})
    monkeypatch.setattr(encrypted, 'verify_artifact', lambda path, key: {'size': 1024})
    monkeypatch.setattr(admin_restore, 'discard_unstarted', lambda folder: None)
    monkeypatch.setattr(credentials, 'prepare_recovery_role', role)
    monkeypatch.setattr(tls, 'prepare_recovery_tls', certificate)
    monkeypatch.setattr(validation, 'validate_plain_candidate', validate)
    monkeypatch.setattr(activation, 'prepare_activation', prepare)
    monkeypatch.setattr(activation_commit, 'commit_activation', commit)
    monkeypatch.setattr(activation_commit, 'sealed_record', lambda *args: {})
    monkeypatch.setattr(thumbnails, 'restore_thumbnails',
                        lambda backup, key, target, progress=None: calls.append('thumbnails') or {'restored': 1094})

    def child(self, name, *arguments):
        calls.append(name)
        return SimpleNamespace(returncode=0, stdout='{"state": "done", "rebuilt": 0, "recovered": 0, "failed": 0}\n')
    monkeypatch.setattr(Move, 'child', child)


def names(calls):
    return [call[0] if isinstance(call, tuple) else call for call in calls]


def test_one_run_ends_with_the_library_working_in_this_installation(world):
    move = Move(world.installation)
    move.open(world.plan)
    result = move.run(KEY, confirm=lambda summary: True)
    # Without People installed, the library's saved People are served view-only, which needs the
    # application's own People libraries but no face model.
    assert names(world.calls) == ['restore', 'role', 'tls', 'validate', 'prepare-activation', 'commit', 'thumbnails',
                                  'people-view', 'start', 'thumbnail-repair']
    assert ('restore', False, 'library') in world.calls and ('validate', True) in world.calls
    # The warning stands for the manual review, and only with the old library given up.
    assert ('prepare-activation', ('old-library-stopped',), True, 4342) in world.calls
    # The database ran for the restore and was stopped before FamilyFoto started its own.
    assert world.database == ['up', 'down'] and names(world.calls).index('start') > names(world.calls).index('commit')
    assert result['state'] == 'done' and result['media_files'] == 318
    root = world.root
    settings = json.loads((root / 'config/local.json').read_text())
    assert settings['browser_password_hash'] == 'old-hash' and settings['video_tools']['ffmpeg'] == '/opt/homebrew/bin/ffmpeg'
    assert settings['host'] == '127.0.0.1' and settings['port'] == 4342 and settings['postgres_admin_password'] == 'pw'
    assert settings['database']['user'] == 'familyfoto_app_x' and settings['recovery']['state'] == 'activated'
    assert settings['tls'] == {'certfile': str(root / 'config/tls/server.pem'), 'keyfile': str(root / 'config/tls/server-key.pem')}
    assert settings['backup'] == {'pg_dump': str(Path('/pg/pg_dump'))} and 'face_detection' not in settings
    # The library's data and certificate are the installation's now; the installer's are kept aside.
    assert (root / 'data/people').is_dir() and (root / 'config/tls/server.pem').read_text() == 'restored authority, new leaf'
    # The certificate's report stays with the restore's records: a backup has no rule for it in config/tls.
    assert not (root / 'config/tls/TLS-REPORT.json').exists() and (root / 'restore/TLS-REPORT.json').is_file()
    assert (root / 'restore/installation-data/setup-marker').exists()
    assert (root / 'restore/installation-tls/server.pem').read_text() == 'installer certificate'
    assert json.loads((root / 'data/people-preview.json').read_text())['port'] == 54343
    assert json.loads((root / 'restore/MOVE-IN-REPORT.json').read_text())['state'] == 'done'


def test_until_the_switch_the_installation_refuses_to_start(world):
    from app.config import load_config  # noqa: F401  (the gate's reader)
    move = Move(world.installation)
    move.open(world.plan)
    settings = json.loads((world.root / 'config/local.json').read_text())
    assert settings['recovery'] == GATE


def test_cancelling_the_warning_leaves_a_checked_library_switched_off(world):
    move = Move(world.installation)
    move.open(world.plan)
    asked = []
    result = move.run(KEY, confirm=lambda summary: asked.append(summary) or False)
    assert result['state'] == 'restored-not-switched' and asked == [{'media_files': 318, 'media_bytes': 3315962224}]
    assert 'prepare-activation' not in names(world.calls) and 'start' not in world.calls
    assert json.loads((world.root / 'config/local.json').read_text())['recovery'] == GATE
    # Running it again skips what is finished and comes back to the warning.
    world.calls.clear()
    again = Move(world.installation)
    again.open(None)
    again.run(KEY, confirm=lambda summary: True)
    assert names(world.calls)[:2] == ['prepare-activation', 'commit'] and 'restore' not in names(world.calls)


def test_a_stop_part_way_carries_on_from_that_stage(world, monkeypatch):
    move = Move(world.installation)
    move.open(world.plan)

    def broken():
        raise RuntimeError('Synthetic stop while starting')
    world.installation.start = broken
    with pytest.raises(RuntimeError):
        move.run(KEY, confirm=lambda summary: True)
    world.calls.clear()
    world.installation.start = lambda: world.calls.append('start')
    again = Move(world.installation)
    again.open(None)
    again.run(KEY, confirm=lambda summary: pytest.fail('Already switched on'))
    assert world.calls == ['start', 'thumbnail-repair']


def test_carrying_on_keeps_the_recorded_choices(world):
    Move(world.installation).open(world.plan)
    with pytest.raises(ExplainedFailure, match='other choices \\(media\\)'):
        Move(world.installation).open({'media': 'in-place'})
    assert Move(world.installation).open({'media': 'copy'})['people_port'] == 54343


def test_only_an_empty_installation_is_restored_into(world):
    settings = json.loads((world.root / 'config/local.json').read_text())
    settings['destination'] = {'root': '/Volumes/Photos/library'}
    (world.root / 'config/local.json').write_text(json.dumps(settings))
    with pytest.raises(ExplainedFailure, match='already has a library'):
        Move(world.installation).open(world.plan)
    assert not (world.root / 'restore').exists()


def test_people_is_set_up_after_the_switch_and_before_the_start(world, monkeypatch):
    plan = {**world.plan, 'people': 'install'}

    def child(self, name, *arguments):
        world.calls.append(name)
        if name == 'people-setup':
            assert '--installation' in arguments and arguments[arguments.index('--port') + 1] == '54343'
            Path(arguments[arguments.index('--report') + 1]).write_text(json.dumps({'library_check': {'passed': True}}))
        return SimpleNamespace(returncode=0, stdout='{"state": "done", "rebuilt": 0, "recovered": 0, "failed": 0}\n')
    monkeypatch.setattr(Move, 'child', child)
    move = Move(world.installation)
    move.open(plan)
    result = move.run(KEY, confirm=lambda summary: True)
    order = names(world.calls)
    assert order.index('commit') < order.index('people-setup') < order.index('start')
    assert result['people'] == {'library_check': {'passed': True}}


def test_a_failed_library_check_is_reported_not_fatal(world, monkeypatch):
    def child(self, name, *arguments):
        if name == 'people-setup':
            Path(arguments[arguments.index('--report') + 1]).write_text(
                json.dumps({'library_check': {'passed': False, 'reason': 'vectors-differ'}}))
            return SimpleNamespace(returncode=1, stdout='')
        return SimpleNamespace(returncode=0, stdout='{"state": "done", "rebuilt": 0, "recovered": 0, "failed": 0}\n')
    monkeypatch.setattr(Move, 'child', child)
    move = Move(world.installation)
    move.open({**world.plan, 'people': 'install'})
    assert move.run(KEY, confirm=lambda summary: True)['people']['library_check']['reason'] == 'vectors-differ'


def test_the_plan_refuses_unclear_choices(tmp_path, monkeypatch):
    monkeypatch.setattr(move_in, 'unlock_backup', lambda backup, password: KEY)
    backup = tmp_path / 'backup'
    (backup / 'catalog').mkdir(parents=True)
    with pytest.raises(ExplainedFailure, match='separate from the backup'):
        make_plan(backup, PASSWORD, 'in-place', backup / 'library', people='skip')
    with pytest.raises(ExplainedFailure, match='not a FamilyFoto backup'):
        make_plan(tmp_path, PASSWORD, 'copy', tmp_path / 'x/library', people='skip')
    with pytest.raises(ValueError, match='in-place or copy'):
        make_plan(backup, PASSWORD, 'move', tmp_path / 'library', people='skip')
    assert '127.0.0.1' in move_in.default_names()


def test_a_library_without_people_installs_nothing_for_people(world, monkeypatch):
    from app.backup import library_restore
    restore = library_restore.restore_library

    def without_people(*args, **kwargs):
        report = restore(*args, **kwargs)
        (args[2] / 'catalog/files/data/people-preview.json').unlink()
        return report
    monkeypatch.setattr(library_restore, 'restore_library', without_people)
    move = Move(world.installation)
    move.open(world.plan)
    move.run(KEY, confirm=lambda summary: True)
    assert 'people-view' not in world.calls


def test_the_folders_the_installation_owns_stay_in_its_data(world):
    """On Windows the running database cluster and the setup record live in data."""
    root = world.root
    (root / 'data/postgres').mkdir()
    (root / 'data/postgres/PG_VERSION').write_text('17')
    (root / 'data/setup').mkdir()
    (root / 'data/setup/install.json').write_text('{}')
    world.installation.keep_data = ('postgres', 'setup')
    move = Move(world.installation)
    move.open(world.plan)
    move.run(KEY, confirm=lambda summary: True)
    assert (root / 'data/postgres/PG_VERSION').read_text() == '17' and (root / 'data/setup/install.json').exists()
    assert (root / 'data/people').is_dir() and (root / 'data/people-preview.json').is_file()
    assert (root / 'restore/installation-data/setup-marker').exists()
    assert not (root / 'restore/installation-data/postgres').exists()
    assert not (root / 'restore/library/catalog/files/data').exists()


def test_a_backup_holding_a_folder_the_installation_owns_is_refused(world, monkeypatch):
    from app.backup import library_restore
    restore = library_restore.restore_library

    def with_postgres(*args, **kwargs):
        report = restore(*args, **kwargs)
        (args[2] / 'catalog/files/data/postgres').mkdir()
        return report
    monkeypatch.setattr(library_restore, 'restore_library', with_postgres)
    world.installation.keep_data = ('postgres',)
    move = Move(world.installation)
    move.open(world.plan)
    with pytest.raises(ExplainedFailure, match='holds postgres'):
        move.run(KEY, confirm=lambda summary: True)
    # Refused before anything moved.
    assert (world.root / 'data/setup-marker').exists() and not (world.root / 'restore/installation-data').exists()


def test_a_stop_while_moving_the_data_carries_on(world, monkeypatch):
    move = Move(world.installation)
    move.open(world.plan)
    real, moves = move_in.os.rename, []

    def rename(source, target):
        moves.append(Path(source).name)
        if Path(source).name == 'people-preview.json' and moves.count('people-preview.json') == 1:
            raise OSError('Synthetic stop')
        real(source, target)
    monkeypatch.setattr(move_in.os, 'rename', rename)
    with pytest.raises(OSError):
        move.run(KEY, confirm=lambda summary: True)
    again = Move(world.installation)
    again.open(None)
    assert again.run(KEY, confirm=lambda summary: True)['state'] == 'done'
    root = world.root
    assert (root / 'data/people').is_dir() and (root / 'data/people-preview.json').is_file()
    assert (root / 'restore/installation-data/setup-marker').exists() and not (root / 'data/setup-marker').exists()


def test_what_a_restore_leaves_in_config_is_all_backed_up(world):
    """Every file the move puts in config/ has a backup rule, so the first backup's check passes."""
    from app.backup.policy import classify_file
    move = Move(world.installation)
    move.open(world.plan)
    move.run(KEY, confirm=lambda summary: True)
    config = world.root / 'config'
    files = [path.relative_to(config).as_posix() for path in config.rglob('*') if path.is_file()]
    assert files
    for name in files:
        classify_file('config', name)  # Raises UnclassifiedState for a file a backup has no rule for.


# The password is checked before anything is recorded, and asked again on every run.

def sealed_backup(tmp_path, password=PASSWORD):
    """A backup folder with a real password context and sealed index, and its key."""
    import io
    from app.backup import passwords
    from app.backup.encrypted import seal_stream
    backup = tmp_path / 'backup'
    (backup / 'catalog').mkdir(parents=True)
    key = passwords.create_context(backup, passwords.BackupPassword(password))
    seal_stream(io.BytesIO(b'{"index": true}'), backup / 'library', key, estimated_bytes=64)
    return backup, key


def test_the_right_password_opens_the_backup(tmp_path):
    backup, key = sealed_backup(tmp_path)
    assert move_in.unlock_backup(backup, PASSWORD) == key


def test_a_wrong_password_is_refused_in_words(tmp_path):
    backup, _ = sealed_backup(tmp_path)
    with pytest.raises(move_in.WrongBackupPassword, match='incorrect.*as it was when this backup was made'):
        move_in.unlock_backup(backup, 'not the password at all')


def test_a_damaged_index_is_not_called_a_wrong_password(tmp_path):
    backup, _ = sealed_backup(tmp_path)
    (backup / 'library/00000000.fernet').write_bytes(b'damaged')
    with pytest.raises(ExplainedFailure, match='opens with this password, but its library index is damaged'):
        move_in.unlock_backup(backup, PASSWORD)


def test_a_key_file_backup_is_not_supported(tmp_path):
    backup, _ = sealed_backup(tmp_path)
    (backup / 'password.json').unlink()
    with pytest.raises(ExplainedFailure, match='not supported'):
        move_in.unlock_backup(backup, PASSWORD)


def test_the_plan_refuses_a_wrong_password_and_records_no_password(tmp_path):
    backup, key = sealed_backup(tmp_path)
    (tmp_path / 'photos').mkdir()
    with pytest.raises(move_in.WrongBackupPassword):
        make_plan(backup, 'not the password at all', 'copy', tmp_path / 'photos/library', people='skip')
    plan = make_plan(backup, PASSWORD, 'copy', tmp_path / 'photos/library', people='skip')
    assert plan['backup_id'] == key.backup_id and PASSWORD not in json.dumps(plan) and 'key_file' not in plan


def test_carrying_on_needs_the_password_of_the_recorded_backup(world, tmp_path):
    other_backup, other = sealed_backup(tmp_path / 'other')
    move = Move(world.installation)
    move.open(world.plan)
    with pytest.raises(ExplainedFailure, match='another backup'):
        move.run(other, confirm=lambda summary: True)
    assert not move.done('restored')


def test_the_switch_stops_if_the_restored_settings_do_not_sign_in_with_the_password(world, monkeypatch):
    move = Move(world.installation)
    move.open(world.plan)
    monkeypatch.setattr(move_in, 'restored_password_matches', lambda target, password: password == PASSWORD)
    with pytest.raises(ExplainedFailure, match='may be damaged or replaced'):
        move.run(KEY, confirm=lambda summary: pytest.fail('asked to switch'), password='a different password')
    assert move.done('checked') and not move.done('switched')
    assert move.run(KEY, confirm=lambda summary: True, password=PASSWORD)['state'] == 'done'


def test_restored_settings_are_checked_against_the_password(tmp_path):
    from app.auth import password_hash
    settings = tmp_path / 'catalog/files/config'
    settings.mkdir(parents=True)
    (settings / 'local.json').write_text(json.dumps({'browser_password_hash': password_hash(PASSWORD)}))
    assert move_in.restored_password_matches(tmp_path, PASSWORD)
    assert not move_in.restored_password_matches(tmp_path, PASSWORD + ' ')
    assert not move_in.restored_password_matches(tmp_path / 'missing', PASSWORD)


def test_restored_access_failure_retries_before_people_and_start(world):
    move = Move(world.installation)
    move.open(world.plan)

    def denied():
        raise PermissionError('job ACL')

    world.installation.restore_access = denied
    with pytest.raises(PermissionError):
        move.run(KEY, confirm=lambda summary: True)
    assert move.done('installed') and not move.done('restore-access')
    assert 'people-view' not in world.calls and 'start' not in world.calls
    world.calls.clear()
    world.installation.restore_access = lambda: world.calls.append('access')
    move.run(KEY, confirm=lambda summary: pytest.fail('already switched'))
    assert world.calls == ['access', 'people-view', 'start', 'thumbnail-repair']
    # An installation restored by the old code can run just the missing step once.
    del move.journal['stages']['restore-access']
    world.calls.clear()
    for _ in range(2):
        move.run(KEY, confirm=lambda summary: pytest.fail('already switched'))
    assert world.calls == ['access']


def test_completed_windows_entry_point_repairs_with_backup_offline(world, monkeypatch, capsys):
    from installer.windows import restore

    move = Move(world.installation)
    move.open(world.plan)
    move.run(KEY, confirm=lambda summary: True)
    backup = Path(world.plan['backup'])
    backup.rename(backup.with_name('disconnected'))
    world.calls.clear()
    world.installation.restore_access = lambda: world.calls.append('verify account and repair')
    monkeypatch.setattr(restore, 'installation', lambda port: world.installation)
    monkeypatch.setattr(restore, 'working_password', lambda *args: pytest.fail('backup offline'))
    for _ in range(2):
        assert restore.main(['--unattended', '--people-port', str(world.installation.people_port)]) == 0
    assert world.calls == ['verify account and repair']
    assert 'desktop access is ready' in capsys.readouterr().out


def test_new_access_version_retries_completed_restore_once(world):
    move = Move(world.installation)
    move.open(world.plan)
    world.installation.restore_access = lambda: world.calls.append('old-access')
    move.run(KEY, confirm=lambda summary: True)
    assert move.done('restore-access')
    world.calls.clear()
    world.installation.restore_access_stage = 'restore-access-media-v2'
    def fail():
        raise PermissionError('media ACL')
    world.installation.restore_access = fail
    with pytest.raises(PermissionError):
        move.run(KEY, confirm=lambda summary: pytest.fail('already switched'))
    assert not move.done('restore-access-media-v2')
    world.installation.restore_access = lambda: world.calls.append('media-access')
    for _ in range(2):
        move.run(KEY, confirm=lambda summary: pytest.fail('already switched'))
    assert world.calls == ['media-access']


def test_versioned_access_checkpoint_uses_matching_public_progress_events(world):
    events = []
    move = Move(world.installation, progress=events.append)
    move.open(world.plan)
    world.installation.restore_access_stage = 'restore-access-media-v2'
    world.installation.restore_access = lambda: None
    move.restore_access()
    assert events == [
        {'stage': 'restore-access', 'state': 'running'},
        {'stage': 'restore-access', 'state': 'done'},
    ]
    saved = json.loads(move.journal_path.read_text())
    assert 'restore-access-media-v2' in saved['stages']
    assert 'restore-access' not in saved['stages']
    assert 'progress_stage' not in saved['results']
    events.clear()
    move.restore_access()
    assert events == []


def test_once_the_backup_is_no_longer_read_a_restore_carries_on_without_it_or_its_password(world, tmp_path):
    """After the thumbnails stage everything left works from the local copy: the backup disk may be
    away, and no password is needed (reviewer scenario: People/startup fails after `installed`)."""
    move = Move(world.installation)
    move.open(world.plan)
    assert move.needs_backup()

    def broken(plan):
        raise RuntimeError('Synthetic People failure')
    move.set_up_people = broken
    world.plan['people'] = 'install'
    move.journal['plan']['people'] = 'install'
    with pytest.raises(RuntimeError):
        move.run(KEY, confirm=lambda summary: True)
    assert move.done('installed') and not move.done('people') and not move.needs_backup()
    again = Move(world.installation)
    again.open(None)
    again.set_up_people = lambda plan: again.finish('people', people={'library_check': {'passed': True}})
    # No key at all, and the backup folder gone.
    import shutil
    shutil.rmtree(world.plan['backup'])
    assert again.run(None, confirm=lambda summary: pytest.fail('Already switched on'))['state'] == 'done'


@pytest.mark.parametrize('people, expected', [
    ('not installed', 'People is not installed'),
    ({'library_check': {'passed': True}}, 'People is set up, and editable.'),
    ({'library_check': 'no-stored-faces'}, 'no saved faces yet'),
    ({'library_check': {'passed': False, 'reason': 'unmatched'}}, 'did not pass (unmatched)'),
    ({'library_check': 'skipped'}, 'runs at the first Discover people'),
    ({'library_check': None}, 'runs at the first Discover people'),
])
def test_people_summary_covers_every_people_result(people, expected):
    # A restore with People on but no faces records 'no-stored-faces' (a string), not a check
    # record; the summary must not assume a record (it crashed restore.py after a finished restore).
    assert expected in move_in.people_summary(people)


def test_both_restore_scripts_use_the_shared_people_summary():
    root = Path(__file__).resolve().parents[1]
    for script in ('installer/macos/restore.py', 'installer/windows/restore.py'):
        text = (root / script).read_text(encoding='utf-8')
        assert 'people_summary(result.get(\'people\'))' in text
        assert "check.get('passed')" not in text
