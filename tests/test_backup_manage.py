import json
from pathlib import Path

import pytest
from types import SimpleNamespace
from app.auth import password_hash
from app.backup import manage, passwords
from app.backup.diagnostics import ExplainedFailure
from tests.backup_keys import ADMIN_PASSWORD, backup_key


@pytest.fixture(autouse=True)
def password_given(monkeypatch):
    """Most of these check what a command passes on: the password and its key are stubbed.
    test_keys_* below run the real choice of folder and the real checks."""
    keys = []

    def key_for(args, text):
        assert text == ADMIN_PASSWORD
        keys.append(backup_key())
        return keys[-1]
    monkeypatch.setattr(manage.passwords, 'from_arguments', lambda args: ADMIN_PASSWORD)
    monkeypatch.setattr(manage, 'key_for', key_for)
    return keys


def _write_private_key(path):
    """No key files any more; kept so each test reads as before."""


def run_verify(tmp_path, capsys, key=None):
    assert manage.main(['verify-library', '--directory', str(tmp_path / 'backup'), '--backup-password-stdin']) == 1
    return capsys.readouterr().out


def test_an_explained_failure_shows_only_its_own_message(tmp_path, monkeypatch, capsys):
    key = tmp_path / 'key'
    _write_private_key(key)

    def fail(*args, **kwargs):
        raise ExplainedFailure('Start again in a new folder.')
    monkeypatch.setattr(manage, 'verify_library', fail)
    report = json.loads(run_verify(tmp_path, capsys, key))
    assert report == {'state': 'failed', 'reason': 'ValueError', 'diagnostic_categories': [],
                      'message': 'Start again in a new folder.'}


def test_every_other_failure_still_shows_no_details(tmp_path, monkeypatch, capsys):
    key = tmp_path / 'key'
    _write_private_key(key)

    def fail(*args, **kwargs):
        raise ValueError('password=secret in a tool message')
    monkeypatch.setattr(manage, 'verify_library', fail)
    assert 'secret' not in run_verify(tmp_path, capsys, key)


def test_activation_preparation_cli_passes_attestations_without_enabling_anything(tmp_path, monkeypatch):
    key = tmp_path / 'key'
    _write_private_key(key)
    calls = []

    def prepare(*args, **kwargs):
        assert set(kwargs['accepted']) == manage.ACCEPTANCE_CHECKS
        assert kwargs['source_unavailable'] is True
        assert kwargs['hostname'] == 'recovered.local' and kwargs['port'] == 4340
        assert args[2] == tmp_path / 'target' and args[6] == tmp_path / 'intent'
        calls.append(True)
        return {'activation_allowed': False}

    monkeypatch.setattr(manage, 'prepare_activation', prepare)
    argv = ['prepare-activation', '--directory', str(tmp_path / 'backup'), '--backup-password-stdin',
            '--target-directory', str(tmp_path / 'target'), '--role-directory', str(tmp_path / 'role'),
            '--tls-directory', str(tmp_path / 'tls'), '--expected-cluster-directory', str(tmp_path / 'cluster'),
            '--port', '55489', '--username', 'fixture', '--intent-directory', str(tmp_path / 'intent'),
            '--hostname', 'recovered.local', '--listen-port', '4340', '--source-unavailable']
    for check in sorted(manage.ACCEPTANCE_CHECKS):
        argv.extend(['--accepted-check', check])
    assert manage.main(argv) == 0
    assert calls == [True]


@pytest.mark.parametrize('action', ['restore', 'restore-library'])
def test_catalog_and_plain_library_cli_forward_explicit_resume(tmp_path, monkeypatch, action):
    key = tmp_path / 'key'
    _write_private_key(key)
    calls = []

    def restore(*args, **kwargs):
        assert kwargs['resume'] is True
        calls.append(action)
        return {'activation_allowed': False}

    monkeypatch.setattr(manage, 'restore_catalog' if action == 'restore' else 'restore_library', restore)
    assert manage.main([action, '--directory', str(tmp_path / 'backup'), '--backup-password-stdin',
        '--port', '55489', '--username', 'fixture', '--database-budget-bytes', '1024',
        '--pg-restore', 'fixture-restore', '--target-directory', str(tmp_path / 'target'),
        '--expected-cluster-directory', str(tmp_path / 'cluster'), '--resume']) == 0
    assert calls == [action]


