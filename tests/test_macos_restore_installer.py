"""The Mac installer's People question, its restore entry, and the switch-warning acceptance."""
import io
import json
from pathlib import Path
import shlex
from types import SimpleNamespace

import pytest

from app.migration.activation import ACCEPTANCE_CHECKS, SWITCH_WARNING, acceptance_checks
from installer.macos import manage, workflow


def flags(**values):
    return SimpleNamespace(**{'with_people': False, 'without_people': False, 'research_only': False, **values})


@pytest.mark.parametrize('values, expected', [({'with_people': True, 'research_only': True}, 'install'),
                                              ({'without_people': True}, 'skip')])
def test_a_flag_decides_without_asking(values, expected, monkeypatch):
    monkeypatch.setattr(workflow, 'interactive', lambda: True)
    assert workflow.people_choice(flags(**values), ask=lambda prompt: pytest.fail('asked')) == expected


@pytest.mark.parametrize('answers, expected', [(['yes'], 'install'), (['No'], 'skip'), (['maybe', 'y'], 'install')])
def test_the_person_is_asked_after_seeing_the_terms(answers, expected, monkeypatch, capsys):
    monkeypatch.setattr(workflow, 'interactive', lambda: True)
    queue = list(answers)
    assert workflow.people_choice(flags(), ask=lambda prompt: queue.pop(0)) == expected
    assert 'non-commercial research use only' in capsys.readouterr().out


def test_three_unclear_answers_stop_the_install(monkeypatch):
    monkeypatch.setattr(workflow, 'interactive', lambda: True)
    with pytest.raises(SystemExit, match='answer yes or no'):
        workflow.people_choice(flags(), ask=lambda prompt: 'later')


def test_an_unattended_install_must_choose(monkeypatch):
    monkeypatch.setattr(workflow, 'interactive', lambda: False)
    with pytest.raises(SystemExit, match='--without-people'):
        workflow.people_choice(flags())


@pytest.mark.parametrize('values, message', [({'with_people': True, 'without_people': True, 'research_only': True}, 'not both'),
                                             ({'research_only': True}, 'goes with --with-people'),
                                             ({'with_people': True}, 'research-only')])
def test_contradicting_flags_are_refused(values, message):
    with pytest.raises(SystemExit, match=message):
        workflow.people_choice(flags(**values))


def test_the_same_terms_as_the_windows_installer():
    from pathlib import Path
    windows = (Path(__file__).resolve().parents[1] / 'installer/windows/setup-common.ps1').read_text(encoding='utf-8')
    if '$PeopleTermsText' not in windows:
        pytest.skip('The Windows question is not on this branch yet')
    assert workflow.PEOPLE_TERMS in windows.replace('\r\n', '\n')


def test_an_unattended_restore_needs_every_choice(monkeypatch):
    monkeypatch.setattr(workflow, 'interactive', lambda: False)
    args = SimpleNamespace(backup=None, key_file=None, media='copy', media_folder=None)
    with pytest.raises(SystemExit, match='--backup'):
        workflow.ask_restore(args)


def test_the_restore_command_carries_the_choices_and_never_the_key():
    args = SimpleNamespace(key_file='/keys/recovery.key', backup='/Volumes/B/backup', media='in-place',
                           media_folder='/Volumes/B/FamilyFoto Library', people_port=54341, tls_name=['mac.local'],
                           allow_unprotected_media=True, old_library_stopped=False)
    command = workflow.restore_command(args, 'install')
    assert '--key-file' not in command and not any('password' in value for value in command)
    assert command[command.index('--media-folder') + 1] == '/Volumes/B/FamilyFoto Library'
    assert '--allow-unprotected-media' in command and '--old-library-stopped' not in command
    assert command[command.index('--people') + 1] == 'install'


def test_the_switch_warning_is_its_own_acceptance_never_offered_on_the_command_line():
    assert acceptance_checks(list(SWITCH_WARNING)) == list(SWITCH_WARNING)
    assert set(SWITCH_WARNING).isdisjoint(ACCEPTANCE_CHECKS)
    with pytest.raises(ValueError):
        acceptance_checks(list(SWITCH_WARNING) + ['library'])
    from app.backup.manage import parser
    with pytest.raises(SystemExit):
        parser().parse_args(['prepare-activation', '--accepted-check', SWITCH_WARNING[0]])