@pytest.mark.parametrize('flag', [[], ['--in-place']])
def test_library_restore_cli_forwards_in_place_only_when_asked(tmp_path, monkeypatch, flag):
    key = tmp_path / 'key'
    _write_private_key(key)
    seen = []
    monkeypatch.setattr(manage, 'restore_library',
                        lambda *args, **kwargs: seen.append(kwargs['in_place']) or {'activation_allowed': False})
    assert manage.main(['restore-library', '--directory', str(tmp_path / 'backup'), '--backup-password-stdin',
        '--port', '55489', '--username', 'fixture', '--database-budget-bytes', '1024',
        '--pg-restore', 'fixture-restore', '--target-directory', str(tmp_path / 'target'),
        '--expected-cluster-directory', str(tmp_path / 'cluster'), *flag]) == 0
    assert seen == [bool(flag)]


def test_library_restore_cli_forwards_a_split_media_folder(tmp_path, monkeypatch):
    key = tmp_path / 'key'
    _write_private_key(key)
    seen = []
    monkeypatch.setattr(manage, 'restore_library', lambda *args, **kwargs: seen.append(kwargs) or {'activation_allowed': False})
    common = ['restore-library', '--directory', str(tmp_path / 'backup'), '--backup-password-stdin',
              '--port', '55489', '--username', 'fixture', '--database-budget-bytes', '1024',
              '--pg-restore', 'fixture-restore', '--target-directory', str(tmp_path / 'target'),
              '--expected-cluster-directory', str(tmp_path / 'cluster'), '--in-place']
    assert manage.main(common) == 0
    assert manage.main([*common, '--media-directory', str(tmp_path / 'ssd/library'), '--allow-unprotected-media']) == 0
    assert 'media_directory' not in seen[0] and 'allow_unprotected_media' not in seen[0]
    assert seen[1]['media_directory'] == tmp_path / 'ssd/library' and seen[1]['allow_unprotected_media'] is True


def test_catalog_restore_has_no_in_place_option(tmp_path):
    key = tmp_path / 'key'
    _write_private_key(key)
    with pytest.raises(SystemExit):
        manage.main(['restore', '--directory', str(tmp_path / 'backup'), '--backup-password-stdin',
            '--port', '55489', '--username', 'fixture', '--database-budget-bytes', '1024',
            '--pg-restore', 'fixture-restore', '--target-directory', str(tmp_path / 'target'),
            '--expected-cluster-directory', str(tmp_path / 'cluster'), '--in-place'])


@pytest.mark.parametrize('action', ['capture-encrypted', 'restore-encrypted'])
def test_encrypted_commands_dispatch_explicit_offline_inputs(tmp_path, capsys, monkeypatch, action):
    key = tmp_path / 'key'
    _write_private_key(key)
    engine = object()
    monkeypatch.setattr(manage, 'engine_for', lambda args: engine)
    calls = []

    def capture(endpoint, dump, config, data, media, directory, supplied_key, tool, repository, **kwargs):
        assert endpoint.dbname == 'fixture'
        assert tool is engine and repository == 'a' * 64
        assert isinstance(supplied_key, passwords.BackupPassword if action == 'restore-encrypted' else passwords.BackupKey) and kwargs['offline_confirmed']
        calls.append(action)
        return {'activation_allowed': False}

    def restore(tool, repository, snapshot, supplied_key, target, endpoint, executable, **kwargs):
        assert tool is engine and repository == 'a' * 64 and snapshot == 'b' * 64
        assert endpoint.dbname == 'postgres' and isinstance(supplied_key, passwords.BackupPassword if action == 'restore-encrypted' else passwords.BackupKey)
        assert kwargs['metadata_budget'] == 2048
        assert kwargs['resume'] is True
        calls.append(action)
        return {'activation_allowed': False}

    monkeypatch.setattr(manage, 'capture_encrypted_library', capture)
    monkeypatch.setattr(manage, 'restore_encrypted_library', restore)
    args = [action, '--backup-password-stdin', '--port', '55489', '--username', 'fixture',
            '--database-budget-bytes', '1024', '--restic', 'fixture-restic',
            '--restic-sha256', 'c' * 64, '--repository', str(tmp_path / 'repository'),
            '--repository-password-file', str(tmp_path / 'password'), '--repository-id', 'a' * 64]
    if action == 'capture-encrypted':
        args += ['--directory', str(tmp_path / 'working'), '--dbname', 'fixture', '--pg-dump', 'fixture-dump',
                 '--config-root', str(tmp_path / 'config'), '--data-root', str(tmp_path / 'data'),
                 '--media-root', str(tmp_path / 'media'), '--offline']
    else:
        args += ['--target-directory', str(tmp_path / 'restored'), '--pg-restore', 'fixture-restore',
                 '--expected-cluster-directory', str(tmp_path / 'cluster'), '--metadata-snapshot', 'b' * 64,
                 '--metadata-budget-bytes', '2048', '--resume']
    assert manage.main(args) == 0
    assert calls == [action]
    assert json.loads(capsys.readouterr().out)['activation_allowed'] is False