def test_the_switch_warning_goes_only_with_the_old_library_given_up(tmp_path):
    from app.migration.activation import prepare_activation
    with pytest.raises(ValueError, match='goes with source_unavailable'):
        prepare_activation(tmp_path, b'k', tmp_path, None, tmp_path, tmp_path, tmp_path / 'intent',
                           expected_cluster_directory=tmp_path, accepted=list(SWITCH_WARNING), source_unavailable=False,
                           hostname='127.0.0.1', port=4341)


def test_the_mac_service_refuses_an_unfinished_restore(tmp_path, monkeypatch):
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config/local.json').write_text(json.dumps(
        {'host': '127.0.0.1', 'port': 4341, 'database': {'host': '127.0.0.1', 'port': 5433},
         'recovery': {'state': 'restoring', 'activation_required': True}}))
    monkeypatch.setattr(manage, 'ROOT', tmp_path)
    with pytest.raises(manage.BackupRunning, match='restore into this installation is not finished'):
        manage.settings()


def restore_args(tmp_path, **values):
    backup = tmp_path / 'disk/backup'
    (backup / 'catalog').mkdir(parents=True, exist_ok=True)
    from app.backup import passwords
    from app.backup.encrypted import seal_stream
    if not (backup / 'password.json').exists():
        key = passwords.new_key(passwords.BackupPassword('fixture administrator password'))
        passwords.publish(backup, key)
        seal_stream(io.BytesIO(b'index'), backup / 'library', key, estimated_bytes=64)
    return SimpleNamespace(**{'backup': backup, 'media': 'copy',
                              'media_folder': tmp_path / 'FamilyFoto Library', 'allow_unprotected_media': False,
                              'people_port': None, 'tls_name': None, **values})


def other_disk(monkeypatch, backup):
    from app.migration import media_folder
    monkeypatch.setattr(media_folder, 'volume_identity',
                        lambda path: 'backup-disk' if path.is_relative_to(backup.parent) else 'mac-disk')


def test_in_place_on_another_disk_is_refused_before_anything_is_installed(tmp_path, monkeypatch):
    """The November slip: in place, with the library folder chosen on the Mac."""
    args = restore_args(tmp_path, media='in-place')
    other_disk(monkeypatch, args.backup)
    with pytest.raises(SystemExit, match="on the backup's disk.*No installation started"):
        workflow.check_restore_choices(args)
    workflow.check_restore_choices(restore_args(tmp_path, media='in-place',
                                                media_folder=args.backup.parent / 'FamilyFoto Library'))
    workflow.check_restore_choices(restore_args(tmp_path))  # A copy may go anywhere.


def test_the_person_is_asked_again_after_a_folder_on_the_wrong_disk(tmp_path, monkeypatch, capsys):
    args = restore_args(tmp_path, media='in-place')
    other_disk(monkeypatch, args.backup)
    monkeypatch.setattr(workflow, 'interactive', lambda: True)
    answers = ['2']
    monkeypatch.setattr('builtins.input', lambda prompt: answers.pop(0))
    monkeypatch.setattr(workflow, 'choose_folder', lambda prompt: tmp_path)
    chosen = workflow.ask_restore(args)
    assert chosen.media == 'copy' and chosen.media_folder == tmp_path / 'FFLibrary'
    assert 'on another disk' in capsys.readouterr().out


def test_an_unprotected_disk_is_accepted_only_when_the_person_says_so(tmp_path, monkeypatch):
    from app.backup.private_storage import UnprotectedDestination
    from app.migration import media_folder

    def exfat(parent, allow_unprotected=False):
        if not allow_unprotected:
            raise UnprotectedDestination('exFAT')
    monkeypatch.setattr(media_folder, 'check_destination_privacy', exfat)
    with pytest.raises(SystemExit, match='--allow-unprotected-media'):
        workflow.check_restore_choices(restore_args(tmp_path))
    monkeypatch.setattr(workflow, 'interactive', lambda: True)
    monkeypatch.setattr('builtins.input', lambda prompt: 'yes')
    assert workflow.ask_restore(restore_args(tmp_path)).allow_unprotected_media is True


def test_an_existing_library_folder_is_refused(tmp_path):
    args = restore_args(tmp_path)
    args.media_folder.mkdir()
    with pytest.raises(SystemExit, match='already exists'):
        workflow.check_restore_choices(args)