def test_capture_requires_offline_and_redacts_errors(tmp_path, capsys, monkeypatch):
    key = tmp_path / 'key'
    _write_private_key(key)
    args = ['capture', '--directory', str(tmp_path / 'backup'), '--backup-password-stdin',
            '--port', '55489', '--username', 'fixture', '--dbname', 'fixture',
            '--database-budget-bytes', '1024', '--config-root', str(tmp_path / 'config'),
            '--data-root', str(tmp_path / 'data'), '--pg-dump', 'missing-test-tool']
    assert manage.main(args) == 1
    assert json.loads(capsys.readouterr().out)['reason'] == 'maintenance'

    def fail(*args, **kwargs):
        raise RuntimeError('SECRET database error payload')
    monkeypatch.setattr(manage, 'capture_catalog', fail)
    assert manage.main([*args, '--offline']) == 1
    assert 'SECRET' not in capsys.readouterr().out


def test_media_commands_refuse_without_offline_confirmation(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(manage, 'maintenance', lambda *args: (_ for _ in ()).throw(AssertionError('No gate yet')))
    for action in ('media-inventory', 'media-copy'):
        args = [action, '--source-root', str(tmp_path / 'source'), '--data-root', str(tmp_path / 'data'),
                '--directory', str(tmp_path / 'target')]
        if action == 'media-copy':
            args += ['--inventory', str(tmp_path / 'inventory')]
        assert manage.main(args) == 1
        assert json.loads(capsys.readouterr().out)['reason'] == 'ValueError'


def test_media_commands_take_supported_maintenance_gate(tmp_path, capsys, monkeypatch):
    from contextlib import contextmanager
    calls = []
    @contextmanager
    def gate(root, operation):
        assert operation == 'backup'
        calls.append(operation)
        yield
    monkeypatch.setattr(manage, 'maintenance', gate)
    monkeypatch.setattr(manage, 'capture_media_inventory', lambda *args, **kwargs: {'files': 0})
    monkeypatch.setattr(manage, 'copy_media_tree', lambda *args, **kwargs: {'files': 0})
    for action in ('media-inventory', 'media-copy'):
        args = [action, '--source-root', str(tmp_path / 'source'), '--data-root', str(tmp_path / 'data'),
                '--directory', str(tmp_path / 'target'), '--offline']
        if action == 'media-copy':
            args += ['--inventory', str(tmp_path / 'inventory')]
        assert manage.main(args) == 0
        assert json.loads(capsys.readouterr().out)['state'] == 'complete'
    assert calls == ['backup', 'backup']


def test_receipt_recovery_command_never_claims_activation(tmp_path, capsys, monkeypatch):
    key = tmp_path / 'key'
    _write_private_key(key)
    calls = []

    def recover(data, directory, supplied_key):
        calls.append((data, directory))
        assert isinstance(supplied_key, passwords.BackupKey)
        return {'capture_id': 'a' * 32}

    monkeypatch.setattr(manage, 'recover_retirement', recover)
    assert manage.main(['recover-retirement', '--directory', str(tmp_path / 'backup'),
                        '--backup-password-stdin', '--data-root', str(tmp_path / 'data')]) == 0
    assert calls == [(tmp_path / 'data', tmp_path / 'backup')]
    assert json.loads(capsys.readouterr().out) == {
        'state': 'receipt-verified', 'capture_id': 'a' * 32,
        'source_retired': True, 'activation_allowed': False}


def test_upgrade_command_requires_offline_and_media_scope_confirmation(tmp_path, capsys):
    key = tmp_path / 'key'
    _write_private_key(key)
    args = ['upgrade-catalog', '--directory', str(tmp_path / 'backup'), '--backup-password-stdin',
            '--port', '55489', '--username', 'fixture', '--dbname', 'fixture',
            '--database-budget-bytes', '1048576', '--config-root', str(tmp_path / 'config'),
            '--data-root', str(tmp_path / 'data'), '--pg-dump', 'missing-fixture-tool',
            '--migrations-directory', str(tmp_path / 'sql')]
    for flags in ([], ['--offline'], ['--catalog-only-upgrade']):
        assert manage.main(args + flags) == 1
        assert json.loads(capsys.readouterr().out)['reason'] == 'maintenance'


def test_disk_cutover_commands_require_offline_before_reading_paths(tmp_path, capsys):
    key = tmp_path / 'key'
    _write_private_key(key)
    common = ['--directory', str(tmp_path / 'job'), '--backup-password-stdin',
              '--port', '55489', '--username', 'fixture', '--dbname', 'fixture',
              '--config-root', str(tmp_path / 'config'), '--data-root', str(tmp_path / 'data')]
    assert manage.main(['recover-disk-move', *common, '--rollback']) == 1
    assert json.loads(capsys.readouterr().out)['reason'] == 'maintenance'
    assert manage.main(['commit-disk-move', *common, '--pg-dump', 'missing-fixture-tool',
                        '--database-budget-bytes', '1048576', '--backup-directory', str(tmp_path / 'backup')]) == 1
    assert json.loads(capsys.readouterr().out)['reason'] == 'maintenance'


def test_tls_preparation_command_does_not_require_or_emit_recovery_secrets(tmp_path, capsys, monkeypatch):
    def prepare(source, target, names):
        assert source == tmp_path / 'source' and target == tmp_path / 'target'
        assert names == ['new-host', '127.0.0.1']
        return {'state': 'prepared', 'activation_allowed': False}
    monkeypatch.setattr(manage, 'prepare_recovery_tls', prepare)
    assert manage.main(['prepare-recovery-tls', '--source-tls', str(tmp_path / 'source'),
                        '--directory', str(tmp_path / 'target'), '--name', 'new-host', '--name', '127.0.0.1']) == 0
    assert json.loads(capsys.readouterr().out) == {'state': 'prepared', 'activation_allowed': False}


def test_role_preparation_command_passes_owned_record_not_live_config(tmp_path, capsys, monkeypatch):
    record = tmp_path / 'database-target.json'
    record.write_text(json.dumps({'fixture': 'owned-record'}))
    def prepare(admin, supplied, directory, **kwargs):
        assert admin.dbname == 'postgres' and admin.port == 55489
        assert supplied == {'fixture': 'owned-record'} and directory == tmp_path / 'credentials'
        assert kwargs['resume'] is True
        return {'state': 'staged-no-login', 'activation_allowed': False}
    monkeypatch.setattr(manage, 'prepare_recovery_role', prepare)
    assert manage.main(['prepare-recovery-role', '--database-record', str(record),
        '--directory', str(tmp_path / 'credentials'), '--port', '55489', '--username', 'fixture',
        '--expected-cluster-directory', str(tmp_path / 'cluster'), '--resume']) == 0
    assert json.loads(capsys.readouterr().out) == {'state': 'staged-no-login', 'activation_allowed': False}


def test_candidate_command_only_validates_explicit_recovery_components(tmp_path, capsys, monkeypatch):
    key = tmp_path / 'key'
    _write_private_key(key)
    def validate(backup, supplied_key, target, admin, role, tls, **kwargs):
        assert (backup, target, role, tls) == tuple(tmp_path / name for name in ('backup', 'target', 'role', 'tls'))
        assert isinstance(supplied_key, passwords.BackupKey) and admin.dbname == 'postgres'
        assert kwargs['expected_cluster_directory'] == tmp_path / 'cluster'
        return {'state': 'candidate-validated', 'activation_allowed': False}
    monkeypatch.setattr(manage, 'validate_plain_candidate', validate)
    assert manage.main(['validate-recovery', '--directory', str(tmp_path / 'backup'),
        '--target-directory', str(tmp_path / 'target'), '--role-directory', str(tmp_path / 'role'),
        '--tls-directory', str(tmp_path / 'tls'), '--backup-password-stdin', '--port', '55489',
        '--username', 'fixture', '--expected-cluster-directory', str(tmp_path / 'cluster')]) == 0
    assert json.loads(capsys.readouterr().out) == {'state': 'candidate-validated', 'activation_allowed': False}


def test_preview_cleanup_command_binds_explicit_owned_record(tmp_path, capsys, monkeypatch):
    record = tmp_path / 'database-target.json'
    record.write_text(json.dumps({'fixture': 'owned-record'}))
    def close(admin, supplied, directory, **kwargs):
        assert admin.dbname == 'postgres' and admin.port == 55489
        assert supplied == {'fixture': 'owned-record'} and directory == tmp_path / 'credentials'
        assert kwargs['expected_cluster_directory'] == tmp_path / 'cluster'
        return {'state': 'closed', 'activation_allowed': False, 'services_started': False}
    monkeypatch.setattr(manage, 'close_preview_access', close)
    assert manage.main(['close-recovery-preview', '--database-record', str(record),
        '--directory', str(tmp_path / 'credentials'), '--port', '55489', '--username', 'fixture',
        '--expected-cluster-directory', str(tmp_path / 'cluster')]) == 0
    assert json.loads(capsys.readouterr().out)['state'] == 'closed'


def test_preview_command_uses_distinct_database_and_https_ports(tmp_path, capsys, monkeypatch):
    key = tmp_path / 'key'
    _write_private_key(key)
    def preview(backup, supplied_key, target, admin, role, tls, scratch, **kwargs):
        assert isinstance(supplied_key, passwords.BackupKey) and admin.port == 55489
        assert kwargs['port'] == 4439 and scratch == tmp_path / 'scratch'
        return {'state': 'preview-closed', 'activation_allowed': False}
    monkeypatch.setattr(manage, 'preview_library', preview)
    assert manage.main(['preview-recovery', '--directory', str(tmp_path / 'backup'),
        '--target-directory', str(tmp_path / 'target'), '--role-directory', str(tmp_path / 'role'),
        '--tls-directory', str(tmp_path / 'tls'), '--backup-password-stdin', '--port', '55489',
        '--username', 'fixture', '--expected-cluster-directory', str(tmp_path / 'cluster'),
        '--scratch-directory', str(tmp_path / 'scratch'), '--listen-port', '4439']) == 0
    assert json.loads(capsys.readouterr().out)['activation_allowed'] is False


# The real choice of folder for each command's key, and the administrator-password check.

def context(folder, password=ADMIN_PASSWORD):
    folder.mkdir(parents=True, exist_ok=True)
    return passwords.create_context(folder, passwords.BackupPassword(password))


def args_for(action, tmp_path, **extra):
    return SimpleNamespace(action=action, directory=tmp_path / 'backup', resume=False, **extra)


def test_keys_come_from_the_folder_each_command_reads(tmp_path, monkeypatch):
    monkeypatch.undo()
    backup = context(tmp_path / 'backup')
    intent = context(tmp_path / 'intent')
    metadata = context(tmp_path / 'backup' / 'metadata')
    for action in ('verify', 'verify-library', 'restore', 'restore-library', 'restore-thumbnails', 'validate-recovery',
                   'preview-recovery', 'prepare-activation', 'recover-retirement', 'recover-disk-move'):
        assert manage.key_for(args_for(action, tmp_path), ADMIN_PASSWORD) == backup
    assert manage.key_for(args_for('commit-activation', tmp_path, intent_directory=tmp_path / 'intent'),
                          ADMIN_PASSWORD) == intent
    assert manage.key_for(args_for('recover-encrypted-publication', tmp_path), ADMIN_PASSWORD) == metadata
    assert manage.key_for(SimpleNamespace(action='capture-library', directory=tmp_path / 'backup', resume=True),
                          ADMIN_PASSWORD) == backup
    with pytest.raises(passwords.WrongPassword):
        manage.key_for(args_for('verify', tmp_path), 'not the password at all')


def test_a_new_backup_needs_the_administrator_password(tmp_path, monkeypatch, capsys):
    monkeypatch.undo()
    config = tmp_path / 'config'
    config.mkdir()
    (config / 'local.json').write_text(json.dumps({'browser_user': 'family', 'browser_password_hash': password_hash(ADMIN_PASSWORD)}))
    args = SimpleNamespace(action='capture-library', directory=tmp_path / 'backup', resume=False, config_root=config)
    key = manage.key_for(args, ADMIN_PASSWORD)
    assert isinstance(key, passwords.BackupKey) and not (tmp_path / 'backup').exists()
    with pytest.raises(passwords.NotAdministratorPassword):
        manage.key_for(args, 'another password entirely')
    # A job folder an earlier attempt left its context in is reused, not replaced.
    job = context(tmp_path / 'job')
    assert manage.key_for(SimpleNamespace(action='commit-disk-move', directory=tmp_path / 'job', config_root=config),
                          ADMIN_PASSWORD) == job


def test_a_wrong_password_is_reported_by_its_reason(tmp_path, monkeypatch, capsys):
    monkeypatch.undo()
    context(tmp_path / 'backup')
    monkeypatch.setattr(manage.passwords, 'from_arguments', lambda args: 'not the password at all')
    report = json.loads(run_verify(tmp_path, capsys))
    assert report['reason'] == 'wrong-password' and 'incorrect' in report['message']
    assert 'not the password' not in json.dumps(report)


def test_there_is_no_key_file_option_or_keygen():
    with pytest.raises(SystemExit):
        manage.parser().parse_args(['keygen', '--directory', 'x'])
    with pytest.raises(SystemExit):
        manage.parser().parse_args(['verify', '--directory', 'x', '--key-file', 'y'])