def test_the_plan_refuses_the_folder_before_the_journal_is_written(tmp_path, monkeypatch):
    from app.migration.media_folder import UnsuitableMediaFolder
    from app.migration.move_in import make_plan
    args = restore_args(tmp_path)
    other_disk(monkeypatch, args.backup)
    with pytest.raises(UnsuitableMediaFolder, match='on another disk'):
        make_plan(args.backup, 'the administrator password', 'in-place', args.media_folder, people='skip')


def test_the_one_line_command_uses_short_flags_and_never_the_key(tmp_path):
    args = restore_args(tmp_path, people_port=54400, tls_name=['mac.lan'])
    line = workflow.one_line(args, 'install')
    assert line.startswith('bash installer/macos/install.sh restore -b ')
    assert "-k " not in line and ' -m copy ' in line
    assert f"-f {shlex.quote(str(args.media_folder))}" in line and "'" in line  # The folder's space is quoted.
    assert '--with-people --research-only --people-port 54400 --tls-name mac.lan' in line
    assert workflow.one_line(args, 'skip').endswith('--without-people --tls-name mac.lan')


def test_the_short_flags_are_the_long_ones():
    args = workflow.parser().parse_args(['restore', '-b', '/B', '-m', 'copy', '-f', '/F', '--without-people'])
    assert (args.backup, args.media, args.media_folder) == (Path('/B'), 'copy', Path('/F'))


def test_a_restore_that_stopped_before_it_began_carries_on_without_installing_core(tmp_path, monkeypatch):
    (tmp_path / 'runtime').mkdir()
    (tmp_path / 'runtime/macos-database-ready.json').write_text('{}')
    monkeypatch.setattr(workflow, 'ROOT', tmp_path)
    args = restore_args(tmp_path)
    monkeypatch.setattr('sys.argv', ['workflow.py', 'restore', '-b', str(args.backup),
                                     '-m', 'copy', '-f', str(args.media_folder), '--without-people'])
    monkeypatch.setattr(workflow.sys, 'platform', 'darwin')
    monkeypatch.setattr(workflow.platform, 'machine', lambda: 'arm64')
    monkeypatch.setattr(workflow.os, 'geteuid', lambda: 501, raising=False)
    monkeypatch.setattr(workflow, 'interactive', lambda: False)
    monkeypatch.setattr(workflow.prepare, 'prepare', lambda *a, **k: pytest.fail('Core installed again'))
    ran = []
    monkeypatch.setattr(workflow, 'run_restore', lambda args, people, password: ran.append((people, password)) or 0)
    monkeypatch.setattr(workflow.passwords, 'prompt', lambda: 'fixture administrator password')
    assert workflow.main() == 0 and ran == [('skip', 'fixture administrator password')]


def test_the_database_setup_gives_no_advice_to_start_it_by_hand():
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / 'installer/macos/manage.py').read_text(encoding='utf-8')
    assert "'--no-instructions'" in source


def test_running_says_which_server_has_the_address(capsys):
    manage.print_running({'port': 4341, 'database': {'port': 5433}})
    out = capsys.readouterr().out
    assert 'Web server: running. Open https://127.0.0.1:4341' in out
    assert 'Database server: running' in out and 'not a web address' in out


def test_a_disk_that_goes_away_is_a_plain_message_not_a_traceback(tmp_path, monkeypatch):
    from app.migration import media_folder

    def gone(parent, allow_unprotected=False):
        raise OSError(6, 'Device not configured')
    monkeypatch.setattr(media_folder, 'check_destination_privacy', gone)
    with pytest.raises(SystemExit, match='could not check .*Device not configured.*disk is connected'):
        workflow.check_restore_choices(restore_args(tmp_path))


def test_terminal_switch_confirmation_uses_a_separate_handle(monkeypatch):
    from installer.macos import restore
    from contextlib import contextmanager
    @contextmanager
    def tty(path, mode):
        assert path == '/dev/tty' and mode in ('r', 'w')
        yield SimpleNamespace(write=lambda text: None, flush=lambda: None, readline=lambda: 'switch\n')
    monkeypatch.setattr('builtins.open', tty)
    assert restore.confirm_switch(False, terminal=True)({})
